"""Création de fichiers : générateurs PDF/Excel (unitaires) et boucle d'outils du chat (intégration)."""

import io

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

import generators as g
import main
from conftest import chat, read_events, sse

PDF_INPUT = {"filename": "rapport.pdf", "title": "Rapport été 2026",
             "content": "## Résumé\n\nPrix : 12,50 € « très bien ».\n\n| Nom | Âge |\n|---|---|\n| Zoé | 31 |\n\n- un\n- deux\n"}
XLSX_INPUT = {"filename": "budget.xlsx", "sheets": [{
    "name": "Budget", "columns": ["Poste", "Montant", "Date"],
    "rows": [["Loyer", 800, "2026-01-05"], ["Courses", 320.5, "2026-01-10"], ["Total", "=SUM(B2:B3)", None]],
    "number_formats": {"Montant": "#,##0.00 €"}}]}


def pdf_text(raw: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(raw)).pages)


# ------------------------------------------------------------ unitaires : PDF
def test_pdf_has_title_content_and_french_characters():
    raw = g.build_pdf(g.PdfInput(**PDF_INPUT))
    assert raw.startswith(b"%PDF")
    text = pdf_text(raw)
    for needle in ["Rapport été 2026", "Résumé", "12,50 €", "« très bien »", "Zoé", "page 1 / 1"]:
        assert needle in text, needle


def test_long_pdf_has_several_numbered_pages():
    raw = g.build_pdf(g.PdfInput(filename="a", title="Long", content="Paragraphe.\n\n" * 200))
    pages = PdfReader(io.BytesIO(raw)).pages
    assert len(pages) > 1
    assert f"page {len(pages)} / {len(pages)}" in pages[-1].extract_text()


def test_pdf_escapes_html_in_title():
    text = pdf_text(g.build_pdf(g.PdfInput(filename="a", title="<b>x</b> & y", content="ok")))
    assert "<b>x</b> & y" in text


# ------------------------------------------------------------ unitaires : Excel
def test_excel_values_formulas_dates_and_formats():
    wb = load_workbook(io.BytesIO(g.build_excel(g.ExcelInput(**XLSX_INPUT))))
    ws = wb["Budget"]
    assert [c.value for c in ws[1]] == ["Poste", "Montant", "Date"]
    assert ws["B2"].value == 800 and ws["B3"].value == 320.5           # vrais nombres
    assert ws["B4"].value == "=SUM(B2:B3)"                              # vraie formule
    assert ws["C2"].value.year == 2026 and ws["C2"].number_format == "DD/MM/YYYY"
    assert ws["B2"].number_format == "#,##0.00 €"
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:C4"
    assert ws["A1"].font.bold


def test_excel_sheet_names_are_cleaned_and_unique():
    data = g.ExcelInput(filename="x", sheets=[
        {"name": "Ventes: T1/2026", "columns": ["a"], "rows": []},
        {"name": "Ventes: T1/2026", "columns": ["a"], "rows": []},
        {"name": "x" * 50, "columns": ["a"], "rows": []}])
    names = load_workbook(io.BytesIO(g.build_excel(data))).sheetnames
    assert names[0] == "Ventes- T1-2026" and names[1] == "Ventes- T1-2026 (2)"
    assert len(names[2]) == 31


def test_excel_rejects_rows_longer_than_columns():
    with pytest.raises(ValueError):
        g.ExcelInput(filename="x", sheets=[{"name": "a", "columns": ["a"], "rows": [[1, 2]]}])


def test_excel_short_rows_are_allowed():
    ws = load_workbook(io.BytesIO(g.build_excel(g.ExcelInput(
        filename="x", sheets=[{"name": "a", "columns": ["a", "b"], "rows": [[1]]}])))).active
    assert ws["A2"].value == 1 and ws["B2"].value is None


# ------------------------------------------------------------ unitaires : noms et stockage
@pytest.mark.parametrize("given,ext,expected", [
    ("rapport.pdf", ".pdf", "rapport.pdf"),
    ("Rapport Été 2026", ".pdf", "Rapport-Ete-2026.pdf"),
    ("../../etc/passwd", ".pdf", "passwd.pdf"),
    ("C:\\\\Users\\\\x\\\\budget.xlsx", ".xlsx", "budget.xlsx"),
    ("???", ".xlsx", "document.xlsx"),
])
def test_safe_filename(given, ext, expected):
    assert g.safe_filename(given, ext) == expected


def test_store_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FILES_DIR", tmp_path)
    fid = g.store(b"data", "a.pdf")
    path, name = g.load(fid)
    assert name == "a.pdf" and path.read_bytes() == b"data"


