#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout, suppress
from types import SimpleNamespace

import config
import main
from sdk_catalog import OciSdkCatalog
from terraform_schema_overlay import apply as apply_terraform_overlay


def build_browser() -> main.OciCompartmentBrowser:
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.resource_specs = browser._build_resource_specs()
    return browser


def audit_core_child_mappings() -> list[dict[str, object]]:
    browser = build_browser()
    report: list[dict[str, object]] = []
    for parent, children in sorted(config.RESOURCE_CONTEXT_CHILDREN.items()):
        if not parent.startswith("core."):
            continue
        for child_name, (resource_type, api_kwargs, row_filter_key) in sorted(
            children.items()
        ):
            try:
                spec = browser.resolve_resource_spec(resource_type)
                report.append(
                    {
                        "parent": parent,
                        "child": child_name,
                        "resource_type": resource_type,
                        "status": "ok",
                        "qualified_name": spec.qualified_name,
                        "scope": spec.scope,
                        "runnable": spec.runnable,
                        "list_operation": spec.list_operation,
                        "lister_name": spec.lister_name,
                        "api_kwargs": list(api_kwargs),
                        "row_filter_key": row_filter_key,
                    }
                )
            except Exception as exc:
                report.append(
                    {
                        "parent": parent,
                        "child": child_name,
                        "resource_type": resource_type,
                        "status": "missing",
                        "error": str(exc),
                        "api_kwargs": list(api_kwargs),
                        "row_filter_key": row_filter_key,
                    }
                )
    return report


def audit_compartment_ls_resource_types() -> dict[str, object]:
    browser = build_browser()
    browser.current = SimpleNamespace(id="ocid1.compartment.example")
    browser.region = lambda: "eu-frankfurt-1"
    browser.compartment_resource_type_cache = {}
    browser.list_children = lambda _id: [
        main.CompartmentNode(
            "ocid1.compartment.child", "bd", "development", "ACTIVE", None
        )
    ]
    instance_type = browser._search_type_for(
        browser.resolve_resource_spec("core.instances")
    )
    vcn_type = browser._search_type_for(browser.resolve_resource_spec("core.vcns"))
    child_only_type = browser._search_type_for(
        browser.resolve_resource_spec("core.provider_remote_regions")
    )

    class Search:
        def search_resources(self, _details: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                data=SimpleNamespace(
                    items=[
                        {"resource_type": instance_type},
                        {"resource_type": instance_type},
                        {"resource_type": vcn_type},
                        {"resource_type": child_only_type},
                    ],
                    opc_next_page=None,
                )
            )

    browser.resource_search = Search()
    browser._retry_oci_call = lambda _func, *_args, **_kwargs: (
        Search().search_resources(None)
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.mount_leaf = None
    shell.mount_collection = None
    shell.collection_context = None
    shell.resource_context = None
    shell.namespace_view = None
    shell._current_node_state = lambda: SimpleNamespace(kind="compartment")
    shell._resource_hierarchy = main.ResourceHierarchy(
        browser, shell._build_resource_context_children()
    )
    output = io.StringIO()
    with redirect_stdout(output):
        shell._list_current_node(False)
    lines = output.getvalue().splitlines()
    browser.list_children = lambda _id: []
    empty_output = io.StringIO()
    with redirect_stdout(empty_output):
        shell._list_current_node(False)
    browser.list_children = lambda _id: [
        main.CompartmentNode(
            "ocid1.compartment.child", "bd", "development", "ACTIVE", None
        )
    ]
    long_output = io.StringIO()
    with redirect_stdout(long_output):
        shell._list_current_node(True)
    ok = (
        lines == ["bd", "core.instances", "core.vcns", "identity.compartments"]
        and "identity.compartments" not in empty_output.getvalue().splitlines()
        and "Name" in long_output.getvalue()
        and "compartment" in long_output.getvalue()
        and "collection" in long_output.getvalue()
    )
    return {
        "spelling": "compartment ls shows present resource types",
        "status": "ok" if ok else "missing",
        "value": lines,
    }


def audit_namespace_view_uses_current_compartment_inventory() -> dict[str, object]:
    """`cd core` must expose only the current compartment's core collections."""
    browser = build_browser()
    browser.root = main.CompartmentNode(
        "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
    )
    browser.current = main.CompartmentNode(
        "ocid1.compartment.dev", "dev", None, "ACTIVE", "ocid1.tenancy.example"
    )
    browser.parents = [browser.root]
    browser.region = lambda: "eu-frankfurt-1"
    instances = browser.resolve_resource_spec("core.instances")
    vcns = browser.resolve_resource_spec("core.vcns")
    users = browser.resolve_resource_spec("identity.users")
    browser.compartment_resource_type_cache = {
        ("eu-frankfurt-1", browser.current.id): (
            9999999999.0,
            ((instances, 1), (vcns, 1), (users, 1)),
        )
    }
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = None
    shell.collection_context = None
    shell.resource_context = None
    shell.mount_collection = None
    shell.mount_entry = None
    shell.mount_leaf = None
    shell.topology_context = None
    shell.schema_path = ()
    shell.session_region = "eu-frankfurt-1"
    shell._resource_hierarchy = main.ResourceHierarchy(
        browser, shell._build_resource_context_children()
    )
    shell._change_locator("core")
    output = io.StringIO()
    with redirect_stdout(output):
        shell._list_current_node(False)
    listed = output.getvalue().splitlines()
    completed = shell._context_resource_completions("in")
    path_completed = shell._complete_namespace_path("core/in")
    stat_children = shell._stat_children(main.NodeState("domain", "/core"))
    ok = (
        shell.namespace_view == "core"
        and listed == ["topology", "instances", "vcns"]
        and completed == ["instances"]
        and path_completed == ["instances"]
        and [child["name"] for child in stat_children]
        == ["instances", "vcns", "topology"]
    )
    return {
        "spelling": "namespace view lists and completes current-compartment collections",
        "status": "ok" if ok else "missing",
    }


def audit_bare_compartment_completion() -> dict[str, object]:
    """Bare `cd <prefix>` completion includes catalog child compartments."""
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        current=SimpleNamespace(id="ocid1.compartment.oc1..root"),
        region=lambda: "eu-frankfurt-1",
    )
    shell.catalog = SimpleNamespace(
        is_ready=lambda: True,
        children_for=lambda _id: (
            main.CompartmentNode("dev", "dev", None, "ACTIVE", "root"),
            main.CompartmentNode("fvass", "fvass", None, "ACTIVE", "root"),
        ),
    )
    shell.namespace_view = None
    shell.collection_context = None
    shell.resource_context = None
    ok = shell._complete_current_compartment_children("f") == ["fvass"]
    return {
        "spelling": "bare compartment completion uses catalog",
        "status": "ok" if ok else "missing",
    }


def audit_identity_compartment_entry_changes_context() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("identity.compartments")
    row = main.ResourceRow("fvass", "ACTIVE", "ocid1.compartment.fvass")
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = "identity"
    shell.collection_context = main.CollectionContext(spec, "compartments")
    shell.resource_context = None
    shell._collection_rows = lambda: [row]
    browser._matching_rows = lambda _rows, target: [row] if target == "fvass" else []
    changed_to: list[str] = []
    browser.change_to_compartment = changed_to.append
    entered = shell._try_enter_resource_from_collection("fvass")
    ok = (
        entered
        and changed_to == [row.id]
        and shell.namespace_view is None
        and shell.collection_context is None
        and shell.resource_context is None
    )
    return {
        "spelling": "identity compartment entry changes namespace context",
        "status": "ok" if ok else "missing",
    }


def audit_core_resource_resolution() -> list[dict[str, object]]:
    browser = build_browser()
    report: list[dict[str, object]] = []
    core_specs = [spec for spec in browser.resource_specs if spec.namespace == "core"]
    spellings = {
        spelling
        for spec in core_specs
        for spelling in (
            f"core.{spec.name}",
            f"core.{spec.name.replace('_', '-')}",
            f"core.{spec.name.replace('_', '')}",
        )
    }
    spellings.update({"core.ipsec", "core.ipsec_connections", "core.public-ip"})
    for spelling in sorted(spellings):
        try:
            spec = browser.resolve_resource_spec(spelling)
            report.append(
                {
                    "spelling": spelling,
                    "status": "ok",
                    "qualified_name": spec.qualified_name,
                }
            )
        except Exception as exc:
            report.append(
                {"spelling": spelling, "status": "missing", "error": str(exc)}
            )
    return report


def audit_dns_resource_resolution() -> list[dict[str, object]]:
    browser = build_browser()
    dns_specs = [spec for spec in browser.resource_specs if spec.namespace == "dns"]
    spellings = {
        spelling
        for spec in dns_specs
        for spelling in (
            f"dns.{spec.name}",
            f"dns.{spec.name.replace('_', '-')}",
            f"dns.{spec.name.replace('_', '')}",
        )
    }
    spellings.update({"dns.resolver-endpoint", "dns.zone-record"})
    report: list[dict[str, object]] = []
    for spelling in sorted(spellings):
        try:
            spec = browser.resolve_resource_spec(spelling)
            report.append(
                {
                    "spelling": spelling,
                    "status": "ok",
                    "qualified_name": spec.qualified_name,
                }
            )
        except Exception as exc:
            report.append(
                {"spelling": spelling, "status": "missing", "error": str(exc)}
            )
    return report


def audit_generative_ai_resource_resolution() -> list[dict[str, object]]:
    """Both OCI Generative AI service domains resolve filesystem spellings."""
    browser = build_browser()
    expected = {
        "generative-ai.projects": "generative_ai.projects",
        "generative-ai.vector-store-connectors": "generative_ai.vector_store_connectors",
        "generative-ai-agent.agents": "generative_ai_agent.agents",
        "generative-ai-agent.knowledge-bases": "generative_ai_agent.knowledge_bases",
    }
    report: list[dict[str, object]] = []
    for spelling, qualified_name in expected.items():
        try:
            spec = browser.resolve_resource_spec(spelling)
            status = "ok" if spec.qualified_name == qualified_name else "missing"
            report.append(
                {
                    "spelling": spelling,
                    "status": status,
                    "qualified_name": spec.qualified_name,
                }
            )
        except Exception as exc:
            report.append(
                {"spelling": spelling, "status": "missing", "error": str(exc)}
            )
    return report


def audit_generative_ai_child_mappings() -> dict[str, object]:
    """Operational GenAI records stay beneath their owning resource."""
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    children = shell._build_resource_context_children()
    expected = {
        "generative_ai.projects": {"files", "containers", "vector-stores"},
        "generative_ai.project_containers": {"files"},
        "generative_ai.project_vector_stores": {"files"},
        "generative_ai.vector_store_connectors": {"file-syncs", "ingestion-logs"},
        "generative_ai.vector_store_connector_file_syncs": {"ingestion-logs"},
        "generative_ai.work_requests": {"work-request-errors", "work-request-logs"},
        "generative_ai_agent.agents": {"agent-endpoints", "tools"},
        "generative_ai_agent.knowledge_bases": {"data-sources"},
        "generative_ai_agent.data_sources": {"data-ingestion-jobs"},
        "generative_ai_agent.work_requests": {
            "work-request-errors",
            "work-request-logs",
        },
    }
    missing = {
        parent: sorted(names - set(children.get(parent, {})))
        for parent, names in expected.items()
        if names - set(children.get(parent, {}))
    }
    return {
        "spelling": "Generative AI resource containment",
        "status": "ok" if not missing else "missing",
        "missing": missing,
    }


def audit_generative_ai_data_adapter_metadata() -> dict[str, object]:
    browser = build_browser()
    expected = {
        "generative_ai.project-files": {"collection": "files", "project_param": "generative_ai_project_id"},
        "generative_ai.project-containers": {"collection": "containers", "project_param": "generative_ai_project_id"},
        "generative_ai.project-vector-stores": {"collection": "vector_stores", "project_param": "generative_ai_project_id"},
        "generative_ai.container-files": {"collection": "containers.files", "project_param": "generative_ai_project_id", "parent_param": "container_id"},
        "generative_ai.vector-store-files": {"collection": "vector_stores.files", "project_param": "generative_ai_project_id", "parent_param": "vector_store_id"},
    }
    ok = all(
        spec.lister_name == "_list_openai_project_data"
        and dict(spec.adapter_config) == metadata
        for name, metadata in expected.items()
        for spec in (browser.resolve_resource_spec(name),)
    )
    return {
        "spelling": "GenAI data paths and parent parameters are spec metadata",
        "status": "ok" if ok else "missing",
    }


def audit_generative_ai_direct_catalog_presence() -> dict[str, object]:
    """A GenAI project remains visible when OCI Resource Search omits it."""
    browser = build_browser()
    browser.current = SimpleNamespace(id="ocid1.compartment.example")
    browser.region = lambda: "eu-frankfurt-1"
    browser.compartment_resource_type_cache = {}
    browser.resource_search = SimpleNamespace(
        search_resources=lambda *_args, **_kwargs: None
    )
    browser._retry_oci_call = lambda *_args, **_kwargs: SimpleNamespace(
        data=SimpleNamespace(items=[], opc_next_page=None)
    )
    browser._list_generic_resource = lambda spec, _kwargs=None, compartment_id=None: (
        [main.ResourceRow("project", "ACTIVE", "ocid1.generativeaiproject.example")]
        if spec.qualified_name == "generative_ai.projects"
        else []
    )
    present = {
        spec.qualified_name: count
        for spec, count in browser.list_current_compartment_resource_types()
    }
    catalog = browser.build_active_region_catalog(compartment_id=browser.current.id)
    key = (browser.current.id, "generative_ai.projects")
    ok = present == {"generative_ai.projects": 1} and catalog.get(key) == ("project",)
    return {
        "spelling": "Generative AI direct catalog presence fallback",
        "status": "ok" if ok else "missing",
        "value": present,
    }


