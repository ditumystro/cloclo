"""Accès sur approbation : jetons signés, demande, e-mail, décision, blocage du chat."""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

import access
import main
from conftest import chat, read_events


@pytest.fixture
def approval(monkeypatch):
    """Active l'approbation et capture les e-mails au lieu de les envoyer."""
    sent = []
    monkeypatch.setattr(access, "OWNER_EMAIL", "moi@gmail.com")
    monkeypatch.setattr(access, "send_owner_email", lambda s, t, h: sent.append((s, t, h)) or True)
    return sent


def decide_link(sent) -> str:
    text = sent[-1][1]
    url = text.split("Approuver ou refuser : ")[1].strip()
    return parse_qs(urlparse(url).query)["t"][0]


def request_access(client, name="Ana", message=""):
    r = client.post("/api/access/request", json={"name": name, "message": message})
    assert r.status_code == 200, r.text
    return r.json()["request_token"]


# ------------------------------------------------------------ unitaires : jetons
def test_sign_and_verify_roundtrip():
    t = access.sign({"kind": "grant", "id": "abc"})
    assert access.verify(t, "grant") == {"kind": "grant", "id": "abc"}


def test_verify_rejects_tampering():
    t = access.sign({"kind": "grant", "id": "abc"})
    body, mac = t.split(".")
    forged = access._b64(b'{"kind":"grant","id":"zzz"}') + "." + mac
    assert access.verify(forged, "grant") is None
    assert access.verify(t[:-2] + "xx", "grant") is None


@pytest.mark.parametrize("bad", [None, "", "abc", "a.b.c", "!!!.???"])
def test_verify_rejects_garbage(bad):
    assert access.verify(bad, "grant") is None


def test_verify_checks_kind():
    """Un jeton de demande ne doit jamais servir de jeton d'accès."""
    assert access.verify(access.sign({"kind": "request", "id": "x"}), "grant") is None


def test_verify_checks_expiry():
    old = access.sign({"kind": "request", "id": "x", "iat": int(time.time()) - 10_000})
    assert access.verify(old, "request", max_age=100) is None
    assert access.verify(old, "request", max_age=100_000)


def test_tokens_from_another_secret_are_rejected(monkeypatch):
    t = access.sign({"kind": "grant", "id": "x"})
    monkeypatch.setattr(access, "ACCESS_SECRET", "autre-secret")
    assert access.verify(t, "grant") is None


def test_check_access_open_when_disabled():
    access.check_access(None)


def test_check_access_requires_grant(approval):
    with pytest.raises(HTTPException) as e:
        access.check_access(None)
    assert e.value.status_code == 403
    access.check_access(access.sign({"kind": "grant", "id": "ok"}))


def test_check_access_revoked(approval, monkeypatch):
    monkeypatch.setattr(access, "REVOKED", {"ok"})
    with pytest.raises(HTTPException):
        access.check_access(access.sign({"kind": "grant", "id": "ok"}))


def test_no_email_configured_returns_false(monkeypatch):
    monkeypatch.setattr(access, "RESEND_API_KEY", "")
    monkeypatch.setattr(access, "GMAIL_ADDRESS", "")
    assert access.send_owner_email("s", "t", "h") is False


def test_email_failure_is_caught(monkeypatch):
    monkeypatch.setattr(access, "RESEND_API_KEY", "re_x")
    monkeypatch.setattr(access, "_send_resend", lambda *a: (_ for _ in ()).throw(OSError("réseau")))
    assert access.send_owner_email("s", "t", "h") is False


def test_gmail_used_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(access, "RESEND_API_KEY", "")
    monkeypatch.setattr(access, "GMAIL_ADDRESS", "moi@gmail.com")
    monkeypatch.setattr(access, "GMAIL_APP_PASSWORD", "abcd")
    monkeypatch.setattr(access, "_send_gmail", lambda *a: calls.append(a))
    assert access.send_owner_email("s", "t", "h") is True and calls


def test_email_escapes_html():
    _, _, h = access._request_email("<script>x</script>", "<b>hi</b>", "1.2.3.4", "id1", "http://x/?t=1")
    assert "<script>" not in h and "&lt;script&gt;" in h and "<b>hi</b>" not in h


# ------------------------------------------------------------ intégration : parcours complet
def test_config_reports_approval(client, approval):
    assert client.get("/api/config").json()["approval_required"] is True


def test_chat_blocked_without_access(client, upstream, approval):
    r = chat(client)
    assert r.status_code == 403
    assert upstream.requests == []  # aucun appel payant


