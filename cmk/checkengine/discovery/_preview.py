#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from cmk.utils.exceptions import OnError
from cmk.utils.hostaddress import HostAddress, HostName
from cmk.utils.labels import DiscoveredHostLabelsStore, HostLabel, ServiceLabel
from cmk.utils.log import console
from cmk.utils.rulesets.ruleset_matcher import RulesetName
from cmk.utils.sectionname import SectionMap, SectionName
from cmk.utils.servicename import ServiceName
from cmk.utils.timeperiod import timeperiod_active

from cmk.checkengine.checking import (
    CheckPlugin,
    CheckPluginName,
    ConfiguredService,
    Item,
    ServiceID,
)
from cmk.checkengine.checkresults import ActiveCheckResult, MetricTuple, ServiceCheckResult
from cmk.checkengine.fetcher import FetcherFunction, HostKey
from cmk.checkengine.legacy import LegacyCheckParameters
from cmk.checkengine.parameters import TimespecificParameters
from cmk.checkengine.parser import group_by_host, ParserFunction
from cmk.checkengine.sectionparser import (
    make_providers,
    Provider,
    SectionPlugin,
    store_piggybacked_sections,
)
from cmk.checkengine.sectionparserutils import check_parsing_errors
from cmk.checkengine.summarize import SummarizerFunction

from ._autochecks import AutocheckEntry
from ._autodiscovery import _Transition, get_host_services
from ._discovery import DiscoveryPlugin
from ._host_labels import analyse_cluster_labels, discover_host_labels, HostLabelPlugin
from ._utils import QualifiedDiscovery

__all__ = ["CheckPreview", "CheckPreviewEntry", "get_check_preview"]


@dataclass(frozen=True)
class CheckPreviewEntry:
    check_source: str
    check_plugin_name: str
    ruleset_name: RulesetName | None
    item: Item
    discovered_parameters: LegacyCheckParameters
    effective_parameters: LegacyCheckParameters
    description: str
    state: int | None
    output: str
    # Service discovery never uses the perfdata in the check table. That entry
    # is constantly discarded, yet passed around(back and forth) as part of the
    # discovery result in the request elements. Some perfdata VALUES are not parsable
    # by ast.literal_eval such as "inf" it lead to ValueErrors. Thus keep perfdata empty
    metrics: list[MetricTuple]
    labels: dict[str, str]
    found_on_nodes: list[HostName]


@dataclass(frozen=True)
class CheckPreview:
    table: Sequence[CheckPreviewEntry]
    labels: QualifiedDiscovery[HostLabel]
    source_results: Mapping[str, ActiveCheckResult]
    kept_labels: Mapping[HostName, Sequence[HostLabel]]


