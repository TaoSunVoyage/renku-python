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
"""Client for handling datasets."""

import concurrent.futures
import fnmatch
import imghdr
import os
import re
import shlex
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from subprocess import PIPE, SubprocessError
from typing import Dict, List, Optional
from urllib import error, parse
from urllib.parse import ParseResult, unquote, urlparse
from urllib.request import urlretrieve

import attr
import patoolib
import requests
from git import GitCommandError, GitError, Repo
from inject import InjectorException
from wcmatch import glob
from yagup import GitURL

from renku.core import errors
from renku.core.management.clone import clone
from renku.core.management.command_builder import inject
from renku.core.management.config import RENKU_HOME
from renku.core.management.repository import RepositoryApiMixin
from renku.core.metadata.database import Database
from renku.core.metadata.immutable import DynamicProxy
from renku.core.models import dataset as new_datasets
from renku.core.models.dataset import DatasetsProvenance, get_dataset_data_dir, is_dataset_name_valid
from renku.core.models.provenance.agent import Person
from renku.core.models.refs import LinkReference
from renku.core.utils import communication
from renku.core.utils.git import (
    add_to_git,
    find_previous_commit,
    get_oauth_url,
    get_object_hash,
    get_renku_repo_url,
    have_same_remote,
    run_command,
)
from renku.core.utils.urls import get_slug, remove_credentials


