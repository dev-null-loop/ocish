from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Iterable

import oci

from config import DIRECT_GETTERS, RESOURCE_ENRICHMENTS
from models import ResourceRow, ResourceSpec


class InventoryNamingMixin:
    """Name projection, row matching, hydration, and enrichment helpers."""

    def _remember_row_names(self, rows: Iterable[ResourceRow]) -> None:
        for row in rows:
            payload = row.payload or {}
            row_id = payload.get("id")
            display_name = payload.get("display_name") or payload.get("name")
            if (
                isinstance(row_id, str)
                and isinstance(display_name, str)
                and row_id != display_name
            ):
                self.name_cache[row_id] = self._humanize_name(display_name)




    def _enrich_relationship_names(self, rows: list[ResourceRow]) -> list[ResourceRow]:
        relationship_ids = {
            str(value)
            for row in rows
            for key, value in (row.details or {}).items()
            if key.endswith("_id")
            and isinstance(value, str)
            and value.startswith("ocid1.")
        }
        if not relationship_ids:
            return rows
        try:
            names = self._current_compartment_resource_names()
        except Exception as exc:
            self.record_partial_failure("current_compartment_resource_names", exc)
            names = {}
        enriched: list[ResourceRow] = []
        for row in rows:
            details = dict(row.details or {})
            for key, value in details.items():
                if (
                    key.endswith("_id")
                    and isinstance(value, str)
                    and value.startswith("ocid1.")
                ):
                    details[key] = names.get(value, "-")
            enriched.append(row.with_details(details))
        return enriched


    def _apply_resource_enrichments(
        self, spec: ResourceSpec, rows: list[ResourceRow]
    ) -> list[ResourceRow]:
        """Apply declarative per-resource presentation enrichments."""
        for rule in RESOURCE_ENRICHMENTS.get(spec.qualified_name, ()):
            client = getattr(self, str(rule["client_attr"]))
            getter = getattr(client, str(rule["getter"]))
            detail = str(rule["detail"])
            response_attr = str(rule["response_attr"])
            fallback = rule.get("fallback", "-")
            enriched: list[ResourceRow] = []
            for index, row in enumerate(rows):
                try:
                    response = self._retry_oci_call(
                        getter,
                        row.id,
                        retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY,
                    ).data
                    value = str(getattr(response, response_attr, fallback) or fallback)
                except Exception as exc:
                    self.record_partial_failure("resource_enrichment", exc)
                    value = str(fallback)
                enriched.append(row.with_detail(detail, value))
                if index + 1 < len(rows):
                    time.sleep(float(rule.get("delay_seconds", 0)))
            rows = enriched
        return rows

    @staticmethod

    def _humanize_name(value: object) -> str:
        return urllib.parse.unquote(str(value))

    @staticmethod

    def _row_display_name(row: ResourceRow) -> str:
        payload = row.payload or {}
        return InventoryNamingMixin._humanize_name(
            payload.get("display_name") or payload.get("name") or row.name
        )

    @classmethod

    def _matching_rows(
        cls, rows: Iterable[ResourceRow], target: str
    ) -> list[ResourceRow]:
        matches: list[ResourceRow] = []
        for row in rows:
            raw_display_name = str(
                (row.payload or {}).get("display_name")
                or (row.payload or {}).get("name")
                or row.name
            )
            if target in {
                row.name,
                row.id,
                raw_display_name,
                cls._row_display_name(row),
            }:
                matches.append(row)
        return matches

    @classmethod

    def _ambiguous_row_error(
        cls, resource_type: str, target: str, matches: list[ResourceRow]
    ) -> str:
        candidates = ", ".join(row.name for row in matches[:5])
        more = "" if len(matches) <= 5 else ", ..."
        return f"ambiguous {resource_type}: {target} (use one of: {candidates}{more})"

    @staticmethod

    def _row_name_suffix(row: ResourceRow) -> str:
        candidate = row.id if row.id not in ("", "-", row.name) else ""
        if candidate.startswith("ocid1."):
            tail = candidate.rsplit(".", 1)[-1]
            return tail[-8:]
        normalized = re.sub(r"[^a-zA-Z0-9]+", "-", candidate).strip("-")
        if normalized:
            return normalized[-8:]
        return "dup"

    @staticmethod

    def _sanitize_row_name(name: str) -> str:
        sanitized = urllib.parse.quote(name, safe="A-Za-z0-9._:@-")
        return sanitized or "unnamed"

    @classmethod

    def _uniquify_row_names(cls, rows: list[ResourceRow]) -> list[ResourceRow]:
        base_names = [cls._sanitize_row_name(row.name) for row in rows]
        counts: dict[str, int] = {}
        for base_name in base_names:
            counts[base_name] = counts.get(base_name, 0) + 1
        if all(count == 1 for count in counts.values()) and all(
            row.name == base_name
            for row, base_name in zip(rows, base_names, strict=False)
        ):
            return rows
        used: set[str] = set()
        result: list[ResourceRow] = []
        for row, base_name in zip(rows, base_names, strict=False):
            candidate = base_name
            if counts[base_name] > 1:
                suffix = cls._row_name_suffix(row)
                candidate = f"{base_name}@{suffix}"
            counter = 2
            while candidate in used:
                candidate = f"{base_name}@{cls._row_name_suffix(row)}-{counter}"
                counter += 1
            result.append(
                ResourceRow(
                    name=candidate,
                    state=row.state,
                    id=row.id,
                    extra=row.extra,
                    details=row.details,
                    payload=row.payload,
                )
            )
            used.add(candidate)
        return result


    def _resolve_name_uncached(self, ocid: str) -> str:
        try:
            details = oci.resource_search.models.GetResourceSummaryUsingOCIDDetails(
                ocid=ocid
            )
            data = self.resource_search.get_resource_summary_using_ocid(details).data
            name = getattr(data, "display_name", None) or getattr(
                data, "identifier", None
            )
            if name:
                return str(name)
        except Exception as exc:
            self.record_partial_failure("resolve_resource_name", exc)
        direct = self._resolve_name_via_direct_get(ocid)
        if direct is not None:
            return direct
        return ocid


    def _resolve_name_via_direct_get(self, ocid: str) -> str | None:
        match = re.match(r"^ocid1\.([^.]+)\.", ocid)
        if not match:
            return None
        kind = match.group(1)
        getter_ref = DIRECT_GETTERS.get(kind)
        if getter_ref is None:
            return None
        try:
            client_attr, method_name = getter_ref
            client = getattr(self, client_attr)
            data = getattr(client, method_name)(ocid).data
        except Exception as exc:
            self.record_partial_failure("resolve_resource_name_direct", exc)
            return None
        name = getattr(data, "display_name", None) or getattr(data, "name", None)
        return str(name) if name else None


    def hydrate_resource_row(self, row: ResourceRow) -> ResourceRow:
        """Turn a search-summary projection row into the resource's full row on entry."""
        match = re.match(r"^ocid1\.([^.]+)\.", row.id)
        if match is None:
            return row
        getter_ref = DIRECT_GETTERS.get(match.group(1))
        if getter_ref is None:
            return row
        try:
            client_attr, method_name = getter_ref
            item = getattr(getattr(self, client_attr), method_name)(row.id).data
        except Exception as exc:
            self.record_partial_failure("hydrate_resource_row", exc)
            return row
        return self._row_from_oci_item(item)


    def _resolve_payload_ids(self, value: object, key: str | None = None) -> object:
        if isinstance(value, dict):
            return {
                item_key: self._resolve_payload_ids(item, item_key)
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_payload_ids(item, key) for item in value]
        if (
            isinstance(value, str)
            and value.startswith("ocid1.")
            and key not in {"id", "identifier"}
        ):
            return self.resolve_name(value)
        return value
