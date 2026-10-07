from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from config import RESOURCE_CONTEXT_CHILDREN, SUPPORT_CLIENTS, TOPOLOGY_EDGES
from inventory import OciCompartmentBrowser
from models import ResourceChildCollection, ResourceRow, ResourceSpec


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
