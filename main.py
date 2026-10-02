#!/usr/bin/env python3
from __future__ import annotations

import cmd
import json
import re
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import oci
import gnureadline as readline

# cmd.Cmd imports ``readline`` lazily inside cmdloop.  Make that import resolve
# to the same GNU Readline module configured by this shell, rather than macOS's
# libedit-backed standard-library module.
sys.modules["readline"] = readline

from completion import CompletionEngine
from config import (
    COLUMN_MINIMUMS,
    COMPARTMENT_COLLECTIONS,
    DIRECT_GETTERS,
    RESOURCE_CONTEXT_CHILDREN,
    RESOURCE_VIRTUAL_CHILDREN,
    ROOT_COLLECTION_VIEWS,
    TIME_QUERY_PROVIDERS,
    TOPOLOGY_EDGES,
    TOPOLOGY_ROOTS,
)
from deletion import DeletionManager
from inventory import ActiveRegionCatalog, OciCompartmentBrowser
from models import (
    CollectionContext,
    CompartmentNode,
    DeleteSpec,
    MountCollectionContext,
    MountEntryContext,
    MountLeafContext,
    NamespaceEntry,
    NodeState,
    RelationshipTarget,
    ResolvedNamespaceNode,
    ResourceChildCollection,
    ResourceContainerPolicy,
    ResourceContext,
    ResourceRow,
    ResourceSpec,
    TopologyContext,
    VirtualKind,
)
from relationships import RelationshipNamespace
from rendering import RenderingMixin
from static_schema import OciSdkSchemaTree


class ResourceHierarchy:
    """Separates canonical ownership from relationship projections."""

    def __init__(
        self,
        browser: OciCompartmentBrowser,
        resource_context_children: dict[str, dict[str, ResourceChildCollection]],
    ) -> None:
        parent_paths: dict[str, set[str]] = {}
        projection_paths: dict[str, set[str]] = {}
        for parent_type, children in resource_context_children.items():
            parent_spec = browser.resolve_resource_spec(parent_type)
            parent_collection = browser._normalize_resource_token(parent_spec.name)
            parent_label = self._singular(parent_collection)
            for child_name, child in children.items():
                child_spec = browser.resolve_resource_spec(child.resource_type)
                path = f"{parent_collection}/<{parent_label}>/{child_name}"
                target = projection_paths if child_spec.runnable else parent_paths
                target.setdefault(child_spec.qualified_name, set()).add(path)
        self._parent_paths = {
            key: tuple(sorted(value)) for key, value in parent_paths.items()
        }
        self._projection_paths = {
            key: tuple(sorted(value)) for key, value in projection_paths.items()
        }

    @staticmethod
    def _singular(name: str) -> str:
        if name.endswith("ies"):
            return name[:-3] + "y"
        return name.removesuffix("s")

    @staticmethod
    def _root_for(spec: ResourceSpec) -> str:
        if spec.scope in {"global", "tenancy"}:
            return "tenancy"
        if spec.scope == "region":
            return "region"
        return "compartment"

    def policy_for(self, spec: ResourceSpec) -> ResourceContainerPolicy:
        return ResourceContainerPolicy(
            root=self._root_for(spec),
            parent_paths=self._parent_paths.get(spec.qualified_name, ()),
            projection_paths=self._projection_paths.get(spec.qualified_name, ()),
        )


class ResourceFieldResolver:
    """Projects any resource payload/detail field as a read-only leaf."""

    @staticmethod
    def _normalize(name: str) -> str:
        return name.lower().replace("-", "_")

    def fields_for(self, resource: ResourceContext) -> dict[str, object]:
        fields = dict(resource.row.payload or {})
        fields.update(
            {
                key: value
                for key, value in (resource.row.details or {}).items()
                if key not in fields
            }
        )
        fields.update(
            {
                "name": resource.row.name,
                "id": resource.row.id,
                "state": resource.row.state,
                "status": resource.row.state,
            }
        )
        if resource.spec.node_capability == "metadata-record":
            fields["raw_json"] = resource.row.payload or {}
        return fields

    def resolve(self, resource: ResourceContext, name: str) -> object:
        requested = self._normalize(name)
        for key, value in self.fields_for(resource).items():
            if self._normalize(key) == requested:
                return value
        raise KeyError(name)

    def names_for(self, resource: ResourceContext) -> list[str]:
        return sorted({key.replace("_", "-") for key in self.fields_for(resource)})


