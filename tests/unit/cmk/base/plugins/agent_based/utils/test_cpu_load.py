#!/usr/bin/env python3
# Copyright (C) 2021 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from unittest.mock import Mock

from cmk.utils.hostaddress import HostName

from cmk.checkengine.checking import CheckPluginName

from cmk.base.api.agent_based.plugin_contexts import current_host, current_service
from cmk.base.plugins.agent_based.agent_based_api.v1 import Metric, Result, State
from cmk.base.plugins.agent_based.utils.cpu import Load, ProcessorType, Section, Threads
from cmk.base.plugins.agent_based.utils.cpu_load import check_cpu_load


def test_cpu_loads_fixed_levels() -> None:
    assert list(
        check_cpu_load(
            {
                "levels1": None,
                "levels5": None,
                "levels15": (2.0, 4.0),
            },
            Section(
                load=Load(0.5, 1.0, 1.5),
                num_cpus=4,
                threads=Threads(count=123),
                type=ProcessorType.physical,
            ),
        )
    ) == [
        Result(state=State.OK, summary="15 min load: 1.50"),
        Metric("load15", 1.5, levels=(8.0, 16.0)),  # levels multiplied by num_cpus
        Result(state=State.OK, summary="15 min load per core: 0.38 (4 physical cores)"),
        Result(state=State.OK, notice="1 min load: 0.50"),
        Metric("load1", 0.5, boundaries=(0, 4)),  # levels multiplied by num_cpus
        Result(state=State.OK, notice="1 min load per core: 0.12 (4 physical cores)"),
        Result(state=State.OK, notice="5 min load: 1.00"),
        Metric("load5", 1.0),  # levels multiplied by num_cpus
        Result(state=State.OK, notice="5 min load per core: 0.25 (4 physical cores)"),
    ]


def test_cpu_loads_predictive(mocker: Mock) -> None:
    # make sure cpu_load check can handle predictive values
    mocker.patch(
        "cmk.base.api.agent_based.utils.get_predictive_levels",
        return_value=(None, (2.2, 4.2, None, None)),
    )
    with current_host(HostName("unittest")), current_service(CheckPluginName("cpu_loads"), "item"):
        assert list(
            check_cpu_load(
                {
                    "levels1": None,
                    "levels5": None,
                    "levels15": {
                        "period": "minute",
                        "horizon": 1,
                        "levels_upper": ("absolute", (2.0, 4.0)),
                    },
                },
                Section(
                    load=Load(0.5, 1.0, 1.5),
                    num_cpus=4,
                    threads=Threads(count=123),
                ),
            )
        ) == [
            Result(state=State.OK, summary="15 min load: 1.50 (no reference for prediction yet)"),
            Metric("load15", 1.5, levels=(2.2, 4.2)),  # those are the predicted values
            Result(state=State.OK, summary="15 min load per core: 0.38 (4 cores)"),
            Result(state=State.OK, notice="1 min load: 0.50"),
            Metric("load1", 0.5, boundaries=(0, 4)),
            Result(state=State.OK, notice="1 min load per core: 0.12 (4 cores)"),
            Result(state=State.OK, notice="5 min load: 1.00"),
            Metric("load5", 1.0),
            Result(state=State.OK, notice="5 min load per core: 0.25 (4 cores)"),
        ]
