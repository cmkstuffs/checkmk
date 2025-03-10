#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from typing import Mapping, Optional

from .agent_based_api.v1 import register, SNMPTree
from .agent_based_api.v1.type_defs import StringTable
from .utils.fortinet import DETECT_FORTISANDBOX

Section = Mapping[str, int]


def parse_fortisandbox_mem_usage(string_table: StringTable) -> Optional[Section]:
    """
    >>> parse_fortisandbox_mem_usage(([["4", "260459760"]]))
    {'MemFree': 256042362470, 'MemTotal': 266710794240}
    """
    if not string_table:
        return None
    total = int(string_table[0][1]) * 1024
    return {
        "MemFree": int(round(total * (1 - float(string_table[0][0]) / 100))),
        "MemTotal": total,
    }


register.snmp_section(
    name="fortisandbox_mem_usage",
    parse_function=parse_fortisandbox_mem_usage,
    detect=DETECT_FORTISANDBOX,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.12356.118.3.1",
        oids=[
            "3",  # fsaSysMemUsage
            "4",  # fsaSysMemCapacity
        ],
    ),
)
