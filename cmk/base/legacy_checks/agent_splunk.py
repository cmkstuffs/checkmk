#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# {
#     'port': 443,
#     'password': 'comein',
#     'infos': ['license_state'],
#     'user': 'itsme'
# }


from typing import Any, Mapping, Optional, Sequence, Union

from cmk.base.check_api import passwordstore_get_cmdline
from cmk.base.config import special_agent_info


def agent_splunk_arguments(
    params: Mapping[str, Any], hostname: str, ipaddress: Optional[str]
) -> Sequence[Union[str, tuple[str, str, str]]]:
    args = []

    args += ["-P", params["protocol"]]
    args += ["-m", " ".join(params["infos"])]
    args += ["-u", params["user"]]
    args += ["-s", passwordstore_get_cmdline("%s", params["password"])]

    if "port" in params:
        args += ["-p", params["port"]]

    if "instance" in params:
        hostname = params["instance"]

    args += [hostname]

    return args


special_agent_info["splunk"] = agent_splunk_arguments
