from __future__ import annotations

import pytest

from inventory import OciCompartmentBrowser


@pytest.fixture
def browser() -> OciCompartmentBrowser:
    """Build the declarative resource registry without OCI credentials or calls."""
    instance = OciCompartmentBrowser.__new__(OciCompartmentBrowser)
    instance.resource_specs = instance._build_resource_specs()
    return instance
