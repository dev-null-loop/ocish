from __future__ import annotations

import importlib
import inspect
import re

from config import NAMESPACE_DISCOVERY, SUPPORT_CLIENTS
from models import ResourceSpec


class InventoryRegistryMixin:
    """Build the declarative resource registry from OCI SDK capabilities."""

    @staticmethod
    def _declared_client_classes() -> dict[str, str]:
        clients = dict(SUPPORT_CLIENTS)
        for namespace, config in NAMESPACE_DISCOVERY.items():
            for key in ("client_map", "support_client_map"):
                for attribute, class_path in dict(config.get(key, {})).items():
                    existing = clients.setdefault(str(attribute), str(class_path))
                    if existing != class_path:
                        raise ValueError(
                            f"conflicting client declaration for {attribute} in {namespace}"
                        )
        return clients

    def _build_resource_specs(self) -> list[ResourceSpec]:
        specs: list[ResourceSpec] = []
        for namespace, config in NAMESPACE_DISCOVERY.items():
            specs.extend(self._build_namespace_resource_specs(namespace, config))
        return sorted(specs, key=lambda spec: (spec.namespace, spec.name))

    def _build_namespace_resource_specs(
        self, namespace: str, config: dict[str, object]
    ) -> list[ResourceSpec]:
        endpoint_family = str(config["endpoint_family"])
        client_map = config.get("client_map", {})
        custom_listers = dict(config.get("custom_listers", {}))
        resource_name_overrides = dict(config.get("resource_name_overrides", {}))
        scope_overrides = dict(config.get("scope_overrides", {}))
        runnable_overrides = dict(config.get("runnable_overrides", {}))
        findable_overrides = dict(config.get("findable_overrides", {}))
        search_type_overrides = dict(config.get("search_type_overrides", {}))
        node_capability_overrides = dict(config.get("node_capability_overrides", {}))
        adapter_config_overrides = dict(config.get("adapter_config_overrides", {}))
        adapter_kind = str(config.get("adapter_kind", "resource-tree"))
        specs: list[ResourceSpec] = []
        seen: set[str] = set()
        for client_attr, class_name in dict(client_map).items():
            module_name, client_name = str(class_name).rsplit(".", 1)
            client_cls = getattr(importlib.import_module(module_name), client_name)
            for method_name, method in inspect.getmembers(client_cls, inspect.isfunction):
                if not method_name.startswith("list_"):
                    continue
                source_resource_name = method_name.removeprefix("list_")
                resource_name = str(
                    resource_name_overrides.get(source_resource_name, source_resource_name)
                )
                if resource_name in seen:
                    continue
                seen.add(resource_name)
                required_params = self._required_param_names(method)
                accepted_params = self._accepted_param_names(method)
                scope, runnable = self._infer_scope(required_params, accepted_params)
                runnable = bool(runnable_overrides.get(source_resource_name, runnable))
                scope = str(scope_overrides.get(source_resource_name, scope))
                specs.append(
                    ResourceSpec(
                        namespace=namespace,
                        name=resource_name,
                        endpoint_family=endpoint_family,
                        scope=scope,
                        lister_name=custom_listers.get(source_resource_name),
                        client_attr=str(client_attr),
                        client_name=client_cls.__name__,
                        list_operation=method_name,
                        required_params=tuple(required_params),
                        accepted_params=tuple(accepted_params),
                        runnable=runnable,
                        findable=bool(
                            findable_overrides.get(
                                source_resource_name,
                                runnable and scope.startswith("compartment"),
                            )
                        ),
                        search_type=search_type_overrides.get(source_resource_name),
                        node_capability=node_capability_overrides.get(
                            source_resource_name, "navigable-resource"
                        ),
                        adapter_kind=adapter_kind,
                        adapter_config=tuple(
                            sorted(
                                dict(
                                    adapter_config_overrides.get(source_resource_name, {})
                                ).items()
                            )
                        ),
                    )
                )
        for extra in config.get("extra_specs", []):
            extra_spec = dict(extra)
            specs.append(
                ResourceSpec(
                    namespace=namespace,
                    name=str(extra_spec["name"]),
                    endpoint_family=str(extra_spec["endpoint_family"]),
                    scope=str(extra_spec["scope"]),
                    lister_name=extra_spec.get("lister_name"),
                    client_attr=extra_spec.get("client_attr"),
                    client_name=extra_spec.get("client_name"),
                    list_operation=extra_spec.get("list_operation"),
                    required_params=tuple(extra_spec.get("required_params", ())),
                    accepted_params=tuple(extra_spec.get("accepted_params", ())),
                    runnable=bool(extra_spec.get("runnable", True)),
                    findable=bool(extra_spec.get("findable", extra_spec.get("runnable", True))),
                    search_type=extra_spec.get("search_type"),
                    node_capability=str(extra_spec.get("node_capability", "navigable-resource")),
                    adapter_kind=str(extra_spec.get("adapter_kind", adapter_kind)),
                    adapter_config=tuple(sorted(dict(extra_spec.get("adapter_config", {})).items())),
                )
            )
        return sorted(specs, key=lambda spec: spec.name)

    @staticmethod
    def _required_param_names(method: object) -> list[str]:
        sig = inspect.signature(method)
        return [
            param.name
            for param in sig.parameters.values()
            if param.name != "self"
            and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
            and param.default is inspect._empty
        ]

    @staticmethod
    def _accepted_param_names(method: object) -> list[str]:
        sig = inspect.signature(method)
        explicit = [
            param.name
            for param in sig.parameters.values()
            if param.name != "self"
            and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        ]
        if explicit:
            return explicit
        doc = getattr(method, "__doc__", "") or ""
        seen: list[str] = []
        for name in re.findall(r":param\s+[^:]+\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:", doc):
            if name not in seen:
                seen.append(name)
        return seen

    @staticmethod
    def _infer_scope(required_params: list[str], accepted_params: list[str]) -> tuple[str, bool]:
        required = set(required_params)
        if not required_params:
            accepted = set(accepted_params)
            if "compartment_id" in accepted:
                if "scope" in accepted:
                    return "compartment+scope", True
                return "compartment(filtered)", True
            if any(InventoryRegistryMixin._is_resource_reference_param(name) for name in accepted_params):
                return "parent-resource", False
            return "region", True
        if required == {"compartment_id"}:
            return "compartment", True
        if required == {"scope", "compartment_id"}:
            return "compartment+scope", True
        if "compartment_id" in required:
            return "compartment+extra", False
        if any(InventoryRegistryMixin._is_resource_reference_param(name) for name in required_params):
            return "parent-resource", False
        return "special", False

    @staticmethod
    def _is_resource_reference_param(name: str) -> bool:
        return name.endswith("_id") and not name.startswith("opc_")
