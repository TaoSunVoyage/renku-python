# -*- coding: utf-8 -*-
#
# Copyright 2018 - Swiss Data Science Center (SDSC)
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
"""Convert models to Common Workflow Language."""

import os
from collections import OrderedDict

from attr._compat import iteritems
from attr._funcs import has
from attr._make import fields

from renga._compat import Path


class CWLClass(object):
    """Include ``class`` field in serialized object."""


def ascwl(inst, recurse=True, filter=None, dict_factory=dict,
          retain_collection_types=False, basedir=None):
    """Return the ``attrs`` attribute values of *inst* as a dict.

    Support ``jsonldPredicate`` in a field metadata for generating
    mappings from lists.

    Adapted from ``attr._funcs``.
    """
    attrs = fields(inst.__class__)
    rv = dict_factory()

    def convert_value(v):
        """Convert special types."""
        if isinstance(v, Path):
            return os.path.relpath(v, basedir) if basedir else str(v)
        return v

    for a in attrs:
        v = getattr(inst, a.name)
        if filter is not None and not filter(a, v):
            continue
        if recurse is True:
            if has(v.__class__):
                rv[a.name] = ascwl(v, recurse=True, filter=filter,
                                   dict_factory=dict_factory, basedir=basedir)

            elif isinstance(v, (tuple, list, set)):
                cf = v.__class__ if retain_collection_types is True else list
                rv[a.name] = cf([
                    ascwl(i, recurse=True, filter=filter,
                          dict_factory=dict_factory, basedir=basedir)
                    if has(i.__class__) else i
                    for i in v
                ])

                if 'jsonldPredicate' in a.metadata:
                    k = a.metadata['jsonldPredicate'].get('mapSubject')
                    if k:
                        vv = dict_factory()
                        for i in rv[a.name]:
                            kk = i.pop(k)
                            vv[kk] = i
                        rv[a.name] = vv

            elif isinstance(v, dict):
                df = dict_factory
                rv[a.name] = df((
                    ascwl(kk, dict_factory=df, basedir=basedir)
                    if has(kk.__class__) else kk,
                    ascwl(vv, dict_factory=df, basedir=basedir)
                    if has(vv.__class__) else vv)
                    for kk, vv in iteritems(v))
            else:
                rv[a.name] = convert_value(v)
        else:
            rv[a.name] = convert_value(v)

    if isinstance(inst, CWLClass):
        rv['class'] = inst.__class__.__name__

    return rv
