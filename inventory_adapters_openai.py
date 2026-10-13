from __future__ import annotations

import os

from config import NAMESPACE_DISCOVERY
from models import ResourceRow, ResourceSpec


class OpenAIInventoryMixin:
    """Generative AI project and collection inventory operations."""

    def _list_openai_project_data(
        self, kwargs: dict[str, object], spec: ResourceSpec
    ) -> list[ResourceRow]:
        config = {
            **dict(NAMESPACE_DISCOVERY[spec.namespace].get("openai_data", {})),
            **dict(spec.adapter_config),
        }
        project_key = config["project_param"]
        project_id = kwargs.get(project_key)
        if not isinstance(project_id, str) or not project_id:
            raise ValueError(f"{project_key} is required")
        request = {"limit": 100}
        parent_key = config.get("parent_param")
        if parent_key:
            parent_id = kwargs.get(parent_key)
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError(f"{parent_key} is required")
            request[parent_key] = parent_id
        payloads = self._list_openai_collection(
            self._openai_project_client(project_id, config),
            config["collection"],
            request,
            int(config["max_pages"]),
        )
        return sorted(
            [
                self._row_from_openai_data_item(payload, project_id)
                for payload in payloads
            ],
            key=lambda row: row.name.lower(),
        )

    def _openai_project_client(
        self, project_id: str, config: dict[str, str]
    ) -> object:
        from openai import OpenAI

        base_url = config["base_url"].format(region=self.region())
        api_key = next(
            (os.getenv(name) for name in config["api_key_env"].split(",") if os.getenv(name)),
            None,
        )
        if api_key:
            return OpenAI(base_url=base_url, api_key=api_key, project=project_id)
        try:
            import httpx
            from oci_genai_auth import OciSessionAuth, OciUserPrincipalAuth
        except ImportError as exc:
            raise RuntimeError(config["auth_error"]) from exc
        try:
            auth = OciSessionAuth(profile_name=self.profile_name or "DEFAULT")
        except KeyError:
            auth = OciUserPrincipalAuth(profile_name=self.profile_name or "DEFAULT")
        return OpenAI(
            base_url=base_url,
            api_key="not-used",
            project=project_id,
            http_client=httpx.Client(auth=auth),
        )

    def _list_openai_collection(
        self,
        client: object,
        collection: str,
        parameters: dict[str, object],
        max_pages: int,
    ) -> list[dict[str, object]]:
        """Read any dotted collection from an OpenAI-compatible client."""
        target: object = client
        for part in collection.split("."):
            target = getattr(target, part)
        try:
            page = target.list(**parameters)
        except Exception as exc:
            if exc.__class__.__name__ == "NotFoundError":
                return []
            raise
        items: list[dict[str, object]] = []
        for _ in range(max_pages):
            items.extend(self._openai_item_dict(item) for item in page.data)
            if not page.has_next_page():
                break
            page = page.get_next_page()
        return items

    @staticmethod
    def _openai_item_dict(item: object) -> dict[str, object]:
        if hasattr(item, "model_dump"):
            return dict(item.model_dump())
        if isinstance(item, dict):
            return dict(item)
        return dict(vars(item))

    def _row_from_openai_data_item(
        self, payload: dict[str, object], project_id: str
    ) -> ResourceRow:
        name = str(
            payload.get("filename")
            or payload.get("name")
            or payload.get("id")
            or "<unnamed>"
        )
        return ResourceRow(
            name=name,
            state=str(payload.get("status") or "-"),
            id=str(payload.get("id") or name),
            extra=str(payload.get("purpose") or ""),
            details={**payload, "generative_ai_project_id": project_id},
            payload={**payload, "generative_ai_project_id": project_id},
        )
