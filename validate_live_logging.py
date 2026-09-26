"""Read-only live smoke coverage for the OCI Logging namespace."""

from __future__ import annotations

import argparse
import json

from inventory import OciCompartmentBrowser


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compartment-id", required=True)
    args = parser.parse_args()

    browser = OciCompartmentBrowser()
    browser.change_to_compartment(args.compartment_id)
    report: list[dict[str, object]] = []
    failures = 0
    rows_by_type = {}
    for spec in browser.resource_specs_for_namespace("logging"):
        try:
            rows = browser.list_resources(spec.qualified_name)
        except Exception as exc:  # live authorization is part of the smoke result
            failures += 1
            report.append(
                {"path": f"logging/{spec.name}", "status": "error", "error": str(exc)}
            )
            continue
        rows_by_type[spec.qualified_name] = rows
        report.append(
            {"path": f"logging/{spec.name}", "status": "ok", "count": len(rows)}
        )

    for group in rows_by_type.get("logging.log_groups", []):
        try:
            logs = browser.list_resources(
                "logging.logs", extra_kwargs={"log_group_id": group.id}
            )
            report.append(
                {
                    "path": f"logging/log-groups/{group.name}/logs",
                    "status": "ok",
                    "count": len(logs),
                }
            )
        except Exception as exc:
            failures += 1
            report.append(
                {
                    "path": f"logging/log-groups/{group.name}/logs",
                    "status": "error",
                    "error": str(exc),
                }
            )

    work_requests = rows_by_type.get("logging.work_requests", [])
    if not work_requests:
        report.append(
            {
                "path": "logging/work-requests/*",
                "status": "skipped",
                "reason": "no work requests",
            }
        )
    for work_request in work_requests:
        for child, resource_type in (
            ("work-request-errors", "logging.work_request_errors"),
            ("work-request-logs", "logging.work_request_logs"),
        ):
            try:
                rows = browser.list_resources(
                    resource_type, extra_kwargs={"work_request_id": work_request.id}
                )
                report.append(
                    {
                        "path": f"logging/work-requests/{work_request.name}/{child}",
                        "status": "ok",
                        "count": len(rows),
                    }
                )
            except Exception as exc:
                failures += 1
                report.append(
                    {
                        "path": f"logging/work-requests/{work_request.name}/{child}",
                        "status": "error",
                        "error": str(exc),
                    }
                )

    print(json.dumps({"summary": {"failures": failures}, "items": report}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
