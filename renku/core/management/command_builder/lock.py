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
"""Command builder for locking."""

from renku.core.management.command_builder.command import Command, check_finalized


class ProjectLock(Command):
    """Builder to get a project wide lock."""

    DEFAULT_ORDER = 5

    def __init__(self, builder: Command) -> None:
        """__init__ of ProjectLock."""
        self._builder = builder

    def _pre_hook(self, builder: Command, context: dict, *args, **kwargs) -> None:
        """Lock the project."""
        if "client" not in context:
            raise ValueError("Commit builder needs a LocalClient to be set.")
        if "stack" not in context:
            raise ValueError("Commit builder needs a stack to be set.")

        context["stack"].enter_context(context["client"].lock)

    @check_finalized
    def build(self) -> Command:
        """Build the command."""
        self._builder.add_pre_hook(self.DEFAULT_ORDER, self._pre_hook)

        return self._builder.build()


class DatasetLock(Command):
    """Builder to lock on a dataset."""

    DEFAULT_ORDER = 5

    def __init__(self, builder: Command) -> None:
        """__init__ of DatasetLock."""
        self._builder = builder

    def _pre_hook(self, builder: Command, context: dict, *args, **kwargs) -> None:
        if "client" not in context:
            raise ValueError("Commit builder needs a LocalClient to be set.")
        if "stack" not in context:
            raise ValueError("Commit builder needs a stack to be set.")

        context["stack"].enter_context(context["client"].lock)

    @check_finalized
    def build(self) -> Command:
        """Build the command."""
        self._builder.add_pre_hook(self.DEFAULT_ORDER, self._pre_hook)

        return self._builder.build()
