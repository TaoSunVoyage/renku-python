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
"""Repository datasets management."""

import re
import shutil
import urllib
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional

import click
import git
import patoolib
import requests

from renku.core import errors
from renku.core.commands.format.dataset_files import DATASET_FILES_FORMATS
from renku.core.commands.format.dataset_tags import DATASET_TAGS_FORMATS
from renku.core.commands.format.datasets import DATASETS_FORMATS
from renku.core.commands.providers import ProviderFactory
from renku.core.errors import DatasetNotFound, InvalidAccessToken, OperationError, ParameterError, UsageError
from renku.core.management import LocalClient
from renku.core.management.command_builder import inject
from renku.core.management.command_builder.command import Command
from renku.core.management.datasets import DATASET_METADATA_PATHS
from renku.core.metadata.immutable import DynamicProxy
from renku.core.models.dataset import (
    Dataset,
    DatasetDetailsJson,
    DatasetsProvenance,
    DatasetTag,
    Url,
    generate_default_name,
    get_dataset_data_dir,
)
from renku.core.models.provenance.agent import Person
from renku.core.models.tabulate import tabulate
from renku.core.utils import communication
from renku.core.utils.doi import is_doi
from renku.core.utils.urls import remove_credentials


@inject.autoparams()
def _list_datasets(datasets_provenance: DatasetsProvenance, format=None, columns=None):
    """List all datasets."""
    if format is None:
        return list(datasets_provenance.datasets)

    if format not in DATASETS_FORMATS:
        raise UsageError("format not supported")

    return DATASETS_FORMATS[format](datasets_provenance.datasets, columns=columns)


def list_datasets():
    """Command for listing datasets."""
    return Command().command(_list_datasets).with_database().require_migration()


@inject.autoparams()
def create_dataset_helper(
    name,
    client: LocalClient,
    title=None,
    description="",
    creators=None,
    keywords=None,
    images=None,
    safe_image_paths=None,
):
    """Create a dataset in the repository."""
    if not creators:
        creators = [Person.from_git(client.repo)]
    else:
        creators, _ = _construct_creators(creators)

    dataset = client.create_dataset(
        name=name,
        title=title,
        description=description,
        creators=creators,
        keywords=keywords,
        images=images,
        safe_image_paths=safe_image_paths,
    )

    return dataset


def create_dataset():
    """Return a command for creating an empty dataset in the current repo."""
    command = Command().command(create_dataset_helper).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _edit_dataset(
    name,
    title,
    description,
    creators,
    client: LocalClient,
    datasets_provenance: DatasetsProvenance,
    keywords=None,
    images=None,
    skip_image_update=False,
    safe_image_paths=None,
):
    """Edit dataset metadata."""
    possible_updates = {
        "creators": creators,
        "description": description,
        "keywords": keywords,
        "title": title,
    }

    creators, no_email_warnings = _construct_creators(creators, ignore_email=True)
    title = title.strip() if isinstance(title, str) else ""

    dataset = client.get_dataset(name=name)

    updated = {k: v for k, v in possible_updates.items() if v}

    if updated:
        dataset.update_metadata(creators=creators, description=description, keywords=keywords, title=title)

    if skip_image_update:
        images_updated = False
    else:
        safe_image_paths.append(client.path)
        images_updated = client.set_dataset_images(dataset, images, safe_image_paths)

    if images_updated:
        updated["images"] = [{"content_url": i.content_url, "position": i.position} for i in dataset.images]

    if not updated:
        return [], no_email_warnings

    datasets_provenance.add_or_update(dataset, creator=Person.from_client(client))

    return updated, no_email_warnings


def edit_dataset():
    """Command for editing dataset metadata."""
    command = Command().command(_edit_dataset).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _show_dataset(name, client: LocalClient):
    """Show detailed dataset information."""
    dataset = client.get_dataset(name)
    return DatasetDetailsJson().dump(dataset)


def show_dataset():
    """Command for showing detailed dataset information."""
    return Command().command(_show_dataset).with_database().require_migration()


