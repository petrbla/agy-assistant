from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agy_runner import AgyResult
from app.attachments import (
    build_attachment_prompt,
    build_inbox_path,
    extract_attachment_text,
    sanitize_filename,
)
from app.config import Settings
from app.db import AssistantDB
from app.memory import WorkspaceMemory
from app.telegram_bot import TelegramAssistantBot


def _make_sample_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Faktury" sheetId="1" r:id="rId1"/></sheets>'
            "</workbook>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Položka</t></si>"
            "<si><t>Částka</t></si>"
            "<si><t>Káva</t></si>"
            "</sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>490</v></c></row>'
            "</sheetData>"
            "</worksheet>",
        )


def _make_sample_docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>Smlouva o dílo</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cena</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>15000 Kč</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body>"
            "</w:document>",
        )


def test_sanitize_and_extract_excel_word_and_csv(tmp_path: Path) -> None:
    assert sanitize_filename("../../tajny/rozpočet 2026.xlsx") == "rozpo_et_2026.xlsx"
    dest, rel = build_inbox_path(tmp_path, "../../faktura.xlsx")
    assert dest.parent == tmp_path / "inbox"
    assert rel.startswith("inbox/tg_") and rel.endswith("faktura.xlsx")

    xlsx_path = tmp_path / "tabulka.xlsx"
    _make_sample_xlsx(xlsx_path)
    xlsx_txt, truncated = extract_attachment_text(xlsx_path)
    assert truncated is False
    assert xlsx_txt is not None
    assert "--- List: Faktury ---" in xlsx_txt
    assert "Položka | Částka" in xlsx_txt
    assert "Káva | 490" in xlsx_txt

    docx_path = tmp_path / "smlouva.docx"
    _make_sample_docx(docx_path)
    docx_txt, truncated_doc = extract_attachment_text(docx_path)
    assert truncated_doc is False
    assert docx_txt is not None
    assert "Smlouva o dílo" in docx_txt
    assert "Cena | 15000 Kč" in docx_txt

    csv_path = tmp_path / "export.csv"
    csv_path.write_bytes("Jméno;Poznámka\nPříliš žluťoučký;OK\n".encode("cp1250"))
    csv_txt, _ = extract_attachment_text(csv_path)
    assert csv_txt is not None and "Příliš žluťoučký" in csv_txt

    img_path = tmp_path / "foto.jpg"
    img_path.write_bytes(b"\xff\xd8\xff")
    img_txt, _ = extract_attachment_text(img_path)
    assert img_txt is None


async def test_telegram_bot_handles_photo_and_excel_attachment(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        telegram_bot_token="123456:ABCDEF_valid_looking_token",
        telegram_allowed_users="999",
    )
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    mem = WorkspaceMemory(tmp_path)
    runner = AsyncMock()
    runner.run = AsyncMock(
        return_value=AgyResult(text="**Hotovo**, přečteno.", conversation_id="c1", duration_ms=10, success=True)
    )
    bot = TelegramAssistantBot(settings, db, mem, runner)

    try:
        bot.bot.send_message = AsyncMock()
        bot.bot.send_chat_action = AsyncMock()

        # 1) Fotka z Telegramu
        async def _fake_dl_photo(file_id, destination):
            Path(destination).write_bytes(b"JPEGDATA")

        bot.bot.download = AsyncMock(side_effect=_fake_dl_photo)
        photo_msg = SimpleNamespace(
            chat=SimpleNamespace(id=999),
            from_user=SimpleNamespace(id=999),
            photo=[SimpleNamespace(file_id="ph_1", file_size=8)],
            document=None,
            voice=None,
            audio=None,
            caption="Co je na té účtence?",
            answer=AsyncMock(),
        )
        await bot._download_and_handle_attachment(photo_msg)
        photo_prompt = runner.run.await_args.args[0]
        assert "Telegram attachment (image)" in photo_prompt
        assert "inbox/tg_" in photo_prompt
        assert "Co je na té účtence?" in photo_prompt
        assert len(list((tmp_path / "inbox").glob("*.jpg"))) == 1

        # 2) Excelový soubor přes F.document
        sample_xlsx = tmp_path / "src.xlsx"
        _make_sample_xlsx(sample_xlsx)
        xlsx_bytes = sample_xlsx.read_bytes()

        async def _fake_dl_xlsx(file_id, destination):
            Path(destination).write_bytes(xlsx_bytes)

        bot.bot.download = AsyncMock(side_effect=_fake_dl_xlsx)
        doc_msg = SimpleNamespace(
            chat=SimpleNamespace(id=999),
            from_user=SimpleNamespace(id=999),
            photo=None,
            document=SimpleNamespace(
                file_id="doc_1",
                file_name="faktura_leden.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                file_size=len(xlsx_bytes),
            ),
            voice=None,
            audio=None,
            caption="Sečti částku",
            answer=AsyncMock(),
        )
        await bot._download_and_handle_attachment(doc_msg)
        doc_prompt = runner.run.await_args.args[0]
        assert "faktura_leden.xlsx" in doc_prompt
        assert "Káva | 490" in doc_prompt
        assert "Sečti částku" in doc_prompt

        prompt_en = build_attachment_prompt(
            rel_path="inbox/test.pdf", kind="document", caption="", lang="en"
        )
        assert "Telegram attachment (document)" in prompt_en
        prompt_cs = build_attachment_prompt(
            rel_path="inbox/test.pdf", kind="dokument", caption="", lang="cs"
        )
        assert "Příloha z Telegramu (dokument)" in prompt_cs
    finally:
        await bot.bot.session.close()

