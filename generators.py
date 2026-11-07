"""Création de fichiers à la demande de Claude : PDF (depuis du Markdown) et
classeurs Excel (feuilles, colonnes, lignes, formules).

Claude fournit le contenu via ses outils create_pdf / create_excel ; ce module
le met en forme. Les fichiers créés sont gardés une heure, le temps que
l'utilisateur les télécharge.
"""

import io
import re
import secrets
import tempfile
import time
import unicodedata
from datetime import date
from html import escape
from pathlib import Path

import markdown
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from lxml import html as lxml_html
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from pydantic import BaseModel, Field, field_validator
from xhtml2pdf import pisa

# ---------------------------------------------------------------- outils pour Claude
CREATE_PDF_TOOL = {
    "name": "create_pdf",
    "description": (
        "Crée un document PDF téléchargeable pour l'utilisateur, mis en page proprement (A4, titres, "
        "listes, tableaux, code). Utilise cet outil quand l'utilisateur demande un PDF, un rapport, une "
        "fiche, une lettre, un CV, un compte rendu ou tout document à télécharger ou imprimer. "
        "Écris le contenu complet en Markdown : # titres, listes, **gras**, *italique*, tableaux "
        "Markdown (| a | b |), blocs de code. N'inclus pas le titre principal dans le contenu : il est "
        "ajouté automatiquement en haut de la première page. Après l'appel, résume en une ou deux "
        "phrases ce que contient le document ; ne recopie pas son contenu dans ta réponse."
    ),
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Nom du fichier, ex. « rapport-ventes-2026.pdf »."},
            "title": {"type": "string", "description": "Titre affiché en haut du document."},
            "content": {"type": "string", "description": "Contenu complet du document, en Markdown."},
        },
        "required": ["filename", "title", "content"],
    },
}

CREATE_EXCEL_TOOL = {
    "name": "create_excel",
    "description": (
        "Crée un classeur Excel (.xlsx) téléchargeable : une ou plusieurs feuilles avec en-têtes mis en "
        "forme, filtres, volets figés et largeurs de colonnes ajustées. Utilise cet outil quand "
        "l'utilisateur demande un tableau Excel, un tableur, un fichier .xlsx, un budget, un planning, "
        "un inventaire ou des données à trier et calculer. Mets les nombres comme nombres (pas en texte), "
        "les dates au format AAAA-MM-JJ, et les calculs comme formules Excel en anglais commençant par "
        "« = » (ex. « =SUM(B2:B10) », « =B2*C2 »), en tenant compte que la ligne 1 contient les en-têtes "
        "et que les données commencent ligne 2. Après l'appel, résume brièvement le contenu du fichier."
    ),
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Nom du fichier, ex. « budget-2026.xlsx »."},
            "sheets": {
                "type": "array",
                "description": "Les feuilles du classeur.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nom de la feuille (31 caractères max)."},
                        "columns": {"type": "array", "items": {"type": "string"}, "description": "En-têtes."},
                        "rows": {
                            "type": "array",
                            "description": "Lignes de données ; chaque ligne a une valeur par colonne.",
                            "items": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}},
                        },
                        "number_formats": {
                            "type": "object",
                            "description": "Format Excel par en-tête de colonne, ex. {\"Prix\": \"#,##0.00 €\", \"Taux\": \"0%\"}.",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["name", "columns", "rows"],
                },
            },
        },
        "required": ["filename", "sheets"],
    },
}

CREATE_WORD_TOOL = {
    "name": "create_word",
    "description": (
        "Crée un document Word (.docx) modifiable et téléchargeable, avec de vrais styles Word (titres, "
        "listes à puces et numérotées, tableaux, gras, italique, code, numéros de page). Utilise cet "
        "outil quand l'utilisateur demande un fichier Word, un .docx, ou un document qu'il veut "
        "modifier ensuite (lettre, CV, contrat, rapport, compte rendu). Pour un document à seulement "
        "lire ou imprimer, préfère create_pdf, sauf si l'utilisateur demande Word. Écris le contenu "
        "complet en Markdown ; n'inclus pas le titre principal, il est ajouté automatiquement. Après "
        "l'appel, résume en une ou deux phrases le contenu ; ne le recopie pas dans ta réponse."
    ),
    "eager_input_streaming": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Nom du fichier, ex. « lettre-motivation.docx »."},
            "title": {"type": "string", "description": "Titre affiché en haut du document."},
            "content": {"type": "string", "description": "Contenu complet du document, en Markdown."},
        },
        "required": ["filename", "title", "content"],
    },
}

