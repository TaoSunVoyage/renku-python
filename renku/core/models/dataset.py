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
"""Models representing datasets."""

import copy
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Union
from urllib.parse import quote, urlparse
from uuid import uuid4

import marshmallow
from marshmallow import EXCLUDE, pre_dump

from renku.core import errors
from renku.core.metadata.database import Database, Index, Persistent
from renku.core.metadata.immutable import Immutable, Slots
from renku.core.models.calamus import DateTimeList, JsonLDSchema, Nested, Uri, fields, prov, renku, schema, wfprov
from renku.core.models.entity import Entity, EntitySchema
from renku.core.models.provenance.agent import Person, PersonSchema, SoftwareAgent
from renku.core.utils import communication
from renku.core.utils.datetime8601 import fix_timezone, local_now, parse_date
from renku.core.utils.urls import get_slug


def is_dataset_name_valid(name):
    """Check if name is a valid slug."""
    return name and name == get_slug(name)


def generate_default_name(dataset_title, dataset_version=None):
    """Get dataset name."""
    max_length = 24
    # For compatibility with older versions use title as name if it is valid; otherwise, use encoded title
    if is_dataset_name_valid(dataset_title):
        return dataset_title

    slug = get_slug(dataset_title)
    name = slug[:max_length]

    if dataset_version:
        max_version_length = 10
        version_slug = get_slug(dataset_version)[:max_version_length]
        name = f"{name[:-(len(version_slug) + 1)]}_{version_slug}"

    return get_slug(name)


class Url:
    """Represents a schema URL reference."""

    def __init__(self, *, id: str = None, url: str = None, url_str: str = None, url_id: str = None):
        self.id: str = id
        self.url: str = url
        self.url_str: str = url_str
        self.url_id: str = url_id

        if not self.url:
            self.url = self.get_default_url()
        elif isinstance(self.url, dict):
            if "_id" in self.url:
                self.url["@id"] = self.url.pop("_id")
            self.url_id = self.url["@id"]
        elif isinstance(self.url, str):
            self.url_str = self.url

        if not self.id or self.id.startswith("_:"):
            self.id = Url.generate_id(url_str=self.url_str, url_id=self.url_id)

    @staticmethod
    def generate_id(url_str, url_id):
        """Generate an identifier for Url."""
        url = url_str or url_id
        id = urlparse(url)._replace(scheme="").geturl().strip("/") if url else uuid4().hex
        id = quote(id, safe="/")

        return f"/urls/{id}"

    def get_default_url(self):
        """Define default value for url field."""
        if self.url_str:
            return self.url_str
        elif self.url_id:
            return {"@id": self.url_id}
        else:
            raise NotImplementedError("Either url_id or url_str has to be set")


class DatasetTag(Slots):
    """Represents a Tag of an instance of a dataset."""

    __slots__ = ("commit", "dataset", "date_created", "description", "id", "name")

    def __init__(
        self,
        *,
        commit: str,
        dataset=None,
        date_created: datetime = None,
        description: str = None,
        id: str = None,
        name: str,
    ):
        if not id or id.startswith("_:"):
            id = DatasetTag.generate_id(commit=commit, name=name)

        super().__init__(
            commit=commit,
            dataset=dataset,
            date_created=parse_date(date_created) or local_now(),
            description=description,
            id=id,
            name=name,
        )

    @staticmethod
    def generate_id(commit: str, name: str) -> str:
        """Define default value for id field."""
        name = quote(f"{name}@{commit}", safe="")
        return f"/dataset-tags/{name}"

    def to_jsonld(self):
        """Create JSON-LD."""
        return DatasetTagSchema().dump(self)


class Language(Immutable):
    """Represent a language of an object."""

    __slots__ = ("alternate_name", "name")

    def __init__(self, alternate_name: str = None, id: str = None, name: str = None):
        id = id or Language.generate_id(name)
        super().__init__(alternate_name=alternate_name, id=id, name=name)

    @staticmethod
    def generate_id(name: str) -> str:
        """Generate @id field."""
        name = quote(name, safe="")
        return f"/languages/{name}"


