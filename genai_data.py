"""OCI OpenAI-compatible project data-plane access."""

from __future__ import annotations

import os
from typing import Any


class GenAiProjectDataClient:
    """Read listable artifacts from one OCI Generative AI project."""

    MAX_PAGES = 20

    def __init__(self, region: str, profile_name: str | None) -> None:
        self.region = region
        self.profile_name = profile_name

    def list(
        self, project_id: str, collection: str, parent_id: str | None = None
    ) -> list[dict[str, Any]]:
        client = self._client(project_id)
        target: object = client
        for part in collection.split("."):
            target = getattr(target, part)
        kwargs: dict[str, Any] = {"limit": 100}
        if collection == "containers.files":
            kwargs["container_id"] = parent_id
        elif collection == "vector_stores.files":
            kwargs["vector_store_id"] = parent_id
        try:
            page = target.list(**kwargs)
        except Exception as exc:
            if exc.__class__.__name__ == "NotFoundError":
                return []
            raise
        items: list[dict[str, Any]] = []
        for _ in range(self.MAX_PAGES):
            items.extend(self._item_dict(item) for item in page.data)
            if not page.has_next_page():
                break
            page = page.get_next_page()
        return items

    def _client(self, project_id: str) -> Any:
        from openai import OpenAI

        base_url = f"https://inference.generativeai.{self.region}.oci.oraclecloud.com/openai/v1"
        api_key = os.getenv("OCISH_OCI_GENAI_API_KEY") or os.getenv("OCI_GENAI_API_KEY")
        if api_key:
            return OpenAI(base_url=base_url, api_key=api_key, project=project_id)
        try:
            import httpx
            from oci_genai_auth import OciSessionAuth, OciUserPrincipalAuth
        except ImportError as exc:
            raise RuntimeError(
                "OCI Generative AI project data needs OCI IAM auth or an API key. "
                "Install oci-genai-auth, or set OCISH_OCI_GENAI_API_KEY."
            ) from exc
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

    @staticmethod
    def _item_dict(item: object) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return dict(item.model_dump())
        if isinstance(item, dict):
            return dict(item)
        return dict(vars(item))
