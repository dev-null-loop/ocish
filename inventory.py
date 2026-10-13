from __future__ import annotations

import importlib
import json
import time
from typing import ClassVar

import oci

from inventory_adapters.core import CoreInventoryMixin
from inventory_adapters.object_storage import ObjectStorageInventoryMixin
from inventory_adapters.openai import OpenAIInventoryMixin
from inventory_adapters.queries import QueryInventoryMixin
from inventory_catalog import ActiveRegionCatalog  # noqa: F401
from inventory_catalog_ops import InventoryCatalogOpsMixin
from inventory_dispatch import InventoryDispatchMixin
from inventory_naming import InventoryNamingMixin
from inventory_registry import InventoryRegistryMixin
from inventory_relationships import InventoryRelationshipsMixin
from inventory_rows import InventoryRowsMixin
from inventory_transport import InventoryTransportMixin
from models import CompartmentNode, RelationshipTarget, ResourceRow


class OciCompartmentBrowser(ObjectStorageInventoryMixin, OpenAIInventoryMixin, QueryInventoryMixin, CoreInventoryMixin, InventoryRowsMixin, InventoryNamingMixin, InventoryDispatchMixin, InventoryCatalogOpsMixin, InventoryRelationshipsMixin, InventoryTransportMixin, InventoryRegistryMixin):
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