def test_full_approval_flow(client, upstream, approval):
    req_token = request_access(client, "Ana", "Je suis ta collègue")
    # 1. le propriétaire reçoit un e-mail
    subject, text, html_body = approval[-1]
    assert "Ana" in subject and "Je suis ta collègue" in text
    # 2. en attente
    status = client.get("/api/access/status", headers={"X-Access-Request": req_token}).json()
    assert status == {"status": "pending"}
    # 3. la page de décision ne décide rien toute seule (antivirus de messagerie)
    t = decide_link(approval)
    page = client.get("/api/access/decide", params={"t": t})
    assert page.status_code == 200 and "Approuver" in page.text
    assert client.get("/api/access/status", headers={"X-Access-Request": req_token}).json()["status"] == "pending"
    # 4. le propriétaire approuve
    done = client.post("/api/access/decide", data={"t": t, "action": "approve"})
    assert "Accès accordé" in done.text
    # 5. le visiteur reçoit son jeton et peut discuter
    status = client.get("/api/access/status", headers={"X-Access-Request": req_token}).json()
    assert status["status"] == "approved"
    r = chat(client, headers={"X-Access-Token": status["access_token"]})
    assert r.status_code == 200 and read_events(r)[-1]["type"] == "done"


def test_denied_flow(client, upstream, approval):
    req_token = request_access(client)
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "deny"})
    status = client.get("/api/access/status", headers={"X-Access-Request": req_token}).json()
    assert status == {"status": "denied"}


def test_grant_survives_restart(client, upstream, approval):
    """Les jetons sont signés : un accès accordé reste valable après un redémarrage."""
    req_token = request_access(client)
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "approve"})
    grant = client.get("/api/access/status", headers={"X-Access-Request": req_token}).json()["access_token"]
    access._requests.clear()  # simule un redémarrage
    assert chat(client, headers={"X-Access-Token": grant}).status_code == 200


def test_approval_works_even_if_request_was_forgotten(client, approval):
    """Si le serveur a redémarré entre la demande et l'approbation, le lien marche quand même."""
    req_token = request_access(client)
    access._requests.clear()
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "approve"})
    assert client.get("/api/access/status", headers={"X-Access-Request": req_token}).json()["status"] == "approved"


def test_unknown_after_restart(client, approval):
    req_token = request_access(client)
    access._requests.clear()
    assert client.get("/api/access/status", headers={"X-Access-Request": req_token}).json() == {"status": "unknown"}


def test_request_token_cannot_be_used_as_access(client, upstream, approval):
    req_token = request_access(client)
    assert chat(client, headers={"X-Access-Token": req_token}).status_code == 403


def test_forged_decide_link_rejected(client, approval):
    r = client.post("/api/access/decide", data={"t": "faux.jeton", "action": "approve"})
    assert "invalide" in r.text
    assert "invalide" in client.get("/api/access/decide", params={"t": "faux"}).text


def test_bad_action_rejected(client, approval):
    request_access(client)
    r = client.post("/api/access/decide", data={"t": decide_link(approval), "action": "hack"})
    assert "invalide" in r.text


def test_status_with_bad_token(client, approval):
    assert client.get("/api/access/status", headers={"X-Access-Request": "nimporte"}).status_code == 400


def test_request_rate_limit(client, approval):
    for _ in range(access.REQUESTS_PER_HOUR):
        request_access(client)
    r = client.post("/api/access/request", json={"name": "spam"})
    assert r.status_code == 429
    assert len(approval) == access.REQUESTS_PER_HOUR


def test_request_validation(client, approval):
    assert client.post("/api/access/request", json={"name": ""}).status_code == 422
    assert client.post("/api/access/request", json={"name": "x" * 81}).status_code == 422


def test_request_refused_when_approval_disabled(client):
    assert client.post("/api/access/request", json={"name": "Ana"}).status_code == 400


def test_link_uses_public_url(client, approval, monkeypatch):
    monkeypatch.setattr(access, "PUBLIC_URL", "https://cloclo.onrender.com")
    request_access(client)
    assert "https://cloclo.onrender.com/api/access/decide?t=" in approval[-1][1]


def test_unsent_email_logs_link(client, monkeypatch, caplog):
    monkeypatch.setattr(access, "OWNER_EMAIL", "moi@gmail.com")
    monkeypatch.setattr(access, "send_owner_email", lambda *a: False)
    r = client.post("/api/access/request", json={"name": "Ana"})
    assert r.json()["notified"] is False
    assert "/api/access/decide?t=" in caplog.text
