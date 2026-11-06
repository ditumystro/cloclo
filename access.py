"""Accès sur approbation : un nouveau visiteur demande l'accès, le propriétaire
reçoit un e-mail avec un lien pour approuver ou refuser, puis le visiteur
reçoit un jeton d'accès signé.

Pas de base de données : les jetons sont signés (HMAC) avec ACCESS_SECRET, donc
un accès accordé reste valable après un redémarrage du serveur. Seules les
demandes en attente vivent en mémoire, le temps que le propriétaire réponde.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import smtplib
import time
import urllib.request
from email.message import EmailMessage

from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("cloclo.access")

# ---------------------------------------------------------------- configuration
# L'approbation est active dès qu'une adresse de propriétaire est définie.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip()
# Clé de signature des jetons. Si elle change, tous les accès sont révoqués.
ACCESS_SECRET = os.environ.get("ACCESS_SECRET", "")
# Adresse publique de l'app, pour les liens de l'e-mail (sinon : déduite de la requête).
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# Identifiants d'accès révoqués, séparés par des virgules (visibles dans l'e-mail).
REVOKED = {x.strip() for x in os.environ.get("REVOKED_ACCESS", "").split(",") if x.strip()}

# Envoi des e-mails : Resend (API HTTPS) si configuré, sinon Gmail (SMTP).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "cloclo <onboarding@resend.dev>")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

REQUEST_TTL = 7 * 24 * 3600      # une demande (et son lien d'approbation) expire au bout de 7 jours
REQUESTS_PER_HOUR = 3            # par IP, pour éviter qu'on inonde ta boîte mail

if OWNER_EMAIL and not ACCESS_SECRET:
    # Sans secret fixe, les accès accordés ne survivent pas à un redémarrage.
    ACCESS_SECRET = secrets.token_urlsafe(32)
    logger.warning("ACCESS_SECRET absent : clé temporaire générée, les accès seront perdus au redémarrage.")


def approval_required() -> bool:
    return bool(OWNER_EMAIL)


# ---------------------------------------------------------------- jetons signés
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    mac = hmac.new(ACCESS_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(token: str | None, kind: str, max_age: int | None = None) -> dict | None:
    """Renvoie le contenu du jeton s'il est authentique, du bon type et pas expiré."""
    if not token or token.count(".") != 1:
        return None
    body, mac = token.split(".")
    expected = _b64(hmac.new(ACCESS_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if data.get("kind") != kind:
        return None
    if max_age is not None and time.time() - data.get("iat", 0) > max_age:
        return None
    return data


def check_access(token: str | None) -> None:
    """Appelé avant chaque message envoyé à Claude."""
    if not approval_required():
        return
    grant = verify(token, "grant")
    if not grant or grant["id"] in REVOKED:
        raise HTTPException(403, "Accès non autorisé. Demande l’accès au propriétaire de l’app.")


# ---------------------------------------------------------------- demandes en attente
# id -> {"name", "status": "pending" | "approved" | "denied"}
_requests: dict[str, dict] = {}
_request_hits: dict[str, list[float]] = {}


def _limit_requests(ip: str) -> None:
    now = time.time()
    hits = [t for t in _request_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= REQUESTS_PER_HOUR:
        raise HTTPException(429, "Trop de demandes d’accès. Réessaie dans une heure.")
    hits.append(now)
    _request_hits[ip] = hits


# ---------------------------------------------------------------- e-mail
def _send_resend(subject: str, text: str, html_body: str) -> None:
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": RESEND_FROM, "to": [OWNER_EMAIL], "subject": subject,
                         "text": text, "html": html_body}).encode(),
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15).read()


