"""Accès sur approbation : jetons signés, demande, e-mail, décision, blocage du chat."""

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

import access
import main
from conftest import chat, read_events


class Outbox(list):
    """E-mails capturés : (destinataire, sujet, texte, html)."""

    def to(self, addr):
        return [m for m in self if m[0] == addr]


@pytest.fixture
def approval(monkeypatch, tmp_path):
    """Active l'approbation, capture les e-mails et isole le fichier des approbations."""
    sent = Outbox()
    monkeypatch.setattr(access, "OWNER_EMAIL", "moi@gmail.com")
    monkeypatch.setattr(access, "ACCESS_STORE", str(tmp_path / "approved.json"))
    monkeypatch.setattr(access, "_approved", None)
    monkeypatch.setattr(access, "send_email", lambda to, s, t, h: sent.append((to, s, t, h)) or True)
    return sent


def decide_link(sent) -> str:
    text = sent.to("moi@gmail.com")[-1][2]
    url = text.split("Approuver ou refuser : ")[1].strip()
    return parse_qs(urlparse(url).query)["t"][0]


def access_link_token(sent, email) -> str:
    text = sent.to(email)[-1][2]
    url = text.split("Ton lien personnel (garde-le, ne le partage pas) : ")[1].split()[0]
    return parse_qs(urlparse(url).query)["acces"][0]


def request_access(client, name="Ana", message="", email="ana@exemple.fr"):
    r = client.post("/api/access/request", json={"name": name, "email": email, "message": message})
    assert r.status_code == 200, r.text
    return r.json().get("request_token")


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


def test_check_access_revoked_by_email(approval, monkeypatch):
    monkeypatch.setattr(access, "REVOKED", {"Ana@Exemple.fr"})
    with pytest.raises(HTTPException):
        access.check_access(access.grant_token("x", "Ana", "ana@exemple.fr"))


def test_no_email_configured_returns_false(monkeypatch):
    monkeypatch.setattr(access, "RESEND_API_KEY", "")
    monkeypatch.setattr(access, "GMAIL_ADDRESS", "")
    assert access.send_email("a@b.c", "s", "t", "h") is False


def test_email_failure_is_caught(monkeypatch):
    monkeypatch.setattr(access, "RESEND_API_KEY", "re_x")
    monkeypatch.setattr(access, "_send_resend", lambda *a: (_ for _ in ()).throw(OSError("réseau")))
    assert access.send_email("a@b.c", "s", "t", "h") is False


def test_gmail_used_when_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(access, "RESEND_API_KEY", "")
    monkeypatch.setattr(access, "GMAIL_ADDRESS", "moi@gmail.com")
    monkeypatch.setattr(access, "GMAIL_APP_PASSWORD", "abcd")
    monkeypatch.setattr(access, "_send_gmail", lambda *a: calls.append(a))
    assert access.send_email("ana@exemple.fr", "s", "t", "h") is True
    assert calls[0][0] == "ana@exemple.fr"


def test_email_escapes_html():
    _, _, h = access._request_email("<script>x</script>", "a@b.c", "<b>hi</b>", "1.2.3.4", "id1", "http://x/?t=1")
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
    to, subject, text, html_body = approval[-1]
    assert to == "moi@gmail.com"
    assert "Ana" in subject and "Je suis ta collègue" in text and "ana@exemple.fr" in text
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
    for i in range(access.REQUESTS_PER_HOUR):
        request_access(client)
    r = client.post("/api/access/request", json={"name": "spam", "email": "spam@exemple.fr"})
    assert r.status_code == 429
    assert len(approval) == 1  # même e-mail : une seule alerte


def test_request_validation(client, approval):
    ok = {"name": "Ana", "email": "ana@exemple.fr"}
    assert client.post("/api/access/request", json={**ok, "name": ""}).status_code == 422
    assert client.post("/api/access/request", json={**ok, "name": "x" * 81}).status_code == 422
    for bad in ["", "ana", "ana@", "@x.fr", "a b@c.fr", "ana@exemple"]:
        assert client.post("/api/access/request", json={**ok, "email": bad}).status_code == 422, bad


def test_request_refused_when_approval_disabled(client):
    assert client.post("/api/access/request", json={"name": "Ana", "email": "ana@exemple.fr"}).status_code == 400