TOOLS = [CREATE_PDF_TOOL, CREATE_WORD_TOOL, CREATE_EXCEL_TOOL]

SYSTEM_PROMPT = (
    "Tu es cloclo, un assistant qui peut aussi créer des fichiers à télécharger : des PDF (outil "
    "create_pdf), des documents Word modifiables (outil create_word) et des classeurs Excel (outil "
    "create_excel). Quand l'utilisateur demande un document "
    "ou un tableau à télécharger, crée-le avec l'outil adapté au lieu d'écrire seulement le contenu "
    "dans la conversation. Le lien de téléchargement s'affiche automatiquement sous ta réponse. "
    "Tu ne peux pas créer d'images ni d'autres types de fichiers ; si on te le demande, dis-le simplement. "
    "Dans l'historique de la conversation, la mention « [Fichier créé : nom] » à la fin de tes réponses "
    "précédentes indique un fichier que tu as réellement créé et que l'utilisateur a déjà reçu : ne le "
    "recrée pas, sauf s'il demande une nouvelle version."
)


# ---------------------------------------------------------------- validation des entrées
MAX_PDF_CHARS = 200_000
MAX_ROWS = 10_000
MAX_COLS = 200
MAX_SHEETS = 20

Cell = str | int | float | bool | None


class PdfInput(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=MAX_PDF_CHARS)


class SheetInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    columns: list[str] = Field(min_length=1, max_length=MAX_COLS)
    rows: list[list[Cell]] = Field(default_factory=list, max_length=MAX_ROWS)
    number_formats: dict[str, str] = Field(default_factory=dict)

    @field_validator("rows")
    @classmethod
    def rows_fit_columns(cls, rows, info):
        ncols = len(info.data.get("columns") or [])
        for i, row in enumerate(rows, start=2):
            if len(row) > ncols:
                raise ValueError(f"la ligne {i} a {len(row)} valeurs pour {ncols} colonnes")
        return rows


class ExcelInput(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    sheets: list[SheetInput] = Field(min_length=1, max_length=MAX_SHEETS)


def safe_filename(name: str, ext: str) -> str:
    """Nom de fichier sûr (pas de chemin, pas de caractères spéciaux), avec la bonne extension."""
    stem = Path(name.replace("\\", "/")).name
    if stem.lower().endswith(ext):
        stem = stem[: -len(ext)]
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:80] or "document"
    return stem + ext


# ---------------------------------------------------------------- PDF
PDF_CSS = """
@page { size: a4 portrait; margin: 2cm 1.8cm 2.2cm 1.8cm;
  @frame footer { -pdf-frame-content: footer; bottom: 0.9cm; margin-left: 1.8cm; margin-right: 1.8cm; height: 0.8cm; } }
body { font-family: Helvetica; font-size: 10.5pt; line-height: 1.45; color: #0B1020; }
h1.doc-title { font-size: 21pt; color: #5B21B6; margin: 0 0 4pt 0; padding-bottom: 6pt; border-bottom: 2pt solid #0891B2; }
p.doc-date { color: #5B6480; font-size: 9pt; margin: 0 0 14pt 0; }
h1 { font-size: 16pt; color: #0B1020; margin: 14pt 0 6pt 0; }
h2 { font-size: 13.5pt; color: #5B21B6; margin: 12pt 0 5pt 0; }
h3 { font-size: 11.5pt; color: #0891B2; margin: 10pt 0 4pt 0; }
h1, h2, h3, h4 { -pdf-keep-with-next: true; }
p { margin: 0 0 7pt 0; }
ul, ol { margin: 0 0 7pt 0; }
li { margin-bottom: 2pt; }
code { font-family: Courier; font-size: 9.5pt; background-color: #F1F4FB; }
pre { font-family: Courier; font-size: 9pt; background-color: #F1F4FB; border: 0.5pt solid #D5DAE4; padding: 6pt; }
table { width: 100%; margin: 4pt 0 10pt 0; }
th { background-color: #5B21B6; color: #FFFFFF; font-weight: bold; padding: 4pt; border: 0.5pt solid #5B21B6; text-align: left; }
td { padding: 4pt; border: 0.5pt solid #D5DAE4; vertical-align: top; }
blockquote { color: #5B6480; border-left: 2pt solid #0891B2; padding-left: 8pt; margin-left: 0; }
hr { color: #D5DAE4; }
#footer { font-size: 8pt; color: #8A93AE; text-align: right; }
"""


