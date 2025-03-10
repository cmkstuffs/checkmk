#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Example for info:
# [['HTTP',  '1', '1682'],
#  ['SMTP',  '1', '216'],
#  ['POP3',  '1', '0'],
#  ['FTP',   '1', '1'],
#  ['HTTPS', '2', '0'],
#  ['IMAP',  '1', '48']]


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import equals, SNMPTree

cpsecure_sessions_default_levels = (2500, 5000)


def inventory_cpsecure_sessions(info):
    inventory = []
    for service, enabled, _sessions in info:
        if enabled == "1":
            inventory.append((service, cpsecure_sessions_default_levels))
    return inventory


def check_cpsecure_sessions(item, params, info):
    for service, enabled, sessions in info:
        if item == service:
            if enabled != "1":
                return 1, "service not enabled"
            num_sessions = int(sessions)
            warn, crit = params
            perfdata = [("sessions", num_sessions, warn, crit, 0)]

            if num_sessions >= crit:
                return 2, "%s sessions (critical at %d)" % (sessions, crit), perfdata
            if num_sessions >= warn:
                return 1, "%s sessions (warning at %d)" % (sessions, warn), perfdata
            return 0, "%s sessions" % sessions, perfdata

    return 3, "service not found"


check_info["cpsecure_sessions"] = LegacyCheckDefinition(
    detect=equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.26546.1.1.2"),
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.26546.3.1.2.1.1.1",
        oids=["1", "2", "3"],
    ),
    service_name="Number of %s sessions",
    discovery_function=inventory_cpsecure_sessions,
    check_function=check_cpsecure_sessions,
)
