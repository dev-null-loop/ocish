from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import oci

from models import CompartmentNode, ResourceRow, ResourceSpec

if TYPE_CHECKING:
    from inventory import OciCompartmentBrowser


class ActiveRegionCatalog:
    """Refresh active-region resource names off the completion path."""

    REFRESH_SECONDS = 60

    def __init__(self, browser: OciCompartmentBrowser) -> None:
        self.browser = browser
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._index_lock = threading.Lock()
        self._scanned_compartments: set[str] = set()
        self._requested_compartment: str | None = None
        self._region: str | None = None
        self._snapshot: dict[tuple[str, str], tuple[str, ...]] = {}
        self._id_snapshot: dict[tuple[str, str], tuple[str, ...]] = {}
        self._resource_snapshot: dict[tuple[str, str], tuple[ResourceRow, ...]] = {}
        self._children_snapshot: dict[str, tuple[CompartmentNode, ...]] = {}
        self._collections_snapshot: dict[str, tuple[str, ...]] = {}
        self._refreshed_at = 0.0

    def start(self) -> None:
        if self._thread is None:
            with self._lock:
                self._requested_compartment = self.browser.current.id
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._wake.set()

    def refresh_soon(self) -> None:
        self._wake.set()

    def request_compartment(self, compartment_id: str) -> None:
        """Refresh completion inventory for the active compartment in the worker."""
        with self._lock:
            self._requested_compartment = compartment_id
        self._wake.set()

    def names_for(self, compartment_id: str, spec: ResourceSpec) -> tuple[str, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return self._snapshot.get((compartment_id, spec.qualified_name), ())

    def ids_for(self, compartment_id: str, spec: ResourceSpec) -> tuple[str, ...]:
        """Return cached OCI IDs; completion never performs a lookup."""
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return getattr(self, "_id_snapshot", {}).get(
                (compartment_id, spec.qualified_name), ()
            )

    def is_ready(self) -> bool:
        with self._lock:
            return self._region == self.browser.region()

    def children_for(self, parent_id: str) -> tuple[CompartmentNode, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return self._children_snapshot.get(parent_id, ())

    def collections_for(self, compartment_id: str) -> tuple[str, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return self._collections_snapshot.get(compartment_id, ())

    def resources_for(
        self, compartment_id: str, spec: ResourceSpec
    ) -> tuple[ResourceRow, ...]:
        with self._lock:
            if self._region != self.browser.region():
                return ()
            return getattr(self, "_resource_snapshot", {}).get(
                (compartment_id, spec.qualified_name), ()
            )

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "region": self._region,
                "refreshed_at": self._refreshed_at,
                "collections": len(self._snapshot),
            }

    def _run(self) -> None:
        while True:
            region = self.browser.region()
            try:
                children = self.browser.build_active_region_compartment_catalog()
            except oci.exceptions.ServiceError:
                children = None
            if children is not None and region == self.browser.region():
                with self._lock:
                    self._region = region
                    self._children_snapshot = children
                    self._snapshot = {}
                    self._id_snapshot = {}
                    self._resource_snapshot = {}
                    self._collections_snapshot = {}
                    self._refreshed_at = time.monotonic()
                    self._scanned_compartments = set()
                    compartment_id = self._requested_compartment
                if compartment_id is not None:
                    self.prime_compartment(compartment_id)
            self._wake.wait(self.REFRESH_SECONDS)
            if self._wake.is_set():
                self._wake.clear()

    def _publish_resources(
        self,
        region: str,
        snapshot: dict[tuple[str, str], tuple[str, ...]],
        ids: dict[tuple[str, str], tuple[str, ...]],
        rows: dict[tuple[str, str], tuple[ResourceRow, ...]],
    ) -> None:
        collections: dict[str, set[str]] = {}
        for compartment_id, qualified_name in snapshot:
            collections.setdefault(compartment_id, set()).add(qualified_name)
        with self._lock:
            if self._region != region or region != self.browser.region():
                return
            self._snapshot = snapshot
            self._id_snapshot = ids
            self._resource_snapshot = rows
            self._collections_snapshot = {
                key: tuple(sorted(value)) for key, value in collections.items()
            }
            self._refreshed_at = time.monotonic()

    def prime_compartment(self, compartment_id: str) -> None:
        """Populate one compartment's completion inventory when explicitly scheduled."""
        with self._index_lock:
            with self._lock:
                if (
                    self._region != self.browser.region()
                    or compartment_id in self._scanned_compartments
                ):
                    return
            try:
                snapshot = self.browser.build_active_region_catalog(
                    compartment_id=compartment_id
                )
            except oci.exceptions.ServiceError:
                return
            ids = dict(getattr(self.browser, "active_region_catalog_ids", {}))
            rows = dict(getattr(self.browser, "active_region_catalog_rows", {}))
            with self._lock:
                merged = {**self._snapshot, **snapshot}
                merged_ids = {**self._id_snapshot, **ids}
                merged_rows = {**self._resource_snapshot, **rows}
                self._scanned_compartments.add(compartment_id)
            self._publish_resources(
                self.browser.region(), merged, merged_ids, merged_rows
            )