def audit_registry_public_taxonomy() -> list[dict[str, object]]:
    browser = build_browser()
    report: list[dict[str, object]] = []
    for namespace in browser.namespaces():
        actual = browser.resource_specs_for_namespace(namespace)
        valid = all(
            spec.runnable
            and (spec.scope == "region" or spec.scope.startswith("compartment"))
            for spec in actual
        )
        report.append(
            {
                "spelling": f"{namespace} registry-derived public taxonomy",
                "status": "ok" if valid else "missing",
                "count": len(actual),
            }
        )
    return report


def audit_resource_status_leaf() -> dict[str, object]:
    spec = main.ResourceSpec(
        namespace="core", name="instances", endpoint_family="iaas", scope="compartment"
    )
    resource = main.ResourceContext(
        spec=spec,
        row=main.ResourceRow(
            name="bastion",
            state="RUNNING",
            id="ocid1.instance.example",
            payload={"lifecycle_state": "RUNNING"},
            details={"availability_domain": "AD-1"},
        ),
    )
    fields = main.ResourceFieldResolver()
    status = fields.resolve(resource, "status")
    availability_domain = fields.resolve(resource, "availability-domain")
    ok = status == "RUNNING" and availability_domain == "AD-1"
    return {
        "spelling": "instances/bastion/<field>",
        "status": "ok" if ok else "missing",
        "value": {"status": status, "availability_domain": availability_domain},
    }


def audit_find_argument_forms() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    current_type_name = shell._parse_find_args("instances basti")
    explicit_path = shell._parse_find_args("dev instances basti")
    ok = current_type_name == ("basti", ["instances"], None) and explicit_path == (
        "basti",
        ["instances"],
        "dev",
    )
    return {
        "spelling": "find <type> [name] and find <path> <type> [name]",
        "status": "ok" if ok else "missing",
    }


def audit_collection_views_are_bounded_and_inspectable() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("core.instances")
    calls: list[int] = []

    def list_resource_page(_spec, *, page_number=1, **_kwargs):
        calls.append(page_number)
        return (
            [
                main.ResourceRow("api-a", "RUNNING", "ocid1.instance.a"),
                main.ResourceRow("batch-a", "STOPPED", "ocid1.instance.b"),
            ],
            {"state": "live", "page": page_number, "page_size": 100, "truncated": True},
        )

    browser.list_resource_page = list_resource_page
    browser._normalize_resource_token = lambda value: value.replace("_", "-")
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.resource_context = None
    shell._completion_cache = {}
    shell._resource_completion_cache = {}
    shell._current_path_suffix = lambda: (
        "/tenancy/dev/core/instances/page/2/name/api/state/RUNNING"
    )
    shell._enter_collection_context(spec, "instances")
    shell._try_enter_resource_from_collection("page")
    shell._try_enter_resource_from_collection("2")
    shell._try_enter_resource_from_collection("name")
    shell._try_enter_resource_from_collection("api")
    shell._try_enter_resource_from_collection("state")
    shell._try_enter_resource_from_collection("RUNNING")
    rows = shell._collection_rows()
    listing = shell._collection_listing_state[shell._current_path_suffix()]
    ok = (
        dict(shell.collection_context.collection_view_options)
        == {"page": 2, "name": "api", "state": "RUNNING"}
        and [row.name for row in rows] == ["api-a"]
        and calls == [2]
        and listing["truncated"] is True
        and listing["filters"] == {"page": 2, "name": "api", "state": "RUNNING"}
    )
    return {
        "spelling": "collection page/name/state views are bounded and disclose listing state",
        "status": "ok" if ok else "missing",
    }


def audit_vcn_teardown_helpers_live_on_shell() -> dict[str, object]:
    context = main.ResourceContext(
        main.ResourceSpec("core", "vcns", "iaas", "compartment"),
        main.ResourceRow(
            "spoke-a",
            "AVAILABLE",
            "ocid1.vcn.example",
            payload={"compartment_id": "ocid1.compartment.example"},
        ),
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    ok = (
        shell._resource_compartment_id(context) == "ocid1.compartment.example"
        and shell._display_name_from_payload({"display_name": "spoke-a"}) == "spoke-a"
        and shell._payload_contains_any(
            {"vcn_id": "ocid1.vcn.example"}, {"ocid1.vcn.example"}
        )
    )
    return {
        "spelling": "recursive VCN teardown helpers are reachable from the shell",
        "status": "ok" if ok else "missing",
    }


def audit_subnet_service_vnic_blockers() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        list_resources=lambda _type: [
            main.ResourceRow(
                "inspection-firewall",
                "ACTIVE",
                "ocid1.networkfirewall.example.12345678",
                payload={
                    "subnet_id": "ocid1.subnet.example",
                    "ipv4_address": "10.0.0.8",
                },
            )
        ],
        get_path=lambda: "/tenancy/dev",
    )
    shell._effective_region = lambda: "eu-frankfurt-1"
    shell._namespace_slug = lambda value: value.replace("_", "-")
    subnet = main.ResourceContext(
        main.ResourceSpec("core", "subnets", "iaas", "compartment"),
        main.ResourceRow(
            "inspection-firewall",
            "AVAILABLE",
            "ocid1.subnet.example",
            payload={"compartment_id": "ocid1.compartment.example"},
        ),
    )
    rows = shell._subnet_service_vnic_rows(subnet)
    ok = (
        len(rows) == 1
        and rows[0].name == "network-firewall@inspection-firewall"
        and rows[0].details
        == {
            "private_ip": "10.0.0.8",
            "hostname_label": "-",
            "owner": "inspection-firewall",
            "owner_type": "network-firewall",
            "owner_path": "/eu-frankfurt-1/tenancy/dev/network-firewall/network-firewalls/inspection-firewall",
            "blocker_kind": "network-firewall",
        }
    )
    shell.browser.list_resources = lambda _type: [
        main.ResourceRow(
            "inspection-firewall",
            "DELETED",
            "ocid1.networkfirewall.example.12345678",
            payload={"subnet_id": "ocid1.subnet.example", "ipv4_address": "10.0.0.8"},
        )
    ]
    deleted_is_not_blocker = shell._subnet_service_vnic_rows(subnet) == []
    return {
        "spelling": "subnet blockers expose non-Compute service VNICs",
        "status": "ok" if ok and deleted_is_not_blocker else "missing",
    }


def audit_network_firewall_namespace() -> dict[str, object]:
    browser = build_browser()
    expected = {
        "network_firewall.network_firewalls",
        "network_firewall.network_firewall_policies",
        "network_firewall.work_requests",
        "network_firewall.security_rules",
    }
    available = {spec.qualified_name for spec in browser.resource_specs}
    ok = (
        expected <= available
        and browser.resolve_namespace_path("network-firewall") == "network_firewall"
    )
    return {
        "spelling": "network firewall namespace and resources are registered",
        "status": "ok" if ok else "missing",
    }


def audit_resource_adapter_kinds_are_declarative() -> dict[str, object]:
    browser = build_browser()
    adapters = {
        spec.qualified_name: spec.adapter_kind
        for spec in browser.resource_specs
        if spec.qualified_name
        in {"audit.events", "object_storage.buckets", "core.instances"}
    }
    ok = adapters == {
        "audit.events": "bounded-time-query",
        "object_storage.buckets": "hierarchical-content",
        "core.instances": "resource-tree",
    }
    return {
        "spelling": "resource adapter kinds are registry-declared",
        "status": "ok" if ok else "missing",
    }


def audit_limits_namespace() -> dict[str, object]:
    """Limits is a bounded, read-only service hierarchy."""
    browser = build_browser()
    specs = {
        spec.qualified_name: spec
        for spec in browser.resource_specs
        if spec.namespace == "limits"
    }
    children = config.RESOURCE_CONTEXT_CHILDREN.get("limits.services", {})
    expected_children = {
        "definitions": "limits.limit-definitions",
        "values": "limits.limit-values",
    }
    actual_children = {
        name: resource_type
        for name, (resource_type, _api_kwargs, _row_filter_key) in children.items()
    }
    services = specs.get("limits.services")
    definitions = specs.get("limits.limit_definitions")
    values = specs.get("limits.limit_values")
    ok = (
        browser.resolve_namespace_path("limits") == "limits"
        and services is not None
        and services.runnable
        and not services.findable
        and services.node_capability == "metadata-record"
        and definitions is not None
        and definitions.node_capability == "terminal-record-set"
        and values is not None
        and values.node_capability == "terminal-record-set"
        and actual_children == expected_children
        and all(
            api_kwargs == (("service_name", "name"),)
            for _name, (_resource_type, api_kwargs, _row_filter_key) in children.items()
        )
    )
    return {
        "spelling": "limits services expose terminal definitions and values",
        "status": "ok" if ok else "missing",
    }


def audit_topology_edges_are_declarative() -> dict[str, object]:
    expected = {
        "network_firewall.network_firewalls",
        "core.instances",
        "containerengine.clusters",
        "load_balancer.load_balancers",
    }
    ok = (
        expected <= set(config.TOPOLOGY_EDGES)
        and all(
            edge["kind"] in {"direct", "derived"}
            for edges in config.TOPOLOGY_EDGES.values()
            for edge in edges
        )
        and all(
            edge.get("via") == "core.vnic_attachments"
            and edge.get("source_field") == "instance_id"
            and edge.get("target_field") == "subnet_id"
            for edge in config.TOPOLOGY_EDGES["core.instances"]
            if edge["kind"] == "derived"
        )
    )
    return {
        "spelling": "topology relationships are declaratively typed",
        "status": "ok" if ok else "missing",
    }


def audit_direct_relationship_fallback_for_instance_images() -> dict[str, object]:
    browser = build_browser()
    browser.current = main.CompartmentNode(
        "ocid1.compartment.example", "dev", None, "ACTIVE", None
    )
    image = main.oci.core.models.Image()
    image.id = "ocid1.image.oc1..example"
    image.display_name = "Oracle Linux"
    image.compartment_id = "ocid1.compartment.example"
    image.lifecycle_state = "AVAILABLE"
    browser.compute = SimpleNamespace(get_image=lambda _id: SimpleNamespace(data=image))
    browser._retry_oci_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    target = browser._direct_relationship_target("ocid1.image.oc1..example")
    ok = (
        target is not None
        and target.spec.qualified_name == "core.images"
        and target.row.name == "Oracle Linux"
    )
    return {
        "spelling": "instance image relationships fall back to a direct getter",
        "status": "ok" if ok else "missing",
    }


def audit_network_firewall_delete_preflight() -> dict[str, object]:
    from deletion import DeletionManager

    manager = DeletionManager.__new__(DeletionManager)
    manager.DELETE_SPECS = {}
    manager.browser = SimpleNamespace(
        network_firewall=SimpleNamespace(
            get_network_firewall=lambda _id: SimpleNamespace(
                data=SimpleNamespace(lifecycle_state="ACTIVE")
            ),
            list_work_requests=lambda *_args, **_kwargs: SimpleNamespace(
                data=SimpleNamespace(items=[])
            ),
        )
    )
    context = main.ResourceContext(
        main.ResourceSpec(
            "network_firewall", "network_firewalls", "network_firewall", "compartment"
        ),
        main.ResourceRow(
            "egress-firewall",
            "ACTIVE",
            "ocid1.networkfirewall.example",
            payload={"compartment_id": "ocid1.compartment.example"},
        ),
    )
    ok = manager._network_firewall_delete_blockers(context) == []
    return {
        "spelling": "network firewall delete preflight checks lifecycle and work requests",
        "status": "ok" if ok else "missing",
    }


def audit_generic_delete_capability_discovery() -> dict[str, object]:
    from deletion import DeletionManager

    manager = DeletionManager.__new__(DeletionManager)
    manager.DELETE_SPECS = {}
    manager.browser = SimpleNamespace(
        widgets=SimpleNamespace(delete_widget=lambda widget_id, **_kwargs: None)
    )
    context = main.ResourceContext(
        main.ResourceSpec(
            "example", "widgets", "example", "compartment", client_attr="widgets"
        ),
        main.ResourceRow("widget-a", "ACTIVE", "ocid1.widget.example"),
    )
    spec = manager._delete_spec_for(context)
    ok = spec.method_name == "delete_widget" and spec.arg_name == "widget_id"
    return {
        "spelling": "rm derives a generic single-ID delete capability",
        "status": "ok" if ok else "missing",
    }


def audit_vcn_blocker_listing_accepts_collection_responses() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        _retry_oci_call=lambda *_args, **_kwargs: SimpleNamespace(
            data=SimpleNamespace(items=["vnic-a"])
        ),
        _collection_items=lambda value: list(value.items),
    )
    rows = shell._list_call_all(lambda: None)
    return {
        "spelling": "VCN blocker listing normalizes OCI collection responses",
        "status": "ok" if rows == ["vnic-a"] else "missing",
    }


def audit_oci_failure_record_is_readable() -> dict[str, object]:
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.THROTTLE_BASE_DELAY = 0
    browser.THROTTLE_RETRIES = 0

    class Denied(Exception):
        status, code, message, request_id = (
            403,
            "NotAuthorized",
            "not authorized",
            "req-123",
        )

    with suppress(Denied):
        browser._retry_oci_call(lambda: (_ for _ in ()).throw(Denied()))
    failure = browser.last_oci_failure
    ok = (
        failure["kind"] == "permission-denied"
        and failure["status"] == 403
        and failure["request_id"] == "req-123"
        and bool(failure["timestamp"])
    )
    return {
        "spelling": "OCI failures retain readable permission/request metadata",
        "status": "ok" if ok else "missing",
    }


def audit_find_respects_current_service_namespace() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = "containerengine"
    specs = shell._find_search_specs()
    ok = bool(specs) and {spec.namespace for spec in specs} == {"containerengine"}
    return {
        "spelling": "find inside a service is service-scoped",
        "status": "ok" if ok else "missing",
    }


