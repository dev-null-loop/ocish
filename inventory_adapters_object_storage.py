from __future__ import annotations

import oci

from models import ResourceRow


class ObjectStorageInventoryMixin:
    """Object Storage namespace and object-body operations."""

    def _object_storage_namespace(self) -> str:
        try:
            return str(self._retry_oci_call(self.object_storage.get_namespace).data)
        except Exception as exc:
            raise ValueError(f"object storage namespace unavailable: {exc}") from exc

    def _list_object_storage_buckets(
        self, _kwargs: dict[str, object]
    ) -> list[ResourceRow]:
        namespace = self._object_storage_namespace()
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.object_storage.list_buckets,
                namespace,
                self.current.id,
            )
        except Exception as exc:
            raise ValueError(f"object storage buckets unavailable: {exc}") from exc
        rows: list[ResourceRow] = []
        for item in self._collection_items(response.data):
            payload = oci.util.to_dict(item)
            name = str(payload.get("name") or "bucket")
            rows.append(
                ResourceRow(
                    name=name,
                    state=str(payload.get("public_access_type") or "-"),
                    id=f"bucket:{namespace}:{name}",
                    payload=payload,
                    details={"namespace": namespace, "bucket_name": name},
                )
            )
        return rows

    def list_object_prefix(
        self, bucket: ResourceRow, prefix: str = ""
    ) -> list[ResourceRow]:
        """List one Object Storage pseudo-directory without downloading bodies."""
        namespace = str(
            (bucket.details or {}).get("namespace") or self._object_storage_namespace()
        )
        bucket_name = str((bucket.details or {}).get("bucket_name") or bucket.name)
        try:
            response: object | None = None
            objects: list[object] = []
            prefixes: set[str] = set()
            start: str | None = None
            for _page in range(self.OBJECT_LIST_MAX_PAGES):
                kwargs: dict[str, object] = {
                    "prefix": prefix,
                    "delimiter": "/",
                    "limit": 1000,
                }
                if start:
                    kwargs["start"] = start
                response = self._retry_oci_call(
                    self.object_storage.list_objects, namespace, bucket_name, **kwargs
                )
                data = getattr(response, "data", response)
                objects.extend(getattr(data, "objects", []) or [])
                prefixes.update(
                    str(item) for item in (getattr(data, "prefixes", []) or [])
                )
                start = getattr(data, "next_start_with", None)
                if not start:
                    break
            if start:
                raise ValueError(
                    f"object listing exceeds {self.OBJECT_LIST_MAX_PAGES * 1000} entries; descend into a prefix"
                )
        except Exception as exc:
            raise ValueError(f"object storage objects unavailable: {exc}") from exc
        rows: list[ResourceRow] = []
        for common_prefix in prefixes:
            child_prefix = str(common_prefix)
            label = child_prefix[len(prefix) :].rstrip("/") or child_prefix.rstrip("/")
            rows.append(
                ResourceRow(
                    name=label,
                    state="directory",
                    id=f"prefix:{child_prefix}",
                    details={"object_prefix": child_prefix, "object_directory": "true"},
                    payload={"prefix": child_prefix},
                )
            )
        for item in objects:
            payload = oci.util.to_dict(item)
            object_name = str(payload.get("name") or "object")
            label = object_name.removeprefix(prefix)
            rows.append(
                ResourceRow(
                    name=label,
                    state=str(payload.get("storage_tier") or "-"),
                    id=f"object:{namespace}:{bucket_name}:{object_name}",
                    payload=payload,
                    details={
                        "namespace": namespace,
                        "bucket_name": bucket_name,
                        "object_name": object_name,
                    },
                )
            )
        return sorted(
            rows, key=lambda row: (row.state != "directory", row.name.casefold())
        )

    def read_object_content(self, object_row: ResourceRow) -> str:
        """Read only small, declared-text Object Storage bodies."""
        details = object_row.details or {}
        namespace = str(details["namespace"])
        bucket = str(details["bucket_name"])
        name = str(details["object_name"])
        response = self._retry_oci_call(
            self.object_storage.get_object, namespace, bucket, name
        )
        headers = getattr(response, "headers", {}) or {}
        length = int(
            headers.get("content-length", headers.get("Content-Length", 0)) or 0
        )
        content_type = str(
            headers.get("content-type", headers.get("Content-Type", ""))
        ).lower()
        if length > self.CONTENT_MAX_BYTES:
            raise ValueError(
                f"object content is {length} bytes; maximum readable size is {self.CONTENT_MAX_BYTES}"
            )
        if content_type and not (
            content_type.startswith("text/")
            or "json" in content_type
            or "xml" in content_type
        ):
            raise ValueError(
                f"object content type {content_type} is not text; inspect metadata instead"
            )
        raw = getattr(getattr(response, "data", None), "content", None)
        if raw is None:
            raw = getattr(getattr(response, "data", None), "raw", None)
            raw = raw.read(self.CONTENT_MAX_BYTES + 1) if raw is not None else b""
        if isinstance(raw, str):
            return raw
        if len(raw) > self.CONTENT_MAX_BYTES:
            raise ValueError(
                f"object content exceeds maximum readable size of {self.CONTENT_MAX_BYTES}"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "object content is not UTF-8 text; inspect metadata instead"
            ) from exc
