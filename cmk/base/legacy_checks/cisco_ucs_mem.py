#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.check_legacy_includes.cisco_ucs import DETECT, map_operability, map_presence
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

# comNET GmbH, Fabian Binder - 2018-05-08

# .1.3.6.1.4.1.9.9.719.1.30.11.1.3  cucsMemoryUnitRn
# .1.3.6.1.4.1.9.9.719.1.30.11.1.19 cucsMemoryUnitSerial
# .1.3.6.1.4.1.9.9.719.1.30.11.1.23 cucsMemoryUnitType
# .1.3.6.1.4.1.9.9.719.1.30.11.1.6  cucsMemoryUnitCapacity
# .1.3.6.1.4.1.9.9.719.1.30.11.1.14 cucsMemoryUnitOperability
# .1.3.6.1.4.1.9.9.719.1.30.11.1.17 cucsMemoryUnitPresence

map_memtype = {
    "0": (0, "undiscovered"),
    "1": (0, "other"),
    "2": (0, "unknown"),
    "3": (0, "dram"),
    "4": (0, "edram"),
    "5": (0, "vram"),
    "6": (0, "sram"),
    "7": (0, "ram"),
    "8": (0, "rom"),
    "9": (0, "flash"),
    "10": (0, "eeprom"),
    "11": (0, "feprom"),
    "12": (0, "eprom"),
    "13": (0, "cdram"),
    "14": (0, "n3DRAM"),
    "15": (0, "sdram"),
    "16": (0, "sgram"),
    "17": (0, "rdram"),
    "18": (0, "ddr"),
    "19": (0, "ddr2"),
    "20": (0, "ddr2FbDimm"),
    "24": (0, "ddr3"),
    "25": (0, "fbd2"),
    "26": (0, "ddr4"),
}


def inventory_cisco_ucs_mem(info):
    for name, _serial, _memtype, _capacity, _status, presence in info:
        if presence != "11":  # do not discover missing units
            yield name, None


def check_cisco_ucs_mem(item, _no_params, info):
    for name, serial, memtype, capacity, status, presence in info:
        if name == item:
            state, state_readable = map_operability.get(
                status, (3, "Unknown, status code %s" % status)
            )
            presence_state, presence_readable = map_presence.get(
                presence, (3, "Unknown, status code %s" % presence)
            )
            memtype_state, memtype_readable = map_memtype.get(
                memtype, (3, "Unknown memory type %s" % memtype)
            )
            yield state, "Status: %s" % state_readable
            yield presence_state, "Presence: %s" % presence_readable
            yield memtype_state, "Type: %s" % memtype_readable
            yield 0, "Size: %s MB, SN: %s" % (capacity, serial)


check_info["cisco_ucs_mem"] = LegacyCheckDefinition(
    detect=DETECT,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.9.9.719.1.30.11.1",
        oids=["3", "19", "23", "6", "14", "17"],
    ),
    service_name="Memory %s",
    discovery_function=inventory_cisco_ucs_mem,
    check_function=check_cisco_ucs_mem,
)
