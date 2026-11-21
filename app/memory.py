from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


CORE_MEMORY_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "HEARTBEAT.md",
)


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
        if target.suffix.lower() not in {".md", ".json", ".txt"} and cleaned not in CORE_MEMORY_FILES:
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
