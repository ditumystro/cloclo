"""Fixtures partagées.

Les tests de chat utilisent le vrai SDK Anthropic, mais branché sur un faux
serveur Anthropic (httpx.MockTransport) : aucune clé, aucun appel réseau,
aucun coût. Le faux serveur renvoie de vrais flux SSE, comme l'API.
"""

import json

import anthropic
import httpx2 as httpx  # le SDK Anthropic 2.x utilise httpx2
import pytest
from fastapi.testclient import TestClient

import main


def sse(model: str = "claude-sonnet-5-5", texts=("Bonjour", " le monde"), stop_reason: str = "end_turn") -> bytes:
    """Construit un flux SSE de l'API Messages, tel qu'Anthropic l'envoie."""
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 0}}}),
    ]
    if texts:
        events.append(("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}))
        for t in texts:
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": t}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    events += [
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                           "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class FakeAnthropic:
    """Faux serveur Anthropic : enregistre les requêtes, renvoie la réponse choisie."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.body = sse()
        self.error_type = "api_error"
        self.raise_exc: Exception | None = None

    def reply(self, *, status=200, body=None, error_type="api_error", raise_exc=None, **sse_kwargs):
        self.status, self.error_type, self.raise_exc = status, error_type, raise_exc
        self.body = body if body is not None else sse(**sse_kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({"url": str(request.url), "headers": dict(request.headers),
                              "json": json.loads(request.content or b"{}")})
        if self.raise_exc:
            raise self.raise_exc
        if self.status != 200:
            return httpx.Response(self.status, json={"type": "error", "error": {
                "type": self.error_type, "message": "erreur simulée"}})
        return httpx.Response(200, content=self.body, headers={"content-type": "text/event-stream"})

    @property
    def last(self) -> dict:
        return self.requests[-1]["json"]


@pytest.fixture
def upstream(monkeypatch) -> FakeAnthropic:
    fake = FakeAnthropic()
    sdk = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(main, "_client", sdk)
    return fake


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Chaque test part d'un état propre : pas de mot de passe, compteurs vides."""
    monkeypatch.setattr(main, "APP_PASSWORD", "")
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 20)
    main._hits.clear()
    yield
    main._hits.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def read_events(response) -> list[dict]:
    """Décode une réponse NDJSON en liste d'événements."""
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def chat(client: TestClient, text="Salut", headers=None, **extra):
    payload = {"messages": [{"role": "user", "content": text}], **extra}
    return client.post("/api/chat", json=payload, headers=headers or {})
