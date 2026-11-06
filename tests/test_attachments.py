"""Fichiers joints : conversion en blocs pour l'API, limites, erreurs."""

import base64
import io

import docx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main
from conftest import read_events

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def att(name: str, raw: bytes, media_type: str = "") -> dict:
    return {"name": name, "media_type": media_type, "data": b64(raw)}


def make_docx(*paragraphs: str, table=None) -> bytes:
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for i, row in enumerate(table):
            for j, cell in enumerate(row):
                t.cell(i, j).text = cell
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def block(name, raw, media_type=""):
    return main.attachment_block(main.Attachment(**att(name, raw, media_type)))[0]


# ------------------------------------------------------------ unitaires : conversion
def test_pdf_becomes_document_block():
    b = block("rapport.pdf", PDF, "application/pdf")
    assert b["type"] == "document" and b["title"] == "rapport.pdf"
    assert b["source"] == {"type": "base64", "media_type": "application/pdf", "data": b64(PDF)}


@pytest.mark.parametrize("name,mime", [("a.png", "image/png"), ("a.jpg", "image/jpeg"),
                                        ("a.jpeg", "image/jpeg"), ("a.gif", "image/gif"),
                                        ("a.webp", "image/webp")])
def test_images_become_image_blocks(name, mime):
    b = block(name, PNG)
    assert b == {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64(PNG)}}


@pytest.mark.parametrize("name", ["notes.txt", "README.md", "data.csv", "x.json", "script.py", "NOTES.TXT"])
def test_text_files_become_text_documents(name):
    b = block(name, "Bonjour, ça va ?".encode())
    assert b["type"] == "document" and b["title"] == name
    assert b["source"] == {"type": "text", "media_type": "text/plain", "data": "Bonjour, ça va ?"}


def test_unknown_extension_with_text_mime_is_text():
    assert block("fichier.conf2", b"a=1", "text/plain")["source"]["data"] == "a=1"


def test_utf8_bom_is_removed():
    assert block("a.txt", "﻿Salut".encode("utf-8"))["source"]["data"] == "Salut"


def test_docx_text_and_tables_are_extracted():
    raw = make_docx("Titre", "Deuxième paragraphe", table=[["Nom", "Âge"], ["Ana", "31"]])
    b = block("cv.docx", raw)
    text = b["source"]["data"]
    assert b["type"] == "document" and b["source"]["type"] == "text"
    assert "Titre" in text and "Deuxième paragraphe" in text and "Ana | 31" in text


@pytest.mark.parametrize("name,raw,needle", [
    ("x.exe", b"MZ\x90\x00", "non pris en charge"),
    ("x.zip", b"PK\x03\x04", "non pris en charge"),
    ("faux.pdf", b"pas un pdf", "PDF valide"),
    ("vide.txt", b"", "vide"),
    ("latin1.txt", "é".encode("latin-1"), "UTF-8"),
    ("casse.docx", b"PK pas un docx", "Word illisible"),
    ("vide.docx", None, "ne contient pas de texte"),
])
def test_bad_files_are_rejected_with_reason(name, raw, needle):
    if raw is None:
        raw = make_docx()
    with pytest.raises(HTTPException) as e:
        main.attachment_block(main.Attachment(name=name, data=b64(raw)))
    assert e.value.status_code == 422 and needle in e.value.detail and name in e.value.detail


def test_invalid_base64_is_rejected():
    with pytest.raises(HTTPException) as e:
        main.attachment_block(main.Attachment(name="a.txt", data="@@@ pas du base64"))
    assert "base64" in e.value.detail


def test_image_over_api_limit_is_rejected():
    with pytest.raises(HTTPException) as e:
        block("grosse.png", b"\x00" * (main.MAX_IMAGE_BYTES + 1))
    assert "trop lourde" in e.value.detail


# ------------------------------------------------------------ unitaires : schéma
def test_message_with_only_a_file_is_valid():
    main.Message(role="user", content="", attachments=[att("a.txt", b"x")])


def test_empty_message_is_invalid():
    with pytest.raises(ValidationError):
        main.Message(role="user", content="   ")


def test_assistant_cannot_have_files():
    with pytest.raises(ValidationError):
        main.Message(role="assistant", content="ok", attachments=[att("a.txt", b"x")])


def test_too_many_files_is_invalid():
    with pytest.raises(ValidationError):
        main.Message(role="user", content="x", attachments=[att(f"{i}.txt", b"x") for i in range(11)])


def test_text_only_message_keeps_plain_string():
    out = main.to_api_messages([main.Message(role="user", content="hi")])
    assert out == [{"role": "user", "content": "hi"}]


def test_files_come_before_question():
    out = main.to_api_messages([main.Message(role="user", content="Résume", attachments=[att("a.pdf", PDF)])])
    assert [b["type"] for b in out[0]["content"]] == ["document", "text"]
    assert out[0]["content"][1] == {"type": "text", "text": "Résume"}


def test_total_size_limit(monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(HTTPException) as e:
        main.to_api_messages([main.Message(role="user", content="x",
                                           attachments=[att("a.txt", b"123456"), att("b.txt", b"123456")])])
    assert e.value.status_code == 413


def test_total_size_counts_whole_conversation(monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    msgs = [main.Message(role="user", content="1", attachments=[att("a.txt", b"123456")]),
            main.Message(role="assistant", content="ok"),
            main.Message(role="user", content="2", attachments=[att("b.txt", b"123456")])]
    with pytest.raises(HTTPException):
        main.to_api_messages(msgs)


# ------------------------------------------------------------ intégration : via /api/chat
def post(client, messages):
    return client.post("/api/chat", json={"messages": messages})


def test_chat_with_pdf_image_docx_and_text(client, upstream):
    msg = {"role": "user", "content": "Compare ces fichiers", "attachments": [
        att("a.pdf", PDF), att("b.png", PNG), att("c.docx", make_docx("Contenu Word")), att("d.md", b"# Titre")]}
    r = post(client, [msg])
    assert read_events(r)[-1]["type"] == "done"
    content = upstream.last["messages"][0]["content"]
    assert [b["type"] for b in content] == ["document", "image", "document", "document", "text"]
    assert content[2]["source"]["data"] == "Contenu Word"
    assert upstream.last["cache_control"] == {"type": "ephemeral"}


def test_chat_file_is_resent_in_later_turns(client, upstream):
    msgs = [{"role": "user", "content": "Lis", "attachments": [att("a.txt", b"secret 42")]},
            {"role": "assistant", "content": "Lu."},
            {"role": "user", "content": "Quel nombre ?"}]
    post(client, msgs)
    sent = upstream.last["messages"]
    assert sent[0]["content"][0]["source"]["data"] == "secret 42"
    assert sent[2] == {"role": "user", "content": "Quel nombre ?"}


def test_chat_file_without_text(client, upstream):
    r = post(client, [{"role": "user", "content": "", "attachments": [att("a.txt", b"x")]}])
    assert r.status_code == 200
    assert [b["type"] for b in upstream.last["messages"][0]["content"]] == ["document"]


def test_chat_rejects_unsupported_file_before_calling_api(client, upstream):
    r = post(client, [{"role": "user", "content": "x", "attachments": [att("virus.exe", b"MZ")]}])
    assert r.status_code == 422
    assert "virus.exe" in r.json()["detail"]
    assert upstream.requests == []


def test_chat_rejects_too_large_upload(client, upstream, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 5)
    r = post(client, [{"role": "user", "content": "x", "attachments": [att("a.txt", b"0123456789")]}])
    assert r.status_code == 413
    assert upstream.requests == []
