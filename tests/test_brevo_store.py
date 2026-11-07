"""Liste des approuvés gardée dans Brevo : survit aux déploiements Render."""

import io
import json
import urllib.error
from urllib.parse import unquote, urlparse

import pytest

import access
from test_access import approval, decide_link, request_access  # noqa: F401 (fixture)


class FakeBrevo:
    """Imite l'API Brevo v3 pour les contacts, listes et dossiers."""

    def __init__(self):
        self.lists, self.folders, self.contacts = [], [], {}
        self.calls, self.down = [], False

    def urlopen(self, req, timeout=None):
        url = urlparse(req.full_url)
        path, method = url.path.removeprefix("/v3"), req.get_method()
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, path))
        assert req.headers["Api-key"] == "xkeysib-test"
        if self.down:
            raise urllib.error.URLError("Brevo injoignable")

        def ok(obj=None, status=200):
            raw = json.dumps(obj).encode() if obj is not None else b""
            r = io.BytesIO(raw)
            r.status = status
            r.__enter__ = lambda *a: r
            r.__exit__ = lambda *a: None
            return r

        if path == "/contacts/lists" and method == "GET":
            return ok({"lists": self.lists, "count": len(self.lists)})
        if path == "/contacts/folders" and method == "GET":
            return ok({"folders": self.folders, "count": len(self.folders)})
        if path == "/contacts/folders" and method == "POST":
            self.folders.append({"id": len(self.folders) + 1, "name": body["name"]})
            return ok({"id": self.folders[-1]["id"]}, 201)
        if path == "/contacts/lists" and method == "POST":
            self.lists.append({"id": 100 + len(self.lists), "name": body["name"], "folderId": body["folderId"]})
            return ok({"id": self.lists[-1]["id"]}, 201)
        if path == "/contacts" and method == "POST":
            c = self.contacts.setdefault(body["email"], {"id": len(self.contacts) + 1, "email": body["email"],
                                                         "attributes": {}, "listIds": []})
            c["attributes"].update(body.get("attributes", {}))
            c["listIds"] = sorted(set(c["listIds"]) | set(body.get("listIds", [])))
            return ok({"id": c["id"]}, 201)
        if path.startswith("/contacts/") and method == "GET":
            email = unquote(path.removeprefix("/contacts/"))
            if email not in self.contacts:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
            return ok(self.contacts[email])
        raise AssertionError(f"appel inattendu {method} {path}")


@pytest.fixture
def brevo(monkeypatch, approval):  # noqa: F811
    fake = FakeBrevo()
    monkeypatch.setattr(access, "BREVO_API_KEY", "xkeysib-test")
    monkeypatch.setattr(access.urllib.request, "urlopen", fake.urlopen)
    return fake


def restart():
    """Simule un redéploiement Render : mémoire et fichier local effacés."""
    access._approved = None
    access._requests.clear()
    access._brevo_list_id = None
    import pathlib
    pathlib.Path(access.ACCESS_STORE).unlink(missing_ok=True)


def test_approval_is_saved_in_brevo_list(client, approval, brevo):
    request_access(client, "Ana", email="Ana@Exemple.fr")
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "approve"})
    assert brevo.lists == [{"id": 100, "name": "cloclo – accès approuvés", "folderId": 1}]
    contact = brevo.contacts["ana@exemple.fr"]
    assert contact["listIds"] == [100] and contact["attributes"]["FIRSTNAME"] == "Ana"


def test_approval_survives_redeploy(client, approval, brevo):
    request_access(client, "Ana")
    client.post("/api/access/decide", data={"t": decide_link(approval), "action": "approve"})
    alerts = len(approval.to("moi@gmail.com"))
    restart()
    r = client.post("/api/access/request", json={"name": "Ana", "email": "ana@exemple.fr"})
    assert r.json() == {"status": "already_approved"}
    assert len(approval.to("moi@gmail.com")) == alerts       # pas de nouvelle alerte
    assert approval.to("ana@exemple.fr")[-1][1] == "Ton accès à cloclo"


def test_existing_list_is_reused(client, approval, brevo):
    brevo.folders = [{"id": 7, "name": "Mes contacts"}]
    brevo.lists = [{"id": 42, "name": "cloclo – accès approuvés", "folderId": 7}]
    access.remember_approval("bob@exemple.fr", "x", "Bob")
    assert brevo.contacts["bob@exemple.fr"]["listIds"] == [42]
    assert ("POST", "/contacts/lists") not in brevo.calls


def test_contact_outside_the_list_is_not_approved(client, approval, brevo):
    """Un contact Brevo qui n'est pas dans la liste (ex. retiré à la main) n'est pas approuvé."""
    brevo.contacts["eve@exemple.fr"] = {"id": 9, "email": "eve@exemple.fr", "attributes": {}, "listIds": [5]}
    assert access.lookup_approval("eve@exemple.fr") is None
    request_access(client, "Eve", email="eve@exemple.fr")
    assert len(approval.to("moi@gmail.com")) == 1             # alerte envoyée au propriétaire


def test_unknown_email_is_not_approved(client, approval, brevo):
    assert access.lookup_approval("nobody@exemple.fr") is None


def test_brevo_down_falls_back_to_owner_alert(client, approval, brevo):
    brevo.down = True
    r = client.post("/api/access/request", json={"name": "Ana", "email": "ana@exemple.fr"})
    assert r.json()["status"] == "pending"
    assert len(approval.to("moi@gmail.com")) == 1


def test_brevo_down_does_not_break_approval(client, approval, brevo):
    request_access(client, "Ana")
    brevo.down = True
    page = client.post("/api/access/decide", data={"t": decide_link(approval), "action": "approve"})
    assert "Accès accordé" in page.text                        # approuvé quand même
    assert access.approved_emails()["ana@exemple.fr"]["name"] == "Ana"


def test_lookup_is_cached_after_first_hit(client, approval, brevo):
    access.remember_approval("ana@exemple.fr", "x", "Ana")
    restart()
    access.lookup_approval("ana@exemple.fr")
    calls = len(brevo.calls)
    access.lookup_approval("ana@exemple.fr")
    assert len(brevo.calls) == calls


def test_without_brevo_key_uses_local_file(client, approval, monkeypatch):
    monkeypatch.setattr(access, "BREVO_API_KEY", "")
    access.remember_approval("ana@exemple.fr", "x", "Ana")
    access._approved = None
    assert access.lookup_approval("ana@exemple.fr")["name"] == "Ana"
