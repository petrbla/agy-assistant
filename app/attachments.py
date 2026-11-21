from __future__ import annotations

import logging
import re
import secrets
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.i18n import t

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})
_TEXT_EXTS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".log",
        ".ini",
        ".cfg",
        ".conf",
        ".sql",
        ".py",
        ".js",
        ".ts",
        ".html",
        ".htm",
        ".css",
        ".sh",
    }
)


def is_image_filename(filename: str, mime_type: str = "") -> bool:
    """Rozpozná obrázek podle přípony nebo MIME typu."""
    if mime_type.lower().startswith("image/"):
        return True
    return Path(filename).suffix.lower() in _IMAGE_EXTS


def sanitize_filename(raw_name: str, default_ext: str = "") -> str:
    """Vyčistí název souboru pro bezpečné uložení do `workspace/inbox/`."""
    base = Path(raw_name or "priloha").name.strip()
    p = Path(base)
    ext = p.suffix.lower()
    stem = _SAFE_NAME_RE.sub("_", p.stem).strip("._") or "soubor"
    ext_clean = _SAFE_NAME_RE.sub("", ext) if ext else default_ext
    if ext_clean and not ext_clean.startswith("."):
        ext_clean = f".{ext_clean}"
    return f"{stem[:70]}{ext_clean[:16]}"


def build_inbox_path(
    workspace_dir: Path, raw_name: str, default_ext: str = ""
) -> tuple[Path, str]:
    """Vytvoří unikátní cílovou cestu ve složce `workspace/inbox/`."""
    inbox_dir = workspace_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    rand = secrets.token_hex(2)
    safe_name = sanitize_filename(raw_name, default_ext=default_ext)
    filename = f"tg_{ts}_{rand}_{safe_name}"
    return inbox_dir / filename, f"inbox/{filename}"


def _read_text_preview(file_path: Path, max_chars: int) -> str | None:
    raw = file_path.read_bytes()
    for enc in ("utf-8", "cp1250", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    cleaned = text.strip()
    if not cleaned:
        return "(Prázdný textový soubor)"
    if len(cleaned) > max_chars:
        return cleaned[:max_chars]
    return cleaned


def _extract_docx_text(file_path: Path) -> str | None:
    w_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(file_path) as zf:
        if "word/document.xml" not in zf.namelist():
            return None
        root = ET.fromstring(zf.read("word/document.xml"))

    body = root.find(f"{w_ns}body")
    if body is None:
        return None

    lines: list[str] = []

    def _para_text(p_elem: ET.Element) -> str:
        parts: list[str] = []
        for elem in p_elem.iter():
            if elem.tag == f"{w_ns}t" and elem.text:
                parts.append(elem.text)
            elif elem.tag == f"{w_ns}tab":
                parts.append("\t")
            elif elem.tag in (f"{w_ns}br", f"{w_ns}cr"):
                parts.append("\n")
        return "".join(parts).strip()

    for child in body:
        if child.tag == f"{w_ns}p":
            txt = _para_text(child)
            if txt:
                lines.append(txt)
        elif child.tag == f"{w_ns}tbl":
            for tr in child.findall(f"{w_ns}tr"):
                row_cells: list[str] = []
                for tc in tr.findall(f"{w_ns}tc"):
                    cell_paras = [_para_text(p) for p in tc.findall(f"{w_ns}p")]
                    cell_txt = " ".join(c for c in cell_paras if c)
                    row_cells.append(cell_txt)
                if any(row_cells):
                    lines.append(" | ".join(row_cells))

    return "\n".join(lines).strip() or "(Dokument Word neobsahuje čitelný text)"


def _col_index_from_ref(ref: str) -> int:
    col = 0
    for ch in ref:
        if "A" <= ch.upper() <= "Z":
            col = (col * 26) + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return max(0, col - 1)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    strings: list[str] = []
    for si in root.findall(f"{ns}si"):
        texts = [t.text or "" for t in si.iter(f"{ns}t")]
        strings.append("".join(texts))
    return strings


def _xlsx_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    names = set(zf.namelist())
    if "xl/workbook.xml" in names:
        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        ns_rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
        rid_map: dict[str, str] = {}
        if "xl/_rels/workbook.xml.rels" in names:
            rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels_root:
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "").lstrip("/")
                if target and not target.startswith("xl/"):
                    target = f"xl/{target}"
                if rid and target in names:
                    rid_map[rid] = target
        sheets: list[tuple[str, str]] = []
        for idx, sh in enumerate(wb_root.iter(f"{ns_main}sheet"), start=1):
            sh_name = sh.attrib.get("name", f"List {idx}")
            rid = sh.attrib.get(f"{ns_rel}id", "")
            target = rid_map.get(rid, f"xl/worksheets/sheet{idx}.xml")
            if target in names:
                sheets.append((sh_name, target))
        if sheets:
            return sheets

    fallback = sorted(n for n in names if re.match(r"^xl/worksheets/sheet\d+\.xml$", n))
    return [(Path(p).stem, p) for p in fallback]


