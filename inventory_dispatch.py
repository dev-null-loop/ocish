from __future__ import annotations

import inspect

import oci

from config import NAMESPACE_DISCOVERY, NAMESPACE_PATH_SLUGS
from models import RelationshipTarget, ResourceRow, ResourceSpec


class InventoryDispatchMixin:
    """Namespace resolution, generic listing, and resource dispatch."""

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

