from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from customchat.metadata import SourceMetadata, classifier_json_schema, normalize_metadata


@dataclass(slots=True)
class VisionProbeResult:
    ready: bool
    reason: str
    message: str


class HttpTransport:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def get_json(self, path: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{path}")
            response.raise_for_status()
            return response.json()

    async def post_json(self, path: str, payload: dict[str, Any], stream: bool = False) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}{path}", json=payload)
            if response.status_code >= 400:
                return {"error": response.json() if response.content else {"message": response.text}}
            return response.json()

    async def stream_sse(self, path: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{self.base_url}{path}", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ").strip()
                    if data == "[DONE]":
                        yield {"type": "done"}
                        continue
                    yield json.loads(data)


class LemonadeClient:
    def __init__(self, base_url: str, http: Any | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = http or HttpTransport(self.base_url)

    async def list_models(self) -> dict[str, Any]:
        return await self.http.get_json("/models")

    async def get_model(self, model_id: str) -> dict[str, Any]:
        return await self.http.get_json(f"/models/{model_id}")

    async def embed(self, model_id: str, texts: list[str]) -> list[list[float]]:
        payload = {"model": model_id, "input": texts}
        response = await self.http.post_json("/embeddings", payload)
        if "error" in response:
            raise RuntimeError(_error_message(response["error"]))
        return [item["embedding"] for item in response.get("data", [])]

    async def rerank(self, model_id: str, query: str, documents: list[str]) -> list[dict[str, Any]]:
        payload = {"model": model_id, "query": query, "documents": documents}
        response = await self.http.post_json("/reranking", payload)
        if "error" in response:
            raise RuntimeError(_error_message(response["error"]))
        return response.get("results", [])

    async def classify(self, model_id: str, title: str, text: str, source_type: str) -> SourceMetadata:
        prompt = (
            "Classify this source for a local RAG database. Return only JSON matching the schema. "
            f"Source type: {source_type}\nTitle: {title}\nText:\n{text[:6000]}"
        )
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 1024,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": classifier_json_schema()},
        }
        response = await self.http.post_json("/chat/completions", payload)
        if "error" in response:
            raise RuntimeError(_error_message(response["error"]))
        content = response["choices"][0]["message"].get("content", "{}")
        return normalize_metadata(json.loads(content))

    async def probe_vision(self, model_id: str) -> VisionProbeResult:
        tiny_png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdac\xfc\xff"
            b"\x1f\x00\x03\x03\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
        ).decode("ascii")
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Reply IMAGE_OK if image input works."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}},
                    ],
                }
            ],
            "max_tokens": 64,
            "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        response = await self.http.post_json("/chat/completions", payload)
        if "error" in response:
            message = _error_message(response["error"])
            reason = "mmproj_missing" if "mmproj" in message.lower() or "image input is not supported" in message.lower() else "error"
            return VisionProbeResult(ready=False, reason=reason, message=message)
        return VisionProbeResult(ready=True, reason="ready", message="Image input accepted.")

    async def chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        payload = dict(payload)
        payload["stream"] = True
        async for event in self.http.stream_sse("/chat/completions", payload):
            yield event


def _error_message(error: Any) -> str:
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        details = error.get("details")
        if isinstance(details, dict):
            response = details.get("response")
            if isinstance(response, dict):
                nested = response.get("error")
                if isinstance(nested, dict) and "message" in nested:
                    return str(nested["message"])
        if "message" in error:
            return str(error["message"])
    return str(error)
