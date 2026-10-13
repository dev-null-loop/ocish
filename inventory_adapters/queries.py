from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import oci

from config import TIME_QUERY_PROVIDERS
from models import ResourceRow, ResourceSpec


class QueryInventoryMixin:
    """Resource Search, logging, audit, metrics, and query adapters."""

    @staticmethod
    def _search_type_for(spec: ResourceSpec) -> str:
        if spec.search_type:
            return spec.search_type
        name = spec.name.replace("-", "_")
        if name.endswith("ies"):
            name = name[:-3] + "y"
        elif name.endswith("s"):
            name = name[:-1]
        return "".join(part.capitalize() for part in name.split("_") if part)

    @staticmethod
    def _search_property_name(field: str) -> str:
        parts = field.split("_")
        return parts[0] + "".join(part.capitalize() for part in parts[1:])

    def list_relationship_projection(
        self, spec: ResourceSpec, relationship_field: str, parent_id: str
    ) -> list[ResourceRow]:
        """Lazily mount a cross-compartment relationship view via OCI Search."""
        key = (self.region(), spec.qualified_name, relationship_field, parent_id)
        cached = self.relationship_projection_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        search_type = self.search_type_for(spec)
        property_name = self._search_property_name(relationship_field)
        literal = parent_id.replace("\\", "\\\\").replace("'", "\\'")
        query = f"query {search_type} resources where {property_name} = '{literal}'"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.resource_search.search_resources,
                details,
            )
            items = self._collection_items(response.data)
        except Exception as exc:
            if getattr(
                exc, "code", None
            ) == "CannotParseRequest" and "Unknown resource type" in str(exc):
                rows = self._list_relationship_projection_fallback(
                    spec, relationship_field, parent_id
                )
                self.relationship_projection_cache[key] = (
                    now + self.PROJECTION_CACHE_TTL_SECONDS,
                    tuple(rows),
                )
                return rows
            raise ValueError(
                f"relationship projection unavailable for {spec.qualified_name}: OCI Search failed ({exc})"
            ) from exc
        rows = self._uniquify_row_names(
            [self._row_from_oci_item(item) for item in items]
        )
        if not rows:
            rows = self._list_relationship_projection_fallback(
                spec, relationship_field, parent_id
            )
        self._remember_row_names(rows)
        self.relationship_projection_cache[key] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            tuple(rows),
        )
        return rows

    def _list_relationship_projection_fallback(
        self, spec: ResourceSpec, relationship_field: str, parent_id: str
    ) -> list[ResourceRow]:
        return [
            row
            for row in self.list_resources(spec.qualified_name)
            if (row.payload or {}).get(
                relationship_field, (row.details or {}).get(relationship_field)
            )
            == parent_id
        ]

    @staticmethod
    def _log_source_resource_id(row: ResourceRow) -> str | None:
        configuration = (row.payload or {}).get("configuration")
        if not isinstance(configuration, dict):
            return None
        source = configuration.get("source")
        if not isinstance(source, dict):
            return None
        resource_id = source.get("resource")
        return resource_id if isinstance(resource_id, str) else None

    def list_tenancy_log_groups(self) -> list[ResourceRow]:
        """Return the tenancy-wide log-group index used by ~/logging."""
        region = self.region()
        cache = getattr(self, "tenancy_log_group_cache", {})
        cached = cache.get(region)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        spec = self.resolve_resource_spec("logging.log-groups")
        query = f"query {self.search_type_for(spec)} resources"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.resource_search.search_resources,
                details,
            )
            rows: list[ResourceRow] = []
            for item in self._collection_items(response.data):
                row = self._row_from_oci_item(item)
                payload = dict(row.payload or {})
                payload["id"] = row.id
                compartment_id = getattr(item, "compartment_id", None) or payload.get(
                    "compartment_id"
                )
                details_map = dict(row.details or {})
                if isinstance(compartment_id, str):
                    details_map["compartment_id"] = compartment_id
                rows.append(
                    ResourceRow(
                        name=row.name,
                        state=row.state,
                        id=row.id,
                        extra=row.extra,
                        details=details_map,
                        payload=payload,
                    )
                )
        except Exception as exc:
            raise ValueError(
                f"tenancy logging index unavailable: OCI Search failed ({exc})"
            ) from exc
        rows = self._uniquify_row_names(rows)
        self._remember_row_names(rows)
        cache[region] = (now + self.PROJECTION_CACHE_TTL_SECONDS, tuple(rows))
        self.tenancy_log_group_cache = cache
        return rows

    @staticmethod
    def _log_entry_time(value: str | None, option: str) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{option} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{option} must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _log_query_literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    @staticmethod
    def _log_entry_name(value: object, index: int) -> str:
        if isinstance(value, (int, float)):
            timestamp = datetime.fromtimestamp(value / 1000, UTC)
            label = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        elif isinstance(value, str) and value:
            label = value.replace("+00:00", "Z")
        else:
            label = "entry"
        return f"{label}-{index + 1}"

    def list_time_query_entries(
        self,
        provider: str,
        parent: ResourceRow | None = None,
        **options: object,
    ) -> list[ResourceRow]:
        """Run one declared time/query provider through the common filesystem view."""
        handlers: dict[str, Callable[..., list[ResourceRow]]] = {
            "logging": self.list_log_entries,
            "audit": self._list_audit_events,
            "monitoring": self._list_monitoring_metrics,
            "apm": self._list_apm_query,
            "log-analytics": self._list_log_analytics_query,
        }
        try:
            handler = handlers[provider]
        except KeyError as exc:
            raise ValueError(f"unknown time-query provider: {provider}") from exc
        return handler(parent, **options)

    @staticmethod
    def _time_query_rows(value: object, *, label: str = "entry") -> list[ResourceRow]:
        """Turn an OCI query response into terminal, raw-payload records."""
        data = getattr(value, "data", value)
        if isinstance(data, (list, tuple)):
            items = list(data)
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            items = list(data["items"])
        elif isinstance(data, dict) and isinstance(data.get("query_result_rows"), list):
            items = list(data["query_result_rows"])
        else:
            items = [data]
        rows: list[ResourceRow] = []
        for index, item in enumerate(items):
            payload = item if isinstance(item, dict) else oci.util.to_dict(item)
            timestamp = (
                payload.get("datetime")
                or payload.get("time")
                or payload.get("event_time")
                or payload.get("time_created")
                or label
            )
            rows.append(
                ResourceRow(
                    name=QueryInventoryMixin._log_entry_name(timestamp, index),
                    state=str(
                        payload.get("lifecycle_state") or payload.get("status") or "-"
                    ),
                    id=f"{label}-{index + 1}",
                    payload=payload,
                )
            )
        return rows

    def _time_window(
        self,
        options: dict[str, object],
        *,
        default_seconds: int,
        maximum_seconds: int | None = None,
    ) -> tuple[datetime, datetime]:
        now = datetime.now(UTC)
        since = options.get("since")
        until = options.get("until")
        start = (
            self._log_entry_time(str(since), "since")
            if since
            else now - timedelta(seconds=default_seconds)
        )
        end = self._log_entry_time(str(until), "until") if until else now
        if start >= end:
            raise ValueError("since must be earlier than until")
        if maximum_seconds and end - start > timedelta(seconds=maximum_seconds):
            raise ValueError(
                f"time range cannot exceed {maximum_seconds // 86400} days"
            )
        return start, end

    def _time_query_page(self, provider: str, options: dict[str, object]) -> int:
        page = int(options.get("page_number", 1))
        maximum = int(TIME_QUERY_PROVIDERS[provider]["max_pages"])
        if not 1 <= page <= maximum:
            raise ValueError(f"page must be between 1 and {maximum}")
        return page

    def _list_audit_events(
        self, _parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        start, end = self._time_window(options, default_seconds=24 * 60 * 60)
        # Audit accepts minute granularity only.  Preserve an inclusive useful
        # default window without exposing invalid seconds to its API.
        start = start.replace(second=0, microsecond=0)
        end = (end + timedelta(minutes=1)).replace(second=0, microsecond=0)
        page_number = self._time_query_page("audit", options)
        try:
            token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                response = self._retry_oci_call(
                    self.audit.list_events,
                    self.current.id,
                    start,
                    end,
                    **({"page": token} if token else {}),
                )
                if current_page == page_number:
                    break
                token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not token:
                    return []
        except Exception as exc:
            raise ValueError(
                f"audit events unavailable: OCI Audit failed ({exc})"
            ) from exc
        return self._time_query_rows(response, label="event")

    def _list_monitoring_metrics(
        self, _parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        query, namespace = options.get("query"), options.get("namespace")
        if (
            not isinstance(query, str)
            or not query
            or not isinstance(namespace, str)
            or not namespace
        ):
            raise ValueError("monitoring requires query and namespace control values")
        start, end = self._time_window(options, default_seconds=60 * 60)
        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace=namespace,
            query=query,
            start_time=start,
            end_time=end,
            resolution=options.get("resolution"),
        )
        try:
            response = self._retry_oci_call(
                self.monitoring.summarize_metrics_data, self.current.id, details
            )
        except Exception as exc:
            raise ValueError(
                f"monitoring metrics unavailable: OCI Monitoring failed ({exc})"
            ) from exc
        return self._time_query_rows(response, label="metric")

    def _list_apm_query(
        self, parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        query = options.get("query")
        if parent is None or not isinstance(query, str) or not query:
            raise ValueError("APM requires an APM domain and a query control value")
        start, end = self._time_window(options, default_seconds=60 * 60)
        details = oci.apm_traces.models.QueryDetails(query_text=query)
        page_number = self._time_query_page("apm", options)
        try:
            token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                kwargs: dict[str, object] = {"limit": 100}
                if token:
                    kwargs["page"] = token
                response = self._retry_oci_call(
                    self.apm_query.query, parent.id, start, end, details, **kwargs
                )
                if current_page == page_number:
                    break
                token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not token:
                    return []
        except Exception as exc:
            raise ValueError(f"APM query unavailable: OCI APM failed ({exc})") from exc
        return self._time_query_rows(response, label="apm-result")

    def _list_log_analytics_query(
        self, _parent: ResourceRow | None, **options: object
    ) -> list[ResourceRow]:
        query = options.get("query")
        if not isinstance(query, str) or not query:
            raise ValueError("Log Analytics requires a query control value")
        start, end = self._time_window(options, default_seconds=60 * 60)
        namespace = self._retry_oci_call(self.object_storage.get_namespace).data
        details = oci.log_analytics.models.QueryDetails(
            compartment_id=self.current.id,
            query_string=query,
            should_run_async=False,
            should_include_total_count=True,
            time_filter=oci.log_analytics.models.TimeRange(
                time_start=start, time_end=end
            ),
        )
        page_number = self._time_query_page("log-analytics", options)
        try:
            token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                kwargs: dict[str, object] = {"limit": 100}
                if token:
                    kwargs["page"] = token
                response = self._retry_oci_call(
                    self.log_analytics.query, namespace, details, **kwargs
                )
                if current_page == page_number:
                    break
                token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not token:
                    return []
        except Exception as exc:
            raise ValueError(
                f"Log Analytics query unavailable: OCI Log Analytics failed ({exc})"
            ) from exc
        return self._time_query_rows(response, label="log-analytics-result")

    def list_log_entries(
        self,
        log: ResourceRow,
        *,
        page_number: int = 1,
        since: str | None = None,
        until: str | None = None,
        contains: str | None = None,
        where: str | None = None,
    ) -> list[ResourceRow]:
        """Read one bounded, filtered page from a configured log's entries."""
        payload = log.payload or {}
        log_group_id = payload.get("log_group_id")
        if not isinstance(log_group_id, str):
            raise ValueError("log entries unavailable: the log group is unknown")
        if not 1 <= page_number <= self.LOG_ENTRY_MAX_PAGE:
            raise ValueError(f"--page must be between 1 and {self.LOG_ENTRY_MAX_PAGE}")
        now = datetime.now(UTC)
        time_start = self._log_entry_time(since, "--since") or (
            now - timedelta(seconds=self.LOG_ENTRY_WINDOW_SECONDS)
        )
        time_end = self._log_entry_time(until, "--until") or now
        if time_start >= time_end:
            raise ValueError("--since must be earlier than --until")
        if time_end - time_start > timedelta(seconds=self.LOG_ENTRY_WINDOW_SECONDS):
            raise ValueError("log entry time range cannot exceed 14 days")
        query = f'search "{self.current.id}/{log_group_id}/{log.id}" '
        if contains:
            query += f"| where logContent = '*{self._log_query_literal(contains)}*' "
        if where:
            field, separator, value = where.partition("=")
            if not separator or not field.strip() or not value:
                raise ValueError("--where must use FIELD=VALUE")
            literal = (
                value
                if re.fullmatch(r"-?\d+(?:\.\d+)?", value)
                or value.lower() in {"true", "false"}
                else f"'{self._log_query_literal(value)}'"
            )
            query += f"| where {field.strip()} = {literal} "
        query += "| sort by datetime desc"
        details = oci.loggingsearch.models.SearchLogsDetails(
            time_start=time_start,
            time_end=time_end,
            search_query=query,
            is_return_field_info=False,
        )
        try:
            page_token: str | None = None
            response: object | None = None
            for current_page in range(1, page_number + 1):
                kwargs: dict[str, object] = {"limit": self.LOG_ENTRY_PAGE_SIZE}
                if page_token:
                    kwargs["page"] = page_token
                response = self._retry_oci_call(
                    self.logging_search.search_logs, details, **kwargs
                )
                if current_page == page_number:
                    break
                page_token = getattr(response, "next_page", None) or getattr(
                    response, "opc_next_page", None
                )
                if not page_token:
                    return []
        except Exception as exc:
            raise ValueError(
                f"log entries unavailable: OCI Logging Search failed ({exc})"
            ) from exc
        rows: list[ResourceRow] = []
        for index, result in enumerate(
            getattr(getattr(response, "data", None), "results", []) or []
        ):
            entry = getattr(result, "data", result)
            entry_payload = (
                entry if isinstance(entry, dict) else oci.util.to_dict(entry)
            )
            timestamp = (
                entry_payload.get("datetime") or entry_payload.get("time") or "entry"
            )
            level = entry_payload.get("level") or entry_payload.get("severity") or "-"
            content = (
                entry_payload.get("logContent") or entry_payload.get("message") or ""
            )
            rows.append(
                ResourceRow(
                    name=self._log_entry_name(timestamp, index),
                    state=str(level),
                    id=f"entry-{index + 1}",
                    extra=str(content),
                    payload=entry_payload,
                )
            )
        return rows

    def _all_log_rows(self) -> list[ResourceRow]:
        """Build the lazy, region-local index behind every resource's logs/ view."""
        region = self.region()
        cached = self.log_relationship_cache.get(region)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return list(cached[1])
        group_spec = self.resolve_resource_spec("logging.log-groups")
        query = f"query {self._search_type_for(group_spec)} resources"
        details = oci.resource_search.models.StructuredSearchDetails(
            type="Structured", query=query
        )
        try:
            response = self._retry_oci_call(
                oci.pagination.list_call_get_all_results,
                self.resource_search.search_resources,
                details,
            )
            groups = self._collection_items(response.data)
            rows: list[ResourceRow] = []
            for group in groups:
                group_id = getattr(group, "identifier", None) or getattr(
                    group, "id", None
                )
                if not isinstance(group_id, str):
                    continue
                log_response = self._retry_oci_call(
                    oci.pagination.list_call_get_all_results,
                    self.logging.list_logs,
                    log_group_id=group_id,
                )
                for item in self._collection_items(log_response.data):
                    row = self._row_from_oci_item(item)
                    payload = dict(row.payload or {})
                    payload["log_group_id"] = group_id
                    details_map = dict(row.details or {})
                    details_map["log_group_id"] = group_id
                    details_map["canonical_path"] = (
                        f"logging/log-groups/{group_id}/logs/{row.name}"
                    )
                    rows.append(
                        ResourceRow(
                            name=row.name,
                            state=row.state,
                            id=row.id,
                            extra=row.extra,
                            details=details_map,
                            payload=payload,
                        )
                    )
        except Exception as exc:
            raise ValueError(
                f"logs projection unavailable: OCI Logging discovery failed ({exc})"
            ) from exc
        rows = self._uniquify_row_names(rows)
        self._remember_row_names(rows)
        self.log_relationship_cache[region] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            tuple(rows),
        )
        return rows

    @staticmethod
    def _payload_resource_ids(value: object) -> set[str]:
        if not isinstance(value, dict):
            return set()
        return {
            item
            for key, item in value.items()
            if key.endswith("_id")
            and isinstance(item, str)
            and item.startswith("ocid1.")
        }

    def _log_source_related_ids(self, source_id: str) -> frozenset[str]:
        key = (self.region(), source_id)
        cached = self.log_source_relation_cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        source_row = self.hydrate_resource_row(ResourceRow(source_id, "-", source_id))
        related = {source_id, *self._payload_resource_ids(source_row.payload)}
        result = frozenset(related)
        self.log_source_relation_cache[key] = (
            now + self.PROJECTION_CACHE_TTL_SECONDS,
            result,
        )
        return result

    def list_related_logs(self, resource_id: str) -> list[ResourceRow]:
        return [
            row
            for row in self._all_log_rows()
            if (source_id := self._log_source_resource_id(row)) is not None
            and resource_id in self._log_source_related_ids(source_id)
        ]
