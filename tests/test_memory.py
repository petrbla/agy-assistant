from __future__ import annotations

from pathlib import Path

import pytest

from app.memory import WorkspaceMemory


def test_memory_read_write_and_list(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("# Soul", encoding="utf-8")
    (tmp_path / ".agents" / "skills" / "morning-briefing").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "morning-briefing" / "SKILL.md").write_text(
        "---\nname: morning-briefing\n---\n", encoding="utf-8"
    )

    mem = WorkspaceMemory(tmp_path)
    files = mem.list_files()
    assert [f.path for f in files] == ["SOUL.md"]
    assert mem.list_skills() == ["morning-briefing"]

    mem.write_file("memory/2026-11-10.md", "Poznámka dne")
    assert mem.read_file("memory/2026-11-10.md") == "Poznámka dne"


def test_memory_blocks_path_traversal(tmp_path: Path) -> None:
    mem = WorkspaceMemory(tmp_path)
    with pytest.raises(ValueError):
        mem.read_file("../secret.txt")
    with pytest.raises(ValueError):
        mem.write_file("../../etc/passwd", "x")
    with pytest.raises(ValueError):
        mem.write_file("script.py", "print(1)")