class ImageObject(Slots):
    """Represents a schema.org `ImageObject`."""

    __slots__ = ("content_url", "id", "position")

    def __init__(self, *, content_url: str, id: str, position: int):
        # TODO: Remove scheme://hostname from id
        super().__init__(content_url=content_url, position=position, id=id)

    @staticmethod
    def generate_id(dataset: "Dataset", position: int) -> str:
        """Generate @id field."""
        return f"{dataset.id}/images/{position}"

    @property
    def is_absolute(self):
        """Whether content_url is an absolute or relative url."""
        return bool(urlparse(self.content_url).netloc)


class RemoteEntity(Slots):
    """Reference to an Entity in a remote repo."""

    __slots__ = ("checksum", "commit_sha", "id", "path", "url")

    def __init__(
        self, *, checksum: str = None, commit_sha: str = None, id: str = None, path: Union[Path, str], url: str
    ):
        super().__init__()
        entity_hash = commit_sha or checksum
        assert entity_hash, "One of checksum or commit_sha must be provided."
        assert checksum is None or commit_sha is None, "Either checksum or commit_sha must be provided, not both."

        # NOTE: For compatibility we use commit_sha when migrating old projects. For all new instances use checksum.
        self.commit_sha: str = commit_sha
        self.checksum: str = checksum
        self.id: str = id or RemoteEntity.generate_id(entity_hash, path)
        self.path: str = str(path)
        self.url: str = url

    @staticmethod
    def generate_id(commit_sha: str, path: Union[Path, str]) -> str:
        """Generate an id."""
        path = quote(str(path))
        return f"/remote-entity/{commit_sha}/{path}"

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, RemoteEntity):
            return False
        return self.commit_sha == other.commit_sha and self.path == other.path and self.url == other.url

    def __hash__(self):
        return hash((self.commit_sha, self.path, self.url))


class DatasetFile(Slots):
    """A file in a dataset."""

    __slots__ = ("based_on", "date_added", "date_removed", "entity", "id", "is_external", "source")

    def __init__(
        self,
        *,
        based_on: RemoteEntity = None,
        date_added: datetime = None,
        date_removed: datetime = None,
        entity: Entity = None,
        id: str = None,
        is_external: bool = False,
        source: Union[Path, str] = None,
    ):
        assert entity is None or isinstance(entity, Entity), f"Invalid entity type: '{entity}'"

        super().__init__()

        self.based_on: RemoteEntity = based_on
        self.date_added: datetime = fix_timezone(date_added) or local_now()
        self.date_removed: datetime = fix_timezone(date_removed)
        self.entity: Entity = entity
        self.id: str = id or DatasetFile.generate_id()
        self.is_external: bool = is_external
        self.source: str = str(source)

    @classmethod
    def from_path(
        cls, client, path: Union[str, Path], source=None, based_on: RemoteEntity = None
    ) -> Optional["DatasetFile"]:
        """Return an instance from a path."""
        entity = Entity.from_revision(client=client, path=path)
        if not entity:
            return

        return cls(entity=entity, is_external=client.is_external_file(path), source=source, based_on=based_on)

    @staticmethod
    def generate_id():
        """Generate an identifier for DatasetFile.

        NOTE: ID should not rely on Entity properties because the same Entity can be added and removed multiple times.
        So, it should be marked by different DatasetFiles.
        """
        return f"/dataset-files/{uuid4().hex}"

    def copy(self) -> "DatasetFile":
        """Return a clone of this object."""
        return copy.copy(self)

    def is_equal_to(self, other: "DatasetFile"):
        """Compare content.

        NOTE: id is generated randomly and should not be included in this comparison.
        """
        return (
            self.based_on == other.based_on
            and self.date_added == other.date_added
            and self.date_removed == other.date_removed
            and self.entity == other.entity
            and self.is_external == other.is_external
            and self.source == other.source
        )

    def remove(self, date: datetime = None):
        """Create a new instance and mark it as removed."""
        date_removed = fix_timezone(date) or local_now()
        self.date_removed = date_removed

    def is_removed(self) -> bool:
        """Return true if dataset is removed and should not be accessed."""
        return self.date_removed is not None

    def to_jsonld(self):
        """Create JSON-LD."""
        return DatasetFileSchema(flattened=True).dump(self)


