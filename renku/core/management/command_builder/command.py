# -*- coding: utf-8 -*-
#
# Copyright 2018-2021 - Swiss Data Science Center (SDSC)
# A partnership between École Polytechnique Fédérale de Lausanne (EPFL) and
# Eidgenössische Technische Hochschule Zürich (ETHZ).
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Command builder."""

import contextlib
import functools
import threading
import typing
from collections import defaultdict

import click
import inject

from renku.core import errors
from renku.core.management.config import RENKU_HOME
from renku.core.utils.communication import CommunicationCallback

_LOCAL = threading.local()


def check_finalized(f):
    """Decorator to prevent modification of finalized builders."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        """Decorator to prevent modification of finalized builders."""
        if not args or not isinstance(args[0], Command):
            raise errors.ParameterError("Command hooks need to be `Command` object methods.")

        if args[0].finalized:
            raise errors.CommandFinalizedError("Cannot modify a finalized `Command`.")

        return f(*args, **kwargs)

    return wrapper


def _patched_get_injector_or_die() -> inject.Injector:
    """Patched version of get_injector_or_die with thread local injectors.

    Allows deferred definition of an injector per thread.
    """
    injector = getattr(_LOCAL, "injector", None)
    if not injector:
        raise inject.InjectorException("No injector is configured")

    return injector


def _patched_configure(
    config: typing.Optional[inject.BinderCallable] = None, bind_in_runtime: bool = True
) -> inject.Injector:
    """Create an injector with a callable config or raise an exception when already configured."""

    if getattr(_LOCAL, "injector", None):
        raise inject.InjectorException("Injector is already configured")

    _LOCAL.injector = inject.Injector(config, bind_in_runtime=bind_in_runtime)

    return _LOCAL.injector


inject.configure = _patched_configure
inject.get_injector_or_die = _patched_get_injector_or_die


def _bind_local_client(binder: inject.Binder, client):
    """Bind a LocalClient to an Injector."""
    from renku.core.management import LocalClient

    binder.bind(LocalClient, client)
    binder.bind("LocalClient", client)


def remove_injector():
    """Remove a thread-local injector."""
    if getattr(_LOCAL, "injector", None):
        del _LOCAL.injector


@contextlib.contextmanager
def replace_injected_client(new_client):
    """Temporarily inject a different client into dependency injection."""
    old_injector = getattr(_LOCAL, "injector", None)
    try:
        if old_injector:
            remove_injector()
        inject.configure(lambda binder: _bind_local_client(binder, new_client))

        yield
    finally:
        remove_injector()

        if old_injector:
            _LOCAL.injector = old_injector


@contextlib.contextmanager
def replace_injection(bindings, constructor_bindings=None):
    """Temporarily inject various test objects."""
    constructor_bindings = constructor_bindings or {}

    def bind(binder):
        for key, value in bindings.items():
            binder.bind(key, value)
        for key, value in constructor_bindings.items():
            binder.bind_to_constructor(key, value)

    old_injector = getattr(_LOCAL, "injector", None)
    try:
        if old_injector:
            remove_injector()
        inject.configure(bind)

        yield
    finally:
        remove_injector()

        if old_injector:
            _LOCAL.injector = old_injector


def update_injected_client(new_client):
    """Update the injected client instance.

    Necessary because we sometimes use attr.evolve to modify a client and this doesn't affect the injected instance.
    """
    from renku.core.management import LocalClient

    injector = getattr(_LOCAL, "injector", None)

    if not injector:
        raise inject.InjectorException("No injector is configured")

    injector._bindings[LocalClient] = lambda: new_client
    injector._bindings["LocalClient"] = lambda: new_client


class Command:
    """Base renku command builder."""

    CLIENT_HOOK_ORDER = 1

    def __init__(self) -> None:
        """__init__ of Command."""
        self.pre_hooks = defaultdict(list)
        self.post_hooks = defaultdict(list)
        self._operation = None
        self._finalized = False
        self._track_std_streams = False
        self._working_directory = None

    def __getattr__(self, name: str) -> typing.Any:
        """Bubble up attributes of wrapped builders."""
        if "_builder" in self.__dict__:
            return getattr(self._builder, name)

        raise AttributeError(f"{self.__class__.__name__} object has no attribute {name}")

    def __setattr__(self, name: str, value: typing.Any) -> None:
        """Set attributes of wrapped builders."""
        if hasattr(self, "_builder") and self.__class__ is not self._builder.__class__:
            self._builder.__setattr__(name, value)

        object.__setattr__(self, name, value)

    def _pre_hook(self, builder: "Command", context: dict, *args, **kwargs) -> None:
        """Setup local client."""
        from renku.core.management import LocalClient
        from renku.core.management.repository import default_path

        ctx = click.get_current_context(silent=True)
        if ctx is None:
            client = LocalClient(
                path=default_path(self._working_directory or "."),
                renku_home=RENKU_HOME,
                external_storage_requested=True,
            )
            ctx = click.Context(click.Command(builder._operation))
        else:
            client = ctx.ensure_object(LocalClient)

        stack = contextlib.ExitStack()

        context["bindings"] = {LocalClient: client, "LocalClient": client}
        context["constructor_bindings"] = {}
        context["client"] = client
        context["stack"] = stack
        context["click_context"] = ctx

    def _post_hook(self, builder: "Command", context: dict, result: "CommandResult", *args, **kwargs) -> None:
        """Post-hook method."""
        remove_injector()

        if result.error:
            raise result.error

    def execute(self, *args, **kwargs) -> "CommandResult":
        """Execute the wrapped operation.

        First executes `pre_hooks` in ascending `order`, passing a read/write context between them.
        It then calls the wrapped `operation`. The result of the operation then gets pass to all the `post_hooks`,
        but in descending `order`. It then returns the result or error if there was one.
        """
        if not self.finalized:
            raise errors.CommandNotFinalizedError("Call `build()` before executing a command")

        context = {}
        if any(self.pre_hooks):
            order = sorted(self.pre_hooks.keys())

            for o in order:
                for hook in self.pre_hooks[o]:
                    hook(self, context, *args, **kwargs)

        output = None
        error = None

        def _bind(binder):
            for key, value in context["bindings"].items():
                binder.bind(key, value)
            for key, value in context["constructor_bindings"].items():
                binder.bind_to_constructor(key, value)

            return binder

        inject.configure(_bind)

        try:
            with context["stack"]:
                output = context["click_context"].invoke(self._operation, *args, **kwargs)
        except errors.RenkuException as e:
            error = e
        except (Exception, BaseException):
            remove_injector()
            raise

        result = CommandResult(output, error, CommandResult.FAILURE if error else CommandResult.SUCCESS)

        if any(self.post_hooks):
            order = sorted(self.post_hooks.keys(), reverse=True)

            for o in order:
                for hook in self.post_hooks[o]:
                    hook(self, context, result, *args, **kwargs)

        return result

    @property
    def finalized(self) -> bool:
        """Whether this builder is still being constructed or has been finalized."""
        if hasattr(self, "_builder"):
            return self._builder.finalized
        return self._finalized

    @check_finalized
    def add_pre_hook(self, order: int, hook: typing.Callable):
        """Add a pre-execution hook.

        :param order: Determines the order of executed hooks, lower numbers get executed first.
        :param hook: The hook to add
        """
        if hasattr(self, "_builder"):
            self._builder.add_pre_hook(order, hook)
        else:
            self.pre_hooks[order].append(hook)

    @check_finalized
    def add_post_hook(self, order: int, hook: typing.Callable):
        """Add a post-execution hook.

        :param order: Determines the order of executed hooks, lower numbers get executed first.
        :param hook: The hook to add
        """
        if hasattr(self, "_builder"):
            self._builder.add_post_hook(order, hook)
        else:
            self.post_hooks[order].append(hook)

    @check_finalized
    def build(self) -> "Command":
        """Build (finalize) the command."""
        if not self._operation:
            raise errors.ConfigurationError("`Command` needs to have a wrapped `command` set")
        self.add_pre_hook(self.CLIENT_HOOK_ORDER, self._pre_hook)
        self.add_post_hook(self.CLIENT_HOOK_ORDER, self._post_hook)

        self._finalized = True

        return self

    @check_finalized
    def command(self, operation: typing.Callable):
        """Set the wrapped command.

        :param operation: The function to wrap in the command builder.
        """
        self._operation = operation

        return self

    @check_finalized
    def working_directory(self, directory: str) -> "Command":
        """Set the working directory for the command.

        :param directory: The working directory to work in.
        """
        self._working_directory = directory

        return self

    @check_finalized
    def track_std_streams(self) -> "Command":
        """Whether to track STD streams or not."""
        self._track_std_streams = True

        return self

    @check_finalized
    def with_git_isolation(self) -> "Command":
        """Whether to run in git isolation or not."""
        from renku.core.management.command_builder.repo import Isolation

        return Isolation(self)

    @check_finalized
    def with_commit(
        self, message: str = None, commit_if_empty: bool = False, raise_if_empty: bool = False, commit_only: bool = None
    ) -> "Command":
        """Create a commit.

        :param message: The commit message. Autogenerated if left empty.
        :param commit_if_empty: Whether to commit if there are no modified files .
        :param raise_if_empty: Whether to raise an exception if there are no modified files.
        :param commit_only: Only commit the supplied paths.
        """
        from renku.core.management.command_builder.repo import Commit

        return Commit(self, message, commit_if_empty, raise_if_empty, commit_only)

    @check_finalized
    def lock_project(self) -> "Command":
        """Acquire a lock for the whole project."""
        from renku.core.management.command_builder.lock import ProjectLock

        return ProjectLock(self)

    @check_finalized
    def lock_dataset(self) -> "Command":
        """Acquire a lock for a dataset."""
        from renku.core.management.command_builder.lock import DatasetLock

        return DatasetLock(self)

    @check_finalized
    def require_migration(self) -> "Command":
        """Check if a migration is needed."""
        from renku.core.management.command_builder.migration import RequireMigration

        return RequireMigration(self)

    @check_finalized
    def require_clean(self) -> "Command":
        """Check that the repository is clean."""
        from renku.core.management.command_builder.repo import RequireClean

        return RequireClean(self)

    @check_finalized
    def require_nodejs(self) -> "Command":
        """Ensure nodejs is installed."""
        from renku.core.management.command_builder.nodejs import RequireNodeJs

        return RequireNodeJs(self)

    @check_finalized
    def with_communicator(self, communicator: CommunicationCallback) -> "Command":
        """Create a communicator."""
        from renku.core.management.command_builder.communication import Communicator

        return Communicator(self, communicator)

    @check_finalized
    def with_database(self, write: bool = False, path: str = None, create: bool = False) -> "Command":
        """Provide an object database connection."""
        from renku.core.management.command_builder.database import DatabaseCommand

        return DatabaseCommand(self, write, path, create)


class CommandResult:
    """The result of a command.

    The return value of the command is set as `.output`, if there was an error, it is set as `.error`, and
    the status of the command is set to either `CommandResult.SUCCESS` or CommandResult.FAILURE`.
    """

    SUCCESS = 0

    FAILURE = 1

    def __init__(self, output, error, status) -> None:
        """__init__ of CommandResult."""
        self.output = output
        self.error = error
        self.status = status
