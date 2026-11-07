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
    assert names == ["create_pdf", "create_word", "create_excel"]
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


# ------------------------------------------------------------ Word (.docx)
from docx import Document  # noqa: E402

WORD_MD = """## Introduction

Texte avec **gras**, *italique* et `code`, accents : été, €, « ok ».

1. Premier
2. Deuxième
    - sous-point
3. Troisième

- Puce A
- Puce B

| Nom | Âge |
|---|---|
| Zoé | 31 |

> Une citation.

```python
print("bonjour")
```

## Autre liste

1. Doit repartir à 1
2. Suite
"""


def word(content=WORD_MD, title="Rapport été 2026"):
    return Document(io.BytesIO(g.build_word(g.PdfInput(filename="x", title=title, content=content))))


def test_word_title_headings_and_text():
    doc = word()
    styles = [(p.style.name, p.text) for p in doc.paragraphs]
    assert styles[0] == ("Title", "Rapport été 2026")
    assert ("Heading 2", "Introduction") in styles and ("Heading 2", "Autre liste") in styles
    body = next(p for p in doc.paragraphs if p.text.startswith("Texte avec"))
    assert body.text == "Texte avec gras, italique et code, accents : été, €, « ok »."
    bold = [r.text for r in body.runs if r.bold]
    italic = [r.text for r in body.runs if r.italic]
    assert bold == ["gras"] and italic == ["italique"]


def test_word_lists_are_real_word_lists():
    styles = [(p.style.name, p.text) for p in word().paragraphs]
    assert ("List Number", "Premier") in styles and ("List Number", "Deuxième") in styles
    assert ("List Bullet 2", "sous-point") in styles
    assert ("List Bullet", "Puce A") in styles


def test_word_numbered_lists_restart_at_one():
    doc = word()
    firsts = [p for p in doc.paragraphs if p.text in ("Premier", "Doit repartir à 1")]
    ids = [p._p.pPr.numPr.numId.val for p in firsts]
    assert ids[0] != ids[1]  # chaque liste a sa propre numérotation


def test_word_table_with_styled_header():
    table = word().tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Nom", "Âge"]
    assert [c.text for c in table.rows[1].cells] == ["Zoé", "31"]
    assert all(r.bold for r in table.rows[0].cells[0].paragraphs[0].runs)
    assert 'w:fill="5B21B6"' in table.rows[0].cells[0]._tc.xml


def test_word_quote_code_and_page_numbers():
    doc = word()
    assert any(p.style.name == "Quote" and p.text == "Une citation." for p in doc.paragraphs)
    assert any(p.text == 'print("bonjour")' for p in doc.paragraphs)
    footer = doc.sections[0].footer.paragraphs[0]._p.xml
    assert "PAGE" in footer and "NUMPAGES" in footer
    assert abs(doc.sections[0].page_width.cm - 21) < 0.1  # A4


def test_word_keeps_line_breaks_like_the_pdf():
    """Adresse ou signature sur plusieurs lignes : chaque ligne reste une ligne."""
    doc = word("Jean Dupont\n12 rue des Lilas\n75001 Paris\n\n1. A\n2. B\n    - sous\n")
    texts = [p.text for p in doc.paragraphs][2:]
    assert texts == ["Jean Dupont\n12 rue des Lilas\n75001 Paris", "A", "B", "sous"]


def test_word_tool_and_download(client, upstream):
    upstream.replies(sse(texts=(), stop_reason="tool_use", tool=("create_word", {
        "filename": "Lettre de motivation", "title": "Lettre", "content": "Madame, Monsieur,\n\nBonjour."})),
        sse(texts=["Ta lettre est prête."]))
    events = read_events(chat(client, "Fais-moi une lettre en Word"))
    file = next(e for e in events if e["type"] == "file")
    assert file["name"] == "Lettre-de-motivation.docx" and file["kind"] == "docx"
    assert any(e["type"] == "status" and "Word" in e["text"] for e in events)
    r = client.get(file["url"])
    assert r.headers["content-type"] == g.MIME[".docx"]
    assert Document(io.BytesIO(r.content)).paragraphs[2].text == "Madame, Monsieur,"


def test_word_tool_is_offered_to_claude(client, upstream):
    chat(client)
    assert [t["name"] for t in upstream.last["tools"]] == ["create_pdf", "create_word", "create_excel"]
    assert "create_word" in g.SYSTEM_PROMPT
