from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from main import OciNavShell


class CompletionEngine:
    """Offline completion for navigable OCI namespace nodes only."""

    MODES: ClassVar[frozenset[str]] = frozenset({"off", "static", "cached", "catalog"})

    def __init__(self, shell: OciNavShell, mode: str = "catalog") -> None:
        self.shell = shell
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        if mode not in self.MODES:
            raise ValueError(
                f"completion mode must be one of: {', '.join(sorted(self.MODES))}"
            )
        self.mode = mode

    def command_names(self, text: str) -> list[str]:
        """Leave command-verb completion to ``cmd.Cmd``.

        A resource type is never a runnable shell command, so do not mix
        namespace entries such as ``core.instances`` into this result.
        """
        if self.mode == "off":
            return []
        return self.shell._command_name_completions(text)

    def resource_path(
        self, text: str, line: str, begidx: int, endidx: int
    ) -> list[str]:
        """Complete only children reachable in the OCI namespace.

        Sources are static registry entries and already-cached OCI reads.  It
        intentionally never calls OCI and does not try to complete arbitrary
        command values or free text.
        """
        if self.mode == "off":
            return []
        argument = self.shell._completion_argument(line, begidx, endidx, text)
        if (
            getattr(self.shell, "mount_collection", None) is not None
            and self.shell.mount_collection.name == "oci"
        ):
            return [
                entry
                for entry in self.shell.oci_schema.children(self.shell.schema_path)
                if entry.startswith(argument)
            ]
        if (
            getattr(self.shell, "mount_collection", None) is not None
            and self.shell.mount_collection.name == "topology"
            and "/" not in argument
        ):
            topology = getattr(self.shell, "topology_context", None)
            static = {
                None: ("vcns",),
                "vcn": ("subnets",),
                "subnet": ("consumers",),
            }.get(getattr(topology, "level", None), ())
            cached = self.shell._completion_cache.get(
                self.shell._current_path_suffix().rstrip("/"), ()
            )
            return sorted(
                entry for entry in {*static, *cached} if entry.startswith(argument)
            )
        topology = getattr(self.shell, "topology_context", None)
        if topology is not None and "/" not in argument:
            if topology.level == "service":
                static = tuple(
                    self.shell.browser._normalize_resource_token(spec.name)
                    for spec in self.shell._topology_service_specs(
                        topology.namespace or ""
                    )
                )
            else:
                static = {
                    "vcn": ("subnets",),
                    "subnet": ("consumers",),
                }.get(topology.level, ())
            cached = self.shell._completion_cache.get(
                self.shell._current_path_suffix().rstrip("/"), ()
            )
            return sorted(
                entry for entry in {*static, *cached} if entry.startswith(argument)
            )
        qualified = (
            (
                self.shell._complete_qualified_resource(argument)
                if self.mode == "static"
                else self.shell._complete_current_collection_entries(argument)
            )
            if self.shell._at_completion_root() and "/" not in argument
            else []
        )
        namespace_paths = (
            self.shell._complete_namespace_path(argument)
            if self.shell._at_completion_root() and "/" in argument
            else []
        )
        absolute = self.shell._complete_absolute_locator(argument)
        cached_locator = self.shell._complete_cached_locator_entries(argument)
        static = (
            set(qualified) | set(namespace_paths) | set(absolute) | set(cached_locator)
        )
        if self.mode == "static":
            return sorted(static)
        cached = (
            set(self.shell._context_resource_completions(argument))
            | set(self.shell._complete_current_compartment_children(argument))
            | set(self.shell._complete_qualified_collection_path(argument))
            | set(self.shell._complete_qualified_resource_path(argument))
            | set(self.shell._cached_resource_completions(text, argument))
            | set(self.shell._complete_known_namespace_paths(argument))
        )
        if self.mode == "catalog":
            cached |= set(self.shell._catalog_resource_completions(argument))
        return sorted(static | cached)

    def path(self, text: str, line: str, begidx: int, endidx: int) -> list[str]:
        """Compatibility alias for callers not yet migrated to ``resource_path``."""
        return self.resource_path(text, line, begidx, endidx)