@attr.s
class DatasetsApiMixin(object):
    """Client for handling datasets."""

    DATASETS = "datasets"
    """Directory for storing dataset metadata in Renku."""

    POINTERS = "pointers"
    """Directory for storing external pointer files."""

    CACHE = "cache"
    """Directory to cache transient data."""

    DATASET_IMAGES = "dataset_images"
    """Directory for dataset images."""

    DATASETS_PROVENANCE = "dataset.json"
    """File for storing datasets' provenance."""

    SUPPORTED_SCHEMES = ("", "file", "http", "https", "git+https", "git+ssh")

    _temporary_datasets_path = None
    _datasets_provenance = None
    _database = None

    @property
    def renku_datasets_path(self):
        """Return a ``Path`` instance of Renku dataset metadata folder."""
        if self._temporary_datasets_path:
            return self._temporary_datasets_path

        return self.path / self.renku_home / self.DATASETS

    @property
    def renku_dataset_images_path(self):
        """Return a ``Path`` instance of Renku dataset metadata folder."""
        if self._temporary_datasets_path:
            return self._temporary_datasets_path

        return self.path / self.renku_home / self.DATASET_IMAGES

    @property
    def datasets_provenance_path(self):
        """Path to store activity files."""
        return self.renku_path / self.DATASETS_PROVENANCE

    def set_temporary_datasets_path(self, path):
        """Set path to Renku dataset metadata directory."""
        self._temporary_datasets_path = path

    def clear_temporary_datasets_path(self):
        """Clear path to Renku dataset metadata directory."""
        self._temporary_datasets_path = None

    def is_using_temporary_datasets_path(self):
        """Return true if temporary datasets path is set."""
        return bool(self._temporary_datasets_path)

    @property
    def renku_pointers_path(self):
        """Return a ``Path`` instance of Renku pointer files folder."""
        path = self.path / self.renku_home / self.POINTERS
        path.mkdir(exist_ok=True)
        return path

    @property
    def datasets(self) -> Dict[str, new_datasets.Dataset]:
        """A map from datasets name to datasets."""
        database = Database.from_path(self.database_path)
        datasets_provenance = DatasetsProvenance(database)
        return {d.name: d for d in datasets_provenance.datasets}

    # FIXME: Remove this method and use proper injection
    def get_database(self) -> Database:
        """Return a Database instance."""

        @inject.autoparams()
        def get_injected_database(database: Database):
            return database

        if not self._database:
            try:
                self._database = get_injected_database()
            except InjectorException:
                self._database = Database.from_path(self.database_path)

        return self._database

    def get_datasets_provenance(self) -> DatasetsProvenance:
        """Return a DatasetsProvenance instance."""

        @inject.autoparams()
        def get_injected_datasets_provenance(datasets_provenance: DatasetsProvenance):
            return datasets_provenance

        if not self._datasets_provenance:
            try:
                self._datasets_provenance = get_injected_datasets_provenance()
            except InjectorException:
                database = self.get_database()
                self._datasets_provenance = DatasetsProvenance(database)

        return self._datasets_provenance

    def get_dataset(self, name, strict=False, immutable=False) -> Optional[new_datasets.Dataset]:
        """Load dataset reference file."""
        datasets_provenance = self.get_datasets_provenance()

        dataset = datasets_provenance.get_by_name(name, immutable=immutable)

        if not dataset and strict:
            raise errors.DatasetNotFound(name=name)

        return dataset

    @contextmanager
    def with_dataset(self, name=None, create=False, commit_database=False, creator: Person = None):
        """Yield an editable metadata object for a dataset."""
        dataset = self.get_dataset(name=name)

        if dataset is None:
            if not create:
                raise errors.DatasetNotFound(name=name)

            # NOTE: Don't update provenance when creating here because it will be updated later
            dataset = self.create_dataset(name=name, update_provenance=False)
        elif create:
            raise errors.DatasetExistsError('Dataset exists: "{}".'.format(name))

        try:
            yield dataset
        except Exception:
            # TODO use a general clean-up strategy: https://github.com/SwissDataScienceCenter/renku-python/issues/736
            raise

        if commit_database:
            self.get_datasets_provenance().add_or_update(dataset, creator=creator)
            self.get_database().commit()

    @inject.autoparams()
    def create_dataset(
        self,
        datasets_provenance: DatasetsProvenance,
        name=None,
        title=None,
        description=None,
        creators=None,
        keywords=None,
        images=None,
        safe_image_paths=None,
        update_provenance=True,
    ):
        """Create a dataset."""
        if not name:
            raise errors.ParameterError("Dataset name must be provided.")

        if not is_dataset_name_valid(name):
            valid_name = get_slug(name)
            raise errors.ParameterError(f'Dataset name "{name}" is not valid (Hint: "{valid_name}" is valid).')

        if self.get_dataset(name=name):
            raise errors.DatasetExistsError(f"Dataset exists: '{name}'")

        if not title:
            title = name

        if creators is None:
            creators = [Person.from_git(self.repo)]

        keywords = keywords or ()

        dataset = new_datasets.Dataset(
            identifier=None, name=name, title=title, description=description, creators=creators, keywords=keywords
        )

        if images:
            safe_image_paths = safe_image_paths or []
            safe_image_paths.append(self.path)
            self.set_dataset_images(dataset, images, safe_image_paths)

        if update_provenance:
            datasets_provenance.add_or_update(dataset)

        return dataset

    def set_dataset_images(self, dataset: new_datasets.Dataset, images, safe_image_paths=None):
        """Set the images on a dataset."""
        safe_image_paths = safe_image_paths or []

        if not images:
            images = []

        image_folder = self.renku_dataset_images_path / dataset.initial_identifier
        image_folder.mkdir(exist_ok=True, parents=True)

        previous_images = dataset.images or []

        dataset.images = []

        images_updated = False

        for img in images:
            position = img["position"]
            content_url = img["content_url"]

            if any(i.position == img["position"] for i in dataset.images):
                raise errors.DatasetImageError(f"Duplicate dataset image specified for position {position}")

            existing = next(
                (i for i in previous_images if i.position == img["position"] and i.content_url == img["content_url"]),
                None,
            )

            if existing:
                dataset.images.append(existing)
                continue
            image_type = None
            if urlparse(content_url).netloc:
                # NOTE: absolute url
                if not img.get("mirror_locally", False):
                    dataset.images.append(
                        new_datasets.ImageObject(
                            content_url=content_url,
                            position=position,
                            id=new_datasets.ImageObject.generate_id(dataset, position),
                        )
                    )
                    images_updated = True
                    continue

                # NOTE: mirror the image locally
                try:
                    path, _ = urlretrieve(content_url)
                except error.URLError as e:
                    raise errors.DatasetImageError(f"Dataset image with url {content_url} couldn't be mirrored") from e

                image_type = imghdr.what(path)
                if image_type:
                    image_type = f".{image_type}"

                content_url = path
                safe_image_paths.append(Path(path).parent)

            path = content_url
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(self.path, path))

            if not os.path.exists(path) or not any(os.path.commonprefix([path, p]) == str(p) for p in safe_image_paths):
                # NOTE: make sure files exists and prevent path traversal
                raise errors.DatasetImageError(f"Dataset image with relative path {content_url} not found")

            if not path.startswith(str(image_folder)):
                # NOTE: only copy dataset image if it's not in .renku/datasets/<id>/images/ already
                if image_type:
                    ext = image_type
                else:
                    _, ext = os.path.splitext(content_url)

                img_path = image_folder / f"{position}{ext}"
                shutil.copy(path, img_path)
            else:
                img_path = path

            dataset.images.append(
                new_datasets.ImageObject(
                    content_url=str(img_path.relative_to(self.path)),
                    position=position,
                    id=new_datasets.ImageObject.generate_id(dataset=dataset, position=position),
                )
            )
            images_updated = True

        new_urls = [i.content_url for i in dataset.images]

        for prev in previous_images:
            # NOTE: Delete images if they were removed
            if prev.content_url in new_urls or urlparse(prev.content_url).netloc:
                continue

            path = prev.content_url
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(self.path, path))

            os.remove(path)

        return images_updated or dataset.images != previous_images

    @inject.autoparams()
    def add_data_to_dataset(
        self,
        dataset,
        urls,
        datasets_provenance: DatasetsProvenance,
        force=False,
        overwrite=False,
        sources=(),
        destination="",
        ref=None,
        external=False,
        extract=False,
        all_at_once=False,
        destination_names=None,
        repository=None,
        clear_files_before=False,
    ):
        """Import the data into the data directory."""
        dataset_datadir = get_dataset_data_dir(self, dataset)

        destination = destination or Path(".")
        destination = self._resolve_path(dataset_datadir, destination)
        destination = self.path / dataset_datadir / destination

        if destination.exists() and not destination.is_dir():
            raise errors.ParameterError(f'Destination is not a directory: "{destination}"')

        self.check_external_storage()

        files = []
        if all_at_once:  # Importing a dataset
            files = self._add_from_urls(
                urls=urls, destination_names=destination_names, destination=destination, extract=extract
            )
        else:
            for url in urls:
                is_remote, is_git, url = _check_url(url)

                if is_git and is_remote:  # Remote git repo
                    sources = sources or ()
                    new_files = self._add_from_git(
                        url=url, sources=sources, destination=destination, ref=ref, repository=repository
                    )
                else:
                    if sources:
                        raise errors.UsageError('Cannot use "--source" with URLs or local files.')

                    if not is_remote:  # Local path, might be git
                        if is_git:
                            communication.warn(
                                "Adding data from local Git repository: "
                                + "Use remote's Git URL instead to enable "
                                + "lineage information and updates."
                            )
                        u = parse.urlparse(url)
                        new_files = self._add_from_local(
                            dataset=dataset, path=u.path, external=external, destination=destination
                        )
                    else:  # Remote URL
                        new_files = self._add_from_url(url=url, destination=destination, extract=extract)

                files.extend(new_files)

        # Remove all files that are under a .git directory
        paths_to_avoid = [f["path"] for f in files if ".git" in str(f["path"]).split(os.path.sep)]
        if paths_to_avoid:
            files = [f for f in files if f["path"] not in paths_to_avoid]
            communication.warn(
                "Ignored adding paths under a .git directory:\n  " + "\n  ".join(str(p) for p in paths_to_avoid)
            )

        files_to_commit = {str(self.path / f["path"]) for f in files}

        if not force:
            ignored_files = self.find_ignored_paths(*files_to_commit)
            if ignored_files:
                ignored_files = set(ignored_files)
                files_to_commit = files_to_commit.difference(ignored_files)
                ignored_sources = []
                for file_ in files:
                    if str(self.path / file_["path"]) in ignored_files:
                        operation = file_.get("operation")
                        if operation:
                            src, _, _ = operation
                            ignored_sources.append(src)
                        else:
                            ignored_sources.append(file_["path"])

                files = [f for f in files if str(self.path / f["path"]) in files_to_commit]
                communication.warn(
                    "Theses paths are ignored by one of your .gitignore "
                    + 'files (use "--force" flag if you really want to add '
                    + "them):\n  "
                    + "\n  ".join([str(p) for p in ignored_sources])
                )

        # all files at this point can be force-added

        if not overwrite:
            existing_files = []
            for path in files_to_commit:
                relative_path = Path(path).relative_to(self.path)
                if dataset.find_file(relative_path):
                    existing_files.append(path)

            if existing_files:
                files_to_commit = files_to_commit.difference(existing_files)
                files = [f for f in files if str(self.path / f["path"]) in files_to_commit]
                communication.warn(
                    "These existing files were not overwritten "
                    + '(use "--overwrite" flag to overwrite them):\n  '
                    + "\n  ".join([str(p) for p in existing_files])
                )

        for data in files:
            operation = data.pop("operation", None)
            if not operation:
                continue

            src, dst, action = operation

            # Remove existing file if any
            self.remove_file(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)

            if action == "copy":
                shutil.copy(src, dst)
            elif action == "move":
                shutil.move(src, dst, copy_function=shutil.copy)
            elif action == "symlink":
                self._create_external_file(src, dst)
                data["is_external"] = True
            else:
                raise errors.OperationError(f"Invalid action {action}")

        # Track non-symlinks in LFS
        if self.check_external_storage():
            self.track_paths_in_storage(*files_to_commit)

        # Force-add to include possible ignored files
        add_to_git(self.repo.git, *files_to_commit, force=True)
        self.repo.git.add(self.renku_pointers_path, force=True)

        staged_files = self.repo.index.diff("HEAD")
        if staged_files:
            msg = "renku dataset: committing {} newly added files".format(len(files_to_commit))
            skip_hooks = not self.external_storage_requested
            self.repo.index.commit(msg, skip_hooks=skip_hooks)
        else:
            communication.warn("No new file was added to project")

        if not files:
            return

        # Generate the DatasetFiles
        dataset_files = []
        for data in files:
            dataset_file = new_datasets.DatasetFile.from_path(
                client=self, path=data["path"], source=data["source"], based_on=data.get("based_on")
            )
            dataset_files.append(dataset_file)

        if clear_files_before:
            dataset.clear_files()
        dataset.add_or_update_files(dataset_files)

        datasets_provenance.add_or_update(dataset, creator=Person.from_client(self))

    def is_protected_path(self, path):
        """Checks if a path is a protected path."""
        try:
            path_in_repo = str(path.relative_to(self.path))
        except ValueError:
            return False

        for protected_path in self.RENKU_PROTECTED_PATHS:
            if fnmatch.fnmatch(path_in_repo, protected_path):
                return True

        return False

    def _add_from_local(self, dataset, path, external, destination):
        """Add a file or directory from a local filesystem."""
        src = Path(os.path.abspath(path))

        if not src.exists():
            raise errors.ParameterError(f"Cannot find file/directory: {path}")

        dst = destination / src.name

        # if we have a directory, recurse
        if src.is_dir():
            if dst.exists() and not dst.is_dir():
                raise errors.ParameterError(f'Cannot copy directory to a file: "{dst}"')
            if src == (self.path / get_dataset_data_dir(self, dataset)).resolve():
                raise errors.ParameterError(f"Cannot add dataset's data directory recursively: {path}")

            if self.is_protected_path(src):
                raise errors.ProtectedFiles([src])

            files = []
            for f in src.iterdir():
                files.extend(
                    self._add_from_local(dataset=dataset, path=os.path.abspath(f), external=external, destination=dst)
                )
            return files
        else:
            # Check if file is in the project and return it
            path_in_repo = None
            if self.is_external_file(src):
                path_in_repo = path
            else:
                try:
                    path_in_repo = src.relative_to(self.path)
                except ValueError:
                    pass
                else:
                    if self.is_protected_path(src):
                        raise errors.ProtectedFiles([src])

            if path_in_repo:
                return [{"path": path_in_repo, "source": path_in_repo, "parent": self}]

        action = "symlink" if external else "copy"
        return [
            {
                "path": dst.relative_to(self.path),
                "source": os.path.relpath(str(src), str(self.path)),
                "parent": self,
                "operation": (src, dst, action),
            }
        ]

    def _add_from_urls(self, urls, destination, destination_names, extract):
        listeners = communication.get_listeners()

        def subscribe_communication_listeners(function, **kwargs):
            try:
                for communicator in listeners:
                    communication.subscribe(communicator)
                return function(**kwargs)
            finally:
                for communicator in listeners:
                    communication.unsubscribe(communicator)

        files = []
        max_workers = min(os.cpu_count() - 1, 4) or 1
        with concurrent.futures.ThreadPoolExecutor(max_workers) as executor:
            futures = {
                executor.submit(
                    subscribe_communication_listeners,
                    self._add_from_url,
                    url=url,
                    destination=destination,
                    extract=extract,
                    filename=name,
                )
                for url, name in zip(urls, destination_names)
            }

            for future in concurrent.futures.as_completed(futures):
                files.extend(future.result())

        return files

    def _add_from_url(self, url, destination, extract, filename=None):
        """Process adding from url and return the location on disk."""
        url = self._provider_check(url)

        try:
            start = time.time() * 1e3
            tmp_root, paths = self._download(url=url, filename=filename, extract=extract)

            exec_time = (time.time() * 1e3 - start) // 1e3
            # If execution time was less or equal to zero seconds,
            # block the thread a bit to avoid being rate limited.
            if exec_time == 0:
                time.sleep(min(os.cpu_count() - 1, 4) or 1)

        except (requests.exceptions.HTTPError, error.HTTPError) as e:  # pragma nocover
            raise errors.OperationError("Cannot download from {}".format(url)) from e

        paths = [(src, destination / src.relative_to(tmp_root)) for src in paths if not src.is_dir()]
        return [
            {
                "operation": (src, dst, "move"),
                "path": dst.relative_to(self.path),
                "source": remove_credentials(url),
                "parent": self,
            }
            for src, dst in paths
        ]

    def _add_from_git(self, url, sources, destination, ref, repository=None):
        """Process adding resources from another git repository."""
        from renku import LocalClient

        u = parse.urlparse(url)

        sources = self._resolve_paths(u.path, sources)

        if not repository:
            repository, repo_path = self.prepare_git_repo(url, ref, depth=None)
        else:
            repo_path = Path(repository.working_dir)

        # Get all files from repo that match sources
        files = set()
        used_sources = set()
        for file in repository.head.commit.tree.traverse():
            path = file.path
            result = self._get_src_and_dst(path, repo_path, sources, destination, used_sources)

            if result:
                files.add(result)

        unused_sources = set(sources.keys()) - used_sources
        if unused_sources:
            unused_sources = {str(s) for s in unused_sources}
            raise errors.ParameterError("No such file or directory", param_hint=unused_sources)

        # Create metadata and move files to dataset
        results = []
        remote_client = LocalClient(repo_path)

        # Pull files from LFS
        paths = set()
        for path, src, _ in files:
            if src.is_dir():
                continue
            if src.is_symlink():
                try:
                    path = str(src.resolve().relative_to(repo_path))
                except ValueError:  # External file
                    pass
            paths.add(path)
        self._fetch_lfs_files(repo_path, paths)

        new_files = []

        for path, src, dst in files:
            if not src.is_dir():
                path_in_dst_repo = dst.relative_to(self.path)
                if path_in_dst_repo in new_files:  # A path with the same destination is already copied
                    continue

                new_files.append(path_in_dst_repo)

                if remote_client.is_external_file(src):
                    operation = (src.resolve(), dst, "symlink")
                else:
                    operation = (src, dst, "move")

                checksum = get_object_hash(repo=remote_client.repo, revision="HEAD", path=path)
                based_on = new_datasets.RemoteEntity(checksum=checksum, path=path, url=url)

                results.append(
                    {
                        "path": path_in_dst_repo,
                        "source": remove_credentials(url),
                        "parent": self,
                        "based_on": based_on,
                        "operation": operation,
                    }
                )

        return results

    @staticmethod
    def _ensure_dropbox(url):
        """Ensure dropbox url is set for file download."""
        if not isinstance(url, ParseResult):
            url = parse.urlparse(url)

        query = url.query or ""
        if "dl=0" in url.query:
            query = query.replace("dl=0", "dl=1")
        else:
            query += "dl=1"

        url = url._replace(query=query)
        return url

    def _provider_check(self, url):
        """Check additional provider related operations."""
        # NOTE: Check if the url is a redirect.
        r = requests.head(url, allow_redirects=True)
        url = parse.urlparse(r.url)

        if "dropbox.com" in url.netloc:
            url = self._ensure_dropbox(url)

        return parse.urlunparse(url)

    def _resolve_paths(self, root_path, paths):
        """Check if paths are within a root path and resolve them."""
        result = OrderedDict()  # Used as an ordered-set
        for path in paths:
            r = self._resolve_path(root_path, path)
            result[r] = None
        return result

    @staticmethod
    def _resolve_path(root_path, path):
        """Check if a path is within a root path and resolve it."""
        try:
            root_path = Path(root_path).resolve()
            path = os.path.abspath(root_path / path)
            return Path(path).relative_to(root_path)
        except ValueError:
            raise errors.ParameterError("File {} is not within path {}".format(path, root_path))

    @staticmethod
    def _get_src_and_dst(path, repo_path, sources, dst_root, used_sources):
        is_wildcard = False
        matched_pattern = None

        if not sources:
            source = Path(".")
        else:
            source = None
            for s in sources.keys():
                try:
                    Path(path).relative_to(s)
                except ValueError:
                    if glob.globmatch(path, str(s), flags=glob.GLOBSTAR):
                        is_wildcard = True
                        source = Path(path)
                        used_sources.add(s)
                        matched_pattern = str(s)
                        break
                else:
                    source = Path(s)
                    used_sources.add(source)
                    break

            if not source:
                return

        if is_wildcard:
            # Search to see if a parent of the path matches the pattern and return it
            while glob.globmatch(str(source.parent), matched_pattern, flags=glob.GLOBSTAR) and source != source.parent:
                source = source.parent

        src = repo_path / path
        source_name = source.name
        relative_path = Path(path).relative_to(source)

        if src.is_dir() and is_wildcard:
            sources[source] = None
            used_sources.add(source)

        dst = dst_root / source_name / relative_path

        return path, src, dst

    @staticmethod
    def _fetch_lfs_files(repo_path, paths):
        """Fetch and checkout paths that are tracked by Git LFS."""
        repo_path = str(repo_path)

        try:
            paths = [shlex.quote(p) for p in paths]
            status = run_command(
                ["git", "lfs", "pull", "--include"],
                *paths,
                separator=",",
                stderr=PIPE,
                cwd=repo_path,
                universal_newlines=True,
            )
            if status.returncode != 0:
                message = "\n\t".join(status.stderr.split("\n"))
                raise errors.GitError(f"Cannot pull LFS objects from server: {message}")
        except KeyboardInterrupt:
            raise
        except SubprocessError as e:
            raise errors.GitError(f"Cannot pull LFS objects from server: {e}")

    def dataset_commits(self, dataset, max_results=None):
        """Gets the newest commit for a dataset or its files.

        Commits are returned sorted from newest to oldest.
        """
        # FIXME: Return correct hash/dataset id in https://github.com/SwissDataScienceCenter/renku-python/issues/2210
        # paths = [(self.path / dataset.path / self.METADATA).resolve()]
        #
        # paths.extend(self.path / f.path for f in dataset.files)
        #
        # commits = self.repo.iter_commits(paths=paths, max_count=max_results)
        #
        # return commits
        return [self.repo.head.commit]

    def add_dataset_tag(self, dataset, tag, description="", force=False):
        """Adds a new tag to a dataset.

        Validates if the tag already exists and that the tag follows
        the same rules as docker tags.
        See https://docs.docker.com/engine/reference/commandline/tag/
        for a documentation of docker tag syntax.

        :raises: errors.ParameterError
        """
        if len(tag) > 128:
            raise errors.ParameterError("Tags can be at most 128 characters long.")

        if not re.match("^(?![.-])[a-zA-Z0-9_.-]{1,128}$", tag):
            raise errors.ParameterError(
                (
                    "Tag {} is invalid. \n"
                    "Only characters a-z, A-Z, 0-9, ., - and _ "
                    "are allowed. \nTag can't start with a . or -"
                ).format(tag)
            )

        if any(t for t in dataset.tags if t.name == tag):
            if force:
                # remove duplicate tag
                dataset.tags = [t for t in dataset.tags if t.name != tag]
            else:
                raise errors.ParameterError("Tag {} already exists".format(tag))

        latest_commit = list(self.dataset_commits(dataset, max_results=1))[0]

        tag = new_datasets.DatasetTag(
            name=tag, description=description, commit=latest_commit.hexsha, dataset=dataset.title
        )

        dataset.tags.append(tag)

    def remove_dataset_tags(self, dataset, tags):
        """Removes tags from a dataset."""
        tag_names = {t.name for t in dataset.tags}
        not_found = set(tags).difference(tag_names)

        if len(not_found) > 0:
            raise errors.ParameterError("Tags {} not found".format(", ".join(not_found)))
        dataset.tags = [t for t in dataset.tags if t.name not in tags]

    @inject.autoparams()
    def move_files(self, files, to_dataset, datasets_provenance: DatasetsProvenance):
        """Move files and their metadata from one or more datasets to a target dataset."""
        datasets = [d.copy() for d in self.datasets.values()]
        if to_dataset:
            # NOTE: Use the same dataset object or otherwise a race happens if dataset is in both source and destination
            to_dataset: new_datasets.Dataset = next(d for d in datasets if d.name == to_dataset)
        modified_datasets = {}

        progress_name = "Updating dataset metadata"
        communication.start_progress(progress_name, total=len(files))
        try:
            for src, dst in files.items():
                src = src.relative_to(self.path)
                dst = dst.relative_to(self.path)
                # NOTE: Files are moved at this point, so, we use can use dst
                new_dataset_file = new_datasets.DatasetFile.from_path(self, dst)
                for dataset in datasets:
                    removed = dataset.unlink_file(src, missing_ok=True)
                    if removed:
                        modified_datasets[dataset.name] = dataset
                        new_dataset_file.based_on = removed.based_on
                        new_dataset_file.source = removed.source
                        if not to_dataset:
                            dataset.add_or_update_files(new_dataset_file)

                    # NOTE: Update dataset if it contains a destination that is being overwritten
                    modified = dataset.find_file(dst)
                    if modified:
                        modified_datasets[dataset.name] = dataset
                        dataset.add_or_update_files(new_dataset_file)

                if to_dataset:
                    to_dataset.add_or_update_files(new_dataset_file)

                communication.update_progress(progress_name, amount=1)
        finally:
            communication.finalize_progress(progress_name)

        for dataset in modified_datasets.values():
            datasets_provenance.add_or_update(dataset, creator=Person.from_client(self))
        if to_dataset:
            datasets_provenance.add_or_update(to_dataset, creator=Person.from_client(self))

    def update_dataset_local_files(self, records: List[DynamicProxy], delete=False):
        """Update files metadata from the git history."""
        updated_files: List[DynamicProxy] = []
        deleted_files: List[DynamicProxy] = []
        progress_text = "Checking for local updates"

        try:
            communication.start_progress(progress_text, len(records))
            for file in records:
                communication.update_progress(progress_text, 1)

                if file.based_on or file.is_external:
                    continue

                if not (self.path / file.entity.path).exists():
                    deleted_files.append(file)
                    continue

                current_checksum = get_object_hash(repo=self.repo, revision="HEAD", path=file.entity.path)
                if not current_checksum:
                    deleted_files.append(file)
                elif current_checksum != file.entity.checksum:
                    updated_files.append(file)
        finally:
            communication.finalize_progress(progress_text)

        if updated_files or (deleted_files and delete):
            self._update_datasets_metadata(updated_files, deleted_files, delete)

        return updated_files, deleted_files

    @inject.autoparams()
    def _update_datasets_metadata(
        self,
        updated_files: List[DynamicProxy],
        deleted_files: List[DynamicProxy],
        delete,
        datasets_provenance: DatasetsProvenance,
    ):
        modified_datasets = {}

        for file in updated_files:
            new_file = new_datasets.DatasetFile.from_path(
                client=self, path=file.entity.path, based_on=file.based_on, source=file.source
            )
            modified_datasets[file.dataset.name] = file.dataset
            file.dataset.add_or_update_files([new_file])

        if delete:
            for file in deleted_files:
                modified_datasets[file.dataset.name] = file.dataset
                file.dataset.unlink_file(file.entity.path)

        for dataset in modified_datasets.values():
            datasets_provenance.add_or_update(dataset, creator=Person.from_client(self))

    def update_dataset_git_files(self, files: List[DynamicProxy], ref, delete=False):
        """Update files and dataset metadata according to their remotes.

        :param files: List of files to be updated
        :param delete: Indicates whether to delete files or not

        :return: List of files that should be deleted
        """
        from renku import LocalClient

        visited_repos = {}
        updated_files: List[DynamicProxy] = []
        deleted_files: List[DynamicProxy] = []

        progress_text = "Checking files for updates"

        try:
            communication.start_progress(progress_text, len(files))
            for file in files:
                communication.update_progress(progress_text, 1)
                if not file.based_on:
                    continue

                based_on = file.based_on
                url = based_on.url
                if url in visited_repos:
                    repo, repo_path, remote_client = visited_repos[url]
                else:
                    repo, repo_path = self.prepare_git_repo(url, ref)
                    remote_client = LocalClient(repo_path)
                    visited_repos[url] = repo, repo_path, remote_client

                checksum = get_object_hash(repo, based_on.path)

                # NOTE: Use commit sha to compare if available for compatibility
                if based_on.commit_sha is not None:
                    remote_commit = find_previous_commit(repo, based_on.path)
                    found = bool(remote_commit)
                    changed = found and based_on.commit_sha != remote_commit.hexsha
                else:
                    found = bool(checksum)
                    changed = found and based_on.checksum != checksum

                src = Path(repo.working_dir) / based_on.path
                dst = self.renku_path.parent / file.entity.path

                if changed:
                    if src.exists():
                        # Fetch file if it is tracked by Git LFS
                        self._fetch_lfs_files(repo_path, {based_on.path})
                        if remote_client.is_external_file(src):
                            self.remove_file(dst)
                            self._create_external_file(src.resolve(), dst)
                        else:
                            shutil.copy(src, dst)
                        file.based_on = new_datasets.RemoteEntity(
                            checksum=checksum, path=based_on.path, url=based_on.url
                        )
                        updated_files.append(file)
                    else:
                        # File was removed or renamed
                        found = False

                if not found:
                    if delete:
                        self.remove_file(dst)
                    deleted_files.append(file)
        finally:
            communication.finalize_progress(progress_text)

        if not updated_files and (not delete or not deleted_files):
            # Nothing to commit or update
            return [], deleted_files

        # Commit changes in files

        file_paths = {str(self.path / f.entity.path) for f in updated_files + deleted_files}
        # Force-add to include possible ignored files that are in datasets
        add_to_git(self.repo.git, *file_paths, force=True)
        skip_hooks = not self.external_storage_requested
        self.repo.index.commit(
            "renku dataset: updated {} files and deleted {} files".format(len(updated_files), len(deleted_files)),
            skip_hooks=skip_hooks,
        )

        self._update_datasets_metadata(updated_files, deleted_files, delete)

        return updated_files, deleted_files

    def _create_external_file(self, src, dst):
        """Create a new external file."""
        try:
            pointer_file = self._create_pointer_file(target=src)
            relative = os.path.relpath(pointer_file, dst.parent)
            os.symlink(relative, dst)
        except OSError as e:
            raise errors.OperationError("Could not create symbolic link") from e

    def _create_pointer_file(self, target, checksum=None):
        """Create a new pointer file."""
        target = Path(target).resolve()

        if checksum is None:
            checksum = self._calculate_checksum(target)

        while True:
            filename = "{}-{}".format(uuid.uuid4(), checksum)
            path = self.renku_pointers_path / filename
            if not path.exists():
                break

        try:
            os.symlink(target, path)
        except FileNotFoundError:
            raise errors.ParameterError("Cannot find external file {}".format(target))

        return path

    def _calculate_checksum(self, filepath):
        try:
            return self.repo.git.hash_object(str(filepath))
        except GitCommandError:
            return None

    @inject.autoparams()
    def update_external_files(self, records: List[DynamicProxy], datasets_provenance: DatasetsProvenance):
        """Update files linked to external storage."""
        updated_files_paths = []
        updated_datasets = {}

        for file in records:
            if file.is_external:
                path = self.path / file.entity.path
                link = path.parent / os.readlink(path)
                pointer_file = self.path / link
                pointer_file = self._update_pointer_file(pointer_file)
                if pointer_file is not None:
                    relative = os.path.relpath(pointer_file, path.parent)
                    os.remove(path)
                    os.symlink(relative, path)
                    updated_files_paths.append(str(path))
                    updated_datasets[file.dataset.name] = file.dataset

        if not updated_files_paths:
            return

        add_to_git(self.repo.git, *updated_files_paths, force=True)
        self.repo.git.add(self.renku_pointers_path, force=True)
        self.repo.index.commit("renku dataset: updated {} external files".format(len(updated_files_paths)))

        for dataset in updated_datasets.values():
            for file in dataset.files:
                if str(self.path / file.entity.path) in updated_files_paths:
                    new_file = new_datasets.DatasetFile.from_path(
                        client=self, path=file.entity.path, source=file.source
                    )
                    dataset.add_or_update_files(new_file)

            datasets_provenance.add_or_update(dataset, creator=Person.from_client(self))

    def _update_pointer_file(self, pointer_file_path):
        """Update a pointer file."""
        try:
            target = pointer_file_path.resolve(strict=True)
        except FileNotFoundError:
            target = pointer_file_path.resolve()
            raise errors.ParameterError("External file not found: {}".format(target))

        checksum = self._calculate_checksum(target)
        current_checksum = pointer_file_path.name.split("-")[-1]

        if checksum == current_checksum:
            return

        os.remove(pointer_file_path)
        return self._create_pointer_file(target, checksum=checksum)

    @staticmethod
    def remove_file(filepath):
        """Remove a file/symlink and its pointer file (for external files)."""
        path = Path(filepath)
        try:
            link = path.parent / os.readlink(path)
        except FileNotFoundError:
            return
        except OSError:  # not a symlink but a normal file
            os.remove(path)
            return

        os.remove(path)

        try:
            os.remove(link)
        except FileNotFoundError:
            pass

    def is_external_file(self, path):
        """Checks if a path within repo is an external file."""
        path = self.path / path
        if not Path(path).is_symlink() or not self._is_path_within_repo(path):
            return False
        pointer = os.readlink(path)
        return f"{self.renku_home}/{self.POINTERS}" in pointer

    def has_external_files(self):
        """Return True if project has external files."""
        for dataset in self.datasets.values():
            for file_ in dataset.files:
                if file_.is_external:
                    return True

    def _is_path_within_repo(self, path):
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        path = Path(path)
        try:
            path.relative_to(self.path)
        except ValueError:
            return False
        else:
            return True

    def prepare_git_repo(self, url, ref=None, gitlab_token=None, renku_token=None, deployment_hostname=None, depth=1):
        """Clone and cache a Git repo."""
        if not url:
            raise errors.GitError("Invalid URL.")

        renku_branch = "renku-default-branch"

        depth = depth if not ref else None
        ref = ref or renku_branch
        u = GitURL.parse(url)
        path = u.path
        if u.host == "localhost":
            path = Path(path).resolve()
            git_url = str(path)
        elif "http" in u.scheme and gitlab_token:
            git_url = get_oauth_url(url, gitlab_token)
        elif "http" in u.scheme and renku_token:
            git_url = get_renku_repo_url(url, deployment_hostname=deployment_hostname, access_token=renku_token)
        else:
            git_url = url

        repo_name = os.path.splitext(os.path.basename(path))[0]
        path = os.path.dirname(path).lstrip("/")
        repo_path = self.renku_path / self.CACHE / u.host / path / repo_name

        if repo_path.exists():
            repo = Repo(str(repo_path))
            if have_same_remote(repo.remotes.origin.url, git_url):
                try:
                    repo.git.checkout(".")
                    repo.git.fetch(all=True)
                    repo.git.checkout(ref)
                    try:
                        repo.git.pull()
                    except GitError:
                        # When ref is not a branch, an error is thrown
                        pass
                except GitError:
                    # ignore the error and try re-cloning
                    pass
                else:
                    return repo, repo_path

            try:
                shutil.rmtree(str(repo_path))
            except PermissionError:
                raise errors.InvalidFileOperation(f"Cannot delete files in {repo_path}: Permission denied")

        repo, _ = clone(git_url, path=str(repo_path), install_githooks=False, depth=depth)

        # Because the name of the default branch is not always 'master', we
        # create an alias of the default branch when cloning the repo. It
        # is used to refer to the default branch later.
        renku_ref = "refs/heads/" + renku_branch
        try:
            repo.git.execute(["git", "symbolic-ref", renku_ref, repo.head.reference.path])
        except GitCommandError as e:
            raise errors.GitError(f"Cannot clone remote Git repo: {url}") from e

        try:
            repo.git.checkout(ref)
        except GitCommandError:
            raise errors.ParameterError(f"Cannot find reference '{ref}' in Git repository: {url}")

        return repo, repo_path

    def _download(self, url, filename, extract, chunk_size=16384):
        def extract_dataset(filepath):
            """Extract downloaded file."""
            try:
                tmp = tempfile.mkdtemp()
                patoolib.extract_archive(str(filepath), outdir=tmp, verbosity=-1)
            except patoolib.util.PatoolError:
                return filepath.parent, [filepath]
            else:
                filepath.unlink()
                return Path(tmp), [p for p in Path(tmp).rglob("*")]

        tmp_root = self.renku_path / self.CACHE
        tmp_root.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.mkdtemp(dir=tmp_root)

        with requests.get(url, stream=True, allow_redirects=True) as request:
            request.raise_for_status()

            if not filename:
                filename = _filename_from_headers(request)

            if not filename:
                u = parse.urlparse(url)
                filename = Path(u.path).name
                if not filename:
                    raise errors.ParameterError(f"URL Cannot find a file to download from {url}")

            download_to = Path(tmp) / filename
            with open(str(download_to), "wb") as file_:
                total_size = int(request.headers.get("content-length", 0))

                communication.start_progress(name=filename, total=total_size)
                try:
                    for chunk in request.iter_content(chunk_size=chunk_size):
                        if chunk:  # ignore keep-alive chunks
                            file_.write(chunk)
                            communication.update_progress(name=filename, amount=len(chunk))
                finally:
                    communication.finalize_progress(name=filename)
        if extract:
            return extract_dataset(download_to)

        return download_to.parent, [download_to]


