from __future__ import annotations

import json

import oci

from models import ResourceRow


class CoreInventoryMixin:
    """Core custom listers and OCI-specific row formatting."""

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