def get_check_preview(
    host_name: HostName,
    ip_address: HostAddress | None,
    *,
    is_cluster: bool,
    cluster_nodes: Sequence[HostName],
    parser: ParserFunction,
    fetcher: FetcherFunction,
    summarizer: SummarizerFunction,
    section_plugins: SectionMap[SectionPlugin],
    section_error_handling: Callable[[SectionName, Sequence[object]], str],
    host_label_plugins: SectionMap[HostLabelPlugin],
    discovery_plugins: Mapping[CheckPluginName, DiscoveryPlugin],
    check_plugins: Mapping[CheckPluginName, CheckPlugin],
    ignore_service: Callable[[HostName, ServiceName], bool],
    ignore_plugin: Callable[[HostName, CheckPluginName], bool],
    get_effective_host: Callable[[HostName, ServiceName], HostName],
    find_service_description: Callable[[HostName, CheckPluginName, Item], ServiceName],
    compute_check_parameters: Callable[[HostName, AutocheckEntry], TimespecificParameters],
    enforced_services: Mapping[ServiceID, tuple[RulesetName, ConfiguredService]],
    on_error: OnError,
) -> CheckPreview:
    """Get the list of service of a host or cluster and guess the current state of
    all services if possible"""

    fetched = fetcher(host_name, ip_address=ip_address)
    parsed = parser((f[0], f[1]) for f in fetched)

    host_sections = parser((f[0], f[1]) for f in fetched)
    host_sections_by_host = group_by_host(
        (HostKey(s.hostname, s.source_type), r.ok) for s, r in host_sections if r.is_ok()
    )
    store_piggybacked_sections(host_sections_by_host)
    providers = make_providers(
        host_sections_by_host,
        section_plugins,
        error_handling=section_error_handling,
    )

    if is_cluster:
        host_labels, kept_labels = analyse_cluster_labels(
            host_name,
            cluster_nodes,
            discovered_host_labels={
                node_name: discover_host_labels(
                    node_name,
                    host_label_plugins,
                    providers=providers,
                    on_error=on_error,
                )
                for node_name in cluster_nodes
            },
            existing_host_labels={
                node_name: DiscoveredHostLabelsStore(node_name).load()
                for node_name in cluster_nodes
            },
        )
    else:
        host_labels = QualifiedDiscovery[HostLabel](
            preexisting=DiscoveredHostLabelsStore(host_name).load(),
            current=discover_host_labels(
                host_name,
                host_label_plugins,
                providers=providers,
                on_error=on_error,
            ),
        )
        kept_labels = {host_name: host_labels.present}

    for result in check_parsing_errors(
        itertools.chain.from_iterable(resolver.parsing_errors for resolver in providers.values())
    ):
        for line in result.details:
            console.warning(line)

    grouped_services = get_host_services(
        host_name,
        is_cluster=is_cluster,
        cluster_nodes=cluster_nodes,
        providers=providers,
        plugins=discovery_plugins,
        ignore_service=ignore_service,
        ignore_plugin=ignore_plugin,
        get_effective_host=get_effective_host,
        get_service_description=find_service_description,
        enforced_services=enforced_services,
        on_error=on_error,
    )

    passive_rows = [
        _check_preview_table_row(
            host_name,
            check_plugins=check_plugins,
            service=ConfiguredService(
                check_plugin_name=entry.check_plugin_name,
                item=entry.item,
                description=find_service_description(host_name, *entry.id()),
                parameters=compute_check_parameters(host_name, entry),
                discovered_parameters=entry.parameters,
                service_labels={n: ServiceLabel(n, v) for n, v in entry.service_labels.items()},
                is_enforced=False,
            ),
            check_source=check_source,
            providers=providers,
            found_on_nodes=found_on_nodes,
        )
        for check_source, services_with_nodes in grouped_services.items()
        for entry, found_on_nodes in services_with_nodes
    ] + [
        _check_preview_table_row(
            host_name,
            service=service,
            check_plugins=check_plugins,
            check_source="manual",  # "enforced" would be nicer
            providers=providers,
            found_on_nodes=[host_name],
        )
        for _ruleset_name, service in enforced_services.values()
    ]

    return CheckPreview(
        table=[*passive_rows],
        labels=host_labels,
        source_results={
            src.ident: result for (src, _sections), result in zip(parsed, summarizer(parsed))
        },
        kept_labels=kept_labels,
    )


def _check_preview_table_row(
    host_name: HostName,
    *,
    service: ConfiguredService,
    check_plugins: Mapping[CheckPluginName, CheckPlugin],
    check_source: _Transition | Literal["manual"],
    providers: Mapping[HostKey, Provider],
    found_on_nodes: Sequence[HostName],
) -> CheckPreviewEntry:
    check_plugin = check_plugins.get(service.check_plugin_name)
    ruleset_name = (
        str(check_plugin.ruleset_name) if check_plugin and check_plugin.ruleset_name else None
    )

    result = (
        check_plugin.function(host_name, service, providers=providers).result
        if check_plugin is not None
        else ServiceCheckResult.check_not_implemented()
    )

    def make_output() -> str:
        return (
            result.output
            or f"WAITING - {check_source.split('_')[-1].title()} check, cannot be done offline"
        )

    return CheckPreviewEntry(
        check_source=check_source,
        check_plugin_name=str(service.check_plugin_name),
        ruleset_name=ruleset_name,
        item=service.item,
        discovered_parameters=service.discovered_parameters,
        effective_parameters=service.parameters.preview(timeperiod_active),
        description=service.description,
        state=result.state,
        output=make_output(),
        metrics=[],
        labels={l.name: l.value for l in service.service_labels.values()},
        found_on_nodes=list(found_on_nodes),
    )
