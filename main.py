"""cloclo : un petit relais FastAPI vers l'API Claude d'Anthropic.

Le navigateur envoie la conversation à /api/chat ; le serveur l'envoie à
Claude avec la clé API (jamais exposée au navigateur) et renvoie la
réponse en flux NDJSON : une ligne JSON par événement.
"""

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal

import anthropic
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

import access
import generators

# ---------------------------------------------------------------- configuration
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Sonnet 5.5 et Opus 5.5 réfléchissent avant de répondre, et cette réflexion
# compte dans max_tokens : une limite basse donne des réponses coupées ou vides.
DEFAULT_MAX_TOKENS = 32_000
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", DEFAULT_MAX_TOKENS))
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))

# Fichiers joints : taille totale par requête, limite par image (fixée par l'API),
# nombre de fichiers par message.
MAX_UPLOAD_BYTES = int(float(os.environ.get("MAX_UPLOAD_MB", "20")) * 1_000_000)
MAX_IMAGE_BYTES = 5_000_000
MAX_FILES_PER_MESSAGE = 10
MAX_BASE64_CHARS = MAX_UPLOAD_BYTES * 4 // 3 + 4

MODELS = {
    "rapide": os.environ.get("CLAUDE_MODEL_FAST", "claude-haiku-5-5"),
    "standard": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
    "approfondi": os.environ.get("CLAUDE_MODEL_DEEP", "claude-opus-5-5"),
}

# Repli côté serveur : si le modèle refuse, l'API relance la requête sur un
# autre modèle. Non disponible pour Claude Haiku 5.5.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
PLACEHOLDER_KEYS = {"", "sk-ant-..."}

# Nombre max d'allers-retours Claude ↔ outils dans une même réponse
MAX_TOOL_ROUNDS = 6
TOOL_STATUS = {"create_pdf": "Création du PDF…", "create_word": "Création du document Word…",
               "create_excel": "Création du fichier Excel…"}

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger("cloclo")
app = FastAPI(title="cloclo", docs_url=None, redoc_url=None)
_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if ANTHROPIC_API_KEY.strip() in PLACEHOLDER_KEYS:
        raise HTTPException(500, "Le serveur n’a pas de clé API : mets ta vraie clé dans ANTHROPIC_API_KEY (fichier .env).")
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ---------------------------------------------------------------- schémas
class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="", max_length=255)
    data: str = Field(max_length=MAX_BASE64_CHARS)  # contenu en base64 ; vide = refusé plus loin, avec un message clair


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=50_000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_FILES_PER_MESSAGE)

    @model_validator(mode="after")
    def check_content(self):
        if self.attachments and self.role != "user":
            raise ValueError("Seuls les messages de l’utilisateur peuvent contenir des fichiers.")
        if not self.content.strip() and not self.attachments:
            raise ValueError("Un message ne peut pas être vide.")
        return self


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=100)
    system: str | None = Field(default=None, max_length=10_000)
    mode: Literal["rapide", "standard", "approfondi"] = "standard"


# ---------------------------------------------------------------- fichiers joints
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx",
    ".jsx", ".py", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb",
    ".php", ".swift", ".sh", ".bash", ".sql", ".r", ".tex", ".srt", ".vtt",
}
EXTENSION_TYPES = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".docx": DOCX_TYPE,
}


def _bad_file(name: str, why: str) -> HTTPException:
    return HTTPException(422, f"Fichier « {name} » : {why}")


def detect_type(att: Attachment) -> str:
    """Type MIME du fichier ; le navigateur n'en donne pas toujours (ex. .md)."""
    ext = Path(att.name).suffix.lower()
    if ext in EXTENSION_TYPES:
        return EXTENSION_TYPES[ext]
    if ext in TEXT_EXTENSIONS or att.media_type.startswith("text/"):
        return "text/plain"
    return att.media_type.lower()


def docx_text(raw: bytes) -> str:
    import docx  # chargé seulement si besoin

    doc = docx.Document(io.BytesIO(raw))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts).strip()


def text_document(name: str, text: str) -> dict:
    return {"type": "document", "title": name,
            "source": {"type": "text", "media_type": "text/plain", "data": text}}