class Dataset(Persistent):
    """Represent a dataset."""

    def __init__(
        self,
        *,
        creators: List[Person] = None,
        dataset_files: List[DatasetFile] = None,
        date_created: datetime = None,
        date_published: datetime = None,
        date_removed: datetime = None,
        derived_from: str = None,
        description: str = None,
        id: str = None,
        identifier: str = None,
        images: List[ImageObject] = None,
        in_language: Language = None,
        initial_identifier: str = None,
        keywords: List[str] = None,
        license: str = None,
        name: str = None,
        same_as: Url = None,
        tags: List[DatasetTag] = None,
        title: str = None,
        version: str = None,
    ):
        if not name:
            assert title, "Either 'name' or 'title' must be set."
            name = generate_default_name(title, version)

        self._validate_name(name)
        self._validate_creator(creators)

        # if `date_published` is set, we are probably dealing with an imported dataset so `date_created` is not needed
        if date_published:
            date_created = None
        if initial_identifier is None:
            assert identifier is None, "Initial identifier can be None only when creating a new Dataset."
            initial_identifier = identifier = uuid4().hex

        self.identifier = identifier or uuid4().hex
        self.id = id or Dataset.generate_id(identifier=self.identifier)
        self.name = name

        self.creators: List[Person] = creators or []
        # `dataset_files` includes existing files and those that have been removed in the previous version
        self.dataset_files: List[DatasetFile] = dataset_files or []
        self.date_created: datetime = fix_timezone(date_created) or local_now()
        self.date_published: datetime = fix_timezone(date_published)
        self.date_removed: datetime = fix_timezone(date_removed)
        self.derived_from: str = derived_from
        self.description: str = description
        self.images: List[ImageObject] = images or []
        self.in_language: Language = in_language
        self.initial_identifier: str = initial_identifier
        self.keywords: List[str] = keywords or []
        self.license: str = license
        self.same_as: Url = same_as
        self.tags: List[DatasetTag] = tags or []
        self.title: str = title
        self.version: str = version

    @classmethod
    def from_jsonld(cls, data, schema_class=None):
        """Create an instance from JSON-LD data."""
        assert isinstance(data, (dict, list)), f"Invalid data type: {data}"

        schema_class = schema_class or DatasetSchema
        self = schema_class(flattened=True).load(data)
        return self

    @staticmethod
    def generate_id(identifier: str) -> str:
        """Generate an identifier for Dataset."""
        return f"/datasets/{identifier}"

    @staticmethod
    def _validate_name(name):
        if not is_dataset_name_valid(name):
            raise errors.ParameterError(f"Invalid dataset name: '{name}'")

    @staticmethod
    def _validate_creator(creators):
        creators = creators or []
        for creator in creators:
            if not isinstance(creator, (Person, SoftwareAgent)):
                raise ValueError(f"Invalid creator type: {creator}")

    @property
    def files(self):
        """Return list of existing files."""
        return [f for f in self.dataset_files if not f.is_removed()]

    @property
    def creators_csv(self):
        """Comma-separated list of creators associated with dataset."""
        return ", ".join(creator.name for creator in self.creators)

    @property
    def creators_full_csv(self):
        """Comma-separated list of creators with full identity."""
        return ", ".join(creator.full_identity for creator in self.creators)

    @property
    def keywords_csv(self):
        """Comma-separated list of keywords associated with dataset."""
        return ", ".join(self.keywords)

    @property
    def tags_csv(self):
        """Comma-separated list of tags associated with dataset."""
        return ",".join(tag.name for tag in self.tags)

    def copy(self) -> "Dataset":
        """Return a clone of this dataset."""
        return Dataset(
            creators=self.creators.copy(),
            dataset_files=[f.copy() for f in self.dataset_files],
            date_created=self.date_created,
            date_published=self.date_published,
            date_removed=self.date_removed,
            derived_from=self.derived_from,
            description=self.description,
            id=self.id,
            identifier=self.identifier,
            images=list(self.images or []),
            in_language=self.in_language,
            initial_identifier=self.initial_identifier,
            keywords=list(self.keywords or []),
            license=self.license,
            name=self.name,
            same_as=self.same_as,
            tags=list(self.tags or []),
            title=self.title,
            version=self.version,
        )

    def replace_identifier(self):
        """Replace dataset's identifier and update relevant fields.

        NOTE: Call this only for newly-created/-imported datasets that don't have a mutability chain because it sets
        `initial_identifier`.
        """
        assert (
            self.derived_from is None
        ), f"Replacing identifier of dataset '{self.name}:{self.identifier}' that is derived from {self.derived_from}"

        self._assign_new_identifier()
        # NOTE: Do not unset `same_as` because it can be set for imported datasets

    def derive_from(self, dataset: "Dataset", creator: Person):
        """Make `self` a derivative of `dataset` and update related fields."""
        assert dataset is not None, "Cannot derive from None"

        self._assign_new_identifier()
        # NOTE: Setting `initial_identifier` is required for migration of broken projects
        self.initial_identifier = dataset.initial_identifier
        self.derived_from = dataset.id
        self.same_as = None
        self.date_created = local_now()
        self.date_published = None

        if creator and hasattr(creator, "email") and not any(c for c in self.creators if c.email == creator.email):
            self.creators.append(creator)

    def _assign_new_identifier(self):
        identifier = uuid4().hex
        self.initial_identifier = identifier
        self.identifier = identifier
        self.id = Dataset.generate_id(identifier)
        # NOTE: We also need to re-assign the _p_oid since identifier has changed
        self.reassign_oid()

    def remove(self, date: datetime = None):
        """Mark the dataset as removed."""
        self.date_removed = fix_timezone(date) or local_now()

    def is_removed(self) -> bool:
        """Return true if dataset is removed."""
        return self.date_removed is not None

    def find_file(self, path: Union[Path, str]) -> Optional[DatasetFile]:
        """Find a file in the dataset using its relative path."""
        path = str(path)
        for file in self.dataset_files:
            if str(file.entity.path) == path and not file.is_removed():
                return file

    def update_files_from(self, current_dataset: "Dataset", date: datetime = None):
        """Check `current_files` to reuse existing entries and mark removed files."""
        new_files: Dict[str, DatasetFile] = {f.entity.path: f for f in self.files}
        current_files: Dict[str, DatasetFile] = {f.entity.path: f for f in current_dataset.files}

        files = []

        for path, file in new_files.items():
            # Use existing entries from `current_files` to avoid creating new ids
            current_file = current_files.pop(path, None)
            if current_file and file.is_equal_to(current_file):
                files.append(current_file)
            else:
                files.append(file)

        # NOTE: Whatever remains in `current_files` are removed in the newer version
        for removed_file in current_files.values():
            removed_file.remove(date)
            files.append(removed_file)

        self.dataset_files = files

    def update_metadata_from(self, other: "Dataset"):
        """Update metadata from another dataset."""
        editable_fields = [
            "creators",
            "date_created",
            "date_published",
            "derived_from",
            "description",
            "images",
            "in_language",
            "keywords",
            "license",
            "same_as",
            "title",
            "version",
        ]
        for name in editable_fields:
            value = getattr(other, name)
            setattr(self, name, value)

    def update_metadata(self, **kwargs):
        """Updates metadata."""
        editable_attributes = ["creators", "description", "keywords", "title"]
        for name, value in kwargs.items():
            if name not in editable_attributes:
                raise errors.ParameterError(f"Cannot edit field: '{name}'")
            if value and value != getattr(self, name):
                setattr(self, name, value)

    def unlink_file(self, path, missing_ok=False) -> Optional[DatasetFile]:
        """Mark a file as removed using its relative path."""
        assert not self.immutable, f"Dataset is immutable {self}"

        file = self.find_file(path)

        if not file:
            if not missing_ok:
                raise errors.InvalidFileOperation(f"File cannot be found: {path}")
            return

        file.remove()

        return file

    def add_or_update_files(self, files: Union[DatasetFile, List[DatasetFile]]):
        """Add new files or update existing files."""
        assert not self.immutable, f"Dataset is immutable {self}"

        if isinstance(files, DatasetFile):
            files = [files]

        new_files = []

        for file in files:
            existing_file = self.find_file(file.entity.path)
            if not existing_file:
                new_files.append(file)
            elif file.entity.checksum != existing_file.entity.checksum or file.date_added != existing_file.date_added:
                self.dataset_files.remove(existing_file)
                new_files.append(file)

        if not new_files:
            return

        self.dataset_files += new_files
        self._p_changed = True

    def clear_files(self):
        """Remove all files."""
        self.dataset_files = []

    def to_jsonld(self):
        """Create JSON-LD."""
        return DatasetSchema(flattened=True).dump(self)