def audit_find_dot_uses_service_scoped_search() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = "containerengine"
    shell.resource_context = None
    shell.collection_context = None
    shell._find_current_compartment_paths_via_search = lambda: [
        (
            "/dev/clusters/oke",
            main.ResourceRow("oke", "ACTIVE", "ocid1.cluster.example"),
        ),
        (
            "/dev/pod-shapes/Pod.Standard.A1.Flex",
            main.ResourceRow("Pod.Standard.A1.Flex", "-", "Pod.Standard.A1.Flex"),
        ),
    ]
    shell._find_all_ocids_via_search = lambda: (_ for _ in ()).throw(
        AssertionError("raw OCID search must not run in a service")
    )
    output = io.StringIO()
    with redirect_stdout(output):
        shell.do_find(".")
    ok = output.getvalue().splitlines() == [
        "/dev/clusters/oke",
        "/dev/pod-shapes/Pod.Standard.A1.Flex",
    ]
    return {
        "spelling": "find dot inside a service emits scoped filesystem paths",
        "status": "ok" if ok else "missing",
    }


def audit_find_uses_generic_search_for_clusters() -> dict[str, object]:
    browser = build_browser()
    browser.region = lambda: "eu-frankfurt-1"
    cluster_spec = browser.resolve_resource_spec("containerengine.clusters")
    cluster_search_type = "ClustersCluster"
    browser.search_type_cache = {cluster_spec.qualified_name: cluster_search_type}
    queries: list[str] = []

    class Search:
        def search_resources(self, details: object, **kwargs: object) -> object:
            queries.append(str(getattr(details, "query", "")))
            if kwargs.get("page") is None:
                return SimpleNamespace(
                    data=SimpleNamespace(items=[], opc_next_page=None),
                    next_page="second-page",
                )
            return SimpleNamespace(
                data=SimpleNamespace(
                    items=[
                        {
                            "resource_type": cluster_search_type,
                            "display_name": "oke-prod",
                            "identifier": "ocid1.cluster.example",
                            "compartment_id": "ocid1.compartment.dev",
                        }
                    ],
                    opc_next_page=None,
                ),
                next_page=None,
                has_next_page=False,
            )

    browser.resource_search = Search()
    browser._retry_oci_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    root = main.CompartmentNode(
        "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
    )
    dev = main.CompartmentNode("ocid1.compartment.dev", "dev", None, "ACTIVE", root.id)
    browser.root = root
    browser.current = root
    browser.parents = []
    browser.compartment_chain = lambda _id: [root, dev]
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    results = shell._find_via_search([cluster_spec], "oke")
    ok = (
        browser._resource_spec_for_search_type(cluster_search_type) == cluster_spec
        and results
        == [
            (
                "/eu-frankfurt-1/tenancy/dev/containerengine/clusters/oke-prod",
                main.ResourceRow(
                    "oke-prod",
                    cluster_search_type,
                    "ocid1.cluster.example",
                    extra=cluster_search_type,
                    details={
                        "compartment_id": "ocid1.compartment.dev",
                        "resource_type": cluster_search_type,
                    },
                    payload={
                        "resource_type": cluster_search_type,
                        "display_name": "oke-prod",
                        "identifier": "ocid1.cluster.example",
                        "compartment_id": "ocid1.compartment.dev",
                    },
                ),
            )
        ]
        and queries
        and len(queries) == 2
        and queries[0].startswith(
            "query ClustersCluster resources where displayName =~ '.*oke.*'"
        )
    )
    return {
        "spelling": "find clusters uses generic OCI Search",
        "status": "ok" if ok else "missing",
    }


def audit_find_falls_back_to_registered_resource_lister() -> dict[str, object]:
    browser = build_browser()
    browser.region = lambda: "eu-frankfurt-1"
    spec = browser.resolve_resource_spec("containerengine.virtual-node-pools")
    root = main.CompartmentNode(
        "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
    )
    dev = main.CompartmentNode("ocid1.compartment.dev", "dev", None, "ACTIVE", root.id)

    class UnknownResourceTypeError(Exception):
        code = "CannotParseRequest"

        def __str__(self) -> str:
            return "Unknown resource type 'virtualnodepool'"

    class Search:
        def search_resources(self, _details: object, **_kwargs: object) -> object:
            raise UnknownResourceTypeError()

    browser.root = root
    browser.current = dev
    browser.parents = [root]
    browser.resource_search = Search()
    browser._retry_oci_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    browser.list_resources = lambda resource_type: (
        [main.ResourceRow("virtual-worker", "ACTIVE", "ocid1.vnp.example")]
        if resource_type == spec.qualified_name
        else []
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._subtree_compartment_targets = lambda: [(dev, [root])]
    results = shell._find_via_search([spec], None)
    ok = results == [
        (
            "/eu-frankfurt-1/tenancy/dev/containerengine/virtual-node-pools/virtual-worker",
            main.ResourceRow("virtual-worker", "ACTIVE", "ocid1.vnp.example"),
        )
    ]
    return {
        "spelling": "find falls back to registered resource lister",
        "status": "ok" if ok else "missing",
    }


def audit_find_skips_unavailable_resource_lister() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("core.compute_global_image_capability_schemas")
    root = main.CompartmentNode(
        "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
    )
    dev = main.CompartmentNode("ocid1.compartment.dev", "dev", None, "ACTIVE", root.id)
    browser.current = dev
    browser.parents = [root]
    browser.list_resources = lambda _resource_type: (_ for _ in ()).throw(
        PermissionError("not authorized")
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._subtree_compartment_targets = lambda: [(dev, [root])]
    results = shell._find_via_resource_lists(spec, None)
    ok = results == [] and browser.current == dev and browser.parents == [root]
    return {
        "spelling": "find skips unavailable resource lister",
        "status": "ok" if ok else "missing",
    }


def audit_find_explicit_type_merges_resource_lister() -> dict[str, object]:
    browser = build_browser()
    browser.region = lambda: "eu-frankfurt-1"
    spec = browser.resolve_resource_spec("core.instances")
    root = main.CompartmentNode(
        "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
    )
    dev = main.CompartmentNode("ocid1.compartment.dev", "dev", None, "ACTIVE", root.id)
    browser.root = root
    browser.current = dev
    browser.parents = [root]
    browser.search_type_cache = {spec.qualified_name: "Instance"}
    browser.resource_search = SimpleNamespace(
        search_resources=lambda *_args, **_kwargs: None
    )
    browser._retry_oci_call = lambda *_args, **_kwargs: SimpleNamespace(
        data=SimpleNamespace(items=[]), next_page=None
    )
    browser.list_resources = lambda _resource_type: [
        main.ResourceRow("bastion", "RUNNING", "ocid1.instance.example")
    ]
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._subtree_compartment_targets = lambda: [(dev, [root])]
    results = shell._find_via_search([spec], None, include_lister_results=True)
    ok = results == [
        (
            "/eu-frankfurt-1/tenancy/dev/core/instances/bastion",
            main.ResourceRow("bastion", "RUNNING", "ocid1.instance.example"),
        )
    ]
    return {
        "spelling": "explicit find type merges registered lister",
        "status": "ok" if ok else "missing",
    }


def audit_find_paths_are_canonical_and_unambiguous() -> dict[str, object]:
    browser = build_browser()
    browser.region = lambda: "eu-frankfurt-1"
    spec = browser.resolve_resource_spec("core.instances")
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    rows = [
        main.ResourceRow("api", "RUNNING", "ocid1.instance.example.12345678"),
        main.ResourceRow("api", "RUNNING", "ocid1.instance.example.87654321"),
    ]
    results = shell._uniquify_find_paths(
        [(shell._canonical_find_path("/tenancy/prod", spec, row), row) for row in rows]
    )
    ok = [path for path, _row in results] == [
        "/eu-frankfurt-1/tenancy/prod/core/instances/api@12345678",
        "/eu-frankfurt-1/tenancy/prod/core/instances/api@87654321",
    ]
    return {
        "spelling": "find results are absolute canonical paths with duplicate-safe leaves",
        "status": "ok" if ok else "missing",
    }


def audit_relationship_projection_falls_back_to_resource_lister() -> dict[str, object]:
    browser = build_browser()
    browser.relationship_projection_cache = {}
    browser.region = lambda: "eu-frankfurt-1"
    spec = browser.resolve_resource_spec("containerengine.virtual-node-pools")

    class UnknownResourceTypeError(Exception):
        code = "CannotParseRequest"

        def __str__(self) -> str:
            return "Unknown resource type 'virtualnodepool'"

    browser.resource_search = SimpleNamespace(
        search_resources=lambda *_args, **_kwargs: None
    )
    browser._retry_oci_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        UnknownResourceTypeError()
    )
    browser.list_resources = lambda _resource_type: [
        main.ResourceRow(
            "worker-pool", "ACTIVE", "ocid1.vnp.match", details={"cluster_id": "parent"}
        ),
        main.ResourceRow(
            "other-pool", "ACTIVE", "ocid1.vnp.other", details={"cluster_id": "other"}
        ),
    ]
    rows = browser.list_relationship_projection(spec, "cluster_id", "parent")
    ok = rows == [
        main.ResourceRow(
            "worker-pool", "ACTIVE", "ocid1.vnp.match", details={"cluster_id": "parent"}
        )
    ]
    return {
        "spelling": "relationship projection falls back to resource lister",
        "status": "ok" if ok else "missing",
    }


def audit_relationship_projection_reconciles_empty_search() -> dict[str, object]:
    browser = build_browser()
    browser.relationship_projection_cache = {}
    browser.region = lambda: "eu-frankfurt-1"
    spec = browser.resolve_resource_spec("containerengine.node-pools")
    browser.resource_search = SimpleNamespace(
        search_resources=lambda *_args, **_kwargs: None
    )
    browser._retry_oci_call = lambda *_args, **_kwargs: SimpleNamespace(
        data=SimpleNamespace(items=[])
    )
    browser.list_resources = lambda _resource_type: [
        main.ResourceRow(
            "general-pool",
            "ACTIVE",
            "ocid1.nodepool.example",
            details={"cluster_id": "cluster-name"},
            payload={"cluster_id": "ocid1.cluster.parent"},
        )
    ]
    rows = browser.list_relationship_projection(
        spec, "cluster_id", "ocid1.cluster.parent"
    )
    ok = len(rows) == 1 and rows[0].name == "general-pool"
    return {
        "spelling": "relationship projection reconciles empty search",
        "status": "ok" if ok else "missing",
    }


def audit_ls_combined_options() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    calls: list[bool] = []
    shell._list_current_node = calls.append
    shell.do_ls("-al")
    ok = calls == [True]
    return {
        "spelling": "ls accepts combined all and long options",
        "status": "ok" if ok else "missing",
    }


def audit_catalog_paginates_search_type_discovery() -> dict[str, object]:
    browser = build_browser()
    browser.search_type_cache = {}
    browser.CATALOG_PAGE_DELAY_SECONDS = 0
    cluster_spec = browser.resolve_resource_spec("containerengine.clusters")
    queries: list[object] = []

    class Search:
        def search_resources(self, _details: object, **kwargs: object) -> object:
            queries.append(kwargs.get("page"))
            if kwargs.get("page") is None:
                return SimpleNamespace(
                    data=SimpleNamespace(
                        items=[{"resource_type": "Bucket"}], opc_next_page=None
                    ),
                    next_page="second-page",
                )
            return SimpleNamespace(
                data=SimpleNamespace(
                    items=[
                        {
                            "resource_type": "ClustersCluster",
                            "compartment_id": "ocid1.compartment.dev",
                            "display_name": "oke-prod",
                        }
                    ],
                    opc_next_page=None,
                ),
                next_page=None,
            )

    browser.resource_search = Search()
    browser._retry_oci_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    snapshots: list[dict[tuple[str, str], tuple[str, ...]]] = []
    catalog = browser.build_active_region_catalog(
        on_snapshot=lambda names, _ids, _rows: snapshots.append(names)
    )
    ok = (
        queries == [None, "second-page"]
        and len(snapshots) == 2
        and catalog[("ocid1.compartment.dev", cluster_spec.qualified_name)]
        == ("oke-prod",)
        and browser.search_type_for(cluster_spec) == "ClustersCluster"
    )
    return {
        "spelling": "catalog paginates OCI Search type discovery",
        "status": "ok" if ok else "missing",
    }


def audit_hyphenated_namespace_resolution() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("load-balancer.protocols")
    ok = spec.qualified_name == "load_balancer.protocols"
    return {
        "spelling": "hyphenated qualified namespace resolution",
        "status": "ok" if ok else "missing",
    }


def audit_namespace_path_slug_policy() -> dict[str, object]:
    """Public service paths are hyphenated while SDK identifiers remain aliases."""
    browser = build_browser()
    hyphen = browser.resolve_resource_spec("load-balancer.load-balancers")
    sdk = browser.resolve_resource_spec("load_balancer.load_balancers")
    ok = (
        "load-balancer" in browser.namespaces()
        and "load_balancer" not in browser.namespaces()
        and browser.resource_specs_for_namespace("load-balancer")
        and hyphen == sdk
        and hyphen.qualified_name == "load_balancer.load_balancers"
    )
    return {
        "spelling": "namespace path slugs are hyphenated with SDK aliases",
        "status": "ok" if ok else "missing",
    }


def audit_explicit_special_capabilities() -> dict[str, object]:
    """Virtual nodes and recursive deletion use typed/capability policies."""
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell._enter_collection_context(
        main.ResourceSpec("logging", "logs", "logging", "compartment"),
        "entries",
        virtual_kind="time-query",
        time_query_provider="logging",
    )
    ok = (
        shell.collection_context.virtual_kind is main.VirtualKind.TIME_QUERY
        and main.OciNavShell.DELETE_SPECS["core.vcns"].recursive_supported
        and not main.OciNavShell.DELETE_SPECS["core.subnets"].recursive_supported
    )
    return {
        "spelling": "special behavior uses explicit capabilities",
        "status": "ok" if ok else "missing",
    }


def audit_load_balancer_listener_name() -> dict[str, object]:
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.current = SimpleNamespace(id="ocid1.compartment.example")
    browser.load_balancer = SimpleNamespace(
        get_load_balancer=lambda _id: SimpleNamespace(
            data=SimpleNamespace(listeners={"map-key": SimpleNamespace(name="TCP-443")})
        )
    )
    browser._humanize_name = lambda value: value
    rows = browser._list_load_balancer_listeners(
        {"load_balancer_id": "ocid1.loadbalancer.example"}
    )
    ok = len(rows) == 1 and rows[0].name == "TCP-443"
    return {
        "spelling": "load balancer listener model name",
        "status": "ok" if ok else "missing",
    }


def audit_load_balancer_long_columns() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    columns = shell._resource_long_columns(
        main.ResourceSpec(
            "load_balancer", "load_balancers", "load_balancer", "compartment"
        )
    )
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    ips = browser._format_ip_addresses([SimpleNamespace(ip_address="203.0.113.10")])
    expected = [
        "name",
        "state",
        "overall_health",
        "is_private",
        "ip_addresses",
        "shape_name",
        "created",
    ]
    ok = [key for key, _label in columns] == expected and ips == "203.0.113.10"
    return {
        "spelling": "load balancer operational long columns",
        "status": "ok" if ok else "missing",
    }


def audit_operational_default_columns() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "active", "ACTIVE", "ocid1.example", details={"endpoint": "203.0.113.10"}
        ),
        main.ResourceRow("idle", "INACTIVE", "ocid1.other", details={}),
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows,
        [
            ("name", "Name"),
            ("state", "State"),
            ("endpoint", "Endpoint"),
            ("unused", "Unused"),
        ],
    )
    configured = all(
        all(key != "extra" and label != "Extra" for key, label in view)
        for view in config.LONG_COLUMNS.values()
    )
    ok = (
        columns == [("name", "Name"), ("state", "State"), ("endpoint", "Endpoint")]
        and configured
    )
    return {
        "spelling": "operational ls -l defaults",
        "status": "ok" if ok else "missing",
    }


