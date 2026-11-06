"""Test de bout en bout avec la vraie API Anthropic (coûte une fraction de centime).

Ignoré par défaut. Pour le lancer :
    set -a; source .env; set +a; RUN_LIVE_TESTS=1 .venv/bin/pytest tests/test_live.py
"""

import os

import pytest
from fastapi.testclient import TestClient

import main
from conftest import read_events

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1" or os.environ.get("ANTHROPIC_API_KEY", "") in main.PLACEHOLDER_KEYS,
    reason="test live : définis RUN_LIVE_TESTS=1 et une vraie ANTHROPIC_API_KEY",
)


@pytest.mark.parametrize("mode", ["rapide", "standard"])
def test_live_chat(monkeypatch, mode):
    monkeypatch.setattr(main, "ANTHROPIC_API_KEY", os.environ["ANTHROPIC_API_KEY"])
    monkeypatch.setattr(main, "_client", None)
    r = TestClient(main.app).post("/api/chat", json={
        "messages": [{"role": "user", "content": "Réponds uniquement par le mot OK."}], "mode": mode})
    events = read_events(r)
    assert events[-1]["type"] == "done", events
    assert "OK" in "".join(e.get("text", "") for e in events).upper()