@pytest.mark.parametrize("fid", ["", "../../etc", "abc", "x" * 100, "a/b" * 10])
def test_load_rejects_bad_ids(fid):
    assert g.load(fid) is None


def test_files_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FILES_DIR", tmp_path)
    fid = g.store(b"data", "a.pdf")
    monkeypatch.setattr(g, "FILE_TTL", -1)
    assert g.load(fid) is None


def test_run_tool_reports_invalid_input():
    file, msg, err = g.run_tool("create_pdf", {"filename": "a.pdf"})
    assert file is None and err and "title" in msg
    assert g.run_tool("delete_everything", {})[2] is True


# ------------------------------------------------------------ intégration : boucle d'outils dans le chat
@pytest.fixture(autouse=True)
def files_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FILES_DIR", tmp_path)


def test_chat_sends_tools(client, upstream):
    chat(client)
    names = [t["name"] for t in upstream.last["tools"]]
    assert names == ["create_pdf", "create_excel"]
    assert all(t["eager_input_streaming"] for t in upstream.last["tools"])


def test_chat_creates_pdf_and_offers_download(client, upstream):
    upstream.replies(sse(texts=["Je prépare le PDF."], stop_reason="tool_use", tool=("create_pdf", PDF_INPUT)),
                     sse(texts=["Voici ton rapport."]))
    events = read_events(chat(client, "Fais-moi un PDF"))
    kinds = [e["type"] for e in events]
    assert "status" in kinds and kinds[-1] == "done"
    file = next(e for e in events if e["type"] == "file")
    assert file["name"] == "rapport.pdf" and file["kind"] == "pdf"
    text = "".join(e.get("text", "") for e in events if e["type"] == "text")
    assert text == "Je prépare le PDF.\n\nVoici ton rapport."
    # le fichier se télécharge
    r = client.get(file["url"])
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="rapport.pdf"' in r.headers["content-disposition"]
    # Claude a reçu le résultat de l'outil au 2e tour
    second = upstream.requests[-1]["json"]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert "rapport.pdf" in second[-1]["content"][0]["content"]
    assert second[-2]["role"] == "assistant"


def test_chat_creates_excel(client, upstream):
    upstream.replies(sse(texts=(), stop_reason="tool_use", tool=("create_excel", XLSX_INPUT)),
                     sse(texts=["Ton budget est prêt."]))
    events = read_events(chat(client, "Un tableau Excel de mon budget"))
    file = next(e for e in events if e["type"] == "file")
    r = client.get(file["url"])
    ws = load_workbook(io.BytesIO(r.content))["Budget"]
    assert ws["B4"].value == "=SUM(B2:B3)"


def test_chat_file_without_text_is_not_an_error(client, upstream):
    upstream.replies(sse(texts=(), stop_reason="tool_use", tool=("create_pdf", PDF_INPUT)),
                     sse(texts=(), stop_reason="end_turn"))
    events = read_events(chat(client))
    assert events[-1]["type"] == "done"


def test_chat_invalid_tool_input_is_sent_back_as_error(client, upstream):
    upstream.replies(sse(texts=(), stop_reason="tool_use", tool=("create_pdf", {"filename": "x.pdf"})),
                     sse(texts=["Désolé, je corrige."]))
    events = read_events(chat(client))
    assert not any(e["type"] == "file" for e in events)
    result = upstream.requests[-1]["json"]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "title" in result["content"]


def test_chat_truncated_tool_input_is_not_run(client, upstream):
    upstream.replies(sse(texts=(), stop_reason="max_tokens", tool=("create_pdf", PDF_INPUT)))
    events = read_events(chat(client))
    assert events[-1]["type"] == "error" and "trop long" in events[-1]["message"]
    assert not any(e["type"] == "file" for e in events)
    assert len(upstream.requests) == 1


def test_chat_tool_loop_is_bounded(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "MAX_TOOL_ROUNDS", 2)
    loop = sse(texts=(), stop_reason="tool_use", tool=("create_pdf", PDF_INPUT))
    upstream.replies(loop, loop, loop, loop)
    events = read_events(chat(client))
    assert len(upstream.requests) == 2
    assert sum(e["type"] == "file" for e in events) == 2


def test_download_unknown_file_is_404(client):
    r = client.get("/api/files/" + "x" * 24)
    assert r.status_code == 404 and "expiré" in r.json()["detail"]


def test_system_prompt_explains_file_notes_in_history():
    """Sans cette consigne, Claude recrée au tour suivant un fichier qu'il a déjà livré."""
    assert "[Fichier créé : nom]" in g.SYSTEM_PROMPT and "ne le recrée pas" in g.SYSTEM_PROMPT
