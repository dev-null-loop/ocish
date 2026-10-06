"""Installed OCI SDK resource discovery."""

from __future__ import annotations

import inspect
import pkgutil

import oci

class OciSdkCatalog:
    """Deterministic installed OCI SDK client/operation catalog."""

    @classmethod
    def discover(cls) -> dict[str, dict[str, tuple[str, ...]]]:
        result: dict[str, dict[str, tuple[str, ...]]] = {}
        for module_info in pkgutil.iter_modules(oci.__path__):
            service = module_info.name
            try:
                module = __import__(f"oci.{service}", fromlist=["*"])
            except Exception:
                continue
            clients: dict[str, tuple[str, ...]] = {}
            for name, client in inspect.getmembers(module, inspect.isclass):
                if not client.__name__.endswith("Client"):
                    continue
                methods = tuple(
                    method_name
                    for method_name, _method in inspect.getmembers(client, inspect.isfunction)
                    if not method_name.startswith("_")
                )
                if methods:
                    clients[name] = methods
            if clients:
                result[service] = clients
        return result
