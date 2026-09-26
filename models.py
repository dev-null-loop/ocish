from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum


@dataclass(frozen=True)
class CompartmentNode:
    id: str
    name: str
    description: str | None
    lifecycle_state: str
    parent_id: str | None


@dataclass(frozen=True)
class ResourceRow:
    name: str
    state: str
    id: str
    extra: str | None = None
    details: dict[str, object] | None = None
    payload: dict[str, object] | None = None

    def with_details(self, details: Mapping[str, object]) -> ResourceRow:
        """Return an immutable row with replacement presentation details."""
        return replace(self, details=dict(details))

    def with_detail(self, key: str, value: object) -> ResourceRow:
        return self.with_details({**(self.details or {}), key: value})


@dataclass(frozen=True)
class ResourceSpec:
    namespace: str
    name: str
    endpoint_family: str
    scope: str
    lister_name: str | None = None
    client_attr: str | None = None
    client_name: str | None = None
    list_operation: str | None = None
    required_params: tuple[str, ...] = ()
    accepted_params: tuple[str, ...] = ()
    runnable: bool = True
    findable: bool = True
    search_type: str | None = None
    node_capability: str = "navigable-resource"
    adapter_kind: str = "resource-tree"

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}"


@dataclass(frozen=True)
class ResourceContext:
    spec: ResourceSpec
    row: ResourceRow

    @property
    def label(self) -> str:
        return self.spec.name.rstrip("s").replace("_", "-")


class VirtualKind(StrEnum):
    RELATIONSHIPS = "relationships"
    TENANCY_LOG_GROUPS = "tenancy-log-groups"
    TIME_QUERY = "time-query"
    TIME_QUERY_SELECTOR = "time-query-selector"
    RELATED_LOGS = "related-logs"
    OBJECT_PREFIXES = "object-prefixes"
    OBJECT_CONTENT = "object-content"
    BLOCKERS = "blockers"


@dataclass(frozen=True)
class RelationshipTarget:
    spec: ResourceSpec | None
    row: ResourceRow
    compartment_id: str


@dataclass(frozen=True)
class CollectionContext:
    spec: ResourceSpec
    collection_name: str
    extra_kwargs: tuple[tuple[str, str], ...] = ()
    row_filter: tuple[str, str] | None = None
    parent_resource: ResourceContext | None = None
    # Nested virtual collections retain the collection that owns their parent
    # resource so their locator remains on the canonical containment path.
    parent_collection: CollectionContext | None = None
    projection_field: str | None = None
    virtual_kind: VirtualKind | None = None
    relationship_targets: tuple[RelationshipTarget, ...] = ()
    time_query_provider: str | None = None
    time_query_options: tuple[tuple[str, object], ...] = ()
    time_query_path: tuple[str, ...] = ()
    time_query_pending: str | None = None
    collection_view_options: tuple[tuple[str, object], ...] = ()
    collection_view_path: tuple[str, ...] = ()
    collection_view_pending: str | None = None
    content_prefix: str = ""
    previous_collection: CollectionContext | None = None

    @property
    def label(self) -> str:
        return self.collection_name


@dataclass(frozen=True)
class ResourceChildCollection:
    resource_type: str
    api_kwargs: tuple[tuple[str, str], ...] = ()
    row_filter_key: str | None = None

    @property
    def projection_field(self) -> str | None:
        return next(
            (
                parameter
                for parameter, source in self.api_kwargs
                if source == "id" and parameter.endswith("_id")
            ),
            self.row_filter_key,
        )


@dataclass(frozen=True)
class ResolvedNamespaceNode:
    kind: str
    value: object | None = None


@dataclass(frozen=True)
class ResourceContainerPolicy:
    root: str
    parent_paths: tuple[str, ...] = ()
    projection_paths: tuple[str, ...] = ()

    @property
    def permits_flat_access(self) -> bool:
        return not self.parent_paths


@dataclass(frozen=True)
class NamespaceEntry:
    name: str
    kind: str


@dataclass(frozen=True)
class MountCollectionContext:
    name: str


@dataclass(frozen=True)
class MountEntryContext:
    collection: str
    name: str


@dataclass(frozen=True)
class MountLeafContext:
    collection: str
    entry: str
    name: str


@dataclass(frozen=True)
class TopologyContext:
    """Navigation state for the non-owning network topology projection."""

    level: str
    vcn: ResourceRow | None = None
    subnet: ResourceRow | None = None
    embedded: bool = False
    namespace: str | None = None
    collection_spec: ResourceSpec | None = None


@dataclass(frozen=True)
class NodeState:
    kind: str
    path_suffix: str


@dataclass(frozen=True)
class DeleteSpec:
    resource_type: str
    client_attr: str
    method_name: str
    arg_name: str
    preview_note: str | None = None
    recursive_supported: bool = False


@dataclass(frozen=True)
class ResolvedRmTarget:
    path: str
    resource_context: ResourceContext
    compartment_current: CompartmentNode
    compartment_parents: tuple[CompartmentNode, ...]