def _filename_from_headers(request):
    """Extract filename from content-disposition headers if available."""
    content_disposition = request.headers.get("content-disposition", None)

    if not content_disposition:
        return None

    entries = content_disposition.split(";")
    name_entry = next((e.strip() for e in entries if e.strip().lower().startswith("filename*=")), None)

    if name_entry:
        name = name_entry.split("=", 1)[1].strip()
        encoding, _, name = name.split("'")
        return unquote(name, encoding, errors="strict")

    name_entry = next((e.strip() for e in entries if e.strip().lower().startswith("filename=")), None)

    if not name_entry:
        return None

    filename = name_entry.split("=", 1)[1].strip()

    if filename.startswith('"'):
        filename = filename[1:-1]
    return filename


def _check_url(url):
    """Check if a url is local/remote and if it contains a git repository."""
    u = parse.urlparse(url)

    if u.scheme not in DatasetsApiMixin.SUPPORTED_SCHEMES:
        raise errors.UrlSchemeNotSupported('Scheme "{}" not supported'.format(u.scheme))

    is_remote = u.scheme not in ("", "file") or url.startswith("git@")
    is_git = False

    if is_remote:
        is_git = u.path.endswith(".git")
        if not is_git:
            # NOTE: Check if the url is a redirect.
            url = requests.head(url, allow_redirects=True).url
    else:
        try:
            Repo(u.path, search_parent_directories=True)
        except GitError:
            pass
        else:
            is_git = True

    return is_remote, is_git, url


DATASET_METADATA_PATHS = [
    Path(RENKU_HOME) / RepositoryApiMixin.DATABASE_PATH,
    Path(RENKU_HOME) / DatasetsApiMixin.DATASETS,
    Path(RENKU_HOME) / DatasetsApiMixin.DATASET_IMAGES,
    Path(RENKU_HOME) / DatasetsApiMixin.POINTERS,
    Path(RENKU_HOME) / LinkReference.REFS,
    Path(RENKU_HOME) / DatasetsApiMixin.DATASETS_PROVENANCE,
    ".gitattributes",
]
