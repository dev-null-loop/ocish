"""Optional Terraform provider naming overlay for an SDK resource catalog."""

from __future__ import annotations

import os
from pathlib import Path


def apply(resources: dict[str, dict[str, tuple[str, ...]]]) -> None:
    """Replace catalog names with Terraform resource coverage when configured."""
    provider_root = os.getenv("OCISH_TERRAFORM_PROVIDER_OCI_ROOT")
    if not provider_root:
        return
    root = Path(provider_root).expanduser()
    docs = root / "website/docs/r"
    services_root = root / "internal/service"
    if not docs.is_dir() or not services_root.is_dir():
        return
    provider_services = sorted(
        (path.name for path in services_root.iterdir() if path.is_dir()),
        key=lambda item: len(item.replace("_", "")),
        reverse=True,
    )
    terraform: dict[str, list[str]] = {}
    for document in docs.glob("*.html.markdown"):
        stem = document.name.removesuffix(".html.markdown").removeprefix("oci_")
        compact = stem.replace("_", "")
        service = next(
            (name for name in provider_services if compact.startswith(name.replace("_", ""))),
            None,
        )
        if service is None:
            continue
        folded = ""
        consumed = 0
        for index, char in enumerate(stem):
            if char != "_":
                folded += char
            if folded == service.replace("_", ""):
                consumed = index + 1
                break
        suffix = stem[consumed:].lstrip("_")
        if suffix:
            terraform.setdefault(service, []).append(suffix)
    sdk_by_folded = {service.replace("_", ""): service for service in resources}
    canonical: dict[str, dict[str, tuple[str, ...]]] = {}
    for provider_service, names in terraform.items():
        service = sdk_by_folded.get(provider_service.replace("_", ""), provider_service)
        if service in canonical and provider_service != service:
            service = provider_service
        sdk_resources = resources.get(service, {})
        for name in names:
            methods = next(
                (value for sdk_name, value in sdk_resources.items() if name.endswith(sdk_name) or sdk_name.endswith(name)),
                ("terraform_resource",),
            )
            canonical.setdefault(service, {})[name] = methods
    resources.clear()
    resources.update(canonical)
