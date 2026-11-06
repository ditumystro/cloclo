# Prompt → Claude

Petite app web : un frontend où l'on tape un prompt, et un backend FastAPI qui
le transmet à l'API Claude avec ta clé et renvoie la réponse en streaming.
La clé API reste sur le serveur, jamais dans le navigateur.

## Lancer en local

```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                 # Windows : set ANTHROPIC_API_KEY=...
uvicorn main:app --reload
```

Puis ouvre http://localhost:8000.

## Fichiers joints

Clique sur 📎 ou glisse des fichiers dans la zone de saisie (on peut aussi coller une capture d'écran).

| Format | Envoi à Claude |
|---|---|
| PDF | tel quel : Claude lit le texte, la mise en page, les tableaux et les images |
| PNG, JPG, GIF, WebP | tel quel (5 Mo max par image) |
| Word (.docx) | converti en texte sur le serveur (paragraphes et tableaux) |
| Texte et code (.txt, .md, .csv, .json, .py…) | en texte (UTF-8) |

Limites : 10 fichiers par message, `MAX_UPLOAD_MB` (20 Mo) au total par conversation.
Les fichiers restent dans la conversation : Claude s'en souvient aux messages suivants.
Ils sont renvoyés à chaque message, avec mise en cache pour ne pas les repayer plein tarif.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_unit.py` : mot de passe, limite de débit, clé API, schémas.
- `tests/test_attachments.py` : fichiers joints (PDF, images, Word, texte), limites, erreurs.
- `tests/test_integration.py` : requêtes HTTP vers l'app, avec le vrai SDK
  Anthropic branché sur un faux serveur Anthropic (aucune clé, aucun coût).
- `tests/test_live.py` : un vrai appel à l'API, ignoré par défaut. Pour le lancer :
  `set -a; source .env; set +a; RUN_LIVE_TESTS=1 pytest tests/test_live.py`

## Déployer sur Render (gratuit)

1. Pousse ce dossier dans un dépôt GitHub (le `.gitignore` exclut déjà `.env`).
2. Sur https://render.com : New → Blueprint, choisis le dépôt. Render lit `render.yaml`.
3. Renseigne `ANTHROPIC_API_KEY` et `APP_PASSWORD` quand Render les demande.
4. Ton app est en ligne sur `https://prompt-claude-xxxx.onrender.com`.

Le plan gratuit met le service en veille après un moment d'inactivité :
la première requête après une pause peut prendre ~30 s.

## Autres hébergeurs

Le `Dockerfile` marche tel quel sur Railway, Fly.io, Google Cloud Run, etc. :
il suffit de définir les variables d'environnement.

```bash
docker build -t prompt-claude .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... -e APP_PASSWORD=... prompt-claude
```

## Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `ANTHROPIC_API_KEY` | Clé API Anthropic (obligatoire) | — |
| `APP_PASSWORD` | Mot de passe demandé dans l'app ; vide = app ouverte à tous | vide |
| `MAX_TOKENS` | Longueur max d’une réponse (réflexion comprise) | 32000 |
| `MAX_UPLOAD_MB` | Taille max des fichiers joints par conversation | 20 |
| `RATE_LIMIT_PER_MIN` | Requêtes max par minute et par IP | 20 |
| `CLAUDE_MODEL_FAST` | Modèle du mode « Rapide » | claude-haiku-5-5 |
| `CLAUDE_MODEL` | Modèle du mode « Standard » | claude-sonnet-5-5 |
| `CLAUDE_MODEL_DEEP` | Modèle du mode « Approfondi » | claude-opus-5-5 |

## API

`POST /api/chat` avec l'en-tête `X-App-Password` si un mot de passe est défini :

```json
{ "messages": [{"role": "user", "content": "Bonjour"}],
  "system": "Réponds en français.", "mode": "standard" }
```

Réponse en NDJSON, une ligne par événement :
`{"type":"text","text":"..."}`, puis `{"type":"done","stop_reason":"end_turn"}`
ou `{"type":"error","message":"..."}`.

## Structure

```
main.py            backend FastAPI
static/index.html  interface
static/app.js      logique du frontend
render.yaml        déploiement Render
Dockerfile         déploiement conteneur
```
