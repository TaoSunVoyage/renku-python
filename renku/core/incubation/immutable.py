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
"""An immutable class."""


class Immutable:
    """An immutable class.

    Subclasses are supposed to use __slots__ to define their members. They must call super().__init__(...) to initialize
    their instances. Immutable classes should only contain immutable members.
    """

    __slots__ = "__weakref__"

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            object.__setattr__(self, key, value)

    def __getstate__(self):
        return {name: getattr(self, name) for name in self.__slots__ if name != "__weakref__"}

    def __setattr__(self, name, value):
        # NOTE: This allows adding other attributes to a class that are not defined in `__slots__`
        if name in self.__slots__ and name != "__weakref__":
            raise TypeError("Cannot modify an immutable class")

        super().__setattr__(name, value)