class DatasetsProvenance:
    """A set of datasets."""

    def __init__(self, database: Database):
        # NOTE: If the database is not initialized yet, DatasetsProvenance should do/return nothing.

        try:
            has_database = database.get("datasets") is not None
        except errors.ObjectNotFoundError:
            has_database = False

        if has_database:
            # A map from name to datasets for current datasets
            self._datasets: Index = database["datasets"]
            # A map from id to datasets that keeps provenance chain tails for all current and removed datasets
            self._provenance_tails: Index = database["datasets-provenance-tails"]
        else:
            self._datasets = Index(name="datasets", object_type=Dataset, attribute="name")
            self._provenance_tails = Index(name="datasets-provenance-tails", object_type=Dataset, attribute="id")

        self._database: Database = database
        self._has_database = has_database

    @property
    def datasets(self) -> List[Dataset]:
        """Return an iterator of datasets."""
        return list(self._datasets.values())

    def get_by_id(self, id: str, immutable=False) -> Optional[Dataset]:
        """Return a dataset by its id."""
        if not self._has_database:
            return

        try:
            dataset = self._database.get_by_id(id)
        except errors.ObjectNotFoundError:
            pass
        else:
            assert isinstance(dataset, Dataset)
            if dataset.immutable and immutable:
                return dataset

            return dataset.copy()

    def get_by_name(self, name: str, immutable=False) -> Optional[Dataset]:
        """Return a dataset by its name."""
        dataset = self._datasets.get(name)
        if not dataset:
            return
        if not dataset.immutable or immutable:
            return dataset

        return dataset.copy()

    def get_provenance(self):
        """Return the provenance for all datasets."""
        return list(self._provenance_tails.values())

    def get_previous_version(self, dataset: Dataset) -> Optional[Dataset]:
        """Return the previous version of a dataset if any."""
        if not dataset.derived_from:
            return
        return self.get_by_id(dataset.derived_from)

    def add_or_update(self, dataset: Dataset, date: datetime = None, creator: Person = None):
        """Add/update a dataset according to its new content.

        NOTE: This functions always mutates the dataset.
        """
        assert isinstance(dataset, Dataset)

        if not self._has_database:
            return

        # NOTE: Dataset's name never changes, so, we use it to detect if a dataset should be mutated.
        current_dataset = self.get_by_name(dataset.name)

        if current_dataset:
            assert (
                not current_dataset.is_removed()
            ), f"Adding/Updating a removed dataset '{dataset.name}:{dataset.identifier}'"

            dataset.update_files_from(current_dataset, date=date)

            # NOTE: Always mutate a dataset to make sure an old identifier is not reused
            dataset.derive_from(current_dataset, creator=creator)
        else:
            assert (
                dataset.derived_from is None
            ), f"Parent dataset {dataset.derived_from} not found for '{dataset.name}:{dataset.identifier}'"

            # NOTE: This happens in migrations of broken projects
            current_dataset = self.get_by_id(dataset.id)
            if current_dataset:
                dataset.replace_identifier()

        self._datasets.add(dataset)

        self._update_database(dataset)

    def add_or_replace(self, dataset: Dataset, date: datetime = None):
        """Add/replace a dataset."""
        assert isinstance(dataset, Dataset)

        if not self._has_database:
            return

        current_dataset = self.get_by_name(dataset.name, immutable=True)

        if current_dataset:
            dataset.update_files_from(current_dataset, date=date)

            # NOTE: Copy metadata to the current dataset
            current_dataset.update_metadata_from(dataset)
            current_dataset.dataset_files = dataset.dataset_files
            dataset = current_dataset
        else:
            assert (
                dataset.derived_from is None
            ), f"Parent dataset {dataset.derived_from} not found for '{dataset.name}:{dataset.identifier}'"

            # NOTE: This happens in migrations of broken projects
            current_dataset = self.get_by_id(dataset.id)
            if current_dataset:
                dataset.replace_identifier()

        self._datasets.add(dataset)

        self._update_database(dataset)

    def remove(self, dataset, date: datetime = None, creator: Person = None):
        """Remove a dataset."""
        assert isinstance(dataset, Dataset)

        if not self._has_database:
            return

        # NOTE: Dataset's name never changes, so, we use it to detect if a dataset should be mutated.
        current_dataset = self._datasets.pop(dataset.name, None)

        if current_dataset:
            assert not current_dataset.is_removed(), f"Removing a removed dataset '{dataset.name}:{dataset.identifier}'"

            # NOTE: We always assign a new identifier to make sure an old identifier is not reused
            dataset.derive_from(current_dataset, creator=creator)
        else:
            # TODO: Should we raise here when migrating
            communication.warn(f"Deleting non-existing dataset '{dataset.name}'")

            assert (
                dataset.derived_from is None
            ), f"Parent dataset {dataset.derived_from} not found for '{dataset.name}:{dataset.identifier}'"

            # NOTE: This happens in migrations of broken projects
            current_dataset = self.get_by_id(dataset.id)
            if current_dataset:
                dataset.replace_identifier()

        dataset.remove(date)

        self._update_database(dataset)

    def _update_database(self, dataset):
        self._provenance_tails.pop(dataset.derived_from, None)
        self._provenance_tails.add(dataset)


