from __future__ import annotations

import time
from collections.abc import Callable

import oci

from models import CompartmentNode, ResourceRow, ResourceSpec


class InventoryCatalogOpsMixin:
    """Search-backed catalog construction and discovery operations."""

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

