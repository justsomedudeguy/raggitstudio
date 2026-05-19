import pytest

from customchat.lemonade_client import LemonadeClient, VisionProbeResult


class RejectingImageTransport:
    async def post_json(self, path, payload, stream=False):
        return {
            "error": {
                "message": "image input is not supported - hint: if this is unexpected, you may need to provide the mmproj"
            }
        }


class NestedRejectingImageTransport:
    async def post_json(self, path, payload, stream=False):
        return {
            "error": {
                "details": {
                    "response": {
                        "error": {
                            "message": "image input is not supported - hint: if this is unexpected, you may need to provide the mmproj"
                        }
                    }
                },
                "message": "llama-server request failed",
                "type": "backend_error",
            }
        }


@pytest.mark.asyncio
async def test_vision_probe_reports_mmproj_missing():
    client = LemonadeClient(base_url="http://example.test/v1", http=RejectingImageTransport())

    result = await client.probe_vision("model-id")

    assert isinstance(result, VisionProbeResult)
    assert result.ready is False
    assert result.reason == "mmproj_missing"


@pytest.mark.asyncio
async def test_vision_probe_prefers_nested_mmproj_error_detail():
    client = LemonadeClient(base_url="http://example.test/v1", http=NestedRejectingImageTransport())

    result = await client.probe_vision("model-id")

    assert result.ready is False
    assert result.reason == "mmproj_missing"
