#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.check_legacy_includes.humidity import check_humidity
from cmk.base.check_legacy_includes.ispro import ispro_sensors_alarm_states
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree
from cmk.base.plugins.agent_based.utils.ispro import DETECT_ISPRO_SENSORS

# .1.3.6.1.4.1.19011.1.3.2.1.3.1.2.1.2.1 "Humidity-R" --> ISPRO-MIB::isDeviceMonitorHumidityName
# .1.3.6.1.4.1.19011.1.3.2.1.3.1.2.1.3.1 4407 --> ISPRO-MIB::isDeviceMonitorHumidity
# .1.3.6.1.4.1.19011.1.3.2.1.3.1.2.1.4.1 3 --> ISPRO-MIB::isDeviceMonitorHumidityAlarm


def inventory_ispro_sensors_humid(info):
    return [(name, None) for name, _reading_str, status in info if status not in ["1", "2"]]


def check_ispro_sensors_humid(item, params, info):
    for name, reading_str, status in info:
        if item == name:
            devstatus, devstatus_name = ispro_sensors_alarm_states(status)
            yield devstatus, "Device status: %s" % devstatus_name
            yield check_humidity(float(reading_str) / 100.0, params)


check_info["ispro_sensors_humid"] = LegacyCheckDefinition(
    detect=DETECT_ISPRO_SENSORS,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.19011.1.3.2.1.3.1.2.1",
        oids=["2", "3", "4"],
    ),
    service_name="Humidity %s",
    discovery_function=inventory_ispro_sensors_humid,
    check_function=check_ispro_sensors_humid,
    check_ruleset_name="humidity",
)
