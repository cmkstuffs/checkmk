#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.check_legacy_includes.temperature import check_temperature
from cmk.base.config import check_info


def inventory_innovaphone_temp(info):
    yield "Ambient", {}


def check_innovaphone_temp(item, params, info):
    return check_temperature(int(info[0][1]), params, "innovaphone_temp_%s" % item)


check_info["innovaphone_temp"] = LegacyCheckDefinition(
    service_name="Temperature %s",
    discovery_function=inventory_innovaphone_temp,
    check_function=check_innovaphone_temp,
    check_ruleset_name="temperature",
    check_default_parameters={"levels": (45.0, 50.0)},
)
