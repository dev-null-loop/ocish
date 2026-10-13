from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from config import RESOURCE_CONTEXT_CHILDREN, SUPPORT_CLIENTS, TOPOLOGY_EDGES
from inventory import OciCompartmentBrowser
from models import (
    CompartmentNode,
    RelationshipTarget,
    ResourceChildCollection,
    ResourceContext,
    ResourceRow,
    ResourceSpec,
)
from relationships import RelationshipNamespace


def _child_resource_type(child: object) -> str:
    if isinstance(child, ResourceChildCollection):
        return child.resource_type
    if isinstance(child, tuple) and child and isinstance(child[0], str):
        return child[0]
    raise TypeError(f"unsupported child collection declaration: {child!r}")


def _public_spellings(spec: ResourceSpec) -> Iterator[str]:
    yield spec.qualified_name
    yield f"{spec.namespace}.{spec.name.replace('_', '-')}"


def test_every_registered_resource_has_a_resolvable_public_path(browser) -> None:
    for spec in browser.resource_specs:
        for spelling in _public_spellings(spec):
            assert browser.resolve_resource_spec(spelling) == spec


def test_resource_registry_has_no_duplicate_qualified_names(browser) -> None:
    names = [spec.qualified_name for spec in browser.resource_specs]
    assert len(names) == len(set(names))


def test_declared_oci_clients_are_importable() -> None:
    clients = OciCompartmentBrowser._declared_client_classes()

    assert clients.items() >= SUPPORT_CLIENTS.items()
    for class_path in clients.values():
        module_name, class_name = class_path.rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), class_name)


def test_file_storage_registry_exposes_required_read_only_collections(browser) -> None:
    expected = {
        "file_storage.file_systems",
        "file_storage.mount_targets",
        "file_storage.exports",
        "file_storage.snapshots",
        "file_storage.work_requests",
        "file_storage.work_request_errors",
        "file_storage.work_request_logs",
    }

    assert expected <= {spec.qualified_name for spec in browser.resource_specs}
    file_systems = browser.resolve_resource_spec("file_storage.file_systems")
    assert file_systems.runnable
    assert dict(file_systems.adapter_config)["availability_domain_mode"] == "all"


