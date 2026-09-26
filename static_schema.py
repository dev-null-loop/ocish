"""Static, SDK-faithful OCI schema tree; intentionally independent of tenancy data."""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import re
from pathlib import Path

import oci

from models import ResourceSpec


class OciSdkSchemaTree:
    """Expose registered SDK resource types as a deterministic read-only tree."""

    def __init__(self, specs: list[ResourceSpec]) -> None:
        self._specs = tuple(specs)
        self._sdk_resources = self._discover_sdk_resources()
        self._model_cache: dict[
            tuple[str, str], tuple[str | None, dict[str, str], dict[str, str]]
        ] = {}

    @staticmethod
    def _discover_sdk_resources() -> dict[str, dict[str, tuple[str, ...]]]:
        """Discover every installed OCI SDK package without constructing clients."""
        result: dict[str, dict[str, tuple[str, ...]]] = {}
        for module_info in pkgutil.iter_modules(oci.__path__):
            service = module_info.name
            try:
                module = __import__(f"oci.{service}", fromlist=["*"])
            except Exception:
                continue
            resources: dict[str, set[str]] = {}
            read_methods: list[tuple[str, str]] = []
            all_methods: list[tuple[str, tuple[str, ...]]] = []
            for _name, client in inspect.getmembers(module, inspect.isclass):
                if not client.__name__.endswith("Client"):
                    continue
                for method_name, _method in inspect.getmembers(
                    client, inspect.isfunction
                ):
                    if method_name.startswith("_"):
                        continue
                    parameters = tuple(inspect.signature(_method).parameters)
                    all_methods.append((method_name, parameters))
                    prefix, separator, resource = method_name.partition("_")
                    if not separator:
                        continue
                    if prefix in {"create", "update", "delete"}:
                        resources.setdefault(resource, set()).add(method_name)
                    elif prefix in {"get", "list"}:
                        read_methods.append((resource, method_name))
            # Read/list APIs enrich a lifecycle-defined resource; they never
            # create a resource node on their own (options, shapes, statuses,
            # work-request logs, and similar query surfaces are excluded).
            for read_name, method_name in read_methods:
                candidates = (
                    read_name,
                    read_name.removesuffix("s"),
                    read_name.removesuffix("es"),
                    read_name.removesuffix("ies") + "y",
                    f"{read_name}s",
                    f"{read_name}es",
                )
                resource = next(
                    (name for name in candidates if name in resources), None
                )
                if resource is not None:
                    resources[resource].add(method_name)
            # A create-only action (for example create_kubeconfig) is not a
            # durable SDK resource. Retain CRUD resources and updateable
            # resources with a readable/listable state (for example addons).
            resources = {
                name: methods
                for name, methods in resources.items()
                if (
                    any(item.startswith("create_") for item in methods)
                    and any(item.startswith(("update_", "delete_")) for item in methods)
                )
                or (
                    any(item.startswith("update_") for item in methods)
                    and any(item.startswith(("get_", "list_")) for item in methods)
                )
                or (
                    any(item.startswith("create_") for item in methods)
                    and any(item == f"get_{name}" for item in methods)
                )
            }
            # Attach action APIs to the resource named by their typed ID
            # parameter (cluster_id, node_pool_id, vault_id, ...). This makes
            # operations navigable without service-specific action mappings.
            for resource, methods in resources.items():
                identifier = f"{resource}_id"
                methods.update(
                    method_name
                    for method_name, parameters in all_methods
                    if identifier in parameters
                )
            if resources:
                result[service] = {
                    name: tuple(sorted(methods)) for name, methods in resources.items()
                }
        OciSdkSchemaTree._add_terraform_coverage(result)
        return result

    @staticmethod
    def _add_terraform_coverage(
        resources: dict[str, dict[str, tuple[str, ...]]],
    ) -> None:
        """Use Terraform resource names as canonical coverage, retaining SDK methods."""
        provider_root = os.getenv("OCISH_TERRAFORM_PROVIDER_OCI_ROOT")
        if not provider_root:
            return
        provider_root_path = Path(provider_root).expanduser()
        docs = provider_root_path / "website/docs/r"
        services_root = provider_root_path / "internal/service"
        if not docs.is_dir() or not services_root.is_dir():
            return
        provider_services = sorted(
            (path.name for path in services_root.iterdir() if path.is_dir()),
            key=lambda item: len(item.replace("_", "")),
            reverse=True,
        )
        terraform: dict[str, list[str]] = {}
        for document in docs.glob("*.html.markdown"):
            stem = document.name.removesuffix(".html.markdown").removeprefix("oci_")
            compact = stem.replace("_", "")
            provider_service = next(
                (
                    key
                    for key in provider_services
                    if compact.startswith(key.replace("_", ""))
                ),
                None,
            )
            if provider_service is None:
                continue
            compact_service = provider_service.replace("_", "")
            consumed = 0
            folded = ""
            for index, char in enumerate(stem):
                if char != "_":
                    folded += char
                if folded == compact_service:
                    consumed = index + 1
                    break
            suffix = stem[consumed:].lstrip("_")
            if suffix:
                terraform.setdefault(provider_service, []).append(suffix)
        sdk_by_folded = {service.replace("_", ""): service for service in resources}
        canonical_services: dict[str, dict[str, tuple[str, ...]]] = {}
        for provider_service, names in terraform.items():
            service = sdk_by_folded.get(
                provider_service.replace("_", ""), provider_service
            )
            if service in canonical_services and provider_service != service:
                service = provider_service
            sdk_resources = resources.get(service, {})
            canonical: dict[str, tuple[str, ...]] = {}
            for name in names:
                methods = next(
                    (
                        value
                        for sdk_name, value in sdk_resources.items()
                        if name.endswith(sdk_name) or sdk_name.endswith(name)
                    ),
                    ("terraform_resource",),
                )
                canonical[name] = methods
            canonical_services.setdefault(service, {}).update(canonical)
        resources.clear()
        resources.update(canonical_services)

    def children(self, path: tuple[str, ...]) -> list[str]:
        if not path:
            return sorted(self._sdk_resources)
        if len(path) == 1:
            return sorted(self._sdk_resources.get(path[0], {}))
        if len(path) == 2 and self._methods(path):
            return ["attributes", "metadata", "operations"]
        if len(path) == 3 and path[2] == "operations" and self._methods(path):
            return list(self._methods(path) or ())
        return []

    def payload(self, path: tuple[str, ...]) -> dict[str, object]:
        if len(path) < 2:
            return {"kind": "schema-directory", "path": "/oci/" + "/".join(path)}
        methods = self._methods(path)
        if methods is None:
            raise ValueError("static SDK schema path not found")
        if len(path) == 2:
            return {
                "kind": "sdk-resource",
                "provider": "oci",
                "native_service": path[0],
                "native_resource": path[1],
                "path": "/oci/" + "/".join(path),
            }
        if len(path) == 4 and path[2] == "operations" and path[3] in methods:
            return {
                "kind": "operation",
                "resource": ".".join(path[:2]),
                "sdk_method": path[3],
            }
        leaf = path[2]
        if leaf == "attributes":
            model, swagger_types, attribute_map = self._model(path)
            return {
                "kind": "attributes",
                "resource": ".".join(path[:2]),
                "sdk_model": model,
                "attributes": [
                    {
                        "native_name": name,
                        "wire_name": attribute_map.get(name, name),
                        "type": value,
                    }
                    for name, value in sorted(swagger_types.items())
                ],
            }
        if leaf == "operations":
            return {
                "kind": "operations",
                "resource": ".".join(path[:2]),
                "sdk_methods": list(methods),
            }
        if leaf == "metadata":
            return {
                "kind": "metadata",
                "provider": "oci",
                "native_service": path[0],
                "native_resource": path[1],
                "source": "installed OCI Python SDK",
            }
        raise ValueError("static SDK schema leaf not found")

    def walk(self, path: tuple[str, ...] = ()) -> list[str]:
        """Return every static leaf below a schema path in deterministic order."""
        results: list[str] = []
        for child in self.children(path):
            child_path = (*path, child)
            nested = self.children(child_path)
            if nested:
                results.extend(self.walk(child_path))
            else:
                results.append("/oci/" + "/".join(child_path))
        return results

    def resources(self, path: tuple[str, ...] = ()) -> list[str]:
        if not path:
            return [
                f"/oci/{service}/{resource}"
                for service in self.children(())
                for resource in self.children((service,))
            ]
        if len(path) == 1:
            return [f"/oci/{path[0]}/{resource}" for resource in self.children(path)]
        return ["/oci/" + "/".join(path)] if self._methods(path) else []

    def _spec(self, path: tuple[str, ...]) -> ResourceSpec | None:
        if len(path) < 2:
            return None
        namespace, name = path[0].replace("-", "_"), path[1].replace("-", "_")
        return next(
            (
                spec
                for spec in self._specs
                if spec.namespace == namespace and spec.name == name
            ),
            None,
        )

    def _methods(self, path: tuple[str, ...]) -> tuple[str, ...] | None:
        return (
            self._sdk_resources.get(path[0], {}).get(path[1])
            if len(path) >= 2
            else None
        )

    def _model(
        self, path: tuple[str, ...]
    ) -> tuple[str | None, dict[str, str], dict[str, str]]:
        key = (path[0], path[1])
        if key in self._model_cache:
            return self._model_cache[key]
        model_name: str | None = None
        try:
            module = importlib.import_module(f"oci.{path[0]}")
            methods = tuple(self._methods(path) or ())
            preferred = (f"get_{path[1]}", f"list_{path[1]}s")
            ordered_methods = [name for name in preferred if name in methods] + [
                name for name in methods if name not in preferred
            ]
            for _name, client in inspect.getmembers(module, inspect.isclass):
                if not client.__name__.endswith("Client"):
                    continue
                for method_name in ordered_methods:
                    method = getattr(client, method_name, None)
                    doc = getattr(method, "__doc__", "") or ""
                    match = re.search(
                        r"data of type :class:`~(oci\.[^`]+\.models\.[A-Za-z0-9_]+)`",
                        doc,
                    )
                    if match:
                        model_name = match.group(1)
                        break
                if model_name:
                    break
            if model_name:
                module_name, class_name = model_name.rsplit(".", 1)
                model = getattr(importlib.import_module(module_name), class_name)()
                result = (
                    model_name,
                    dict(getattr(model, "swagger_types", {})),
                    dict(getattr(model, "attribute_map", {})),
                )
                self._model_cache[key] = result
                return result
        except Exception:
            pass
        result = (None, {}, {})
        self._model_cache[key] = result
        return result
