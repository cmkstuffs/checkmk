#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# <<<websphere_mq_queues>>>
# 0 CD.ISS.CATSOS.REPLY.C000052 5000
# 0 CD.ISS.COBA.REPLY.C000052 5000
# 0 CD.ISS.DEUBA.REPLY.C000052 5000
# 0 CD.ISS.TIQS.REPLY.C000052 5000
# 0 CD.ISS.VWD.REPLY.C000052 5000

# Old output
# <<<websphere_mq_queues>>>
# 0 CD.ISS.CATSOS.REPLY.C000052
# 0 CD.ISS.COBA.REPLY.C000052
# 0 CD.ISS.DEUBA.REPLY.C000052
# 0 CD.ISS.TIQS.REPLY.C000052
# 0 CD.ISS.VWD.REPLY.C000052

# Very new output
# <<<websphere_mq_queues>>>
# 0  BRK.REPLY.CONVERTQ  2016_04_08-15_31_43
# 0  BRK.REPLY.CONVERTQ  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REPLY.FAILUREQ  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REPLY.INQ  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REPLY.OUTQ  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REPLYQ.IMS.MILES  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REPLYQ.MILES  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REQUEST.FAILUREQ  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REQUEST.INQ  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  BRK.REQUESTQ.MILES  5000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  DEAD.QUEUE.IGNORE  100000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43
# 0  DEAD.QUEUE.SECURITY  100000  CURDEPTH(0)LGETDATE()LGETTIME() 2016_04_08-15_31_43


# mypy: disable-error-code="var-annotated"

import time

from cmk.base.check_api import check_levels, get_age_human_readable, LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import IgnoreResultsError, render

websphere_mq_queues_default_levels = {
    "message_count": (1000, 1200),
    "message_count_perc": (80.0, 90.0),
}


def parse_websphere_mq_queues(string_table):
    parsed = {}
    for line in string_table:
        if len(line) < 2:
            continue

        try:
            cur_depth = int(line[0])
        except ValueError:
            continue

        inst = parsed.setdefault(line[1], {})
        inst.setdefault("cur_depth", cur_depth)

        if len(line) >= 3:
            if line[2].isdigit():
                inst.setdefault("max_depth", int(line[2]))

            if len(line) > 3:
                for what in "".join(line[3:-1]).replace(" ", "").split(")"):
                    if "(" in what:
                        key, val = what.split("(")
                        inst.setdefault(key, val)

                try:
                    inst.setdefault(
                        "time_on_client", time.mktime(time.strptime(line[-1], "%Y_%m_%d-%H_%M_%S"))
                    )
                except ValueError:
                    pass

    return parsed


def inventory_websphere_mq_queues(parsed):
    return [(queue_name, websphere_mq_queues_default_levels) for queue_name in parsed]


def check_websphere_mq_queues(item, params, parsed):
    data = parsed.get(item)
    if data is None:
        raise IgnoreResultsError("Login into database failed")

    if isinstance(params, tuple):
        params = {
            "message_count": params,
            "message_count_perc": websphere_mq_queues_default_levels["message_count_perc"],
        }

    cur_depth = data["cur_depth"]
    yield check_levels(
        cur_depth,
        "queue",
        params.get("message_count", (None, None)),
        human_readable_func=lambda x: "%d" % x,
        infoname="Messages in queue",
    )

    max_depth = data.get("max_depth")
    if max_depth:
        # Just for ordering:
        # 1. message count
        # 2. message count percent
        used_perc = float(cur_depth) / max_depth * 100
        yield check_levels(
            used_perc,
            None,
            params.get("message_count_perc", (None, None)),
            human_readable_func=render.percent,
            infoname="Of max. %d messages" % max_depth,
        )

    if data.get("time_on_client") and "LGETDATE" in data and "LGETTIME" in data:
        lgetdate = data["LGETDATE"]
        lgettime = data["LGETTIME"]

        params = params.get("messages_not_processed", {})

        if cur_depth and lgetdate and lgettime:
            time_str = "%s %s" % (lgetdate, lgettime)
            time_diff = data["time_on_client"] - time.mktime(
                time.strptime(time_str, "%Y-%m-%d %H.%M.%S")
            )

            diff_state, diff_info, _diff_perf = check_levels(
                time_diff,
                None,
                params.get("age", (None, None)),
                human_readable_func=get_age_human_readable,
            )

            yield diff_state, "Messages not processed since %s" % diff_info

        elif cur_depth:
            yield params.get("state", 0), "No age of %d message%s not processed" % (
                cur_depth,
                cur_depth > 1 and "s" or "",
            )

        else:
            yield 0, "Messages processed"


check_info["websphere_mq_queues"] = LegacyCheckDefinition(
    parse_function=parse_websphere_mq_queues,
    service_name="MQ Queue %s",
    discovery_function=inventory_websphere_mq_queues,
    check_function=check_websphere_mq_queues,
    check_ruleset_name="websphere_mq",
)
