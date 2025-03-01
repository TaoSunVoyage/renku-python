# -*- coding: utf-8 -*-
#
# Copyright 2018-2021- Swiss Data Science Center (SDSC)
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
"""Represent an annotation for a workflow."""

from marshmallow import EXCLUDE

from renku.core.models.calamus import JsonLDSchema, dcterms, fields, oa


class Annotation:
    """Represents a custom annotation for a research object."""

    def __init__(self, *, id: str, body=None, source=None):
        self.id = id
        self.body = body
        self.source = source


class AnnotationSchema(JsonLDSchema):
    """Annotation schema."""

    class Meta:
        """Meta class."""

        rdf_type = oa.Annotation
        model = Annotation
        unknown = EXCLUDE

    id = fields.Id()
    body = fields.RawJsonLD(oa.hasBody)
    source = fields.Raw(dcterms.creator)
