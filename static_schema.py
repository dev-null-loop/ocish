"""Static, SDK-faithful OCI schema tree; intentionally independent of tenancy data."""

from __future__ import annotations

from sdk_catalog import OciSdkCatalog


class OciSdkSchemaTree:
    """Expose installed OCI SDK clients and operations as a read-only tree."""

    def __init__(self, _specs: object = None) -> None:
        self._sdk_resources = OciSdkCatalog.discover()

    def children(self, path: tuple[str, ...]) -> list[str]:
        if not path:
            return sorted(self._sdk_resources)
        if len(path) == 1:
            return sorted(self._sdk_resources.get(path[0], {}))
        if len(path) == 2 and self._methods(path):
            return ["metadata", "operations"]
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
                "kind": "sdk-client",
                "provider": "oci",
                "native_service": path[0],
                "sdk_client": path[1],
                "path": "/oci/" + "/".join(path),
            }
        if len(path) == 4 and path[2] == "operations" and path[3] in methods:
            return {
                "kind": "operation",
                "client": ".".join(path[:2]),
                "sdk_method": path[3],
            }
        leaf = path[2]
        if leaf == "operations":
            return {
                "kind": "operations",
                "client": ".".join(path[:2]),
                "sdk_methods": list(methods),
            }
        if leaf == "metadata":
            return {
                "kind": "metadata",
                "provider": "oci",
                "native_service": path[0],
                "sdk_client": path[1],
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
                f"/oci/{service}/{client}"
                for service in self.children(())
                for client in self.children((service,))
            ]
        if len(path) == 1:
            return [f"/oci/{path[0]}/{client}" for client in self.children(path)]
        return ["/oci/" + "/".join(path)] if self._methods(path) else []

    def _methods(self, path: tuple[str, ...]) -> tuple[str, ...] | None:
        return (
            self._sdk_resources.get(path[0], {}).get(path[1])
            if len(path) >= 2
            else None
        )