def test_availability_domain_adapter_lists_file_storage_in_every_domain(
    browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response:
        def __init__(self, data: object) -> None:
            self.data = data

    class Identity:
        @staticmethod
        def list_availability_domains(_tenancy_id: str) -> object:
            raise AssertionError("pagination wrapper should intercept this call")

    class FileStorage:
        @staticmethod
        def list_file_systems(**_kwargs: object) -> object:
            raise AssertionError("pagination wrapper should intercept this call")

    calls: list[dict[str, object]] = []

    def paginate(operation: object, *args: object, **kwargs: object) -> Response:
        if operation is browser.identity.list_availability_domains:
            assert args == (browser.tenancy_id,)
            return Response(
                [
                    type("AvailabilityDomain", (), {"name": "AD-1"})(),
                    type("AvailabilityDomain", (), {"name": "AD-2"})(),
                ]
            )
        assert operation is browser.file_storage.list_file_systems
        calls.append(dict(kwargs))
        return Response([])

    browser.current = CompartmentNode("compartment", "app", None, "ACTIVE", None)
    browser.tenancy_id = "ocid1.tenancy.oc1..example"
    browser.identity = Identity()
    browser.file_storage = FileStorage()
    browser._retry_oci_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    browser._row_from_oci_item = lambda item: item
    monkeypatch.setattr("inventory.oci.pagination.list_call_get_all_results", paginate)

    browser._list_generic_resource(
        browser.resolve_resource_spec("file_storage.file_systems")
    )

    assert calls == [
        {"compartment_id": "compartment", "availability_domain": "AD-1"},
        {"compartment_id": "compartment", "availability_domain": "AD-2"},
    ]


def test_client_rebuild_uses_declared_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = OciCompartmentBrowser.__new__(OciCompartmentBrowser)
    browser.config = {"region": "eu-frankfurt-1"}
    constructed: list[tuple[str, dict[str, str]]] = []

    class Client:
        def __init__(self, config: dict[str, str]) -> None:
            constructed.append((type(self).__name__, config))

    class Module:
        def __getattr__(self, class_name: str) -> type[Client]:
            return type(class_name, (Client,), {})

    monkeypatch.setattr(
        OciCompartmentBrowser,
        "_declared_client_classes",
        staticmethod(lambda: {"one": "module.One", "two": "module.Two"}),
    )
    monkeypatch.setattr("inventory.importlib.import_module", lambda _name: Module())

    browser._rebuild_clients()

    assert isinstance(browser.one, Client)
    assert isinstance(browser.two, Client)
    assert [name for name, _config in constructed] == ["One", "Two"]


def test_declared_child_collections_reference_registered_resources(browser) -> None:
    for parent, children in RESOURCE_CONTEXT_CHILDREN.items():
        browser.resolve_resource_spec(parent)
        for child in children.values():
            resource_type = _child_resource_type(child)
            qualified = (
                resource_type
                if "." in resource_type
                else f"{parent.partition('.')[0]}.{resource_type}"
            )
            browser.resolve_resource_spec(qualified)


def test_topology_edges_reference_registered_resources(browser) -> None:
    for source, edges in TOPOLOGY_EDGES.items():
        browser.resolve_resource_spec(source)
        for edge in edges:
            browser.resolve_resource_spec(str(edge["target"]))
            assert edge["kind"] in {"direct", "derived"}
            assert edge["role"]
            if edge["kind"] == "direct":
                assert edge["field"]
            else:
                browser.resolve_resource_spec(str(edge["via"]))
                assert edge["source_field"]
                assert edge["target_field"]


@pytest.mark.parametrize(
    ("input_name", "expected"),
    [
        ("load-balancer.load-balancers", "load_balancer.load_balancers"),
        ("network-firewall.network-firewalls", "network_firewall.network_firewalls"),
        ("generative-ai.projects", "generative_ai.projects"),
    ],
)
def test_documented_hyphenated_namespace_paths_resolve(
    browser, input_name: str, expected: str
) -> None:
    assert browser.resolve_resource_spec(input_name).qualified_name == expected


@pytest.mark.parametrize(
    ("compartment_id", "expected"),
    [
        ("ocid1.compartment.oc1..owned", "core.custom-images"),
        (None, "core.platform-images"),
    ],
)
def test_search_canonicalization_is_registry_declared(
    browser, compartment_id: str | None, expected: str
) -> None:
    browser.tenancy_id = "ocid1.tenancy.oc1..tenant"
    browser.hydrate_resource_row = lambda row: row
    row = ResourceRow(
        name="image",
        state="AVAILABLE",
        id="ocid1.image.oc1..example",
        details={"compartment_id": compartment_id},
    )
    spec = browser.resolve_resource_spec("core.images")

    assert dict(spec.adapter_config)["canonical_search_field"] == "compartment_id"
    assert browser.canonical_spec_for_search_row(spec, row).qualified_name == expected


def test_compartment_image_discovery_uses_the_custom_images_collection(
    browser, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = type(
        "SearchImage",
        (),
        {
            "resource_type": "Image",
            "compartment_id": "ocid1.compartment.oc1..owned",
            "display_name": "golden-image",
            "identifier": "ocid1.image.oc1..golden",
            "lifecycle_state": "AVAILABLE",
        },
    )()

    class Response:
        data = type("Data", (), {"items": [image], "opc_next_page": None})()

    browser.current = CompartmentNode("owned", "app", None, "ACTIVE", None)
    browser.config = {"region": "eu-frankfurt-1"}
    browser.tenancy_id = "ocid1.tenancy.oc1..tenant"
    browser.resource_search = type("ResourceSearch", (), {"search_resources": object()})()
    browser.search_type_cache = {}
    browser._retry_oci_call = lambda *_args, **_kwargs: Response()
    browser._direct_catalog_resources = lambda _compartment_id: []
    monkeypatch.setattr(
        "inventory.oci.util.to_dict",
        lambda item: {
            "resource_type": item.resource_type,
            "compartment_id": item.compartment_id,
            "display_name": item.display_name,
            "identifier": item.identifier,
            "lifecycle_state": item.lifecycle_state,
        },
    )

    discovered = browser.list_current_compartment_resource_types()

    assert [(spec.qualified_name, count) for spec, count in discovered] == [
        ("core.custom-images", 1)
    ]


def test_declared_topology_edge_becomes_a_canonical_relationship() -> None:
    cluster_spec = ResourceSpec("containerengine", "clusters", "", "compartment")
    subnet_spec = ResourceSpec("core", "subnets", "", "compartment")
    subnet_row = ResourceRow("api-subnet", "AVAILABLE", "ocid1.subnet.oc1..api")
    target = RelationshipTarget(subnet_spec, subnet_row, "ocid1.compartment.oc1..app")

    class Browser:
        @staticmethod
        def resolve_resource_spec(resource_type: str) -> ResourceSpec:
            assert resource_type == "core.subnets"
            return subnet_spec

        @staticmethod
        def resolve_resource_id(spec: ResourceSpec, ocid: str) -> RelationshipTarget:
            assert spec == subnet_spec
            assert ocid == subnet_row.id
            return target

        @staticmethod
        def relationship_target(_ocid: str) -> None:
            return None

        @staticmethod
        def record_partial_failure(_operation: str, _exc: Exception) -> None:
            raise AssertionError("declared relationship resolution should succeed")

    class Shell:
        browser = Browser()

    cluster = ResourceContext(
        cluster_spec,
        ResourceRow(
            "prod-oke",
            "ACTIVE",
            "ocid1.cluster.oc1..prod",
            payload={"endpoint_config": {"subnet_id": subnet_row.id}},
        ),
    )

    assert RelationshipNamespace(Shell()).direct(cluster.row.payload, cluster) == [
        ("api-endpoint", target)
    ]