def _construct_creators(creators, ignore_email=False):
    from collections.abc import Iterable

    creators = creators or ()

    if not isinstance(creators, Iterable) or isinstance(creators, str):
        raise errors.ParameterError("Invalid type")

    people = []
    no_email_warnings = []
    for creator in creators:
        if isinstance(creator, str):
            person = Person.from_string(creator)
        elif isinstance(creator, dict):
            person = Person.from_dict(creator)
        else:
            raise errors.ParameterError("Invalid type")

        message = 'A valid format is "Name <email> [affiliation]"'

        if not person.name:  # pragma: no cover
            raise errors.ParameterError(f'Name is invalid: "{creator}".\n{message}')

        if not person.email:
            if not ignore_email:  # pragma: no cover
                raise errors.ParameterError(f'Email is invalid: "{creator}".\n{message}')
            else:
                no_email_warnings.append(creator)

        people.append(person)

    return people, no_email_warnings


@inject.autoparams()
def _add_to_dataset(
    urls,
    name,
    client: LocalClient,
    external=False,
    force=False,
    overwrite=False,
    create=False,
    sources=(),
    destination="",
    ref=None,
    with_metadata=None,
    extract=False,
    all_at_once=False,
    destination_names=None,
    total_size=None,
    repository=None,
    clear_files_before=False,
):
    """Add data to a dataset."""
    if len(urls) == 0:
        raise UsageError("No URL is specified")
    if sources and len(urls) > 1:
        raise UsageError('Cannot use "--source" with multiple URLs.')

    if total_size is None:
        total_size = 0
        for url in urls:
            try:
                with requests.get(url, stream=True, allow_redirects=True) as r:
                    total_size += int(r.headers.get("content-length", 0))
            except requests.exceptions.RequestException:
                pass
    usage = shutil.disk_usage(client.path)

    if total_size > usage.free:
        mb = 2 ** 20
        message = "Insufficient disk space (required: {:.2f} MB" "/available: {:.2f} MB). ".format(
            total_size / mb, usage.free / mb
        )
        raise OperationError(message)

    try:
        with client.with_dataset(name=name, create=create) as dataset:
            client.add_data_to_dataset(
                dataset,
                urls=urls,
                external=external,
                force=force,
                overwrite=overwrite,
                sources=sources,
                destination=destination,
                ref=ref,
                extract=extract,
                all_at_once=all_at_once,
                destination_names=destination_names,
                repository=repository,
                clear_files_before=clear_files_before,
            )
            if with_metadata:
                dataset.update_metadata_from(with_metadata)

        return dataset
    except DatasetNotFound:
        raise DatasetNotFound(
            message='Dataset "{0}" does not exist.\n'
            'Use "renku dataset create {0}" to create the dataset or retry '
            '"renku dataset add {0}" command with "--create" option for '
            "automatic dataset creation.".format(name)
        )
    except (FileNotFoundError, git.exc.NoSuchPathError) as e:
        raise ParameterError("Could not find paths/URLs: \n{0}".format("\n".join(urls))) from e


