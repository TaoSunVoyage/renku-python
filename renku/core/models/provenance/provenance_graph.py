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
"""Represent provenance graph."""

import json
from pathlib import Path
from typing import Dict, List, Optional, Union

from marshmallow import EXCLUDE
from rdflib import ConjunctiveGraph

from renku.core.management.command_builder.command import inject
from renku.core.metadata.database import Database
from renku.core.models.calamus import JsonLDSchema, Nested, schema
from renku.core.models.provenance.activity import Activity, ActivityCollection, ActivitySchema


class ProvenanceGraph:
    """A graph of all executions (Activities)."""

    def __init__(self, activities: List[Activity] = None):
        self.activities: List[Activity] = activities or []

        self._custom_bindings: Dict[str, str] = {}
        self._graph: Optional[ConjunctiveGraph] = None
        self._loaded: bool = False
        # TODO: Remove _order and rely on Activity's ended_at_time and started_at_time for ordering
        self._order: int = len(self.activities) + 1
        self._path: Optional[Path] = None

    @property
    def custom_bindings(self) -> Dict[str, str]:
        """Return custom bindings."""
        return self._custom_bindings

    @custom_bindings.setter
    def custom_bindings(self, custom_bindings: Dict[str, str]):
        """Set custom prefix to namespace bindings."""
        self._custom_bindings = custom_bindings

    def add(self, node: Union[Activity, ActivityCollection]) -> None:
        """Add an Activity/ActivityCollection to the graph."""
        activity_collection = node if isinstance(node, ActivityCollection) else ActivityCollection(activities=[node])

        for activity in activity_collection.activities:
            assert not any([a for a in self.activities if a.id == activity.id]), f"Identifier exists {activity.id}"
            activity.order = self._order
            self._order += 1
            self.activities.append(activity)

        self._p_changed = True

    @classmethod
    @inject.autoparams()
    def from_database(cls, database: Database):
        """Return an instance from a metadata database."""
        activity_tree = database.get("activities")
        activities = list(activity_tree.values())
        self = ProvenanceGraph(activities=activities)
        # NOTE: If we sort then all ghost objects will be loaded which is not what we want
        # self.activities.sort(key=lambda e: e.order)
        return self

    @classmethod
    def from_json(cls, path: Union[Path, str], lazy: bool = False) -> "ProvenanceGraph":
        """Return an instance from a JSON file."""
        if Path(path).exists():
            if not lazy:
                with open(path) as file_:
                    data = json.load(file_)
                    self = cls.from_jsonld(data=data) if data else ProvenanceGraph(activities=[])
                    self.activities.sort(key=lambda e: e.order)
                    self._loaded = True
            else:
                self = ProvenanceGraph(activities=[])
                self._loaded = False
        else:
            self = ProvenanceGraph(activities=[])
            self._loaded = True

        self._path = Path(path)

        return self

    @classmethod
    def from_jsonld(cls, data) -> "ProvenanceGraph":
        """Create an instance from JSON-LD data."""
        if isinstance(data, cls):
            return data
        elif not isinstance(data, list):
            raise ValueError(data)

        self = ProvenanceGraphSchema(flattened=True).load(data)
        self._loaded = True

        return self

    def to_jsonld(self):
        """Create JSON-LD."""
        return ProvenanceGraphSchema(flattened=True).dump(self)

    def to_json(self, path=None):
        """Write an instance to file."""
        path = path or self._path
        data = self.to_jsonld()
        with open(path, "w", encoding="utf-8") as file_:
            json.dump(data, file_, ensure_ascii=False, sort_keys=True, indent=2)

    @property
    def rdf_graph(self):
        """Create an RDFLib ConjunctiveGraph."""
        self._create_rdf_graph()
        return self._graph

    def _create_rdf_graph(self):
        if self._graph:
            return

        self._graph = ConjunctiveGraph()

        if not self._path.exists():
            return

        self._graph.parse(location=str(self._path), format="json-ld")

        self._graph.bind("foaf", "http://xmlns.com/foaf/0.1/")
        self._graph.bind("oa", "http://www.w3.org/ns/oa#")
        self._graph.bind("prov", "http://www.w3.org/ns/prov#")
        self._graph.bind("renku", "https://swissdatasciencecenter.github.io/renku-ontology#")
        self._graph.bind("schema", "http://schema.org/")
        self._graph.bind("wf", "http://www.w3.org/2005/01/wf/flow#")
        self._graph.bind("wfprov", "http://purl.org/wf4ever/wfprov#")

        for prefix, namespace in self._custom_bindings.items():
            self._graph.bind(prefix, namespace)

    def get_latest_plans_usages(self):
        """Return a list of tuples with path and check of all Usage paths."""
        plan_orders = self.query(LATEST_PLAN_EXECUTION_ORDER)
        usages = self.query(ALL_USAGES)

        latest_usages = (u for u in usages for o in plan_orders if u[1] == o[1])

        return [(str(u[0]), str(u[-2]), str(u[-1])) for u in latest_usages]

    def query(self, query):
        """Run a SPARQL query and return the result."""
        self._create_rdf_graph()
        return self._graph.query(query)


class ProvenanceGraphSchema(JsonLDSchema):
    """ProvenanceGraph schema."""

    class Meta:
        """Meta class."""

        rdf_type = [schema.Collection]
        model = ProvenanceGraph
        unknown = EXCLUDE

    activities = Nested(schema.hasPart, ActivitySchema, many=True, missing=None)


LATEST_PLAN_EXECUTION_ORDER = """
    SELECT ?plan (MAX(?order) AS ?maxOrder)
    WHERE
    {
        ?activity a prov:Activity .
        ?activity prov:qualifiedAssociation/prov:hadPlan ?plan .
        ?activity renku:order ?order
    }
    GROUP BY ?plan
    """


ALL_USAGES = """
    SELECT ?plan ?order ?usage ?path ?checksum
    WHERE
    {
        ?activity a prov:Activity .
        ?activity prov:qualifiedAssociation/prov:hadPlan ?plan .
        ?activity renku:order ?order .
        ?activity prov:qualifiedUsage ?usage .
        ?usage prov:entity ?entity .
        ?entity prov:atLocation ?path .
        ?entity renku:checksum ?checksum .
    }
    """


LATEST_USAGES = """
    SELECT ?path ?checksum ?order ?maxOrder
    WHERE
    {
        {
            SELECT ?path ?checksum ?order
            WHERE
            {
                ?activity a prov:Activity .
                ?entity renku:checksum ?checksum .
                ?entity prov:atLocation ?path .
                ?entity (prov:qualifiedGeneration/prov:activity) ?activity .
                ?activity renku:order ?order
            }
        }
        .
        {
            SELECT ?path (MAX(?order_) AS ?maxOrder)
            WHERE
            {
                SELECT ?path ?order_
                WHERE
                {
                    ?activity a prov:Activity .
                    ?entity prov:atLocation ?path .
                    ?entity (prov:qualifiedGeneration/prov:activity) ?activity .
                    ?activity renku:order ?order_
                }
            }
            GROUP BY ?path
        }
        FILTER(?order = ?maxOrder)
    }
    """