class UrlSchema(JsonLDSchema):
    """Url schema."""

    class Meta:
        """Meta class."""

        rdf_type = schema.URL
        model = Url
        unknown = EXCLUDE

    id = fields.Id(missing=None)
    url = Uri(schema.url, missing=None)


class DatasetTagSchema(JsonLDSchema):
    """DatasetTag schema."""

    class Meta:
        """Meta class."""

        rdf_type = schema.PublicationEvent
        model = DatasetTag
        unknown = EXCLUDE

    commit = fields.String(schema.location)
    dataset = fields.String(schema.about)
    date_created = fields.DateTime(schema.startDate, missing=None, format="iso", extra_formats=("%Y-%m-%d",))
    description = fields.String(schema.description, missing=None)
    id = fields.Id()
    name = fields.String(schema.name)

    @pre_dump
    def fix_timezone(self, obj, many=False, **kwargs):
        """Pre dump hook."""
        if many:
            return [self.fix_timezone(o, many=False, **kwargs) for o in obj]
        object.__setattr__(obj, "date_created", self._fix_timezone(obj.date_created))
        return obj


class LanguageSchema(JsonLDSchema):
    """Language schema."""

    class Meta:
        """Meta class."""

        rdf_type = schema.Language
        model = Language
        unknown = EXCLUDE

    alternate_name = fields.String(schema.alternateName)
    name = fields.String(schema.name)


