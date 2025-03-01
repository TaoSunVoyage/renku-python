# -*- coding: utf-8 -*-
#
# Copyright 2017-2021 - Swiss Data Science Center (SDSC)
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
"""Command line options."""

import click

from renku.core.errors import RenkuException

from .git import set_git_isolation


def install_completion(ctx, attr, value):  # pragma: no cover
    """Install completion for the current shell."""
    import click_completion.core

    if not value or ctx.resilient_parsing:
        return value

    shell, path = click_completion.core.install()
    click.secho("{0} completion installed in {1}".format(shell, path), fg="green")
    ctx.exit()


option_isolation = click.option(
    "--isolation",
    is_flag=True,
    default=False,
    callback=lambda ctx, param, value: set_git_isolation(value),
    help="Set up the isolation for invoking of the given command.",
)


def check_siblings(graph, outputs):
    """Check that all outputs have their siblings listed."""
    siblings = set()
    for node in outputs:
        siblings |= graph.siblings(node)

    siblings = {node.path for node in siblings}
    missing = siblings - {node.path for node in outputs}
    missing = {m for m in missing if all(not m.startswith(node.path) for node in outputs)}

    if missing:
        msg = "Include the files above in the command " "or use the --with-siblings option."
        raise RenkuException(
            "There are missing output siblings:\n\n"
            "\t{0}\n\n{1}".format("\n\t".join(click.style(path, fg="red") for path in missing), msg)
        )
    return outputs


def with_siblings(graph, outputs):
    """Include all missing siblings."""
    siblings = set()
    for node in outputs:
        siblings |= graph.siblings(node)
    return siblings


option_check_siblings = click.option(
    "--check-siblings",
    "siblings",
    flag_value=check_siblings,
    default=True,
    help=check_siblings.__doc__,
    type=click.types.UnprocessedParamType(),
)
option_with_siblings = click.option(
    "--with-siblings",
    "siblings",
    flag_value=with_siblings,
    default=True,
    help=with_siblings.__doc__,
    type=click.types.UnprocessedParamType(),
)


def option_siblings(func):
    """Combine siblings options."""
    return option_check_siblings(option_with_siblings(func))


option_external_storage_requested = click.option(
    "external_storage_requested",
    "--external-storage/--no-external-storage",
    " /-S",
    is_flag=True,
    default=True,
    help="Use an external file storage service.",
)
