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
"""Represent dependency graph."""

from collections import deque
from pathlib import Path
from typing import List, Optional, Tuple

import networkx
from marshmallow import EXCLUDE

from renku.core.metadata.database import Database, Index
from renku.core.models.calamus import JsonLDSchema, Nested, schema
from renku.core.models.workflow.plan import Plan, PlanSchema


class DependencyGraph:
    """A graph of all execution templates (Plans)."""

    # TODO: dependency graph can have cycles in it because up until now there was no check to prevent this

    def __init__(self, plans: Index):
        """Initialized."""
        self._plans: Index = plans

        # NOTE: If we connect nodes then all ghost objects will be loaded which is not what we want
        self._graph = None

    @classmethod
    def from_database(cls, database: Database) -> "DependencyGraph":
        """Return an instance from a metadata database."""
        plans = database["plans"]
        self = DependencyGraph(plans=plans)

        return self

    @property
    def graph(self) -> networkx.DiGraph:
        """A networkx.DiGraph containing all plans."""
        if not self._graph:
            self._graph = networkx.DiGraph()
            self._graph.add_nodes_from(self._plans.values())
            self._connect_all_nodes()

        return self._graph

    @property
    def plans(self) -> List[Plan]:
        """A list of all plans in the graph."""
        return list(self._plans.values())

    def add(self, plan: Plan) -> Plan:
        """Add a plan to the graph if a similar plan does not exists."""
        existing_plan = self._find_similar_plan(plan)
        if existing_plan:
            return existing_plan

        assert not any(
            [p for p in self._plans.values() if p.name == plan.name]
        ), f"Duplicate name {plan.id}, {plan.name}"
        # NOTE: It's possible to have the same identifier but different list of arguments (e.g.
        # test_rerun_with_edited_inputs). We return the existing plan and use the new plan to determine rerun params.
        plan_with_same_id = self._plans.get(plan.id)
        if plan_with_same_id:
            return plan_with_same_id
        assert not any([p for p in self._plans.values() if p.id == plan.id]), f"Identifier exists {plan.id}"
        self._add_helper(plan)

        # FIXME some existing projects have cyclic dependency; make this check outside this model.
        # assert networkx.algorithms.dag.is_directed_acyclic_graph(self.graph)

        return plan

    def _find_similar_plan(self, plan: Plan) -> Optional[Plan]:
        """Search for a similar plan and return it."""
        for p in self._plans.values():
            if p.is_similar_to(plan):
                return p

    def _add_helper(self, plan: Plan):
        self._plans.add(plan)

        self.graph.add_node(plan)
        self._connect_node_to_others(node=plan)

    def _connect_all_nodes(self):
        for node in self.graph:
            self._connect_node_to_others(node)

    def _connect_node_to_others(self, node: Plan):
        for other_node in self.graph:
            self._connect_two_nodes(from_=node, to_=other_node)
            self._connect_two_nodes(from_=other_node, to_=node)

    def _connect_two_nodes(self, from_: Plan, to_: Plan):
        for o in from_.outputs:
            for i in to_.inputs:
                if DependencyGraph._is_super_path(o.default_value, i.default_value):
                    self.graph.add_edge(from_, to_, name=o.default_value)

    def visualize_graph(self):
        """Visualize graph using matplotlib."""
        networkx.draw(self.graph, with_labels=True, labels={n: n.name for n in self.graph.nodes})

        pos = networkx.spring_layout(self.graph)
        edge_labels = networkx.get_edge_attributes(self.graph, "name")
        networkx.draw_networkx_edge_labels(self.graph, pos=pos, edge_labels=edge_labels)

    def to_png(self, path):
        """Create a PNG image from graph."""
        networkx.drawing.nx_pydot.to_pydot(self.graph).write_png(path)

    @staticmethod
    def _is_super_path(parent, child):
        parent = Path(parent).resolve()
        child = Path(child).resolve()
        return parent == child or parent in child.parents

    def get_dependent_paths(self, plan_id, path):
        """Get a list of downstream paths."""
        nodes = deque()
        node: Plan
        for node in self.graph:
            if plan_id == node.id and any(self._is_super_path(path, p.default_value) for p in node.inputs):
                nodes.append(node)

        paths = set()

        # TODO: This loops infinitely if there is a cycle in the graph
        while nodes:
            node = nodes.popleft()
            outputs_paths = [o.default_value for o in node.outputs]
            paths.update(outputs_paths)

            nodes.extend(self.graph.successors(node))

        return paths

    def get_downstream(self, modified_usages, deleted_usages) -> Tuple[List[Plan], List[Plan]]:
        """Return a list of Plans in topological order that should be updated."""

        def node_has_deleted_inputs(node_):
            for _, path_, _ in deleted_usages:
                if any(self._is_super_path(path_, p.default_value) for p in node_.inputs):
                    return True
            return False

        nodes = set()
        nodes_with_deleted_inputs = set()
        node: Plan
        for plan_id, path, _ in modified_usages:
            for node in self.graph:
                if plan_id == node.id and any(self._is_super_path(path, p.default_value) for p in node.inputs):
                    nodes.add(node)
                    nodes.update(networkx.algorithms.dag.descendants(self.graph, node))

        sorted_nodes = []
        for node in networkx.algorithms.dag.topological_sort(self.graph):
            if node in nodes:
                if node_has_deleted_inputs(node):
                    nodes_with_deleted_inputs.add(node)
                else:
                    sorted_nodes.append(node)

        return sorted_nodes, list(nodes_with_deleted_inputs)


class DependencyGraphSchema(JsonLDSchema):
    """DependencyGraph schema."""

    class Meta:
        """Meta class."""

        rdf_type = [schema.Collection]
        model = DependencyGraph
        unknown = EXCLUDE

    _plans = Nested(schema.hasPart, PlanSchema, init_name="plans", many=True, missing=None)