def audit_ll_alias() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    calls: list[str] = []
    shell.do_ls = calls.append
    shell.do_ll("instances")
    shell.do_ll("")
    ok = calls == ["-l instances", "-l"]
    return {"spelling": "ll alias for ls -l", "status": "ok" if ok else "missing"}


def audit_subnet_access_column() -> dict[str, object]:
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    public = browser._format_subnet_access(False, None)
    private = browser._format_subnet_access(True, None)
    subnet_columns = config.LONG_COLUMNS["core.subnets"]
    ok = (
        public == "Public (Regional)"
        and private == "Private (Regional)"
        and ("subnet_access", "Subnet access") in subnet_columns
        and all(key != "availability_domain" for key, _label in subnet_columns)
        and all(key != "dns_label" for key, _label in subnet_columns)
    )
    return {
        "spelling": "subnet access long column",
        "status": "ok" if ok else "missing",
    }


def audit_address_column_priority() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "node",
            "ACTIVE",
            "ocid1.example",
            details={"ip_address": "203.0.113.10", "shape": "VM.Standard"},
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows, [("name", "Name"), ("state", "State"), ("shape", "Shape")]
    )
    ok = columns == [
        ("name", "Name"),
        ("state", "State"),
        ("ip_address", "IP"),
        ("shape", "Shape"),
    ]
    return {
        "spelling": "CIDR and IP default column priority",
        "status": "ok" if ok else "missing",
    }


def audit_operational_column_formula() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "subnet",
            "AVAILABLE",
            "ocid1.subnet.example",
            details={
                "cidr": "10.0.0.0/24",
                "dns_label": "internal",
                "subnet_access": "Private (Regional)",
                "vcn_id": "network",
                "created": "2026-09-14T00:00:00Z",
            },
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows, config.LONG_COLUMNS["core.subnets"]
    )
    ok = [key for key, _label in columns] == [
        "name",
        "state",
        "cidr",
        "subnet_access",
        "vcn_id",
        "created",
    ]
    return {
        "spelling": "scored operational ll formula",
        "status": "ok" if ok else "missing",
    }


def audit_load_balancer_health_column() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "public",
            "ACTIVE",
            "ocid1.loadbalancer.example",
            details={
                "overall_health": "OK",
                "ip_addresses": "203.0.113.10",
                "shape_name": "flexible",
            },
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows, config.LONG_COLUMNS["load_balancer.load_balancers"]
    )
    ok = [key for key, _label in columns][:3] == ["name", "state", "overall_health"]
    return {
        "spelling": "load balancer overall health default",
        "status": "ok" if ok else "missing",
    }


def audit_load_balancer_lister_keeps_rows_immutable() -> dict[str, object]:
    """Long listings must enrich frozen ResourceRow values by replacement."""
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.load_balancer = SimpleNamespace(
        get_load_balancer_health=lambda _id, **_kwargs: None,
    )
    browser._resolve_resource_spec = lambda _name: main.ResourceSpec(
        "load_balancer", "load_balancers", "load_balancer", "compartment"
    )
    browser._list_generic_resource = lambda *_args, **_kwargs: [
        main.ResourceRow(
            "lb", "ACTIVE", "ocid1.loadbalancer.oc1..example", details={"shape": "flex"}
        )
    ]
    browser._remember_row_names = lambda _rows: None
    browser._enrich_relationship_names = lambda rows: rows
    calls = 0

    def retry(_func, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(data=SimpleNamespace(status="OK"))

    browser._retry_oci_call = retry
    rows = browser.list_resources("load_balancer.load-balancers")
    ok = len(rows) == 1 and rows[0].details == {
        "shape": "flex",
        "overall_health": "OK",
    }
    return {
        "spelling": "load balancer health enrichment preserves immutable rows",
        "status": "ok" if ok else "missing",
    }


def audit_opaque_relationship_id_suppression() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "subnet",
            "AVAILABLE",
            "ocid1.subnet.example",
            details={"vcn_id": "ocid1.vcn.example", "cidr": "10.0.0.0/24"},
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows,
        [("name", "Name"), ("state", "State"), ("vcn_id", "VCN"), ("cidr", "CIDR")],
    )
    ok = [key for key, _label in columns] == ["name", "state", "cidr"]
    return {
        "spelling": "raw relationship OCIDs omitted from ll",
        "status": "ok" if ok else "missing",
    }


def audit_relationship_name_column() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "oke",
            "ACTIVE",
            "ocid1.cluster.example",
            details={"vcn_id": "cluster-network", "kubernetes_version": "v1.34"},
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows, config.LONG_COLUMNS["containerengine.clusters"]
    )
    ok = ("vcn_id", "VCN") in columns
    return {
        "spelling": "general relationship name default",
        "status": "ok" if ok else "missing",
    }


def audit_relationship_precedes_created() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "oke",
            "ACTIVE",
            "ocid1.cluster.example",
            details={
                "kubernetes_version": "v1.35",
                "cluster_type": "ENHANCED_CLUSTER",
                "vcn_id": "network",
                "private_endpoint": "10.0.0.10:6443",
                "created": "2026-08-06T14:03:55Z",
            },
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows, config.LONG_COLUMNS["containerengine.clusters"]
    )
    keys = [key for key, _label in columns]
    ok = "vcn_id" in keys and "created" not in keys
    return {
        "spelling": "relationships precede lifecycle timestamps",
        "status": "ok" if ok else "missing",
    }


def audit_created_is_final_column() -> dict[str, object]:
    rows = [
        main.ResourceRow(
            "vcn",
            "AVAILABLE",
            "ocid1.vcn.example",
            details={
                "cidr": "10.0.0.0/16",
                "created": "2026-09-14T00:00:00Z",
                "vcn_id": "network",
            },
        )
    ]
    columns = main.OciNavShell._effective_long_columns(
        rows,
        [
            ("name", "Name"),
            ("state", "State"),
            ("created", "Created"),
            ("cidr", "CIDR"),
            ("vcn_id", "VCN"),
        ],
    )
    ok = columns[-1] == ("created", "Created")
    return {
        "spelling": "Created is always the final ll column",
        "status": "ok" if ok else "missing",
    }


def audit_general_relationship_enrichment() -> dict[str, object]:
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser._current_compartment_resource_names = lambda: {
        "ocid1.vcn.example": "network"
    }
    rows = [
        main.ResourceRow(
            "subnet",
            "AVAILABLE",
            "ocid1.subnet.example",
            details={"vcn_id": "ocid1.vcn.example"},
        )
    ]
    enriched = browser._enrich_relationship_names(rows)
    ok = enriched[0].details == {"vcn_id": "network"}
    return {
        "spelling": "general OCID-to-name relationship enrichment",
        "status": "ok" if ok else "missing",
    }


def audit_security_rule_nsg_name_rendering() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        resolve_name=lambda value: f"workers-oke200 ({value})"
    )
    value = shell._render_detail_value(
        "source", "ocid1.networksecuritygroup.oc1.eu-frankfurt-1.example"
    )
    ok = value == "workers-oke200"
    return {
        "spelling": "NSG security-rule endpoints render as names",
        "status": "ok" if ok else "missing",
    }


def audit_region_mount_uses_qualified_type() -> dict[str, object]:
    calls: list[str] = []
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        list_resources=lambda resource_type: calls.append(resource_type) or []
    )
    shell._mount_entries("region")
    ok = calls == ["identity.region-subscriptions"]
    return {
        "spelling": "absolute-path region check uses qualified subscription type",
        "status": "ok" if ok else "missing",
    }


def audit_virtual_relationship_symlink() -> dict[str, object]:
    vcn_spec = main.ResourceSpec("core", "vcns", "iaas", "compartment")
    target = main.RelationshipTarget(
        spec=vcn_spec,
        row=main.ResourceRow("network", "AVAILABLE", "ocid1.vcn.example"),
        compartment_id="ocid1.compartment.example",
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        _normalize_resource_token=lambda value: value.replace("-", "_").replace(
            "_", "-"
        ),
        relationship_target=lambda ocid: (
            target if ocid == "ocid1.vcn.example" else None
        ),
        compartment_chain=lambda _id: [
            main.CompartmentNode(
                "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
            ),
            main.CompartmentNode(
                "ocid1.compartment.example",
                "dev",
                None,
                "ACTIVE",
                "ocid1.tenancy.example",
            ),
        ],
        _sanitize_row_name=lambda value: value,
    )
    shell.resource_context = main.ResourceContext(
        main.ResourceSpec(
            "containerengine", "clusters", "containerengine", "compartment"
        ),
        main.ResourceRow(
            "oke200",
            "ACTIVE",
            "ocid1.cluster.example",
            payload={"vcn_id": "ocid1.vcn.example"},
        ),
    )
    shell.mount_leaf = None
    shell.mount_entry = None
    shell.mount_collection = None
    shell.namespace_view = "containerengine"
    references = shell._relationship_references()
    shell._effective_region = lambda: "eu-frankfurt-1"
    shell._namespace_slug = lambda value: value.replace("_", "-")
    output = io.StringIO()
    with redirect_stdout(output):
        shell.do_readlink("vcn")
    ok = references == [("vcn", target)] and output.getvalue().strip() == (
        "/eu-frankfurt-1/tenancy/dev/core/vcns/network"
    )
    return {
        "spelling": "virtual VCN relationship symlink",
        "status": "ok" if ok else "missing",
    }


def audit_compartment_is_metadata_not_symlink() -> dict[str, object]:
    compartment_spec = main.ResourceSpec(
        "identity", "compartments", "identity", "tenancy"
    )
    target = main.RelationshipTarget(
        spec=compartment_spec,
        row=main.ResourceRow("fvass", "ACTIVE", "ocid1.compartment.example"),
        compartment_id="ocid1.tenancy.example",
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(relationship_target=lambda _ocid: target)
    shell.resource_context = main.ResourceContext(
        main.ResourceSpec("core", "vcns", "iaas", "compartment"),
        main.ResourceRow(
            "VCN1",
            "AVAILABLE",
            "ocid1.vcn.example",
            payload={"compartment_id": target.row.id},
        ),
    )
    ok = shell._relationship_references() == []
    return {
        "spelling": "compartment ownership remains metadata, not a symlink",
        "status": "ok" if ok else "missing",
    }


def audit_child_attributes_are_not_parent_symlinks() -> dict[str, object]:
    nsg_spec = main.ResourceSpec(
        "core", "network_security_groups", "iaas", "compartment"
    )
    target = main.RelationshipTarget(
        spec=nsg_spec,
        row=main.ResourceRow(
            "other-nsg", "AVAILABLE", "ocid1.networksecuritygroup.example"
        ),
        compartment_id="ocid1.compartment.example",
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        relationship_target=lambda _ocid: target,
        list_resources=lambda *_args, **_kwargs: [
            main.ResourceRow("rule", "-", "rule", payload={"source": target.row.id})
        ],
        PROJECTION_CACHE_TTL_SECONDS=30,
    )
    shell.resource_context = main.ResourceContext(
        nsg_spec,
        main.ResourceRow("cp-oke200", "AVAILABLE", "ocid1.networksecuritygroup.parent"),
    )
    shell._resource_context_children_map = {
        nsg_spec.qualified_name: {
            "security-rules": main.ResourceChildCollection(
                "core.network_security_group_security_rules"
            )
        }
    }
    shell._indirect_relationship_cache = {}
    ok = shell._relationship_references() == []
    return {
        "spelling": "child rule attributes are not parent symlinks",
        "status": "ok" if ok else "missing",
    }


def audit_indirect_relationships_require_policy() -> dict[str, object]:
    """Resources without an indirect policy must not list child collections."""
    calls = 0

    def list_resources(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return []

    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        relationship_target=lambda _ocid: None,
        list_resources=list_resources,
        PROJECTION_CACHE_TTL_SECONDS=30,
    )
    shell.resource_context = main.ResourceContext(
        main.ResourceSpec("core", "instances", "iaas", "compartment"),
        main.ResourceRow("instance", "RUNNING", "ocid1.instance.oc1..example"),
    )
    shell._resource_context_children_map = {
        "core.instances": {
            "vnics": main.ResourceChildCollection("core.vnic_attachments")
        }
    }
    shell._indirect_relationship_cache = {}
    ok = shell._indirect_relationship_references() == [] and calls == 0
    return {
        "spelling": "indirect relationships require explicit policy",
        "status": "ok" if ok else "missing",
    }


def audit_repeated_relationships_are_directories() -> dict[str, object]:
    nsg_spec = main.ResourceSpec(
        "core", "network_security_groups", "iaas", "compartment"
    )
    targets = {
        f"ocid1.networksecuritygroup.{name}": main.RelationshipTarget(
            spec=nsg_spec,
            row=main.ResourceRow(
                name, "AVAILABLE", f"ocid1.networksecuritygroup.{name}"
            ),
            compartment_id="ocid1.compartment.example",
        )
        for name in ("backend-a", "backend-b")
    }
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        relationship_target=targets.get,
        _normalize_resource_token=lambda value: value.replace("_", "-"),
        _matching_rows=lambda rows, name: [row for row in rows if row.name == name],
        compartment_chain=lambda _id: [
            main.CompartmentNode(
                "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
            ),
            main.CompartmentNode(
                "ocid1.compartment.example",
                "dev",
                None,
                "ACTIVE",
                "ocid1.tenancy.example",
            ),
        ],
        _sanitize_row_name=lambda value: value,
    )
    shell.resource_context = main.ResourceContext(
        nsg_spec,
        main.ResourceRow(
            "cp-oke200",
            "AVAILABLE",
            "ocid1.networksecuritygroup.parent",
            payload={"backend_nsg_ids": list(targets)},
        ),
    )
    shell._resource_context_children_map = {}
    shell._indirect_relationship_cache = {}
    shell._completion_cache = {}
    shell._resource_completion_cache = {}
    collections = shell._relationship_collections()
    shell._effective_region = lambda: "eu-frankfurt-1"
    shell._namespace_slug = lambda value: value.replace("_", "-")
    entered = shell._try_enter_collection_context("backend-nsgs")
    names = [
        target.row.name for target in shell.collection_context.relationship_targets
    ]
    output = io.StringIO()
    with redirect_stdout(output):
        shell.do_readlink("backend-a")
    ok = (
        set(collections) == {"backend-nsgs"}
        and shell._relationship_references() == []
        and entered
        and names == ["backend-a", "backend-b"]
        and output.getvalue().strip()
        == "/eu-frankfurt-1/tenancy/dev/core/network-security-groups/backend-a"
    )
    return {
        "spelling": "repeated relationships use the relationships directory",
        "status": "ok" if ok else "missing",
    }


