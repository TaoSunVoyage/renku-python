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
"""Integration tests for non-dataset CLI command."""
from pathlib import Path

import pytest
from flaky import flaky

from renku.cli import cli
from renku.core.commands.clone import project_clone_command
from renku.core.utils.contexts import chdir
from tests.utils import format_result_exception


@pytest.mark.integration
@flaky(max_runs=10, min_passes=1)
@pytest.mark.parametrize("url", ["https://dev.renku.ch/gitlab/renku-testing/project-9"])
def test_renku_clone(runner, monkeypatch, url):
    """Test cloning of a Renku repo and existence of required settings."""
    from renku.core.management.storage import StorageApiMixin

    with runner.isolated_filesystem() as project_path:
        result = runner.invoke(cli, ["clone", url, project_path])
        assert 0 == result.exit_code, format_result_exception(result) + str(result.stderr_bytes)
        assert (Path(project_path) / "Dockerfile").exists()

        # Check Git hooks are installed
        result = runner.invoke(cli, ["githooks", "install"])
        assert 0 == result.exit_code, format_result_exception(result) + str(result.stderr_bytes)
        assert "Hook already exists." in result.output

        result = runner.invoke(cli, ["migrate"])
        assert 0 == result.exit_code, format_result_exception(result) + str(result.stderr_bytes)

        # Check Git LFS is enabled
        with monkeypatch.context() as monkey:
            # Pretend that git-lfs is not installed.
            monkey.setattr(StorageApiMixin, "storage_installed", False)
            # Repo is using external storage but it's not installed.
            result = runner.invoke(cli, ["run", "touch", "output"])

            assert "External storage is not configured" in result.output
            assert 1 == result.exit_code, result.output + str(result.stderr_bytes)


@pytest.mark.integration
@flaky(max_runs=10, min_passes=1)
@pytest.mark.parametrize("url", ["https://dev.renku.ch/gitlab/renku-testing/project-9"])
def test_renku_clone_with_config(tmp_path, url):
    """Test cloning of a Renku repo and existence of required settings."""
    with chdir(tmp_path):
        repo, _ = (
            project_clone_command()
            .build()
            .execute(url, config={"user.name": "sam", "user.email": "s@m.i", "filter.lfs.custom": "0"})
        ).output

        assert "master" == repo.active_branch.name
        reader = repo.config_reader()
        reader.values()

        lfs_config = dict(reader.items("filter.lfs"))
        assert "0" == lfs_config.get("custom")


@pytest.mark.integration
@flaky(max_runs=10, min_passes=1)
@pytest.mark.parametrize("url", ["https://dev.renku.ch/gitlab/renku-testing/project-9"])
def test_renku_clone_checkout_rev(tmp_path, url):
    """Test cloning of a repo checking out a rev with static config."""
    with chdir(tmp_path):
        repo, _ = (
            project_clone_command()
            .build()
            .execute(
                url,
                config={"user.name": "sam", "user.email": "s@m.i", "filter.lfs.custom": "0"},
                checkout_rev="97f907e1a3f992d4acdc97a35df73b8affc917a6",
            )
        ).output

        assert "97f907e1a3f992d4acdc97a35df73b8affc917a6" == str(repo.head.commit)
        reader = repo.config_reader()
        reader.values()

        lfs_config = dict(reader.items("filter.lfs"))
        assert "0" == lfs_config.get("custom")


@pytest.mark.integration
@flaky(max_runs=10, min_passes=1)
@pytest.mark.parametrize("rev,detached", [("test-branch", False), ("my-tag", True)])
def test_renku_clone_checkout_revs(tmp_path, rev, detached):
    """Test cloning of a Renku repo checking out a rev."""
    with chdir(tmp_path):
        repo, _ = (
            project_clone_command()
            .build()
            .execute("https://dev.renku.ch/gitlab/renku-python-integration-tests/no-renku.git", checkout_rev=rev)
        ).output

        if detached:
            # NOTE: cloning a tag sets head to the commit of the tag, get tag that the head commit points to
            assert rev == repo.git.describe("--tags", repo.head.commit)
        else:
            assert rev == repo.head.ref.name


@pytest.mark.integration
@pytest.mark.parametrize("path,expected_path", [("", "project-9"), (".", "."), ("new-name", "new-name")])
@flaky(max_runs=10, min_passes=1)
def test_renku_clone_uses_project_name(runner, path, expected_path):
    """Test renku clone uses project name as target-path by default."""
    remote = "https://dev.renku.ch/gitlab/renku-testing/project-9"

    with runner.isolated_filesystem() as project_path:
        result = runner.invoke(cli, ["clone", remote, path])
        assert 0 == result.exit_code, format_result_exception(result) + str(result.stderr_bytes)
        assert (Path(project_path) / expected_path / "Dockerfile").exists()


@pytest.mark.integration
@flaky(max_runs=10, min_passes=1)
def test_renku_clone_private_project_error(runner):
    """Test renku clone prints proper error message when a project is private."""
    remote = "git@dev.renku.ch:mohammad.alisafaee/test-private-project.git"

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["clone", remote, ""])

        assert 0 != result.exit_code
        assert "Please make sure you have the correct access rights" in result.output
        assert "and the repository exists." in result.output
