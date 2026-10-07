from __future__ import annotations

import inspect
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import oci

from config import (
    DIRECT_GETTERS,
    NAMESPACE_DISCOVERY,
    NAMESPACE_PATH_SLUGS,
    RESOURCE_ENRICHMENTS,
    TIME_QUERY_PROVIDERS,
)
from models import CompartmentNode, RelationshipTarget, ResourceRow, ResourceSpec


class ActiveRegionCatalog:
    """Refresh active-region resource names off the completion path."""

    REFRESH_SECONDS = 60

    def __init__(self, browser: OciCompartmentBrowser) -> None:
        self.browser = browser
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._index_lock = threading.Lock()
        self._scanned_compartments: set[str] = set()
        self._requested_compartment: str | None = None
        self._region: str | None = None
        self._snapshot: dict[tuple[str, str], tuple[str, ...]] = {}
        self._id_snapshot: dict[tuple[str, str], tuple[str, ...]] = {}
        self._resource_snapshot: dict[tuple[str, str], tuple[ResourceRow, ...]] = {}
        self._children_snapshot: dict[str, tuple[CompartmentNode, ...]] = {}
        self._collections_snapshot: dict[str, tuple[str, ...]] = {}
        self._refreshed_at = 0.0

    def start(self) -> None:
        if self._thread is None:
            with self._lock:
                self._requested_compartment = self.browser.current.id
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._wake.set()

    def refresh_soon(self) -> None:
        self._wake.set()

    def request_compartment(self, compartment_id: str) -> None:
        """Refresh completion inventory for the active compartment in the worker."""
        with self._lock:
            self._requested_compartment = compartment_id
        self._wake.set()

    def names_for(self, compartment_id: str, spec: ResourceSpec) -> tuple[str, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return self._snapshot.get((compartment_id, spec.qualified_name), ())

    def ids_for(self, compartment_id: str, spec: ResourceSpec) -> tuple[str, ...]:
        """Return cached OCI IDs; completion never performs a lookup."""
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return getattr(self, "_id_snapshot", {}).get(
                (compartment_id, spec.qualified_name), ()
            )

    def is_ready(self) -> bool:
        with self._lock:
            return self._region == self.browser.region()

    def children_for(self, parent_id: str) -> tuple[CompartmentNode, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return self._children_snapshot.get(parent_id, ())

    def collections_for(self, compartment_id: str) -> tuple[str, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return self._collections_snapshot.get(compartment_id, ())

    def resources_for(
        self, compartment_id: str, spec: ResourceSpec
    ) -> tuple[ResourceRow, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return getattr(self, "_resource_snapshot", {}).get(
                (compartment_id, spec.qualified_name), ()
            )

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "region": self._region,
                "refreshed_at": self._refreshed_at,
                "collections": len(self._snapshot),
            }

    def _run(self) -> None:
        while True:
            region = self.browser.region()
            try:
                children = self.browser.build_active_region_compartment_catalog()
            except oci.exceptions.ServiceError:
                children = None
            if children is not None and region == self.browser.region():
                with self._lock:
                    self._region = region
                    self._children_snapshot = children
                    self._snapshot = {}
                    self._id_snapshot = {}
                    self._resource_snapshot = {}
                    self._collections_snapshot = {}
                    self._refreshed_at = time.monotonic()
                    self._scanned_compartments = set()
                    compartment_id = self._requested_compartment
                if compartment_id is not None:
                    self.prime_compartment(compartment_id)
            self._wake.wait(self.REFRESH_SECONDS)
            if self._wake.is_set():
                self._wake.clear()

    def _publish_resources(
        self,
        region: str,
        snapshot: dict[tuple[str, str], tuple[str, ...]],
        ids: dict[tuple[str, str], tuple[str, ...]],
        rows: dict[tuple[str, str], tuple[ResourceRow, ...]],
    ) -> None:
        collections: dict[str, set[str]] = {}
        for compartment_id, qualified_name in snapshot:
            collections.setdefault(compartment_id, set()).add(qualified_name)
        with self._lock:
            if self._region != region or region != self.browser.region():
                return
            self._snapshot = snapshot
            self._id_snapshot = ids
            self._resource_snapshot = rows
            self._collections_snapshot = {
                key: tuple(sorted(value)) for key, value in collections.items()
            }
            self._refreshed_at = time.monotonic()

    def prime_compartment(self, compartment_id: str) -> None:
        """Populate one compartment's completion inventory when explicitly scheduled."""
        with self._index_lock:
            with self._lock:
                if (
                    self._region != self.browser.region()
                    or compartment_id in self._scanned_compartments
                ):
                    return
            try:
                snapshot = self.browser.build_active_region_catalog(
                    compartment_id=compartment_id
                )
            except oci.exceptions.ServiceError:
                return
            ids = dict(getattr(self.browser, "active_region_catalog_ids", {}))
            rows = dict(getattr(self.browser, "active_region_catalog_rows", {}))
            with self._lock:
                merged = {**self._snapshot, **snapshot}
                merged_ids = {**self._id_snapshot, **ids}
                merged_rows = {**self._resource_snapshot, **rows}
                self._scanned_compartments.add(compartment_id)
            self._publish_resources(
                self.browser.region(), merged, merged_ids, merged_rows
            )


class OciCompartmentBrowser:
    CANONICAL_SEARCH_VARIANTS: ClassVar[dict[str, tuple[str, str, str]]] = {
        "core.images": ("compartment_id", "core.custom-images", "core.platform-images"),
    }
    THROTTLE_RETRIES = 5
    THROTTLE_BASE_DELAY = 0.5
    PROJECTION_CACHE_TTL_SECONDS = 30.0
    # OCI Logging Search rejects windows longer than 14 days.  Use its maximum
    # supported range so a quiet log still exposes its newest retained entries.
    LOG_ENTRY_WINDOW_SECONDS = 14 * 24 * 60 * 60
    LOG_ENTRY_PAGE_SIZE = 50
    LOG_ENTRY_MAX_PAGE = 20
    CONTENT_MAX_BYTES = 64 * 1024
    OBJECT_LIST_MAX_PAGES = 20
    CATALOG_PAGE_SIZE = 1000
    CATALOG_PAGE_DELAY_SECONDS = 0.25
    CHILDREN_CACHE_TTL_SECONDS = 30.0
    # Short OCI service names that cannot be inferred by punctuation folding.
    # All other spellings are resolved from the SDK-derived resource registry.
    RESOURCE_ALIASES: ClassVar[dict[str, dict[str, str]]] = {
        "core": {
            "ipsec": "ip_sec_connections",
        },
        "generative_ai": {
            "generativeaiprojects": "projects",
        },
    }

    def __init__(self, profile: str | None = None) -> None:
        self.profile_name = profile
        self.config = (
            oci.config.from_file(profile_name=profile)
            if profile
            else oci.config.from_file()
        )
        self._rebuild_clients()
        self.tenancy_id = self.config["tenancy"]
        self.root = self._get_tenancy_root()
        self.current = self.root
        self.parents: list[CompartmentNode] = []
        self.name_cache: dict[str, str] = {}
        self.search_type_cache: dict[str, str] = {}
        self.children_cache: dict[
            tuple[str, str], tuple[float, tuple[CompartmentNode, ...]]
        ] = {}
        self.compartment_resource_name_cache: dict[
            tuple[str, str], tuple[float, dict[str, str]]
        ] = {}
        self.relationship_target_cache: dict[
            str, tuple[float, RelationshipTarget | None]
        ] = {}
        self.relationship_projection_cache: dict[
            tuple[str, str, str, str], tuple[float, tuple[ResourceRow, ...]]
        ] = {}
        self.log_relationship_cache: dict[
            str, tuple[float, tuple[ResourceRow, ...]]
        ] = {}
        self.tenancy_log_group_cache: dict[
            str, tuple[float, tuple[ResourceRow, ...]]
        ] = {}
        self.log_source_relation_cache: dict[
            tuple[str, str], tuple[float, frozenset[str]]
        ] = {}
        self.resource_specs = self._build_resource_specs()

    def _rebuild_clients(self) -> None:
        self.identity = oci.identity.IdentityClient(self.config)
        self.virtual_network = oci.core.VirtualNetworkClient(self.config)
        self.compute = oci.core.ComputeClient(self.config)
        self.compute_management = oci.core.ComputeManagementClient(self.config)
        self.blockstorage = oci.core.BlockstorageClient(self.config)
        self.container_engine = oci.container_engine.ContainerEngineClient(self.config)
        self.generative_ai = oci.generative_ai.GenerativeAiClient(self.config)
        self.generative_ai_agent = oci.generative_ai_agent.GenerativeAiAgentClient(
            self.config
        )
        self.load_balancer = oci.load_balancer.LoadBalancerClient(self.config)
        self.network_firewall = oci.network_firewall.NetworkFirewallClient(self.config)
        self.logging = oci.logging.LoggingManagementClient(self.config)
        self.logging_search = oci.loggingsearch.LogSearchClient(self.config)
        self.audit = oci.audit.AuditClient(self.config)
        self.monitoring = oci.monitoring.MonitoringClient(self.config)
        self.apm_query = oci.apm_traces.QueryClient(self.config)
        self.log_analytics = oci.log_analytics.LogAnalyticsClient(self.config)
        self.object_storage = oci.object_storage.ObjectStorageClient(self.config)
        self.artifacts = oci.artifacts.ArtifactsClient(self.config)
        self.devops = oci.devops.DevopsClient(self.config)
        self.apm_domain = oci.apm_control_plane.ApmDomainClient(self.config)
        self.dns = oci.dns.DnsClient(self.config)
        self.limits = oci.limits.LimitsClient(self.config)
        self.resource_manager = oci.resource_manager.ResourceManagerClient(self.config)
        self.resource_search = oci.resource_search.ResourceSearchClient(self.config)

    def region(self) -> str:
        return str(self.config.get("region", "-"))

    def set_region(self, region: str) -> None:
        old_region = self.region()
        self.config["region"] = region
        try:
            self._rebuild_clients()
            self._retry_oci_call(self.identity.get_tenancy, self.tenancy_id)
        except Exception:
            self.config["region"] = old_region
            self._rebuild_clients()
            raise

    def _get_tenancy_root(self) -> CompartmentNode:
        tenancy = self._retry_oci_call(self.identity.get_tenancy, self.tenancy_id).data
        return CompartmentNode(
            id=tenancy.id,
            name=tenancy.name,
            description=getattr(tenancy, "description", None),
            lifecycle_state=str(getattr(tenancy, "lifecycle_state", "ACTIVE")),
            parent_id=None,
        )

    def list_children(self, parent_id: str) -> list[CompartmentNode]:
        key = (self.region(), parent_id)
        cached = getattr(self, "children_cache", {}).get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        compartments = self._retry_oci_call(
            oci.pagination.list_call_get_all_results,
            self.identity.list_compartments,
            parent_id,
            compartment_id_in_subtree=False,
            access_level="ACCESSIBLE",
        ).data
        nodes = [
            CompartmentNode(
                id=item.id,
                name=item.name,
                description=item.description,
                lifecycle_state=str(item.lifecycle_state),
                parent_id=item.compartment_id,
            )
            for item in compartments
            if str(item.lifecycle_state) == "ACTIVE"
        ]
        for node in nodes:
            self.name_cache[node.id] = node.name
        result = tuple(sorted(nodes, key=lambda node: node.name.lower()))
        if not hasattr(self, "children_cache"):
            self.children_cache = {}
        self.children_cache[key] = (now + self.CHILDREN_CACHE_TTL_SECONDS, result)
        return list(result)

    def build_active_region_compartment_catalog(
        self,
    ) -> dict[str, tuple[CompartmentNode, ...]]:
        """Discover the accessible compartment tree once for completion."""
        compartments = self._retry_oci_call(
            oci.pagination.list_call_get_all_results,
            self.identity.list_compartments,
            self.root.id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        ).data
        children: dict[str, list[CompartmentNode]] = {}
        for item in compartments:
            if str(item.lifecycle_state) != "ACTIVE":
                continue
            node = CompartmentNode(
                id=item.id,
                name=item.name,
                description=item.description,
                lifecycle_state=str(item.lifecycle_state),
                parent_id=item.compartment_id,
            )
            self.name_cache[node.id] = node.name
            if node.parent_id is not None:
                children.setdefault(node.parent_id, []).append(node)
        return {
            parent_id: tuple(sorted(nodes, key=lambda node: node.name.casefold()))
            for parent_id, nodes in children.items()
        }

    def find_child(self, name: str) -> CompartmentNode | None:
        for child in self.list_children(self.current.id):
            if child.name == name:
                return child
        return None

    def get_path(self) -> str:
        chain = [*self.parents, self.current]
        return "/" + "/".join(node.name for node in chain)

    def change_directory(self, target: str) -> CompartmentNode:
        if target in ("", "."):
            return self.current
        if target in ("/", "~"):
            self.current = self.root
            self.parents = []
            return self.current
        if "/" not in target:
            return self._change_directory_one(target)

        absolute = target.startswith(("/", "~"))
        parts = [part for part in target.replace("~", "/", 1).split("/") if part]
        saved_current = self.current
        saved_parents = list(self.parents)
        try:
            if absolute:
                self.current = self.root
                self.parents = []
            for part in parts:
                self._change_directory_one(part)
            return self.current
        except Exception:
            self.current = saved_current
            self.parents = saved_parents
            raise

    def _change_directory_one(self, target: str) -> CompartmentNode:
        if target in ("", "."):
            return self.current
        if target == "..":
            if self.parents:
                self.current = self.parents.pop()
            return self.current
        child = self.find_child(target)
        if child is None:
            raise ValueError(f"compartment not found: {target}")
        self.parents.append(self.current)
        self.current = child
        return child

    def describe_current(self) -> str:
        return json.dumps(self.current_payload(), indent=2, sort_keys=True)

    def current_payload(self) -> dict[str, object]:
        if self.current.id == self.root.id:
            obj = self.identity.get_tenancy(self.current.id).data
        else:
            obj = self.identity.get_compartment(self.current.id).data
        return self._resolve_payload_ids(oci.util.to_dict(obj))

    def supported_resource_types(self) -> list[str]:
        return [spec.qualified_name for spec in self.resource_specs_for_namespace()]

    def resolve_name(self, ocid: str | None) -> str:
        if ocid in (None, "", "-"):
            return "-"
        value = str(ocid)
        if value in self.name_cache:
            cached = self.name_cache[value]
            if cached == value or cached.endswith(f"({value})"):
                return cached
            return f"{cached} ({value})"
        resolved = self._resolve_name_uncached(value)
        self.name_cache[value] = resolved
        if resolved == value or resolved.endswith(f"({value})"):
            return resolved
        return f"{resolved} ({value})"

    def _resource_spec_for_search_type(
        self, resource_type: object
    ) -> ResourceSpec | None:
        if not isinstance(resource_type, str):
            return None
        normalized = resource_type.casefold()
        matches = [
            spec
            for spec in self.resource_specs
            if self._search_type_for(spec).casefold() == normalized
        ]
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        suffix_matches = [
            spec
            for spec in self.resource_specs
            if normalized.endswith(self._search_type_for(spec).casefold())
        ]
        return suffix_matches[0] if len(suffix_matches) == 1 else None

    def search_type_for(self, spec: ResourceSpec) -> str:
        return getattr(self, "search_type_cache", {}).get(
            spec.qualified_name, self._search_type_for(spec)
        )

    def build_active_region_catalog(
        self,
        on_snapshot: Callable[
            [
                dict[tuple[str, str], tuple[str, ...]],
                dict[tuple[str, str], tuple[str, ...]],
                dict[tuple[str, str], tuple[ResourceRow, ...]],
            ],
            None,
        ]
        | None = None,
        compartment_id: str | None = None,
    ) -> dict[tuple[str, str], tuple[str, ...]]:
        """Build a completion-only catalog for the active OCI region."""
        query = "query all resources"
        if compartment_id is not None:
            escaped = compartment_id.replace("\\", "\\\\").replace("'", "\\'")
            query = f"query all resources where compartmentId = '{escaped}'"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        names: dict[tuple[str, str], set[str]] = {}
        ids: dict[tuple[str, str], set[str]] = {}
        rows: dict[tuple[str, str], list[ResourceRow]] = {}
        current = getattr(self, "current", None)
        target_compartment_id = compartment_id or getattr(current, "id", None)

        def snapshot() -> tuple[
            dict[tuple[str, str], tuple[str, ...]],
            dict[tuple[str, str], tuple[str, ...]],
            dict[tuple[str, str], tuple[ResourceRow, ...]],
        ]:
            return (
                {
                    key: tuple(sorted(value, key=str.casefold))
                    for key, value in names.items()
                },
                {key: tuple(sorted(value)) for key, value in ids.items()},
                {
                    key: tuple(self._uniquify_row_names(value))
                    for key, value in rows.items()
                },
            )

        page: str | None = None
        while True:
            response = self._retry_oci_call(
                self.resource_search.search_resources,
                details,
                limit=self.CATALOG_PAGE_SIZE,
                page=page,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            )
            items = list(response.data.items or [])
            for item in items:
                payload = oci.util.to_dict(item)
                if not isinstance(payload, dict):
                    continue
                spec = self._resource_spec_for_search_type(payload.get("resource_type"))
                item_compartment_id = payload.get("compartment_id")
                name = payload.get("display_name") or payload.get("name")
                identifier = payload.get("identifier") or payload.get("id")
                resource_type = payload.get("resource_type")
                if spec is not None and isinstance(resource_type, str):
                    previous = self.search_type_cache.get(spec.qualified_name)
                    if previous is None or len(resource_type) > len(previous):
                        self.search_type_cache[spec.qualified_name] = resource_type
                if (
                    spec is None
                    or not self._is_public_resource_spec(spec)
                    or not isinstance(item_compartment_id, str)
                    or not isinstance(name, str)
                    or not name
                ):
                    continue
                names.setdefault((item_compartment_id, spec.qualified_name), set()).add(
                    self._humanize_name(name)
                )
                if isinstance(identifier, str) and identifier.startswith("ocid1."):
                    ids.setdefault(
                        (item_compartment_id, spec.qualified_name), set()
                    ).add(identifier)
                    rows.setdefault(
                        (item_compartment_id, spec.qualified_name), []
                    ).append(
                        ResourceRow(
                            name=self._humanize_name(name),
                            state=str(payload.get("lifecycle_state") or "-"),
                            id=identifier,
                            payload=payload,
                        )
                    )
            if on_snapshot is not None:
                on_snapshot(*snapshot())
            page = getattr(response, "next_page", None)
            if not page or not items:
                break
            time.sleep(self.CATALOG_PAGE_DELAY_SECONDS)
        if isinstance(target_compartment_id, str):
            for spec, direct_rows in self._direct_catalog_resources(
                target_compartment_id
            ):
                key = (target_compartment_id, spec.qualified_name)
                names.setdefault(key, set()).update(row.name for row in direct_rows)
                ids.setdefault(key, set()).update(row.id for row in direct_rows)
                rows.setdefault(key, []).extend(direct_rows)
        catalog, id_catalog, row_catalog = snapshot()
        self.active_region_catalog_ids = id_catalog
        self.active_region_catalog_rows = row_catalog
        return catalog

    def relationship_target(self, ocid: str) -> RelationshipTarget | None:
        cached = getattr(self, "relationship_target_cache", {}).get(ocid)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        target: RelationshipTarget | None = None
        try:
            escaped_ocid = ocid.replace("\\", "\\\\").replace("'", "\\'")
            details = oci.resource_search.models.StructuredSearchDetails(
                type="Structured",
                query=f"query all resources where identifier = '{escaped_ocid}'",
            )
            response = self._retry_oci_call(
                self.resource_search.search_resources,
                details,
                limit=1,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
            item = next(iter(response.items or []), None)
            if item is None:
                raise ValueError("resource not found in OCI Search")
            payload = oci.util.to_dict(item)
            if not isinstance(payload, dict):
                raise ValueError("invalid Resource Search summary")
            spec = self._resource_spec_for_search_type(payload.get("resource_type"))
            compartment_id = payload.get("compartment_id")
            if not isinstance(compartment_id, str):
                target = None
            else:
                target = RelationshipTarget(
                    spec=spec,
                    row=self.hydrate_resource_row(
                        ResourceRow(
                            name=self._humanize_name(
                                payload.get("display_name")
                                or payload.get("name")
                                or ocid
                            ),
                            state=str(payload.get("lifecycle_state") or "-"),
                            id=ocid,
                            payload=payload,
                        )
                    ),
                    compartment_id=compartment_id,
                )
        except Exception as exc:
            self.record_partial_failure("relationship_target", exc)
            target = None
        if target is None:
            target = self._direct_relationship_target(ocid)
        self.relationship_target_cache[ocid] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            target,
        )
        return target

    def _direct_relationship_target(self, ocid: str) -> RelationshipTarget | None:
        """Resolve OCI Search omissions through a declared direct getter."""
        match = re.match(r"^ocid1\.([^.]+)\.", ocid)
        if match is None:
            return None
        getter = DIRECT_GETTERS.get(match.group(1))
        resource_types = {
            "image": "core.images",
            "subnet": "core.subnets",
            "volume": "core.volumes",
            "bootvolume": "core.boot-volumes",
            "networkfirewall": "network_firewall.network-firewalls",
            "networkfirewallpolicy": "network_firewall.network-firewall-policies",
        }
        resource_type = resource_types.get(match.group(1))
        if getter is None or resource_type is None:
            return None
        try:
            client_attr, method_name = getter
            item = self._retry_oci_call(
                getattr(getattr(self, client_attr), method_name), ocid
            ).data
            payload = oci.util.to_dict(item)
            if not isinstance(payload, dict):
                return None
            compartment_id = payload.get("compartment_id") or self.current.id
            if not isinstance(compartment_id, str):
                return None
            return RelationshipTarget(
                spec=self._resolve_resource_spec(resource_type),
                row=self._row_from_oci_item(item),
                compartment_id=compartment_id,
            )
        except Exception as exc:
            self.record_partial_failure("direct_relationship_target", exc)
            return None

    def compartment_chain(self, compartment_id: str) -> list[CompartmentNode]:
        if compartment_id == self.root.id:
            return [self.root]
        chain: list[CompartmentNode] = []
        current_id = compartment_id
        while current_id != self.root.id:
            item = self._retry_oci_call(self.identity.get_compartment, current_id).data
            chain.append(
                CompartmentNode(
                    id=item.id,
                    name=item.name,
                    description=getattr(item, "description", None),
                    lifecycle_state=str(getattr(item, "lifecycle_state", "ACTIVE")),
                    parent_id=item.compartment_id,
                )
            )
            current_id = item.compartment_id
        return [self.root, *reversed(chain)]

    def change_to_compartment(self, compartment_id: str) -> None:
        chain = self.compartment_chain(compartment_id)
        self.current = chain[-1]
        self.parents = chain[:-1]

    def _remember_row_names(self, rows: Iterable[ResourceRow]) -> None:
        for row in rows:
            payload = row.payload or {}
            row_id = payload.get("id")
            display_name = payload.get("display_name") or payload.get("name")
            if (
                isinstance(row_id, str)
                and isinstance(display_name, str)
                and row_id != display_name
            ):
                self.name_cache[row_id] = self._humanize_name(display_name)

    def _current_compartment_resource_names(self) -> dict[str, str]:
        key = (self.region(), self.current.id)
        cached = getattr(self, "compartment_resource_name_cache", {}).get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        query = "query all resources where compartmentId = '{}'".format(
            self.current.id.replace("\\", "\\\\").replace("'", "\\'")
        )
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        names: dict[str, str] = {}
        page: str | None = None
        while True:
            response = self._retry_oci_call(
                self.resource_search.search_resources,
                details,
                limit=1000,
                page=page,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
            items = list(response.items or [])
            for item in items:
                payload = oci.util.to_dict(item)
                if not isinstance(payload, dict):
                    continue
                ocid = payload.get("identifier") or payload.get("id")
                name = payload.get("display_name") or payload.get("name")
                if isinstance(ocid, str) and isinstance(name, str) and name:
                    names[ocid] = self._humanize_name(name)
            page = getattr(response, "opc_next_page", None)
            if not page or not items:
                break
            time.sleep(0.25)
        self.name_cache.update(names)
        self.compartment_resource_name_cache[key] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            names,
        )
        return names

    def list_current_compartment_resource_types(
        self,
    ) -> list[tuple[ResourceSpec, int]]:
        """Return supported resource types present in the current compartment."""
        key = (self.region(), self.current.id)
        cached = getattr(self, "compartment_resource_type_cache", {}).get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        query = "query all resources where compartmentId = '{}'".format(
            self.current.id.replace("\\", "\\\\").replace("'", "\\'")
        )
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        counts: dict[str, int] = {}
        specs: dict[str, ResourceSpec] = {}
        page: str | None = None
        while True:
            response = self._retry_oci_call(
                self.resource_search.search_resources,
                details,
                limit=self.CATALOG_PAGE_SIZE,
                page=page,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
            items = list(response.items or [])
            for item in items:
                payload = oci.util.to_dict(item)
                if not isinstance(payload, dict):
                    continue
                spec = self._resource_spec_for_search_type(payload.get("resource_type"))
                if spec is None or not self._is_public_resource_spec(spec):
                    continue
                specs[spec.qualified_name] = spec
                counts[spec.qualified_name] = counts.get(spec.qualified_name, 0) + 1
            page = getattr(response, "opc_next_page", None)
            if not page or not items:
                break
            time.sleep(self.CATALOG_PAGE_DELAY_SECONDS)
        for spec, rows in self._direct_catalog_resources(self.current.id):
            specs[spec.qualified_name] = spec
            counts[spec.qualified_name] = len(rows)
            self._remember_row_names(rows)
        result = tuple(
            sorted(
                ((specs[name], count) for name, count in counts.items()),
                key=lambda item: item[0].qualified_name,
            )
        )
        if not hasattr(self, "compartment_resource_type_cache"):
            self.compartment_resource_type_cache = {}
        self.compartment_resource_type_cache[key] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            result,
        )
        return list(result)

    def _enrich_relationship_names(self, rows: list[ResourceRow]) -> list[ResourceRow]:
        relationship_ids = {
            str(value)
            for row in rows
            for key, value in (row.details or {}).items()
            if key.endswith("_id")
            and isinstance(value, str)
            and value.startswith("ocid1.")
        }
        if not relationship_ids:
            return rows
        try:
            names = self._current_compartment_resource_names()
        except Exception as exc:
            self.record_partial_failure("current_compartment_resource_names", exc)
            names = {}
        enriched: list[ResourceRow] = []
        for row in rows:
            details = dict(row.details or {})
            for key, value in details.items():
                if (
                    key.endswith("_id")
                    and isinstance(value, str)
                    and value.startswith("ocid1.")
                ):
                    details[key] = names.get(value, "-")
            enriched.append(row.with_details(details))
        return enriched

    def _apply_resource_enrichments(
        self, spec: ResourceSpec, rows: list[ResourceRow]
    ) -> list[ResourceRow]:
        """Apply declarative per-resource presentation enrichments."""
        for rule in RESOURCE_ENRICHMENTS.get(spec.qualified_name, ()):
            client = getattr(self, str(rule["client_attr"]))
            getter = getattr(client, str(rule["getter"]))
            detail = str(rule["detail"])
            response_attr = str(rule["response_attr"])
            fallback = rule.get("fallback", "-")
            enriched: list[ResourceRow] = []
            for index, row in enumerate(rows):
                try:
                    response = self._retry_oci_call(
                        getter,
                        row.id,
                        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
                    ).data
                    value = str(getattr(response, response_attr, fallback) or fallback)
                except Exception as exc:
                    self.record_partial_failure("resource_enrichment", exc)
                    value = str(fallback)
                enriched.append(row.with_detail(detail, value))
                if index + 1 < len(rows):
                    time.sleep(float(rule.get("delay_seconds", 0)))
            rows = enriched
        return rows

    @staticmethod
    def _humanize_name(value: object) -> str:
        return urllib.parse.unquote(str(value))

    @staticmethod
    def _row_display_name(row: ResourceRow) -> str:
        payload = row.payload or {}
        return OciCompartmentBrowser._humanize_name(
            payload.get("display_name") or payload.get("name") or row.name
        )

    @classmethod
    def _matching_rows(
        cls, rows: Iterable[ResourceRow], target: str
    ) -> list[ResourceRow]:
        matches: list[ResourceRow] = []
        for row in rows:
            raw_display_name = str(
                (row.payload or {}).get("display_name")
                or (row.payload or {}).get("name")
                or row.name
            )
            if target in {
                row.name,
                row.id,
                raw_display_name,
                cls._row_display_name(row),
            }:
                matches.append(row)
        return matches

    @classmethod
    def _ambiguous_row_error(
        cls, resource_type: str, target: str, matches: list[ResourceRow]
    ) -> str:
        candidates = ", ".join(row.name for row in matches[:5])
        more = "" if len(matches) <= 5 else ", ..."
        return f"ambiguous {resource_type}: {target} (use one of: {candidates}{more})"

    @staticmethod
    def _row_name_suffix(row: ResourceRow) -> str:
        candidate = row.id if row.id not in ("", "-", row.name) else ""
        if candidate.startswith("ocid1."):
            tail = candidate.rsplit(".", 1)[-1]
            return tail[-8:]
        normalized = re.sub(r"[^a-zA-Z0-9]+", "-", candidate).strip("-")
        if normalized:
            return normalized[-8:]
        return "dup"

    @staticmethod
    def _sanitize_row_name(name: str) -> str:
        sanitized = urllib.parse.quote(name, safe="A-Za-z0-9._:@-")
        return sanitized or "unnamed"

    @classmethod
    def _uniquify_row_names(cls, rows: list[ResourceRow]) -> list[ResourceRow]:
        base_names = [cls._sanitize_row_name(row.name) for row in rows]
        counts: dict[str, int] = {}
        for base_name in base_names:
            counts[base_name] = counts.get(base_name, 0) + 1
        if all(count == 1 for count in counts.values()) and all(
            row.name == base_name
            for row, base_name in zip(rows, base_names, strict=False)
        ):
            return rows
        used: set[str] = set()
        result: list[ResourceRow] = []
        for row, base_name in zip(rows, base_names, strict=False):
            candidate = base_name
            if counts[base_name] > 1:
                suffix = cls._row_name_suffix(row)
                candidate = f"{base_name}@{suffix}"
            counter = 2
            while candidate in used:
                candidate = f"{base_name}@{cls._row_name_suffix(row)}-{counter}"
                counter += 1
            result.append(
                ResourceRow(
                    name=candidate,
                    state=row.state,
                    id=row.id,
                    extra=row.extra,
                    details=row.details,
                    payload=row.payload,
                )
            )
            used.add(candidate)
        return result

    def _resolve_name_uncached(self, ocid: str) -> str:
        try:
            details = oci.resource_search.models.GetResourceSummaryUsingOCIDDetails(
                ocid=ocid
            )
            data = self.resource_search.get_resource_summary_using_ocid(details).data
            name = getattr(data, "display_name", None) or getattr(
                data, "identifier", None
            )
            if name:
                return str(name)
        except Exception as exc:
            self.record_partial_failure("resolve_resource_name", exc)
        direct = self._resolve_name_via_direct_get(ocid)
        if direct is not None:
            return direct
        return ocid

    def _resolve_name_via_direct_get(self, ocid: str) -> str | None:
        match = re.match(r"^ocid1\.([^.]+)\.", ocid)
        if not match:
            return None
        kind = match.group(1)
        getter_ref = DIRECT_GETTERS.get(kind)
        if getter_ref is None:
            return None
        try:
            client_attr, method_name = getter_ref
            client = getattr(self, client_attr)
            data = getattr(client, method_name)(ocid).data
        except Exception as exc:
            self.record_partial_failure("resolve_resource_name_direct", exc)
            return None
        name = getattr(data, "display_name", None) or getattr(data, "name", None)
        return str(name) if name else None

    def hydrate_resource_row(self, row: ResourceRow) -> ResourceRow:
        """Turn a search-summary projection row into the resource's full row on entry."""
        match = re.match(r"^ocid1\.([^.]+)\.", row.id)
        if match is None:
            return row
        getter_ref = DIRECT_GETTERS.get(match.group(1))
        if getter_ref is None:
            return row
        try:
            client_attr, method_name = getter_ref
            item = getattr(getattr(self, client_attr), method_name)(row.id).data
        except Exception as exc:
            self.record_partial_failure("hydrate_resource_row", exc)
            return row
        return self._row_from_oci_item(item)

    def _resolve_payload_ids(self, value: object, key: str | None = None) -> object:
        if isinstance(value, dict):
            return {
                item_key: self._resolve_payload_ids(item, item_key)
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_payload_ids(item, key) for item in value]
        if (
            isinstance(value, str)
            and value.startswith("ocid1.")
            and key not in {"id", "identifier"}
        ):
            return self.resolve_name(value)
        return value

    def namespaces(self) -> list[str]:
        return sorted(
            {self.namespace_path_slug(spec.namespace) for spec in self.resource_specs}
        )

    @staticmethod
    def namespace_path_slug(namespace: str) -> str:
        return NAMESPACE_PATH_SLUGS.get(namespace, namespace.replace("_", "-"))

    @staticmethod
    def resolve_namespace_path(value: str) -> str | None:
        normalized = value.lower()
        for namespace, slug in NAMESPACE_PATH_SLUGS.items():
            if normalized in {namespace, slug}:
                return namespace
        return None

    def resource_specs_for_namespace(
        self, namespace: str | None = None
    ) -> list[ResourceSpec]:
        specs = self.resource_specs
        if namespace is not None:
            namespace = self.resolve_namespace_path(namespace) or namespace
            specs = [spec for spec in specs if spec.namespace == namespace]
        specs = [spec for spec in specs if self._is_public_resource_spec(spec)]
        return sorted(specs, key=lambda spec: (spec.namespace, spec.name))

    def list_resources(
        self,
        resource_type: str,
        extra_kwargs: dict[str, str] | None = None,
        row_filter: tuple[str, str] | None = None,
    ) -> list[ResourceRow]:
        spec = self._resolve_resource_spec(resource_type)
        if spec.lister_name:
            lister = getattr(self, spec.lister_name)
            parameters = inspect.signature(lister).parameters
            rows = (
                lister(extra_kwargs or {}, spec)
                if len(parameters) >= 2
                else lister(extra_kwargs or {})
            )
        else:
            if not spec.runnable and not extra_kwargs:
                raise ValueError(
                    f"{spec.qualified_name} requires extra identifiers and is not listable from the current compartment alone"
                )
            rows = self._list_generic_resource(spec, extra_kwargs or {})
        rows = self._apply_resource_enrichments(spec, rows)
        self._remember_row_names(rows)
        if row_filter is not None:
            key, expected = row_filter
            if key == "id":
                rows = [row for row in rows if row.id == expected]
            else:
                rows = [row for row in rows if (row.details or {}).get(key) == expected]
        rows = self._enrich_relationship_names(rows)
        return self._uniquify_row_names(rows)

    def list_resource_page(
        self,
        spec: ResourceSpec,
        *,
        page_number: int = 1,
        page_size: int = 100,
        extra_kwargs: dict[str, str] | None = None,
        row_filter: tuple[str, str] | None = None,
    ) -> tuple[list[ResourceRow], dict[str, object]]:
        """Read one explicit, bounded provider page for a normal collection."""
        if spec.lister_name or not spec.client_attr or not spec.list_operation:
            rows = self.list_resources(
                spec.qualified_name, extra_kwargs=extra_kwargs, row_filter=row_filter
            )
            return rows[:page_size], {
                "state": "provider-unpaged",
                "page": 1,
                "page_size": page_size,
                "returned": min(len(rows), page_size),
                "truncated": len(rows) > page_size,
            }
        if not 1 <= page_number <= 20:
            raise ValueError("collection page must be between 1 and 20")
        client = getattr(self, spec.client_attr)
        operation = getattr(client, spec.list_operation)
        kwargs: dict[str, object] = {}
        accepted = set(spec.accepted_params or spec.required_params)
        if "compartment_id" in accepted:
            kwargs["compartment_id"] = self.current.id
        if "scope" in accepted:
            kwargs["scope"] = "REGION"
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        token: str | None = None
        response: object | None = None
        for current_page in range(1, page_number + 1):
            page_kwargs = {**kwargs, "limit": page_size}
            if token:
                page_kwargs["page"] = token
            response = self._retry_oci_call(operation, **page_kwargs)
            if current_page == page_number:
                break
            token = getattr(response, "next_page", None) or getattr(
                response, "opc_next_page", None
            )
            if not token:
                return [], {
                    "state": "live",
                    "page": page_number,
                    "page_size": page_size,
                    "returned": 0,
                    "truncated": False,
                }
        assert response is not None
        rows = [
            self._row_from_oci_item(item)
            for item in self._collection_items(response.data)
        ]
        rows = self._apply_resource_enrichments(spec, rows)
        self._remember_row_names(rows)
        if row_filter is not None:
            key, expected = row_filter
            rows = [
                row
                for row in rows
                if (row.id if key == "id" else (row.details or {}).get(key)) == expected
            ]
        rows = self._uniquify_row_names(self._enrich_relationship_names(rows))
        next_token = getattr(response, "next_page", None) or getattr(
            response, "opc_next_page", None
        )
        return rows, {
            "state": "live",
            "page": page_number,
            "page_size": page_size,
            "returned": len(rows),
            "truncated": bool(next_token),
        }

    def _object_storage_namespace(self) -> str:
        try:
            return str(self._retry_oci_call(self.object_storage.get_namespace).data)
        except Exception as exc:
            raise ValueError(f"object storage namespace unavailable: {exc}") from exc

    def _list_object_storage_buckets(
        self, _kwargs: dict[str, object]
    ) -> list[ResourceRow]:
        namespace = self._object_storage_namespace()
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.object_storage.list_buckets,
                namespace,
                self.current.id,
            )
        except Exception as exc:
            raise ValueError(f"object storage buckets unavailable: {exc}") from exc
        rows: list[ResourceRow] = []
        for item in self._collection_items(response.data):
            payload = oci.util.to_dict(item)
            name = str(payload.get("name") or "bucket")
            rows.append(
                ResourceRow(
                    name=name,
                    state=str(payload.get("public_access_type") or "-"),
                    id=f"bucket:{namespace}:{name}",
                    payload=payload,
                    details={"namespace": namespace, "bucket_name": name},
                )
            )
        return rows

    def list_object_prefix(
        self, bucket: ResourceRow, prefix: str = ""
    ) -> list[ResourceRow]:
        """List one Object Storage pseudo-directory without downloading bodies."""
        namespace = str(
            (bucket.details or {}).get("namespace") or self._object_storage_namespace()
        )
        bucket_name = str((bucket.details or {}).get("bucket_name") or bucket.name)
        try:
            response: object | None = None
            objects: list[object] = []
            prefixes: set[str] = set()
            start: str | None = None
            for _page in range(self.OBJECT_LIST_MAX_PAGES):
                kwargs: dict[str, object] = {
                    "prefix": prefix,
                    "delimiter": "/",
                    "limit": 1000,
                }
                if start:
                    kwargs["start"] = start
                response = self._retry_oci_call(
                    self.object_storage.list_objects, namespace, bucket_name, **kwargs
                )
                data = getattr(response, "data", response)
                objects.extend(getattr(data, "objects", []) or [])
                prefixes.update(
                    str(item) for item in (getattr(data, "prefixes", []) or [])
                )
                start = getattr(data, "next_start_with", None)
                if not start:
                    break
            if start:
                raise ValueError(
                    f"object listing exceeds {self.OBJECT_LIST_MAX_PAGES * 1000} entries; descend into a prefix"
                )
        except Exception as exc:
            raise ValueError(f"object storage objects unavailable: {exc}") from exc
        rows: list[ResourceRow] = []
        for common_prefix in prefixes:
            child_prefix = str(common_prefix)
            label = child_prefix[len(prefix) :].rstrip("/") or child_prefix.rstrip("/")
            rows.append(
                ResourceRow(
                    name=label,
                    state="directory",
                    id=f"prefix:{child_prefix}",
                    details={"object_prefix": child_prefix, "object_directory": "true"},
                    payload={"prefix": child_prefix},
                )
            )
        for item in objects:
            payload = oci.util.to_dict(item)
            object_name = str(payload.get("name") or "object")
            label = object_name.removeprefix(prefix)
            rows.append(
                ResourceRow(
                    name=label,
                    state=str(payload.get("storage_tier") or "-"),
                    id=f"object:{namespace}:{bucket_name}:{object_name}",
                    payload=payload,
                    details={
                        "namespace": namespace,
                        "bucket_name": bucket_name,
                        "object_name": object_name,
                    },
                )
            )
        return sorted(
            rows, key=lambda row: (row.state != "directory", row.name.casefold())
        )

    def read_object_content(self, object_row: ResourceRow) -> str:
        """Read only small, declared-text Object Storage bodies."""
        details = object_row.details or {}
        namespace = str(details["namespace"])
        bucket = str(details["bucket_name"])
        name = str(details["object_name"])
        response = self._retry_oci_call(
            self.object_storage.get_object, namespace, bucket, name
        )
        headers = getattr(response, "headers", {}) or {}
        length = int(
            headers.get("content-length", headers.get("Content-Length", 0)) or 0
        )
        content_type = str(
            headers.get("content-type", headers.get("Content-Type", ""))
        ).lower()
        if length > self.CONTENT_MAX_BYTES:
            raise ValueError(
                f"object content is {length} bytes; maximum readable size is {self.CONTENT_MAX_BYTES}"
            )
        if content_type and not (
            content_type.startswith("text/")
            or "json" in content_type
            or "xml" in content_type
        ):
            raise ValueError(
                f"object content type {content_type} is not text; inspect metadata instead"
            )
        raw = getattr(getattr(response, "data", None), "content", None)
        if raw is None:
            raw = getattr(getattr(response, "data", None), "raw", None)
            raw = raw.read(self.CONTENT_MAX_BYTES + 1) if raw is not None else b""
        if isinstance(raw, str):
            return raw
        if len(raw) > self.CONTENT_MAX_BYTES:
            raise ValueError(
                f"object content exceeds maximum readable size of {self.CONTENT_MAX_BYTES}"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "object content is not UTF-8 text; inspect metadata instead"
            ) from exc

    def resolve_resource_row(
        self,
        resource_type: str,
        name_or_id: str,
        extra_kwargs: dict[str, str] | None = None,
        row_filter: tuple[str, str] | None = None,
    ) -> ResourceRow:
        rows = self.list_resources(
            resource_type, extra_kwargs=extra_kwargs, row_filter=row_filter
        )
        matches = self._matching_rows(rows, name_or_id)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"{resource_type} not found: {name_or_id}")
        raise ValueError(self._ambiguous_row_error(resource_type, name_or_id, matches))

    def resolve_resource_id(self, spec: ResourceSpec, ocid: str) -> RelationshipTarget:
        """Resolve one declared resource by OCID with a bounded OCI Search call."""
        target = self.relationship_target(ocid)
        if target is None:
            raise ValueError(f"{spec.qualified_name} not found: {ocid}")
        if target.spec is None or target.spec.qualified_name != spec.qualified_name:
            actual = (
                target.spec.qualified_name if target.spec is not None else "unknown"
            )
            raise ValueError(f"{ocid} is {actual}, not {spec.qualified_name}")
        return target

    def list_resource_types(self) -> list[str]:
        return [
            self._normalize_resource_token(spec.name)
            for spec in self.resource_specs_for_namespace()
        ]

    def _is_public_resource_spec(self, spec: ResourceSpec) -> bool:
        return spec.runnable and (
            spec.scope == "region" or spec.scope.startswith("compartment")
        )

    def resolve_resource_spec(self, resource_type: str) -> ResourceSpec:
        return self._resolve_resource_spec(resource_type)

    def canonical_spec_for_search_row(
        self, spec: ResourceSpec, row: ResourceRow
    ) -> ResourceSpec:
        """Resolve registry-declared ownership variants in ambiguous Search types."""
        variant = self.CANONICAL_SEARCH_VARIANTS.get(spec.qualified_name)
        if variant is None:
            return spec
        field, owned_type, catalog_type = variant
        hydrated = self.hydrate_resource_row(row)
        value = (hydrated.details or {}).get(field)
        if value in (None, "", "-", "None", self.tenancy_id):
            return self.resolve_resource_spec(catalog_type)
        return self.resolve_resource_spec(owned_type)

    def _build_resource_specs(self) -> list[ResourceSpec]:
        specs: list[ResourceSpec] = []
        for namespace, config in NAMESPACE_DISCOVERY.items():
            specs.extend(self._build_namespace_resource_specs(namespace, config))
        return sorted(specs, key=lambda spec: (spec.namespace, spec.name))

    def _build_namespace_resource_specs(
        self, namespace: str, config: dict[str, object]
    ) -> list[ResourceSpec]:
        client_classes = {
            "oci.core.VirtualNetworkClient": oci.core.VirtualNetworkClient,
            "oci.core.ComputeClient": oci.core.ComputeClient,
            "oci.core.ComputeManagementClient": oci.core.ComputeManagementClient,
            "oci.core.BlockstorageClient": oci.core.BlockstorageClient,
            "oci.container_engine.ContainerEngineClient": oci.container_engine.ContainerEngineClient,
            "oci.generative_ai.GenerativeAiClient": oci.generative_ai.GenerativeAiClient,
            "oci.generative_ai_agent.GenerativeAiAgentClient": oci.generative_ai_agent.GenerativeAiAgentClient,
            "oci.load_balancer.LoadBalancerClient": oci.load_balancer.LoadBalancerClient,
            "oci.network_firewall.NetworkFirewallClient": oci.network_firewall.NetworkFirewallClient,
            "oci.logging.LoggingManagementClient": oci.logging.LoggingManagementClient,
            "oci.audit.AuditClient": oci.audit.AuditClient,
            "oci.monitoring.MonitoringClient": oci.monitoring.MonitoringClient,
            "oci.apm_control_plane.ApmDomainClient": oci.apm_control_plane.ApmDomainClient,
            "oci.log_analytics.LogAnalyticsClient": oci.log_analytics.LogAnalyticsClient,
            "oci.object_storage.ObjectStorageClient": oci.object_storage.ObjectStorageClient,
            "oci.artifacts.ArtifactsClient": oci.artifacts.ArtifactsClient,
            "oci.devops.DevopsClient": oci.devops.DevopsClient,
            "oci.dns.DnsClient": oci.dns.DnsClient,
            "oci.identity.IdentityClient": oci.identity.IdentityClient,
            "oci.limits.LimitsClient": oci.limits.LimitsClient,
            "oci.resource_manager.ResourceManagerClient": oci.resource_manager.ResourceManagerClient,
        }
        endpoint_family = str(config["endpoint_family"])
        client_map = config.get("client_map", {})
        custom_listers = dict(config.get("custom_listers", {}))
        resource_name_overrides = dict(config.get("resource_name_overrides", {}))
        scope_overrides = dict(config.get("scope_overrides", {}))
        runnable_overrides = dict(config.get("runnable_overrides", {}))
        findable_overrides = dict(config.get("findable_overrides", {}))
        search_type_overrides = dict(config.get("search_type_overrides", {}))
        node_capability_overrides = dict(config.get("node_capability_overrides", {}))
        adapter_kind = str(config.get("adapter_kind", "resource-tree"))
        specs: list[ResourceSpec] = []
        seen: set[str] = set()
        for client_attr, class_name in dict(client_map).items():
            client_cls = client_classes[str(class_name)]
            for method_name, method in inspect.getmembers(
                client_cls, inspect.isfunction
            ):
                if not method_name.startswith("list_"):
                    continue
                source_resource_name = method_name.removeprefix("list_")
                resource_name = str(
                    resource_name_overrides.get(
                        source_resource_name, source_resource_name
                    )
                )
                if resource_name in seen:
                    continue
                seen.add(resource_name)
                required_params = self._required_param_names(method)
                accepted_params = self._accepted_param_names(method)
                scope, runnable = self._infer_scope(required_params, accepted_params)
                runnable = bool(runnable_overrides.get(source_resource_name, runnable))
                scope = str(scope_overrides.get(source_resource_name, scope))
                specs.append(
                    ResourceSpec(
                        namespace=namespace,
                        name=resource_name,
                        endpoint_family=endpoint_family,
                        scope=scope,
                        lister_name=custom_listers.get(source_resource_name),
                        client_attr=str(client_attr),
                        client_name=client_cls.__name__,
                        list_operation=method_name,
                        required_params=tuple(required_params),
                        accepted_params=tuple(accepted_params),
                        runnable=runnable,
                        findable=bool(
                            findable_overrides.get(
                                source_resource_name,
                                runnable and scope.startswith("compartment"),
                            )
                        ),
                        search_type=search_type_overrides.get(source_resource_name),
                        node_capability=node_capability_overrides.get(
                            source_resource_name, "navigable-resource"
                        ),
                        adapter_kind=adapter_kind,
                    )
                )
        for extra in config.get("extra_specs", []):
            extra_spec = dict(extra)
            specs.append(
                ResourceSpec(
                    namespace=namespace,
                    name=str(extra_spec["name"]),
                    endpoint_family=str(extra_spec["endpoint_family"]),
                    scope=str(extra_spec["scope"]),
                    lister_name=extra_spec.get("lister_name"),
                    client_attr=extra_spec.get("client_attr"),
                    client_name=extra_spec.get("client_name"),
                    list_operation=extra_spec.get("list_operation"),
                    required_params=tuple(extra_spec.get("required_params", ())),
                    accepted_params=tuple(extra_spec.get("accepted_params", ())),
                    runnable=bool(extra_spec.get("runnable", True)),
                    findable=bool(
                        extra_spec.get("findable", extra_spec.get("runnable", True))
                    ),
                    search_type=extra_spec.get("search_type"),
                    node_capability=str(
                        extra_spec.get("node_capability", "navigable-resource")
                    ),
                    adapter_kind=str(extra_spec.get("adapter_kind", adapter_kind)),
                    adapter_config=tuple(
                        sorted(dict(extra_spec.get("adapter_config", {})).items())
                    ),
                )
            )
        return sorted(specs, key=lambda spec: spec.name)

    @staticmethod
    def _required_param_names(method: object) -> list[str]:
        sig = inspect.signature(method)
        required: list[str] = []
        for param in sig.parameters.values():
            if param.name == "self":
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if param.default is inspect._empty:
                required.append(param.name)
        return required

    @staticmethod
    def _accepted_param_names(method: object) -> list[str]:
        sig = inspect.signature(method)
        explicit = [
            param.name
            for param in sig.parameters.values()
            if param.name != "self"
            and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        ]
        if explicit:
            return explicit
        doc = getattr(method, "__doc__", "") or ""
        seen: list[str] = []
        for name in re.findall(r":param\s+[^:]+\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:", doc):
            if name not in seen:
                seen.append(name)
        return seen

    @staticmethod
    def _infer_scope(
        required_params: list[str], accepted_params: list[str]
    ) -> tuple[str, bool]:
        required = set(required_params)
        if not required_params:
            accepted = set(accepted_params)
            if "compartment_id" in accepted:
                if "scope" in accepted:
                    return "compartment+scope", True
                return "compartment(filtered)", True
            if any(
                OciCompartmentBrowser._is_resource_reference_param(name)
                for name in accepted_params
            ):
                return "parent-resource", False
            return "region", True
        if required == {"compartment_id"}:
            return "compartment", True
        if required == {"scope", "compartment_id"}:
            return "compartment+scope", True
        if "compartment_id" in required:
            return "compartment+extra", False
        if any(
            OciCompartmentBrowser._is_resource_reference_param(name)
            for name in required_params
        ):
            return "parent-resource", False
        return "special", False

    @staticmethod
    def _is_resource_reference_param(name: str) -> bool:
        return name.endswith("_id") and not name.startswith("opc_")

    def _direct_catalog_resources(
        self, compartment_id: str
    ) -> list[tuple[ResourceSpec, list[ResourceRow]]]:
        """Discover every runnable resource for direct-catalog service domains.

        A service declaration chooses the discovery strategy; resource names
        are always derived from the installed OCI SDK list operations.
        """
        resources: list[tuple[ResourceSpec, list[ResourceRow]]] = []
        for namespace, config in NAMESPACE_DISCOVERY.items():
            if config.get("catalog_discovery") != "direct":
                continue
            for spec in self.resource_specs_for_namespace(namespace):
                if not spec.scope.startswith("compartment"):
                    continue
                try:
                    rows = self._list_generic_resource(
                        spec, compartment_id=compartment_id
                    )
                except Exception as exc:
                    self.record_partial_failure("discover_namespace_resources", exc)
                    continue
                if rows:
                    resources.append((spec, rows))
        return resources

    def _list_generic_resource(
        self,
        spec: ResourceSpec,
        extra_kwargs: dict[str, str] | None = None,
        compartment_id: str | None = None,
    ) -> list[ResourceRow]:
        if not spec.client_attr or not spec.list_operation:
            raise ValueError(f"{spec.qualified_name} has no runnable list operation")
        client = getattr(self, spec.client_attr)
        operation = getattr(client, spec.list_operation)
        kwargs = {}
        accepted = set(spec.accepted_params or spec.required_params)
        if "compartment_id" in accepted:
            kwargs["compartment_id"] = compartment_id or self.current.id
        if "scope" in accepted:
            kwargs["scope"] = "REGION"
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        response_data = self._retry_oci_call(
            oci.pagination.list_call_get_all_results, operation, **kwargs
        ).data
        items = self._collection_items(response_data)
        rows = [self._row_from_oci_item(item) for item in items]
        return sorted(rows, key=lambda row: row.name.lower())

    def _list_openai_project_data(
        self, kwargs: dict[str, object], spec: ResourceSpec
    ) -> list[ResourceRow]:
        config = {
            **dict(NAMESPACE_DISCOVERY[spec.namespace].get("openai_data", {})),
            **dict(spec.adapter_config),
        }
        project_key = config["project_param"]
        project_id = kwargs.get(project_key)
        if not isinstance(project_id, str) or not project_id:
            raise ValueError(f"{project_key} is required")
        request = {"limit": 100}
        parent_key = config.get("parent_param")
        if parent_key:
            parent_id = kwargs.get(parent_key)
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError(f"{parent_key} is required")
            request[parent_key] = parent_id
        payloads = self._list_openai_collection(
            self._openai_project_client(project_id, config),
            config["collection"],
            request,
            int(config["max_pages"]),
        )
        return sorted(
            [
                self._row_from_openai_data_item(payload, project_id)
                for payload in payloads
            ],
            key=lambda row: row.name.lower(),
        )

    def _openai_project_client(
        self, project_id: str, config: dict[str, str]
    ) -> object:
        from openai import OpenAI

        base_url = config["base_url"].format(region=self.region())
        api_key = next(
            (os.getenv(name) for name in config["api_key_env"].split(",") if os.getenv(name)),
            None,
        )
        if api_key:
            return OpenAI(base_url=base_url, api_key=api_key, project=project_id)
        try:
            import httpx
            from oci_genai_auth import OciSessionAuth, OciUserPrincipalAuth
        except ImportError as exc:
            raise RuntimeError(config["auth_error"]) from exc
        try:
            auth = OciSessionAuth(profile_name=self.profile_name or "DEFAULT")
        except KeyError:
            auth = OciUserPrincipalAuth(profile_name=self.profile_name or "DEFAULT")
        return OpenAI(
            base_url=base_url,
            api_key="not-used",
            project=project_id,
            http_client=httpx.Client(auth=auth),
        )

    @staticmethod
    def _list_openai_collection(
        client: object,
        collection: str,
        parameters: dict[str, object],
        max_pages: int,
    ) -> list[dict[str, object]]:
        """Read any dotted collection from an OpenAI-compatible client."""
        target: object = client
        for part in collection.split("."):
            target = getattr(target, part)
        try:
            page = target.list(**parameters)
        except Exception as exc:
            if exc.__class__.__name__ == "NotFoundError":
                return []
            raise
        items: list[dict[str, object]] = []
        for _ in range(max_pages):
            items.extend(self._openai_item_dict(item) for item in page.data)
            if not page.has_next_page():
                break
            page = page.get_next_page()
        return items

    @staticmethod
    def _openai_item_dict(item: object) -> dict[str, object]:
        if hasattr(item, "model_dump"):
            return dict(item.model_dump())
        if isinstance(item, dict):
            return dict(item)
        return dict(vars(item))

    def _row_from_openai_data_item(
        self, payload: dict[str, object], project_id: str
    ) -> ResourceRow:
        name = str(
            payload.get("filename")
            or payload.get("name")
            or payload.get("id")
            or "<unnamed>"
        )
        return ResourceRow(
            name=name,
            state=str(payload.get("status") or "-"),
            id=str(payload.get("id") or name),
            extra=str(payload.get("purpose") or ""),
            details={**payload, "generative_ai_project_id": project_id},
            payload={**payload, "generative_ai_project_id": project_id},
        )

    @staticmethod
    def _search_type_for(spec: ResourceSpec) -> str:
        if spec.search_type:
            return spec.search_type
        name = spec.name.replace("-", "_")
        if name.endswith("ies"):
            name = name[:-3] + "y"
        elif name.endswith("s"):
            name = name[:-1]
        return "".join(part.capitalize() for part in name.split("_") if part)

    @staticmethod
    def _search_property_name(field: str) -> str:
        parts = field.split("_")
        return parts[0] + "".join(part.capitalize() for part in parts[1:])

    def list_relationship_projection(
        self, spec: ResourceSpec, relationship_field: str, parent_id: str
    ) -> list[ResourceRow]:
        """Lazily mount a cross-compartment relationship view via OCI Search."""
        key = (self.region(), spec.qualified_name, relationship_field, parent_id)
        cached = self.relationship_projection_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        search_type = self.search_type_for(spec)
        property_name = self._search_property_name(relationship_field)
        literal = parent_id.replace("\\", "\\\\").replace("'", "\\'")
        query = f"query {search_type} resources where {property_name} = '{literal}'"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.resource_search.search_resources,
                details,
            )
            items = self._collection_items(response.data)
        except Exception as exc:
            if getattr(
                exc, "code", None
            ) == "CannotParseRequest" and "Unknown resource type" in str(exc):
                rows = self._list_relationship_projection_fallback(
                    spec, relationship_field, parent_id
                )
                self.relationship_projection_cache[key] = (
                    now + self.PROJECTION_CACHE_TTL_SECONDS,
                    tuple(rows),
                )
                return rows
            raise ValueError(
                f"relationship projection unavailable for {spec.qualified_name}: OCI Search failed ({exc})"
            ) from exc
        rows = self._uniquify_row_names(
            [self._row_from_oci_item(item) for item in items]
        )
        if not rows:
            rows = self._list_relationship_projection_fallback(
                spec, relationship_field, parent_id
            )
        self._remember_row_names(rows)
        self.relationship_projection_cache[key] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            tuple(rows),
        )
        return rows

    def _list_relationship_projection_fallback(
        self, spec: ResourceSpec, relationship_field: str, parent_id: str
    ) -> list[ResourceRow]:
        return [
            row
            for row in self.list_resources(spec.qualified_name)
            if (row.payload or {}).get(
                relationship_field, (row.details or {}).get(relationship_field)
            )
            == parent_id
        ]

    @staticmethod
    def _log_source_resource_id(row: ResourceRow) -> str | None:
        configuration = (row.payload or {}).get("configuration")
        if not isinstance(configuration, dict):
            return None
        source = configuration.get("source")
        if not isinstance(source, dict):
            return None
        resource_id = source.get("resource")
        return resource_id if isinstance(resource_id, str) else None

    def list_tenancy_log_groups(self) -> list[ResourceRow]:
        """Return the tenancy-wide log-group index used by ~/logging."""
        region = self.region()
        cache = getattr(self, "tenancy_log_group_cache", {})
        cached = cache.get(region)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        spec = self.resolve_resource_spec("logging.log-groups")
        query = f"query {self.search_type_for(spec)} resources"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.resource_search.search_resources,
                details,
            )
            rows: list[ResourceRow] = []
            for item in self._collection_items(response.data):
                row = self._row_from_oci_item(item)
                payload = dict(row.payload or {})
                payload["id"] = row.id
                compartment_id = getattr(item, "compartment_id", None) or payload.get(
                    "compartment_id"
                )
                details_map = dict(row.details or {})
                if isinstance(compartment_id, str):
                    details_map["compartment_id"] = compartment_id
                rows.append(
                    ResourceRow(
                        name=row.name,
                        state=row.state,
                        id=row.id,
                        extra=row.extra,
                        details=details_map,
                        payload=payload,
                    )
                )
        except Exception as exc:
            raise ValueError(
                f"tenancy logging index unavailable: OCI Search failed ({exc})"
            ) from exc
        rows = self._uniquify_row_names(rows)
        self._remember_row_names(rows)
        cache[region] = (now + self.PROJECTION_CACHE_TTL_SECONDS, tuple(rows))
        self.tenancy_log_group_cache = cache
        return rows

    @staticmethod
    def _log_entry_time(value: str | None, option: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{option} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{option} must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _log_query_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _log_entry_name(value: object, index: int) -> str:
        if isinstance(value, (int, float)):
            timestamp = datetime.fromtimestamp(value / 1000, UTC)
            label = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        elif isinstance(value, str) and value:
            label = value.replace("+00:00", "Z")
        else:
            label = "entry"
        return f"{label}-{index + 1}"

    def list_time_query_entries(
        self,
        provider: str,
        parent: ResourceRow | None = None,
        **options: object,
    ) -> list[ResourceRow]:
        """Run one declared time/query provider through the common filesystem view."""
        handlers: dict[str, Callable[..., list[ResourceRow]]] = {
            "logging": self.list_log_entries,
            "audit": self._list_audit_events,
            "monitoring": self._list_monitoring_metrics,
            "apm": self._list_apm_query,
            "log-analytics": self._list_log_analytics_query,
        }
        try:
            handler = handlers[provider]
        except KeyError as exc:
            raise ValueError(f"unknown time-query provider: {provider}") from exc
        return handler(parent, **options)

    @staticmethod
    def _time_query_rows(value: object, *, label: str = "entry") -> list[ResourceRow]:
        """Turn an OCI query response into terminal, raw-payload records."""
        data = getattr(value, "data", value)
        if isinstance(data, (list, tuple)):
            items = list(data)
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            items = list(data["items"])
        elif isinstance(data, dict) and isinstance(data.get("query_result_rows"), list):
            items = list(data["query_result_rows"])
        else:
            items = [data]
        rows: list[ResourceRow] = []
        for index, item in enumerate(items):
            payload = item if isinstance(item, dict) else oci.util.to_dict(item)
            timestamp = (
                payload.get("datetime")
                or payload.get("time")
                or payload.get("event_time")
                or payload.get("time_created")
                or label
            )
            rows.append(
                ResourceRow(
                    name=OciCompartmentBrowser._log_entry_name(timestamp, index),
                    state=str(
                        payload.get("lifecycle_state") or payload.get("status") or "-"
                    ),
                    id=f"{label}-{index + 1}",
                    payload=payload,
                )
            )
        return rows

    def _time_window(
        self,
        options: dict[str, object],
        *,
        default_seconds: int,
        maximum_seconds: int | None = None,
    ) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        since = options.get("since")
        until = options.get("until")
        start = (
            self._log_entry_time(str(since), "since")
            if since
            else now - timedelta(seconds=default_seconds)
        )
        end = self._log_entry_time(str(until), "until") if until else now
        if start >= end:
            raise ValueError("since must be earlier than until")
        if maximum_seconds and end - start > timedelta(seconds=maximum_seconds):
            raise ValueError(
                f"time range cannot exceed {maximum_seconds // 86400} days"
            )
        return start, end

    def _time_query_page(self, provider: str, options: dict[str, object]) -> int:
        page = int(options.get("page_number", 1))
        maximum = int(TIME_QUERY_PROVIDERS[provider]["max_pages"])
        if not 1 <= page <= maximum:
            raise ValueError(f"page must be between 1 and {maximum}")
        return page

    def _list_audit_events(
        self, _parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        start, end = self._time_window(options, default_seconds=24 * 60 * 60)
        # Audit accepts minute granularity only.  Preserve an inclusive useful
        # default window without exposing invalid seconds to its API.
        start = start.replace(second=0, microsecond=0)
        end = (end + timedelta(minutes=1)).replace(second=0, microsecond=0)
        page_number = self._time_query_page("audit", options)
        try:
            token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                response = self._retry_oci_call(
                    self.audit.list_events,
                    self.current.id,
                    start,
                    end,
                    **({"page": token} if token else {}),
                )
                if current_page == page_number:
                    break
                token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not token:
                    return []
        except Exception as exc:
            raise ValueError(
                f"audit events unavailable: OCI Audit failed ({exc})"
            ) from exc
        return self._time_query_rows(response, label="event")

    def _list_monitoring_metrics(
        self, _parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        query, namespace = options.get("query"), options.get("namespace")
        if (
            not isinstance(query, str)
            or not query
            or not isinstance(namespace, str)
            or not namespace
        ):
            raise ValueError("monitoring requires query and namespace control values")
        start, end = self._time_window(options, default_seconds=60 * 60)
        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace=namespace,
            query=query,
            start_time=start,
            end_time=end,
            resolution=options.get("resolution"),
        )
        try:
            response = self._retry_oci_call(
                self.monitoring.summarize_metrics_data, self.current.id, details
            )
        except Exception as exc:
            raise ValueError(
                f"monitoring metrics unavailable: OCI Monitoring failed ({exc})"
            ) from exc
        return self._time_query_rows(response, label="metric")

    def _list_apm_query(
        self, parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        query = options.get("query")
        if parent is None or not isinstance(query, str) or not query:
            raise ValueError("APM requires an APM domain and a query control value")
        start, end = self._time_window(options, default_seconds=60 * 60)
        details = oci.apm_traces.models.QueryDetails(query_text=query)
        page_number = self._time_query_page("apm", options)
        try:
            token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                kwargs: dict[str, object] = {"limit": 100}
                if token:
                    kwargs["page"] = token
                response = self._retry_oci_call(
                    self.apm_query.query, parent.id, start, end, details, **kwargs
                )
                if current_page == page_number:
                    break
                token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not token:
                    return []
        except Exception as exc:
            raise ValueError(f"APM query unavailable: OCI APM failed ({exc})") from exc
        return self._time_query_rows(response, label="apm-result")

    def _list_log_analytics_query(
        self, _parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        query = options.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("Log Analytics requires a query control value")
        start, end = self._time_window(options, default_seconds=60 * 60)
        namespace = self._retry_oci_call(self.object_storage.get_namespace).data
        details = oci.log_analytics.models.QueryDetails(
            compartment_id=self.current.id,
            query_string=query,
            should_run_async=False,
            should_include_total_count=True,
            time_filter=oci.log_analytics.models.TimeRange(
                time_start=start, time_end=end
            ),
        )
        page_number = self._time_query_page("log-analytics", options)
        try:
            token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                kwargs: dict[str, object] = {"limit": 100}
                if token:
                    kwargs["page"] = token
                response = self._retry_oci_call(
                    self.log_analytics.query, namespace, details, **kwargs
                )
                if current_page == page_number:
                    break
                token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not token:
                    return []
        except Exception as exc:
            raise ValueError(
                f"Log Analytics query unavailable: OCI Log Analytics failed ({exc})"
            ) from exc
        return self._time_query_rows(response, label="log-analytics-result")

    def list_log_entries(
        self,
        log: ResourceRow,
        *,
        page_number: int = 1,
        since: str | None = None,
        until: str | None = None,
        contains: str | None = None,
        where: str | None = None,
    ) -> list[ResourceRow]:
        """Read one bounded, filtered page from a configured log's entries."""
        payload = log.payload or {}
        log_group_id = payload.get("log_group_id")
        if not isinstance(log_group_id, str):
            raise ValueError("log entries unavailable: the log group is unknown")
        if not 1 <= page_number <= self.LOG_ENTRY_MAX_PAGE:
            raise ValueError(f"--page must be between 1 and {self.LOG_ENTRY_MAX_PAGE}")
        now = datetime.now(UTC)
        time_start = self._log_entry_time(since, "--since") or (
            now - timedelta(seconds=self.LOG_ENTRY_WINDOW_SECONDS)
        )
        time_end = self._log_entry_time(until, "--until") or now
        if time_start >= time_end:
            raise ValueError("--since must be earlier than --until")
        if time_end - time_start > timedelta(seconds=self.LOG_ENTRY_WINDOW_SECONDS):
            raise ValueError("log entry time range cannot exceed 14 days")
        query = f'search "{self.current.id}/{log_group_id}/{log.id}" '
        if contains:
            query += f"| where logContent = '*{self._log_query_literal(contains)}*' "
        if where:
            field, separator, value = where.partition("=")
            if not separator or not field.strip() or not value:
                raise ValueError("--where must use FIELD=VALUE")
            literal = (
                value
                if re.fullmatch(r"-?\d+(?:\.\d+)?", value)
                or value.lower() in {"true", "false"}
                else f"'{self._log_query_literal(value)}'"
            )
            query += f"| where {field.strip()} = {literal} "
        query += "| sort by datetime desc"
        details = oci.loggingsearch.models.SearchLogsDetails(
            time_start=time_start,
            time_end=time_end,
            search_query=query,
            is_return_field_info=False,
        )
        try:
            page_token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                kwargs: dict[str, object] = {"limit": self.LOG_ENTRY_PAGE_SIZE}
                if page_token:
                    kwargs["page"] = page_token
                response = self._retry_oci_call(
                    self.logging_search.search_logs, details, **kwargs
                )
                if current_page == page_number:
                    break
                page_token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not page_token:
                    return []
        except Exception as exc:
            raise ValueError(
                f"log entries unavailable: OCI Logging Search failed ({exc})"
            ) from exc
        rows: list[ResourceRow] = []
        for index, result in enumerate(
            getattr(getattr(response, "data", None), "results", []) or []
        ):
            entry = getattr(result, "data", result)
            entry_payload = (
                entry if isinstance(entry, dict) else oci.util.to_dict(entry)
            )
            timestamp = (
                entry_payload.get("datetime") or entry_payload.get("time") or "entry"
            )
            level = entry_payload.get("level") or entry_payload.get("severity") or "-"
            content = (
                entry_payload.get("logContent") or entry_payload.get("message") or ""
            )
            rows.append(
                ResourceRow(
                    name=self._log_entry_name(timestamp, index),
                    state=str(level),
                    id=f"entry-{index + 1}",
                    extra=str(content),
                    payload=entry_payload,
                )
            )
        return rows

    def _all_log_rows(self) -> list[ResourceRow]:
        """Build the lazy, region-local index behind every resource's logs/ view."""
        region = self.region()
        cached = self.log_relationship_cache.get(region)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        group_spec = self.resolve_resource_spec("logging.log-groups")
        query = f"query {self._search_type_for(group_spec)} resources"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.resource_search.search_resources,
                details,
            )
            groups = self._collection_items(response.data)
            rows: list[ResourceRow] = []
            for group in groups:
                group_id = getattr(group, "identifier", None) or getattr(
                    group, "id", None
                )
                if not isinstance(group_id, str):
                    continue
                log_response = self._retry_oci_call(
                    oci.pagination.list_call_get_all_results,
                    self.logging.list_logs,
                    log_group_id=group_id,
                )
                for item in self._collection_items(log_response.data):
                    row = self._row_from_oci_item(item)
                    payload = dict(row.payload or {})
                    payload["log_group_id"] = group_id
                    details_map = dict(row.details or {})
                    details_map["log_group_id"] = group_id
                    details_map["canonical_path"] = (
                        f"logging/log-groups/{group_id}/logs/{row.name}"
                    )
                    rows.append(
                        ResourceRow(
                            name=row.name,
                            state=row.state,
                            id=row.id,
                            extra=row.extra,
                            details=details_map,
                            payload=payload,
                        )
                    )
        except Exception as exc:
            raise ValueError(
                f"logs projection unavailable: OCI Logging discovery failed ({exc})"
            ) from exc
        rows = self._uniquify_row_names(rows)
        self._remember_row_names(rows)
        self.log_relationship_cache[region] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            tuple(rows),
        )
        return rows

    @staticmethod
    def _payload_resource_ids(value: object) -> set[str]:
        if not isinstance(value, dict):
            return set()
        return {
            item
            for key, item in value.items()
            if key.endswith("_id")
            and isinstance(item, str)
            and item.startswith("ocid1.")
        }

    def _log_source_related_ids(self, source_id: str) -> frozenset[str]:
        key = (self.region(), source_id)
        cached = self.log_source_relation_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        source_row = self.hydrate_resource_row(ResourceRow(source_id, "-", source_id))
        related = {source_id, *self._payload_resource_ids(source_row.payload)}
        result = frozenset(related)
        self.log_source_relation_cache[key] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            result,
        )
        return result

    def list_related_logs(self, resource_id: str) -> list[ResourceRow]:
        return [
            row
            for row in self._all_log_rows()
            if (source_id := self._log_source_resource_id(row)) is not None
            and resource_id in self._log_source_related_ids(source_id)
        ]

    @staticmethod
    def _is_throttling_error(exc: Exception) -> bool:
        return isinstance(exc, oci.exceptions.ServiceError) and exc.status == 429

    def _retry_oci_call(
        self, func: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        delay = self.THROTTLE_BASE_DELAY
        for attempt in range(self.THROTTLE_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                if (
                    not self._is_throttling_error(exc)
                    or attempt >= self.THROTTLE_RETRIES
                ):
                    self._record_oci_failure(func, exc)
                    raise
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    def _record_oci_failure(self, func: Callable[..., object], exc: Exception) -> None:
        self._record_failure(
            getattr(func, "__name__", type(func).__name__), exc, state="error"
        )

    def record_partial_failure(self, operation: str, exc: Exception) -> None:
        """Record a skipped optional OCI read for the current stat envelope."""
        self._record_failure(operation, exc, state="partial")

    def _record_failure(self, operation: str, exc: Exception, *, state: str) -> None:
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        message = str(getattr(exc, "message", None) or exc)
        normalized = message.casefold()
        kind = "error"
        if status in {401, 403}:
            kind = "permission-denied"
        elif "not subscribed" in normalized:
            kind = "region-not-subscribed"
        elif status == 404 or "not available" in normalized:
            kind = "service-unavailable"
        elif status == 429:
            kind = "throttled"
        self.last_oci_failure = {
            "state": state,
            "kind": kind,
            "operation": operation,
            "status": status,
            "code": code,
            "message": message,
            "request_id": getattr(exc, "request_id", None),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _collection_items(value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        items = getattr(value, "items", None)
        if isinstance(items, list):
            return items
        return [value]

    def _row_from_oci_item(self, item: object) -> ResourceRow:
        name = (
            getattr(item, "display_name", None)
            or getattr(item, "name", None)
            or getattr(item, "output_name", None)
            or getattr(item, "resource_name", None)
            or getattr(item, "region_name", None)
            or getattr(item, "provider_service_name", None)
            or getattr(item, "provider_name", None)
            or getattr(item, "cidr_block", None)
            or getattr(item, "ip_address", None)
            or getattr(item, "id", None)
            or "<unnamed>"
        )
        state = (
            getattr(item, "lifecycle_state", None)
            or getattr(item, "status", None)
            or getattr(item, "state", None)
            or getattr(item, "resource_drift_status", None)
            or getattr(item, "ip_state", None)
            or "-"
        )
        rid = getattr(item, "id", None) or getattr(item, "identifier", None) or name
        extra = (
            getattr(item, "description", None)
            or getattr(item, "resource_type", None)
            or getattr(item, "provider_name", None)
            or getattr(item, "cidr_block", None)
            or getattr(item, "shape", None)
            or ""
        )
        details = self._extract_common_details(item)
        return ResourceRow(
            name=self._humanize_name(name),
            state=str(state),
            id=str(rid),
            extra=str(extra or ""),
            details=details,
            payload=oci.util.to_dict(item),
        )

    def _extract_common_details(self, item: object) -> dict[str, str]:
        details: dict[str, str] = {}
        field_extractors = {
            "created": lambda obj: self._format_time(
                getattr(obj, "time_created", None)
                or getattr(getattr(obj, "metadata", None), "time_created", None)
            ),
            "cidr": lambda obj: getattr(obj, "cidr_block", None) or None,
            "cidr_block": lambda obj: getattr(obj, "cidr_block", None) or None,
            "dns_label": lambda obj: getattr(obj, "dns_label", None) or None,
            "availability_domain": lambda obj: (
                getattr(obj, "availability_domain", None) or None
            ),
            "fault_domain": lambda obj: getattr(obj, "fault_domain", None) or None,
            "shape": lambda obj: getattr(obj, "shape", None) or None,
            "size_gb": lambda obj: self._first_present(
                self._format_number(getattr(obj, "size_in_gbs", None)),
                self._format_size_gb_from_mbs(getattr(obj, "size_in_mbs", None)),
            ),
            "compartment_id": lambda obj: getattr(obj, "compartment_id", None) or None,
            "vcn_id": lambda obj: getattr(obj, "vcn_id", None) or None,
            "subnet_id": lambda obj: getattr(obj, "subnet_id", None) or None,
            "vnic_id": lambda obj: getattr(obj, "vnic_id", None) or None,
            "drg_id": lambda obj: getattr(obj, "drg_id", None) or None,
            "drg_route_table_id": lambda obj: (
                getattr(obj, "drg_route_table_id", None) or None
            ),
            "drg_route_distribution_id": lambda obj: (
                getattr(obj, "drg_route_distribution_id", None) or None
            ),
            "cpe_id": lambda obj: getattr(obj, "cpe_id", None) or None,
            "instance_id": lambda obj: getattr(obj, "instance_id", None) or None,
            "instance_pool_id": lambda obj: (
                getattr(obj, "instance_pool_id", None) or None
            ),
            "dedicated_vm_host_id": lambda obj: (
                getattr(obj, "dedicated_vm_host_id", None) or None
            ),
            "compute_capacity_topology_id": lambda obj: (
                getattr(obj, "compute_capacity_topology_id", None) or None
            ),
            "compute_gpu_memory_cluster_id": lambda obj: (
                getattr(obj, "compute_gpu_memory_cluster_id", None) or None
            ),
            "virtual_circuit_id": lambda obj: (
                getattr(obj, "virtual_circuit_id", None) or None
            ),
            "provider_service_id": lambda obj: (
                getattr(obj, "provider_service_id", None) or None
            ),
            "network_security_group_id": lambda obj: (
                getattr(obj, "network_security_group_id", None) or None
            ),
            "image_id": lambda obj: getattr(obj, "image_id", None) or None,
            "ipsc_id": lambda obj: getattr(obj, "ipsc_id", None) or None,
            "stack_id": lambda obj: getattr(obj, "stack_id", None) or None,
            "network_entity_id": lambda obj: (
                getattr(obj, "network_entity_id", None) or None
            ),
            "resource_id": lambda obj: getattr(obj, "resource_id", None) or None,
            "public_ip": lambda obj: getattr(obj, "public_ip", None) or None,
            "private_ip": lambda obj: getattr(obj, "private_ip", None) or None,
            "ip_address": lambda obj: getattr(obj, "ip_address", None) or None,
            "ip_addresses": lambda obj: self._format_ip_addresses(
                getattr(obj, "ip_addresses", None)
            ),
            "ipv6_cidr_block": lambda obj: (
                getattr(obj, "ipv6_cidr_block", None) or None
            ),
            "ipv6_cidr_blocks": lambda obj: self._format_list(
                getattr(obj, "ipv6_cidr_blocks", None)
            ),
            "shape_name": lambda obj: getattr(obj, "shape_name", None) or None,
            "display_name": lambda obj: getattr(obj, "display_name", None) or None,
            "cpe_device_shape_id": lambda obj: (
                getattr(obj, "cpe_device_shape_id", None) or None
            ),
            "provider_name": lambda obj: getattr(obj, "provider_name", None) or None,
            "provider_service_name": lambda obj: (
                getattr(obj, "provider_service_name", None) or None
            ),
            "public_ip_pool_id": lambda obj: (
                getattr(obj, "public_ip_pool_id", None) or None
            ),
            "volume_group_id": lambda obj: (
                getattr(obj, "volume_group_id", None) or None
            ),
            "resolver_id": lambda obj: getattr(obj, "resolver_id", None) or None,
            "view_id": lambda obj: getattr(obj, "view_id", None) or None,
            "zone_id": lambda obj: getattr(obj, "zone_id", None) or None,
            "steering_policy_id": lambda obj: (
                getattr(obj, "steering_policy_id", None) or None
            ),
            "user_id": lambda obj: getattr(obj, "user_id", None) or None,
            "group_id": lambda obj: getattr(obj, "group_id", None) or None,
            "tag_namespace_id": lambda obj: (
                getattr(obj, "tag_namespace_id", None) or None
            ),
            "hostname_label": lambda obj: getattr(obj, "hostname_label", None) or None,
            "is_primary": lambda obj: self._format_boolish(
                getattr(obj, "is_primary", None)
            ),
            "is_private": lambda obj: self._format_boolish(
                getattr(obj, "is_private", None)
            ),
            "is_protected": lambda obj: self._format_boolish(
                getattr(obj, "is_protected", None)
            ),
            "email_verified": lambda obj: self._format_boolish(
                getattr(obj, "email_verified", None)
            ),
            "is_mfa_activated": lambda obj: self._format_boolish(
                getattr(obj, "is_mfa_activated", None)
            ),
            "is_home_region": lambda obj: self._format_boolish(
                getattr(obj, "is_home_region", None)
            ),
            "is_cost_tracking": lambda obj: self._format_boolish(
                getattr(obj, "is_cost_tracking", None)
            ),
            "is_retired": lambda obj: self._format_boolish(
                getattr(obj, "is_retired", None)
            ),
            "lifetime": lambda obj: getattr(obj, "lifetime", None) or None,
            "direction": lambda obj: getattr(obj, "direction", None) or None,
            "protocol": lambda obj: getattr(obj, "protocol", None) or None,
            "source": lambda obj: getattr(obj, "source", None) or None,
            "device": lambda obj: getattr(obj, "device", None) or None,
            "domain": lambda obj: getattr(obj, "domain", None) or None,
            "rtype": lambda obj: getattr(obj, "rtype", None) or None,
            "ttl": lambda obj: self._format_number(getattr(obj, "ttl", None)),
            "rdata": lambda obj: getattr(obj, "rdata", None) or None,
            "scope": lambda obj: getattr(obj, "scope", None) or None,
            "scope_type": lambda obj: getattr(obj, "scope_type", None) or None,
            "value": lambda obj: self._format_number(getattr(obj, "value", None)),
            "are_quotas_supported": lambda obj: self._format_boolish(
                getattr(obj, "are_quotas_supported", None)
            ),
            "is_eligible_for_limit_increase": lambda obj: self._format_boolish(
                getattr(obj, "is_eligible_for_limit_increase", None)
            ),
            "zone_type": lambda obj: getattr(obj, "zone_type", None) or None,
            "domain_name": lambda obj: getattr(obj, "domain_name", None) or None,
            "template": lambda obj: getattr(obj, "template", None) or None,
            "address": lambda obj: getattr(obj, "address", None) or None,
            "endpoint_type": lambda obj: getattr(obj, "endpoint_type", None) or None,
            "email": lambda obj: getattr(obj, "email", None) or None,
            "matching_rule": lambda obj: getattr(obj, "matching_rule", None) or None,
            "statement_count": lambda obj: self._format_count(
                getattr(obj, "statements", None)
            ),
            "home_region": lambda obj: getattr(obj, "home_region", None) or None,
            "license_type": lambda obj: getattr(obj, "license_type", None) or None,
            "region_name": lambda obj: getattr(obj, "region_name", None) or None,
            "region_key": lambda obj: getattr(obj, "region_key", None) or None,
            "region": lambda obj: getattr(obj, "region", None) or None,
            "key": lambda obj: getattr(obj, "key", None) or None,
            "fingerprint": lambda obj: getattr(obj, "fingerprint", None) or None,
            "key_id": lambda obj: getattr(obj, "key_id", None) or None,
            "time_expires": lambda obj: self._format_time(
                getattr(obj, "time_expires", None)
            ),
            "expires_on": lambda obj: self._format_time(
                getattr(obj, "expires_on", None)
            ),
            "time_finished": lambda obj: self._format_time(
                getattr(obj, "time_finished", None)
            ),
            "time_drift_checked": lambda obj: self._format_time(
                getattr(obj, "time_drift_checked", None)
            ),
            "availability_domain_name": lambda obj: (
                getattr(obj, "availability_domain", None) or None
            ),
            "location_name": lambda obj: getattr(obj, "location_name", None) or None,
            "port_name": lambda obj: getattr(obj, "port_name", None) or None,
            "boot_volume_id": lambda obj: getattr(obj, "boot_volume_id", None) or None,
            "volume_id": lambda obj: getattr(obj, "volume_id", None) or None,
            "vpus_per_gb": lambda obj: self._format_number(
                getattr(obj, "vpus_per_gb", None)
            ),
            "volume_count": lambda obj: self._format_count(
                getattr(obj, "volume_ids", None)
            ),
            "type": lambda obj: getattr(obj, "type", None) or None,
            "bandwidth_shape_name": lambda obj: (
                getattr(obj, "bandwidth_shape_name", None) or None
            ),
            "is_read_only": lambda obj: self._format_boolish(
                getattr(obj, "is_read_only", None)
            ),
            "is_default": lambda obj: self._format_boolish(
                getattr(obj, "is_default", None)
            ),
            "is_free_tier": lambda obj: self._format_boolish(
                getattr(obj, "is_free_tier", None)
            ),
            "is_migration_required": lambda obj: self._format_boolish(
                getattr(obj, "is_migration_required", None)
            ),
            "is_sensitive": lambda obj: self._format_boolish(
                getattr(obj, "is_sensitive", None)
            ),
            "network_details_type": lambda obj: (
                getattr(getattr(obj, "network_details", None), "type", None) or None
            ),
            "route_type": lambda obj: getattr(obj, "route_type", None) or None,
            "next_hop_drg_attachment_id": lambda obj: (
                getattr(obj, "next_hop_drg_attachment_id", None) or None
            ),
            "message": lambda obj: getattr(obj, "message", None) or None,
            "code": lambda obj: getattr(obj, "code", None) or None,
            "supported_virtual_circuit_types": lambda obj: self._format_list(
                getattr(obj, "supported_virtual_circuit_types", None)
            ),
            "timestamp": lambda obj: self._first_present(
                self._format_time(getattr(obj, "timestamp", None)),
                self._format_time(getattr(obj, "time_accepted", None)),
                self._format_time(getattr(obj, "time_started", None)),
                self._format_time(getattr(obj, "time_finished", None)),
            ),
            "operation_type": lambda obj: getattr(obj, "operation_type", None) or None,
            "operation": lambda obj: getattr(obj, "operation", None) or None,
            "resource_type": lambda obj: getattr(obj, "resource_type", None) or None,
            "resource_name": lambda obj: getattr(obj, "resource_name", None) or None,
            "resource_address": lambda obj: (
                getattr(obj, "resource_address", None) or None
            ),
            "resource_drift_status": lambda obj: (
                getattr(obj, "resource_drift_status", None) or None
            ),
            "status": lambda obj: self._format_status_list(
                getattr(obj, "status", None)
            ),
            "percent_complete": lambda obj: self._format_number(
                getattr(obj, "percent_complete", None)
            ),
            "terraform_version": lambda obj: (
                getattr(obj, "terraform_version", None) or None
            ),
            "config_source_provider_type": lambda obj: (
                getattr(obj, "config_source_provider_type", None) or None
            ),
            "discovery_scope": lambda obj: (
                getattr(obj, "discovery_scope", None) or None
            ),
            "output_name": lambda obj: getattr(obj, "output_name", None) or None,
            "output_type": lambda obj: getattr(obj, "output_type", None) or None,
            "output_value": lambda obj: self._format_output_value(
                getattr(obj, "output_value", None)
            ),
            "ocpus": lambda obj: self._first_present(
                self._format_number(
                    getattr(getattr(obj, "shape_config", None), "ocpus", None)
                ),
                self._format_number(
                    getattr(getattr(obj, "node_shape_config", None), "ocpus", None)
                ),
            ),
            "memory_gb": lambda obj: self._first_present(
                self._format_number(
                    getattr(getattr(obj, "shape_config", None), "memory_in_gbs", None)
                ),
                self._format_number(
                    getattr(
                        getattr(obj, "node_shape_config", None), "memory_in_gbs", None
                    )
                ),
            ),
            "bandwidth_shape": lambda obj: (
                getattr(obj, "bandwidth_shape_name", None) or None
            ),
            "storage_size_gb": lambda obj: self._format_number(
                getattr(obj, "storage_size_in_gbs", None)
            ),
            "is_ipv6_enabled": lambda obj: self._format_boolish(
                getattr(obj, "is_ipv6enabled", None)
            ),
            "route_rules": lambda obj: self._format_count(
                getattr(obj, "route_rules", None)
            ),
            "security_rules": lambda obj: self._format_security_rule_count(obj),
            "attachments": lambda obj: self._format_count(
                self._first_nonempty_sequence(
                    getattr(obj, "attachments", None),
                    getattr(obj, "resources", None),
                )
            ),
            "destination": lambda obj: getattr(obj, "destination", None) or None,
            "destination_type": lambda obj: (
                getattr(obj, "destination_type", None) or None
            ),
            "source_type": lambda obj: getattr(obj, "source_type", None) or None,
            "gateway_type": lambda obj: getattr(obj, "gateway_type", None) or None,
            "kubernetes_version": lambda obj: (
                getattr(obj, "kubernetes_version", None) or None
            ),
            "cluster_type": lambda obj: getattr(obj, "type", None) or None,
            "private_endpoint": lambda obj: (
                getattr(getattr(obj, "endpoints", None), "private_endpoint", None)
                or None
            ),
            "public_endpoint": lambda obj: (
                getattr(getattr(obj, "endpoints", None), "public_endpoint", None)
                or None
            ),
            "node_shape": lambda obj: getattr(obj, "node_shape", None) or None,
            "cluster_id": lambda obj: getattr(obj, "cluster_id", None) or None,
            "node_count": lambda obj: self._format_number(
                getattr(getattr(obj, "node_config_details", None), "size", None)
            ),
            "subnet_count": lambda obj: self._format_count(
                getattr(obj, "subnet_ids", None)
            ),
            "dns_zone_count": lambda obj: self._format_count(
                getattr(obj, "dns_zones", None)
            ),
            "node_image_name": lambda obj: (
                getattr(obj, "node_image_name", None) or None
            ),
            "description": lambda obj: getattr(obj, "description", None) or None,
        }
        for key, extractor in field_extractors.items():
            value = extractor(item)
            if value not in (None, "-", ""):
                details[key] = str(value)
        return details

    @staticmethod
    def _first_present(*values: str) -> str | None:
        for value in values:
            if value not in (None, "-", ""):
                return value
        return None

    @staticmethod
    def _first_nonempty_sequence(*values: object) -> object | None:
        for value in values:
            if value:
                return value
        return None

    @staticmethod
    def _format_count(value: object) -> str | None:
        if value is None:
            return None
        try:
            return str(len(value))
        except TypeError:
            return None

    @staticmethod
    def _format_boolish(value: object) -> str | None:
        if value is None:
            return None
        return str(bool(value))

    @staticmethod
    def _format_ip_addresses(value: object) -> str | None:
        if not isinstance(value, list):
            return None
        addresses = [
            str(address)
            for item in value
            for address in [
                getattr(item, "ip_address", None)
                or (item.get("ip_address") if isinstance(item, dict) else None)
            ]
            if address
        ]
        return ", ".join(addresses) or None

    @staticmethod
    def _format_subnet_access(
        prohibit_public_ip: object, availability_domain: object
    ) -> str:
        visibility = "Private" if bool(prohibit_public_ip) else "Public"
        scope = (
            "Regional"
            if availability_domain in (None, "", "regional")
            else "AD-specific"
        )
        return f"{visibility} ({scope})"

    def _format_security_rule_count(self, item: object) -> str | None:
        ingress = getattr(item, "ingress_security_rules", None)
        egress = getattr(item, "egress_security_rules", None)
        if ingress is None and egress is None:
            return None
        ingress_count = len(ingress or [])
        egress_count = len(egress or [])
        return f"{ingress_count}/{egress_count}"

    @staticmethod
    def _format_status_list(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return str(value)

    @staticmethod
    def _format_list(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
        return str(value)

    def _resolve_resource_spec(self, value: str) -> ResourceSpec:
        resource_type = value.lower()
        normalized = self._normalize_resource_token(resource_type)
        namespace, _, name = resource_type.partition(".")
        namespace = self.resolve_namespace_path(namespace) or namespace.replace(
            "-", "_"
        )
        qualified = bool(_)
        candidates = (
            self.resource_specs if qualified else self.resource_specs_for_namespace()
        )
        if qualified:
            candidates = [spec for spec in candidates if spec.namespace == namespace]
            if not candidates:
                raise ValueError(f"unsupported resource type: {normalized}")
        alias_name = self.RESOURCE_ALIASES.get(
            namespace if qualified else "core", {}
        ).get(self._compact_resource_token(name if qualified else resource_type))
        if alias_name:
            alias = next((spec for spec in candidates if spec.name == alias_name), None)
            if alias is not None:
                return alias

        requested_name = name if qualified else resource_type
        requested_compact = self._compact_resource_token(requested_name)
        exact = [
            spec
            for spec in candidates
            if self._compact_resource_token(spec.name) == requested_compact
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            matches = ", ".join(
                spec.qualified_name
                for spec in sorted(exact, key=lambda item: item.qualified_name)
            )
            raise ValueError(f"ambiguous resource type: {normalized} ({matches})")
        singular = [
            spec
            for spec in candidates
            if self._compact_resource_token(spec.name) == requested_compact + "s"
        ]
        if len(singular) == 1:
            return singular[0]
        if len(singular) > 1:
            matches = ", ".join(
                spec.qualified_name
                for spec in sorted(singular, key=lambda item: item.qualified_name)
            )
            raise ValueError(f"ambiguous resource type: {normalized} ({matches})")
        if qualified:
            raise ValueError(f"unsupported resource type: {normalized}")
        default = next(
            (
                spec
                for spec in self.resource_specs
                if spec.namespace == "core"
                and self._compact_resource_token(spec.name) == requested_compact
            ),
            None,
        )
        if default is not None:
            return default
        raise ValueError(f"unsupported resource type: {normalized}")

    @staticmethod
    def _normalize_resource_token(value: str) -> str:
        return value.lower().replace("_", "-")

    @staticmethod
    def _compact_resource_token(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    def _list_compartment_rows(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        return [
            ResourceRow(
                name=node.name,
                state=node.lifecycle_state,
                id=node.id,
                extra=node.description or "",
                details={"description": node.description or "-", "ocid": node.id},
                payload=oci.util.to_dict(self.identity.get_compartment(node.id).data),
            )
            for node in self.list_children(self.current.id)
        ]

    def _list_load_balancer_listeners(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        load_balancer_id = (filters or {}).get("load_balancer_id")
        if not load_balancer_id:
            raise ValueError("load_balancer.listeners requires load_balancer_id")
        load_balancer = self.load_balancer.get_load_balancer(load_balancer_id).data
        listeners = getattr(load_balancer, "listeners", None) or {}
        rows: list[ResourceRow] = []
        for map_key, listener in listeners.items():
            name = getattr(listener, "name", None) or map_key
            payload = oci.util.to_dict(listener)
            if not isinstance(payload, dict):
                payload = {}
            payload["load_balancer_id"] = load_balancer_id
            rows.append(
                ResourceRow(
                    name=self._humanize_name(name),
                    state="ACTIVE",
                    id=f"{load_balancer_id}:listener:{name}",
                    details={"load_balancer_id": load_balancer_id},
                    payload=payload,
                )
            )
        return sorted(rows, key=lambda row: row.name.lower())

    def _list_node_pool_options(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        node_pool_option_id = (filters or {}).get("node_pool_option_id")
        if not node_pool_option_id:
            raise ValueError(
                "containerengine.node-pool-options requires node_pool_option_id"
            )
        response = self._retry_oci_call(
            self.container_engine.get_node_pool_options, node_pool_option_id
        )
        payload = oci.util.to_dict(response.data)
        if not isinstance(payload, dict):
            payload = {}
        payload["node_pool_option_id"] = node_pool_option_id
        details = {
            "kubernetes_versions": len(payload.get("kubernetes_versions") or []),
            "shapes": len(payload.get("shapes") or []),
            "images": len(payload.get("images") or []),
            "sources": len(payload.get("sources") or []),
        }
        return [
            ResourceRow(
                name="options",
                state="AVAILABLE",
                id=f"node-pool-options:{node_pool_option_id}",
                details=details,
                payload=payload,
            )
        ]

    def _list_terraform_versions(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        kwargs = dict(filters or {})
        compartment_id = kwargs.pop("compartment_id", self.current.id)
        if compartment_id == self.root.id:
            raise ValueError("orm.terraform_versions requires a non-root compartment")
        response = self.resource_manager.list_terraform_versions(
            compartment_id=compartment_id, **kwargs
        )
        items = self._collection_items(response.data)
        return sorted(
            [self._row_from_oci_item(item) for item in items],
            key=lambda row: row.name.lower(),
        )

    def _list_orm_jobs(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        kwargs = dict(filters or {})
        compartment_id = kwargs.pop("compartment_id", self.current.id)
        items = self._collection_items(
            oci.pagination.list_call_get_all_results(
                self.resource_manager.list_jobs,
                compartment_id=compartment_id,
                **kwargs,
            ).data
        )
        rows = [self._row_from_oci_item(item) for item in items]
        if rows or "stack_id" in kwargs:
            return sorted(rows, key=lambda row: row.name.lower())
        seen = {row.id for row in rows}
        search_query = (
            f"query OrmJob resources where compartmentId = '{compartment_id}'"
        )
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=search_query
        )
        try:
            search_items = self.resource_search.search_resources(details).data.items
        except Exception as exc:
            self.record_partial_failure("search_orm_jobs", exc)
            search_items = []
        for item in search_items:
            row = self._row_from_oci_item(item)
            if row.id in seen:
                continue
            payload = dict(row.payload or {})
            payload["search_only"] = True
            detail_map = dict(row.details or {})
            detail_map["search_only"] = "True"
            rows.append(
                ResourceRow(
                    name=row.name,
                    state=row.state,
                    id=row.id,
                    extra=row.extra or "[search-only]",
                    details=detail_map,
                    payload=payload,
                )
            )
            seen.add(row.id)
        return sorted(rows, key=lambda row: row.name.lower())

    def _list_vcns(self, filters: dict[str, str] | None = None) -> list[ResourceRow]:
        items = oci.pagination.list_call_get_all_results(
            self.virtual_network.list_vcns,
            self.current.id,
            **(filters or {}),
        ).data
        return sorted(
            [
                ResourceRow(
                    name=self._humanize_name(item.display_name or item.id),
                    state=str(item.lifecycle_state),
                    id=item.id,
                    extra=(item.cidr_block or ""),
                    details={
                        "cidr": item.cidr_block or "-",
                        "ipv6_cidr_blocks": self._format_list(
                            getattr(item, "ipv6_cidr_blocks", None)
                        ),
                        "dns_label": getattr(item, "dns_label", None) or "-",
                        "is_ipv6_enabled": str(getattr(item, "is_ipv6enabled", False)),
                        "default_route_table_id": getattr(
                            item, "default_route_table_id", None
                        )
                        or "-",
                        "default_security_list_id": getattr(
                            item, "default_security_list_id", None
                        )
                        or "-",
                        "created": self._format_time(
                            getattr(item, "time_created", None)
                        ),
                    },
                    payload=oci.util.to_dict(item),
                )
                for item in items
            ],
            key=lambda row: row.name.lower(),
        )

    def _list_subnets(self, filters: dict[str, str] | None = None) -> list[ResourceRow]:
        items = oci.pagination.list_call_get_all_results(
            self.virtual_network.list_subnets,
            self.current.id,
            **(filters or {}),
        ).data
        return sorted(
            [
                ResourceRow(
                    name=self._humanize_name(item.display_name or item.id),
                    state=str(item.lifecycle_state),
                    id=item.id,
                    extra=(item.cidr_block or ""),
                    details={
                        "cidr": item.cidr_block or "-",
                        "ipv6_cidr_blocks": self._format_list(
                            getattr(item, "ipv6_cidr_blocks", None)
                        ),
                        "availability_domain": getattr(
                            item, "availability_domain", None
                        )
                        or "regional",
                        "dns_label": getattr(item, "dns_label", None) or "-",
                        "vcn_id": getattr(item, "vcn_id", None) or "-",
                        "subnet_access": self._format_subnet_access(
                            getattr(item, "prohibit_public_ip_on_vnic", False),
                            getattr(item, "availability_domain", None),
                        ),
                        "created": self._format_time(
                            getattr(item, "time_created", None)
                        ),
                    },
                    payload=oci.util.to_dict(item),
                )
                for item in items
            ],
            key=lambda row: row.name.lower(),
        )

    def _list_ipv6s(self, filters: dict[str, str] | None = None) -> list[ResourceRow]:
        filters = filters or {}
        parent_keys = {
            key: value
            for key, value in filters.items()
            if key in {"subnet_id", "vnic_id"}
        }
        if not parent_keys:
            raise ValueError("core.ipv6s requires a subnet or VNIC parent context")
        items = self._retry_oci_call(
            oci.pagination.list_call_get_all_results,
            self.virtual_network.list_ipv6s,
            **parent_keys,
        ).data
        return sorted(
            (self._row_from_oci_item(item) for item in items),
            key=lambda row: row.name.lower(),
        )

    def _list_instances(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        items = oci.pagination.list_call_get_all_results(
            self.compute.list_instances,
            self.current.id,
            **(filters or {}),
        ).data
        vnics_by_instance = self._get_vnic_map(self.current.id)
        return sorted(
            [
                ResourceRow(
                    name=self._humanize_name(item.display_name or item.id),
                    state=str(item.lifecycle_state),
                    id=item.id,
                    extra=(item.shape or ""),
                    details={
                        "public_ip": vnics_by_instance.get(item.id, {}).get(
                            "public_ip", "-"
                        ),
                        "private_ip": vnics_by_instance.get(item.id, {}).get(
                            "private_ip", "-"
                        ),
                        "shape": item.shape or "-",
                        "ocpus": self._format_number(
                            getattr(item.shape_config, "ocpus", None)
                        ),
                        "memory_gb": self._format_number(
                            getattr(item.shape_config, "memory_in_gbs", None)
                        ),
                        "availability_domain": getattr(item, "availability_domain", "-")
                        or "-",
                        "fault_domain": getattr(item, "fault_domain", "-") or "-",
                        "created": self._format_time(
                            getattr(item, "time_created", None)
                        ),
                    },
                    payload=oci.util.to_dict(item),
                )
                for item in items
            ],
            key=lambda row: row.name.lower(),
        )

    def _list_custom_images(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        return [
            row
            for row in self._list_images(filters)
            if (row.details or {}).get("compartment_id") not in ("-", "None", None)
        ]

    def _list_platform_images(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        return [
            row
            for row in self._list_images(filters)
            if (row.details or {}).get("compartment_id") in ("-", "None", None)
        ]

    def _list_images(self, filters: dict[str, str] | None = None) -> list[ResourceRow]:
        items = oci.pagination.list_call_get_all_results(
            self.compute.list_images,
            self.current.id,
            **(filters or {}),
        ).data
        image_names = {
            item.id: self._humanize_name(item.display_name or item.id)
            for item in items
            if getattr(item, "id", None)
        }
        return sorted(
            [
                ResourceRow(
                    name=self._humanize_name(item.display_name or item.id),
                    state=str(item.lifecycle_state),
                    id=item.id,
                    extra=(
                        (item.operating_system or "")
                        + (
                            " " + item.operating_system_version
                            if item.operating_system_version
                            else ""
                        )
                    ).strip(),
                    details={
                        "os": item.operating_system or "-",
                        "os_version": item.operating_system_version or "-",
                        "base_image": image_names.get(
                            getattr(item, "base_image_id", None),
                            getattr(item, "base_image_id", None) or "-",
                        ),
                        "compartment_id": str(
                            getattr(item, "compartment_id", None) or "-"
                        ),
                        "size_mbs": self._format_number(
                            getattr(item, "size_in_mbs", None)
                        ),
                        "size_gb": self._format_size_gb_from_mbs(
                            getattr(item, "size_in_mbs", None)
                        ),
                        "launch_mode": getattr(item, "launch_mode", None) or "-",
                        "created": self._format_time(
                            getattr(item, "time_created", None)
                        ),
                    },
                    payload=oci.util.to_dict(item),
                )
                for item in items
            ],
            key=lambda row: row.name.lower(),
        )

    def _list_route_rules(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        if not filters or "route_table_id" not in filters:
            raise ValueError("core.route-rules requires route_table_id")
        route_table = self.virtual_network.get_route_table(
            filters["route_table_id"]
        ).data
        items = getattr(route_table, "route_rules", None) or []
        rows: list[ResourceRow] = []
        for idx, item in enumerate(items, start=1):
            payload = oci.util.to_dict(item)
            rows.append(
                ResourceRow(
                    name=str(payload.get("destination") or f"rule-{idx}"),
                    state=str(payload.get("route_type") or "-"),
                    id=str(payload.get("network_entity_id") or f"route-rule-{idx}"),
                    extra=str(payload.get("description") or ""),
                    details={
                        "destination": payload.get("destination") or "-",
                        "destination_type": payload.get("destination_type") or "-",
                        "network_entity_id": payload.get("network_entity_id") or "-",
                        "route_type": payload.get("route_type") or "-",
                        "description": payload.get("description") or "-",
                    },
                    payload=payload,
                )
            )
        return rows

    def _list_security_list_rules(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        if not filters or "security_list_id" not in filters:
            raise ValueError("core.security-list-rules requires security_list_id")
        security_list = self.virtual_network.get_security_list(
            filters["security_list_id"]
        ).data
        rows: list[ResourceRow] = []
        for direction, attr_name in (
            ("INGRESS", "ingress_security_rules"),
            ("EGRESS", "egress_security_rules"),
        ):
            items = getattr(security_list, attr_name, None) or []
            for idx, item in enumerate(items, start=1):
                payload = oci.util.to_dict(item)
                if not isinstance(payload, dict):
                    payload = {
                        "source": getattr(item, "source", None),
                        "destination": getattr(item, "destination", None),
                        "protocol": getattr(item, "protocol", None),
                        "description": getattr(item, "description", None),
                    }
                payload["direction"] = direction
                source = payload.get("source")
                destination = payload.get("destination")
                protocol = payload.get("protocol")
                anchor = source if direction == "INGRESS" else destination
                name = str(anchor or f"{direction.lower()}-{idx}")
                rid = str(
                    payload.get("id")
                    or payload.get("security_rule_id")
                    or f"{filters['security_list_id']}:{direction}:{idx}"
                )
                rows.append(
                    ResourceRow(
                        name=name,
                        state=str(protocol or "-"),
                        id=rid,
                        extra=str(payload.get("description") or ""),
                        details={
                            "direction": direction,
                            "protocol": str(protocol or "-"),
                            "source": str(source or "-"),
                            "destination": str(destination or "-"),
                            "description": str(payload.get("description") or "-"),
                        },
                        payload=payload,
                    )
                )
        return rows

    def _list_zone_records(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        if not filters or "zone_name_or_id" not in filters:
            raise ValueError("dns.zone-records requires zone_name_or_id")
        response = self.dns.get_zone_records(filters["zone_name_or_id"]).data
        items = getattr(response, "items", None) or []
        rows: list[ResourceRow] = []
        for idx, item in enumerate(items, start=1):
            payload = oci.util.to_dict(item)
            name = str(payload.get("domain") or f"record-{idx}")
            rid = str(
                payload.get("record_hash") or f"{name}:{payload.get('rtype', idx)}"
            )
            rows.append(
                ResourceRow(
                    name=name,
                    state=str(payload.get("rtype") or "-"),
                    id=rid,
                    extra=str(payload.get("rdata") or ""),
                    details={
                        "domain": payload.get("domain") or "-",
                        "rtype": payload.get("rtype") or "-",
                        "ttl": str(payload.get("ttl") or "-"),
                        "rdata": payload.get("rdata") or "-",
                        "is_protected": str(bool(payload.get("is_protected", False))),
                    },
                    payload=payload,
                )
            )
        return rows

    def _list_region_subscriptions(
        self, filters: dict[str, str] | None = None
    ) -> list[ResourceRow]:
        items = oci.pagination.list_call_get_all_results(
            self.identity.list_region_subscriptions,
            tenancy_id=self.tenancy_id,
            **(filters or {}),
        ).data
        return sorted(
            [self._row_from_oci_item(item) for item in items],
            key=lambda row: row.name.lower(),
        )

    def _get_vnic_map(self, compartment_id: str) -> dict[str, dict[str, str]]:
        attachments = oci.pagination.list_call_get_all_results(
            self.compute.list_vnic_attachments,
            compartment_id=compartment_id,
        ).data
        result: dict[str, dict[str, str]] = {}
        for attachment in attachments:
            if not getattr(attachment, "vnic_id", None) or not getattr(
                attachment, "instance_id", None
            ):
                continue
            try:
                vnic = self.virtual_network.get_vnic(attachment.vnic_id).data
            except Exception as exc:
                self.record_partial_failure("enrich_instance_vnic", exc)
                continue
            result[attachment.instance_id] = {
                "public_ip": getattr(vnic, "public_ip", None) or "-",
                "private_ip": getattr(vnic, "private_ip", None) or "-",
            }
        return result

    @staticmethod
    def _format_number(value: object) -> str:
        if value is None:
            return "-"
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    @staticmethod
    def _format_time(value: object) -> str:
        if value is None:
            return "-"
        return str(value)

    @staticmethod
    def _format_size_gb_from_mbs(value: object) -> str:
        if value is None:
            return "-"
        try:
            size_gb = float(value) / 1024.0
        except (TypeError, ValueError):
            return str(value)
        if size_gb.is_integer():
            return str(int(size_gb))
        return f"{size_gb:.1f}"

    @staticmethod
    def _format_output_value(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        return str(value)
