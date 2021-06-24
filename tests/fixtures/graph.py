# -*- coding: utf-8 -*-
#
# Copyright 2021 Swiss Data Science Center (SDSC)
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
"""Renku fixtures for graph management."""
import pytest


@pytest.fixture
def client_with_new_graph(client):
    """A client with new graph metadata."""
    from renku.core.incubation.graph import generate_graph

    generate_graph().build().execute(force=True)

    yield client


@pytest.fixture
def datasets_provenance():
    """A function to return DatasetsProvenance for a client."""
    from renku.core.incubation.database import Database
    from renku.core.models.dataset import DatasetsProvenance

    def get_datasets_provenance(client):
        """Return dataset provenance if available."""
        assert client.has_graph_files()

        database = Database.from_path(client.database_path)
        return DatasetsProvenance.from_database(database)

    return get_datasets_provenance