def test_link_uses_public_url(client, approval, monkeypatch):
    monkeypatch.setattr(access, "PUBLIC_URL", "https://cloclo.onrender.com")
    request_access(client)
    assert "https://cloclo.onrender.com/api/access/decide?t=" in approval[-1][2]


def test_unsent_email_logs_link(client, monkeypatch, caplog, tmp_path):
    monkeypatch.setattr(access, "OWNER_EMAIL", "moi@gmail.com")
    monkeypatch.setattr(access, "ACCESS_STORE", str(tmp_path / "a.json"))
    monkeypatch.setattr(access, "_approved", None)
    monkeypatch.setattr(access, "send_email", lambda *a: False)
    r = client.post("/api/access/request", json={"name": "Ana", "email": "ana@exemple.fr"})
    assert r.json()["notified"] is False
    assert "/api/access/decide?t=" in caplog.text


# ------------------------------------------------------------ une seule demande, lien par e-mail
def approve(client, approval, email="ana@exemple.fr", name="Ana"):
    req = request_access(client, name, email=email)
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "approve"})
    return req


def test_approval_emails_personal_link_to_visitor(client, upstream, approval):
    approve(client, approval)
    mails = approval.to("ana@exemple.fr")
    assert len(mails) == 1 and "Ton accès à cloclo" in mails[0][1]
    token = access_link_token(approval, "ana@exemple.fr")
    # le lien marche sur un autre appareil, sans rien demander d'autre
    assert client.get("/api/access/check", headers={"X-Access-Token": token}).json()["name"] == "Ana"
    assert chat(client, headers={"X-Access-Token": token}).status_code == 200


def test_access_link_survives_restart(client, upstream, approval):
    approve(client, approval)
    token = access_link_token(approval, "ana@exemple.fr")
    access._requests.clear()
    access._approved = None
    assert chat(client, headers={"X-Access-Token": token}).status_code == 200


def test_already_approved_does_not_alert_owner_again(client, approval):
    approve(client, approval)
    owner_alerts = len(approval.to("moi@gmail.com"))
    r = client.post("/api/access/request", json={"name": "Ana", "email": "ANA@exemple.fr"})
    assert r.json() == {"status": "already_approved"}
    assert len(approval.to("moi@gmail.com")) == owner_alerts          # pas de nouvelle alerte
    assert len(approval.to("ANA@exemple.fr")) == 1                    # lien renvoyé à la personne


def test_already_approved_survives_restart(client, approval):
    """La liste des e-mails approuvés est gardée sur disque."""
    approve(client, approval)
    access._approved = None
    access._requests.clear()
    r = client.post("/api/access/request", json={"name": "Ana", "email": "ana@exemple.fr"})
    assert r.json()["status"] == "already_approved"


def test_pending_request_is_not_duplicated(client, approval):
    t1 = request_access(client, email="bob@exemple.fr", name="Bob")
    t2 = request_access(client, email="bob@exemple.fr", name="Bob")
    assert len(approval.to("moi@gmail.com")) == 1
    assert access.verify(t1, "request")["id"] == access.verify(t2, "request")["id"]


def test_denied_person_can_ask_again(client, approval):
    request_access(client, email="bob@exemple.fr", name="Bob")
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "deny"})
    request_access(client, email="bob@exemple.fr", name="Bob")
    assert len(approval.to("moi@gmail.com")) == 2


def test_check_rejects_bad_or_revoked_link(client, approval, monkeypatch):
    assert client.get("/api/access/check", headers={"X-Access-Token": "faux"}).status_code == 403
    token = access.grant_token("x", "Ana", "ana@exemple.fr")
    monkeypatch.setattr(access, "REVOKED", {"ana@exemple.fr"})
    assert client.get("/api/access/check", headers={"X-Access-Token": token}).status_code == 403


def test_decide_page_reports_failed_visitor_email(client, approval, monkeypatch):
    request_access(client)
    t = decide_link(approval)
    monkeypatch.setattr(access, "send_email", lambda *a: False)
    page = client.post("/api/access/decide", data={"t": t, "action": "approve"})
    assert "n’a pas pu partir" in page.text


def test_store_file_is_written(client, approval):
    approve(client, approval)
    import json, pathlib
    data = json.loads(pathlib.Path(access.ACCESS_STORE).read_text())
    assert data["ana@exemple.fr"]["name"] == "Ana"