def _send_gmail(subject: str, text: str, html_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_ADDRESS, OWNER_EMAIL
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def send_owner_email(subject: str, text: str, html_body: str) -> bool:
    """Envoie l'alerte au propriétaire. Renvoie False si aucun envoi n'est configuré ou s'il échoue."""
    try:
        if RESEND_API_KEY:
            _send_resend(subject, text, html_body)
        elif GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
            _send_gmail(subject, text, html_body)
        else:
            return False
        return True
    except Exception:
        logger.exception("Échec de l’envoi de l’e-mail d’alerte")
        return False


def _base_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def _request_email(name: str, message: str, ip: str, rid: str, link: str) -> tuple[str, str, str]:
    subject = f"cloclo : {name} demande l’accès"
    text = (f"{name} demande à utiliser cloclo.\n\n"
            + (f"Message : {message}\n\n" if message else "")
            + f"Adresse IP : {ip}\nIdentifiant : {rid}\n\n"
            f"Approuver ou refuser : {link}\n")
    e = html.escape
    html_body = f"""<div style="font-family:system-ui,sans-serif;font-size:15px;line-height:1.5;color:#1C2333">
<p><strong>{e(name)}</strong> demande à utiliser <strong>cloclo</strong>.</p>
{f'<p style="background:#EEF1F6;padding:10px 14px;border-radius:10px">« {e(message)} »</p>' if message else ''}
<p style="color:#5F6880;font-size:13px">Adresse IP : {e(ip)} · Identifiant : {e(rid)}</p>
<p><a href="{e(link)}" style="display:inline-block;background:#2F4BD8;color:#fff;text-decoration:none;
padding:10px 18px;border-radius:999px;font-weight:600">Voir la demande</a></p>
<p style="color:#5F6880;font-size:13px">Le lien ouvre une page où tu choisis « Approuver » ou « Refuser ».
Il expire dans 7 jours.</p></div>"""
    return subject, text, html_body


# ---------------------------------------------------------------- routes
router = APIRouter()


class AccessRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    message: str = Field(default="", max_length=500)


@router.post("/api/access/request")
async def request_access(body: AccessRequest, request: Request):
    if not approval_required():
        raise HTTPException(400, "Cette app ne demande pas d’approbation.")
    ip = request.client.host if request.client else "inconnue"
    _limit_requests(ip)

    rid = secrets.token_urlsafe(6)
    name, message = body.name.strip(), body.message.strip()
    _requests[rid] = {"name": name, "status": "pending"}

    link = f"{_base_url(request)}/api/access/decide?t={sign({'kind': 'decide', 'id': rid, 'name': name, 'iat': int(time.time())})}"
    sent = await asyncio.to_thread(send_owner_email, *_request_email(name, message, ip, rid, link))
    if not sent:
        # Aucun e-mail configuré (ou échec) : le lien reste utilisable depuis les journaux du serveur.
        logger.warning("Demande d’accès de %s (%s) — e-mail non envoyé. Lien : %s", name, rid, link)

    return {"request_token": sign({"kind": "request", "id": rid, "iat": int(time.time())}), "notified": sent}


@router.get("/api/access/status")
async def access_status(x_access_request: str | None = Header(default=None)):
    req = verify(x_access_request, "request", max_age=REQUEST_TTL)
    if not req:
        raise HTTPException(400, "Demande inconnue ou expirée. Refais une demande d’accès.")
    entry = _requests.get(req["id"])
    if not entry:
        return {"status": "unknown"}  # serveur redémarré avant la réponse : refaire une demande
    if entry["status"] == "approved":
        return {"status": "approved",
                "access_token": sign({"kind": "grant", "id": req["id"], "name": entry["name"], "iat": int(time.time())})}
    return {"status": entry["status"]}


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@700&family=Space+Grotesk:wght@400;500&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet"><title>{html.escape(title)} · cloclo</title>
<style>body{{font:16px/1.6 "Space Grotesk",system-ui,sans-serif;color:#E8ECFF;margin:0;padding:3rem 1rem;min-height:100vh;
background:radial-gradient(900px 500px at 85% -10%,rgba(139,92,255,.25),transparent 60%),#05070F}}
.card{{max-width:28rem;margin:0 auto;background:rgba(17,24,44,.7);border:1px solid rgba(139,92,255,.45);border-radius:18px;
padding:1.6rem;box-shadow:0 0 40px rgba(139,92,255,.15)}}
h1{{font:700 1.3rem/1.3 Unbounded,system-ui,sans-serif;margin:0 0 .8rem}}
button{{font:700 .8rem "JetBrains Mono",monospace;letter-spacing:.1em;text-transform:uppercase;border:none;border-radius:12px;
padding:.75rem 1.2rem;cursor:pointer;margin:.3rem .5rem 0 0}}
.ok{{background:linear-gradient(120deg,#3EE6FF,#8B5CFF 55%,#FF4FD8);color:#fff;box-shadow:0 0 22px rgba(139,92,255,.4)}}
.no{{background:transparent;color:#FF6B8B;border:1px solid #FF6B8B}}
.muted{{color:#8B95B8;font:.85rem "JetBrains Mono",monospace}}code{{color:#3EE6FF}}</style></head><body><div class="card">{body}</div></body></html>""")


@router.get("/api/access/decide", response_class=HTMLResponse)
async def decide_page(t: str = ""):
    # Une page de confirmation plutôt qu'une approbation directe : les antivirus
    # de messagerie ouvrent parfois les liens des e-mails tout seuls.
    data = verify(t, "decide", max_age=REQUEST_TTL)
    if not data:
        return _page("Lien invalide", "<h1>Lien invalide ou expiré</h1><p class='muted'>Demande à la personne de refaire une demande.</p>")
    name = html.escape(data["name"])
    status = _requests.get(data["id"], {}).get("status")
    done = {"approved": "Tu as déjà approuvé cette demande.", "denied": "Tu as déjà refusé cette demande."}.get(status, "")
    return _page("Demande d’accès", f"""<h1>{name} demande l’accès à cloclo</h1>
<p class="muted">Identifiant : {html.escape(data['id'])}{' · ' + done if done else ''}</p>
<form method="post" action="/api/access/decide">
<input type="hidden" name="t" value="{html.escape(t)}">
<button class="ok" name="action" value="approve">Approuver</button>
<button class="no" name="action" value="deny">Refuser</button></form>""")


@router.post("/api/access/decide", response_class=HTMLResponse)
async def decide(t: str = Form(...), action: str = Form(...)):
    data = verify(t, "decide", max_age=REQUEST_TTL)
    if not data or action not in ("approve", "deny"):
        return _page("Lien invalide", "<h1>Lien invalide ou expiré</h1>")
    status = "approved" if action == "approve" else "denied"
    _requests[data["id"]] = {"name": data["name"], "status": status}
    name = html.escape(data["name"])
    if status == "approved":
        return _page("Accès accordé", f"<h1>✓ Accès accordé à {name}</h1><p>Sa page se débloque toute seule dans quelques secondes.</p>"
                     f"<p class='muted'>Pour retirer cet accès plus tard, ajoute <code>{html.escape(data['id'])}</code> à REVOKED_ACCESS.</p>")
    return _page("Accès refusé", f"<h1>Accès refusé à {name}</h1><p class='muted'>La personne verra que sa demande a été refusée.</p>")
