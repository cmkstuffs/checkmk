#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Mapping, Sequence

import pytest

from tests.testlib import ActiveCheck

pytestmark = pytest.mark.checks

STATIC_ARGS = ["--inventory-as-check", "$HOSTNAME$"]


@pytest.mark.parametrize(
    "params,expected_args",
    [
        (
            {},
            ["--inv-fail-status=1", "--hw-changes=0", "--sw-changes=0", "--sw-missing=0"]
            + STATIC_ARGS,
        ),
        (
            {"timeout": 0},
            ["--inv-fail-status=1", "--hw-changes=0", "--sw-changes=0", "--sw-missing=0"]
            + STATIC_ARGS,
        ),
    ],
)
def test_check_cmk_inv_argument_parsing(
    params: Mapping[str, object], expected_args: Sequence[str]
) -> None:
    """Tests if all required arguments are present."""
    active_check = ActiveCheck("check_cmk_inv")
    assert active_check.run_argument_function(params) == expected_args