class OciNavShell(RenderingMixin, cmd.Cmd):
    intro = "flocish (Fast Lean OCI SHell). Type help or ? to list commands."
    ruler = "-"
    FIND_RESULT_LIMIT = 200
    FIND_PAGE_SIZE = 100
    FIND_ALL_PAGE_SIZE = 1000
    FIND_ALL_PAGE_DELAY_SECONDS = 0.25
    DEFAULT_LONG_COLUMN_LIMIT = 6
    DELETE_WAIT_SECONDS = 120
    DELETE_WAIT_INTERVAL = 2
    TERMINAL_BLOCKER_STATES: ClassVar[frozenset[str]] = frozenset(
        {"DELETED", "TERMINATED", "CANCELED", "SUCCEEDED"}
    )
    VCN_RECURSIVE_COLLECTION_ORDER: ClassVar[tuple[str, ...]] = (
        "vlans",
        "subnets",
        "route-table-cleanup",
        "drg-attachments",
        "local-peering-gateways",
        "internet-gateways",
        "nat-gateways",
        "service-gateways",
        "network-security-groups",
        "route-tables",
        "security-lists",
        "dhcp-options",
    )
    DELETE_SPECS: ClassVar[dict[str, DeleteSpec]] = {
        "core.custom-images": DeleteSpec(
            resource_type="core.custom-images",
            client_attr="compute",
            method_name="delete_image",
            arg_name="image_id",
        ),
        "core.vcns": DeleteSpec(
            resource_type="core.vcns",
            client_attr="virtual_network",
            method_name="delete_vcn",
            arg_name="vcn_id",
            preview_note="direct delete only; no recursive teardown is performed",
            recursive_supported=True,
        ),
        "core.subnets": DeleteSpec(
            resource_type="core.subnets",
            client_attr="virtual_network",
            method_name="delete_subnet",
            arg_name="subnet_id",
        ),
        "core.drg_attachments": DeleteSpec(
            resource_type="core.drg_attachments",
            client_attr="virtual_network",
            method_name="delete_drg_attachment",
            arg_name="drg_attachment_id",
        ),
        "core.local_peering_gateways": DeleteSpec(
            resource_type="core.local_peering_gateways",
            client_attr="virtual_network",
            method_name="delete_local_peering_gateway",
            arg_name="local_peering_gateway_id",
        ),
        "core.internet_gateways": DeleteSpec(
            resource_type="core.internet_gateways",
            client_attr="virtual_network",
            method_name="delete_internet_gateway",
            arg_name="ig_id",
        ),
        "core.nat_gateways": DeleteSpec(
            resource_type="core.nat_gateways",
            client_attr="virtual_network",
            method_name="delete_nat_gateway",
            arg_name="nat_gateway_id",
        ),
        "core.service_gateways": DeleteSpec(
            resource_type="core.service_gateways",
            client_attr="virtual_network",
            method_name="delete_service_gateway",
            arg_name="service_gateway_id",
        ),
        "core.network_security_groups": DeleteSpec(
            resource_type="core.network_security_groups",
            client_attr="virtual_network",
            method_name="delete_network_security_group",
            arg_name="network_security_group_id",
        ),
        "core.route_tables": DeleteSpec(
            resource_type="core.route_tables",
            client_attr="virtual_network",
            method_name="delete_route_table",
            arg_name="rt_id",
        ),
        "core.security_lists": DeleteSpec(
            resource_type="core.security_lists",
            client_attr="virtual_network",
            method_name="delete_security_list",
            arg_name="security_list_id",
        ),
        "core.dhcp_options": DeleteSpec(
            resource_type="core.dhcp_options",
            client_attr="virtual_network",
            method_name="delete_dhcp_options",
            arg_name="dhcp_id",
        ),
        "core.vlans": DeleteSpec(
            resource_type="core.vlans",
            client_attr="virtual_network",
            method_name="delete_vlan",
            arg_name="vlan_id",
        ),
        "network_firewall.network_firewalls": DeleteSpec(
            resource_type="network_firewall.network_firewalls",
            client_attr="network_firewall",
            method_name="delete_network_firewall",
            arg_name="network_firewall_id",
        ),
    }
    PROTOCOL_LABELS: ClassVar[dict[str, str]] = {
        "1": "ICMP",
        "6": "TCP",
        "17": "UDP",
        "58": "ICMPv6",
        "all": "ALL",
    }
    NAME_RESOLVED_DETAIL_KEYS: ClassVar[set[str]] = {
        "compartment_id",
        "vcn_id",
        "subnet_id",
        "vnic_id",
        "drg_id",
        "drg_route_table_id",
        "drg_route_distribution_id",
        "cpe_id",
        "instance_id",
        "instance_pool_id",
        "dedicated_vm_host_id",
        "compute_capacity_topology_id",
        "compute_gpu_memory_cluster_id",
        "virtual_circuit_id",
        "provider_service_id",
        "public_ip_pool_id",
        "volume_group_id",
        "resolver_id",
        "view_id",
        "zone_id",
        "steering_policy_id",
        "user_id",
        "group_id",
        "tag_namespace_id",
        "network_security_group_id",
        "image_id",
        "ipsc_id",
        "cluster_id",
        "stack_id",
        "base_image",
        "volume_id",
        "boot_volume_id",
        "resource_id",
        "network_entity_id",
        "next_hop_drg_attachment_id",
        "default_route_table_id",
        "default_security_list_id",
        "ocid",
    }

    def __init__(self, browser: OciCompartmentBrowser) -> None:
        super().__init__()
        self._configure_readline_completion()
        self.browser = browser
        self.session_region = browser.region()
        self.mount_collection: MountCollectionContext | None = None
        self.mount_entry: MountEntryContext | None = None
        self.mount_leaf: MountLeafContext | None = None
        self.topology_context: TopologyContext | None = None
        self.schema_path: tuple[str, ...] = ()
        self.oci_schema = OciSdkSchemaTree(browser.resource_specs)
        self.namespace_view: str | None = None
        self.resource_context: ResourceContext | None = None
        self.collection_context: CollectionContext | None = None
        self._completion_cache: dict[str, tuple[str, ...]] = {}
        self._resource_completion_cache: dict[str, dict[str, ResourceRow]] = {}
        self._known_namespace_paths: set[str] = set()
        self._namespace_entry_cache: dict[str, tuple[NamespaceEntry, ...]] = {}
        self._terminal_record_cache: dict[str, dict[str, ResourceRow]] = {}
        self._resource_context_children_map = self._build_resource_context_children()
        self._resource_hierarchy = ResourceHierarchy(
            browser, self._resource_context_children_map
        )
        self._resource_fields = ResourceFieldResolver()
        self.relationships = RelationshipNamespace(self)
        self.catalog = ActiveRegionCatalog(browser)
        self.catalog.start()
        self.completion = CompletionEngine(self)
        self.deletion = DeletionManager(self)
        self._previous_locator: tuple[object, str] | None = None
        self._update_prompt()

    @staticmethod
    def _configure_readline_completion() -> None:
        """Keep OCI path punctuation inside one completion token."""
        delimiters = (
            readline.get_completer_delims()
            .replace(".", "")
            .replace("-", "")
            .replace(":", "")
            .replace("@", "")
            .replace("%", "")
        )
        readline.set_completer_delims(delimiters)
        readline.parse_and_bind('"\\e.": yank-last-arg')
        readline.parse_and_bind('"≥": yank-last-arg')

    def _build_resource_context_children(
        self,
    ) -> dict[str, dict[str, ResourceChildCollection]]:
        result: dict[str, dict[str, ResourceChildCollection]] = {}
        for parent, children in RESOURCE_CONTEXT_CHILDREN.items():
            try:
                self.browser.resolve_resource_spec(parent)
            except ValueError:
                continue
            supported_children: dict[str, ResourceChildCollection] = {}
            for name, (resource_type, api_kwargs, row_filter_key) in children.items():
                try:
                    self.browser.resolve_resource_spec(resource_type)
                except ValueError:
                    continue
                supported_children[name] = ResourceChildCollection(
                    resource_type=resource_type,
                    api_kwargs=api_kwargs,
                    row_filter_key=row_filter_key,
                )
            if supported_children:
                result[parent] = supported_children
        return result

    def _update_prompt(self) -> None:
        if self.mount_collection is not None and self.mount_collection.name == "oci":
            suffix = "/".join(getattr(self, "schema_path", ()))
            self.prompt = f"/oci{('/' + suffix) if suffix else ''} $ "
            return
        nodes = [self.browser.root, *self.browser.parents, self.browser.current]
        names: list[str] = []
        seen: set[str] = set()
        for node in nodes:
            if node.id in seen:
                continue
            seen.add(node.id)
            names.append(node.name)
        suffix = self._current_path_suffix().strip("/")
        current_scope = "/".join(names)
        remainder = suffix.removeprefix(current_scope).strip("/")
        scope = "/".join(
            [self._effective_region(), *names, *([remainder] if remainder else [])]
        )
        self.prompt = f"/{scope} $ "

    def _region_view_name(self) -> str | None:
        if (
            self.mount_collection is None
            or self.mount_collection.name != "region"
            or self.mount_entry is None
        ):
            return None
        return self.mount_entry.name

    def _effective_region(self) -> str:
        return self._region_view_name() or self.session_region

    def _sync_browser_region(self) -> None:
        target = self._effective_region()
        if self.browser.region() != target:
            self.browser.set_region(target)

    def _request_current_compartment_catalog(self) -> None:
        """Schedule active-compartment completion data when a catalog is running."""
        catalog = getattr(self, "catalog", None)
        browser = getattr(self, "browser", None)
        current = getattr(browser, "current", None)
        if catalog is not None and current is not None:
            catalog.request_compartment(current.id)

    def _prime_current_compartment_completion(self) -> None:
        """Populate the current-compartment cache before returning from ``cd``.

        Completion itself remains read-only against local snapshots.  A catalog
        refresh can lag behind navigation, so ``cd`` eagerly creates the same
        per-compartment type cache that ``ll`` uses.
        """
        browser = getattr(self, "browser", None)
        loader = getattr(browser, "list_current_compartment_resource_types", None)
        if loader is not None:
            try:
                loader()
            except Exception:
                # Navigation must remain usable when inventory is unavailable.
                pass
        self._request_current_compartment_catalog()

    def _known_compartment_collections(self, compartment_id: str) -> tuple[str, ...]:
        """Return live collection types from the catalog, falling back to its cache."""
        catalog = getattr(self, "catalog", None)
        if catalog is not None and catalog.is_ready():
            collections = catalog.collections_for(compartment_id)
            if collections:
                return collections
        try:
            cache_key = (self.browser.region(), compartment_id)
        except AttributeError:
            return ()
        cached = getattr(self.browser, "compartment_resource_type_cache", {}).get(
            cache_key
        )
        return (
            tuple(spec.qualified_name for spec, _count in cached[1])
            if cached is not None
            else ()
        )

    def _relative_browser_path(self) -> str:
        path = self.browser.get_path()
        root_path = "/" + self.browser.root.name
        if path == root_path:
            return ""
        if path.startswith(root_path + "/"):
            return path[len(root_path) :]
        return path

    def _namespace_slug(self, namespace: str) -> str:
        resolver = getattr(self.browser, "namespace_path_slug", None)
        return (
            resolver(namespace) if resolver is not None else namespace.replace("_", "-")
        )

    def _compose_suffix(self, base_path: str) -> str:
        path = base_path or ""
        if self.namespace_view is not None:
            namespace_slug = self._namespace_slug(self.namespace_view)
            path = f"{path}/{namespace_slug}" if path else f"/{namespace_slug}"
        if self.collection_context is None and self.resource_context is None:
            return path or "/"
        if (
            self.collection_context is not None
            and self.collection_context.parent_resource is not None
        ):
            if (
                self.collection_context.virtual_kind
                in {VirtualKind.TIME_QUERY, VirtualKind.TIME_QUERY_SELECTOR}
                and self.collection_context.parent_collection is not None
            ):
                parent = self.collection_context.parent_collection
                parent_collection = self.browser._normalize_resource_token(
                    parent.parent_resource.spec.name
                )
                parent_base = (
                    f"{path}/{parent_collection}/{parent.parent_resource.row.name}/{parent.collection_name}"
                    if path
                    else f"/{parent_collection}/{parent.parent_resource.row.name}/{parent.collection_name}"
                )
                entry_path = "/".join(
                    self.collection_context.time_query_path
                    or (self.collection_context.collection_name,)
                )
                return f"{parent_base}/{self.collection_context.parent_resource.row.name}/{entry_path}"
            if self.collection_context.parent_collection is not None:
                parent = self.collection_context.parent_collection
                parent_collection = self.browser._normalize_resource_token(
                    parent.parent_resource.spec.name
                )
                parent_base = (
                    f"{path}/{parent_collection}/{parent.parent_resource.row.name}/{parent.collection_name}"
                    if path
                    else f"/{parent_collection}/{parent.parent_resource.row.name}/{parent.collection_name}"
                )
                base = f"{parent_base}/{self.collection_context.parent_resource.row.name}/{self.collection_context.collection_name}"
                if self.resource_context is not None:
                    return f"{base}/{self.resource_context.row.name}"
                return base
            parent_collection = self.browser._normalize_resource_token(
                self.collection_context.parent_resource.spec.name
            )
            base = (
                f"{path}/{parent_collection}/{self.collection_context.parent_resource.row.name}/{self.collection_context.collection_name}"
                if path
                else f"/{parent_collection}/{self.collection_context.parent_resource.row.name}/{self.collection_context.collection_name}"
            )
            if self.resource_context is not None:
                return f"{base}/{self.resource_context.row.name}"
            return base
        if self.collection_context is not None:
            base = (
                f"{path}/{self.collection_context.collection_name}"
                if path
                else f"/{self.collection_context.collection_name}"
            )
            if self.collection_context.collection_view_path:
                base = (
                    f"{base}/{'/'.join(self.collection_context.collection_view_path)}"
                )
            if self.resource_context is not None:
                return f"{base}/{self.resource_context.row.name}"
            return base
        collection_name = self.browser._normalize_resource_token(
            self.resource_context.spec.name
        )
        base = f"{path}/{collection_name}" if path else f"/{collection_name}"
        return f"{base}/{self.resource_context.row.name}"

    def _current_node_state(self) -> NodeState:
        if getattr(self, "topology_context", None) is not None:
            return NodeState(kind="topology", path_suffix=self._topology_path_suffix())
        if self.mount_collection is not None and self.mount_collection.name == "oci":
            return NodeState(
                kind="static-schema", path_suffix="/oci/" + "/".join(self.schema_path)
            )
        if self.mount_leaf is not None:
            return NodeState(
                kind="mount-leaf",
                path_suffix=f"/{self.mount_collection.name}/{self.mount_leaf.entry}/{self.mount_leaf.name}",
            )
        if self.mount_collection is not None and self.mount_entry is None:
            return NodeState(
                kind="mount-root",
                path_suffix=f"/{self.mount_collection.name}",
            )
        if (
            self.mount_collection is not None
            and self.mount_entry is not None
            and self.mount_entry.collection == "region"
            and self._relative_browser_path() == ""
            and self.collection_context is None
            and self.resource_context is None
        ):
            return NodeState(kind="region-anchor", path_suffix=self.browser.get_path())
        if (
            self.mount_collection is not None
            and self.mount_collection.name == "catalog"
            and self.mount_entry is not None
        ):
            base = f"{self.browser.get_path()}/catalog/{self.mount_entry.name}"
            if self.resource_context is not None:
                return NodeState(
                    kind="resource",
                    path_suffix=f"{base}/{self.resource_context.row.name}",
                )
            return NodeState(kind="collection", path_suffix=base)
        if (
            self.resource_context is not None
            and self.collection_context is not None
            and self.collection_context.parent_resource is not None
            and self.collection_context.virtual_kind
            in {VirtualKind.RELATIONSHIPS, VirtualKind.RELATED_LOGS}
        ):
            return NodeState(
                kind="relationship-projection",
                path_suffix=self._compose_suffix(self.browser.get_path()),
            )
        if self.collection_context is not None and self.resource_context is None:
            base = self._compose_suffix(self.browser.get_path())
            return NodeState(kind="collection", path_suffix=base)
        if self.resource_context is not None:
            base = self._compose_suffix(self.browser.get_path())
            return NodeState(kind="resource", path_suffix=base)
        if self.namespace_view is not None:
            return NodeState(
                kind="domain", path_suffix=self._compose_suffix(self.browser.get_path())
            )
        return NodeState(kind="compartment", path_suffix=self.browser.get_path())

    def _current_path_suffix(self) -> str:
        return self._current_node_state().path_suffix

    def _current_locator(self) -> str:
        region = self._effective_region()
        return f"oci:{self._current_path_suffix()}@{region}"

    def _resource_context_children(self) -> dict[str, ResourceChildCollection]:
        if self.resource_context is None:
            return {}
        return self._resource_context_children_map.get(
            self.resource_context.spec.qualified_name, {}
        )

    def _root_mounts(self) -> list[str]:
        return ["catalog", "oci", "region", "topology"]

    def _mount_entries(self, name: str) -> list[str]:
        if name == "topology":
            return ["vcns"]
        if name == "oci":
            return self.oci_schema.children(self.schema_path)
        if name == "catalog":
            return [
                spec.qualified_name.replace("_", "-")
                for spec in self.browser.resource_specs
                if spec.scope in {"region", "regional-catalog"}
            ]
        if name == "region":
            return [
                row.name
                for row in self.browser.list_resources("identity.region-subscriptions")
            ]
        raise ValueError(f"unknown mount: {name}")

    @staticmethod
    def _topology_value(row: ResourceRow, field: str) -> object:
        """Read a declared edge field without service-specific navigation code."""
        value: object = row.payload or {}
        for part in field.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
        if value is None and "." not in field:
            value = (row.details or {}).get(field)
        return value

    def _topology_path_suffix(self) -> str:
        current = self.topology_context
        assert current is not None
        if current.namespace is not None:
            parts = [
                *self.browser.get_path().strip("/").split("/"),
                self._namespace_slug(current.namespace),
                "topology",
            ]
            if current.level == "service":
                return "/" + "/".join(parts)
            if (
                current.level == "service-collection"
                and current.collection_spec is not None
            ):
                parts.append(
                    self.browser._normalize_resource_token(current.collection_spec.name)
                )
                return "/" + "/".join(parts)
        elif current.embedded:
            assert current.vcn is not None
            parts = [
                *self.browser.get_path().strip("/").split("/"),
                self._namespace_slug("core"),
                "vcns",
                self.browser._sanitize_row_name(current.vcn.name),
                "topology",
            ]
        elif current.namespace is None:
            parts = ["topology"]
        if not current.embedded:
            if current.level in {"vcns", "vcn", "subnets", "subnet", "consumers"}:
                parts.append("vcns")
            if current.vcn is not None:
                parts.append(self.browser._sanitize_row_name(current.vcn.name))
        if current.level in {"subnets", "subnet", "consumers"}:
            parts.append("subnets")
        if current.subnet is not None:
            parts.append(self.browser._sanitize_row_name(current.subnet.name))
        if current.level == "consumers":
            parts.append("consumers")
        return "/" + "/".join(parts)

    def _topology_service_specs(self, namespace: str) -> list[ResourceSpec]:
        return [
            self.browser.resolve_resource_spec(resource_type)
            for resource_type in TOPOLOGY_ROOTS.get(namespace, ())
        ]

    def _topology_vcns(self) -> list[ResourceRow]:
        return self.browser.list_resources("core.vcns")

    def _topology_subnets(self, vcn: ResourceRow) -> list[ResourceRow]:
        return self.browser.list_resources(
            "core.subnets", extra_kwargs={"vcn_id": vcn.id}
        )

    def _topology_consumers(self, subnet: ResourceRow) -> list[RelationshipTarget]:
        """Return projections whose declared topology edge terminates at subnet.

        The registry is intentionally the only service-specific input.  A
        direct edge compares its declared field; a derived edge declares the
        intermediate collection and its source/target fields.
        """
        targets: list[RelationshipTarget] = []
        for resource_type, edges in TOPOLOGY_EDGES.items():
            try:
                spec = self.browser.resolve_resource_spec(resource_type)
                rows = self.browser.list_resources(resource_type)
            except Exception:
                continue
            for edge in edges:
                if edge.get("target") != "core.subnets":
                    continue
                kind = edge.get("kind")
                if kind == "direct":
                    for row in rows:
                        value = self._topology_value(row, str(edge["field"]))
                        if value is None:
                            try:
                                row = self.browser.hydrate_resource_row(row)
                                value = self._topology_value(row, str(edge["field"]))
                            except Exception:
                                # The projection remains bounded: a service
                                # without a usable detail getter simply does
                                # not claim an unverified topology edge.
                                continue
                        values = value if isinstance(value, list) else [value]
                        if subnet.id in values:
                            details = {
                                **(row.details or {}),
                                "topology_role": str(edge["role"]),
                                "topology_kind": "direct",
                            }
                            targets.append(
                                RelationshipTarget(
                                    spec,
                                    row.with_details(details),
                                    self.browser.current.id,
                                )
                            )
                elif kind == "derived":
                    via = str(edge.get("via") or "")
                    source_field = str(edge.get("source_field") or "")
                    target_field = str(edge.get("target_field") or "")
                    if not via or not source_field or not target_field:
                        continue
                    try:
                        via_rows = self.browser.list_resources(via)
                    except Exception:
                        continue
                    source_ids = {
                        str(self._topology_value(row, source_field))
                        for row in via_rows
                        if self._topology_value(row, target_field) == subnet.id
                    }
                    for row in rows:
                        if row.id not in source_ids:
                            continue
                        details = {
                            **(row.details or {}),
                            "topology_role": str(edge["role"]),
                            "topology_kind": "derived",
                        }
                        targets.append(
                            RelationshipTarget(
                                spec, row.with_details(details), self.browser.current.id
                            )
                        )
        return sorted(
            targets,
            key=lambda item: (
                item.row.name.casefold(),
                item.spec.qualified_name if item.spec else "",
            ),
        )

    def _topology_projection_name(
        self, target: RelationshipTarget, all_targets: list[RelationshipTarget]
    ) -> str:
        duplicate = sum(item.row.name == target.row.name for item in all_targets) > 1
        return (
            f"{target.row.name}@{target.spec.qualified_name.replace('.', '-').replace('_', '-')}"
            if duplicate and target.spec is not None
            else target.row.name
        )

    def _is_subscribed_region(self, name: str) -> bool:
        """Resolve a region from OCI's tenancy subscriptions, never a static list."""
        return name in self._mount_entries("region")

    def _mount_entry_payload(self, collection: str, name: str) -> dict[str, object]:
        if collection == "region":
            rows = self.browser.list_resources("region-subscriptions")
            row = next((item for item in rows if item.name == name), None)
            if row is None:
                raise ValueError(f"{collection} not found: {name}")
            payload = dict(row.payload or {})
            payload["kind"] = "region"
            payload["active"] = name == self.session_region
            return payload
        raise ValueError(f"unknown mount: {collection}")

    def _current_mount_payload(self) -> dict[str, object]:
        if self.mount_collection is None:
            raise ValueError("no mount context")
        if self.mount_entry is not None:
            return self._mount_entry_payload(
                self.mount_entry.collection, self.mount_entry.name
            )
        entries = self._mount_entries(self.mount_collection.name)
        if self.mount_collection.name == "region":
            current = self.session_region
        else:
            current = None
        return {
            "kind": "collection",
            "name": self.mount_collection.name,
            "count": len(entries),
            "current": current,
        }

    def _collection_rows(self) -> list[ResourceRow]:
        if self.collection_context is None:
            return []
        listing: dict[str, object] = {"state": "virtual"}
        if self.collection_context.virtual_kind == VirtualKind.RELATIONSHIPS:
            rows = [
                target.row for target in self.collection_context.relationship_targets
            ]
        elif self.collection_context.virtual_kind == VirtualKind.TENANCY_LOG_GROUPS:
            rows = self.browser.list_tenancy_log_groups()
        elif self.collection_context.virtual_kind == VirtualKind.TIME_QUERY:
            rows = self.browser.list_time_query_entries(
                self.collection_context.time_query_provider or "",
                (
                    self.collection_context.parent_resource.row
                    if self.collection_context.parent_resource is not None
                    else None
                ),
                **dict(self.collection_context.time_query_options),
            )
        elif (
            self.collection_context.virtual_kind == VirtualKind.OBJECT_PREFIXES
            and self.collection_context.parent_resource is not None
        ):
            rows = self.browser.list_object_prefix(
                self.collection_context.parent_resource.row,
                self.collection_context.content_prefix,
            )
        elif self.collection_context.virtual_kind == VirtualKind.OBJECT_CONTENT:
            rows = []
        elif (
            self.collection_context.virtual_kind == VirtualKind.BLOCKERS
            and self.collection_context.parent_resource is not None
        ):
            rows = self._subnet_service_vnic_rows(
                self.collection_context.parent_resource
            )
        elif (
            self.collection_context.virtual_kind == VirtualKind.RELATED_LOGS
            and self.collection_context.parent_resource is not None
        ):
            rows = self.browser.list_related_logs(
                self.collection_context.parent_resource.row.id
            )
        elif (
            self.collection_context.projection_field
            and self.collection_context.parent_resource is not None
        ):
            rows = self.browser.list_relationship_projection(
                self.collection_context.spec,
                self.collection_context.projection_field,
                self.collection_context.parent_resource.row.id,
            )
        else:
            options = dict(self.collection_context.collection_view_options)
            try:
                rows, listing = self.browser.list_resource_page(
                    self.collection_context.spec,
                    page_number=int(options.get("page", 1)),
                    extra_kwargs=dict(self.collection_context.extra_kwargs),
                    row_filter=self.collection_context.row_filter,
                )
            except AttributeError:
                rows = self.browser.list_resources(
                    self.collection_context.spec.qualified_name,
                    extra_kwargs=dict(self.collection_context.extra_kwargs),
                    row_filter=self.collection_context.row_filter,
                )
                listing = {"state": "provider-unpaged", "returned": len(rows)}
            except Exception as exc:
                if not hasattr(self, "_collection_listing_state"):
                    self._collection_listing_state = {}
                status = getattr(exc, "status", None)
                self._collection_listing_state[
                    self._current_path_suffix().rstrip("/")
                ] = {
                    "state": "permission-limited" if status in {401, 403} else "error",
                    "error": str(exc),
                }
                raise
            if prefix := options.get("name"):
                rows = [
                    row
                    for row in rows
                    if row.name.casefold().startswith(str(prefix).casefold())
                ]
            if state := options.get("state"):
                rows = [
                    row for row in rows if row.state.casefold() == str(state).casefold()
                ]
        inherited = dict(self.collection_context.extra_kwargs)
        if inherited:
            rows = [
                ResourceRow(
                    name=row.name,
                    state=row.state,
                    id=row.id,
                    extra=row.extra,
                    details={**inherited, **(row.details or {})},
                    payload={**inherited, **(row.payload or {})},
                )
                for row in rows
            ]
        self._completion_cache[self._current_path_suffix().rstrip("/")] = tuple(
            row.name for row in rows
        )
        self._resource_completion_cache[self._current_path_suffix().rstrip("/")] = {
            row.name: row for row in rows
        }
        if not hasattr(self, "_terminal_record_cache"):
            self._terminal_record_cache = {}
        self._terminal_record_cache[self._current_path_suffix().rstrip("/")] = {
            row.name: row
            for row in rows
            if not self._row_is_navigable(self.collection_context.spec, row)
        }
        if not hasattr(self, "_collection_listing_state"):
            self._collection_listing_state = {}
        self._collection_listing_state[self._current_path_suffix().rstrip("/")] = {
            **listing,
            "returned": len(rows),
            "filters": dict(self.collection_context.collection_view_options),
        }
        return rows

    def _enter_collection_context(
        self,
        spec: ResourceSpec,
        collection_name: str,
        extra_kwargs: dict[str, str] | None = None,
        row_filter: tuple[str, str] | None = None,
        parent_resource: ResourceContext | None = None,
        parent_collection: CollectionContext | None = None,
        projection_field: str | None = None,
        virtual_kind: VirtualKind | str | None = None,
        relationship_targets: tuple[RelationshipTarget, ...] = (),
        time_query_provider: str | None = None,
        time_query_options: dict[str, object] | None = None,
        time_query_path: tuple[str, ...] = (),
        time_query_pending: str | None = None,
        collection_view_options: dict[str, object] | None = None,
        collection_view_path: tuple[str, ...] = (),
        collection_view_pending: str | None = None,
        content_prefix: str = "",
        previous_collection: CollectionContext | None = None,
    ) -> None:
        self.collection_context = CollectionContext(
            spec=spec,
            collection_name=collection_name,
            extra_kwargs=tuple(sorted((extra_kwargs or {}).items())),
            row_filter=row_filter,
            parent_resource=parent_resource,
            parent_collection=parent_collection,
            projection_field=projection_field,
            virtual_kind=(
                VirtualKind(virtual_kind) if virtual_kind is not None else None
            ),
            relationship_targets=relationship_targets,
            time_query_provider=time_query_provider,
            time_query_options=tuple(sorted((time_query_options or {}).items())),
            time_query_path=time_query_path,
            time_query_pending=time_query_pending,
            collection_view_options=tuple(
                sorted((collection_view_options or {}).items())
            ),
            collection_view_path=collection_view_path,
            collection_view_pending=collection_view_pending,
            content_prefix=content_prefix,
            previous_collection=previous_collection,
        )
        self.resource_context = None

    def _try_enter_collection_context(self, target: str) -> bool:
        normalized = self.browser._normalize_resource_token(target)
        if (
            self.resource_context is None
            and self.collection_context is None
            and self.namespace_view is not None
            and normalized == "topology"
        ):
            self.topology_context = TopologyContext(
                level="service", namespace=self.namespace_view
            )
            return True
        if self.resource_context is not None:
            children = self._resource_context_children()
            if (
                self.resource_context.spec.qualified_name == "core.vcns"
                and normalized == "topology"
            ):
                self.topology_context = TopologyContext(
                    level="subnets", vcn=self.resource_context.row, embedded=True
                )
                return True
            if (
                self.resource_context.spec.qualified_name == "core.subnets"
                and normalized == "blockers"
            ):
                self._enter_collection_context(
                    self.resource_context.spec,
                    "blockers",
                    parent_resource=self.resource_context,
                    virtual_kind=VirtualKind.BLOCKERS,
                )
                return True
            if normalized in children:
                collection = children[normalized]
                spec = self.browser.resolve_resource_spec(collection.resource_type)
                row_filter = None
                if collection.row_filter_key:
                    row_filter = (
                        collection.row_filter_key,
                        self.resource_context.row.id,
                    )
                self._enter_collection_context(
                    spec,
                    normalized,
                    extra_kwargs=self._resource_context_kwargs(collection),
                    row_filter=row_filter,
                    parent_resource=self.resource_context,
                    # Explicit child API arguments (for example vcn_id) are
                    # authoritative.  Search projections are only for child
                    # collections that have no direct list scope.
                    projection_field=(
                        collection.projection_field
                        if spec.runnable and not collection.api_kwargs
                        else None
                    ),
                )
                return True
            if normalized == "relationships":
                view = self._relationship_namespace().view(self.resource_context)
                targets_by_id = {
                    item.row.id: item
                    for item in (
                        *(target for _name, target in view.links),
                        *(
                            target
                            for group in view.collections.values()
                            for target in group
                        ),
                    )
                }
                targets = tuple(targets_by_id.values())
                if not targets:
                    return False
                spec = next(
                    target.spec for target in targets if target.spec is not None
                )
                self._enter_collection_context(
                    spec,
                    "relationships",
                    parent_resource=self.resource_context,
                    virtual_kind=VirtualKind.RELATIONSHIPS,
                    relationship_targets=targets,
                )
                return True
            relationship_targets = self._relationship_collections().get(normalized)
            if relationship_targets:
                spec = next(
                    target.spec
                    for target in relationship_targets
                    if target.spec is not None
                )
                self._enter_collection_context(
                    spec,
                    normalized,
                    parent_resource=self.resource_context,
                    virtual_kind=VirtualKind.RELATIONSHIPS,
                    relationship_targets=relationship_targets,
                )
                return True
            virtual_children = {
                **RESOURCE_VIRTUAL_CHILDREN.get("default", {}),
                **RESOURCE_VIRTUAL_CHILDREN.get(
                    self.resource_context.spec.qualified_name, {}
                ),
            }
            if normalized in virtual_children:
                rule = virtual_children[normalized]
                if rule is None:
                    return False
                kind = str(rule["kind"])
                spec = (
                    self.browser.resolve_resource_spec(str(rule["resource_type"]))
                    if kind == "related-logs"
                    else self.resource_context.spec
                )
                self._enter_collection_context(
                    spec,
                    normalized,
                    parent_resource=self.resource_context,
                    parent_collection=(
                        self.collection_context
                        if kind == VirtualKind.TIME_QUERY.value
                        else None
                    ),
                    virtual_kind=kind,
                    time_query_provider=(
                        str(rule["provider"])
                        if kind == VirtualKind.TIME_QUERY.value
                        else None
                    ),
                    time_query_path=(
                        (normalized,) if kind == VirtualKind.TIME_QUERY.value else ()
                    ),
                    content_prefix="",
                )
                return True
            return False
        if (
            self.collection_context is not None
            and self.collection_context.virtual_kind
            and self.collection_context.virtual_kind
            in {VirtualKind.TIME_QUERY, VirtualKind.TIME_QUERY_SELECTOR}
        ):
            current = self.collection_context
            provider = current.time_query_provider or ""
            controls = set(TIME_QUERY_PROVIDERS[provider]["controls"])
            options = dict(current.time_query_options)
            path = current.time_query_path
            pending = current.time_query_pending
            if pending is not None:
                if pending == "page":
                    try:
                        options["page_number"] = int(target)
                    except ValueError:
                        raise ValueError("query page must be an integer") from None
                elif pending == "where-field":
                    self._enter_collection_context(
                        current.spec,
                        target,
                        parent_resource=current.parent_resource,
                        parent_collection=current.parent_collection,
                        virtual_kind=VirtualKind.TIME_QUERY_SELECTOR,
                        time_query_provider=provider,
                        time_query_options=options,
                        time_query_path=(*path, target),
                        time_query_pending=f"where-value:{target}",
                        previous_collection=current,
                    )
                    return True
                elif pending.startswith("where-value:"):
                    options["where"] = (
                        f"{pending.removeprefix('where-value:')}={target}"
                    )
                else:
                    options[pending] = target
                self._enter_collection_context(
                    current.spec,
                    target,
                    parent_resource=current.parent_resource,
                    parent_collection=current.parent_collection,
                    virtual_kind=VirtualKind.TIME_QUERY,
                    time_query_provider=provider,
                    time_query_options=options,
                    time_query_path=(*path, target),
                    previous_collection=current,
                )
                return True
            if target == "page" or target in controls:
                self._enter_collection_context(
                    current.spec,
                    target,
                    parent_resource=current.parent_resource,
                    parent_collection=current.parent_collection,
                    virtual_kind=VirtualKind.TIME_QUERY_SELECTOR,
                    time_query_provider=provider,
                    time_query_options=options,
                    time_query_path=(*path, target),
                    time_query_pending=("where-field" if target == "where" else target),
                    previous_collection=current,
                )
                return True
        try:
            resource_type = (
                f"{self.namespace_view}.{target}"
                if self.namespace_view is not None and "." not in target
                else target
            )
            spec = self.browser.resolve_resource_spec(resource_type)
        except ValueError:
            return False
        # A locator may use an alias (core.instances, instance, underscores),
        # but its persisted path always uses the canonical collection name.
        normalized = self.browser._normalize_resource_token(spec.name)
        # Compatibility names such as `instance` still enter the canonical
        # service mount.  The prompt must never hide that navigation state.
        if self.namespace_view is None:
            self.namespace_view = spec.namespace
        if self.resource_context is None and self.collection_context is None:
            policy = self._resource_hierarchy.policy_for(spec)
            if not policy.permits_flat_access:
                paths = " or ".join(policy.parent_paths)
                raise ValueError(
                    f"{spec.qualified_name} is contained by {paths}; flat access is disabled"
                )
        if not spec.runnable and spec.lister_name is None:
            raise ValueError(
                f"{spec.qualified_name} requires a parent resource context; "
                f"enter its parent resource first and then list {normalized}"
            )
        root_view = ROOT_COLLECTION_VIEWS.get(spec.qualified_name)
        tenancy_root_view = root_view is not None and (
            self.browser.current.id == self.browser.root.id
            or str(root_view.get("kind")) == VirtualKind.TIME_QUERY.value
        )
        self._enter_collection_context(
            spec,
            normalized,
            virtual_kind=(str(root_view["kind"]) if tenancy_root_view else None),
            time_query_provider=(
                str(root_view["provider"])
                if root_view is not None
                and str(root_view.get("kind")) == VirtualKind.TIME_QUERY.value
                else None
            ),
        )
        return True

    def _try_enter_resource_from_collection(self, target: str) -> bool:
        if self.collection_context is None:
            return False
        current = self.collection_context
        if current.virtual_kind is None:
            options = dict(current.collection_view_options)
            pending = current.collection_view_pending
            if pending is not None:
                if pending == "page":
                    try:
                        value: object = int(target)
                    except ValueError:
                        raise ValueError("collection page must be an integer") from None
                    if not 1 <= value <= 20:
                        raise ValueError("collection page must be between 1 and 20")
                else:
                    value = target
                options[pending] = value
                self._enter_collection_context(
                    current.spec,
                    current.collection_name,
                    extra_kwargs=dict(current.extra_kwargs),
                    row_filter=current.row_filter,
                    parent_resource=current.parent_resource,
                    parent_collection=current.parent_collection,
                    projection_field=current.projection_field,
                    collection_view_options=options,
                    collection_view_path=(*current.collection_view_path, target),
                )
                return True
            if target in {"page", "name", "state"}:
                self._enter_collection_context(
                    current.spec,
                    current.collection_name,
                    extra_kwargs=dict(current.extra_kwargs),
                    row_filter=current.row_filter,
                    parent_resource=current.parent_resource,
                    parent_collection=current.parent_collection,
                    projection_field=current.projection_field,
                    collection_view_options=options,
                    collection_view_path=(*current.collection_view_path, target),
                    collection_view_pending=target,
                )
                return True
        if target.startswith("ocid1."):
            resolved = self.browser.resolve_resource_id(
                self.collection_context.spec, target
            )
            if (
                self.collection_context.spec.scope != "region"
                and resolved.compartment_id != self.browser.current.id
            ):
                raise ValueError(
                    f"{target} is not in compartment {self.browser.current.name}"
                )
            self.resource_context = ResourceContext(
                spec=self.collection_context.spec,
                row=resolved.row,
            )
            return True
        matches = self.browser._matching_rows(self._collection_rows(), target)
        if len(matches) == 1:
            if (
                self.collection_context.spec.qualified_name == "object_storage.buckets"
                and self.collection_context.virtual_kind != VirtualKind.OBJECT_PREFIXES
            ):
                self.resource_context = ResourceContext(
                    spec=self.collection_context.spec, row=matches[0]
                )
                return True
            if self.collection_context.virtual_kind == VirtualKind.OBJECT_PREFIXES:
                row = matches[0]
                if (row.details or {}).get("object_directory") == "true":
                    self._enter_collection_context(
                        self.collection_context.spec,
                        row.name,
                        parent_resource=self.collection_context.parent_resource,
                        virtual_kind=VirtualKind.OBJECT_PREFIXES,
                        content_prefix=str(
                            (row.details or {}).get("object_prefix") or ""
                        ),
                        previous_collection=self.collection_context,
                    )
                    return True
                self.resource_context = ResourceContext(
                    spec=self.collection_context.spec, row=row
                )
                return True
            if (
                not self._row_is_navigable(self.collection_context.spec, matches[0])
                and self.collection_context.spec.qualified_name
                not in COMPARTMENT_COLLECTIONS
            ):
                raise ValueError(
                    f"{self.collection_context.collection_name} records are terminal; "
                    f"use cat {target} to inspect one"
                )
            row = matches[0]
            if self.collection_context.spec.qualified_name in COMPARTMENT_COLLECTIONS:
                self.browser.change_to_compartment(row.id)
                self.namespace_view = None
                self.collection_context = None
                self.resource_context = None
                return True
            if self.collection_context.virtual_kind == VirtualKind.RELATIONSHIPS:
                relationship = next(
                    target
                    for target in self.collection_context.relationship_targets
                    if target.row.id == row.id
                )
                self.browser.change_to_compartment(relationship.compartment_id)
                self.namespace_view = relationship.spec.namespace
                self.collection_context = CollectionContext(
                    spec=relationship.spec,
                    collection_name=self.browser._normalize_resource_token(
                        relationship.spec.name
                    ),
                )
                self.resource_context = ResourceContext(
                    spec=relationship.spec, row=relationship.row
                )
                return True
            if self.collection_context.virtual_kind == VirtualKind.TENANCY_LOG_GROUPS:
                compartment_id = (row.details or {}).get("compartment_id")
                if isinstance(compartment_id, str) and compartment_id != "-":
                    self.browser.change_to_compartment(compartment_id)
                # The global index is only an entry point.  Once a group is
                # selected, return to its compartment-owned canonical path.
                self.collection_context = CollectionContext(
                    spec=self.collection_context.spec,
                    collection_name=self.collection_context.collection_name,
                )
            if self.collection_context.projection_field:
                row = self.browser.hydrate_resource_row(row)
            self.resource_context = ResourceContext(
                spec=self.collection_context.spec,
                row=row,
            )
            return True
        if not matches:
            raise ValueError(
                f"{self.collection_context.collection_name} not found: {target}"
            )
        raise ValueError(
            self.browser._ambiguous_row_error(
                self.collection_context.collection_name, target, matches
            )
        )

    @staticmethod
    def _row_is_navigable(spec: ResourceSpec, row: ResourceRow) -> bool:
        if spec.node_capability == "metadata-record":
            return bool(row.payload)
        payload = row.payload or {}
        payload_id = payload.get("id")
        match = re.match(r"^ocid1\.([^.]+)\.", row.id)
        return (
            spec.node_capability == "navigable-resource"
            and isinstance(payload_id, str)
            and payload_id == row.id
            and match is not None
            # A direct getter is needed to hydrate a leaf resource, but a
            # resource with declared child collections is navigable without
            # one.  Logging log groups are the important example: OCI lists
            # them, and their logs child is addressed with log_group_id.
            and (
                match.group(1) in DIRECT_GETTERS
                or spec.qualified_name in RESOURCE_CONTEXT_CHILDREN
                or spec.qualified_name in RESOURCE_VIRTUAL_CHILDREN
            )
        )

    def _step_up(self) -> None:
        if getattr(self, "topology_context", None) is not None:
            current = self.topology_context
            if current.level == "service-collection":
                self.topology_context = TopologyContext(
                    "service", namespace=current.namespace
                )
            elif current.level == "service":
                self.topology_context = None
            elif current.level == "consumers":
                self.topology_context = TopologyContext(
                    "subnet",
                    current.vcn,
                    current.subnet,
                    current.embedded,
                    current.namespace,
                )
            elif current.level == "subnet":
                self.topology_context = TopologyContext(
                    "subnets",
                    current.vcn,
                    embedded=current.embedded,
                    namespace=current.namespace,
                )
            elif current.level == "subnets":
                self.topology_context = (
                    None
                    if current.embedded
                    else TopologyContext(
                        "vcn", current.vcn, namespace=current.namespace
                    )
                )
            elif current.level == "vcn":
                self.topology_context = (
                    TopologyContext(
                        "service-collection",
                        namespace=current.namespace,
                        collection_spec=self.browser.resolve_resource_spec("core.vcns"),
                    )
                    if current.namespace is not None
                    else TopologyContext("vcns")
                )
            else:
                self.topology_context = None
            return
        if self.mount_collection is not None and self.mount_collection.name == "oci":
            if self.schema_path:
                self.schema_path = self.schema_path[:-1]
            else:
                self.mount_collection = None
            return
        if self.mount_leaf is not None:
            self.mount_leaf = None
            return
        if self.resource_context is not None and self.collection_context is not None:
            self.resource_context = None
            return
        if self.collection_context is not None:
            if (
                self.collection_context.virtual_kind
                and self.collection_context.virtual_kind
                in {VirtualKind.TIME_QUERY, VirtualKind.TIME_QUERY_SELECTOR}
                and self.collection_context.previous_collection is not None
            ):
                self.collection_context = self.collection_context.previous_collection
                self.resource_context = None
                return
            parent_resource = self.collection_context.parent_resource
            parent_collection = self.collection_context.parent_collection
            self.collection_context = None
            self.resource_context = parent_resource
            self.collection_context = parent_collection
            return
        if self.resource_context is not None:
            self.resource_context = None
            return
        if (
            self.mount_entry is not None
            and self.mount_entry.collection == "region"
            and self.browser.parents
        ):
            self.browser.change_directory("..")
            return
        if self.mount_entry is not None:
            was_region = self.mount_entry.collection == "region"
            self.mount_entry = None
            if was_region:
                self._sync_browser_region()
            return
        if self.mount_collection is not None:
            self.mount_collection = None
            return
        if self.namespace_view is not None:
            self.namespace_view = None
            return
        self.browser.change_directory("..")

    def _snapshot_locator(
        self,
    ) -> tuple[
        CompartmentNode,
        list[CompartmentNode],
        CollectionContext | None,
        ResourceContext | None,
        MountCollectionContext | None,
        MountEntryContext | None,
        MountLeafContext | None,
        TopologyContext | None,
        str | None,
        tuple[str, ...],
    ]:
        return (
            self.browser.current,
            list(self.browser.parents),
            self.collection_context,
            self.resource_context,
            self.mount_collection,
            self.mount_entry,
            self.mount_leaf,
            getattr(self, "topology_context", None),
            self.namespace_view,
            self.schema_path,
        )

    def _restore_locator(
        self,
        snapshot: tuple[
            CompartmentNode,
            list[CompartmentNode],
            CollectionContext | None,
            ResourceContext | None,
            MountCollectionContext | None,
            MountEntryContext | None,
            MountLeafContext | None,
            TopologyContext | None,
            str | None,
            tuple[str, ...],
        ],
    ) -> None:
        (
            current,
            parents,
            collection_context,
            resource_context,
            mount_collection,
            mount_entry,
            mount_leaf,
            topology_context,
            namespace_view,
            schema_path,
        ) = snapshot
        self.browser.current = current
        self.browser.parents = parents
        self.collection_context = collection_context
        self.resource_context = resource_context
        self.mount_collection = mount_collection
        self.mount_entry = mount_entry
        self.mount_leaf = mount_leaf
        self.topology_context = topology_context
        self.namespace_view = namespace_view
        self.schema_path = schema_path
        self._sync_browser_region()

    def _change_locator(self, target: str) -> None:
        if target in ("", "."):
            return
        preserve_path_for_region = target.startswith("/region/")
        absolute = target.startswith(("/", "~"))
        parts = [part for part in target.replace("~", "/", 1).split("/") if part]
        saved_current = self.browser.current
        saved_parents = list(self.browser.parents)
        saved_collection = self.collection_context
        saved_resource = self.resource_context
        saved_mount_collection = self.mount_collection
        saved_mount_entry = self.mount_entry
        saved_mount_leaf = self.mount_leaf
        saved_topology_context = getattr(self, "topology_context", None)
        saved_schema_path = getattr(self, "schema_path", ())
        saved_namespace_view = self.namespace_view
        saved_session_region = self.session_region
        try:
            # A leading region is a first-class absolute namespace component:
            # /us-ashburn-1/<tenancy>/<compartment>, not /region/us-ashburn-1.
            # Subscription discovery keeps this accurate for the tenancy.
            if (
                absolute
                and parts
                and parts[0] not in self._root_mounts()
                and parts[0] != self.browser.root.name
                and self._is_subscribed_region(parts[0])
            ):
                self.session_region = parts.pop(0)
            if absolute:
                if not preserve_path_for_region:
                    self.browser.change_directory("/")
                self.collection_context = None
                self.resource_context = None
                self.mount_collection = None
                self.mount_entry = None
                self.mount_leaf = None
                self.topology_context = None
                self.namespace_view = None
                self.schema_path = ()
                if parts and parts[0] == self.browser.root.name:
                    parts = parts[1:]
            for part in parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    self._step_up()
                    continue
                if self.mount_leaf is not None:
                    raise ValueError(f"not found: {part}")
                if (
                    self.topology_context is not None
                    and self.topology_context.namespace is not None
                ):
                    current = self.topology_context
                    if current.level == "service":
                        spec = next(
                            (
                                item
                                for item in self._topology_service_specs(
                                    current.namespace
                                )
                                if self.browser._normalize_resource_token(item.name)
                                == self.browser._normalize_resource_token(part)
                            ),
                            None,
                        )
                        if spec is None:
                            raise ValueError(f"topology collection not found: {part}")
                        self.topology_context = TopologyContext(
                            level="service-collection",
                            namespace=current.namespace,
                            collection_spec=spec,
                        )
                        continue
                    if current.level == "service-collection":
                        assert current.collection_spec is not None
                        matches = self.browser._matching_rows(
                            self.browser.list_resources(
                                current.collection_spec.qualified_name
                            ),
                            part,
                        )
                        if len(matches) != 1:
                            raise ValueError(f"topology resource not found: {part}")
                        row = matches[0]
                        if current.collection_spec.qualified_name == "core.vcns":
                            self.topology_context = TopologyContext(
                                level="vcn", vcn=row, namespace=current.namespace
                            )
                            continue
                        self.collection_context = CollectionContext(
                            spec=current.collection_spec,
                            collection_name=self.browser._normalize_resource_token(
                                current.collection_spec.name
                            ),
                        )
                        self.resource_context = ResourceContext(
                            spec=current.collection_spec, row=row
                        )
                        self.topology_context = None
                        continue
                    if current.level == "vcn":
                        if part != "subnets":
                            raise ValueError("topology VCN contains: subnets")
                        self.topology_context = TopologyContext(
                            level="subnets",
                            vcn=current.vcn,
                            namespace=current.namespace,
                        )
                        continue
                    if current.level == "subnets":
                        assert current.vcn is not None
                        matches = self.browser._matching_rows(
                            self._topology_subnets(current.vcn), part
                        )
                        if len(matches) != 1:
                            raise ValueError(f"subnet not found in topology: {part}")
                        self.topology_context = TopologyContext(
                            level="subnet",
                            vcn=current.vcn,
                            subnet=matches[0],
                            namespace=current.namespace,
                        )
                        continue
                    if current.level == "subnet":
                        if part != "consumers":
                            raise ValueError("topology subnet contains: consumers")
                        self.topology_context = TopologyContext(
                            level="consumers",
                            vcn=current.vcn,
                            subnet=current.subnet,
                            namespace=current.namespace,
                        )
                        continue
                    if current.level == "consumers":
                        assert current.subnet is not None
                        targets = self._topology_consumers(current.subnet)
                        match = next(
                            (
                                item
                                for item in targets
                                if self._topology_projection_name(item, targets) == part
                            ),
                            None,
                        )
                        if match is None or match.spec is None:
                            raise ValueError(f"topology consumer not found: {part}")
                        self.browser.change_to_compartment(match.compartment_id)
                        self.namespace_view = match.spec.namespace
                        self.collection_context = CollectionContext(
                            spec=match.spec,
                            collection_name=self.browser._normalize_resource_token(
                                match.spec.name
                            ),
                        )
                        self.resource_context = ResourceContext(
                            spec=match.spec, row=match.row
                        )
                        self.topology_context = None
                        continue
                if (
                    self.mount_collection is not None
                    and self.mount_collection.name == "topology"
                ):
                    current = self.topology_context
                    if current is None:
                        if part != "vcns":
                            raise ValueError("topology contains: vcns")
                        self.topology_context = TopologyContext(level="vcns")
                        continue
                    if current.level == "vcns":
                        matches = self.browser._matching_rows(
                            self._topology_vcns(), part
                        )
                        if len(matches) != 1:
                            raise ValueError(f"VCN not found: {part}")
                        self.topology_context = TopologyContext(
                            level="vcn", vcn=matches[0]
                        )
                        continue
                    if current.level == "vcn":
                        if part != "subnets":
                            raise ValueError("topology VCN contains: subnets")
                        self.topology_context = TopologyContext(
                            level="subnets", vcn=current.vcn
                        )
                        continue
                    if current.level == "subnets":
                        assert current.vcn is not None
                        matches = self.browser._matching_rows(
                            self._topology_subnets(current.vcn), part
                        )
                        if len(matches) != 1:
                            raise ValueError(f"subnet not found in topology: {part}")
                        self.topology_context = TopologyContext(
                            level="subnet",
                            vcn=current.vcn,
                            subnet=matches[0],
                            embedded=current.embedded,
                        )
                        continue
                    if current.level == "subnet":
                        if part != "consumers":
                            raise ValueError("topology subnet contains: consumers")
                        self.topology_context = TopologyContext(
                            level="consumers",
                            vcn=current.vcn,
                            subnet=current.subnet,
                            embedded=current.embedded,
                        )
                        continue
                    if current.level == "consumers":
                        assert current.subnet is not None
                        targets = self._topology_consumers(current.subnet)
                        match = next(
                            (
                                item
                                for item in targets
                                if self._topology_projection_name(item, targets) == part
                            ),
                            None,
                        )
                        if match is None or match.spec is None:
                            raise ValueError(f"topology consumer not found: {part}")
                        self.browser.change_to_compartment(match.compartment_id)
                        self.namespace_view = match.spec.namespace
                        self.collection_context = CollectionContext(
                            spec=match.spec,
                            collection_name=self.browser._normalize_resource_token(
                                match.spec.name
                            ),
                        )
                        self.resource_context = ResourceContext(
                            spec=match.spec, row=match.row
                        )
                        self.mount_collection = None
                        self.topology_context = None
                        continue
                if self.mount_entry is not None and self.mount_entry.collection not in {
                    "catalog",
                    "region",
                }:
                    raise ValueError(f"not found: {part}")
                if (
                    self.mount_collection is not None
                    and self.mount_collection.name == "catalog"
                    and self.mount_entry is None
                ):
                    if part not in self._mount_entries("catalog"):
                        raise ValueError(f"catalog not found: {part}")
                    spec = self.browser.resolve_resource_spec(part)
                    if spec.scope not in {"region", "regional-catalog"}:
                        raise ValueError(f"catalog not found: {part}")
                    self.mount_entry = MountEntryContext(
                        collection="catalog", name=part
                    )
                    self.namespace_view = spec.namespace
                    self._enter_collection_context(
                        spec, self.browser._normalize_resource_token(spec.name)
                    )
                    continue
                if (
                    self.mount_collection is not None
                    and self.mount_collection.name == "oci"
                ):
                    if part not in self.oci_schema.children(self.schema_path):
                        raise ValueError(f"OCI SDK schema path not found: {part}")
                    self.schema_path = (*self.schema_path, part)
                    continue
                if self.mount_collection is not None and not (
                    self.mount_collection.name in {"catalog", "region"}
                    and self.mount_entry is not None
                ):
                    entries = self._mount_entries(self.mount_collection.name)
                    if part not in entries:
                        raise ValueError(
                            f"{self.mount_collection.name} not found: {part}"
                        )
                    self.mount_entry = MountEntryContext(
                        collection=self.mount_collection.name, name=part
                    )
                    self.mount_leaf = None
                    self.collection_context = None
                    self.resource_context = None
                    self._sync_browser_region()
                    continue
                if part in self._root_mounts():
                    self.mount_collection = MountCollectionContext(name=part)
                    self.mount_entry = None
                    self.mount_leaf = None
                    self.collection_context = None
                    self.resource_context = None
                    self.schema_path = ()
                    self.topology_context = None
                    continue
                namespace = self.browser.resolve_namespace_path(part)
                if self.namespace_view is None and namespace is not None:
                    self.namespace_view = namespace
                    self.collection_context = None
                    self.resource_context = None
                    continue
                if self.resource_context is not None and self._follow_relationship(
                    part
                ):
                    continue
                if (
                    self.resource_context is not None
                    and self._try_enter_collection_context(part)
                ):
                    continue
                if self.collection_context is not None:
                    self._try_enter_resource_from_collection(part)
                    continue
                child = self.browser.find_child(part)
                if child is not None:
                    self.browser.parents.append(self.browser.current)
                    self.browser.current = child
                    self.collection_context = None
                    self.resource_context = None
                    continue
                if self._try_enter_collection_context(part):
                    continue
                raise ValueError(f"not found: {part}")
            self._sync_browser_region()
        except Exception:
            self.browser.current = saved_current
            self.browser.parents = saved_parents
            self.collection_context = saved_collection
            self.resource_context = saved_resource
            self.mount_collection = saved_mount_collection
            self.mount_entry = saved_mount_entry
            self.mount_leaf = saved_mount_leaf
            self.topology_context = saved_topology_context
            self.schema_path = saved_schema_path
            self.namespace_view = saved_namespace_view
            self.session_region = saved_session_region
            self._sync_browser_region()
            raise

    def _resource_context_kwargs(
        self, collection: ResourceChildCollection
    ) -> dict[str, str]:
        if self.resource_context is None:
            return {}
        kwargs: dict[str, str] = {}
        for param_name, source in collection.api_kwargs:
            if source == "id":
                kwargs[param_name] = self.resource_context.row.id
                continue
            if source == "name":
                kwargs[param_name] = self.resource_context.row.name
                continue
            value = (self.resource_context.row.details or {}).get(source)
            if value in (None, "-", ""):
                value = (self.resource_context.row.payload or {}).get(source)
            if value not in (None, "-", ""):
                kwargs[param_name] = value
        return kwargs

    def _list_rows_for_type(
        self, resource_type: str
    ) -> tuple[ResourceSpec, list[ResourceRow]]:
        if (
            self.collection_context is not None
            and self.resource_context is None
            and self.browser._normalize_resource_token(resource_type)
            == self.collection_context.collection_name
        ):
            return self.collection_context.spec, self._collection_rows()
        normalized = self.browser._normalize_resource_token(resource_type)
        children = self._resource_context_children()
        if normalized in children:
            collection = children[normalized]
            spec = self.browser.resolve_resource_spec(collection.resource_type)
            row_filter = None
            if collection.row_filter_key and self.resource_context is not None:
                row_filter = (collection.row_filter_key, self.resource_context.row.id)
            rows = self.browser.list_resources(
                collection.resource_type,
                extra_kwargs=self._resource_context_kwargs(collection),
                row_filter=row_filter,
            )
            return spec, rows
        spec = self.browser.resolve_resource_spec(resource_type)
        rows = self.browser.list_resources(resource_type)
        return spec, rows

    def _current_node_data(self) -> dict[str, object]:
        node = self._current_node_state()
        if node.kind == "topology":
            current = getattr(self, "topology_context", None)
            assert current is not None
            payload: dict[str, object] = {
                "kind": "topology-projection",
                "level": current.level,
                "ownership": "none",
                "operation_blockers": "not evaluated by topology",
            }
            if current.vcn is not None:
                payload["vcn"] = {"name": current.vcn.name, "id": current.vcn.id}
            if current.subnet is not None:
                payload["subnet"] = {
                    "name": current.subnet.name,
                    "id": current.subnet.id,
                }
            if current.level == "consumers" and current.subnet is not None:
                targets = self._topology_consumers(current.subnet)
                payload["projections"] = [
                    {
                        "name": self._topology_projection_name(target, targets),
                        "canonical_path": self._canonical_resource_path(target),
                        "role": (target.row.details or {}).get("topology_role"),
                        "edge": (target.row.details or {}).get("topology_kind"),
                    }
                    for target in targets
                ]
            return payload
        if node.kind == "static-schema":
            return self.oci_schema.payload(self.schema_path)
        if node.kind in {"mount-root", "mount-leaf"}:
            return self._current_mount_payload()
        if node.kind == "domain":
            specs = [
                spec
                for spec in self.browser.resource_specs_for_namespace(
                    self.namespace_view
                )
                if self.browser._is_public_resource_spec(spec)
            ]
            return {
                "kind": "domain",
                "name": self._namespace_slug(self.namespace_view),
                "count": len(specs),
            }
        if node.kind == "region-anchor":
            return self._mount_entry_payload("region", self.mount_entry.name)
        if self.collection_context is not None and self.resource_context is None:
            if self.collection_context.virtual_kind == VirtualKind.TIME_QUERY_SELECTOR:
                provider = self.collection_context.time_query_provider or ""
                control = (
                    self.collection_context.time_query_pending
                    or self.collection_context.collection_name
                )
                return {
                    "kind": "control-file",
                    "provider": provider,
                    "name": control,
                    "value": dict(self.collection_context.time_query_options).get(
                        control
                    ),
                    "write": f"write {control} <value>",
                }
            payload: dict[str, object] = {
                "kind": "collection",
                "type": self.collection_context.spec.qualified_name,
                "adapter": self.collection_context.spec.adapter_kind,
                "collection": self.collection_context.collection_name,
                "compartment": self.browser.get_path(),
                "parent_resource": (
                    {
                        "type": self.collection_context.parent_resource.spec.qualified_name,
                        "name": self.collection_context.parent_resource.row.name,
                        "id": self.collection_context.parent_resource.row.id,
                    }
                    if self.collection_context.parent_resource is not None
                    else None
                ),
            }
            payload["listing"] = getattr(self, "_collection_listing_state", {}).get(
                self._current_path_suffix().rstrip("/"),
                {"state": "not-listed"},
            )
            if self.collection_context.virtual_kind in {
                VirtualKind.TIME_QUERY,
                VirtualKind.TIME_QUERY_SELECTOR,
            }:
                provider = self.collection_context.time_query_provider or ""
                payload.update(
                    {
                        "provider": provider,
                        "controls": (
                            "page",
                            *TIME_QUERY_PROVIDERS[provider]["controls"],
                        ),
                        "values": dict(self.collection_context.time_query_options),
                        "write": "write <control> <value>; then ls",
                    }
                )
            return payload
        if self.resource_context is None:
            return self.browser.current_payload()
        if self.resource_context.row.payload is not None:
            return self.resource_context.row.payload
        return {
            "type": self.resource_context.spec.qualified_name,
            "name": self.resource_context.row.name,
            "id": self.resource_context.row.id,
            "state": self.resource_context.row.state,
            "details": self.resource_context.row.details or {},
        }

    def _stat_node_kind(self, node: NodeState) -> str:
        if self.collection_context is not None and (
            self.collection_context.virtual_kind == VirtualKind.TIME_QUERY_SELECTOR
        ):
            return "terminal-record"
        return {
            "resource": "resource",
            "relationship-projection": "projection",
            "collection": "collection",
            "compartment": "directory",
            "domain": "directory",
            "mount-root": "directory",
            "mount-leaf": "terminal-record",
            "region-anchor": "directory",
            "static-schema": "directory",
        }.get(node.kind, "directory")

    def _canonical_resource_path(self, target: RelationshipTarget) -> str:
        assert target.spec is not None
        current = self.browser.current
        return "/".join(
            (
                "",
                self._effective_region(),
                self.browser.root.name,
                *(node.name for node in self.browser.parents),
                current.name,
                self._namespace_slug(target.spec.namespace),
                self.browser._normalize_resource_token(target.spec.name),
                self.browser._sanitize_row_name(target.row.name),
            )
        )

    def _stat_canonical_path(self, node: NodeState) -> str:
        if node.kind == "static-schema":
            return node.path_suffix.rstrip("/") or "/oci"
        return f"/{self._effective_region()}{node.path_suffix}"

    def _stat_children(self, node: NodeState) -> list[dict[str, str]]:
        if node.kind == "topology":
            current = getattr(self, "topology_context", None)
            assert current is not None
            if current.level == "service":
                return [
                    {
                        "name": self.browser._normalize_resource_token(spec.name),
                        "kind": "collection",
                    }
                    for spec in self._topology_service_specs(current.namespace or "")
                ]
            if current.level == "vcn":
                return [{"name": "subnets", "kind": "directory"}]
            if current.level == "subnet":
                return [{"name": "consumers", "kind": "projection-directory"}]
            if current.level == "consumers" and current.subnet is not None:
                targets = self._topology_consumers(current.subnet)
                return [
                    {
                        "name": self._topology_projection_name(target, targets),
                        "kind": "projection",
                    }
                    for target in targets
                ]
            return []
        if self.resource_context is not None:
            return [
                {"name": entry.name, "kind": entry.kind}
                for entry in self._resource_entries()
            ]
        if self.collection_context is not None:
            provider = self.collection_context.time_query_provider
            if provider:
                return [
                    {"name": name, "kind": "control-file"}
                    for name in ("page", *TIME_QUERY_PROVIDERS[provider]["controls"])
                ]
            if self.collection_context.virtual_kind is None:
                return [
                    {"name": name, "kind": "view-directory"}
                    for name in ("page", "name", "state")
                ]
            return []
        if node.kind == "static-schema":
            return [
                {"name": name, "kind": "directory"}
                for name in self.oci_schema.children(self.schema_path)
            ]
        if node.kind == "domain" and self.namespace_view is not None:
            entries = [
                {
                    "name": self.browser._normalize_resource_token(spec.name),
                    "kind": "collection",
                }
                for spec in self.browser.resource_specs_for_namespace(
                    self.namespace_view
                )
                if self.browser._is_public_resource_spec(spec)
            ]
            if self._topology_service_specs(self.namespace_view):
                entries.append({"name": "topology", "kind": "topology-directory"})
            return entries
        return []

    def _stat_scope(self, node: NodeState) -> dict[str, object]:
        if node.kind == "static-schema":
            return {"region": None, "compartment": None}
        current = self.browser.current
        return {
            "region": self._effective_region(),
            "compartment": {
                "id": getattr(current, "id", None),
                "path": self.browser.get_path(),
            },
        }

    def _current_payload(self) -> dict[str, object]:
        """Return the uniform stat envelope for the current namespace node."""
        node = self._current_node_state()
        kind = self._stat_node_kind(node)
        capabilities = ["read"]
        if kind in {"directory", "collection", "projection"}:
            capabilities.extend(("list", "navigate"))
        if (
            self.collection_context is not None
            and self.collection_context.time_query_provider
        ):
            capabilities.append("write-controls")
        payload: dict[str, object] = {
            "kind": kind,
            "canonical_path": self._stat_canonical_path(node),
            "scope": self._stat_scope(node),
            "freshness": {"state": "not-tracked", "cached_at": None},
            "children": self._stat_children(node),
            "capabilities": capabilities,
            "data": self._current_node_data(),
        }
        failure = getattr(self.browser, "last_oci_failure", None)
        if isinstance(failure, dict):
            payload["failure"] = failure
        if self.resource_context is not None:
            payload["oci"] = {
                "type": self.resource_context.spec.qualified_name,
                "id": self.resource_context.row.id,
            }
            payload["adapter"] = self.resource_context.spec.adapter_kind
        if (
            self.collection_context is not None
            and self.collection_context.time_query_provider
        ):
            provider = self.collection_context.time_query_provider
            payload["query"] = {
                "provider": provider,
                "controls": ("page", *TIME_QUERY_PROVIDERS[provider]["controls"]),
                "values": dict(self.collection_context.time_query_options),
                "max_pages": TIME_QUERY_PROVIDERS[provider]["max_pages"],
            }
        return payload

    def _time_query_record_payload(self, row: ResourceRow) -> dict[str, object]:
        """Describe one bounded-query result as a terminal namespace record."""
        current = self.collection_context
        assert current is not None and current.time_query_provider is not None
        provider = current.time_query_provider
        node = self._current_node_state()
        return {
            "kind": "terminal-record",
            "canonical_path": (
                f"/{self._effective_region()}{node.path_suffix}/"
                f"{self.browser._sanitize_row_name(row.name)}"
            ),
            "scope": self._stat_scope(node),
            "freshness": {"state": "not-tracked", "cached_at": None},
            "children": [],
            "capabilities": ["read"],
            "record": {"id": row.id, "state": row.state},
            "query": {
                "provider": provider,
                "values": dict(current.time_query_options),
                "max_pages": TIME_QUERY_PROVIDERS[provider]["max_pages"],
            },
            "data": row.payload
            or {
                "id": row.id,
                "name": row.name,
                "state": row.state,
                "details": row.details or {},
            },
        }

    def _resource_actions_payload(self) -> dict[str, object]:
        if self.resource_context is None:
            raise ValueError("actions is only available on a resource")
        path = self._current_path_suffix()
        try:
            delete_spec = self.deletion._delete_spec_for(self.resource_context)
        except ValueError:
            delete_spec = None
        actions: dict[str, object] = {}
        if delete_spec is not None:
            actions["delete"] = {
                "kind": "preview-action",
                "path": path,
                "apply_with": f"rm --apply {path}",
                "recursive_supported": delete_spec.recursive_supported,
            }
        return {"kind": "actions", "resource": path, "entries": actions}

    def _resource_entries(self) -> list[NamespaceEntry]:
        if self.resource_context is None:
            return []
        relationships = self._relationship_namespace().view(self.resource_context)
        entries = [
            NamespaceEntry(name, "field")
            for name in self._resource_fields.names_for(self.resource_context)
        ]
        virtual_children = {
            **RESOURCE_VIRTUAL_CHILDREN.get("default", {}),
            **RESOURCE_VIRTUAL_CHILDREN.get(
                self.resource_context.spec.qualified_name, {}
            ),
        }
        entries.extend(
            NamespaceEntry(name, "projection")
            for name, rule in virtual_children.items()
            if rule is not None
            and (
                not rule.get("requires_detail")
                or (self.resource_context.row.details or {}).get(
                    str(rule["requires_detail"])
                )
            )
            and (
                not rule.get("requires_absent_detail")
                or not (self.resource_context.row.details or {}).get(
                    str(rule["requires_absent_detail"])
                )
            )
        )
        if relationships.links or relationships.collections:
            entries.append(NamespaceEntry("relationships", "relationships"))
        entries.extend(
            NamespaceEntry(name, "relationship-directory")
            for name in relationships.collections
        )
        entries.extend(
            NamespaceEntry(name, "collection")
            for name in self._resource_context_children()
        )
        if self.resource_context.spec.qualified_name == "core.vcns":
            entries.append(NamespaceEntry("topology", "topology-directory"))
        if self.resource_context.spec.qualified_name == "core.subnets":
            entries.append(NamespaceEntry("blockers", "blocker-directory"))
        unique = {entry.name: entry for entry in entries}
        resolved = tuple(sorted(unique.values(), key=lambda entry: entry.name))
        if hasattr(self, "_namespace_entry_cache"):
            self._namespace_entry_cache[self._current_path_suffix().rstrip("/")] = (
                resolved
            )
        return list(resolved)

    def _resolve_namespace_node(self, target: str) -> ResolvedNamespaceNode:
        """Resolve any virtual path once, preserving typed file-vs-directory semantics."""
        parts = [part for part in target.rstrip("/").split("/") if part]
        parent = "/".join(parts[:-1])
        if target.startswith("/") and parent:
            parent = "/" + parent
        if parent:
            self._change_locator(parent)
        if (
            parts
            and self.collection_context is not None
            and hasattr(self.collection_context, "spec")
        ):
            matches = self.browser._matching_rows(self._collection_rows(), parts[-1])
            if (
                len(matches) == 1
                and not self._row_is_navigable(self.collection_context.spec, matches[0])
                and self.collection_context.spec.qualified_name
                not in COMPARTMENT_COLLECTIONS
            ):
                return ResolvedNamespaceNode("record", matches[0])
        if parts and self.resource_context is not None:
            leaf = parts[-1]
            try:
                return ResolvedNamespaceNode(
                    "field", self._resource_fields.resolve(self.resource_context, leaf)
                )
            except KeyError:
                pass
        self._change_locator(parts[-1] if parent else target)
        return ResolvedNamespaceNode("directory")

    def _with_resolved_target(
        self, target: str, action: Callable[[ResolvedNamespaceNode], None]
    ) -> None:
        snapshot = self._snapshot_locator()
        try:
            action(self._resolve_namespace_node(target))
        except ValueError as exc:
            print(exc)
        finally:
            self._restore_locator(snapshot)

    def _list_target(self, target: str, long_format: bool) -> None:
        def render(node: ResolvedNamespaceNode) -> None:
            if node.kind == "directory":
                self._list_current_node(long_format)
                return
            name = (
                "actions"
                if node.kind == "actions"
                else target.rstrip("/").split("/")[-1]
            )
            if long_format:
                self._print_simple_table(
                    [("name", "Name"), ("type", "Type")],
                    [{"name": name, "type": node.kind}],
                )
            else:
                print(name)

        self._with_resolved_target(target, render)

    def _cat_target(self, target: str) -> None:
        def render(node: ResolvedNamespaceNode) -> None:
            if node.kind == "field":
                self._print_resource_field(node.value)
            elif node.kind == "record":
                row = node.value
                assert isinstance(row, ResourceRow)
                if (
                    self.collection_context is not None
                    and self.collection_context.virtual_kind == VirtualKind.TIME_QUERY
                ):
                    print(
                        json.dumps(
                            self._time_query_record_payload(row),
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    return
                print(
                    json.dumps(
                        row.payload
                        or {
                            "id": row.id,
                            "name": row.name,
                            "state": row.state,
                            "details": row.details or {},
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            elif node.kind == "actions":
                print(
                    json.dumps(
                        self._resource_actions_payload(), indent=2, sort_keys=True
                    )
                )
            else:
                self._read_current_node()

        self._with_resolved_target(target, render)

    def _payload_contains_any(self, value: object, targets: set[str]) -> bool:
        if isinstance(value, dict):
            return any(
                self._payload_contains_any(item, targets) for item in value.values()
            )
        if isinstance(value, list):
            return any(self._payload_contains_any(item, targets) for item in value)
        if isinstance(value, str):
            return value in targets
        return False

    def _relationship_namespace(self) -> RelationshipNamespace:
        namespace = getattr(self, "relationships", None)
        if namespace is None:
            namespace = RelationshipNamespace(self)
            self.relationships = namespace
        return namespace

    @staticmethod
    def _payload_ocid_references(payload: object) -> list[tuple[str, str]]:
        return RelationshipNamespace._payload_ocid_references(payload)

    def _direct_relationship_references(
        self, payload: object
    ) -> list[tuple[str, RelationshipTarget]]:
        return self._relationship_namespace().direct(payload, self.resource_context)

    @staticmethod
    def _is_ownership_link_label(label: str) -> bool:
        return RelationshipNamespace._is_ownership_label(label)

    def _indirect_relationship_references(self) -> list[tuple[str, RelationshipTarget]]:
        if self.resource_context is None:
            return []
        return self._relationship_namespace()._indirect(self.resource_context)

    def _relationship_candidates(self) -> list[tuple[str, RelationshipTarget]]:
        if self.resource_context is None:
            return []
        namespace = self._relationship_namespace()
        return [
            *namespace.direct(
                self.resource_context.row.payload or {}, self.resource_context
            ),
            *namespace._indirect(self.resource_context),
        ]

    @staticmethod
    def _relationship_collection_name(indexed_label: str) -> str:
        return RelationshipNamespace._collection_name(indexed_label)

    def _relationship_collections(self) -> dict[str, tuple[RelationshipTarget, ...]]:
        return self._relationship_namespace().view(self.resource_context).collections

    def _relationship_references(self) -> list[tuple[str, RelationshipTarget]]:
        return list(self._relationship_namespace().view(self.resource_context).links)

    def _relationship_target(self, name: str) -> tuple[RelationshipTarget, str] | None:
        normalized = self.browser._normalize_resource_token(name)
        for link_name, target in self._relationship_references():
            if self.browser._normalize_resource_token(link_name) != normalized:
                continue
            if target.spec is None:
                continue
            return target, self._canonical_relationship_path(target)
        return None

    def _canonical_relationship_path(self, target: RelationshipTarget) -> str:
        """Return the target's canonical resource path, never a projection path."""
        if target.spec is None:
            raise ValueError("relationship target has no resource type")
        chain = self.browser.compartment_chain(target.compartment_id)
        return "/".join(
            [
                "",
                self._effective_region(),
                *(node.name for node in chain),
                self._namespace_slug(target.spec.namespace),
                self.browser._normalize_resource_token(target.spec.name),
                self.browser._sanitize_row_name(target.row.name),
            ]
        )

    def _readlink_target(self, name: str) -> tuple[RelationshipTarget, str] | None:
        direct = self._relationship_target(name)
        if direct is not None:
            return direct
        current = self.collection_context
        if current is None or current.virtual_kind != VirtualKind.RELATIONSHIPS:
            return None
        matches = self.browser._matching_rows(
            [target.row for target in current.relationship_targets], name
        )
        if len(matches) != 1:
            return None
        target = next(
            item
            for item in current.relationship_targets
            if item.row.id == matches[0].id
        )
        return target, self._canonical_relationship_path(target)

    def _follow_relationship(self, name: str) -> bool:
        target = self._relationship_target(name)
        if target is None:
            return False
        relationship, _path = target
        self.browser.change_to_compartment(relationship.compartment_id)
        self.namespace_view = relationship.spec.namespace
        self.collection_context = CollectionContext(
            spec=relationship.spec,
            collection_name=self.browser._normalize_resource_token(
                relationship.spec.name
            ),
        )
        self.resource_context = ResourceContext(
            spec=relationship.spec, row=relationship.row
        )
        self.mount_collection = None
        self.mount_entry = None
        self.mount_leaf = None
        return True

    def _list_vcn_child_rows(
        self, vcn_context: ResourceContext, collection_name: str
    ) -> list[ResourceRow]:
        child_map = RESOURCE_CONTEXT_CHILDREN["core.vcns"]
        if collection_name not in child_map:
            raise ValueError(f"unsupported vcn child collection: {collection_name}")
        resource_type, api_kwargs, row_filter_key = child_map[collection_name]
        extra_kwargs: dict[str, str] = {}
        for param_name, source in api_kwargs:
            if source == "id":
                extra_kwargs[param_name] = vcn_context.row.id
            elif source == "compartment_id":
                extra_kwargs[param_name] = self._resource_compartment_id(vcn_context)
        rows = self.browser.list_resources(resource_type, extra_kwargs=extra_kwargs)
        if row_filter_key:
            rows = [
                row
                for row in rows
                if (row.details or {}).get(row_filter_key) == vcn_context.row.id
            ]
        return rows

    @staticmethod
    def _resource_compartment_id(resource_context: ResourceContext) -> str:
        payload = resource_context.row.payload or {}
        value = payload.get("compartment_id")
        if isinstance(value, str) and value.startswith("ocid1.compartment."):
            return value
        raise ValueError(
            f"unable to determine compartment_id for {resource_context.spec.qualified_name}"
        )

    @staticmethod
    def _display_name_from_payload(payload: dict[str, object]) -> str:
        for key in ("display_name", "name", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return "<unnamed>"

    def _payload_contains_any(self, value: object, targets: set[str]) -> bool:
        if isinstance(value, dict):
            return any(
                self._payload_contains_any(item, targets) for item in value.values()
            )
        if isinstance(value, list):
            return any(self._payload_contains_any(item, targets) for item in value)
        return isinstance(value, str) and value in targets

    def _with_browser_compartment_scope(
        self,
        current: CompartmentNode,
        parents: tuple[CompartmentNode, ...],
        action: Callable[[], object],
    ) -> object:
        saved_current = self.browser.current
        saved_parents = list(self.browser.parents)
        try:
            self.browser.current = current
            self.browser.parents = list(parents)
            return action()
        finally:
            self.browser.current = saved_current
            self.browser.parents = saved_parents

    def _row_path(self, base_path: str, collection_name: str, row: ResourceRow) -> str:
        return f"{base_path}/{collection_name}/{row.name}"

    def _list_call_all(
        self, operation: Callable[..., object], **kwargs: object
    ) -> list[object]:
        response = self.browser._retry_oci_call(
            oci.pagination.list_call_get_all_results,
            operation,
            **kwargs,
        )
        return self.browser._collection_items(response.data)

    def _discover_vcn_blockers(
        self, vcn_context: ResourceContext, subnet_rows: list[ResourceRow]
    ) -> list[dict[str, str]]:
        compartment_id = self._resource_compartment_id(vcn_context)
        vcn_id = vcn_context.row.id
        subnet_ids = {row.id for row in subnet_rows}
        targets = {vcn_id, *subnet_ids}
        blockers: list[dict[str, str]] = []

        for subnet in subnet_rows:
            for vnic in self._subnet_service_vnic_rows(
                ResourceContext(
                    spec=self.browser.resolve_resource_spec("core.subnets"), row=subnet
                )
            ):
                blockers.append(
                    {
                        "kind": str(
                            (vnic.details or {}).get("blocker_kind") or "service-vnic"
                        ),
                        "name": (
                            f"{subnet.name}/{vnic.name}"
                            f" ({(vnic.details or {}).get('owner')})"
                        ),
                        "id": vnic.id,
                    }
                )

        instance_ids: set[str] = set()
        attachments = self._list_call_all(
            self.browser.compute.list_vnic_attachments, compartment_id=compartment_id
        )
        for attachment in attachments:
            instance_id = getattr(attachment, "instance_id", None)
            vnic_id = getattr(attachment, "vnic_id", None)
            if not instance_id or not vnic_id:
                continue
            try:
                vnic = self.browser.virtual_network.get_vnic(vnic_id).data
            except Exception:
                continue
            if (
                getattr(vnic, "subnet_id", None) not in subnet_ids
                and getattr(vnic, "vcn_id", None) != vcn_id
            ):
                continue
            if instance_id in instance_ids:
                continue
            instance_ids.add(instance_id)
            try:
                instance = self.browser.compute.get_instance(instance_id).data
                name = str(
                    getattr(instance, "display_name", None)
                    or getattr(instance, "id", instance_id)
                )
            except Exception:
                name = str(instance_id)
            blockers.append({"kind": "instance", "name": name, "id": str(instance_id)})

        for kind, operation in (
            ("load-balancer", self.browser.load_balancer.list_load_balancers),
            ("oke-cluster", self.browser.container_engine.list_clusters),
            ("oke-node-pool", self.browser.container_engine.list_node_pools),
            (
                "oke-virtual-node-pool",
                self.browser.container_engine.list_virtual_node_pools,
            ),
            ("instance-pool", self.browser.compute_management.list_instance_pools),
            ("cluster-network", self.browser.compute_management.list_cluster_networks),
        ):
            try:
                items = self._list_call_all(operation, compartment_id=compartment_id)
            except Exception:
                continue
            for item in items:
                payload = oci.util.to_dict(item)
                if not isinstance(payload, dict):
                    continue
                if self._is_terminal_blocker_payload(payload):
                    continue
                if not self._payload_contains_any(payload, targets):
                    continue
                blockers.append(
                    {
                        "kind": kind,
                        "name": self._display_name_from_payload(payload),
                        "id": str(
                            payload.get("id")
                            or payload.get("identifier")
                            or self._display_name_from_payload(payload)
                        ),
                    }
                )
        return blockers

    def _is_terminal_blocker_payload(self, payload: dict[str, object]) -> bool:
        state = str(
            payload.get("lifecycle_state")
            or payload.get("lifecycleState")
            or payload.get("status")
            or ""
        ).upper()
        return state in self.TERMINAL_BLOCKER_STATES

    def _subnet_service_vnic_rows(
        self, subnet_context: ResourceContext
    ) -> list[ResourceRow]:
        """Return Network Firewalls that own service VNICs in one subnet."""
        try:
            firewalls = self.browser.list_resources(
                "network_firewall.network-firewalls"
            )
        except Exception:
            return []
        rows: list[ResourceRow] = []
        for firewall in firewalls:
            payload = firewall.payload or {}
            if (
                payload.get("subnet_id") != subnet_context.row.id
                or firewall.state.upper() in self.TERMINAL_BLOCKER_STATES
            ):
                continue
            owner_path = (
                f"/{self._effective_region()}{self.browser.get_path()}"
                f"/{self._namespace_slug('network_firewall')}/network-firewalls/{firewall.name}"
            )
            rows.append(
                ResourceRow(
                    name=f"network-firewall@{firewall.name}",
                    state=firewall.state,
                    id=firewall.id,
                    details={
                        "private_ip": payload.get("ipv4_address") or "-",
                        "hostname_label": "-",
                        "owner": firewall.name,
                        "owner_type": "network-firewall",
                        "owner_path": owner_path,
                        "blocker_kind": "network-firewall",
                    },
                    payload=payload,
                )
            )
        return rows

    def do_pwd(self, arg: str) -> None:
        """Print the current compartment path."""
        print(self._current_locator())

    def _complete_qualified_resource(self, text: str) -> list[str]:
        prefix = text.lower()
        completions = {
            spec.qualified_name.replace("_", "-")
            for spec in self.browser.resource_specs_for_namespace()
        }
        return [name for name in sorted(completions) if name.startswith(prefix)]

    def _complete_current_collection_entries(self, text: str) -> list[str]:
        """Complete only collections currently discoverable by ``ll``."""
        if not hasattr(self.browser, "current") or not hasattr(self.browser, "region"):
            return self._complete_qualified_resource(text)
        qualified_names = self._known_compartment_collections(self.browser.current.id)
        entries: list[str] = []
        for qualified_name in qualified_names:
            try:
                spec = self.browser.resolve_resource_spec(qualified_name)
            except ValueError:
                continue
            if (
                spec.qualified_name not in COMPARTMENT_COLLECTIONS
                and self._resource_hierarchy.policy_for(spec).permits_flat_access
            ):
                entries.append(spec.qualified_name.replace("_", "-"))
        if "." not in text:
            namespaces = {entry.partition(".")[0] + "." for entry in entries}
            return sorted(entry for entry in namespaces if entry.startswith(text))
        return sorted(entry for entry in entries if entry.startswith(text))

    def _complete_core_resource(self, text: str) -> list[str]:
        return [
            name
            for name in self._complete_qualified_resource(text)
            if name.startswith("core.")
        ]

    def _complete_namespace_path(self, argument: str) -> list[str]:
        """Complete registry-derived domain/type path components without OCI calls."""
        typed = argument.lstrip("/")
        if "/" not in typed:
            return [
                name for name in self.browser.namespaces() if name.startswith(typed)
            ]
        parent, leaf = typed.rsplit("/", 1)
        domain = parent.rsplit("/", 1)[-1]
        return [
            spec.name.replace("_", "-")
            for spec in self.browser.resource_specs_for_namespace(domain)
            if spec.name.replace("_", "-").startswith(leaf)
        ]

    def _context_resource_completions(self, argument: str) -> list[str]:
        """Complete already-known namespace/resource entries without OCI calls."""
        if getattr(self, "resource_context", None) is not None:
            try:
                path = self._current_path_suffix().rstrip("/")
            except AttributeError:
                path = ""
            if "/" in argument:
                parent, leaf = argument.rsplit("/", 1)
                cached_rows = getattr(self, "_completion_cache", {}).get(
                    f"{path}/{parent.strip('/')}", ()
                )
                return [name for name in cached_rows if name.startswith(leaf)]
            cached = getattr(self, "_namespace_entry_cache", {}).get(path)
            if cached is not None:
                return [
                    entry.name for entry in cached if entry.name.startswith(argument)
                ]
            context = self.resource_context
            entries = {
                *self._resource_fields.names_for(context),
                *self._resource_context_children_map.get(
                    context.spec.qualified_name, {}
                ),
                "logs",
                "relationships",
            }
            if context.spec.qualified_name == "core.subnets":
                entries.add("blockers")
            return [entry for entry in sorted(entries) if entry.startswith(argument)]
        if getattr(self, "collection_context", None) is not None:
            current = self.collection_context
            if not isinstance(current, CollectionContext):
                return []
            if current.time_query_provider:
                entries = (
                    "page",
                    *TIME_QUERY_PROVIDERS[current.time_query_provider]["controls"],
                )
            elif current.virtual_kind is None:
                entries = ("page", "name", "state")
            else:
                entries = ()
            return [entry for entry in entries if entry.startswith(argument)]
        namespace_view = getattr(self, "namespace_view", None)
        if (
            namespace_view is not None
            and getattr(self, "collection_context", None) is None
            and "." not in argument
        ):
            entries = [
                spec.name.replace("_", "-")
                for spec in self.browser.resource_specs_for_namespace(namespace_view)
                if spec.name.replace("_", "-").startswith(argument)
            ]
            if self._topology_service_specs(namespace_view) and "topology".startswith(
                argument
            ):
                entries.append("topology")
            return entries
        return []

    def _complete_current_compartment_children(self, argument: str) -> list[str]:
        """Complete bare child-compartment names from local catalog state."""
        if (
            "/" in argument
            or getattr(self, "namespace_view", None) is not None
            or getattr(self, "collection_context", None) is not None
            or getattr(self, "resource_context", None) is not None
        ):
            return []
        catalog = getattr(self, "catalog", None)
        if catalog is not None and catalog.is_ready():
            children = catalog.children_for(self.browser.current.id)
        else:
            try:
                cache_key = (self.browser.region(), self.browser.current.id)
            except (AttributeError, TypeError):
                return []
            cached = getattr(self.browser, "children_cache", {}).get(cache_key)
            children = cached[1] if cached is not None else ()
        return [child.name for child in children if child.name.startswith(argument)]

    def _at_completion_root(self) -> bool:
        return all(
            getattr(self, name, None) is None
            for name in (
                "resource_context",
                "collection_context",
                "mount_collection",
                "mount_entry",
                "mount_leaf",
            )
        )

    @staticmethod
    def _completion_argument(line: str, begidx: int, endidx: int, text: str) -> str:
        """Recover the whole path argument when readline splits words at '/'."""
        start = begidx
        while start > 0 and not line[start - 1].isspace():
            start -= 1
        return line[start:endidx] if line else text

    def _cached_resource_completions(self, text: str, argument: str) -> list[str]:
        cache = getattr(self, "_completion_cache", {})
        matches: set[str] = set()
        if (
            getattr(self, "collection_context", None) is not None
            and "/" not in argument
        ):
            path = self._current_path_suffix().rstrip("/")
            matches.update(
                name for name in cache.get(path, ()) if name.startswith(text)
            )

        typed = argument.replace("~", "", 1).lstrip("/")
        for path, names in cache.items():
            normalized_path = path.lstrip("/")
            prefix = normalized_path + "/"
            if not typed.startswith(prefix):
                continue
            name_prefix = typed[len(prefix) :]
            # readline replaces only the trailing word after '/', so return the
            # leaf name—not the whole path.
            matches.update(name for name in names if name.startswith(name_prefix))
        return sorted(matches)

    def _remember_namespace_paths(self, paths: list[str]) -> None:
        if not hasattr(self, "_known_namespace_paths"):
            self._known_namespace_paths = set()
        self._known_namespace_paths.update(paths)

    def _complete_known_namespace_paths(self, argument: str) -> list[str]:
        """Complete only path components learned from prior namespace reads."""
        if "/" not in argument:
            return []
        raw = argument.replace("~", "", 1).rstrip("/")
        parent, _, leaf = raw.rpartition("/")
        prefix = f"{parent}/" if parent else "/"
        entries = {
            path[len(prefix) :].split("/", 1)[0]
            for path in getattr(self, "_known_namespace_paths", set())
            if path.startswith(prefix)
        }
        return sorted(entry for entry in entries if entry.startswith(leaf))

    def _complete_absolute_locator(self, argument: str) -> list[str]:
        """Complete a known absolute locator from local compartment state and caches."""
        if not argument.startswith(("/", "~")):
            return []
        parts = [part for part in argument.replace("~", "", 1).split("/") if part]
        compartment_names = [
            self.browser.root.name,
            *(node.name for node in self.browser.parents),
            self.browser.current.name,
        ]
        if parts and parts[0] == self.browser.root.name:
            parts = parts[1:]
        relative_compartments = compartment_names[1:]
        matched = 0
        while (
            matched < len(parts)
            and matched < len(relative_compartments)
            and parts[matched] == relative_compartments[matched]
        ):
            matched += 1
        if matched < len(parts) and matched < len(relative_compartments):
            return []
        remainder = parts[matched:]
        if not remainder:
            return [name for name in self.browser.namespaces()]
        if len(remainder) == 1:
            return [
                name
                for name in self.browser.namespaces()
                if name.startswith(remainder[0])
            ]
        domain, leaf = remainder[-2], remainder[-1]
        if len(remainder) == 2:
            return [
                spec.name.replace("_", "-")
                for spec in self.browser.resource_specs_for_namespace(domain)
                if spec.name.replace("_", "-").startswith(leaf)
            ]
        return []

    def _complete_cached_locator_entries(self, argument: str) -> list[str]:
        """Complete a compartment path from the same local snapshots used by ls.

        This deliberately does not call ``find_child`` or OCI.  A prior ``ls`` of
        the path has already populated both the child and resource-type caches.
        """
        if "/" not in argument:
            return []
        raw = argument.replace("~", "", 1)
        absolute = raw.startswith("/")
        tokens = raw.lstrip("/").split("/")
        parents, leaf = tokens[:-1], tokens[-1]
        current = (
            getattr(self.browser, "root", None)
            if absolute
            else getattr(self.browser, "current", None)
        )
        if current is None or not hasattr(current, "id"):
            return []
        region = self.browser.region()

        if parents and parents[0] == region:
            parents = parents[1:]
        if parents and parents[0] == self.browser.root.name:
            parents = parents[1:]

        catalog = getattr(self, "catalog", None)
        catalog_ready = catalog is not None and catalog.is_ready()

        def children_for(parent_id: str) -> tuple[CompartmentNode, ...]:
            if catalog_ready:
                return catalog.children_for(parent_id)
            cached = getattr(self.browser, "children_cache", {}).get(
                (region, parent_id)
            )
            return cached[1] if cached is not None else ()

        for name in parents:
            children = children_for(current.id)
            current = next((child for child in children if child.name == name), None)
            if current is None:
                return []

        children = children_for(current.id)
        entries = {child.name for child in children}
        qualified_names = self._known_compartment_collections(current.id)
        for qualified_name in qualified_names:
            try:
                spec = self.browser.resolve_resource_spec(qualified_name)
            except ValueError:
                continue
            if spec.qualified_name in COMPARTMENT_COLLECTIONS:
                continue
            hierarchy = getattr(self, "_resource_hierarchy", None)
            if (
                hierarchy is not None
                and not hierarchy.policy_for(spec).permits_flat_access
            ):
                continue
            entries.add(spec.qualified_name.replace("_", "-"))
        return sorted(entry for entry in entries if entry.startswith(leaf))

    def _complete_qualified_collection_path(self, argument: str) -> list[str]:
        """Complete cached names below a dotted qualified collection, without OCI calls."""
        if "/" not in argument:
            return []
        collection, leaf = argument.rsplit("/", 1)
        try:
            spec = self.browser.resolve_resource_spec(collection)
        except ValueError:
            return []
        suffix = f"/{self._namespace_slug(spec.namespace)}/{self.browser._normalize_resource_token(spec.name)}"
        matches = [
            name
            for path, names in self._completion_cache.items()
            if path.endswith(suffix)
            for name in names
            if name.startswith(leaf)
        ]
        return [f"{name}/" if name == leaf else name for name in matches]

    def _complete_qualified_resource_path(self, argument: str) -> list[str]:
        """Complete cached fields below a qualified resource path, without OCI calls."""
        if argument.count("/") < 2:
            return []
        collection, resource_name, leaf = argument.split("/", 2)
        try:
            spec = self.browser.resolve_resource_spec(collection)
        except ValueError:
            return []
        suffix = f"/{self._namespace_slug(spec.namespace)}/{self.browser._normalize_resource_token(spec.name)}"
        row = next(
            (
                rows.get(resource_name)
                for path, rows in self._resource_completion_cache.items()
                if path.endswith(suffix) and resource_name in rows
            ),
            None,
        )
        if row is None:
            return []
        context = ResourceContext(spec=spec, row=row)
        entries = set(self._resource_fields.names_for(context)) | {
            "logs",
            "relationships",
        }
        entries.update(self._resource_context_children_map.get(spec.qualified_name, {}))
        return sorted(entry for entry in entries if entry.startswith(leaf))

    def _catalog_resource_path(
        self, argument: str
    ) -> tuple[CompartmentNode, ResourceSpec, str] | None:
        """Parse a catalog-backed path ending in a resource-name prefix."""
        catalog = getattr(self, "catalog", None)
        if catalog is None or not catalog.is_ready() or "/" not in argument:
            return None
        raw = argument.replace("~", "", 1)
        absolute = raw.startswith("/")
        tokens = raw.lstrip("/").split("/")
        parents, leaf = tokens[:-1], tokens[-1]
        if not parents:
            return None
        current = self.browser.root if absolute else self.browser.current
        if not hasattr(current, "id"):
            return None
        region = self.browser.region()
        if parents and parents[0] == region:
            parents = parents[1:]
        if parents and parents[0] == self.browser.root.name:
            parents = parents[1:]
        if not parents:
            return None
        if "." in parents[-1]:
            collection = parents.pop()
        elif len(parents) >= 2:
            collection = f"{parents[-2]}.{parents[-1]}"
            parents = parents[:-2]
        else:
            return None
        try:
            spec = self.browser.resolve_resource_spec(collection)
        except ValueError:
            return None
        for name in parents:
            current = next(
                (
                    child
                    for child in catalog.children_for(current.id)
                    if child.name == name
                ),
                None,
            )
            if current is None:
                return None
        return current, spec, leaf

    def _catalog_resource_completions(self, argument: str) -> list[str]:
        """Complete resource names from the active-region background catalog."""
        catalog = getattr(self, "catalog", None)
        catalog_path = self._catalog_resource_path(argument)
        if catalog_path is not None:
            compartment, spec, leaf = catalog_path
            return [
                (
                    row.name
                    if not self._row_is_navigable(spec, row)
                    else f"{row.name}/"
                    if row.name == leaf
                    else row.name
                )
                for row in catalog.resources_for(compartment.id, spec)
                if row.name.startswith(leaf)
            ]
        if "/" not in argument:
            return []
        collection, leaf = argument.rsplit("/", 1)
        try:
            if "." in collection:
                spec = self.browser.resolve_resource_spec(collection)
            else:
                parts = [
                    part for part in collection.replace("~", "", 1).split("/") if part
                ]
                domain_index = next(
                    index
                    for index, part in enumerate(parts)
                    if part in self.browser.namespaces()
                )
                domain, resource_type = parts[domain_index : domain_index + 2]
                spec = self.browser.resolve_resource_spec(f"{domain}.{resource_type}")
        except (StopIteration, ValueError):
            return []
        if catalog is None:
            return []
        names = catalog.names_for(self.browser.current.id, spec)
        ids = catalog.ids_for(self.browser.current.id, spec)
        return [
            f"{name}/" if name == leaf else name
            for name in (*names, *ids)
            if name.startswith(leaf)
        ]

    def completenames(self, text: str, *ignored: object) -> list[str]:
        return self._completion_engine().command_names(text)

    def completedefault(
        self, text: str, line: str, begidx: int, endidx: int
    ) -> list[str]:
        """Do not guess at free-form command arguments."""
        return []

    def _command_name_completions(self, text: str) -> list[str]:
        """Return executable shell verbs, never resource namespace entries."""
        return cmd.Cmd.completenames(self, text)

    def _completion_engine(self) -> CompletionEngine:
        engine = getattr(self, "completion", None)
        if engine is None:
            engine = CompletionEngine(self)
            self.completion = engine
        return engine

    def _complete_resource_argument(
        self, text: str, line: str, begidx: int, endidx: int
    ) -> list[str]:
        return self._completion_engine().resource_path(text, line, begidx, endidx)

    def complete_ls(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_ll(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_cd(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_cat(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_readlink(
        self, text: str, line: str, begidx: int, endidx: int
    ) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_head(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_tail(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_rm(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def complete_find(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        return self._complete_resource_argument(text, line, begidx, endidx)

    def do_ls(self, arg: str) -> None:
        """List a directory or resource collection: ls [OPTION]... [PATH]."""
        try:
            argv = shlex.split(arg)
        except ValueError as exc:
            print(f"parse error: {exc}")
            return
        if "--help" in argv:
            if len(argv) != 1:
                print("usage: ls [OPTION]... [PATH]")
                return
            self.help_ls()
            return
        long_format = False
        target = None
        index = 0
        while index < len(argv):
            token = argv[index]
            if token in {"-l", "--long"} or (
                token.startswith("-")
                and not token.startswith("--")
                and set(token[1:]) <= {"a", "l"}
                and "l" in token
            ):
                long_format = True
                index += 1
                continue
            if token in {"-a", "--all"} or (
                token.startswith("-")
                and not token.startswith("--")
                and set(token[1:]) <= {"a", "l"}
            ):
                index += 1
                continue
            if token.startswith("-"):
                print(f"unsupported ls option: {token}")
                return
            if target is not None:
                print("usage: ls [OPTION]... [PATH]")
                return
            target = token
            index += 1
        if target is None:
            try:
                self._list_current_node(long_format)
            except ValueError as exc:
                print(exc)
            return
        self._list_target(target, long_format)

    def do_ll(self, arg: str) -> None:
        """Alias for ls -l: ll [path]."""
        self.do_ls(f"-l {arg}".strip())

    def do_completion(self, arg: str) -> None:
        """Show or set completion mode: completion [off|static|cached|catalog]."""
        mode = arg.strip()
        if not mode:
            print(self.completion.mode)
            return
        try:
            self.completion.set_mode(mode)
        except ValueError as exc:
            print(exc)

    def do_cd(self, arg: str) -> None:
        """Change compartment or resource context: cd <name>, cd vcns/<name>, cd .., cd /, cd ~"""
        target = arg.strip()
        if not target:
            print("usage: cd <compartment-name>|..|/|~")
            return
        if target == "-":
            previous = getattr(self, "_previous_locator", None)
            if previous is None:
                print("cd: no previous location")
                return
            current = (self._snapshot_locator(), self.session_region)
            snapshot, session_region = previous
            self.session_region = session_region
            self._restore_locator(snapshot)
            self._previous_locator = current
            self._prime_current_compartment_completion()
            self._update_prompt()
            return
        previous = (self._snapshot_locator(), self.session_region)
        saved_resource_context = self.resource_context
        saved_collection_context = self.collection_context
        try:
            self._change_locator(target)
        except ValueError as exc:
            self.resource_context = saved_resource_context
            self.collection_context = saved_collection_context
            print(exc)
            return
        self._previous_locator = previous
        self._prime_current_compartment_completion()
        self._update_prompt()

    def do_cat(self, arg: str) -> None:
        """Print node content: cat . | cat <resource>/<field> | cat <path>/."""
        target = arg.strip()
        if target in ("", "."):
            self._read_current_node()
            return
        if self.resource_context is not None and "/" not in target:
            relationship = self._relationship_target(target)
            if relationship is not None:
                linked, path = relationship
                print(
                    json.dumps(
                        {
                            "kind": "symlink",
                            "name": target,
                            "canonical_path": path,
                            "type": linked.spec.qualified_name,
                            "id": linked.row.id,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
            try:
                self._print_resource_field(
                    self._resource_fields.resolve(self.resource_context, target)
                )
            except KeyError:
                pass
            else:
                return
        if target.endswith("/."):
            self._cat_target(target[:-2] or "/")
            return
        self._cat_target(target)

    def do_readlink(self, arg: str) -> None:
        """Print a relationship's canonical target path: readlink <relationship>."""
        name = arg.strip()
        if not name or "/" in name:
            print("usage: readlink <relationship>")
            return
        if (
            getattr(self, "topology_context", None) is not None
            and self.topology_context.level == "consumers"
        ):
            assert self.topology_context.subnet is not None
            targets = self._topology_consumers(self.topology_context.subnet)
            target = next(
                (
                    item
                    for item in targets
                    if self._topology_projection_name(item, targets) == name
                ),
                None,
            )
            if target is None:
                print(f"not a relationship: {name}")
                return
            print(self._canonical_resource_path(target))
            return
        target = self._readlink_target(name)
        if target is None:
            print(f"not a relationship: {name}")
            return
        _relationship, path = target
        print(path)

    def _cached_terminal_record(self, name: str) -> ResourceRow | None:
        path = self._current_path_suffix().rstrip("/")
        return getattr(self, "_terminal_record_cache", {}).get(path, {}).get(name)

    @staticmethod
    def _bounded_record_lines(row: ResourceRow) -> list[str]:
        payload = row.payload or {
            "id": row.id,
            "name": row.name,
            "state": row.state,
            "details": row.details or {},
        }
        return json.dumps(payload, indent=2, sort_keys=True, default=str).splitlines()

    def _head_or_tail(self, arg: str, *, tail: bool) -> None:
        try:
            argv = shlex.split(arg)
            count = 10
            if argv[:1] == ["-n"]:
                count = int(argv[1])
                argv = argv[2:]
            if count < 1 or len(argv) != 1:
                raise ValueError
        except (IndexError, ValueError):
            print("usage: head|tail [-n LINES] <cached-terminal-record>")
            return
        row = self._cached_terminal_record(argv[0])
        if row is None:
            print("record is not cached; run ll in this bounded collection first")
            return
        lines = self._bounded_record_lines(row)
        for line in lines[-count:] if tail else lines[:count]:
            print(line)

    def do_head(self, arg: str) -> None:
        """Read the first lines of a cached terminal record: head [-n N] RECORD."""
        self._head_or_tail(arg, tail=False)

    def do_tail(self, arg: str) -> None:
        """Read the last lines of a cached terminal record: tail [-n N] RECORD."""
        self._head_or_tail(arg, tail=True)

    def do_grep(self, arg: str) -> None:
        """Search already-cached terminal records: grep <text>."""
        pattern = arg.strip().casefold()
        if not pattern:
            print("usage: grep <text>")
            return
        path = self._current_path_suffix().rstrip("/")
        records = getattr(self, "_terminal_record_cache", {}).get(path, {})
        for name, row in records.items():
            if (
                pattern
                in json.dumps(row.payload or {}, sort_keys=True, default=str).casefold()
            ):
                print(name)

    def do_stat(self, arg: str) -> None:
        """Read the current namespace stat record: stat [.]"""
        if arg.strip() not in {"", "."}:
            print("stat only reads the current node; use cd first")
            return
        self._read_current_node()

    def do_tree(self, arg: str) -> None:
        """Show one cached namespace level: tree [.]"""
        if arg.strip() not in {"", "."}:
            print("tree only reads the current cached node; use cd first")
            return
        print(self._current_locator())
        if self.resource_context is not None:
            path = self._current_path_suffix().rstrip("/")
            entries = getattr(self, "_namespace_entry_cache", {}).get(path, ())
            for entry in entries:
                suffix = "/" if entry.kind not in {"field", "terminal-record"} else ""
                print(f"├── {entry.name}{suffix}")
            return
        if self.collection_context is not None:
            path = self._current_path_suffix().rstrip("/")
            names = getattr(self, "_completion_cache", {}).get(path, ())
            for name in names:
                print(f"├── {name}")
            return
        print("└── (no cached entries; run ls or ll first)")

    def do_rm(self, arg: str) -> None:
        """Preview or apply deletion for a supported resource path: rm [-r] [-f|--apply] <resource-path>."""
        self.deletion.run(arg)

    def do_write(self, arg: str) -> None:
        """Set a virtual control file: write <control> <value>."""
        try:
            parts = shlex.split(arg, posix=True)
            control, value = parts[0], " ".join(parts[1:])
            if not value:
                raise ValueError
        except (ValueError, IndexError):
            print("usage: write <control> <value>")
            return
        current = self.collection_context
        if current is None or current.virtual_kind not in {
            VirtualKind.TIME_QUERY,
            VirtualKind.TIME_QUERY_SELECTOR,
        }:
            print("write is available only in a time/query collection")
            return
        provider = current.time_query_provider or ""
        allowed = {"page", *TIME_QUERY_PROVIDERS[provider]["controls"]}
        if control == "." and current.time_query_pending is not None:
            control = current.time_query_pending
        if control not in allowed:
            print(f"unknown control: {control}")
            return
        options = dict(current.time_query_options)
        if control == "page":
            try:
                options["page_number"] = int(value)
            except ValueError:
                print("query page must be an integer")
                return
        else:
            options[control] = value
        self._enter_collection_context(
            current.spec,
            current.collection_name,
            parent_resource=current.parent_resource,
            parent_collection=current.parent_collection,
            virtual_kind=VirtualKind.TIME_QUERY,
            time_query_provider=provider,
            time_query_options=options,
            time_query_path=current.time_query_path,
            previous_collection=current.previous_collection,
        )

    def do_help(self, arg: str) -> None:
        target = arg.strip()
        if target:
            super().do_help(target)
            return
        print(
            "\n".join(
                (
                    "Usage: ocish COMMAND [ARGUMENT]...",
                    "Explore an OCI tenancy through a Plan 9-style namespace.",
                    "",
                    "Commands:",
                    "  ls [OPTION]... [PATH]  list present resource types or directory entries",
                    "  ll [PATH]               alias for 'ls --long [PATH]'",
                    "  cd PATH                  change namespace context",
                    "  pwd                      print the current namespace path",
                    "  cat [PATH][/.]           print a node or resource field",
                    "  readlink RELATION         print a relationship's canonical target",
                    "  tree [.]                  show one cached namespace level",
                    "  stat [.]                  read the current node's stat record",
                    "  head|tail [-n N] RECORD   read a cached terminal record",
                    "  grep TEXT                 search cached terminal records",
                    "  find . | TYPE [NAME]     search the current compartment",
                    "  completion [MODE]        show or set completion mode",
                    "  rm [-r] [-f|--apply] PATH  preview or apply deletion",
                    "  exit                     leave the shell",
                    "",
                    "PATH may be relative or absolute; domains include core, dns, identity,",
                    "containerengine, logging, orm, and load-balancer.",
                    "",
                    "Try 'help ls' or 'ls --help' for listing options.",
                    "Try 'help COMMAND' for command-specific help.",
                )
            )
        )

    def help_ls(self) -> None:
        print(
            "\n".join(
                (
                    "Usage: ls [OPTION]... [PATH]",
                    "List present resource types in a compartment or entries in a namespace directory.",
                    "",
                    "With no PATH, list resource types present in the current compartment.",
                    "Every listed type can be entered with 'cd TYPE'.",
                    "",
                    "Options:",
                    "  -a, --all   include all entries (the namespace has no hidden entries)",
                    "  -l, --long  use a long listing format",
                    "      --help  display this help and return to the shell",
                    "",
                    "The 'll [PATH]' command is an alias for 'ls --long [PATH]'.",
                )
            )
        )

    def help_context(self) -> None:
        print("resource context:")
        print("  ls [path] [-l]        list a directory")
        print("  cd <path>             move through the locator")
        print("  cat .                 read the current node")
        print("  cat <leaf>            read one leaf in the current directory")
        print("  cat <path>/.          read any node by path")
        print("  cd ..                 move one level up")

    def help_completion(self) -> None:
        print("completion [off|static|cached|catalog] (never calls OCI):")
        print("  off                   disable all shell completion")
        print(
            "  static                domains, types, and known absolute path prefixes"
        )
        print(
            "  cached                static plus listed resource names and cached resource fields"
        )
        print(
            "  catalog               cached plus active-region OCI Search catalog (default)"
        )

    def do_find(self, arg: str) -> None:
        """Find resources: find [--ocid] . | find <type> [name] | find <path> <type> [name]."""
        raw_ocids = arg.strip() == "--ocid ."
        if arg.strip() in {".", "--ocid ."}:
            if (
                getattr(self, "mount_collection", None) is not None
                and self.mount_collection.name == "oci"
            ):
                for path in self.oci_schema.resources(self.schema_path):
                    print(path)
                return
            if self.resource_context is not None or self.collection_context is not None:
                print(
                    "find . is available only in a compartment context; use ls or cd .."
                )
                return
            if raw_ocids:
                try:
                    for ocid in self._find_all_ocids_via_search():
                        print(ocid)
                except Exception as exc:
                    print(f"OCI Search failed: {exc}")
                return
            try:
                results = self._find_current_compartment_paths_via_search()
            except Exception as exc:
                print(exc)
                return
            self._print_find_results(results)
            if len(results) >= self.FIND_RESULT_LIMIT:
                print(f"stopped at {self.FIND_RESULT_LIMIT} matches")
            return
        saved_current = self.browser.current
        saved_parents = list(self.browser.parents)
        try:
            name_pattern, type_filters, path = self._parse_find_args(arg)
            if path:
                self.browser.change_directory(path)
            specs = self._find_search_specs(type_filters)
        except ValueError as exc:
            self.browser.current = saved_current
            self.browser.parents = saved_parents
            print(exc)
            return
        try:
            results = self._find_via_search(
                specs, name_pattern, include_lister_results=bool(type_filters)
            )
        except Exception as exc:
            print(exc)
            return
        finally:
            self.browser.current = saved_current
            self.browser.parents = saved_parents
        if results:
            self._print_find_results(results)
            if len(results) >= self.FIND_RESULT_LIMIT:
                print(f"stopped at {self.FIND_RESULT_LIMIT} matches")
        return

    def _print_resource_specs_long(self, specs: list[ResourceSpec]) -> None:
        if not specs:
            return
        columns = [
            ("qualified_name", "Type"),
            ("client_name", "Client"),
            ("list_operation", "List op"),
            ("endpoint_family", "Endpoint"),
            ("scope", "Scope"),
            ("runnable", "Runnable"),
        ]
        raw_widths = {}
        for key, header in columns:
            values = [getattr(spec, key) for spec in specs]
            raw_widths[key] = max(len(header), *(len(str(v)) for v in values))
        widths = self._fit_column_widths(columns, raw_widths)
        print(
            "  ".join(
                self._truncate_cell(header, widths[key]).ljust(widths[key])
                for key, header in columns
            )
        )
        for spec in specs:
            print(
                "  ".join(
                    self._truncate_cell(str(getattr(spec, key)), widths[key]).ljust(
                        widths[key]
                    )
                    for key, _header in columns
                )
            )

    def _fit_column_widths(
        self, columns: list[tuple[str, str]], raw_widths: dict[str, int]
    ) -> dict[str, int]:
        separator_width = 2 * (len(columns) - 1)
        terminal_width = shutil.get_terminal_size((160, 24)).columns
        target_width = max(terminal_width - separator_width, len(columns) * 6)
        widths = {
            key: min(raw_widths[key], COLUMN_MINIMUMS.get(key, 8))
            for key, _header in columns
        }
        growth_order = [
            "name",
            "state",
            *(key for key, _header in columns if key not in {"name", "state"}),
        ]
        for key in growth_order:
            if key not in widths:
                continue
            while widths[key] < raw_widths[key] and sum(widths.values()) < target_width:
                widths[key] += 1
        return widths

    @staticmethod
    def _truncate_cell(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        if width <= 1:
            return value[:width]
        return value[: width - 1] + "…"

    def do_exit(self, arg: str) -> bool:
        """Exit the shell."""
        return True

    def do_quit(self, arg: str) -> bool:
        """Exit the shell."""
        return True

    def do_EOF(self, arg: str) -> bool:
        print()
        return True

    def emptyline(self) -> None:
        return

    def default(self, line: str) -> None:
        argv = shlex.split(line)
        if not argv:
            return
        print(f"unknown command: {argv[0]}")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv
    if len(argv) > 1:
        program = Path(argv[0]).name if argv else "ocish"
        print(f"usage: {program}", file=sys.stderr)
        return 1
    try:
        browser = OciCompartmentBrowser()
    except Exception as exc:  # pragma: no cover - simple CLI fallback
        print(f"failed to initialize OCI client: {exc}", file=sys.stderr)
        return 1
    OciNavShell(browser).cmdloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