def add_to_dataset():
    """Create a command for adding data to datasets."""
    command = Command().command(_add_to_dataset).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(raise_if_empty=True, commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _list_files(
    client: LocalClient, datasets=None, creators=None, include=None, exclude=None, format=None, columns=None
):
    """List dataset files."""
    records = _filter(names=datasets, creators=creators, include=include, exclude=exclude, immutable=True)
    for record in records:
        record.title = record.dataset.title
        record.dataset_name = record.dataset.name
        record.dataset_id = record.dataset.id
        record.creators_csv = record.dataset.creators_csv
        record.creators_full_csv = record.dataset.creators_full_csv
        record.full_path = client.path / record.entity.path
        record.path = record.entity.path
        record.name = Path(record.entity.path).name
        record.added = record.date_added

    if format is None:
        return records

    if format not in DATASETS_FORMATS:
        raise UsageError("format not supported")

    return DATASET_FILES_FORMATS[format](records, columns=columns)


def list_files():
    """Command for listing dataset files."""
    return Command().command(_list_files).with_database().require_migration()


@inject.autoparams()
def _file_unlink(name, include, exclude, client: LocalClient, datasets_provenance: DatasetsProvenance, yes=False):
    """Remove matching files from a dataset."""
    if not include and not exclude:
        raise ParameterError(
            (
                "include or exclude filters not found.\n"
                "Check available filters with 'renku dataset unlink --help'\n"
                "Hint: 'renku dataset unlink my-dataset -I path'"
            )
        )

    dataset = client.get_dataset(name=name)

    if not dataset:
        raise ParameterError("Dataset does not exist.")

    records = _filter(names=[name], include=include, exclude=exclude)
    if not records:
        raise ParameterError("No records found.")

    if not yes:
        prompt_text = (
            f'You are about to remove following from "{name}" dataset.'
            + "\n"
            + "\n".join([str(record.entity.path) for record in records])
            + "\nDo you wish to continue?"
        )
        communication.confirm(prompt_text, abort=True, warning=True)

    for file in records:
        dataset.unlink_file(file.entity.path)

    datasets_provenance.add_or_update(dataset, creator=Person.from_client(client))

    return records


def file_unlink():
    """Command for removing matching files from a dataset."""
    command = Command().command(_file_unlink).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _remove_dataset(name, client: LocalClient, datasets_provenance: DatasetsProvenance):
    """Delete a dataset."""
    dataset = client.get_dataset(name=name, strict=True)
    datasets_provenance.remove(dataset=dataset)


def remove_dataset():
    """Command for deleting a dataset."""
    command = Command().command(_remove_dataset).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _export_dataset(name, provider_name, publish, tag, client: LocalClient, **kwargs):
    """Export data to 3rd party provider.

    :raises: ``ValueError``, ``HTTPError``, ``InvalidAccessToken``,
             ``DatasetNotFound``
    """
    provider_name = provider_name.lower()

    # TODO: all these callbacks are ugly, improve in #737
    config_key_secret = "access_token"

    dataset_ = client.get_dataset(name, strict=True)

    try:
        provider = ProviderFactory.from_id(provider_name)
    except KeyError:
        raise ParameterError("Unknown provider.")

    provider.set_parameters(**kwargs)

    selected_tag = None
    selected_commit = client.repo.head.commit

    if tag:
        selected_tag = next((t for t in dataset_.tags if t.name == tag), None)

        if not selected_tag:
            raise ValueError("Tag {} not found".format(tag))

        selected_commit = selected_tag.commit
    elif dataset_.tags and len(dataset_.tags) > 0:
        tag_result = _prompt_tag_selection(dataset_.tags)

        if tag_result:
            selected_tag = tag_result
            selected_commit = tag_result.commit

            # FIXME: This won't work and needs to be fixed in #renku-python/issues/2210
            # If the tag is created automatically for imported datasets, it
            # does not have the dataset yet and we need to use the next commit
            with client.with_commit(selected_commit):
                test_ds = client.get_dataset(name)
            if not test_ds:
                commits = client.dataset_commits(dataset_)
                next_commit = selected_commit
                for commit in commits:
                    if commit.hexsha == selected_commit:
                        selected_commit = next_commit.hexsha
                        break
                    next_commit = commit

    with client.with_commit(selected_commit):
        dataset_ = client.get_dataset(name)
        if not dataset_:
            raise DatasetNotFound(name=name)

        dataset_.data_dir = get_dataset_data_dir(client, dataset_)

        access_token = client.get_value(provider_name, config_key_secret)
        exporter = provider.get_exporter(dataset_, access_token=access_token)

        if access_token is None:
            access_token = _prompt_access_token(exporter)

            if access_token is None or len(access_token) == 0:
                raise InvalidAccessToken()

            client.set_value(provider_name, config_key_secret, access_token, global_only=True)
            exporter.set_access_token(access_token)

        try:
            destination = exporter.export(publish=publish, tag=selected_tag, client=client)
        except errors.AuthenticationError:
            client.remove_value(provider_name, config_key_secret, global_only=True)
            raise

    communication.echo(f"Exported to: {destination}")


def export_dataset():
    """Command for exporting a dataset to 3rd party provider."""
    command = Command().command(_export_dataset).with_database()
    return command.require_migration().require_clean()


@inject.autoparams()
def _import_dataset(
    uri, client: LocalClient, name="", extract=False, yes=False, previous_dataset=None, delete=False, gitlab_token=None
):
    """Import data from a 3rd party provider or another renku project."""
    provider, err = ProviderFactory.from_uri(uri)
    if err and provider is None:
        raise ParameterError(f"Could not process '{uri}'.\n{err}")

    try:
        record = provider.find_record(uri, gitlab_token=gitlab_token)
        dataset = record.as_dataset(client)
        files = record.files_info
        total_size = 0

        if not yes:
            communication.echo(
                tabulate(
                    files,
                    headers=OrderedDict(
                        (
                            ("checksum", "checksum"),
                            ("filename", "name"),
                            ("size_in_mb", "size (mb)"),
                            ("filetype", "type"),
                        )
                    ),
                    floatfmt=".2f",
                )
            )

            text_prompt = "Do you wish to download this version?"
            if not record.is_last_version(uri):
                text_prompt = f"Newer version found at {record.latest_uri}\n{text_prompt}"

            communication.confirm(text_prompt, abort=True, warning=True)

            for file_ in files:
                if file_.size_in_mb is not None:
                    total_size += file_.size_in_mb

            total_size *= 2 ** 20

    except KeyError as e:
        raise ParameterError(f"Could not process '{uri}'.\nUnable to fetch metadata: {e}")
    except LookupError as e:
        raise ParameterError(f"Could not process '{uri}'.\nReason: {e}")

    if not files:
        raise ParameterError(f"Dataset '{uri}' has no files.")

    if not provider.is_git_based:
        if not name:
            name = generate_default_name(dataset.title, dataset.version)

        dataset.same_as = Url(url_id=remove_credentials(uri))
        if is_doi(dataset.identifier):
            dataset.same_as = Url(url_str=urllib.parse.urljoin("https://doi.org", dataset.identifier))

        urls, names = zip(*[(f.source, f.filename) for f in files])

        dataset = _add_to_dataset(
            urls=urls,
            name=name,
            create=not previous_dataset,
            with_metadata=dataset,
            force=True,
            extract=extract,
            all_at_once=True,
            destination_names=names,
            total_size=total_size,
            overwrite=True,
            clear_files_before=True,
        )

        if previous_dataset:
            dataset = _update_metadata(dataset, previous_dataset, delete, dataset.same_as)

        if dataset.version:
            tag_name = re.sub("[^a-zA-Z0-9.-_]", "_", dataset.version)
            _tag_dataset_helper(
                dataset=dataset,
                tag=tag_name,
                description=f"Tag {dataset.version} created by renku import",
                update_provenance=False,
            )
    else:
        name = name or dataset.name

        dataset.same_as = Url(url_id=record.latest_uri)

        if not dataset.data_dir:
            raise OperationError(f"Data directory for dataset must be set: {dataset.name}")

        sources = []

        if record.datadir_exists:
            sources = [f"{dataset.data_dir}/**"]

        for file in dataset.files:
            try:
                Path(file.entity.path).relative_to(dataset.data_dir)
            except ValueError:  # Files that are not in dataset's data directory
                sources.append(file.entity.path)

        new_dataset = _add_to_dataset(
            urls=[record.project_url],
            name=name,
            sources=sources,
            with_metadata=dataset,
            create=not previous_dataset,
            overwrite=True,
            repository=record.repository,
            clear_files_before=True,
        )

        if previous_dataset:
            _update_metadata(new_dataset, previous_dataset, delete, dataset.same_as)

    if provider.supports_images:
        record.import_images(dataset)


def import_dataset():
    """Create a command for importing datasets."""
    command = Command().command(_import_dataset).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _update_metadata(new_dataset: Dataset, previous_dataset, delete, same_as, client: LocalClient):
    """Update metadata and remove files that exists in ``previous_dataset`` but not in ``new_dataset``."""
    current_paths = set(str(f.entity.path) for f in new_dataset.files)

    # NOTE: remove files not present in the dataset anymore
    for file in previous_dataset.files:
        if str(file.entity.path) in current_paths:
            continue

        if delete:
            client.remove_file(client.path / file.entity.path)

    new_dataset.same_as = same_as
    # NOTE: Remove derived_from because this is an updated and imported dataset
    new_dataset.derived_from = None

    return new_dataset


@inject.autoparams()
def _update_datasets(names, creators, include, exclude, ref, delete, client: LocalClient, external=False):
    """Update dataset files."""
    ignored_datasets = []

    if (include or exclude) and names and any(d.same_as for d in client.datasets.values() if d.name in names):
        raise errors.UsageError("--include/--exclude is incompatible with datasets created by 'renku dataset import'")

    names_provided = bool(names)

    # NOTE: update imported datasets
    if not include and not exclude:
        for dataset in client.datasets.values():
            if names and dataset.name not in names or not dataset.same_as:
                continue

            uri = dataset.same_as.url
            if isinstance(uri, dict):
                uri = uri.get("@id")
            provider, err = ProviderFactory.from_uri(uri)

            if not provider:
                continue

            record = provider.find_record(uri)

            if record.is_last_version(uri) and record.version == dataset.version:
                continue

            uri = record.latest_uri

            # NOTE: set extract to false if there are any archives present in the dataset
            extract = True
            for f in dataset.files:
                try:
                    patoolib.get_archive_format(f.entity.path)
                except patoolib.util.PatoolError:
                    continue
                else:
                    extract = False
                    break

            _import_dataset(
                uri=uri, name=dataset.name, extract=extract, yes=True, previous_dataset=dataset, delete=delete
            )

            communication.echo(f"Updated dataset '{dataset.name}' from remote provider")

            if names:
                names.remove(dataset.name)
            ignored_datasets.append(dataset.name)
    else:
        ignored_datasets = [d.name for d in client.datasets.values() if d.same_as]

    if names_provided and not names:
        return

    records = _filter(names=names, creators=creators, include=include, exclude=exclude, ignore=ignored_datasets)

    if not records:
        if ignored_datasets:
            return
        raise ParameterError("No files matched the criteria.")

    possible_updates = []
    unique_remotes = set()
    external_files = []
    local_files = []

    for file in records:
        if file.based_on:
            possible_updates.append(file)
            unique_remotes.add(file.based_on.url)
        elif file.is_external:
            external_files.append(file)
        else:
            local_files.append(file)

    if ref and len(unique_remotes) > 1:
        raise ParameterError(
            "Cannot use '--ref' with more than one Git repository.\n"
            "Limit list of files to be updated to one repository. See 'renku dataset update -h' for more information."
        )

    if external_files:
        if external:
            client.update_external_files(external_files)
        else:
            communication.echo("To update external files run update command with '--external' flag.")

    updated_files = []
    deleted_files = []

    if possible_updates:
        updated_files, deleted_files = client.update_dataset_git_files(files=possible_updates, ref=ref, delete=delete)

    if local_files:
        updated, deleted = client.update_dataset_local_files(records=local_files, delete=delete)
        updated_files.extend(updated)
        deleted_files.extend(deleted)

    if deleted_files and not delete:
        communication.echo("Some files are deleted. To also delete them from datasets' metadata use '--delete' flag.")

    message = f"Updated {len(updated_files)} files"
    if delete:
        message += f" and deleted {len(deleted_files)} files"
    communication.echo(message)


def update_datasets():
    """Command for updating datasets."""
    command = Command().command(_update_datasets).lock_dataset().with_database(write=True)
    return command.require_migration().require_clean().with_commit(commit_only=DATASET_METADATA_PATHS)


def _include_exclude(file_path, include=None, exclude=None):
    """Check if file matches one of include filters and not in exclude filter.

    :param file_path: Path to the file.
    :param include: Tuple containing patterns to which include from result.
    :param exclude: Tuple containing patterns to which exclude from result.
    """
    if exclude is not None and exclude:
        for pattern in exclude:
            if file_path.match(pattern):
                return False

    if include is not None and include:
        for pattern in include:
            if file_path.match(pattern):
                return True
        return False

    return True


@inject.autoparams()
def _filter(
    client: LocalClient, names=None, creators=None, include=None, exclude=None, ignore=None, immutable=False
) -> List[DynamicProxy]:
    """Filter dataset files by specified filters.

    :param names: Filter by specified dataset names.
    :param creators: Filter by creators.
    :param include: Include files matching file pattern.
    :param exclude: Exclude files matching file pattern.
    :param ignore: Ignored datasets.
    :param immutable: Return immutable copies of dataset objects.
    """
    if isinstance(creators, str):
        creators = set(creators.split(","))

    if isinstance(creators, list) or isinstance(creators, tuple):
        creators = set(creators)

    records = []
    unused_names = set(names)
    for dataset in client.datasets.values():
        if not immutable:
            dataset = dataset.copy()
        if (not names or dataset.name in names) and (not ignore or dataset.name not in ignore):
            if unused_names:
                unused_names.remove(dataset.name)
            for file in dataset.files:
                record = DynamicProxy(file)
                record.dataset = dataset
                record.client = client
                path = Path(record.entity.path)
                match = _include_exclude(path, include, exclude)

                if creators:
                    dataset_creators = {c.name for c in dataset.creators}
                    match = match and creators.issubset(dataset_creators)

                if match:
                    records.append(record)

    if unused_names:
        unused_names = ", ".join(unused_names)
        raise ParameterError(f"Dataset does not exist: {unused_names}")

    return sorted(records, key=lambda r: r.date_added)


@inject.autoparams()
def _tag_dataset(name, tag, description, client: LocalClient, update_provenance=True, force=False):
    """Creates a new tag for a dataset."""
    dataset = client.get_dataset(name, strict=True)
    _tag_dataset_helper(
        dataset=dataset, tag=tag, description=description, update_provenance=update_provenance, force=force
    )


@inject.autoparams()
def _tag_dataset_helper(
    dataset,
    tag,
    description,
    client: LocalClient,
    datasets_provenance: DatasetsProvenance,
    update_provenance=True,
    force=False,
):
    try:
        client.add_dataset_tag(dataset, tag, description, force)
    except ValueError as e:
        raise ParameterError(e)
    else:
        if update_provenance:
            datasets_provenance.add_or_update(dataset)


def tag_dataset():
    """Command for creating a new tag for a dataset."""
    command = Command().command(_tag_dataset).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _remove_dataset_tags(name, tags, client: LocalClient, datasets_provenance: DatasetsProvenance):
    """Removes tags from a dataset."""
    dataset = client.get_dataset(name, strict=True)

    try:
        client.remove_dataset_tags(dataset, tags)
    except ValueError as e:
        raise ParameterError(e)
    else:
        datasets_provenance.add_or_update(dataset)


def remove_dataset_tags():
    """Command for removing tags from a dataset."""
    command = Command().command(_remove_dataset_tags).lock_dataset().with_database(write=True)
    return command.require_migration().with_commit(commit_only=DATASET_METADATA_PATHS)


@inject.autoparams()
def _list_tags(name, format, client: LocalClient):
    """List all tags for a dataset."""
    dataset = client.get_dataset(name, strict=True)

    tags = sorted(dataset.tags, key=lambda t: t.date_created)

    return DATASET_TAGS_FORMATS[format](tags)


def list_tags():
    """Command for listing a dataset's tags."""
    return Command().command(_list_tags).with_database().require_migration()


def _prompt_access_token(exporter):
    """Prompt user for an access token for a provider.

    :return: The new access token
    """
    text_prompt = "You must configure an access token\n"
    text_prompt += "Create one at: {0}\n".format(exporter.access_token_url())
    text_prompt += "Access token"

    return communication.prompt(text_prompt, type=str)


def _prompt_tag_selection(tags) -> Optional[DatasetTag]:
    """Prompt user to chose a tag or <HEAD>."""
    # Prompt user to select a tag to export
    tags = sorted(tags, key=lambda t: t.date_created)

    text_prompt = "Tag to export: \n\n<HEAD>\t[1]\n"

    text_prompt += "\n".join("{}\t[{}]".format(t.name, i) for i, t in enumerate(tags, start=2))

    text_prompt += "\n\nTag"
    selection = communication.prompt(text_prompt, type=click.IntRange(1, len(tags) + 1), default=1)

    if selection > 1:
        return tags[selection - 2]
    return None
