#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import equals, SNMPTree

mbg_lantime_refclock_refmode_map = {
    "0": "notavailable",
    "1": "normalOperation",
    "2": "trackingSearching",
    "3": "antennaFaulty",
    "4": "warmBoot",
    "5": "coldBoot",
    "6": "antennaShortcircuit",
}

mbg_lantime_refclock_gpsstate_map = {
    "0": "not available",
    "1": "synchronized",
    "2": "not synchronized",
}

# number of good satellites
mbg_lantime_refclock_default_levels = (3, 3)


def inventory_mbg_lantime_refclock(info):
    if len(info) > 0 and len(info[0]) == 6:
        return [(None, mbg_lantime_refclock_default_levels)]
    return []


def check_mbg_lantime_refclock(item, params, info):
    if len(info) > 0 and len(info[0]) == 6:
        ref_mode, gps_state, gps_pos, gps_sat_good, gps_sat_total, _gps_mode = info[0]

        state = 0
        state_txt = []

        # Handle the reported refclock mode
        thr_txt = ""
        if ref_mode in ["0", "3", "6"]:
            state = max(state, 2)
            thr_txt = " (!!)"
        elif ref_mode in ["2", "4", "5"]:
            state = max(state, 1)
            thr_txt = " (!)"
        state_txt.append(
            "Refclock State: %s%s"
            % (mbg_lantime_refclock_refmode_map.get(ref_mode, "UNKNOWN"), thr_txt)
        )

        # Handle gps state
        thr_txt = ""
        if gps_state in ["0", "2"]:
            state = max(state, 2)
            thr_txt = " (!!)"
        state_txt.append(
            "GPS State: %s%s"
            % (mbg_lantime_refclock_gpsstate_map.get(gps_state, "UNKNOWN"), thr_txt)
        )

        # Add gps position
        state_txt.append(gps_pos)

        # Handle number of satellites
        thr_txt = ""
        if params[0] is not None and int(gps_sat_good) < params[1]:
            state = max(state, 2)
            thr_txt = " (!!)"
        elif params[1] is not None and int(gps_sat_good) < params[0]:
            state = max(state, 1)
            thr_txt = " (!)"
        state_txt.append("Satellites: %s/%s%s" % (gps_sat_good, gps_sat_total, thr_txt))

        perfdata = [("sat_good", gps_sat_good, params[0], params[1]), ("sat_total", gps_sat_total)]

        return (state, ", ".join(state_txt), perfdata)

    return (3, "Got no state information")


check_info["mbg_lantime_refclock"] = LegacyCheckDefinition(
    detect=equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.5597.3"),
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.5597.3.2",
        oids=["4", "6", "7", "9", "10", "16"],
    ),
    service_name="LANTIME Refclock",
    discovery_function=inventory_mbg_lantime_refclock,
    check_function=check_mbg_lantime_refclock,
)
