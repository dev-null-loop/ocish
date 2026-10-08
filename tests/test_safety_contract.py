from __future__ import annotations

from contextlib import suppress

import pytest

import main
from deletion import DeletionManager
from inventory import OciCompartmentBrowser
from models import CompartmentNode, ResourceRow, ResourceSpec


class _ServiceUnavailable(Exception):
    status = 503
    code = "ServiceUnavailable"
    message = "service not available"
    request_id = "request-partial"


class _PermissionDenied(Exception):
    status = 403
    code = "NotAuthorized"
    message = "not authorized"
    request_id = "request-denied"


class _BlockerBrowser:
    root = CompartmentNode("root", "tenancy", None, "ACTIVE", None)

    def build_active_region_compartment_catalog(self):
        parent = CompartmentNode("parent", "platform", None, "ACTIVE", "root")
        child = CompartmentNode("child", "workloads", None, "ACTIVE", "parent")
        return {"root": (parent,), "parent": (child,)}

    @staticmethod
    def _normalize_resource_token(value: str) -> str:
        return value.replace("_", "-")

    @staticmethod
    def _sanitize_row_name(value: str) -> str:
        return value


class _BlockerShell:
    browser = _BlockerBrowser()

    @staticmethod
    def _effective_region() -> str:
        return "us-phoenix-1"

    @staticmethod
    def _namespace_slug(value: str) -> str:
        return value.replace("_", "-")


def test_best_effort_oci_failure_is_explicitly_partial() -> None:
    browser = OciCompartmentBrowser.__new__(OciCompartmentBrowser)
    browser.record_partial_failure("relationship_projection", _ServiceUnavailable())

    assert browser.last_oci_failure["state"] == "partial"
    assert browser.last_oci_failure["kind"] == "service-unavailable"
    assert browser.last_oci_failure["operation"] == "relationship_projection"
    assert browser.last_oci_failure["status"] == 503
    assert browser.last_oci_failure["code"] == "ServiceUnavailable"
    assert browser.last_oci_failure["message"] == "service not available"
    assert browser.last_oci_failure["request_id"] == "request-partial"
    assert browser.last_oci_failure["timestamp"]


def test_blocking_oci_failure_is_not_marked_partial() -> None:
    browser = OciCompartmentBrowser.__new__(OciCompartmentBrowser)
    browser.THROTTLE_BASE_DELAY = 0
    browser.THROTTLE_RETRIES = 0

    with suppress(_PermissionDenied):
        browser._retry_oci_call(lambda: (_ for _ in ()).throw(_PermissionDenied()))

    assert browser.last_oci_failure["state"] == "error"
    assert browser.last_oci_failure["kind"] == "permission-denied"
    assert browser.last_oci_failure["request_id"] == "request-denied"


def test_readline_configuration_skips_unicode_binding_in_ascii_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings: list[str] = []

    class Readline:
        @staticmethod
        def get_completer_delims() -> str:
            return " .-:@%"

        @staticmethod
        def set_completer_delims(_delimiters: str) -> None:
            return None

        @staticmethod
        def parse_and_bind(binding: str) -> None:
            bindings.append(binding)

    monkeypatch.setattr(main, "readline", Readline())
    monkeypatch.setattr(main.locale, "getencoding", lambda: "ascii")

    main.OciNavShell._configure_readline_completion()

    assert bindings == ['"\\e.": yank-last-arg']


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("core/vcns/network", (False, False, "core/vcns/network")),
        ("-r core/vcns/network", (False, True, "core/vcns/network")),
        ("--apply core/vcns/network", (True, False, "core/vcns/network")),
        ("-r --apply core/vcns/network", (True, True, "core/vcns/network")),
    ],
)
def test_rm_requires_an_explicit_apply_flag(
    arguments: str, expected: tuple[bool, bool, str]
) -> None:
    manager = DeletionManager.__new__(DeletionManager)
    assert manager._parse_rm_args(arguments) == expected


@pytest.mark.parametrize("arguments", ("", "-f", "--unknown path", "one two"))
def test_rm_rejects_ambiguous_or_invalid_arguments(arguments: str) -> None:
    manager = DeletionManager.__new__(DeletionManager)
    with pytest.raises(ValueError):
        manager._parse_rm_args(arguments)


def test_vcn_blocker_paths_preserve_cross_compartment_ownership() -> None:
    manager = DeletionManager(_BlockerShell())
    current = CompartmentNode("child", "workloads", None, "ACTIVE", "parent")
    parents = (CompartmentNode("parent", "platform", None, "ACTIVE", "root"),)
    spec = ResourceSpec("load_balancer", "load_balancers", "", "compartment")
    row = ResourceRow("public", "ACTIVE", "ocid1.loadbalancer.example")

    assert manager._canonical_blocker_path(spec, row, current, parents) == (
        "/us-phoenix-1/tenancy/platform/workloads/"
        "load-balancer/load-balancers/public"
    )


def test_vcn_blocker_scan_includes_every_accessible_compartment() -> None:
    manager = DeletionManager(_BlockerShell())
    current = CompartmentNode("child", "workloads", None, "ACTIVE", "parent")
    scopes, complete = manager._accessible_blocker_scopes(
        current,
        (CompartmentNode("parent", "platform", None, "ACTIVE", "root"),),
    )

    assert complete
    assert [(node.id, tuple(parent.id for parent in parents)) for node, parents in scopes] == [
        ("parent", ()),
        ("child", ("parent",)),
    ]
