from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import oci

from config import INDIRECT_RELATIONSHIP_POLICIES, RELATIONSHIP_PROVENANCE_RULES
from models import RelationshipTarget, ResourceContext

if TYPE_CHECKING:
    from main import OciNavShell


@dataclass(frozen=True)
class RelationshipView:
    links: tuple[tuple[str, RelationshipTarget], ...]
    collections: dict[str, tuple[RelationshipTarget, ...]]


class RelationshipNamespace:
    """Build the operational relationship view for one resource context."""

    def __init__(self, shell: OciNavShell) -> None:
        self.shell = shell
        self._indirect_cache: dict[
            str, tuple[float, tuple[tuple[str, RelationshipTarget], ...]]
        ] = {}

    @staticmethod
    def _payload_ocid_references(payload: object) -> list[tuple[str, str]]:
        references: list[tuple[str, str]] = []

        def visit(value: object, label: str | None = None) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    visit(nested, str(key).removesuffix("_id").replace("_", "-"))
            elif isinstance(value, list):
                for index, nested in enumerate(value, start=1):
                    visit(nested, f"{label or 'resource'}-{index}")
            elif (
                isinstance(value, str)
                and value.startswith("ocid1.")
                and label is not None
            ):
                references.append((label, value))

        visit(payload)
        return references

    @staticmethod
    def _is_ownership_label(label: str) -> bool:
        return label.replace("-", "").replace("_", "").casefold() in {
            "compartment",
            "compartmentid",
        }

    def _provenance_target(
        self, resource: ResourceContext, rule: dict[str, object], target_id: str
    ) -> RelationshipTarget | None:
        resource_type = str(rule["resource_type"])
        kwargs = {
            parameter: resource.row.id
            for parameter, source in rule.get("api_kwargs", ())
            if source == "id"
        }
        try:
            rows = self.shell.browser.list_resources(resource_type, extra_kwargs=kwargs)
        except Exception:
            return None
        row = next((item for item in rows if item.id == target_id), None)
        if row is None:
            return None
        return RelationshipTarget(
            spec=self.shell.browser.resolve_resource_spec(resource_type),
            row=row,
            compartment_id=self.shell.browser.current.id,
        )

    def direct(
        self, payload: object, resource: ResourceContext | None = None
    ) -> list[tuple[str, RelationshipTarget]]:
        references: list[tuple[str, RelationshipTarget]] = []
        for label, ocid in self._payload_ocid_references(payload):
            if self._is_ownership_label(label):
                continue
            target = self.shell.browser.relationship_target(ocid)
            if target is None and resource is not None:
                for rule in RELATIONSHIP_PROVENANCE_RULES.get(
                    resource.spec.qualified_name, ()
                ):
                    if label != rule["source_label"]:
                        continue
                    target = self._provenance_target(resource, rule, ocid)
                    label = str(rule["label"])
                    break
            if target is not None:
                references.append((label, target))
        return references

    def _indirect(
        self, resource: ResourceContext
    ) -> list[tuple[str, RelationshipTarget]]:
        if not hasattr(self.shell, "browser"):
            return []
        policy = INDIRECT_RELATIONSHIP_POLICIES.get(resource.spec.qualified_name)
        if policy is None:
            return []
        cached = self._indirect_cache.get(resource.row.id)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        references: list[tuple[str, RelationshipTarget]] = []
        try:
            children = self.shell._resource_context_children()
        except AttributeError:
            return references
        child_names = tuple(policy.get("child_collections", ()))
        max_collections = int(policy.get("max_child_collections", len(child_names)))
        selected_children = [
            children[name] for name in child_names if name in children
        ][:max_collections]
        for collection in selected_children:
            try:
                row_filter = (
                    (collection.row_filter_key, resource.row.id)
                    if collection.row_filter_key
                    else None
                )
                rows = self.shell.browser.list_resources(
                    collection.resource_type,
                    extra_kwargs=self.shell._resource_context_kwargs(collection),
                    row_filter=row_filter,
                )
            except oci.exceptions.ServiceError:
                continue
            for row in rows:
                for _label, target in self.direct(row.payload or {}):
                    if (
                        policy.get("include_same_type")
                        and target.spec is not None
                        and target.spec.qualified_name == resource.spec.qualified_name
                        and target.row.id != resource.row.id
                    ):
                        references.append(
                            (
                                f"network-security-groups-{len(references) + 1}",
                                target,
                            )
                        )
                if (
                    resource.spec.qualified_name == "core.instances"
                    and collection.resource_type == "vnic-attachments"
                ):
                    vnic_id = (row.payload or {}).get("vnic_id")
                    if not isinstance(vnic_id, str) or not vnic_id.startswith("ocid1."):
                        continue
                    try:
                        vnic = self.shell.browser._retry_oci_call(
                            self.shell.browser.virtual_network.get_vnic, vnic_id
                        ).data
                        subnet_id = getattr(vnic, "subnet_id", None)
                    except Exception:
                        continue
                    if not isinstance(subnet_id, str):
                        continue
                    target = self.shell.browser.relationship_target(subnet_id)
                    if target is not None:
                        references.append(("subnet-1", target))
        self._indirect_cache[resource.row.id] = (
            now + getattr(self.shell.browser, "PROJECTION_CACHE_TTL_SECONDS", 30),
            tuple(references),
        )
        return references

    @staticmethod
    def _collection_name(indexed_label: str) -> str:
        base = re.sub(r"-\d+$", "", indexed_label).removesuffix("-ids")
        return base if base.endswith("s") else f"{base}s"

    def view(self, resource: ResourceContext | None) -> RelationshipView:
        if resource is None:
            return RelationshipView((), {})
        candidates = [
            *self.direct(resource.row.payload or {}, resource),
            *self._indirect(resource),
        ]
        links: list[tuple[str, RelationshipTarget]] = []
        collections: dict[str, list[RelationshipTarget]] = {}
        seen_names: set[str] = set()
        seen_targets: set[str] = set()
        for label, target in candidates:
            if self._is_ownership_label(label) or target.spec is None:
                continue
            if re.search(r"-\d+$", label):
                name = self._collection_name(label)
                if target.row.id not in {
                    item.row.id for item in collections.get(name, [])
                }:
                    collections.setdefault(name, []).append(target)
                continue
            if target.row.id == resource.row.id or target.row.id in seen_targets:
                continue
            link_name = label
            suffix = 2
            while link_name in seen_names:
                link_name = f"{label}-{suffix}"
                suffix += 1
            seen_names.add(link_name)
            seen_targets.add(target.row.id)
            links.append((link_name, target))
        return RelationshipView(
            tuple(links), {name: tuple(items) for name, items in collections.items()}
        )
