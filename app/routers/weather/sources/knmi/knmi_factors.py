#!/usr/bin/env python
# -*- coding: utf-8 -*-

# SPDX-FileCopyrightText: 2019-2021 Alliander N.V.
#
# SPDX-License-Identifier: MPL-2.0

# factor name mapping

import json
import os


with open(os.path.join(os.path.dirname(__file__), "arome_var_map.json"), "r") as _f:
    arome_factors = json.load(_f)