def audit_cluster_work_request_projection() -> dict[str, object]:
    """Clusters expose scoped work requests and creation provenance."""
    cluster_spec = main.ResourceSpec(
        "containerengine", "clusters", "containerengine", "compartment"
    )
    work_request_spec = main.ResourceSpec(
        "containerengine", "work_requests", "containerengine", "compartment"
    )
    work_request = main.ResourceRow(
        "create-cluster", "SUCCEEDED", "ocid1.clustersworkrequest.oc1..example"
    )
    calls: list[tuple[str, object]] = []

    def list_resources(resource_type: str, extra_kwargs=None, **_kwargs):
        calls.append((resource_type, extra_kwargs))
        return (
            [work_request]
            if resource_type
            in {
                "containerengine.work-requests",
                "containerengine.work_requests",
            }
            and extra_kwargs == {"cluster_id": "ocid1.cluster.oc1..example"}
            else []
        )

    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        relationship_target=lambda _ocid: None,
        list_resources=list_resources,
        resolve_resource_spec=lambda _name: work_request_spec,
        _normalize_resource_token=lambda value: value.replace("_", "-"),
        get_path=lambda: "/dev",
        current=SimpleNamespace(id="ocid1.compartment.oc1..example"),
        PROJECTION_CACHE_TTL_SECONDS=30,
    )
    shell.resource_context = main.ResourceContext(
        cluster_spec,
        main.ResourceRow(
            "cluster",
            "ACTIVE",
            "ocid1.cluster.oc1..example",
            payload={"metadata": {"created_by_work_request_id": work_request.id}},
        ),
    )
    shell.mount_leaf = None
    shell.mount_entry = None
    shell.mount_collection = None
    shell.namespace_view = "containerengine"
    shell._resource_context_children_map = {
        cluster_spec.qualified_name: {
            "work-requests": main.ResourceChildCollection(
                "containerengine.work-requests", (("cluster_id", "id"),)
            )
        }
    }
    shell._completion_cache = {}
    shell._resource_completion_cache = {}
    links = dict(shell._relationship_references())
    entered = shell._try_enter_collection_context("work-requests")
    scoped = shell._collection_rows() if entered else []
    ok = (
        links.get("creation-work-request") is not None
        and len(scoped) == 1
        and scoped[0].id == work_request.id
    )
    return {
        "spelling": "cluster work requests are scoped operational projections",
        "status": "ok" if ok else "missing",
    }


def audit_absolute_locator_completion() -> dict[str, object]:
    browser = build_browser()
    browser.root = SimpleNamespace(name="ionsfa9e57")
    browser.parents = []
    browser.current = SimpleNamespace(name="fvass")
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._completion_cache = {"/fvass/core/instances": ("minikube",)}
    static = shell._complete_absolute_locator("~/fvass/core/inst")
    cached = shell._cached_resource_completions("mini", "~/fvass/core/instances/mini")
    ok = "instances" in static and cached == ["minikube"]
    return {
        "spelling": "absolute locator completion from static registry and local cache",
        "status": "ok" if ok else "missing",
    }