class ImageObjectSchema(JsonLDSchema):
    """ImageObject schema."""

    class Meta:
        """Meta class."""

        rdf_type = schema.ImageObject
        model = ImageObject
        unknown = EXCLUDE

    content_url = fields.String(schema.contentUrl)
    id = fields.Id(missing=None)
    position = fields.Integer(schema.position)


class RemoteEntitySchema(JsonLDSchema):
    """RemoteEntity schema."""

    class Meta:
        """Meta class."""

        rdf_type = [prov.Entity, schema.DigitalDocument]
        model = RemoteEntity
        unknown = EXCLUDE

    commit_sha = fields.String(renku.commit_sha)
    id = fields.Id()
    path = fields.String(prov.atLocation)
    url = fields.String(schema.url)


class DatasetFileSchema(JsonLDSchema):
    """DatasetFile schema."""

    class Meta:
        """Meta class."""

        rdf_type = [prov.Entity, schema.DigitalDocument, wfprov.Artifact]
        model = DatasetFile
        unknown = EXCLUDE

    based_on = Nested(schema.isBasedOn, RemoteEntitySchema, missing=None)
    date_added = DateTimeList(schema.dateCreated, format="iso", extra_formats=("%Y-%m-%d",))
    date_removed = fields.DateTime(prov.invalidatedAtTime, missing=None, format="iso")
    entity = Nested(prov.entity, EntitySchema)
    id = fields.Id()
    is_external = fields.Boolean(renku.external, missing=False)
    source = fields.String(renku.source, missing=None)