def _extract_xlsx_text(file_path: Path, max_rows_per_sheet: int = 200) -> str | None:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(file_path) as zf:
        shared = _xlsx_shared_strings(zf)
        sheets = _xlsx_sheets(zf)
        if not sheets:
            return None

        out: list[str] = []
        for sheet_name, sheet_path in sheets:
            root = ET.fromstring(zf.read(sheet_path))
            sheet_rows: list[str] = []
            truncated = False
            for row in root.iter(f"{ns}row"):
                cells_by_col: dict[int, str] = {}
                max_col = -1
                for c in row.findall(f"{ns}c"):
                    ref = c.attrib.get("r", "")
                    col_idx = _col_index_from_ref(ref) if ref else (max_col + 1)
                    max_col = max(max_col, col_idx)
                    c_type = c.attrib.get("t", "")
                    val = ""
                    if c_type == "s":
                        v_el = c.find(f"{ns}v")
                        if v_el is not None and v_el.text and v_el.text.isdigit():
                            s_idx = int(v_el.text)
                            if 0 <= s_idx < len(shared):
                                val = shared[s_idx]
                    elif c_type == "inlineStr":
                        is_el = c.find(f"{ns}is")
                        if is_el is not None:
                            val = "".join(t.text or "" for t in is_el.iter(f"{ns}t"))
                    elif c_type == "b":
                        v_el = c.find(f"{ns}v")
                        val = "TRUE" if (v_el is not None and v_el.text == "1") else "FALSE"
                    else:
                        v_el = c.find(f"{ns}v")
                        if v_el is not None and v_el.text is not None:
                            val = v_el.text
                    cells_by_col[col_idx] = val.replace("\n", " ").strip()

                if max_col >= 0:
                    row_vals = [cells_by_col.get(i, "") for i in range(max_col + 1)]
                    if any(row_vals):
                        if len(sheet_rows) >= max_rows_per_sheet:
                            truncated = True
                            break
                        sheet_rows.append(" | ".join(row_vals))

            out.append(f"--- List: {sheet_name} ---")
            if sheet_rows:
                out.extend(sheet_rows)
                if truncated:
                    out.append(f"... (zobrazeno prvních {max_rows_per_sheet} řádků)")
            else:
                out.append("(prázdný list)")
        return "\n".join(out)


def _extract_odf_text(file_path: Path) -> str | None:
    text_ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    table_ns = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    with zipfile.ZipFile(file_path) as zf:
        if "content.xml" not in zf.namelist():
            return None
        root = ET.fromstring(zf.read("content.xml"))

    lines: list[str] = []
    for table in root.iter(f"{table_ns}table"):
        t_name = table.attrib.get(f"{table_ns}name", "")
        if t_name:
            lines.append(f"--- Tabulka: {t_name} ---")
        for row in table.findall(f"{table_ns}table-row"):
            cells: list[str] = []
            for cell in row.findall(f"{table_ns}table-cell"):
                cell_txt = " ".join(
                    "".join(p.itertext()).strip() for p in cell.findall(f"{text_ns}p")
                ).strip()
                cells.append(cell_txt)
            if any(cells):
                lines.append(" | ".join(cells))
    if lines:
        return "\n".join(lines)

    paras = ["".join(el.itertext()).strip() for el in root.iter() if el.tag in (f"{text_ns}h", f"{text_ns}p")]
    non_empty = [p for p in paras if p]
    return "\n".join(non_empty) if non_empty else None


