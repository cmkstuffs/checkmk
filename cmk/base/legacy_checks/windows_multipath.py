#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Agent output:
# <<<windows_multipath>>>
# 4
# (yes, thats all)


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info


def inventory_windows_multipath(info):
    try:
        num_active = int(info[0][0])
    except (ValueError, IndexError):
        return []

    if num_active > 0:
        return [(None, num_active)]
    return []


def check_windows_multipath(item, params, info):
    num_active = int(info[0][0])

    yield 0, "Paths active: %s" % (num_active)

    if isinstance(params, tuple):
        num_paths, warn, crit = params
        warn_num = (warn / 100.0) * num_paths
        crit_num = (crit / 100.0) * num_paths
        if num_active < crit_num:
            state = 2
        elif num_active < warn_num:
            state = 1
        else:
            state = 0

        if state > 0:
            yield state, "(warn/crit below %d/%d)" % (warn_num, crit_num)
    else:
        if isinstance(params, int):
            num_paths = params
        else:
            num_paths = 4

        yield 0, "Expected paths: %s" % num_paths
        if num_active < num_paths:
            yield 2, "(crit below %d)" % num_paths
        elif num_active > num_paths:
            yield 1, "(warn at %d)" % num_paths


check_info["windows_multipath"] = LegacyCheckDefinition(
    service_name="Multipath",
    discovery_function=inventory_windows_multipath,
    check_function=check_windows_multipath,
    check_ruleset_name="windows_multipath",
)
