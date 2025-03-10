#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import check_levels, LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree
from cmk.base.plugins.agent_based.utils.liebert import DETECT_LIEBERT, parse_liebert_float

# example output
# .1.3.6.1.4.1.476.1.42.3.9.20.1.10.1.2.1.5303 Free Cooling Valve Open Position
# .1.3.6.1.4.1.476.1.42.3.9.20.1.20.1.2.1.5303 0
# .1.3.6.1.4.1.476.1.42.3.9.20.1.30.1.2.1.5303 %


def discover_liebert_cooling_position(section):
    yield from ((item, {}) for item in section if item.startswith("Free Cooling"))


def check_liebert_cooling_position(item, params, parsed):
    if not (data := parsed.get(item)):
        return

    yield check_levels(
        data[0],
        "capacity_perc",
        (params.get("max_capacity", (None, None)) + params.get("min_capacity", (None, None))),
        unit=data[1],
    )


check_info["liebert_cooling_position"] = LegacyCheckDefinition(
    detect=DETECT_LIEBERT,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.476.1.42.3.9.20.1",
        oids=["10.1.2.1.5303", "20.1.2.1.5303", "30.1.2.1.5303"],
    ),
    parse_function=parse_liebert_float,
    service_name="%s",
    discovery_function=discover_liebert_cooling_position,
    check_function=check_liebert_cooling_position,
    check_ruleset_name="liebert_cooling_position",
    check_default_parameters={
        "min_capacity": (90, 80),
    },
)
