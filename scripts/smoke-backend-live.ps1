$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONPATH = "backend/app"

@'
import asyncio

from customchat.config import settings
from customchat.lemonade_client import LemonadeClient


async def main():
    client = LemonadeClient(settings.lemonade_base_url)
    model = await client.get_model(settings.chat_model_id)
    vision = await client.probe_vision(settings.chat_model_id)
    embedding = await client.embed(settings.embedding_model_id, ["alpha smoke test"])
    rerank = await client.rerank(
        settings.reranker_model_id,
        "alpha",
        ["alpha document", "unrelated document"],
    )
    print({
        "model": model.get("id"),
        "context": model.get("recipe_options", {}).get("ctx_size"),
        "vision": {"ready": vision.ready, "reason": vision.reason},
        "embedding_dimensions": len(embedding[0]),
        "rerank_results": rerank[:2],
    })


asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -

