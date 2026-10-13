from __future__ import annotations

import re

import oci

from models import ResourceRow, ResourceSpec


class InventoryRowsMixin:
    """OCI row conversion, enrichment, formatting, and resource resolution."""

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
