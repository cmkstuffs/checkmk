#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree
from cmk.base.plugins.agent_based.utils.huawei import DETECT_HUAWEI_OSN

# The dBm should not get too low. So we check only for lower levels


def inventory_huawei_osn_laser(info):
    for line in info:
        yield (line[0], None)


def check_huawei_osn_laser(item, params, info):
    def check_state(reading, params):
        warn, crit = params
        if reading <= crit:
            state = 2
        elif reading <= warn:
            state = 1
        else:
            state = 0

        if state:
            return state, "(warn/crit below %s/%s dBm)" % (warn, crit)
        return 0, None

    for line in info:
        if item == line[0]:
            dbm_in = float(line[2]) / 10
            dbm_out = float(line[1]) / 10

            warn_in, crit_in = params["levels_low_in"]
            warn_out, crit_out = params["levels_low_out"]

            # In
            yield 0, "In: %.1f dBm" % dbm_in, [
                ("input_signal_power_dBm", dbm_in, warn_in, crit_in),
            ]
            yield check_state(dbm_in, (warn_in, crit_in))

            # And out
            yield 0, "Out: %.1f dBm" % dbm_out, [
                ("output_signal_power_dBm", dbm_out, warn_out, crit_out)
            ]
            yield check_state(dbm_out, (warn_out, crit_out))

            # FEC Correction
            fec_before = line[3]
            fec_after = line[4]
            if not fec_before == "" and not fec_after == "":
                yield 0, "FEC Correction before/after: %s/%s" % (fec_before, fec_after)


check_info["huawei_osn_laser"] = LegacyCheckDefinition(
    detect=DETECT_HUAWEI_OSN,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.2011.2.25.3.40.50.119.10.1",
        oids=["6.200", "2.200", "2.203", "2.252", "2.253"],
    ),
    service_name="Laser %s",
    discovery_function=inventory_huawei_osn_laser,
    check_function=check_huawei_osn_laser,
    check_ruleset_name="huawei_osn_laser",
    check_default_parameters={
        "levels_low_in": (-160.0, -180.0),
        "levels_low_out": (-35.0, -40.0),
    },
)