def _extract_pptx_text(file_path: Path) -> str | None:
    a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    with zipfile.ZipFile(file_path) as zf:
        slides = sorted(
            (n for n in zf.namelist() if re.match(r"^ppt/slides/slide\d+\.xml$", n)),
            key=lambda s: int(re.search(r"(\d+)", s).group(1)),  # type: ignore[union-attr]
        )
        if not slides:
            return None
        out: list[str] = []
        for idx, slide_path in enumerate(slides, start=1):
            root = ET.fromstring(zf.read(slide_path))
            texts = [t.text.strip() for t in root.iter(f"{a_ns}t") if t.text and t.text.strip()]
            out.append(f"--- Snímek {idx} ---")
            out.extend(texts or ["(bez textu)"])
        return "\n".join(out)


def _extract_pdf_text(file_path: Path) -> str | None:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return None
    try:
        res = subprocess.run(
            [pdftotext, "-layout", str(file_path), "-"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0:
            txt = res.stdout.decode("utf-8", errors="replace").strip()
            return txt or None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def extract_attachment_text(file_path: Path, *, max_chars: int = 12000) -> tuple[str | None, bool]:
    """Extrahuje textovou reprezentaci běžných souborů (Excel, Word, CSV, TXT, PPTX, ODF, PDF).

    Vrací `(extrahovaný_text, byl_zkrácen)`. Pro obrázky či nepodporované binární formáty vrací `(None, False)`.
    """
    ext = file_path.suffix.lower()
    raw_text: str | None = None
    try:
        if ext in (".xlsx", ".xlsm"):
            raw_text = _extract_xlsx_text(file_path)
        elif ext == ".docx":
            raw_text = _extract_docx_text(file_path)
        elif ext in (".odt", ".ods"):
            raw_text = _extract_odf_text(file_path)
        elif ext == ".pptx":
            raw_text = _extract_pptx_text(file_path)
        elif ext == ".pdf":
            raw_text = _extract_pdf_text(file_path)
        elif ext in _TEXT_EXTS:
            raw_text = _read_text_preview(file_path, max_chars + 1)
    except Exception as exc:
        logger.warning("Nepodařilo se vyčíst text z přílohy %s: %s", file_path.name, exc)
        return None, False

    if raw_text is None:
        return None, False
    if len(raw_text) > max_chars:
        return raw_text[:max_chars], True
    return raw_text, False


def build_attachment_prompt(
    *,
    rel_path: str,
    kind: str,
    caption: str,
    mime_type: str = "",
    size_bytes: int = 0,
    extracted_text: str | None = None,
    truncated: bool = False,
    lang: str = "en",
) -> str:
    """Sestaví prompt pro `agy` obsahující cestu k příloze a případně její extrahovaný obsah."""
    meta_parts = [t("prompts.att_file", lang, rel_path=rel_path)]
    if size_bytes > 0:
        meta_parts.append(t("prompts.att_size", lang, size_bytes=size_bytes))
    if mime_type:
        meta_parts.append(t("prompts.att_type", lang, mime_type=mime_type))
    header = t("prompts.att_header", lang, kind=kind, meta=", ".join(meta_parts))

    is_image = kind in ("image", "obrázek")
    is_voice = kind in ("voice message", "audio", "hlasová zpráva")

    lines = [header]
    if is_image:
        lines.append(t("prompts.att_image_instruction", lang, rel_path=rel_path))
    elif extracted_text is not None:
        lines.append(t("prompts.att_extracted_start", lang, rel_path=rel_path))
        lines.append(extracted_text)
        if truncated:
            lines.append(t("prompts.att_extracted_truncated", lang, rel_path=rel_path))
        lines.append(t("prompts.att_extracted_end", lang))
    else:
        lines.append(t("prompts.att_disk_instruction", lang, rel_path=rel_path))

    clean_caption = caption.strip()
    if clean_caption:
        lines.append(t("prompts.att_user_message", lang, caption=clean_caption))
    elif is_image:
        lines.append(t("prompts.att_default_image_task", lang))
    elif is_voice:
        lines.append(t("prompts.att_default_voice_task", lang))
    else:
        lines.append(t("prompts.att_default_file_task", lang))

    return "\n".join(lines)