def attachment_block(att: Attachment) -> tuple[dict, int]:
    """Convertit un fichier joint en bloc de contenu pour l'API. Renvoie (bloc, taille)."""
    try:
        raw = base64.b64decode(att.data, validate=True)
    except (binascii.Error, ValueError):
        raise _bad_file(att.name, "contenu illisible (base64 invalide).")
    if not raw:
        raise _bad_file(att.name, "le fichier est vide.")

    kind = detect_type(att)
    b64 = base64.standard_b64encode(raw).decode()
    if kind == "application/pdf":
        if not raw.startswith(b"%PDF"):
            raise _bad_file(att.name, "ce n’est pas un PDF valide.")
        return {"type": "document", "title": att.name,
                "source": {"type": "base64", "media_type": kind, "data": b64}}, len(raw)
    if kind in IMAGE_TYPES:
        if len(raw) > MAX_IMAGE_BYTES:
            raise _bad_file(att.name, f"image trop lourde (max {MAX_IMAGE_BYTES // 1_000_000} Mo).")
        return {"type": "image", "source": {"type": "base64", "media_type": kind, "data": b64}}, len(raw)
    if kind == DOCX_TYPE:
        try:
            text = docx_text(raw)
        except Exception:
            raise _bad_file(att.name, "document Word illisible ou corrompu.")
        if not text:
            raise _bad_file(att.name, "le document Word ne contient pas de texte.")
        return text_document(att.name, text), len(raw)
    if kind == "text/plain":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise _bad_file(att.name, "le texte n’est pas en UTF-8.")
        return text_document(att.name, text), len(raw)
    raise _bad_file(att.name, "type non pris en charge. Formats acceptés : PDF, images (PNG, JPG, GIF, WebP), "
                              "Word (.docx), texte et code (.txt, .md, .csv, .json, .py…).")