class DatasetSchema(JsonLDSchema):
    """Dataset schema."""

    class Meta:
        """Meta class."""

        rdf_type = [prov.Entity, schema.Dataset]
        model = Dataset
        unknown = EXCLUDE

    creators = Nested(schema.creator, PersonSchema, many=True)
    date_created = fields.DateTime(schema.dateCreated, missing=None, format="iso", extra_formats=("%Y-%m-%d",))
    date_removed = fields.DateTime(prov.invalidatedAtTime, missing=None, format="iso")
    date_published = fields.DateTime(
        schema.datePublished, missing=None, format="%Y-%m-%d", extra_formats=("iso", "%Y-%m-%dT%H:%M:%S")
    )
    # FIXME: Implement proper export
    # derived_from = Nested(prov.wasDerivedFrom, UrlSchema, missing=None)
    derived_from = fields.String(prov.wasDerivedFrom, missing=None)
    description = fields.String(schema.description, missing=None)
    dataset_files = Nested(schema.hasPart, DatasetFileSchema, many=True)
    id = fields.Id(missing=None)
    identifier = fields.String(schema.identifier)
    images = fields.Nested(schema.image, ImageObjectSchema, missing=None, many=True)
    in_language = Nested(schema.inLanguage, LanguageSchema, missing=None)
    keywords = fields.List(schema.keywords, fields.String(), missing=None)
    license = Uri(schema.license, missing=None)
    name = fields.String(renku.slug)
    initial_identifier = fields.String(renku.originalIdentifier)
    same_as = Nested(schema.sameAs, UrlSchema, missing=None)
    tags = Nested(schema.subjectOf, DatasetTagSchema, many=True)
    title = fields.String(schema.name)
    version = fields.String(schema.version, missing=None)


class DatasetCreatorsJson(marshmallow.Schema):
    """Schema for the dataset creators."""

    name = marshmallow.fields.String()
    email = marshmallow.fields.String()
    affiliation = marshmallow.fields.String()


class DatasetDetailsJson(marshmallow.Schema):
    """Serialize a dataset to a response object."""

    name = marshmallow.fields.String(required=True)
    version = marshmallow.fields.String(allow_none=True)
    created_at = marshmallow.fields.String(allow_none=True, attribute="date_created")

    title = marshmallow.fields.String()
    creators = marshmallow.fields.List(marshmallow.fields.Nested(DatasetCreatorsJson))
    description = marshmallow.fields.String()
    keywords = marshmallow.fields.List(marshmallow.fields.String())
    identifier = marshmallow.fields.String()


class DatasetFileDetailsJson(marshmallow.Schema):
    """Serialize dataset files to a response object."""

    path = marshmallow.fields.String()
    created = marshmallow.fields.DateTime()
    added = marshmallow.fields.DateTime()

    size = marshmallow.fields.String()
    is_lfs = marshmallow.fields.Boolean()

    dataset_id = marshmallow.fields.String()
    dataset_name = marshmallow.fields.String()

    creators = marshmallow.fields.List(marshmallow.fields.Nested(DatasetCreatorsJson))


class ImageObjectJson(marshmallow.Schema):
    """ImageObject json schema."""

    content_url = marshmallow.fields.String()
    position = marshmallow.fields.Integer()


class ImageObjectRequestJson(marshmallow.Schema):
    """ImageObject json schema."""

    file_id = marshmallow.fields.String()
    content_url = marshmallow.fields.String()
    position = marshmallow.fields.Integer()
    mirror_locally = marshmallow.fields.Bool(default=False)


def get_dataset_data_dir(client, dataset: Dataset) -> str:
    """Return default data directory for a dataset."""
    return os.path.join(client.data_dir, dataset.name)
