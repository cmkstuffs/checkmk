#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# fmt: off
# mypy: disable-error-code=var-annotated
checkname = "ceph_status"

mock_item_state = {
    "": {
        "ceph_status.epoch.rate" : (0, 175986),
    },
    "osds": {
        "ceph_osds.epoch.rate" : (0, 54070),
    }
}

info = [
    ["{"],
    ['"health":', "{"],
    ['"status":', '"HEALTH_OK",'],
    ['"checks":', "{},"],
    ['"mutes":', "[]"],
    ["},"],
    ['"election_epoch":', "175986,"],
    ['"osdmap":', "{"],
    ['"epoch":', "54070,"],
    ['"num_osds":', "32,"],
    ['"num_up_osds":', "32,"],
    ['"osd_up_since":', "1605039365,"],
    ['"num_in_osds":', "32,"],
    ['"osd_in_since":', "1605039365,"],
    ['"num_remapped_pgs":', "0"],
    ["},"],
    ['"progress_events":', "{}"],
    ["}"],
]

discovery = {"": [(None, {})], "osds": [(None, {})], "pgs": [], "mgrs": []}

checks = {
    "": [
        (
            None,
            {"epoch": (1, 3, 30)},
            [(0, "Health: OK", []), (0, "Epoch rate (30 minutes 0 seconds average): 0.00", [])],
        )
    ],
    "osds": [
        (
            None,
            {"epoch": (50, 100, 15), "num_out_osds": (7.0, 5.0), "num_down_osds": (7.0, 5.0)},
            [
                (0, "Epoch rate (15 minutes 0 seconds average): 0.00", []),
                (0, "OSDs: 32, Remapped PGs: 0", []),
                (0, "OSDs out: 0, 0%", []),
                (0, "OSDs down: 0, 0%", []),
            ],
        )
    ],
}
