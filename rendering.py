from __future__ import annotations

import json
import re
import shlex
import time
import urllib.parse
from collections.abc import Iterable

import oci

from config import (
    ADDRESS_COLUMN_PRIORITY,
    GENERIC_COLUMN_LABELS,
    GENERIC_COLUMN_PRIORITY,
    LONG_COLUMNS,
)
from models import CompartmentNode, ResourceRow, ResourceSpec


class RenderingMixin:
    DEFAULT_LONG_COLUMN_LIMIT = 6

    def _print_children(self, children: Iterable[CompartmentNode]) -> None:
        for child in children:
            print(child.name)

    def _print_children_long(self, children: Iterable[CompartmentNode]) -> None:
        child_list = list(children)
        if not child_list:
            return
        width = max(len(child.name) for child in child_list)
        for child in child_list:
            description = child.description or ""
            print(f"{child.name:<{width}}  {child.lifecycle_state:<8}  {description}")

    def _print_resources(self, rows: Iterable[ResourceRow]) -> None:
        for row in rows:
            print(self._display_text(row.name))

    @staticmethod
    def _display_text(value: object) -> object:
        return urllib.parse.unquote(value) if isinstance(value, str) else value

    @staticmethod
    def _search_type_name(spec: ResourceSpec) -> str:
        if spec.search_type:
            return spec.search_type
        name = spec.name.replace("-", "_")
        if name.endswith("ies"):
            base = name[:-3] + "y"
        elif name.endswith("s"):
            base = name[:-1]
        else:
            base = name
        return "".join(part.capitalize() for part in base.split("_") if part)

    @staticmethod
    def _search_regex_literal(value: str) -> str:
        escaped = re.escape(value)
        escaped = escaped.replace("'", "\\'")
        return f".*{escaped}.*"

    @staticmethod
    def _search_string_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _chunked(values: list[str], size: int) -> list[list[str]]:
        return [values[idx : idx + size] for idx in range(0, len(values), size)]

    def _search_summary_row(self, item: object) -> ResourceRow:
        payload = oci.util.to_dict(item)
        name = (
            payload.get("display_name")
            or payload.get("name")
            or payload.get("identifier")
            or "<unnamed>"
        )
        rid = payload.get("identifier") or payload.get("id") or name
        state = payload.get("lifecycle_state") or payload.get("resource_type") or "-"
        details = {
            "compartment_id": str(payload.get("compartment_id") or "-"),
            "resource_type": str(payload.get("resource_type") or "-"),
        }
        if payload.get("time_created"):
            details["created"] = self.browser._format_time(payload.get("time_created"))
        return ResourceRow(
            name=str(name),
            state=str(state),
            id=str(rid),
            extra=str(payload.get("resource_type") or ""),
            details=details,
            payload=payload,
        )

    def _compartment_path_map(self) -> dict[str, str]:
        return {
            node.id: path
            for node, parents in self._subtree_compartment_targets()
            for path in ["/" + "/".join(n.name for n in [*parents, node])]
        }

    def _canonical_find_path(
        self, compartment_path: str, spec: ResourceSpec, row: ResourceRow
    ) -> str:
        """Return the absolute namespace address for a discovered resource."""
        return "/".join(
            (
                "",
                self.browser.region(),
                compartment_path.strip("/"),
                self.browser.namespace_path_slug(spec.namespace),
                spec.name.replace("_", "-"),
                self.browser._sanitize_row_name(row.name),
            )
        )

    def _uniquify_find_paths(
        self, results: list[tuple[str, ResourceRow]]
    ) -> list[tuple[str, ResourceRow]]:
        """Keep search output copy-pastable when siblings share a display name."""
        grouped: dict[str, list[ResourceRow]] = {}
        for path, row in results:
            grouped.setdefault(path.rsplit("/", 1)[0], []).append(row)
        return [
            (f"{parent}/{self.browser._sanitize_row_name(row.name)}", row)
            for parent, rows in grouped.items()
            for row in self.browser._uniquify_row_names(rows)
        ]

    def _find_search_specs(
        self, type_filters: list[str] | None = None
    ) -> list[ResourceSpec]:
        if not type_filters:
            return [
                spec
                for spec in self.browser.resource_specs_for_namespace(
                    self.namespace_view
                )
                if spec.findable
            ]
        specs: list[ResourceSpec] = []
        for resource_type in type_filters:
            spec = self.browser.resolve_resource_spec(resource_type)
            if not spec.findable:
                raise ValueError(
                    f"{spec.qualified_name} is not searchable in hierarchy mode"
                )
            specs.append(spec)
        return specs

    def _find_via_search(
        self,
        specs: list[ResourceSpec],
        name_pattern: str | None,
        include_lister_results: bool = False,
    ) -> list[tuple[str, ResourceRow]]:
        at_tenancy_root = (
            self.browser.current.id == self.browser.root.id and not self.browser.parents
        )
        path_map = {} if at_tenancy_root else self._compartment_path_map()
        if not at_tenancy_root and not path_map:
            return []
        results: list[tuple[str, ResourceRow]] = []
        seen: set[str] = set()
        scopes = [(None, None)] if at_tenancy_root else list(path_map.items())
        for spec in specs:
            search_type = self.browser.search_type_for(spec)
            fallback_to_list = False
            for scope_compartment_id, scope_path in scopes:
                clauses: list[str] = []
                if scope_compartment_id is not None:
                    clauses.append(
                        "compartmentId = "
                        f"'{self._search_string_literal(scope_compartment_id)}'"
                    )
                if name_pattern:
                    clauses.append(
                        f"displayName =~ '{self._search_regex_literal(name_pattern)}'"
                    )
                query = f"query {search_type} resources"
                if clauses:
                    query += " where " + " && ".join(clauses)
                details = oci.resource_search.models.StructuredSearchDetails(
                    type="Structured", query=query
                )
                page: str | None = None
                while True:
                    try:
                        response = self.browser._retry_oci_call(
                            self.browser.resource_search.search_resources,
                            details,
                            limit=self.FIND_PAGE_SIZE,
                            page=page,
                        )
                    except Exception as exc:
                        if self._is_unknown_search_resource_type(exc):
                            fallback_to_list = True
                            break
                        location = scope_path or self.browser.get_path()
                        raise RuntimeError(
                            f"OCI Search failed for {location}: {exc}"
                        ) from exc
                    items = list(response.data.items or [])
                    for item in items:
                        row = self._search_summary_row(item)
                        if row.id in seen:
                            continue
                        row_compartment_id = str(
                            (row.details or {}).get("compartment_id") or "-"
                        )
                        resource_path = path_map.get(row_compartment_id)
                        if resource_path is None and at_tenancy_root:
                            chain = self.browser.compartment_chain(row_compartment_id)
                            resource_path = "/" + "/".join(node.name for node in chain)
                            path_map[row_compartment_id] = resource_path
                        if resource_path is None:
                            continue
                        canonical = self._canonical_find_path(resource_path, spec, row)
                        results.append((canonical, row))
                        seen.add(row.id)
                        if len(results) >= self.FIND_RESULT_LIMIT:
                            return self._uniquify_find_paths(results)
                    page = getattr(response, "next_page", None)
                    if not page:
                        break
                if fallback_to_list:
                    break
            if fallback_to_list or include_lister_results:
                for result in self._find_via_resource_lists(spec, name_pattern):
                    if result[1].id in seen:
                        continue
                    results.append(result)
                    seen.add(result[1].id)
                    if len(results) >= self.FIND_RESULT_LIMIT:
                        return self._uniquify_find_paths(results)
        return self._uniquify_find_paths(results)

    @staticmethod
    def _is_unknown_search_resource_type(exc: Exception) -> bool:
        return getattr(
            exc, "code", None
        ) == "CannotParseRequest" and "Unknown resource type" in str(exc)

    def _find_via_resource_lists(
        self, spec: ResourceSpec, name_pattern: str | None
    ) -> list[tuple[str, ResourceRow]]:
        """Fallback for registered types that OCI Search cannot query directly."""
        saved_current = self.browser.current
        saved_parents = list(self.browser.parents)
        results: list[tuple[str, ResourceRow]] = []
        try:
            for node, parents in self._subtree_compartment_targets():
                self.browser.current = node
                self.browser.parents = list(parents)
                try:
                    rows = self.browser.list_resources(spec.qualified_name)
                except Exception as exc:
                    self.browser.record_partial_failure("find_resource_list", exc)
                    continue
                path = "/" + "/".join(item.name for item in [*parents, node])
                for row in rows:
                    if (
                        name_pattern
                        and name_pattern.casefold() not in row.name.casefold()
                    ):
                        continue
                    results.append((self._canonical_find_path(path, spec, row), row))
        finally:
            self.browser.current = saved_current
            self.browser.parents = saved_parents
        return results

    def _find_all_ocids_via_search(self) -> list[str]:
        """Return every OCI Search-indexed resource OCID in the current compartment."""
        compartment_id = self.browser.current.id
        query = f"query all resources where compartmentId = '{self._search_string_literal(compartment_id)}'"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        ocids: list[str] = []
        seen: set[str] = set()
        page: str | None = None
        while True:
            response = self.browser._retry_oci_call(
                self.browser.resource_search.search_resources,
                details,
                limit=self.FIND_ALL_PAGE_SIZE,
                page=page,
                retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
            ).data
            items = list(response.items or [])
            for item in items:
                payload = oci.util.to_dict(item)
                if isinstance(payload, dict):
                    ocid = payload.get("identifier") or payload.get("id")
                else:
                    ocid = getattr(item, "identifier", None) or getattr(
                        item, "id", None
                    )
                if (
                    not isinstance(ocid, str)
                    or not ocid.startswith("ocid1.")
                    or ocid in seen
                ):
                    continue
                ocids.append(ocid)
                seen.add(ocid)
            page = getattr(response, "opc_next_page", None)
            if not page or not items:
                return ocids
            time.sleep(self.FIND_ALL_PAGE_DELAY_SECONDS)

    def _find_current_compartment_paths_via_search(
        self,
    ) -> list[tuple[str, ResourceRow]]:
        """Build canonical paths from one compartment-scoped OCI Search query."""
        results: list[tuple[str, ResourceRow]] = []
        seen: set[str] = set()
        tenancy_base = f"/{self._effective_region()}/{self.browser.root.name}"

        def finalized() -> list[tuple[str, ResourceRow]]:
            grouped: dict[str, list[ResourceRow]] = {}
            for path, row in results:
                grouped.setdefault(path.rsplit("/", 1)[0], []).append(row)
            return [
                (f"{parent}/{self.browser._sanitize_row_name(row.name)}", row)
                for parent, rows in grouped.items()
                for row in self.browser._uniquify_row_names(rows)
            ]

        for node, parents in self._subtree_compartment_targets():
            query = (
                "query all resources where compartmentId = "
                f"'{self._search_string_literal(node.id)}'"
            )
            details = oci.resource_search.models.StructuredSearchDetails(
                type="Structured", query=query
            )
            page: str | None = None
            compartment_base = "/" + "/".join(
                [self._effective_region(), *(item.name for item in [*parents, node])]
            )
            while True:
                response = self.browser._retry_oci_call(
                    self.browser.resource_search.search_resources,
                    details,
                    limit=self.FIND_ALL_PAGE_SIZE,
                    page=page,
                    retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
                ).data
                items = list(response.items or [])
                for item in items:
                    row = self._search_summary_row(item)
                    if row.id in seen:
                        continue
                    spec = self.browser._resource_spec_for_search_type(
                        (row.payload or {}).get("resource_type")
                    )
                    if spec is None or (
                        spec.scope
                        not in {"region", "regional-catalog", "tenancy", "global"}
                        and not spec.scope.startswith("compartment")
                    ):
                        continue
                    spec = self.browser.canonical_spec_for_search_row(spec, row)
                    name = self.browser._sanitize_row_name(row.name)
                    if spec.scope in {"region", "regional-catalog"}:
                        base = f"{tenancy_base}/catalog"
                    elif spec.scope in {"tenancy", "global"}:
                        base = tenancy_base
                    else:
                        base = compartment_base
                    results.append(
                        (
                            f"{base}/{self.browser.namespace_path_slug(spec.namespace)}/{spec.name.replace('_', '-')}/{name}",
                            row,
                        )
                    )
                    seen.add(row.id)
                    if len(results) >= self.FIND_RESULT_LIMIT:
                        return finalized()
                page = getattr(response, "opc_next_page", None)
                if not page or not items:
                    break
                time.sleep(self.FIND_ALL_PAGE_DELAY_SECONDS)
        return finalized()

    def _parse_find_args(self, arg: str) -> tuple[str | None, list[str], str | None]:
        try:
            argv = shlex.split(arg)
        except ValueError as exc:
            raise ValueError(f"parse error: {exc}") from exc
        if not argv:
            raise ValueError(
                "usage: find . | find <type> [name] | find <path> <type> [name]"
            )
        if any(token.startswith("-") for token in argv):
            raise ValueError(
                "usage: find . | find <type> [name] | find <path> <type> [name]"
            )
        if len(argv) == 1:
            return None, [argv[0]], None
        if len(argv) == 2:
            # Prefer the natural current-location form: ``find <type> <name>``.
            # A resource type is static, while a relative compartment path is
            # dynamic; recognizing the type first prevents ``find instances
            # basti`` from attempting to enter a compartment named
            # ``instances``.
            try:
                self.browser.resolve_resource_spec(argv[0])
            except ValueError:
                pass
            else:
                return argv[1], [argv[0]], None
            return None, [argv[1]], argv[0]
        if len(argv) == 3:
            return argv[2], [argv[1]], argv[0]
        raise ValueError(
            "usage: find . | find <type> [name] | find <path> <type> [name]"
        )

    def _subtree_compartment_targets(
        self,
    ) -> list[tuple[CompartmentNode, list[CompartmentNode]]]:
        root = self.browser.current
        root_parents = list(self.browser.parents)
        targets: list[tuple[CompartmentNode, list[CompartmentNode]]] = [
            (root, root_parents)
        ]
        queue: list[tuple[CompartmentNode, list[CompartmentNode]]] = [
            (root, root_parents)
        ]
        while queue:
            node, parents = queue.pop(0)
            child_parents = [*parents, node]
            for child in self.browser.list_children(node.id):
                targets.append((child, child_parents))
                queue.append((child, child_parents))
        return targets

    def _print_find_results(self, results: list[tuple[str, ResourceRow]]) -> None:
        if not results:
            return
        self._remember_namespace_paths([path for path, _row in results])
        for path, _row in sorted(results, key=lambda item: item[0].lower()):
            print(self._display_text(path))

    def _print_resources_long(
        self, spec: ResourceSpec, rows: Iterable[ResourceRow]
    ) -> None:
        row_list = list(rows)
        if not row_list:
            return
        columns = self._resource_long_columns(spec)
        if columns is not None:
            self._print_detail_table(
                row_list, self._effective_long_columns(row_list, columns)
            )
            return
        generic_columns = self._infer_generic_long_columns(row_list)
        if generic_columns is not None:
            self._print_detail_table(row_list, generic_columns)
            return
        self._print_simple_table(
            [("name", "Name"), ("state", "State")],
            [{"name": row.name, "state": row.state} for row in row_list],
        )

    def _resource_long_columns(
        self, spec: ResourceSpec
    ) -> list[tuple[str, str]] | None:
        return LONG_COLUMNS.get(spec.qualified_name)

    @staticmethod
    def _effective_long_columns(
        rows: list[ResourceRow], columns: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """Choose populated columns by operational value, with an address-first rule."""

        def populated(key: str) -> bool:
            return key in {"name", "state"} or any(
                (row.details or {}).get(key) not in (None, "", "-") for row in rows
            )

        def opaque_relationship_id(key: str) -> bool:
            return key.endswith("_id") and any(
                isinstance((row.details or {}).get(key), str)
                and (row.details or {}).get(key, "").startswith("ocid1.")
                for row in rows
            )

        configured_keys = {key for key, _label in columns}
        columns = [
            *columns,
            *[
                (key, GENERIC_COLUMN_LABELS[key])
                for key in ADDRESS_COLUMN_PRIORITY
                if key not in configured_keys and populated(key)
            ],
        ]
        scored = [
            (RenderingMixin._operational_column_score(key), position, key, label)
            for position, (key, label) in enumerate(columns)
            if populated(key) and not opaque_relationship_id(key)
        ]
        selected = sorted(
            (item for item in scored if item[0] > 0),
            key=lambda item: (-item[0], item[1]),
        )[: RenderingMixin.DEFAULT_LONG_COLUMN_LIMIT]
        selected.sort(key=lambda item: (item[2] == "created", -item[0], item[1]))
        return [(key, label) for _score, _position, key, label in selected]

    @staticmethod
    def _operational_column_score(key: str) -> int:
        """Score identity, reachability, configuration, relationships, then lifecycle."""
        if key in {"name", "state", "display_name", "resource_name", "ip_address"}:
            return 100
        if key in {
            "subnet_access",
            "overall_health",
            "protocol",
            "port",
            "direction",
            "source",
            "destination",
            "domain",
            "rtype",
            "ttl",
            "rdata",
        }:
            return 90
        if (
            key in ADDRESS_COLUMN_PRIORITY
            or "cidr" in key
            or key.endswith("_ip")
            or "address" in key
        ):
            return 90
        if any(
            token in key
            for token in (
                "shape",
                "ocpu",
                "memory",
                "size",
                "vpus",
                "version",
                "policy",
                "route",
                "rule",
                "type",
                "endpoint",
                "protocol",
                "port",
                "mfa",
                "retired",
                "read_only",
                "drift",
                "percent_complete",
                "operation",
            )
        ):
            return 70
        if key in {
            "created",
            "timestamp",
            "time_finished",
            "time_expires",
            "expires_on",
            "time_drift_checked",
        }:
            return 50
        if key.endswith("_id") or key in {
            "vcn",
            "cluster",
            "view",
            "zone",
            "region",
            "availability_domain",
        }:
            return 60
        if key in {
            "description",
            "message",
            "code",
            "email",
            "hostname_label",
            "dns_label",
            "template",
            "scope",
        }:
            return 30
        return 0

    def _infer_generic_long_columns(
        self, rows: list[ResourceRow]
    ) -> list[tuple[str, str]] | None:
        if not rows or not any(row.details for row in rows):
            return None
        chosen: list[tuple[str, str]] = [("name", "Name"), ("state", "State")]
        labels = {label.casefold() for _key, label in chosen}
        for key in GENERIC_COLUMN_PRIORITY:
            label = GENERIC_COLUMN_LABELS[key]
            if label.casefold() in labels:
                continue
            if key == "description" and any(
                (row.extra or "") == (row.details or {}).get("description", "")
                for row in rows
            ):
                continue
            if any((row.details or {}).get(key) not in (None, "-", "") for row in rows):
                chosen.append((key, label))
                labels.add(label.casefold())
        if not any(col[0] == "created" for col in chosen) and any(
            (row.details or {}).get("created") for row in rows
        ):
            chosen.append(("created", "Created"))
        chosen = self._effective_long_columns(rows, chosen)
        return chosen if len(chosen) > 2 else None

    def _print_detail_table(
        self, rows: list[ResourceRow], columns: list[tuple[str, str]]
    ) -> None:
        raw_widths: dict[str, int] = {}
        for key, header in columns:
            if key == "name":
                values = [self._display_text(row.name) for row in rows]
            elif key == "state":
                values = [row.state for row in rows]
            else:
                values = [
                    self._render_detail_value(key, (row.details or {}).get(key, "-"))
                    for row in rows
                ]
            raw_widths[key] = max(len(header), *(len(str(v)) for v in values))
        widths = self._fit_column_widths(columns, raw_widths)
        print(
            "  ".join(
                self._truncate_cell(header, widths[key]).ljust(widths[key])
                for key, header in columns
            )
        )
        for row in rows:
            rendered = []
            for key, _header in columns:
                if key == "name":
                    value = self._display_text(row.name)
                elif key == "state":
                    value = row.state
                else:
                    value = self._render_detail_value(
                        key, (row.details or {}).get(key, "-")
                    )
                rendered.append(
                    self._truncate_cell(str(value), widths[key]).ljust(widths[key])
                )
            print("  ".join(rendered))

    def _print_simple_table(
        self, columns: list[tuple[str, str]], rows: list[dict[str, str]]
    ) -> None:
        if not rows:
            return
        raw_widths = {
            key: max(
                len(header),
                *(len(str(self._display_text(row.get(key, "")))) for row in rows),
            )
            for key, header in columns
        }
        widths = self._fit_column_widths(columns, raw_widths)
        print(
            "  ".join(
                self._truncate_cell(header, widths[key]).ljust(widths[key])
                for key, header in columns
            )
        )
        for row in rows:
            print(
                "  ".join(
                    self._truncate_cell(
                        str(self._display_text(row.get(key, ""))), widths[key]
                    ).ljust(widths[key])
                    for key, _header in columns
                )
            )

    def _render_detail_value(self, key: str, value: object) -> object:
        if key == "protocol" and isinstance(value, str):
            value = self._render_protocol(value)
        if (
            key in self.NAME_RESOLVED_DETAIL_KEYS
            and isinstance(value, str)
            and value.startswith("ocid1.")
        ):
            value = self.browser.resolve_name(value)
        if (
            key in {"source", "destination"}
            and isinstance(value, str)
            and value.startswith("ocid1.networksecuritygroup.")
        ):
            value = self.browser.resolve_name(value).removesuffix(f" ({value})")
        return self._display_text(value)

    def _render_protocol(self, value: str) -> str:
        label = self.PROTOCOL_LABELS.get(value)
        if label is None:
            return value
        if value == "all":
            return label
        return f"{label} ({value})"

    def _list_current_node(self, long_format: bool) -> None:
        node = self._current_node_state()
        topology = getattr(self, "topology_context", None)
        if topology is not None:
            self._list_topology_node(topology, long_format)
            return
        self._list_non_topology_node(node, long_format)

    def _list_topology_node(self, topology: object, long_format: bool) -> None:
        """Render only the topology projection branch of the namespace."""
        if topology is not None:
            if topology.level == "service":
                specs = self._topology_service_specs(topology.namespace or "")
                entries = [
                    {
                        "name": self.browser._normalize_resource_token(spec.name),
                        "type": spec.qualified_name,
                    }
                    for spec in specs
                ]
                if long_format:
                    self._print_simple_table(
                        [("name", "Name"), ("type", "Type")], entries
                    )
                else:
                    for entry in entries:
                        print(entry["name"])
                return
            if topology.level == "service-collection":
                assert topology.collection_spec is not None
                rows = self.browser.list_resources(
                    topology.collection_spec.qualified_name
                )
                self._completion_cache[self._current_path_suffix().rstrip("/")] = tuple(
                    row.name for row in rows
                )
                if long_format:
                    self._print_resources_long(topology.collection_spec, rows)
                else:
                    self._print_resources(rows)
                return
            if topology.level == "vcns":
                rows = self._topology_vcns()
                self._completion_cache[self._current_path_suffix().rstrip("/")] = tuple(
                    row.name for row in rows
                )
                if long_format:
                    self._print_resources_long(
                        self.browser.resolve_resource_spec("core.vcns"), rows
                    )
                else:
                    self._print_resources(rows)
                return
            if topology.level == "vcn":
                entries = [{"name": "subnets", "type": "topology-directory"}]
            elif topology.level == "subnets":
                assert topology.vcn is not None
                rows = self._topology_subnets(topology.vcn)
                self._completion_cache[self._current_path_suffix().rstrip("/")] = tuple(
                    row.name for row in rows
                )
                if long_format:
                    self._print_resources_long(
                        self.browser.resolve_resource_spec("core.subnets"), rows
                    )
                else:
                    self._print_resources(rows)
                return
            elif topology.level == "subnet":
                entries = [{"name": "consumers", "type": "projection-directory"}]
            else:
                assert topology.subnet is not None
                targets = self._topology_consumers(topology.subnet)
                entries = [
                    {
                        "name": self._topology_projection_name(target, targets),
                        "type": target.spec.qualified_name
                        if target.spec
                        else "resource",
                        "role": (target.row.details or {}).get("topology_role", "-"),
                        "edge": (target.row.details or {}).get("topology_kind", "-"),
                    }
                    for target in targets
                ]
                self._completion_cache[self._current_path_suffix().rstrip("/")] = tuple(
                    str(entry["name"]) for entry in entries
                )
                if long_format:
                    self._print_simple_table(
                        [
                            ("name", "Name"),
                            ("type", "Type"),
                            ("role", "Role"),
                            ("edge", "Edge"),
                        ],
                        entries,
                    )
                else:
                    for entry in entries:
                        print(entry["name"])
                return
            if long_format:
                self._print_simple_table([("name", "Name"), ("type", "Type")], entries)
            else:
                for entry in entries:
                    print(entry["name"])
            return
        return

    def _list_non_topology_node(self, node: object, long_format: bool) -> None:
        """Render non-topology namespace nodes."""
        if node.kind == "static-schema":
            entries = self.oci_schema.children(self.schema_path)
            if long_format:
                self._print_simple_table(
                    [("name", "Name"), ("type", "Type")],
                    [
                        {
                            "name": entry,
                            "type": "directory"
                            if self.oci_schema.children((*self.schema_path, entry))
                            else "file",
                        }
                        for entry in entries
                    ],
                )
            else:
                for entry in entries:
                    print(entry)
            return
        if self.mount_leaf is not None:
            return
        if node.kind == "mount-root":
            entries = self._mount_entries(self.mount_collection.name)
            if long_format:
                current = (
                    self.session_region
                    if self.mount_collection.name == "region"
                    else "-"
                )
                self._print_simple_table(
                    [("name", "Name"), ("current", "Current")],
                    [
                        {
                            "name": item,
                            "current": "*"
                            if current != "-" and item == current
                            else "-",
                        }
                        for item in entries
                    ],
                )
                return
            for item in entries:
                print(item)
            return
        if self.mount_collection is not None and node.kind not in {
            "region-anchor",
            "collection",
            "resource",
            "relationship-projection",
            "compartment",
        }:
            return
        if self.collection_context is not None and self.resource_context is None:
            self._list_collection_node(long_format)
            return
        if self.resource_context is not None:
            self._list_resource_node(long_format)
            return
        if self.namespace_view is not None:
            self._list_namespace_node(long_format)
            return
        self._list_compartment_node(long_format)

    def _list_collection_node(self, long_format: bool) -> None:
        """Render collection controls and rows."""
        if self.collection_context is not None and self.resource_context is None:
            if (
                getattr(self.collection_context.virtual_kind, "value", None)
                == "time-query"
            ):
                from config import TIME_QUERY_PROVIDERS

                provider = self.collection_context.time_query_provider or ""
                views = ["page", *TIME_QUERY_PROVIDERS[provider]["controls"]]
                options = dict(self.collection_context.time_query_options)
                required = {
                    "monitoring": {"query", "namespace"},
                    "apm": {"query"},
                    "log-analytics": {"query"},
                }.get(provider, set())
                rows = self._collection_rows() if required.issubset(options) else []
                if long_format:
                    self._print_simple_table(
                        [("name", "Name"), ("type", "Type")],
                        [
                            *({"name": name, "type": "control"} for name in views),
                            *({"name": row.name, "type": "record"} for row in rows),
                        ],
                    )
                else:
                    for name in views:
                        print(name)
                    self._print_resources(rows)
                return
            rows = self._collection_rows()
            views = (
                ["page", "name", "state"]
                if self.collection_context.virtual_kind is None
                else []
            )
            if long_format:
                if views:
                    print(f"Views: {'  '.join(f'{name}/' for name in views)}")
                self._print_resources_long(self.collection_context.spec, rows)
            else:
                for name in views:
                    print(name)
                self._print_resources(rows)
            return
        return

    def _list_compartment_node(self, long_format: bool) -> None:
        """Render direct child compartments and present root collections."""
        try:
            resource_types = self.browser.list_current_compartment_resource_types()
        except Exception as exc:
            print(f"OCI Search failed: {exc}")
            return
        children = self.browser.list_children(self.browser.current.id)
        resource_types = [
            (spec, count)
            for spec, count in resource_types
            if (
                spec.qualified_name != "identity.compartments"
                and self._resource_hierarchy.policy_for(spec).permits_flat_access
            )
        ]
        if children:
            resource_types.append(
                (
                    self.browser.resolve_resource_spec("identity.compartments"),
                    len(children),
                )
            )
        entries = [
            {
                "name": child.name,
                "kind": "compartment",
                "count": "-",
                "description": child.description or "",
            }
            for child in children
        ]
        entries.extend(
            {
                "name": spec.qualified_name.replace("_", "-"),
                "kind": "collection",
                "count": count,
                "description": "",
            }
            for spec, count in resource_types
        )
        entries.sort(key=lambda entry: str(entry["name"]).casefold())
        if long_format:
            self._print_simple_table(
                [
                    ("name", "Name"),
                    ("kind", "Kind"),
                    ("count", "Count"),
                    ("description", "Description"),
                ],
                entries,
            )
            return
        for entry in entries:
            print(entry["name"])

    def _list_resource_node(self, long_format: bool) -> None:
        """Render a resolved resource leaf without mixing collection policy."""
        entries = self._resource_entries()
        if not long_format:
            for entry in entries:
                print(entry.name)
            return
        self._print_simple_table(
            [("name", "Name"), ("type", "Type")],
            [{"name": entry.name, "type": entry.kind} for entry in entries],
        )

    def _list_namespace_node(self, long_format: bool) -> None:
        """Render only collections present in the active compartment namespace."""
        specs = self._present_namespace_specs(self.namespace_view)
        topology = self._topology_service_specs(self.namespace_view)
        if long_format:
            if topology:
                print("Views: topology/")
            self._print_resource_specs_long(specs)
            return
        if topology:
            print("topology")
        for spec in specs:
            print(self.browser._normalize_resource_token(spec.name))

    def _read_current_node(self) -> None:
        if (
            self.collection_context is not None
            and getattr(self.collection_context.virtual_kind, "value", None)
            == "object-content"
            and self.collection_context.parent_resource is not None
        ):
            print(
                self.browser.read_object_content(
                    self.collection_context.parent_resource.row
                ),
                end="",
            )
            return
        print(json.dumps(self._current_payload(), indent=2, sort_keys=True))

    @staticmethod
    def _print_resource_field(value: object) -> None:
        if isinstance(value, (dict, list)):
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            print(value)
