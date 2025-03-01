# -*- coding: utf-8 -*-
#
# Copyright 2017-2021- Swiss Data Science Center (SDSC)
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
"""Test workflow commands."""

from renku.cli import cli
from tests.utils import format_result_exception


def test_workflow_remove_command(runner, project):
    """test workflow remove with builder."""
    workflow_name = "test_workflow"

    result = runner.invoke(cli, ["graph", "workflow", "remove", workflow_name])
    assert 2 == result.exit_code

    result = runner.invoke(cli, ["run", "--success-code", "0", "--no-output", "--name", workflow_name, "echo", "foo"])
    assert 0 == result.exit_code, format_result_exception(result)

    result = runner.invoke(cli, ["graph", "workflow", "remove", "--force", workflow_name])
    assert 0 == result.exit_code, format_result_exception(result)
