from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo


CORE_MEMORY_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "HEARTBEAT.md",
)
MAX_IMPORT_ZIP_BYTES = 10 * 1024 * 1024
MAX_IMPORT_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_IMPORT_FILES = 500
MEMORY_EXPORT_EXCLUDED_ROOT = frozenset({"schedule_request.json", ".env"})
_ROOT_TEXT_SUFFIXES = frozenset({".md", ".txt"})
_MEMORY_SUFFIXES = frozenset({".md", ".txt", ".json"})
_SKILL_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml"})


@dataclass(frozen=True)
class WorkspaceFileInfo:
    """Metadata souboru ve workspace."""

    name: str
    path: str
    size_bytes: int
    updated_at: str


class WorkspaceMemory:
    """Bezpečná správa Markdown paměti a skillů v adresáři workspace."""

    def __init__(self, workspace_dir: Path, timezone_name: str = "Europe/Prague") -> None:
        self.workspace_dir = workspace_dir.resolve()
        self.tz = ZoneInfo(timezone_name)

    def _resolve_safe(self, relative_path: str) -> Path:
        """Přeloží relativní cestu a ověří, že neopouští workspace."""
        cleaned = relative_path.strip().lstrip("/\\")
        if not cleaned:
            raise ValueError("Cesta k souboru nesmí být prázdná.")
        target = (self.workspace_dir / cleaned).resolve()
        if not target.is_relative_to(self.workspace_dir):
            raise ValueError("Nepovolená cesta mimo workspace.")
        norm_rel = cleaned.replace("\\", "/")
        allowed_suffixes = (
            _SKILL_SUFFIXES if norm_rel.startswith(".agents/skills/") else _MEMORY_SUFFIXES
        )
        if target.suffix.lower() not in allowed_suffixes and cleaned not in CORE_MEMORY_FILES:
            raise ValueError("Povoleny jsou pouze textové/konfigurační soubory (.md, .json, .txt).")
        return target

    def list_files(self) -> list[WorkspaceFileInfo]:
        """Vrátí seznam základních souborů paměti a denních logů."""
        results: list[WorkspaceFileInfo] = []
        candidates: list[Path] = [self.workspace_dir / name for name in CORE_MEMORY_FILES]

        memory_dir = self.workspace_dir / "memory"
        if memory_dir.is_dir():
            candidates.extend(sorted(memory_dir.glob("*.md"), reverse=True)[:14])

        seen: set[Path] = set()
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            stat = resolved.stat()
            rel = resolved.relative_to(self.workspace_dir).as_posix()
            updated = datetime.fromtimestamp(stat.st_mtime, tz=self.tz).isoformat()
            results.append(
                WorkspaceFileInfo(
                    name=resolved.name,
                    path=rel,
                    size_bytes=stat.st_size,
                    updated_at=updated,
                )
            )
        return results

    def list_skills(self) -> list[str]:
        """Vrátí názvy nainstalovaných skillů ve složce .agents/skills."""
        skills_dir = self.workspace_dir / ".agents" / "skills"
        if not skills_dir.is_dir():
            return []
        return sorted(
            item.name
            for item in skills_dir.iterdir()
            if item.is_dir() and (item / "SKILL.md").is_file()
        )

    def read_file(self, relative_path: str) -> str:
        """Přečte obsah souboru z workspace."""
        target = self._resolve_safe(relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"Soubor neexistuje: {relative_path}")
        return target.read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str) -> WorkspaceFileInfo:
        """Zapíše obsah souboru do workspace."""
        target = self._resolve_safe(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(content, encoding="utf-8")
        except PermissionError:
            target.unlink(missing_ok=True)
            target.write_text(content, encoding="utf-8")
        stat = target.stat()
        return WorkspaceFileInfo(
            name=target.name,
            path=target.relative_to(self.workspace_dir).as_posix(),
            size_bytes=stat.st_size,
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=self.tz).isoformat(),
        )

    def list_all_exportable_files(self) -> list[Path]:
        """Vrátí seznam všech exportovatelných souborů paměti a skillů (bez symlinků a inboxu)."""
        files_by_rel: dict[str, Path] = {}

        def _register(path: Path) -> None:
            if path.is_symlink() or not path.is_file():
                return
            try:
                resolved = path.resolve()
            except OSError:
                return
            if not resolved.is_relative_to(self.workspace_dir):
                return
            rel = resolved.relative_to(self.workspace_dir).as_posix()
            files_by_rel.setdefault(rel, resolved)

        # 1. Základní soubory paměti + další .md/.txt soubory přímo v kořenu workspace
        for name in CORE_MEMORY_FILES:
            _register(self.workspace_dir / name)
        try:
            for item in sorted(self.workspace_dir.iterdir(), key=lambda p: p.name.lower()):
                if item.is_symlink() or not item.is_file():
                    continue
                if item.name in MEMORY_EXPORT_EXCLUDED_ROOT or item.name.startswith("."):
                    continue
                if item.name in CORE_MEMORY_FILES or item.suffix.lower() in _ROOT_TEXT_SUFFIXES:
                    _register(item)
        except OSError:
            pass

        # 2. Všechny soubory v memory/ (rekurzivně)
        memory_dir = self.workspace_dir / "memory"
        if memory_dir.is_dir() and not memory_dir.is_symlink():
            for item in sorted(memory_dir.rglob("*"), key=lambda p: p.as_posix().lower()):
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix.lower() in _MEMORY_SUFFIXES:
                    _register(item)

        # 3. Všechny skilly v .agents/skills/ (rekurzivně)
        skills_dir = self.workspace_dir / ".agents" / "skills"
        if skills_dir.is_dir() and not skills_dir.is_symlink():
            for item in sorted(skills_dir.rglob("*"), key=lambda p: p.as_posix().lower()):
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix.lower() in _SKILL_SUFFIXES:
                    _register(item)

        return [files_by_rel[rel] for rel in sorted(files_by_rel)]

    def _validate_import_rel_path(self, raw_name: str) -> str | None:
        """Ověří cestu ze ZIP archivu; vrátí normalizovanou cestu, None pro ignorované položky nebo vyhodí ValueError."""
        norm = (raw_name or "").replace("\\", "/").strip()
        if not norm or norm.endswith("/"):
            return None
        while norm.startswith("./"):
            norm = norm[2:]
        if not norm or norm.startswith("/") or ":" in norm.split("/", 1)[0]:
            raise ValueError(f"Nepovolená cesta v archivu: {raw_name!r}")
        if any(ord(ch) < 32 for ch in norm):
            raise ValueError(f"Neplatné znaky v cestě archivu: {raw_name!r}")

        parts = norm.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"Nepovolená cesta v archivu (path traversal): {raw_name!r}")

        top = parts[0]
        if top == "__MACOSX" or parts[-1] in {".DS_Store", "Thumbs.db"}:
            return None
        if norm == "manifest.json":
            return None

        target = (self.workspace_dir / norm).resolve()
        if not target.is_relative_to(self.workspace_dir):
            raise ValueError(f"Nepovolená cesta mimo workspace: {raw_name!r}")

        suffix = Path(norm).suffix.lower()
        if len(parts) == 1:
            if (
                norm in MEMORY_EXPORT_EXCLUDED_ROOT
                or norm.startswith(".")
                or (norm not in CORE_MEMORY_FILES and suffix not in _ROOT_TEXT_SUFFIXES)
            ):
                raise ValueError(f"Nepovolený kořenový soubor v archivu: {norm!r}")
        elif top == "memory":
            if suffix not in _MEMORY_SUFFIXES:
                raise ValueError(f"Nepovolená přípona v memory/: {norm!r}")
        elif len(parts) >= 3 and parts[0] == ".agents" and parts[1] == "skills":
            if suffix not in _SKILL_SUFFIXES:
                raise ValueError(f"Nepovolená přípona v .agents/skills/: {norm!r}")
        else:
            raise ValueError(f"Nepovolené umístění souboru v archivu: {norm!r}")

        return norm

    def export_memory_zip(self, *, app_version: str = "") -> tuple[bytes, int]:
        """Zabalí kompletní paměť a vlastní skilly do ZIP archivu v paměti."""
        export_files = self.list_all_exportable_files()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in export_files:
                rel = path.relative_to(self.workspace_dir).as_posix()
                zf.write(path, arcname=rel)
            manifest = {
                "format": "agy-assistant-memory-v1",
                "exported_at": datetime.now(tz=self.tz).isoformat(),
                "app_version": app_version,
                "file_count": len(export_files),
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            )
        return buf.getvalue(), len(export_files)

    @staticmethod
    def _clear_directory_contents(directory: Path) -> None:
        if not directory.is_dir() or directory.is_symlink():
            return
        for item in sorted(directory.rglob("*"), reverse=True):
            if item.is_symlink() or item.is_file():
                item.unlink(missing_ok=True)
            elif item.is_dir():
                try:
                    item.rmdir()
                except OSError:
                    pass

    def import_memory_zip(
        self,
        raw_zip: bytes,
        *,
        mode: Literal["merge", "replace"] = "merge",
    ) -> dict[str, Any]:
        """Bezpečně ověří a naimportuje soubory paměti a skillů ze ZIP archivu."""
        if not raw_zip:
            raise ValueError("Nahraný ZIP archiv je prázdný.")
        if len(raw_zip) > MAX_IMPORT_ZIP_BYTES:
            raise ValueError("ZIP archiv překračuje maximální povolenou velikost 10 MB.")
        if mode not in ("merge", "replace"):
            raise ValueError("Neplatný režim importu (povoleno: merge, replace).")

        validated_entries: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        total_uncompressed = 0

        try:
            with zipfile.ZipFile(io.BytesIO(raw_zip), mode="r") as zf:
                infos = zf.infolist()
                if len(infos) > MAX_IMPORT_FILES + 20:
                    raise ValueError(
                        f"Archiv obsahuje příliš mnoho položek (limit {MAX_IMPORT_FILES})."
                    )
                for info in infos:
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        raise ValueError(
                            f"Archiv nesmí obsahovat symbolické odkazy: {info.filename!r}"
                        )
                    rel = self._validate_import_rel_path(info.filename)
                    if rel is None:
                        continue
                    if rel in seen_paths:
                        continue
                    if len(validated_entries) >= MAX_IMPORT_FILES:
                        raise ValueError(
                            f"Archiv překračuje limit {MAX_IMPORT_FILES} souborů."
                        )
                    if info.file_size > MAX_IMPORT_UNCOMPRESSED_BYTES:
                        raise ValueError(
                            "Rozbalená velikost souboru překračuje limit 25 MB."
                        )
                    raw_bytes = zf.read(info)
                    total_uncompressed += len(raw_bytes)
                    if total_uncompressed > MAX_IMPORT_UNCOMPRESSED_BYTES:
                        raise ValueError(
                            "Celková rozbalená velikost archivu překračuje limit 25 MB."
                        )
                    try:
                        text = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise ValueError(
                            f"Soubor {rel!r} není platný text v kódování UTF-8."
                        ) from exc
                    seen_paths.add(rel)
                    validated_entries.append((rel, text))
        except zipfile.BadZipFile as exc:
            raise ValueError("Nahraný soubor není platný ZIP archiv.") from exc

        if not validated_entries:
            raise ValueError("Archiv neobsahuje žádné platné soubory paměti.")

        if mode == "replace":
            self._clear_directory_contents(self.workspace_dir / "memory")
            self._clear_directory_contents(self.workspace_dir / ".agents" / "skills")

        imported_paths: list[str] = []
        for rel, content in validated_entries:
            self.write_file(rel, content)
            imported_paths.append(rel)

        return {
            "status": "ok",
            "mode": mode,
            "imported_count": len(imported_paths),
            "files": imported_paths,
        }
