#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


def cmctc_translate_status(status):
    return {4: 0, 7: 1, 8: 1, 9: 2}.get(status, 3)  # ok  # warning  # too low  # too high


def cmctc_translate_status_text(status):
    return {
        1: "notAvail",
        2: "lost",
        3: "changed",
        4: "ok",
        5: "off",
        6: "on",
        7: "warning",
        8: "tooLow",
        9: "tooHigh",
    }.get(status, "UNKNOWN")
