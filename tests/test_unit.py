"""Tests unitaires : fonctions et schémas, sans HTTP."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main


# ------------------------------------------------------------ mot de passe
def test_password_not_required_when_unset():
    main.check_password(None)  # ne lève rien


def test_password_accepts_correct(monkeypatch):
    monkeypatch.setattr(main, "APP_PASSWORD", "secret")
    main.check_password("secret")


@pytest.mark.parametrize("given", [None, "", "Secret", "secret ", "autre"])
def test_password_rejects_wrong(monkeypatch, given):
    monkeypatch.setattr(main, "APP_PASSWORD", "secret")
    with pytest.raises(HTTPException) as e:
        main.check_password(given)
    assert e.value.status_code == 401


# ------------------------------------------------------------ limite de débit
def test_rate_limit_allows_up_to_limit(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 3)
    for _ in range(3):
        main.check_rate_limit("1.2.3.4")
    with pytest.raises(HTTPException) as e:
        main.check_rate_limit("1.2.3.4")
    assert e.value.status_code == 429


def test_rate_limit_is_per_ip(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 1)
    main.check_rate_limit("1.1.1.1")
    main.check_rate_limit("2.2.2.2")  # autre IP : pas bloquée


def test_rate_limit_window_expires(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MIN", 1)
    now = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    main.check_rate_limit("ip")
    now[0] += 61
    main.check_rate_limit("ip")  # la fenêtre d'une minute est passée


def test_client_ip_ignores_spoofed_forwarded_for():
    """X-Forwarded-For vient du client : s'y fier permet de contourner la limite.
    En production, uvicorn --proxy-headers met déjà la vraie IP dans request.client."""
    req = SimpleNamespace(headers={"x-forwarded-for": "6.6.6.6"}, client=SimpleNamespace(host="10.0.0.1"))
    assert main.client_ip(req) == "10.0.0.1"


def test_client_ip_without_client():
    req = SimpleNamespace(headers={}, client=None)
    assert main.client_ip(req) == "inconnu"


# ------------------------------------------------------------ clé API
@pytest.mark.parametrize("key", ["", "sk-ant-...", "  "])
def test_get_client_refuses_missing_or_placeholder_key(monkeypatch, key):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", key)
    monkeypatch.setattr(main, "_client", None)
    with pytest.raises(HTTPException) as e:
        main.get_client()
    assert e.value.status_code == 500
    assert "ANTHROPIC_API_KEY" in e.value.detail


def test_get_client_is_cached(monkeypatch):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", "sk-ant-real-looking")
    monkeypatch.setattr(main, "_client", None)
    assert main.get_client() is main.get_client()


# ------------------------------------------------------------ schémas
def test_chat_request_defaults():
    r = main.ChatRequest(messages=[{"role": "user", "content": "hi"}])
    assert r.mode == "standard" and r.system is None


@pytest.mark.parametrize("payload", [
    {"messages": []},                                                    # vide
    {"messages": [{"role": "system", "content": "x"}]},                  # rôle interdit
    {"messages": [{"role": "user", "content": ""}]},                     # contenu vide
    {"messages": [{"role": "user", "content": "x" * 50_001}]},           # trop long
    {"messages": [{"role": "user", "content": "x"}], "mode": "turbo"},   # mode inconnu
    {"messages": [{"role": "user", "content": "x"}], "system": "s" * 10_001},
    {"messages": [{"role": "user", "content": "x"}] * 101},              # trop de messages
])
def test_chat_request_rejects_invalid(payload):
    with pytest.raises(ValidationError):
        main.ChatRequest(**payload)


def test_every_mode_has_a_model():
    assert set(main.MODELS) == {"rapide", "standard", "approfondi"}
    assert all(m.startswith("claude-") for m in main.MODELS.values())


def test_max_tokens_leaves_room_for_thinking():
    """Sonnet 5.5 et Opus 5.5 réfléchissent par défaut, et cette réflexion compte
    dans max_tokens : avec 4096, les réponses sortent coupées ou vides."""
    assert main.DEFAULT_MAX_TOKENS >= 16_000
