"""End-to-end tests through the real HTTP entry point.

Everything here goes through the deployed surface — the same application object
uvicorn serves — rather than calling the graph directly. The distinction matters
for this template specifically: the entry node requires a verified caller, and
nothing but this adapter establishes that in a standalone deployment. A test
that calls the graph directly proves the pipeline works; only this one proves a
deployed request can reach it.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import httpx
import pytest

from tests.conftest import StubChatClient, base_config

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TOKEN = "integration-test-token"


class _Client:
    """Drives the application object over ASGI, exactly as a server would."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://agent.invalid") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(_run())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)


@pytest.fixture
def client(monkeypatch):
    """The shipped application with a stub chat client and bearer auth enabled."""
    from src.api import server
    from src.graph.graph import PriceLabelVerificationAgent

    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    agent = PriceLabelVerificationAgent(config=base_config(llm=StubChatClient()), project_root=_ROOT)
    agent.compile()
    monkeypatch.setattr(server, "agent", agent)
    return _Client(server.app)


def _post(client, labels, *, token: str | None = _TOKEN, context=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/invoke",
        json={
            "input": json.dumps(labels, ensure_ascii=False),
            "session_id": "it-1",
            "input_context": context if context is not None else {"regulatory_profile": "JP"},
        },
        headers=headers,
    )


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_a_verified_caller_gets_a_real_report(client, clean_label):
    """The public path does real domain work on caller data.

    Not a fixed baseline: the numbers in the response are computed from the
    numbers in the request.
    """
    response = _post(client, [clean_label])
    assert response.status_code == 200
    report = json.loads(response.json()["output"])
    assert report["summary"]["total"] == 1
    assert report["summary"]["pass"] == 1
    assert report["labels"][0]["label_id"] == "AEON-1"


def test_a_discrepancy_submitted_over_http_is_found(client):
    response = _post(client, [{"label_id": "HTTP-1", "pos_price": 980, "web_price": 1080}])
    report = json.loads(response.json()["output"])
    assert report["summary"]["fail"] == 1
    assert report["labels"][0]["checks"]["price_consistency"] == "FAIL"


def test_an_unauthenticated_caller_is_refused(client, clean_label):
    assert _post(client, [clean_label], token=None).status_code == 401


def test_a_wrong_token_is_refused_without_saying_why(client, clean_label):
    response = _post(client, [clean_label], token="wrong-token")
    assert response.status_code == 401
    assert response.json()["detail"] == "Token is invalid or expired."


def test_a_non_ascii_authorization_header_is_refused_not_crashed(client):
    """Headers decode as latin-1, and comparing them as text raises on non-ASCII.

    The header is sent as raw bytes because that is what arrives on the wire.
    Refusing has to stay a 401 — a 500 here would turn a bad credential into an
    availability signal.
    """
    response = client.post(
        "/invoke",
        json={"input": "[]", "session_id": "it-2", "input_context": {}},
        headers={"Authorization": "Bearer トークン".encode()},
    )
    assert response.status_code == 401


def test_the_structured_context_reaches_the_graph(client, clean_label):
    """An unimplemented profile sent over HTTP is refused by the graph.

    The profile only has that effect if the context channel actually arrives,
    so this doubles as proof the channel is wired end to end.
    """
    response = _post(client, [clean_label], context={"regulatory_profile": "EU"})
    assert response.status_code == 200
    assert response.json()["status"] in ("error", "ERROR")
    assert not response.json().get("output")


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "Bearer abcdefghijklmnopqrstuvwx",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdefghij",
        "postgresql://user:redacted@db.example/labels",
    ],
)
def test_a_credential_in_the_context_channel_is_refused_readably(client, clean_label, secret):
    """Without this the request fails at the backbone's first node.

    The first node copies the caller's context verbatim into its own result and
    the framework's output gate scans every value of every result, so the run
    dies before any of this template's code executes and the caller is told
    nothing about which field caused it. Refusing here does not change what
    succeeds; it changes an opaque failure into an actionable one.
    """
    response = _post(client, [clean_label], context={"regulatory_profile": "JP", "memo": secret})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "input_context.memo" in detail
    assert secret not in detail


def test_a_hostile_context_field_name_is_reported_by_position(client, clean_label):
    """Field names are caller data too, so an unsafe one is not echoed back."""
    response = _post(
        client,
        [clean_label],
        context={"a b c <script>": "sk-abcdefghijklmnopqrstuvwxyz0123456789"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "field #1" in detail
    assert "<script>" not in detail


def test_ordinary_domain_text_on_the_same_field_still_passes(client, clean_label):
    response = _post(
        client,
        [clean_label],
        context={"regulatory_profile": "JP", "memo": "週末セールの棚札確認"},
    )
    assert response.status_code == 200
    assert json.loads(response.json()["output"])["summary"]["total"] == 1


def test_the_refusal_set_matches_the_framework_block_set(client):
    """Per-field scanning composes exactly to scanning the whole mapping.

    That identity is what lets the refusal name a field without widening or
    narrowing what is blocked, so it is pinned rather than assumed.
    """
    from framework.security.credential_detector import detect_credentials_in_value

    from src.api.server import screen_input_context

    for context in (
        {},
        {"a": "ordinary text"},
        {"a": "sk-abcdefghijklmnopqrstuvwxyz0123456789"},
        {"a": "ok", "b": {"nested": "AKIAABCDEFGHIJKLMNOP"}},
        {"a": ["ok", "Bearer abcdefghijklmnopqrstuvwx"]},
        {"a": 1, "b": None, "c": 2.5},
    ):
        refused = screen_input_context(context) is not None
        assert refused == bool(detect_credentials_in_value(context)), context


def test_an_oversized_context_is_refused(client, clean_label):
    response = _post(client, [clean_label], context={"memo": "x" * 300_000})
    assert response.status_code == 413


def test_a_malformed_batch_is_refused_without_echoing_it(client):
    response = _post(client, "not-a-list")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("error", "ERROR")
    assert not body.get("output")
