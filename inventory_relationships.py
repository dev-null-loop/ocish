from __future__ import annotations

import re
import time

import oci

from config import DIRECT_GETTERS
from models import CompartmentNode, RelationshipTarget, ResourceRow


class InventoryRelationshipsMixin:
    """Relationship resolution and compartment-chain operations."""

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
