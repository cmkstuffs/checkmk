#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Author: Lars Michelsen <lm@mathias-kettner.de>

# Blades:
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.3'  => 'cpqRackServerBladeIndex',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.4'  => 'cpqRackServerBladeName',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.6'  => 'cpqRackServerBladePartNumber',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.7'  => 'cpqRackServerBladeSparePartNumber',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.8'  => 'cpqRackServerBladePosition',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.12' => 'cpqRackServerBladePresent',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.16' => 'cpqRackServerBladeSerialNum',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.17' => 'cpqRackServerBladeProductId',
# Seems not to be implemented:
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.21' => 'cpqRackServerBladeStatus',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.22' => 'cpqRackServerBladeFaultMajor',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.23' => 'cpqRackServerBladeFaultMinor',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.24' => 'cpqRackServerBladeFaultDiagnosticString',
# '.1.3.6.1.4.1.232.22.2.4.1.1.1.25' => 'cpqRackServerBladePowered',


from cmk.base.check_api import LegacyCheckDefinition, saveint
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree
from cmk.base.plugins.agent_based.utils.hp import DETECT_HP_BLADE

# GENERAL MAPS:

hp_blade_present_map = {1: "other", 2: "absent", 3: "present"}
hp_blade_status_map = {1: "Other", 2: "Ok", 3: "Degraded", 4: "Failed"}

hp_blade_status2nagios_map = {
    "Other": 2,
    "Ok": 0,
    "Degraded": 1,
    "Failed": 2,
}


def inventory_hp_blade_blades(info):
    return [
        (line[0], None) for line in info if hp_blade_present_map.get(int(line[1]), "") == "present"
    ]


def check_hp_blade_blades(item, params, info):
    for line in info:
        if line[0] == item:
            present_state = hp_blade_present_map[int(line[1])]
            if present_state != "present":
                return (
                    2,
                    "Blade was present but is not available anymore"
                    " (Present state: %s)" % present_state,
                )

            # Status field can be an empty string.
            # Seems not to be implemented. The MIB file tells me that this value
            # should represent a state but is empty. So set it to "fake" OK and
            # display the other gathered information.
            state = 2 if line[2] == "" else line[2]
            state = saveint(state)

            snmp_state = hp_blade_status_map[state]
            status = hp_blade_status2nagios_map[snmp_state]
            return (
                status,
                "Blade status is %s (Product: %s Name: %s S/N: %s)"
                % (snmp_state, line[3], line[4], line[5]),
            )
    return (3, "item not found in snmp data")


check_info["hp_blade_blades"] = LegacyCheckDefinition(
    detect=DETECT_HP_BLADE,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.232.22.2.4.1.1.1",
        oids=["3", "12", "21", "17", "4", "16"],
    ),
    service_name="Blade %s",
    discovery_function=inventory_hp_blade_blades,
    check_function=check_hp_blade_blades,
)
