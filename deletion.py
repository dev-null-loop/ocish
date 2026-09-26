from __future__ import annotations

import inspect
import re
import shlex
import time
import urllib.parse
from typing import TYPE_CHECKING

import oci

from config import DIRECT_GETTERS, RESOURCE_CONTEXT_CHILDREN
from models import DeleteSpec, ResolvedRmTarget, ResourceContext

if TYPE_CHECKING:
    from main import OciNavShell


class DeletionManager:
    def __init__(self, shell: OciNavShell) -> None:
        self.shell = shell

    def __getattr__(self, name: str) -> object:
        return getattr(self.shell, name)

    def _parse_rm_args(self, arg: str) -> tuple[bool, bool, str]:
        try:
            argv = shlex.split(arg)
        except ValueError as exc:
            raise ValueError(f"parse error: {exc}") from exc
        apply = False
        recursive = False
        target: str | None = None
        for token in argv:
            if token in {"-f", "--apply"}:
                apply = True
                continue
            if token in {"-r", "--recursive"}:
                recursive = True
                continue
            if token.startswith("-"):
                raise ValueError(f"unsupported rm option: {token}")
            if target is not None:
                raise ValueError("usage: rm [-r] [-f|--apply] <resource-path>")
            target = token
        if target is None:
            raise ValueError("usage: rm [-r] [-f|--apply] <resource-path>")
        return apply, recursive, target

    def _resolve_rm_target(self, target: str) -> ResolvedRmTarget:
        snapshot = self._snapshot_locator()
        try:
            self._change_locator(target)
            if self.resource_context is None:
                raise ValueError("rm requires a resource path")
            if self.collection_context is None:
                resolved_path = self._compose_suffix(self.browser.get_path())
            else:
                resolved_path = self._current_path_suffix()
            return ResolvedRmTarget(
                path=resolved_path,
                resource_context=self.resource_context,
                compartment_current=self.browser.current,
                compartment_parents=tuple(self.browser.parents),
            )
        finally:
            self._restore_locator(snapshot)

    def _delete_spec_for(self, resource_context: ResourceContext) -> DeleteSpec:
        spec = self.DELETE_SPECS.get(resource_context.spec.qualified_name)
        if spec is not None:
            return spec
        resource_spec = resource_context.spec
        if not resource_spec.client_attr:
            raise ValueError(
                f"rm is not supported for {resource_spec.qualified_name}: no OCI client"
            )
        client = getattr(self.browser, resource_spec.client_attr, None)
        singular = resource_spec.name
        if singular.endswith("ies"):
            singular = singular[:-3] + "y"
        else:
            singular = singular.removesuffix("s")
        method_name = f"delete_{singular}"
        operation = getattr(client, method_name, None)
        if operation is None:
            raise ValueError(
                f"rm is not supported for {resource_spec.qualified_name}: "
                f"OCI client has no {method_name}"
            )
        required = [
            parameter.name
            for parameter in inspect.signature(operation).parameters.values()
            if parameter.name != "self"
            and parameter.default is inspect.Parameter.empty
            and parameter.kind
            in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        ]
        if len(required) != 1 or not required[0].endswith("_id"):
            raise ValueError(
                f"rm is not supported for {resource_spec.qualified_name}: "
                "delete API requires additional identifiers"
            )
        return DeleteSpec(
            resource_type=resource_spec.qualified_name,
            client_attr=resource_spec.client_attr,
            method_name=method_name,
            arg_name=required[0],
        )

    def _resource_compartment_id(self, resource_context: ResourceContext) -> str:
        payload = resource_context.row.payload or {}
        value = payload.get("compartment_id")
        if isinstance(value, str) and value.startswith("ocid1.compartment."):
            return value
        raise ValueError(
            f"unable to determine compartment_id for {resource_context.spec.qualified_name}"
        )

    @staticmethod
    def _display_name_from_payload(payload: dict[str, object]) -> str:
        for key in ("display_name", "name", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return urllib.parse.unquote(value)
        return "<unnamed>"

    def _payload_contains_any(self, value: object, targets: set[str]) -> bool:
        if isinstance(value, dict):
            return any(
                self._payload_contains_any(item, targets) for item in value.values()
            )
        if isinstance(value, list):
            return any(self._payload_contains_any(item, targets) for item in value)
        if isinstance(value, str):
            return value in targets
        return False

    def _route_cleanup_target_ids(
        self, collections: list[dict[str, object]]
    ) -> set[str]:
        target_collections = {
            "local-peering-gateways",
            "internet-gateways",
            "nat-gateways",
            "service-gateways",
        }
        target_ids: set[str] = set()
        for collection in collections:
            if collection["name"] not in target_collections:
                continue
            for step in collection["rows"]:
                target_ids.add(step["resource_context"].row.id)
        return target_ids

    def _build_route_table_cleanup_steps(
        self,
        resolved_path: str,
        vcn_context: ResourceContext,
    ) -> list[dict[str, object]]:
        route_table_rows = self._list_vcn_child_rows(vcn_context, "route-tables")
        target_ids = {
            row.id
            for collection_name in (
                "local-peering-gateways",
                "internet-gateways",
                "nat-gateways",
                "service-gateways",
            )
            for row in self._list_vcn_child_rows(vcn_context, collection_name)
        }
        steps: list[dict[str, object]] = []
        for row in route_table_rows:
            route_table = self.browser.virtual_network.get_route_table(row.id).data
            route_rules = list(getattr(route_table, "route_rules", None) or [])
            remaining_rules = [
                rule
                for rule in route_rules
                if getattr(rule, "network_entity_id", None) not in target_ids
            ]
            removed = len(route_rules) - len(remaining_rules)
            if removed <= 0:
                continue
            steps.append(
                {
                    "path": self._row_path(resolved_path, "route-tables", row),
                    "resource_context": ResourceContext(
                        spec=self.browser.resolve_resource_spec("route-tables"),
                        row=row,
                    ),
                    "removed_count": removed,
                    "route_rules": [oci.util.to_dict(rule) for rule in remaining_rules],
                }
            )
        return steps

    def _apply_route_table_cleanup_step(self, step: dict[str, object]) -> None:
        route_table_context = step["resource_context"]
        details = oci.core.models.UpdateRouteTableDetails(
            route_rules=step["route_rules"]
        )
        self.browser.virtual_network.update_route_table(
            route_table_context.row.id, details
        )
        print(f"update {step['path']} (-{step['removed_count']} rules)")

    def _build_vcn_rm_plan(self, target: ResolvedRmTarget) -> dict[str, object]:
        resolved_path = target.path
        vcn_context = target.resource_context
        delete_spec = self._delete_spec_for(vcn_context)
        if not delete_spec.recursive_supported:
            raise ValueError(
                f"rm -r is not supported for {vcn_context.spec.qualified_name}"
            )

        def build() -> dict[str, object]:
            payload = vcn_context.row.payload or {}
            default_route_table_id = payload.get("default_route_table_id")
            default_security_list_id = payload.get("default_security_list_id")
            default_dhcp_options_id = payload.get("default_dhcp_options_id")
            collections: list[dict[str, object]] = []
            subnet_rows = self._list_vcn_child_rows(vcn_context, "subnets")
            blockers = self._discover_vcn_blockers(vcn_context, subnet_rows)
            for collection_name in self.VCN_RECURSIVE_COLLECTION_ORDER:
                if collection_name == "route-table-cleanup":
                    collections.append(
                        {
                            "name": collection_name,
                            "rows": self._build_route_table_cleanup_steps(
                                resolved_path, vcn_context
                            ),
                        }
                    )
                    continue
                rows = self._list_vcn_child_rows(vcn_context, collection_name)
                if collection_name == "route-tables":
                    rows = [row for row in rows if row.id != default_route_table_id]
                elif collection_name == "security-lists":
                    rows = [row for row in rows if row.id != default_security_list_id]
                elif collection_name == "dhcp-options":
                    rows = [row for row in rows if row.id != default_dhcp_options_id]
                spec = self.browser.resolve_resource_spec(
                    RESOURCE_CONTEXT_CHILDREN["core.vcns"][collection_name][0]
                )
                step_rows = []
                for row in rows:
                    child_context = ResourceContext(spec=spec, row=row)
                    step_rows.append(
                        {
                            "path": self._row_path(resolved_path, collection_name, row),
                            "resource_context": child_context,
                            "delete_spec": self._delete_spec_for(child_context),
                        }
                    )
                collections.append({"name": collection_name, "rows": step_rows})
            return {
                "path": resolved_path,
                "resource_context": vcn_context,
                "blockers": blockers,
                "collections": collections,
                "direct_delete_spec": delete_spec,
            }

        return self._with_browser_compartment_scope(
            target.compartment_current,
            target.compartment_parents,
            build,
        )

    def _print_vcn_rm_plan(self, plan: dict[str, object]) -> None:
        print(f"would rm -r {plan['path']}")
        blockers = plan["blockers"]
        if blockers:
            print("blockers:")
            for blocker in blockers[:20]:
                print(f"  {blocker['kind']}: {blocker['name']} ({blocker['id']})")
            if len(blockers) > 20:
                print(f"  ... and {len(blockers) - 20} more")
        for collection in plan["collections"]:
            action = (
                "update" if collection["name"] == "route-table-cleanup" else "delete"
            )
            print(f"  {action} {collection['name']}: {len(collection['rows'])}")
        print("  delete vcn: 1")

    def _wait_until_deleted(self, resource_context: ResourceContext) -> None:
        match = re.match(r"^ocid1\.([^.]+)\.", resource_context.row.id)
        if not match:
            return
        getter_ref = DIRECT_GETTERS.get(match.group(1))
        if getter_ref is None:
            return
        client_attr, method_name = getter_ref
        client = getattr(self.browser, client_attr)
        getter = getattr(client, method_name)
        deadline = time.time() + self.DELETE_WAIT_SECONDS
        while time.time() < deadline:
            try:
                response = getter(resource_context.row.id)
            except oci.exceptions.ServiceError as exc:
                if exc.status == 404:
                    return
                raise
            data = getattr(response, "data", None)
            state = str(getattr(data, "lifecycle_state", "") or "")
            if not state or state.upper() not in {
                "TERMINATING",
                "DETACHING",
                "DELETING",
            }:
                time.sleep(self.DELETE_WAIT_INTERVAL)
                continue
            time.sleep(self.DELETE_WAIT_INTERVAL)

    def _apply_rm_step(
        self, path: str, resource_context: ResourceContext, delete_spec: DeleteSpec
    ) -> None:
        self._apply_rm(path, resource_context, delete_spec)
        self._wait_until_deleted(resource_context)

    def _apply_vcn_rm_plan(self, plan: dict[str, object]) -> None:
        blockers = plan["blockers"]
        if blockers:
            print("delete blocked:")
            print(f"path: {plan['path']}")
            print("blockers must be removed first:")
            for blocker in blockers[:20]:
                print(f"  {blocker['kind']}: {blocker['name']} ({blocker['id']})")
            if len(blockers) > 20:
                print(f"  ... and {len(blockers) - 20} more")
            return
        for collection in plan["collections"]:
            for step in collection["rows"]:
                try:
                    if collection["name"] == "route-table-cleanup":
                        self._apply_route_table_cleanup_step(step)
                    else:
                        self._apply_rm_step(
                            step["path"], step["resource_context"], step["delete_spec"]
                        )
                except Exception as exc:
                    if collection["name"] == "route-table-cleanup":
                        print(f"update failed {step['path']}: {exc}")
                    else:
                        self._print_rm_error(
                            step["path"],
                            exc,
                            step["resource_context"],
                            step["delete_spec"],
                        )
                    return
        vcn_context = plan["resource_context"]
        delete_spec = plan["direct_delete_spec"]
        try:
            self._apply_rm_step(plan["path"], vcn_context, delete_spec)
        except Exception as exc:
            self._print_rm_error(plan["path"], exc, vcn_context, delete_spec)

    def _generic_recursive_children(
        self, parent: ResourceContext
    ) -> list[dict[str, object]]:
        children: list[dict[str, object]] = []
        for name, (
            resource_type,
            api_kwargs,
            row_filter_key,
        ) in RESOURCE_CONTEXT_CHILDREN.get(parent.spec.qualified_name, {}).items():
            qualified = (
                resource_type
                if "." in resource_type
                else f"{parent.spec.namespace}.{resource_type}"
            )
            kwargs: dict[str, str] = {}
            for parameter, source in api_kwargs:
                if source == "id":
                    kwargs[parameter] = parent.row.id
                elif source == "name":
                    kwargs[parameter] = parent.row.name
                else:
                    value = (parent.row.details or {}).get(source) or (
                        parent.row.payload or {}
                    ).get(source)
                    if isinstance(value, str) and value:
                        kwargs[parameter] = value
            rows = self.browser.list_resources(qualified, extra_kwargs=kwargs)
            if row_filter_key:
                rows = [
                    row
                    for row in rows
                    if (row.details or {}).get(row_filter_key) == parent.row.id
                ]
            steps: list[dict[str, object]] = []
            for row in rows:
                context = ResourceContext(
                    spec=self.browser.resolve_resource_spec(qualified), row=row
                )
                try:
                    delete_spec = self._delete_spec_for(context)
                except ValueError as exc:
                    steps.append({"path": row.name, "error": str(exc)})
                else:
                    steps.append(
                        {
                            "path": row.name,
                            "resource_context": context,
                            "delete_spec": delete_spec,
                        }
                    )
            children.append({"name": name, "steps": steps})
        return children

    def _build_generic_recursive_plan(
        self, target: ResolvedRmTarget
    ) -> dict[str, object]:
        parent_spec = self._delete_spec_for(target.resource_context)

        def build() -> dict[str, object]:
            return {
                "path": target.path,
                "resource_context": target.resource_context,
                "delete_spec": parent_spec,
                "children": self._generic_recursive_children(target.resource_context),
            }

        return self._with_browser_compartment_scope(
            target.compartment_current, target.compartment_parents, build
        )

    def _print_generic_recursive_plan(self, plan: dict[str, object]) -> None:
        print(f"would rm -r {plan['path']}")
        for child in plan["children"]:
            unsupported = [step for step in child["steps"] if "error" in step]
            print(f"  delete {child['name']}: {len(child['steps'])}")
            for step in unsupported:
                print(f"    blocked {step['path']}: {step['error']}")
        print("  delete resource: 1")

    def _apply_generic_recursive_plan(self, plan: dict[str, object]) -> None:
        for child in plan["children"]:
            for step in child["steps"]:
                if "error" in step:
                    print(f"delete blocked: {step['path']}: {step['error']}")
                    return
                try:
                    self._apply_rm_step(
                        step["path"], step["resource_context"], step["delete_spec"]
                    )
                except Exception as exc:
                    self._print_rm_error(
                        step["path"], exc, step["resource_context"], step["delete_spec"]
                    )
                    return
        if not self._preflight_delete(plan["path"], plan["resource_context"]):
            return
        self._apply_rm_step(plan["path"], plan["resource_context"], plan["delete_spec"])

    def _print_rm_preview(
        self,
        resolved_path: str,
        resource_context: ResourceContext,
        delete_spec: DeleteSpec,
    ) -> None:
        print(f"would rm {resolved_path}")
        if delete_spec.preview_note:
            print(f"note: {delete_spec.preview_note}")

    def _network_firewall_delete_blockers(
        self, resource_context: ResourceContext
    ) -> list[str]:
        if resource_context.spec.qualified_name != "network_firewall.network_firewalls":
            return []
        firewall = self.browser.network_firewall.get_network_firewall(
            resource_context.row.id
        ).data
        state = str(getattr(firewall, "lifecycle_state", "") or "")
        if state in {"DELETING", "DELETED"}:
            return [f"firewall lifecycle state is {state}"]
        compartment_id = self._resource_compartment_id(resource_context)
        response = self.browser.network_firewall.list_work_requests(
            compartment_id,
            resource_id=resource_context.row.id,
            limit=100,
        ).data
        active = {
            "ACCEPTED",
            "IN_PROGRESS",
            "WAITING",
            "CANCELING",
        }
        return [
            f"active work request {getattr(item, 'id', '<unknown>')} ({getattr(item, 'status', '-')})"
            for item in getattr(response, "items", []) or []
            if str(getattr(item, "status", "")).upper() in active
        ]

    def _preflight_delete(
        self, resolved_path: str, resource_context: ResourceContext
    ) -> bool:
        try:
            blockers = self._network_firewall_delete_blockers(resource_context)
        except Exception as exc:
            print(f"rm preflight failed {resolved_path}: {exc}")
            return False
        if blockers:
            print(f"rm blocked {resolved_path}:")
            for blocker in blockers:
                print(f"  {blocker}")
            return False
        if resource_context.spec.qualified_name == "network_firewall.network_firewalls":
            print("preflight: firewall is deletable; no active firewall work requests")
        else:
            has_getter, readable = self._generic_delete_preflight(resource_context)
            if has_getter and not readable:
                return False
            if has_getter:
                print(
                    "preflight: resource is readable and has an unambiguous delete API"
                )
            else:
                print(
                    "preflight: delete API is unambiguous; no direct read capability is declared"
                )
        return True

    def _generic_delete_preflight(
        self, resource_context: ResourceContext
    ) -> tuple[bool, bool]:
        match = re.match(r"^ocid1\.([^.]+)\.", resource_context.row.id)
        if match is None:
            return False, False
        getter = DIRECT_GETTERS.get(match.group(1))
        if getter is None:
            return False, False
        client_attr, method_name = getter
        try:
            getattr(getattr(self.browser, client_attr), method_name)(
                resource_context.row.id
            )
        except Exception as exc:
            print(f"rm preflight failed: direct read failed ({exc})")
            return True, False
        return True, True

    def _apply_rm(
        self,
        resolved_path: str,
        resource_context: ResourceContext,
        delete_spec: DeleteSpec,
    ) -> None:
        client = getattr(self.browser, delete_spec.client_attr)
        operation = getattr(client, delete_spec.method_name)
        operation(**{delete_spec.arg_name: resource_context.row.id})
        print(f"rm {resolved_path}")

    def _print_rm_error(
        self,
        path: str,
        exc: Exception,
        resource_context: ResourceContext,
        delete_spec: DeleteSpec,
    ) -> None:
        if isinstance(exc, oci.exceptions.ServiceError):
            print(f"rm failed {path}: {exc.status} {exc.code}: {exc.message}")
            if delete_spec.preview_note:
                print(f"note: {delete_spec.preview_note}")
            if delete_spec.recursive_supported and exc.code == "IncorrectState":
                print(
                    "hint: remove or detach dependent VCN resources first; direct rm does not recurse"
                )
            return
        print(f"rm failed: {exc}")

    def run(self, arg: str) -> None:
        """Preview or apply deletion for a supported resource path: rm [-r] [-f|--apply] <resource-path>."""
        try:
            apply, recursive, target = self._parse_rm_args(arg)
            resolved = self._resolve_rm_target(target)
        except ValueError as exc:
            print(exc)
            return
        if recursive:
            try:
                if resolved.resource_context.spec.qualified_name == "core.vcns":
                    plan = self._build_vcn_rm_plan(resolved)
                    printer, apply_plan = (
                        self._print_vcn_rm_plan,
                        self._apply_vcn_rm_plan,
                    )
                else:
                    plan = self._build_generic_recursive_plan(resolved)
                    printer, apply_plan = (
                        self._print_generic_recursive_plan,
                        self._apply_generic_recursive_plan,
                    )
            except ValueError as exc:
                print(exc)
                return
            if apply:
                apply_plan(plan)
            else:
                printer(plan)
            return
        resolved_path = resolved.path
        resource_context = resolved.resource_context
        try:
            delete_spec = self._delete_spec_for(resource_context)
        except ValueError as exc:
            print(exc)
            return
        try:
            if not self._preflight_delete(resolved_path, resource_context):
                return
            if apply:
                self._apply_rm(resolved_path, resource_context, delete_spec)
            else:
                self._print_rm_preview(resolved_path, resource_context, delete_spec)
        except Exception as exc:
            self._print_rm_error(resolved_path, exc, resource_context, delete_spec)
