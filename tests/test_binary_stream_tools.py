"""Unit tests for _resolve_to_download_url using httpx.MockTransport."""

import asyncio
from collections.abc import Callable

import httpx

from wavix_mcp.server import _resolve_to_download_url

BASE_URL = "https://api.test.example"
PATH = "/v1/recordings/aa566501-c591-4a8b-b3b9-cc1295398b72"


def _resolve(handler: Callable[[httpx.Request], httpx.Response]) -> dict:
    async def run() -> dict:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
            return await _resolve_to_download_url(client, BASE_URL, PATH)

    return asyncio.run(run())


def test_resolve_returns_pre_signed_url_on_redirect():
    presigned = "https://s3.amazonaws.com/bucket/recording.mp3?X-Amz-Signature=abc"
    result = _resolve(lambda _request: httpx.Response(302, headers={"Location": presigned}))
    assert result["download_url"] == presigned
    assert result["status_code"] == 302
    assert result["note"].startswith("Pre-signed URL")


def test_resolve_returns_synthetic_url_on_2xx_body():
    result = _resolve(
        lambda _request: httpx.Response(
            200,
            headers={"Content-Type": "audio/mpeg", "Content-Length": "99999"},
            content=b"\xff\xfb\x90\x00",
        )
    )
    assert result["download_url"] == f"{BASE_URL}{PATH}"
    assert result["content_type"] == "audio/mpeg"
    assert result["status_code"] == 200
    assert result["note"].startswith("Authenticated URL")


def test_resolve_returns_error_envelope_on_4xx():
    result = _resolve(
        lambda _request: httpx.Response(
            404,
            headers={"Content-Type": "application/json"},
            json={"message": "Recording not found"},
        )
    )
    assert result["error"] == "HTTP 404"
    assert result["status_code"] == 404
    assert "Recording not found" in result["body"]


def test_resolve_handles_binary_error_body():
    result = _resolve(
        lambda _request: httpx.Response(
            500,
            headers={"Content-Type": "application/octet-stream"},
            content=b"\xff\xfe\xfd\xfc",
        )
    )
    assert result["error"] == "HTTP 500"
    assert result["body"] == "<binary body omitted>"
