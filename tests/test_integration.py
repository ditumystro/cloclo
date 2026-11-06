"""Tests d'intégration : requêtes HTTP réelles vers l'app FastAPI, qui appelle le
vrai SDK Anthropic branché sur un faux serveur Anthropic (voir conftest.py)."""

import httpx2 as httpx  # le SDK Anthropic 2.x utilise httpx2
import pytest

import main
from conftest import chat, read_events, sse


# ------------------------------------------------------------ routes simples
def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_config_without_password(client):
    assert client.get("/api/config").json() == {"password_required": False,
                                                 "modes": ["rapide", "standard", "approfondi"]}


def test_config_with_password(client, monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "pw")
    assert client.get("/api/config").json()["password_required"] is True


def test_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<h1>cloclo</h1>" in r.text and "/app.js" in r.text
    assert client.get("/app.js").status_code == 200


def test_unknown_api_route_is_404(client):
    assert client.get("/api/nope").status_code == 404


# ------------------------------------------------------------ chat : cas normal
def test_chat_streams_text_then_done(client, upstream):
    r = chat(client)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    events = read_events(r)
    assert [e["type"] for e in events] == ["text", "text", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "text") == "Bonjour le monde"
    assert events[-1]["stop_reason"] == "end_turn"


def test_chat_sends_conversation_and_system(client, upstream):
    msgs = [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "R1"},
            {"role": "user", "content": "Q2"}]
    client.post("/api/chat", json={"messages": msgs, "system": "  Sois bref.  "})
    sent = upstream.last
    assert sent["messages"] == msgs
    assert sent["system"] == "Sois bref."
    assert sent["max_tokens"] == main.MAX_TOKENS
    assert sent["stream"] is True


def test_chat_blank_system_is_not_sent(client, upstream):
    chat(client, system="   ")
    assert "system" not in upstream.last


@pytest.mark.parametrize("mode", ["rapide", "standard", "approfondi"])
def test_chat_mode_selects_model(client, upstream, mode):
    chat(client, mode=mode)
    assert upstream.last["model"] == main.MODELS[mode]


def test_chat_utf8_is_not_escaped(client, upstream):
    upstream.reply(texts=["Réponse : « ça marche » ✓"])
    r = chat(client)
    assert "« ça marche » ✓" in r.text


def test_chat_reports_truncation(client, upstream):
    upstream.reply(stop_reason="max_tokens")
    assert read_events(chat(client))[-1] == {"type": "done", "stop_reason": "max_tokens",
                                             "model": "claude-sonnet-5-5"}


# ------------------------------------------------------------ chat : garde-fous
def test_chat_requires_password(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "pw")
    assert chat(client).status_code == 401
    assert chat(client, headers={"X-App-Password": "faux"}).status_code == 401
    assert chat(client, headers={"X-App-Password": "pw"}).status_code == 200
    assert len(upstream.requests) == 1  # les requêtes refusées n'ont rien coûté


def test_chat_rate_limit(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 2)
    assert chat(client).status_code == 200
    assert chat(client).status_code == 200
    assert chat(client).status_code == 429


def test_chat_rate_limit_not_bypassed_by_forwarded_for(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 1)
    assert chat(client, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert chat(client, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


@pytest.mark.parametrize("msgs", [
    [{"role": "assistant", "content": "x"}],
    [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
])
def test_chat_must_start_and_end_with_user(client, upstream, msgs):
    r = client.post("/api/chat", json={"messages": msgs})
    assert r.status_code == 422
    assert upstream.requests == []


def test_chat_invalid_json_is_422(client, upstream):
    r = client.post("/api/chat", content="pas du json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_chat_without_api_key_gives_clear_error(client, monkeypatch):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-...")  # valeur de .env.example
    monkeypatch.setattr(main, "_client", None)
    r = chat(client)
    assert r.status_code == 500
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


# ------------------------------------------------------------ chat : erreurs d'Anthropic
@pytest.mark.parametrize("status,error_type,needle", [
    (401, "authentication_error", "Clé API refusée"),
    (429, "rate_limit_error", "Limite de l’API"),
    (404, "not_found_error", "Modèle introuvable"),
    (400, "invalid_request_error", "Requête refusée"),
    (529, "overloaded_error", "(529)"),
    (500, "api_error", "(500)"),
])
def test_chat_maps_api_errors(client, upstream, status, error_type, needle):
    upstream.reply(status=status, error_type=error_type)
    events = read_events(chat(client))
    assert events[-1]["type"] == "error"
    assert needle in events[-1]["message"]


def test_chat_connection_error(client, upstream):
    upstream.reply(raise_exc=httpx.ConnectError("réseau coupé"))
    events = read_events(chat(client))
    assert events == [{"type": "error", "message": "Impossible de joindre l’API Anthropic depuis le serveur."}]


def test_chat_refusal_is_explained(client, upstream):
    """Claude peut refuser (stop_reason « refusal ») sans écrire de texte :
    l'interface ne doit pas rester vide."""
    upstream.reply(texts=(), stop_reason="refusal")
    events = read_events(chat(client))
    assert events[-1]["type"] == "error"
    assert "refus" in events[-1]["message"].lower()


def test_chat_empty_answer_is_explained(client, upstream):
    upstream.reply(texts=(), stop_reason="end_turn")
    events = read_events(chat(client))
    assert events[-1]["type"] == "error"


def test_chat_broken_stream_ends_with_error_line(client, upstream):
    """Un flux SSE corrompu ne doit pas couper la réponse sans explication."""
    upstream.reply(body=b"event: message_start\ndata: {pas du json}\n\n")
    events = read_events(chat(client))
    assert events and events[-1]["type"] == "error"


# ------------------------------------------------------------ repli en cas de refus
@pytest.mark.parametrize("mode", ["standard", "approfondi"])
def test_chat_enables_server_side_fallback(client, upstream, mode):
    chat(client, mode=mode)
    req = upstream.requests[-1]
    assert req["json"].get("fallbacks") == "default"
    assert "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")


def test_chat_no_fallback_for_haiku(client, upstream):
    """Claude Haiku 5.5 n'a pas de repli côté serveur : ne pas l'envoyer."""
    chat(client, mode="rapide")
    assert "fallbacks" not in upstream.last
