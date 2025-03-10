#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


# mypy: disable-error-code="var-annotated"

from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info


def inventory_unitrends_replication(info):
    inventory = []
    for _application, _result, _complete, target, _instance in info:
        if target not in [x[0] for x in inventory]:
            inventory.append((target, None))
    return inventory


def check_unitrends_replication(item, _no_params, info):
    # this never gone be a blessed check :)
    replications = [x for x in info if x[3] == item]
    if len(replications) == 0:
        return 3, "No Entries found"
    not_successfull = [x for x in replications if x[1] != "Success"]
    if len(not_successfull) == 0:
        return 0, "All Replications in the last 24 hours Successfull"
    messages = []
    for _application, result, _complete, target, instance in not_successfull:
        messages.append("Target: %s, Result: %s, Instance: %s  " % (target, result, instance))
    # TODO: Maybe a good place to use multiline output here
    return 2, "Errors from the last 24 hours: " + "/ ".join(messages)


check_info["unitrends_replication"] = LegacyCheckDefinition(
    service_name="Replicaion %s",
    discovery_function=inventory_unitrends_replication,
    check_function=check_unitrends_replication,
)