def to_api_messages(messages: list[Message]) -> list[dict]:
    """Messages de l'app → messages de l'API, fichiers joints inclus."""
    out, total = [], 0
    for m in messages:
        if not m.attachments:
            out.append({"role": m.role, "content": m.content})
            continue
        blocks = []
        for att in m.attachments:
            block, size = attachment_block(att)
            total += size
            blocks.append(block)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Fichiers trop lourds : {MAX_UPLOAD_BYTES // 1_000_000} Mo au total maximum.")
        if m.content.strip():
            blocks.append({"type": "text", "text": m.content})
        out.append({"role": m.role, "content": blocks})
    return out


# ---------------------------------------------------------------- garde-fous
_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Pas de lecture directe de X-Forwarded-For : le client peut l'inventer pour
    # contourner la limite. Derrière un proxy, uvicorn --proxy-headers place déjà
    # la vraie IP dans request.client.
    return request.client.host if request.client else "inconnu"


def check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Trop de demandes. Attends une minute avant de renvoyer.")
    q.append(now)


def check_password(given: str | None) -> None:
    if APP_PASSWORD and not secrets.compare_digest(given or "", APP_PASSWORD):
        raise HTTPException(401, "Mot de passe incorrect.")


# ---------------------------------------------------------------- routes
app.include_router(access.router)


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/config")
async def config():
    return {"password_required": bool(APP_PASSWORD), "approval_required": access.approval_required(),
            "modes": list(MODELS)}


@app.post("/api/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    x_app_password: str | None = Header(default=None),
    x_access_token: str | None = Header(default=None),
):
    check_password(x_app_password)
    access.check_access(x_access_token)
    check_rate_limit(client_ip(request))

    if body.messages[0].role != "user" or body.messages[-1].role != "user":
        raise HTTPException(422, "La conversation doit commencer et finir par un message de l’utilisateur.")

    messages = to_api_messages(body.messages)
    client = get_client()
    # Consignes fixes d'abord (outils + système), puis celles de l'utilisateur :
    # le début de la requête reste identique d'un message à l'autre, donc en cache.
    system = [{"type": "text", "text": generators.SYSTEM_PROMPT}]
    if body.system and body.system.strip():
        system.append({"type": "text", "text": "Consignes de l’utilisateur :\n" + body.system.strip()})
    params = {
        "model": MODELS[body.mode],
        "max_tokens": MAX_TOKENS,
        "system": system,
        "tools": generators.TOOLS,
        "messages": messages,
        # Les fichiers sont renvoyés à chaque message de la conversation : la mise
        # en cache évite de les repayer plein tarif à chaque fois.
        "cache_control": {"type": "ephemeral"},
    }
    if "haiku" in params["model"]:
        open_stream = client.messages.stream
    else:
        open_stream = client.beta.messages.stream
        params |= {"betas": [FALLBACK_BETA], "fallbacks": "default"}

    def line(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + "\n"

    async def events():
        wrote = False      # du texte a déjà été envoyé au navigateur
        files = 0          # fichiers créés pendant cette réponse
        json_retries = 0
        final = None
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                try:
                    separate = wrote  # nouveau tour après un outil : on saute une ligne
                    async with open_stream(**params) as stream:
                        async for event in stream:
                            if event.type == "text" and event.text:
                                if separate:
                                    yield line({"type": "text", "text": "\n\n"})
                                    separate = False
                                wrote = True
                                yield line({"type": "text", "text": event.text})
                            elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                                yield line({"type": "status", "text": TOOL_STATUS.get(event.content_block.name, "Création du fichier…")})
                        final = await stream.get_final_message()
                    json_retries = 0
                except ValueError:
                    # JSON d'outil illisible : pas de tool_use_id à qui répondre, on relance le tour.
                    json_retries += 1
                    if json_retries > 2:
                        raise
                    continue

                tool_uses = [b for b in final.content if b.type == "tool_use"]
                if final.stop_reason == "refusal" or not tool_uses:
                    break
                if final.stop_reason == "max_tokens":
                    yield line({"type": "error", "message": "Le document demandé est trop long pour être créé en une fois. Demande-le en plusieurs parties."})
                    return

                results = []
                for block in tool_uses:
                    file, result, is_error = await asyncio.to_thread(generators.run_tool, block.name, block.input)
                    if file:
                        files += 1
                        yield line({"type": "file", "name": file["name"], "kind": file["kind"], "size": file["size"],
                                    "url": f"/api/files/{file['id']}"})
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result,
                                    **({"is_error": True} if is_error else {})})
                params["messages"] = params["messages"] + [
                    {"role": "assistant", "content": final.content},
                    {"role": "user", "content": results},
                ]
            else:
                logger.warning("Trop de tours d’outils, arrêt")

            if final is not None and final.stop_reason == "refusal" and not wrote and not files:
                yield line({"type": "error", "message": "Claude a refusé de répondre à cette demande. Reformule-la."})
            elif not wrote and not files:
                yield line({"type": "error", "message": "Claude a renvoyé une réponse vide. Réessaie ou reformule."})
            else:
                yield line({"type": "done", "stop_reason": final.stop_reason if final else None,
                            "model": final.model if final else params["model"]})
        except anthropic.AuthenticationError:
            yield line({"type": "error", "message": "Clé API refusée par Anthropic. Vérifie ANTHROPIC_API_KEY."})
        except anthropic.RateLimitError:
            yield line({"type": "error", "message": "Limite de l’API Anthropic atteinte. Réessaie dans un moment."})
        except anthropic.NotFoundError:
            yield line({"type": "error", "message": f"Modèle introuvable : {params['model']}. Vérifie les variables CLAUDE_MODEL*."})
        except anthropic.BadRequestError as e:
            yield line({"type": "error", "message": f"Requête refusée par l’API : {e.message}"})
        except anthropic.APIStatusError as e:
            yield line({"type": "error", "message": f"Erreur de l’API Anthropic ({e.status_code}). Réessaie."})
        except anthropic.APIConnectionError:
            yield line({"type": "error", "message": "Impossible de joindre l’API Anthropic depuis le serveur."})
        except Exception:
            # Le flux HTTP est déjà ouvert : on ne peut plus renvoyer un code 500,
            # alors on termine par une ligne d'erreur lisible plutôt qu'une coupure.
            logger.exception("Erreur inattendue pendant le flux")
            yield line({"type": "error", "message": "Erreur inattendue sur le serveur. Réessaie."})

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/files/{fid}")
async def download(fid: str):
    """Téléchargement d'un fichier créé par Claude (lien valable une heure)."""
    found = generators.load(fid)
    if not found:
        raise HTTPException(404, "Ce fichier a expiré ou n’existe pas. Demande à Claude de le recréer.")
    path, name = found
    return FileResponse(path, filename=name, media_type=generators.MIME.get(Path(name).suffix, "application/octet-stream"))


@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    # Le navigateur revalide la page et le script à chaque visite : une mise à
    # jour de l'app est visible tout de suite, sans Ctrl+Maj+R.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Le frontend (monté en dernier pour ne pas masquer /api)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