def audit_ls_cached_collection_path_completion() -> dict[str, object]:
    browser = build_browser()
    browser.root = main.CompartmentNode(
        "ocid1.tenancy.example", "ionsfa9e57", None, "ACTIVE", None
    )
    browser.current = browser.root
    browser.parents = []
    browser.region = lambda: "eu-frankfurt-1"
    bd = main.CompartmentNode(
        "ocid1.compartment.bd", "bd", None, "ACTIVE", browser.root.id
    )
    dev = main.CompartmentNode("ocid1.compartment.dev", "dev", None, "ACTIVE", bd.id)
    cluster_spec = browser.resolve_resource_spec("containerengine.clusters")
    browser.children_cache = {
        ("eu-frankfurt-1", browser.root.id): (9999999999.0, (bd,)),
        ("eu-frankfurt-1", bd.id): (9999999999.0, (dev,)),
        ("eu-frankfurt-1", dev.id): (9999999999.0, ()),
    }
    browser.compartment_resource_type_cache = {
        ("eu-frankfurt-1", dev.id): (9999999999.0, ((cluster_spec, 1),))
    }
    catalog = main.ActiveRegionCatalog.__new__(main.ActiveRegionCatalog)
    catalog.browser = browser
    catalog._lock = __import__("threading").Lock()
    catalog._index_lock = __import__("threading").Lock()
    catalog._scanned_compartments = {dev.id}
    catalog._region = "eu-frankfurt-1"
    catalog._snapshot = {}
    catalog._id_snapshot = {}
    catalog._resource_snapshot = {
        (dev.id, cluster_spec.qualified_name): tuple(
            browser._uniquify_row_names(
                [
                    main.ResourceRow(
                        "cluster",
                        "ACTIVE",
                        "ocid1.cluster.oc1.eu-frankfurt-1.exampleuniqueid1234abcd",
                    ),
                    main.ResourceRow(
                        "cluster",
                        "ACTIVE",
                        "ocid1.cluster.oc1.eu-frankfurt-1.exampleuniqueid5678efgh",
                    ),
                ]
            )
        )
    }
    catalog._children_snapshot = {
        browser.root.id: (bd,),
        bd.id: (dev,),
        dev.id: (),
    }
    catalog._collections_snapshot = {dev.id: (cluster_spec.qualified_name,)}
    catalog.prime_compartment = lambda _id: (_ for _ in ()).throw(
        AssertionError("completion must not fetch OCI")
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.catalog = catalog
    shell.resource_context = None
    shell.collection_context = None
    shell.mount_collection = None
    shell.mount_entry = None
    shell.mount_leaf = None
    shell.namespace_view = None
    shell._resource_hierarchy = main.ResourceHierarchy(
        browser, shell._build_resource_context_children()
    )
    completed = main.CompletionEngine(shell).path(
        "conta", "cat bd/dev/conta", 4, len("cat bd/dev/conta")
    )
    resources = main.CompletionEngine(shell).path(
        "cl",
        "cat bd/dev/containerengine.clusters/cl",
        len("cat bd/dev/containerengine.clusters/"),
        len("cat bd/dev/containerengine.clusters/cl"),
    )
    browser.current = dev
    browser.parents = [browser.root, bd]
    catalog._collections_snapshot = {}
    fallback_namespace = shell._complete_current_collection_entries("conta")
    fallback_collection = shell._complete_current_collection_entries("containerengine.")
    ok = completed == ["containerengine.clusters"] and resources == [
        "cluster@1234abcd",
        "cluster@5678efgh",
    ] and fallback_namespace == ["containerengine."] and fallback_collection == [
        "containerengine.clusters"
    ]
    return {
        "spelling": "catalog dotted collection and unambiguous resource completion",
        "status": "ok" if ok else "missing",
    }


def audit_cd_primes_completion_cache() -> dict[str, object]:
    loads: list[bool] = []
    requests: list[str] = []
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = SimpleNamespace(
        current=SimpleNamespace(id="ocid1.compartment.dev"),
        list_current_compartment_resource_types=lambda: loads.append(True),
    )
    shell.catalog = SimpleNamespace(request_compartment=requests.append)
    shell._prime_current_compartment_completion()
    ok = loads == [True] and requests == ["ocid1.compartment.dev"]
    return {
        "spelling": "cd primes current-compartment completion inventory",
        "status": "ok" if ok else "missing",
    }


def audit_topology_completion_is_declarative() -> dict[str, object]:
    calls: list[object] = []

    class Shell:
        mount_collection = main.MountCollectionContext("topology")
        topology_context = None
        _completion_cache: dict[str, tuple[str, ...]] = {}

        def _completion_argument(self, _line, _begidx, _endidx, text):
            return text

        def _topology_completion_entries(self, topology):
            calls.append(topology)
            return ("vcns",)

        def _current_path_suffix(self):
            return "/topology"

    completed = main.CompletionEngine(Shell()).resource_path("v", "ll v", 3, 4)
    ok = completed == ["vcns"] and calls == [None]
    return {
        "spelling": "topology completion uses declarative child resolver",
        "status": "ok" if ok else "missing",
    }


def audit_topology_uses_one_transition_machine() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.topology_context = None
    shell._advance_topology("vcns")
    root_ok = shell.topology_context == main.TopologyContext(level="vcns")
    shell.topology_context = main.TopologyContext(
        level="vcn", vcn=main.ResourceRow("vcn", "ACTIVE", "ocid1.vcn.example")
    )
    shell._advance_topology("subnets")
    child_ok = shell.topology_context is not None and shell.topology_context.level == "subnets"
    return {
        "spelling": "topology root and child navigation share one transition machine",
        "status": "ok" if root_ok and child_ok else "missing",
    }


def audit_cmd_uses_gnu_readline() -> dict[str, object]:
    """cmd.Cmd must register completion with the configured GNU module."""
    import cmd
    import sys

    calls: list[object] = []
    original_set_completer = main.readline.set_completer

    def record_completer(callback: object) -> None:
        calls.append(callback)
        original_set_completer(callback)

    class Probe(cmd.Cmd):
        prompt = ""

        def do_EOF(self, _arg: str) -> bool:
            return True

    main.readline.set_completer = record_completer
    try:
        probe = Probe()
        probe.cmdqueue = ["EOF"]
        probe.cmdloop()
    finally:
        main.readline.set_completer = original_set_completer
    ok = sys.modules.get("readline") is main.readline and any(
        callable(callback) for callback in calls
    )
    return {
        "spelling": "cmd loop uses GNU readline completion module",
        "status": "ok" if ok else "missing",
    }


def audit_child_filter_precedes_name_enrichment() -> dict[str, object]:
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    spec = main.ResourceSpec(
        "core", "vnic_attachments", "iaas", "compartment", lister_name="_fake_rows"
    )
    browser._resolve_resource_spec = lambda _type: spec
    browser._fake_rows = lambda _filters: [
        main.ResourceRow(
            "attachment",
            "ATTACHED",
            "ocid1.attachment.example",
            details={"instance_id": "ocid1.instance.example"},
        )
    ]
    browser._remember_row_names = lambda _rows: None
    browser._enrich_relationship_names = lambda rows: [
        main.ResourceRow(
            row.name, row.state, row.id, details={"instance_id": "instance-name"}
        )
        for row in rows
    ]
    browser._uniquify_row_names = lambda rows: rows
    rows = browser.list_resources(
        "core.vnic-attachments", row_filter=("instance_id", "ocid1.instance.example")
    )
    ok = len(rows) == 1 and rows[0].details == {"instance_id": "instance-name"}
    return {
        "spelling": "child filters run before relationship name enrichment",
        "status": "ok" if ok else "missing",
    }


def audit_find_all_requires_compartment_context() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.resource_context = object()
    shell.collection_context = None
    shell._find_all_ocids_via_search = lambda: (_ for _ in ()).throw(
        AssertionError("must not search")
    )
    import io
    from contextlib import redirect_stdout

    output = io.StringIO()
    with redirect_stdout(output):
        shell.do_find(".")
    ok = (
        output.getvalue().strip()
        == "find . is available only in a compartment context; use ls or cd .."
    )
    return {
        "spelling": "find . is compartment-scoped",
        "status": "ok" if ok else "missing",
    }


def audit_qualified_collection_path_completion() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._completion_cache = {"/identity/users": ("alice", "admin")}
    completed = shell._complete_qualified_collection_path("identity.users/al")
    ok = completed == ["alice"]
    return {
        "spelling": "dotted qualified collection path completion",
        "status": "ok" if ok else "missing",
    }


def audit_resource_completion_boundary() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._completion_cache = {"/fvass/core/instances": ("minikube",)}
    completed = shell._complete_qualified_collection_path("core.instances/minikube")
    ok = completed == ["minikube/"]
    return {
        "spelling": "resource completion adds field-path boundary",
        "status": "ok" if ok else "missing",
    }


def audit_collection_completion_is_not_namespace_completion() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = "core"
    shell.collection_context = object()
    shell.resource_context = None
    completed = shell._context_resource_completions("all")
    ok = completed == []
    return {
        "spelling": "collection completion excludes namespace resource types",
        "status": "ok" if ok else "missing",
    }


def audit_collection_entry_name_completion() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("core.instances")
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.collection_context = main.CollectionContext(spec, "instances")
    shell.resource_context = None
    shell._completion_cache = {"/bd/dev/core/instances": ("api", "bastion")}
    shell._current_path_suffix = lambda: "/bd/dev/core/instances"
    completed = shell._context_resource_completions("ba")
    ok = completed == ["bastion"]
    return {
        "spelling": "collection context completes cached entry names",
        "status": "ok" if ok else "missing",
    }


def audit_completion_help() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    import io
    from contextlib import redirect_stdout

    output = io.StringIO()
    with redirect_stdout(output):
        shell.help_completion()
    text = output.getvalue()
    ok = all(term in text for term in ("never calls OCI", "off", "static", "cached"))
    return {
        "spelling": "completion capability help",
        "status": "ok" if ok else "missing",
    }


def audit_completion_modes() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.resource_context = shell.collection_context = shell.mount_collection = (
        shell.mount_entry
    ) = shell.mount_leaf = None
    shell.namespace_view = None
    shell._completion_cache = {"/fvass/core/instances": ("minikube",)}
    engine = main.CompletionEngine(shell)
    cached = engine.path(
        "mini", "cd core.instances/mini", 3, len("cd core.instances/mini")
    )
    engine.set_mode("static")
    static = engine.path(
        "mini", "cd core.instances/mini", 3, len("cd core.instances/mini")
    )
    engine.set_mode("off")
    off = engine.path("", "cd ", 3, 3)
    ok = "minikube" in cached and static == [] and off == []
    return {"spelling": "central completion modes", "status": "ok" if ok else "missing"}


def audit_cd_previous_locator() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    restored: list[object] = []
    prompts: list[bool] = []
    shell.session_region = "eu-frankfurt-1"
    shell._previous_locator = ("previous", "uk-london-1")
    shell._snapshot_locator = lambda: "current"
    shell._restore_locator = lambda snapshot: restored.append(snapshot)
    shell._update_prompt = lambda: prompts.append(True)
    shell.do_cd("-")
    ok = (
        restored == ["previous"]
        and shell.session_region == "uk-london-1"
        and shell._previous_locator == ("current", "eu-frankfurt-1")
        and prompts == [True]
    )
    return {
        "spelling": "cd - restores and toggles the previous locator",
        "status": "ok" if ok else "missing",
    }


def audit_active_region_catalog_completion() -> dict[str, object]:
    browser = build_browser()
    browser.root = SimpleNamespace(name="ionsfa9e57")
    browser.parents = []
    browser.current = SimpleNamespace(id="ocid1.compartment.example", name="fvass")
    browser.region = lambda: "eu-frankfurt-1"
    spec = browser.resolve_resource_spec("core.instances")
    catalog = main.ActiveRegionCatalog.__new__(main.ActiveRegionCatalog)
    catalog.browser = browser
    catalog._lock = __import__("threading").Lock()
    catalog._region = "eu-frankfurt-1"
    catalog._snapshot = {(browser.current.id, spec.qualified_name): ("minikube",)}
    catalog._id_snapshot = {
        (browser.current.id, spec.qualified_name): ("ocid1.instance.example",)
    }
    catalog._refreshed_at = 1.0
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.catalog = catalog
    completed = shell._catalog_resource_completions("~/fvass/core/instances/mini")
    id_completed = shell._catalog_resource_completions(
        "~/fvass/core/instances/ocid1.instance"
    )
    ok = completed == ["minikube"] and id_completed == ["ocid1.instance.example"]
    return {
        "spelling": "active-region catalog completion",
        "status": "ok" if ok else "missing",
    }


def audit_stat_envelope_preserves_raw_payload() -> dict[str, object]:
    ocid = "ocid1.vcn.example"
    payload = {"display_name": "VCN1", "id": ocid, "vcn_id": ocid}
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.resource_context = main.ResourceContext(
        main.ResourceSpec("core", "instances", "iaas", "compartment"),
        main.ResourceRow(
            "instance", "RUNNING", "ocid1.instance.example", payload=payload
        ),
    )
    shell.collection_context = None
    shell.browser = SimpleNamespace(
        current=SimpleNamespace(id="ocid1.compartment.example"),
        get_path=lambda: "/tenancy/dev",
    )
    shell._effective_region = lambda: "eu-frankfurt-1"
    shell._resource_entries = list
    shell._current_node_state = lambda: main.NodeState(
        "resource", "/tenancy/dev/core/instances/instance"
    )
    stat = shell._current_payload()
    ok = (
        stat["kind"] == "resource"
        and stat["canonical_path"]
        == "/eu-frankfurt-1/tenancy/dev/core/instances/instance"
        and stat["oci"] == {"type": "core.instances", "id": "ocid1.instance.example"}
        and stat["scope"]["compartment"]["id"] == "ocid1.compartment.example"
        and stat["data"] == payload
        and stat["freshness"] == {"state": "not-tracked", "cached_at": None}
    )
    return {
        "spelling": "cat stat envelope preserves raw OCI JSON",
        "status": "ok" if ok else "missing",
    }


def audit_direct_ocid_resource_locator() -> dict[str, object]:
    browser = build_browser()
    browser.current = SimpleNamespace(id="ocid1.compartment.example", name="fvass")
    spec = browser.resolve_resource_spec("core.instances")
    row = main.ResourceRow("minikube", "RUNNING", "ocid1.instance.example")
    browser.resolve_resource_id = lambda actual_spec, _ocid: main.RelationshipTarget(
        actual_spec, row, "ocid1.compartment.example"
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.collection_context = main.CollectionContext(spec, "instances")
    shell.resource_context = None
    entered = shell._try_enter_resource_from_collection("ocid1.instance.example")
    ok = entered and shell.resource_context == main.ResourceContext(spec, row)
    return {
        "spelling": "direct OCID resource locator avoids collection listing",
        "status": "ok" if ok else "missing",
    }


def audit_qualified_resource_field_completion() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._resource_fields = main.ResourceFieldResolver()
    shell._resource_context_children_map = {}
    shell._resource_completion_cache = {
        "/fvass/core/instances": {
            "minikube": main.ResourceRow(
                "minikube",
                "RUNNING",
                "ocid1.instance.example",
                payload={"private_ip": "10.0.0.2"},
            )
        }
    }
    completed = shell._complete_qualified_resource_path("core.instances/minikube/pri")
    ok = completed == ["private-ip"]
    return {
        "spelling": "qualified resource field completion from local row cache",
        "status": "ok" if ok else "missing",
    }


def audit_find_all_ocids() -> dict[str, object]:
    calls: list[tuple[object, dict[str, object]]] = []

    class Search:
        def search_resources(self, details: object, **kwargs: object) -> object:
            calls.append((details, kwargs))
            return SimpleNamespace(
                data=SimpleNamespace(
                    items=[SimpleNamespace(identifier="ocid1.instance.example")],
                    opc_next_page=None,
                )
            )

    class Browser:
        current = SimpleNamespace(id="ocid1.compartment.example")
        resource_search = Search()

        @staticmethod
        def _retry_oci_call(func: object, *args: object, **kwargs: object) -> object:
            return func(*args, **kwargs)

    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = Browser()
    ocids = shell._find_all_ocids_via_search()
    details, kwargs = calls[0]
    query = getattr(details, "query", "")
    ok = (
        ocids == ["ocid1.instance.example"]
        and query
        == "query all resources where compartmentId = 'ocid1.compartment.example'"
        and kwargs.get("limit") == shell.FIND_ALL_PAGE_SIZE
        and kwargs.get("retry_strategy") is not None
    )
    return {
        "spelling": "find . OCI Search OCID listing",
        "status": "ok" if ok else "missing",
    }


def audit_typed_namespace_resolver() -> dict[str, object]:
    """A path must resolve identically for every command consumer."""
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell._resource_fields = main.ResourceFieldResolver()
    shell._snapshot_locator = lambda: None
    shell._restore_locator = lambda _snapshot: None
    shell.collection_context = None
    shell.resource_context = None
    resource = main.ResourceContext(
        spec=main.ResourceSpec(
            namespace="core",
            name="security_lists",
            endpoint_family="iaas",
            scope="compartment",
        ),
        row=main.ResourceRow(
            name="private",
            state="AVAILABLE",
            id="ocid1.securitylist.example",
            payload={"system_tags": {"oracle": "tag"}},
        ),
    )
    calls: list[str] = []

    def change(target: str) -> None:
        calls.append(target)
        if target == "core.security-lists":
            shell.collection_context = object()
            shell.resource_context = None
        elif target in {"private", "core.security-lists/private"}:
            shell.collection_context = object()
            shell.resource_context = resource
        else:
            raise ValueError(target)

    shell._change_locator = change
    directory = shell._resolve_namespace_node("core.security-lists/private")
    directory_ok = directory.kind == "directory" and calls == [
        "core.security-lists",
        "private",
    ]
    calls.clear()
    shell.collection_context = shell.resource_context = None
    field = shell._resolve_namespace_node("core.security-lists/private/system-tags")
    field_ok = (
        field.kind == "field"
        and field.value == {"oracle": "tag"}
        and calls == ["core.security-lists/private"]
    )
    ok = directory_ok and field_ok
    return {
        "spelling": "typed resource path resolver",
        "status": "ok" if ok else "missing",
    }


def audit_universal_logs_projection() -> dict[str, object]:
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.resource_context = main.ResourceContext(
        main.ResourceSpec(
            "load_balancer", "load_balancers", "load_balancer", "compartment"
        ),
        main.ResourceRow("public", "ACTIVE", "ocid1.loadbalancer.example"),
    )
    shell._resource_fields = main.ResourceFieldResolver()
    shell._resource_context_children_map = {}
    entries = {entry.name: entry.kind for entry in shell._resource_entries()}
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.log_source_relation_cache = {}
    browser.region = lambda: "eu-frankfurt-1"
    source_id = "ocid1.subnet.example"
    vcn_id = "ocid1.vcn.example"
    log = main.ResourceRow(
        "flow",
        "ACTIVE",
        "ocid1.log.example",
        payload={"configuration": {"source": {"resource": source_id}}},
    )
    browser._all_log_rows = lambda: [log]
    browser.hydrate_resource_row = lambda _row: main.ResourceRow(
        source_id, "AVAILABLE", source_id, payload={"vcn_id": vcn_id}
    )
    related = browser.list_related_logs(vcn_id)
    ok = entries.get("logs") == "projection" and related == [log]
    return {
        "spelling": "every resource logs projection",
        "status": "ok" if ok else "missing",
    }


def audit_logging_log_group_is_navigable() -> dict[str, object]:
    """A log group must be enterable to reach its declared logs child."""
    browser = build_browser()
    spec = browser.resolve_resource_spec("logging.log_groups")
    log_group_id = "ocid1.loggroup.oc1..example"
    row = main.ResourceRow(
        "grp_devops", "ACTIVE", log_group_id, payload={"id": log_group_id}
    )
    ok = main.OciNavShell._row_is_navigable(spec, row)
    return {
        "spelling": "logging log groups are navigable to logs",
        "status": "ok" if ok else "missing",
    }


def audit_tenancy_logging_uses_log_group_index() -> dict[str, object]:
    """The tenancy-root logging domain must aggregate log groups by owner."""
    browser = build_browser()
    browser.root = main.CompartmentNode(
        "ocid1.tenancy.example", "tenancy", None, "ACTIVE", None
    )
    browser.current = browser.root
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = "logging"
    shell.resource_context = None
    shell.collection_context = None
    shell._resource_hierarchy = main.ResourceHierarchy(
        browser, shell._build_resource_context_children()
    )
    entered = shell._try_enter_collection_context("log-groups")
    context = shell.collection_context
    ok = (
        entered and context is not None and context.virtual_kind == "tenancy-log-groups"
    )
    return {
        "spelling": "tenancy logging aggregates log groups",
        "status": "ok" if ok else "missing",
    }


def audit_log_entries_projection() -> dict[str, object]:
    """Configured logs must query their own bounded Logging Search stream."""
    browser = build_browser()
    browser.current = main.CompartmentNode(
        "ocid1.compartment.oc1..example", "dev", None, "ACTIVE", None
    )
    calls: list[tuple[object, int, str | None]] = []

    class Search:
        def search_logs(
            self, details: object, *, limit: int, page: str | None = None
        ) -> object:
            calls.append((details, limit, page))
            data = {
                "datetime": "2026-09-20T12:00:00Z",
                "level": "ERROR",
                "logContent": "failed request",
            }
            if page:
                data["datetime"] = "2026-09-19T12:00:00Z"
            return SimpleNamespace(
                data=SimpleNamespace(results=[SimpleNamespace(data=data)]),
                next_page=None if page else "second-page",
            )

    browser.logging_search = Search()
    browser._retry_oci_call = lambda func, *args, **kwargs: func(*args, **kwargs)
    rows = browser.list_log_entries(
        main.ResourceRow(
            "app-log",
            "ACTIVE",
            "ocid1.log.oc1..example",
            payload={"log_group_id": "ocid1.loggroup.oc1..example"},
        )
    )
    details, limit, page = calls[0]
    filtered_rows = browser.list_log_entries(
        main.ResourceRow(
            "app-log",
            "ACTIVE",
            "ocid1.log.oc1..example",
            payload={"log_group_id": "ocid1.loggroup.oc1..example"},
        ),
        page_number=2,
        since="2026-09-19T00:00:00Z",
        until="2026-09-20T00:00:00Z",
        contains="failed",
        where="logContent.data.status=200",
    )
    filtered_details, filtered_limit, filtered_page = calls[-1]
    query = getattr(details, "search_query", "")
    entry_shell = main.OciNavShell.__new__(main.OciNavShell)
    entry_shell.resource_context = main.ResourceContext(
        main.ResourceSpec("logging", "logs", "logging", "compartment"),
        main.ResourceRow("app-log", "ACTIVE", "ocid1.log.oc1..example"),
    )
    entry_shell._resource_fields = main.ResourceFieldResolver()
    entry_shell._resource_context_children_map = {}
    entry_names = {entry.name for entry in entry_shell._resource_entries()}
    ok = (
        len(rows) == 1
        and rows[0].state == "ERROR"
        and rows[0].extra == "failed request"
        and rows[0].name == "2026-09-20T12:00:00Z-1"
        and "ocid1.compartment.oc1..example/ocid1.loggroup.oc1..example/ocid1.log.oc1..example"
        in query
        and limit == browser.LOG_ENTRY_PAGE_SIZE
        and page is None
        and (details.time_end - details.time_start).total_seconds()
        == browser.LOG_ENTRY_WINDOW_SECONDS
        and browser.LOG_ENTRY_WINDOW_SECONDS == 14 * 24 * 60 * 60
        and len(filtered_rows) == 1
        and filtered_rows[0].name == "2026-09-19T12:00:00Z-1"
        and filtered_limit == browser.LOG_ENTRY_PAGE_SIZE
        and filtered_page == "second-page"
        and "logContent = '*failed*'" in filtered_details.search_query
        and "logContent.data.status = 200" in filtered_details.search_query
        and entry_names >= {"entries"}
        and "logs" not in entry_names
    )
    return {
        "spelling": "configured log entries use bounded Logging Search",
        "status": "ok" if ok else "missing",
    }


def audit_log_entries_keep_containment_path() -> dict[str, object]:
    """A log's entries must not escape its log-group/logs path."""
    browser = build_browser()
    browser.current = main.CompartmentNode(
        "ocid1.compartment.oc1..example", "dnicu", None, "ACTIVE", None
    )
    browser.parents = []
    group_spec = browser.resolve_resource_spec("logging.log-groups")
    log_spec = browser.resolve_resource_spec("logging.logs")
    group = main.ResourceContext(
        group_spec,
        main.ResourceRow("dicom-logs", "ACTIVE", "ocid1.loggroup.oc1..example"),
    )
    log = main.ResourceContext(
        log_spec,
        main.ResourceRow("api-access", "ACTIVE", "ocid1.log.oc1..example"),
    )
    logs = main.CollectionContext(
        spec=log_spec,
        collection_name="logs",
        parent_resource=group,
    )
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.namespace_view = "logging"
    shell.mount_collection = None
    shell.mount_entry = None
    shell.mount_leaf = None
    shell.resource_context = None
    shell.collection_context = main.CollectionContext(
        spec=log_spec,
        collection_name="entries",
        parent_resource=log,
        parent_collection=logs,
        virtual_kind="time-query",
        time_query_provider="logging",
    )
    entry_path = shell._current_path_suffix()
    shell._step_up()
    log_path = shell._current_path_suffix()
    ok = entry_path.endswith(
        "/logging/log-groups/dicom-logs/logs/api-access/entries"
    ) and log_path.endswith("/logging/log-groups/dicom-logs/logs/api-access")
    return {
        "spelling": "log entries retain canonical log-group containment path",
        "status": "ok" if ok else "missing",
    }


def audit_log_entry_query_paths() -> dict[str, object]:
    """Entry filters and pages compose as namespace paths."""
    shell = main.OciNavShell.__new__(main.OciNavShell)
    browser = build_browser()
    browser.current = main.CompartmentNode(
        "ocid1.compartment.oc1..example", "dev", None, "ACTIVE", None
    )
    browser.parents = []
    log_spec = browser.resolve_resource_spec("logging.logs")
    group_spec = browser.resolve_resource_spec("logging.log-groups")
    group = main.ResourceContext(
        group_spec, main.ResourceRow("group", "ACTIVE", "ocid1.loggroup.oc1..example")
    )
    log = main.ResourceContext(
        log_spec, main.ResourceRow("api", "ACTIVE", "ocid1.log.oc1..example")
    )
    shell.browser = browser
    shell.mount_leaf = None
    shell.mount_entry = None
    shell.mount_collection = None
    shell.resource_context = None
    shell.collection_context = main.CollectionContext(
        spec=log_spec,
        collection_name="entries",
        parent_resource=log,
        parent_collection=main.CollectionContext(
            spec=log_spec, collection_name="logs", parent_resource=group
        ),
        virtual_kind="time-query",
        time_query_provider="logging",
        time_query_path=("entries",),
    )
    shell._try_enter_collection_context("page")
    shell._try_enter_collection_context("2")
    page_ok = dict(shell.collection_context.time_query_options) == {"page_number": 2}
    shell._step_up()
    shell._step_up()
    shell._try_enter_collection_context("where")
    shell._try_enter_collection_context("logContent.data.status")
    shell._try_enter_collection_context("200")
    where_ok = dict(shell.collection_context.time_query_options) == {
        "where": "logContent.data.status=200"
    }
    return {
        "spelling": "log entry query paths",
        "status": "ok" if page_ok and where_ok else "missing",
    }


def audit_time_query_adapter_controls() -> dict[str, object]:
    """All query services select declared providers and use generic write state."""
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.resource_specs = browser._build_resource_specs()
    browser.current = main.CompartmentNode("compartment", "dev", None, "ACTIVE", None)
    browser.parents = []
    expected = {
        "audit.events",
        "monitoring.metrics",
        "apm.apm_domains",
        "log_analytics.queries",
    }
    specs_ok = all(browser.resolve_resource_spec(name).runnable for name in expected)
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.resource_context = None
    shell.mount_leaf = None
    shell.mount_entry = None
    shell.mount_collection = None
    shell.namespace_view = "monitoring"
    shell._effective_region = lambda: "eu-frankfurt-1"
    browser.get_path = lambda: "/dev"
    shell.collection_context = main.CollectionContext(
        spec=browser.resolve_resource_spec("monitoring.metrics"),
        collection_name="metrics",
        virtual_kind=main.VirtualKind.TIME_QUERY,
        time_query_provider="monitoring",
    )
    shell.do_write("namespace oci_computeagent")
    shell.do_write("query 'CpuUtilization[1m].mean()'")
    controls_ok = dict(shell.collection_context.time_query_options) == {
        "namespace": "oci_computeagent",
        "query": "CpuUtilization[1m].mean()",
    }
    shell.collection_context = main.CollectionContext(
        spec=shell.collection_context.spec,
        collection_name="query",
        virtual_kind=main.VirtualKind.TIME_QUERY_SELECTOR,
        time_query_provider="monitoring",
        time_query_options=(("query", "CpuUtilization[1m].mean()"),),
        time_query_pending="query",
    )
    control_read_ok = (
        shell._current_payload()["kind"] == "terminal-record"
        and shell._current_payload()["data"]["value"] == "CpuUtilization[1m].mean()"
    )
    shell.collection_context = main.CollectionContext(
        spec=browser.resolve_resource_spec("monitoring.metrics"),
        collection_name="metrics",
        virtual_kind=main.VirtualKind.TIME_QUERY,
        time_query_provider="monitoring",
        time_query_options=(
            ("namespace", "oci_computeagent"),
            ("query", "CpuUtilization[1m].mean()"),
        ),
    )
    shell._current_node_state = lambda: main.NodeState(
        "collection", "/dev/monitoring/metrics"
    )
    record = shell._time_query_record_payload(
        main.ResourceRow("metric-1", "-", "metric-1", payload={"value": 42})
    )
    record_ok = (
        record["kind"] == "terminal-record"
        and record["query"]["provider"] == "monitoring"
        and record["query"]["max_pages"] == 1
        and record["data"] == {"value": 42}
    )
    return {
        "spelling": "shared time/query adapter uses declared providers and controls",
        "status": "ok"
        if specs_ok and controls_ok and control_read_ok and record_ok
        else "missing",
    }


def audit_time_query_provider_requests() -> dict[str, object]:
    """Every provider applies controls to its OCI request and supports its pages."""
    browser = main.OciCompartmentBrowser.__new__(main.OciCompartmentBrowser)
    browser.current = main.CompartmentNode("compartment", "dev", None, "ACTIVE", None)
    browser._retry_oci_call = lambda function, *args, **kwargs: function(
        *args, **kwargs
    )

    class Response:
        def __init__(self, data, next_page=None):
            self.data, self.next_page = data, next_page

    audit_calls, apm_calls, la_calls = [], [], []
    browser.audit = type(
        "Audit",
        (),
        {
            "list_events": lambda _self, *args, **kwargs: (
                audit_calls.append((args, kwargs)) or Response([{"event_time": "a"}])
            )
        },
    )()
    browser.monitoring = type(
        "Monitoring",
        (),
        {
            "summarize_metrics_data": lambda _self, _compartment, details: Response(
                [{"time": "m"}]
            )
        },
    )()
    browser.apm_query = type(
        "Apm",
        (),
        {
            "query": lambda _self, *args, **kwargs: (
                apm_calls.append(kwargs)
                or Response({"query_result_rows": [{"time": "a"}]})
            )
        },
    )()
    browser.object_storage = type(
        "ObjectStorage", (), {"get_namespace": lambda _self: Response("namespace")}
    )()
    browser.log_analytics = type(
        "LogAnalytics",
        (),
        {
            "query": lambda _self, *args, **kwargs: (
                la_calls.append((args, kwargs)) or Response({"items": [{"time": "l"}]})
            )
        },
    )()
    audit_rows = browser._list_audit_events(None, page_number=1)
    metric_rows = browser._list_monitoring_metrics(
        None, query="CpuUtilization[1m].mean()", namespace="oci_computeagent"
    )
    apm_rows = browser._list_apm_query(
        main.ResourceRow("domain", "ACTIVE", "domain-id"),
        query="show traces",
        page_number=1,
    )
    la_rows = browser._list_log_analytics_query(
        None, query="* | stats count", page_number=1
    )
    la_details = la_calls[0][0][1]
    ok = (
        len(audit_calls) == 1
        and audit_calls[0][0][0] == "compartment"
        and len(audit_rows) == len(metric_rows) == len(apm_rows) == len(la_rows) == 1
        and apm_calls[0]["limit"] == 100
        and la_calls[0][0][0] == "namespace"
        and la_details.time_filter.time_start < la_details.time_filter.time_end
    )
    return {
        "spelling": "time/query providers apply OCI request controls",
        "status": "ok" if ok else "missing",
    }


def audit_static_schema_components_are_separated() -> dict[str, object]:
    import static_schema

    tree_source = static_schema.__file__
    source = open(tree_source, encoding="utf-8").read()
    ok = (
        callable(OciSdkCatalog.discover)
        and "OCISH_TERRAFORM_PROVIDER_OCI_ROOT" not in source
        and "pkgutil.iter_modules" not in source
    )
    return {
        "spelling": "static schema tree excludes SDK discovery and Terraform overlay",
        "status": "ok" if ok else "missing",
    }


def audit_static_schema_exposes_sdk_operations() -> dict[str, object]:
    from static_schema import OciSdkSchemaTree

    tree = OciSdkSchemaTree()
    clients = tree.children(("core",))
    operations = tree.children(("core", "ComputeClient", "operations"))
    payload = tree.payload(("core", "ComputeClient", "operations", "list_instances"))
    ok = (
        "ComputeClient" in clients
        and "list_instances" in operations
        and payload == {
            "kind": "operation",
            "client": "core.ComputeClient",
            "sdk_method": "list_instances",
        }
    )
    return {
        "spelling": "static schema exposes installed SDK client operations directly",
        "status": "ok" if ok else "missing",
    }


def audit_terraform_overlay_is_optional() -> dict[str, object]:
    resources = {"core": {"instance": ("get_instance",)}}
    previous = os.environ.pop("OCISH_TERRAFORM_PROVIDER_OCI_ROOT", None)
    try:
        apply_terraform_overlay(resources)
    finally:
        if previous is not None:
            os.environ["OCISH_TERRAFORM_PROVIDER_OCI_ROOT"] = previous
    ok = resources == {"core": {"instance": ("get_instance",)}}
    return {
        "spelling": "Terraform schema overlay is optional without configuration",
        "status": "ok" if ok else "missing",
    }


def audit_static_schema_target_restore() -> dict[str, object]:
    """Reading a static-schema target must not leak its path into the locator."""
    shell = main.OciNavShell.__new__(main.OciNavShell)
    browser = build_browser()
    browser.current = main.CompartmentNode("root", "tenancy", None, "ACTIVE", None)
    browser.parents = []
    shell.browser = browser
    shell.session_region = "test-region"
    shell.mount_collection = main.MountCollectionContext("oci")
    shell.mount_entry = None
    shell.mount_leaf = None
    shell.collection_context = None
    shell.resource_context = None
    shell.namespace_view = None
    shell.schema_path = ("zpr", "configuration")
    shell._sync_browser_region = lambda: None
    snapshot = shell._snapshot_locator()
    shell.schema_path = ("zpr", "configuration", "operations")
    shell._restore_locator(snapshot)
    return {
        "spelling": "static schema target reads restore locator path",
        "status": "ok" if shell.schema_path == ("zpr", "configuration") else "missing",
    }


def audit_registry_completion() -> dict[str, object]:
    browser = build_browser()
    expected = {
        spec.qualified_name.replace("_", "-")
        for spec in browser.resource_specs_for_namespace("core")
    }
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    completed = set(shell._complete_core_resource("core."))
    ok = completed == expected
    return {
        "spelling": "core.<Tab>",
        "status": "ok" if ok else "missing",
        "count": len(completed),
        "missing": sorted(expected - completed),
        "unexpected": sorted(completed - expected),
    }


def audit_static_qualified_completion() -> dict[str, object]:
    main.OciNavShell._configure_readline_completion()
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell._completion_cache = {}
    expected = {
        spec.qualified_name.replace("_", "-")
        for spec in browser.resource_specs_for_namespace()
    }
    completed = set(shell._complete_qualified_resource(""))
    line = "ls containerengine.virtual-node-po"
    partial = shell._complete_resource_argument(
        "containerengine.virtual-node-po", line, 3, len(line)
    )
    partial_ok = partial == ["containerengine.virtual-node-pools"]
    slash_line = "ls core/inst"
    slash = shell._complete_resource_argument(
        "inst", slash_line, len("ls core/"), len(slash_line)
    )
    slash_ok = "instances" in slash and "core.instances" not in slash
    shell.namespace_view = "core"
    namespace_ok = "instances" in shell._complete_resource_argument(
        "inst", "ls inst", 3, 7
    )
    shell.resource_context = main.ResourceContext(
        main.ResourceSpec("core", "instances", "iaas", "compartment"),
        main.ResourceRow(
            "bastion", "RUNNING", "ocid1.instance.example", payload={"system_tags": {}}
        ),
    )
    shell._resource_fields = main.ResourceFieldResolver()
    shell._resource_context_children_map = {}
    leaf_ok = {"state", "status"}.issubset(
        shell._complete_resource_argument("st", "cat st", 4, 6)
    )
    command_names = set(shell.completenames(""))
    command_root_ok = "ls" in command_names and "core.instances" not in command_names
    free_text_ok = shell.completedefault("query", "grep query", 5, 10) == []
    find_ok = shell.complete_find("inst", "find inst", 5, 9) == shell._complete_resource_argument(
        "inst", "find inst", 5, 9
    )
    delimiters = main.readline.get_completer_delims()
    delimiter_ok = all(character not in delimiters for character in ".-:@%")
    return {
        "spelling": "all service <Tab>",
        "status": "ok"
        if completed == expected
        and partial_ok
        and slash_ok
        and namespace_ok
        and leaf_ok
        and command_root_ok
        and free_text_ok
        and find_ok
        and delimiter_ok
        else "missing",
        "count": len(completed),
        "missing": sorted(expected - completed),
        "unexpected": sorted(completed - expected),
    }


def audit_resource_container_policy() -> dict[str, object]:
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    child_map = shell._build_resource_context_children()
    expected = list(browser.resource_specs)
    hierarchy = main.ResourceHierarchy(browser, child_map)
    policies = {item.qualified_name: hierarchy.policy_for(item) for item in expected}
    valid_roots = {"compartment", "tenancy", "region"}
    canonical_edges: list[str] = []
    projection_edges: list[str] = []
    violations: list[str] = []
    for parent, children in child_map.items():
        parent_spec = browser.resolve_resource_spec(parent)
        parent_collection = browser._normalize_resource_token(parent_spec.name)
        parent_label = hierarchy._singular(parent_collection)
        for child_name, child in children.items():
            child_spec = browser.resolve_resource_spec(child.resource_type)
            path = f"{parent_collection}/<{parent_label}>/{child_name}"
            policy = hierarchy.policy_for(child_spec)
            if child_spec.runnable:
                projection_edges.append(path)
                if path not in policy.projection_paths:
                    violations.append(
                        f"missing projection: {child_spec.qualified_name} ← {path}"
                    )
            else:
                canonical_edges.append(path)
                if path not in policy.parent_paths:
                    violations.append(
                        f"missing canonical parent: {child_spec.qualified_name} ← {path}"
                    )
    for spec in expected:
        policy = policies[spec.qualified_name]
        if policy.parent_paths and spec.runnable:
            violations.append(
                f"runnable resource has canonical parent: {spec.qualified_name}"
            )
    ok = (
        all(policy.root in valid_roots for policy in policies.values())
        and not violations
    )
    return {
        "spelling": "all resource ownership and relationship paths",
        "status": "ok" if ok else "missing",
        "resources": len(expected),
        "scope_owned": sum(policy.permits_flat_access for policy in policies.values()),
        "parent_owned": sum(
            not policy.permits_flat_access for policy in policies.values()
        ),
        "relationship_projections": len(projection_edges),
        "canonical_parent_edges": len(canonical_edges),
        "missing": violations,
        "unexpected": [],
    }


def audit_unsupported_child_collection_is_hidden() -> dict[str, object]:
    """An older OCI SDK must not make shell initialization fail."""
    browser = build_browser()
    resolve = browser.resolve_resource_spec

    def resolve_without_provider_remote_regions(
        resource_type: str,
    ) -> main.ResourceSpec:
        if resource_type == "provider-remote-regions":
            raise ValueError("unsupported resource type: provider-remote-regions")
        return resolve(resource_type)

    browser.resolve_resource_spec = resolve_without_provider_remote_regions
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    child_map = shell._build_resource_context_children()
    hierarchy = main.ResourceHierarchy(browser, child_map)
    children = child_map.get("core.fast_connect_provider_services", {})
    ok = "provider-remote-regions" not in children and hierarchy is not None
    return {
        "spelling": "unsupported SDK child collection is hidden at startup",
        "status": "ok" if ok else "missing",
    }


def audit_scoped_child_list_precedence() -> dict[str, object]:
    """Children with a direct scope argument must never be projected by Search."""
    browser = build_browser()
    shell = main.OciNavShell.__new__(main.OciNavShell)
    shell.browser = browser
    shell.collection_context = None
    shell.resource_context = main.ResourceContext(
        browser.resolve_resource_spec("core.vcns"),
        main.ResourceRow("VCN1", "AVAILABLE", "ocid1.vcn.example"),
    )
    shell._resource_context_children_map = {
        "core.vcns": {
            "local-peering-gateways": main.ResourceChildCollection(
                "core.local_peering_gateways", (("vcn_id", "id"),), None
            )
        }
    }
    entered = shell._try_enter_collection_context("local-peering-gateways")
    context = shell.collection_context
    ok = (
        entered
        and context is not None
        and dict(context.extra_kwargs) == {"vcn_id": "ocid1.vcn.example"}
        and context.projection_field is None
    )
    return {
        "spelling": "scoped child list APIs take precedence over Search projection",
        "status": "ok" if ok else "missing",
    }


def audit_cluster_node_pool_options() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("containerengine.node-pool-options")
    child = config.RESOURCE_CONTEXT_CHILDREN["containerengine.clusters"].get(
        "node-pool-options"
    )
    options = main.oci.container_engine.models.NodePoolOptions(
        kubernetes_versions=["v1.31.1"],
        shapes=["VM.Standard.E4.Flex", "VM.Standard.A1.Flex"],
        images=["Oracle-Linux-8.10"],
        sources=[{"sourceType": "IMAGE"}],
    )
    browser.container_engine = SimpleNamespace(
        get_node_pool_options=lambda option_id: SimpleNamespace(data=options)
    )
    browser._retry_oci_call = lambda operation, *args: operation(*args)
    rows = browser._list_node_pool_options(
        {"node_pool_option_id": "ocid1.cluster.oc1.iad.example"}
    )
    row = rows[0] if len(rows) == 1 else None
    ok = (
        child
        == (
            "containerengine.node-pool-options",
            (("node_pool_option_id", "id"),),
            None,
        )
        and spec.lister_name == "_list_node_pool_options"
        and spec.node_capability == "terminal-record-set"
        and row is not None
        and row.name == "options"
        and row.details
        == {"kubernetes_versions": 1, "shapes": 2, "images": 1, "sources": 1}
        and row.payload is not None
        and row.payload.get("node_pool_option_id") == "ocid1.cluster.oc1.iad.example"
    )
    return {
        "spelling": "cluster node-pool-options is a terminal options record",
        "status": "ok" if ok else "missing",
    }


def audit_addon_options_metadata_records() -> dict[str, object]:
    browser = build_browser()
    spec = browser.resolve_resource_spec("containerengine.addon-options")
    enabled_spec = browser.resolve_resource_spec("containerengine.addons")
    row = main.ResourceRow(
        "OciVcnIpNative",
        "ACTIVE",
        "OciVcnIpNative",
        payload={
            "addon_group": "oke/cluster-network",
            "addon_schema_version": "v1.0.0",
            "description": "VCN-native pod networking.",
        },
    )
    resource = main.ResourceContext(spec, row)
    fields = main.ResourceFieldResolver().names_for(resource)
    ok = (
        spec.node_capability == "metadata-record"
        and enabled_spec.node_capability == "metadata-record"
        and main.OciNavShell._row_is_navigable(spec, row)
        and main.OciNavShell._row_is_navigable(enabled_spec, row)
        and {"addon-group", "addon-schema-version", "description", "raw-json"}.issubset(
            fields
        )
        and main.ResourceFieldResolver().resolve(resource, "raw-json") == row.payload
    )
    return {
        "spelling": "cluster addons and addon options expose metadata fields and raw-json",
        "status": "ok" if ok else "missing",
    }


def main_cli() -> int:
    child_mappings = audit_core_child_mappings()
    resource_resolution = audit_core_resource_resolution()
    dns_resolution = audit_dns_resource_resolution()
    generative_ai_resolution = audit_generative_ai_resource_resolution()
    report = (
        child_mappings
        + resource_resolution
        + dns_resolution
        + generative_ai_resolution
        + audit_registry_public_taxonomy()
        + [
            audit_compartment_ls_resource_types(),
            audit_namespace_view_uses_current_compartment_inventory(),
            audit_bare_compartment_completion(),
            audit_identity_compartment_entry_changes_context(),
            audit_resource_status_leaf(),
            audit_find_argument_forms(),
            audit_collection_views_are_bounded_and_inspectable(),
            audit_vcn_teardown_helpers_live_on_shell(),
            audit_subnet_service_vnic_blockers(),
            audit_network_firewall_namespace(),
            audit_resource_adapter_kinds_are_declarative(),
            audit_limits_namespace(),
            audit_topology_edges_are_declarative(),
            audit_direct_relationship_fallback_for_instance_images(),
            audit_network_firewall_delete_preflight(),
            audit_generic_delete_capability_discovery(),
            audit_vcn_blocker_listing_accepts_collection_responses(),
            audit_oci_failure_record_is_readable(),
            audit_find_respects_current_service_namespace(),
            audit_find_dot_uses_service_scoped_search(),
            audit_find_uses_generic_search_for_clusters(),
            audit_find_falls_back_to_registered_resource_lister(),
            audit_find_skips_unavailable_resource_lister(),
            audit_find_explicit_type_merges_resource_lister(),
            audit_find_paths_are_canonical_and_unambiguous(),
            audit_relationship_projection_falls_back_to_resource_lister(),
            audit_relationship_projection_reconciles_empty_search(),
            audit_ls_combined_options(),
            audit_catalog_paginates_search_type_discovery(),
            audit_hyphenated_namespace_resolution(),
            audit_namespace_path_slug_policy(),
            audit_explicit_special_capabilities(),
            audit_load_balancer_listener_name(),
            audit_load_balancer_long_columns(),
            audit_operational_default_columns(),
            audit_ll_alias(),
            audit_subnet_access_column(),
            audit_address_column_priority(),
            audit_operational_column_formula(),
            audit_load_balancer_health_column(),
            audit_load_balancer_lister_keeps_rows_immutable(),
            audit_opaque_relationship_id_suppression(),
            audit_relationship_name_column(),
            audit_relationship_precedes_created(),
            audit_created_is_final_column(),
            audit_general_relationship_enrichment(),
            audit_security_rule_nsg_name_rendering(),
            audit_region_mount_uses_qualified_type(),
            audit_virtual_relationship_symlink(),
            audit_compartment_is_metadata_not_symlink(),
            audit_child_attributes_are_not_parent_symlinks(),
            audit_indirect_relationships_require_policy(),
            audit_repeated_relationships_are_directories(),
            audit_cluster_work_request_projection(),
            audit_absolute_locator_completion(),
            audit_ls_cached_collection_path_completion(),
            audit_cd_primes_completion_cache(),
            audit_topology_completion_is_declarative(),
            audit_topology_uses_one_transition_machine(),
            audit_cmd_uses_gnu_readline(),
            audit_child_filter_precedes_name_enrichment(),
            audit_find_all_requires_compartment_context(),
            audit_qualified_collection_path_completion(),
            audit_resource_completion_boundary(),
            audit_collection_completion_is_not_namespace_completion(),
            audit_collection_entry_name_completion(),
            audit_completion_help(),
            audit_completion_modes(),
            audit_cd_previous_locator(),
            audit_active_region_catalog_completion(),
            audit_direct_ocid_resource_locator(),
            audit_stat_envelope_preserves_raw_payload(),
            audit_qualified_resource_field_completion(),
            audit_find_all_ocids(),
            audit_typed_namespace_resolver(),
            audit_universal_logs_projection(),
            audit_logging_log_group_is_navigable(),
            audit_tenancy_logging_uses_log_group_index(),
            audit_log_entries_projection(),
            audit_log_entries_keep_containment_path(),
            audit_log_entry_query_paths(),
            audit_time_query_adapter_controls(),
            audit_time_query_provider_requests(),
            audit_static_schema_components_are_separated(),
            audit_static_schema_exposes_sdk_operations(),
            audit_terraform_overlay_is_optional(),
            audit_static_schema_target_restore(),
            audit_registry_completion(),
            audit_static_qualified_completion(),
            audit_resource_container_policy(),
            audit_unsupported_child_collection_is_hidden(),
            audit_scoped_child_list_precedence(),
            audit_cluster_node_pool_options(),
            audit_addon_options_metadata_records(),
            audit_generative_ai_child_mappings(),
            audit_generative_ai_data_adapter_metadata(),
            audit_generative_ai_direct_catalog_presence(),
        ]
    )
    missing = [row for row in report if row["status"] != "ok"]
    print(
        json.dumps(
            {
                "summary": {"total": len(report), "missing": len(missing)},
                "items": report,
            },
            indent=2,
        )
    )
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
