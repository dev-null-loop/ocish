from __future__ import annotations

import importlib
import inspect
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Iterable
from typing import ClassVar

import oci

from config import (
    DIRECT_GETTERS,
    NAMESPACE_DISCOVERY,
    NAMESPACE_PATH_SLUGS,
    RESOURCE_ENRICHMENTS,
)
from inventory_adapters.core import CoreInventoryMixin
from inventory_adapters.object_storage import ObjectStorageInventoryMixin
from inventory_adapters.openai import OpenAIInventoryMixin
from inventory_adapters.queries import QueryInventoryMixin
from inventory_catalog import ActiveRegionCatalog  # noqa: F401
from inventory_registry import InventoryRegistryMixin
from inventory_rows import InventoryRowsMixin
from inventory_transport import InventoryTransportMixin
from models import CompartmentNode, RelationshipTarget, ResourceRow, ResourceSpec


class OciCompartmentBrowser(ObjectStorageInventoryMixin, OpenAIInventoryMixin, QueryInventoryMixin, CoreInventoryMixin, InventoryRowsMixin, InventoryTransportMixin, InventoryRegistryMixin):
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
        for attribute, class_path in self._declared_client_classes().items():
            module_name, class_name = class_path.rsplit(".", 1)
            client_class = getattr(importlib.import_module(module_name), class_name)
            setattr(self, attribute, client_class(self.config))

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
                row = ResourceRow(
                    name=self._humanize_name(name or identifier or "<unnamed>"),
                    state=str(payload.get("lifecycle_state") or "-"),
                    id=str(identifier or name or ""),
                    details={
                        "compartment_id": str(item_compartment_id or "-"),
                        "resource_type": str(resource_type or "-"),
                    },
                    payload=payload,
                )
                if spec is not None and isinstance(resource_type, str):
                    previous = self.search_type_cache.get(spec.qualified_name)
                    if previous is None or len(resource_type) > len(previous):
                        self.search_type_cache[spec.qualified_name] = resource_type
                    spec = self.canonical_spec_for_search_row(spec, row)
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
                    ).append(row)
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
                row = ResourceRow(
                    name=self._humanize_name(
                        payload.get("display_name")
                        or payload.get("name")
                        or payload.get("identifier")
                        or "<unnamed>"
                    ),
                    state=str(payload.get("lifecycle_state") or "-"),
                    id=str(payload.get("identifier") or payload.get("id") or ""),
                    details={
                        "compartment_id": str(payload.get("compartment_id") or "-"),
                        "resource_type": str(payload.get("resource_type") or "-"),
                    },
                    payload=payload,
                )
                spec = self.canonical_spec_for_search_row(spec, row)
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
            actual = target.spec.qualified_name if target.spec is not None else "unknown"
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
        config = dict(spec.adapter_config)
        field = config.get("canonical_search_field")
        owned_type = config.get("canonical_search_owned")
        catalog_type = config.get("canonical_search_catalog")
        if not all(isinstance(value, str) for value in (field, owned_type, catalog_type)):
            return spec
        value = (row.details or {}).get(field)
        if value is None:
            value = (self.hydrate_resource_row(row).details or {}).get(field)
        if value in (None, "", "-", "None", self.tenancy_id):
            return self.resolve_resource_spec(self._qualified_variant(spec, catalog_type))
        return self.resolve_resource_spec(self._qualified_variant(spec, owned_type))

    @staticmethod
    def _qualified_variant(spec: ResourceSpec, resource_type: str) -> str:
        if "." in resource_type:
            return resource_type
        return f"{spec.namespace}.{resource_type}"

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
        adapter_config = dict(spec.adapter_config)
        accepted = set(spec.accepted_params or spec.required_params)
        if "compartment_id" in accepted:
            kwargs["compartment_id"] = compartment_id or self.current.id
        if "scope" in accepted:
            kwargs["scope"] = "REGION"
        if extra_kwargs:
            kwargs.update(extra_kwargs)
        availability_domains: tuple[str | None, ...] = (None,)
        if (
            adapter_config.get("availability_domain_mode") == "all"
            and "availability_domain" in accepted
        ):
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.identity.list_availability_domains,
                self.tenancy_id,
            )
            availability_domains = tuple(
                str(getattr(domain, "name", ""))
                for domain in self._collection_items(response.data)
                if getattr(domain, "name", None)
            )
        items: list[object] = []
        for availability_domain in availability_domains:
            call_kwargs = dict(kwargs)
            if availability_domain is not None:
                call_kwargs["availability_domain"] = availability_domain
            response_data = self._retry_oci_call(
                oci.pagination.list_call_get_all_results, operation, **call_kwargs
            ).data
            items.extend(self._collection_items(response_data))
        rows = [self._row_from_oci_item(item) for item in items]
        return sorted({row.id: row for row in rows}.values(), key=lambda row: row.name.lower())
