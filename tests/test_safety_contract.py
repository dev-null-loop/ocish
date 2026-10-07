from __future__ import annotations

from contextlib import suppress

import pytest

from deletion import DeletionManager
from inventory import OciCompartmentBrowser


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