def build_pdf(data: PdfInput) -> bytes:
    body = markdown.markdown(data.content, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    today = date.today().strftime("%d/%m/%Y")
    html = f"""<html><head><meta charset="utf-8"><style>{PDF_CSS}</style></head><body>
<div id="footer">{escape(data.title)} · page <pdf:pagenumber> / <pdf:pagecount></div>
<h1 class="doc-title">{escape(data.title)}</h1><p class="doc-date">{today}</p>
{body}</body></html>"""
    out = io.BytesIO()
    result = pisa.CreatePDF(html, dest=out, encoding="utf-8")
    if result.err:
        raise ValueError("la mise en page du PDF a échoué")
    return out.getvalue()


# ---------------------------------------------------------------- Word
VIOLET, CYAN, INK, MUTED = "5B21B6", "0891B2", "0B1020", "5B6480"
INLINE_TAGS = {"strong", "b", "em", "i", "code", "a", "span", "br", "u", "del", "s"}


def _set_cell_shading(cell, hex_color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def _add_field(run, instr: str) -> None:
    """Champ Word (ex. PAGE, NUMPAGES) : le numéro est calculé par Word à l'ouverture."""
    for tag, text in (("begin", None), (None, instr), ("separate", None), (None, "1"), ("end", None)):
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText" if text == instr else "w:t")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)


def _restart_numbering(doc, paragraph) -> None:
    """Une nouvelle liste numérotée repart à 1 (sinon Word continue la liste précédente)."""
    numbering = doc.part.numbering_part.numbering_definitions._numbering
    style_num = doc.styles["List Number"]._element.pPr.numPr.numId.val
    abstract = numbering.num_having_numId(style_num).abstractNumId.val
    num = numbering.add_num(abstract)
    override = num.add_lvlOverride(ilvl=0)
    override.add_startOverride(1)
    num_pr = paragraph._p.get_or_add_pPr().get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = 0
    num_pr.get_or_add_numId().val = num.numId


def _runs(paragraph, node, bold=False, italic=False, code=False, strike=False) -> None:
    """Ajoute le texte d'un nœud HTML (et de ses enfants en ligne) au paragraphe Word."""
    def put(text, b=bold, i=italic, c=code, st=strike):
        # Comme en HTML, un retour à la ligne dans le source n'est qu'un espace
        text = re.sub(r"\s*\n\s*", " ", text or "")
        if not text.strip() and not paragraph.runs:
            return
        if not text:
            return
        run = paragraph.add_run(text)
        run.bold, run.italic = b or None, i or None
        if st:
            run.font.strike = True
        if c:
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
            run.font.color.rgb = RGBColor.from_string(VIOLET)

    put(node.text)
    for child in node:
        tag = child.tag if isinstance(child.tag, str) else ""
        if tag == "br":
            paragraph.add_run().add_break()
            put((child.tail or "").lstrip())
            continue
        elif tag in ("ul", "ol", "table", "pre"):
            pass  # blocs imbriqués : traités par _blocks
        else:
            _runs(paragraph, child,
                  bold or tag in ("strong", "b"), italic or tag in ("em", "i"),
                  code or tag == "code", strike or tag in ("del", "s"))
        put(child.tail)


def _trim(paragraph) -> None:
    """Retire les espaces en fin de paragraphe (laissés par la mise en page du Markdown)."""
    for run in reversed(paragraph.runs):
        stripped = run.text.rstrip()
        if stripped:
            run.text = stripped
            break
        run._r.getparent().remove(run._r)


def _list(doc, node, level: int = 0) -> None:
    ordered = node.tag == "ol"
    style = ("List Number" if ordered else "List Bullet") + (f" {level + 1}" if level else "")
    first = True
    for li in node.findall("li"):
        para = doc.add_paragraph(style=style)
        # le texte du <li> (les <p> internes des listes « aérées » comptent comme du texte)
        holder = li
        if len(li) and li[0].tag == "p" and not (li.text or "").strip():
            holder = li[0]
        _runs(para, holder)
        _trim(para)
        if ordered and first and level == 0:
            _restart_numbering(doc, para)
        first = False
        for sub in li:
            if sub.tag in ("ul", "ol"):
                _list(doc, sub, min(level + 1, 2))


def _table(doc, node) -> None:
    rows = node.findall(".//tr")
    if not rows:
        return
    ncols = max(len(r.findall("th") + r.findall("td")) for r in rows)
    table = doc.add_table(rows=0, cols=ncols)
    table.style = "Table Grid"
    for r_idx, tr in enumerate(rows):
        cells = tr.findall("th") + tr.findall("td")
        row = table.add_row().cells
        header = r_idx == 0 and tr.findall("th")
        for c_idx in range(ncols):
            cell = row[c_idx]
            para = cell.paragraphs[0]
            if c_idx < len(cells):
                _runs(para, cells[c_idx], bold=bool(header))
            if header:
                _set_cell_shading(cell, VIOLET)
                for run in para.runs:
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    doc.add_paragraph()


def _blocks(doc, root) -> None:
    for node in root:
        tag = node.tag if isinstance(node.tag, str) else ""
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            _runs(doc.add_heading(level=min(int(tag[1]), 4)), node)
        elif tag == "p":
            para = doc.add_paragraph()
            _runs(para, node)
            _trim(para)
        elif tag in ("ul", "ol"):
            _list(doc, node)
        elif tag == "table":
            _table(doc, node)
        elif tag == "pre":
            para = doc.add_paragraph(style="No Spacing")
            run = para.add_run(node.text_content().rstrip("\n"))
            run.font.name, run.font.size = "Consolas", Pt(9.5)
            para.paragraph_format.left_indent = Cm(0.5)
            doc.add_paragraph()
        elif tag == "blockquote":
            para = doc.add_paragraph(style="Quote")
            _runs(para, node[0] if len(node) and node[0].tag == "p" else node)
        elif tag == "hr":
            doc.add_paragraph("─" * 30).alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif tag:
            _runs(doc.add_paragraph(), node)


def build_word(data: "PdfInput") -> bytes:
    doc = Document()
    section = doc.sections[0]
    section.page_height, section.page_width = Cm(29.7), Cm(21)
    section.left_margin = section.right_margin = Cm(2.2)
    section.top_margin, section.bottom_margin = Cm(2), Cm(2)

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    normal.font.color.rgb = RGBColor.from_string(INK)
    for level, color in ((1, INK), (2, VIOLET), (3, CYAN), (4, CYAN)):
        style = doc.styles[f"Heading {level}"]
        style.font.color.rgb = RGBColor.from_string(color)
        style.font.name = "Calibri"

    title = doc.add_heading(data.title, level=0)
    for run in title.runs:
        run.font.color.rgb = RGBColor.from_string(VIOLET)
    date_para = doc.add_paragraph(date.today().strftime("%d/%m/%Y"))
    date_para.runs[0].font.color.rgb = RGBColor.from_string(MUTED)
    date_para.runs[0].font.size = Pt(9)

    # nl2br : une ligne du Markdown reste une ligne (adresses, signatures), comme dans le PDF
    html = markdown.markdown(data.content, extensions=["tables", "fenced_code", "sane_lists", "nl2br"])
    root = lxml_html.fragment_fromstring(html, create_parent="div")
    _blocks(doc, root)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    lead = footer.add_run(f"{data.title} · page ")
    _add_field(footer.add_run(), "PAGE")
    footer.add_run(" / ")
    _add_field(footer.add_run(), "NUMPAGES")
    for run in footer.runs:
        run.font.size, run.font.color.rgb = Pt(8), RGBColor.from_string(MUTED)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ---------------------------------------------------------------- Excel
HEADER_FILL = PatternFill("solid", fgColor="5B21B6")
HEADER_FONT = Font(bold=True, color="FFFFFF")
THIN = Side(style="thin", color="D5DAE4")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _sheet_title(name: str, used: set[str]) -> str:
    title = re.sub(r"[\[\]:*?/\\]", "-", name).strip("'") or "Feuille"
    title = title[:31]
    base, n = title, 2
    while title.lower() in used:
        suffix = f" ({n})"
        title = base[: 31 - len(suffix)] + suffix
        n += 1
    used.add(title.lower())
    return title


def _cell_value(value: Cell):
    """Nombres et formules restent tels quels ; les dates AAAA-MM-JJ deviennent de vraies dates."""
    if isinstance(value, str):
        if ISO_DATE.match(value):
            try:
                return date.fromisoformat(value)
            except ValueError:
                return value
        return value[:32_767]
    return value


def build_excel(data: ExcelInput) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for sheet in data.sheets:
        ws = wb.create_sheet(_sheet_title(sheet.name, used))
        ws.append(sheet.columns)
        for row in sheet.rows:
            ws.append([_cell_value(v) for v in row])

        ncols = len(sheet.columns)
        for col in range(1, ncols + 1):
            head = ws.cell(row=1, column=col)
            head.fill, head.font = HEADER_FILL, HEADER_FONT
            head.alignment = Alignment(vertical="center", wrap_text=True)
            head.border = Border(bottom=THIN)
        ws.row_dimensions[1].height = 22
        ws.freeze_panes = "A2"
        if sheet.rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{len(sheet.rows) + 1}"

        formats = {k.strip().lower(): v for k, v in sheet.number_formats.items()}
        for col, header in enumerate(sheet.columns, start=1):
            letter = get_column_letter(col)
            fmt = formats.get(header.strip().lower())
            width = len(str(header))
            for cell in ws[letter][1:]:
                if isinstance(cell.value, date):
                    cell.number_format = fmt or "DD/MM/YYYY"
                elif fmt:
                    cell.number_format = fmt
                shown = 12 if isinstance(cell.value, str) and cell.value.startswith("=") else len(str(cell.value or ""))
                width = max(width, shown)
            ws.column_dimensions[letter].width = min(max(width + 2, 8), 60)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ---------------------------------------------------------------- stockage temporaire
FILE_TTL = 3600  # une heure
FILES_DIR = Path(tempfile.gettempdir()) / "cloclo-files"
MIME = {".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _cleanup() -> None:
    if not FILES_DIR.exists():
        return
    now = time.time()
    for path in FILES_DIR.iterdir():
        try:
            if now - path.stat().st_mtime > FILE_TTL:
                path.unlink()
        except OSError:
            pass


def store(content: bytes, filename: str) -> str:
    """Enregistre le fichier et renvoie un identifiant impossible à deviner."""
    _cleanup()
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    fid = secrets.token_urlsafe(18)
    (FILES_DIR / f"{fid}__{filename}").write_bytes(content)
    return fid


def load(fid: str) -> tuple[Path, str] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", fid or ""):
        return None
    matches = list(FILES_DIR.glob(f"{fid}__*")) if FILES_DIR.exists() else []
    if not matches or time.time() - matches[0].stat().st_mtime > FILE_TTL:
        return None
    path = matches[0]
    return path, path.name.split("__", 1)[1]


# ---------------------------------------------------------------- exécution d'un outil
def run_tool(name: str, raw_input) -> tuple[dict | None, str, bool]:
    """Exécute un outil. Renvoie (fichier ou None, texte du résultat pour Claude, erreur ?)."""
    try:
        if name == "create_pdf":
            data = PdfInput.model_validate(raw_input)
            filename, content, kind = safe_filename(data.filename, ".pdf"), build_pdf(data), "pdf"
        elif name == "create_word":
            data = PdfInput.model_validate(raw_input)
            filename, content, kind = safe_filename(data.filename, ".docx"), build_word(data), "docx"
        elif name == "create_excel":
            data = ExcelInput.model_validate(raw_input)
            filename, content, kind = safe_filename(data.filename, ".xlsx"), build_excel(data), "xlsx"
        else:
            return None, f"Outil inconnu : {name}", True
    except ValueError as e:  # inclut les erreurs de validation pydantic
        return None, f"Entrée invalide, corrige-la et réessaie : {str(e)[:1500]}", True

    fid = store(content, filename)
    file = {"id": fid, "name": filename, "kind": kind, "size": len(content)}
    size = f"{max(1, len(content) // 1000)} Ko"
    return file, f"Fichier créé : {filename} ({size}). L’utilisateur voit le lien de téléchargement.", False
