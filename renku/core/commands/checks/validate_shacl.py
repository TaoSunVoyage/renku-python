# -*- coding: utf-8 -*-
#
# Copyright 2020 - Swiss Data Science Center (SDSC)
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
"""Check KG structure using SHACL."""
import pyld
import yaml
from rdflib.namespace import Namespace
from rdflib.term import BNode

from renku.core.commands.echo import WARNING
from renku.core.models.jsonld import NoDatesSafeLoader
from renku.core.utils.shacl import validate_graph


def _shacl_graph_to_string(graph):
    """Converts a shacl validation graph into human readable format."""
    sh = Namespace("http://www.w3.org/ns/shacl#")

    problems = []

    for _, result in graph.subject_objects(sh.result):
        path = graph.value(result, sh.resultPath)
        res = graph.value(result, sh.resultMessage)

        if res:
            message = "{0}: {1}".format(path, res)
        else:
            kind = graph.value(result, sh.sourceConstraintComponent)
            focusNode = graph.value(result, sh.focusNode)

            if isinstance(focusNode, BNode):
                focusNode = "<Anonymous>"

            message = "{0}: Type: {1}, Node ID: {2}".format(path, kind, focusNode)

        problems.append(message)

    return "\n\t".join(problems)


def check_project_structure(client):
    """Validate project metadata against SHACL."""
    project_path = client.renku_metadata_path

    conform, graph, t = _check_shacl_structure_for_path(project_path)

    if conform:
        return True, None

    problems = "{0}Invalid structure of project metadata\n\t{1}".format(WARNING, _shacl_graph_to_string(graph))

    return False, problems


def check_datasets_structure(client):
    """Validate dataset metadata against SHACL."""
    ok = True

    problems = [f"{WARNING}Invalid structure of dataset metadata"]

    for dataset in client.datasets.values():
        data = dataset.to_jsonld()
        try:
            conform, graph, t = _check_shacl_structure(data)
        except (Exception, BaseException) as e:
            problems.append(f"Couldn't validate dataset '{dataset.name}': {e}\n\n")
            continue

        if conform:
            continue

        ok = False

        problems.append(f"{dataset.name}\n\t{_shacl_graph_to_string(graph)}\n")

    if ok:
        return True, None

    return False, "\n".join(problems)


def _check_shacl_structure_for_path(path):
    with path.open(mode="r") as fp:
        data = yaml.load(fp, Loader=NoDatesSafeLoader) or {}

    return _check_shacl_structure(data)


def _check_shacl_structure(data):
    """Validates all metadata against the SHACL schema."""
    rdf = pyld.jsonld.to_rdf(data, options={"format": "application/n-quads", "produceGeneralizedRdf": True})

    return validate_graph(rdf)
