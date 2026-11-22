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


def test_memory_export_and_import_zip_merge_replace_and_security(tmp_path: Path) -> None:
    import io
    import json
    import zipfile

    src_ws = tmp_path / "src"
    src_ws.mkdir()
    (src_ws / "SOUL.md").write_text("# Osobnost", encoding="utf-8")
    (src_ws / "MEMORY.md").write_text("# Dlouhodobá paměť", encoding="utf-8")
    (src_ws / "NOTES.txt").write_text("Kořenová poznámka", encoding="utf-8")
    (src_ws / ".env").write_text("SECRET=neexportovat", encoding="utf-8")
    (src_ws / "schedule_request.json").write_text("{}", encoding="utf-8")
    (src_ws / "inbox").mkdir()
    (src_ws / "inbox" / "tajny.txt").write_text("příloha", encoding="utf-8")

    # Více než 14 denních souborů v memory/ + vnořený skill v .agents/skills/
    (src_ws / "memory").mkdir()
    for i in range(18):
        (src_ws / "memory" / f"2026-11-{i + 1:02d}.md").write_text(
            f"Den {i + 1}", encoding="utf-8"
        )
    skill_dir = src_ws / ".agents" / "skills" / "custom-skill"
    (skill_dir / "resources").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: custom-skill\n---\n", encoding="utf-8")
    (skill_dir / "resources" / "notes.yaml").write_text("key: value\n", encoding="utf-8")

    src_mem = WorkspaceMemory(src_ws)
    zip_bytes, count = src_mem.export_memory_zip(app_version="0.0.14")
    assert count == 23  # 3 kořenové + 18 v memory/ + 2 ve skillu

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "agy-assistant-memory-v1"
        assert manifest["file_count"] == 23
        assert ".env" not in names
        assert "schedule_request.json" not in names
        assert "inbox/tajny.txt" not in names
        assert "memory/2026-11-01.md" in names
        assert ".agents/skills/custom-skill/resources/notes.yaml" in names

    # Cílový workspace se stávajícím souborem: test merge i replace
    dst_ws = tmp_path / "dst"
    dst_ws.mkdir()
    (dst_ws / "memory").mkdir()
    (dst_ws / "memory" / "existing.md").write_text("stará poznámka", encoding="utf-8")
    (dst_ws / ".agents" / "skills" / "old-skill").mkdir(parents=True)
    (dst_ws / ".agents" / "skills" / "old-skill" / "SKILL.md").write_text("old", encoding="utf-8")

    dst_mem = WorkspaceMemory(dst_ws)
    merge_res = dst_mem.import_memory_zip(zip_bytes, mode="merge")
    assert merge_res["imported_count"] == 23
    assert (dst_ws / "memory" / "existing.md").is_file()
    assert (dst_ws / ".agents" / "skills" / "old-skill" / "SKILL.md").is_file()
    assert (dst_ws / "memory" / "2026-11-01.md").read_text(encoding="utf-8") == "Den 1"

    rep_res = dst_mem.import_memory_zip(zip_bytes, mode="replace")
    assert rep_res["imported_count"] == 23
    assert not (dst_ws / "memory" / "existing.md").exists()
    assert not (dst_ws / ".agents" / "skills" / "old-skill" / "SKILL.md").exists()
    assert (dst_ws / "SOUL.md").read_text(encoding="utf-8") == "# Osobnost"
    assert (dst_ws / ".agents" / "skills" / "custom-skill" / "SKILL.md").is_file()

    # Bezpečnost: Zip Slip, nepovolená přípona i poškozený ZIP nesmí projít ani zapsat
    bad_buf = io.BytesIO()
    with zipfile.ZipFile(bad_buf, "w") as zf:
        zf.writestr("MEMORY.md", "nesmi se zapsat")
        zf.writestr("../evil.md", "pwned")
    with pytest.raises(ValueError):
        dst_mem.import_memory_zip(bad_buf.getvalue(), mode="merge")
    assert (dst_ws / "MEMORY.md").read_text(encoding="utf-8") == "# Dlouhodobá paměť"

    bad_ext = io.BytesIO()
    with zipfile.ZipFile(bad_ext, "w") as zf:
        zf.writestr("memory/hack.py", "print(1)")
    with pytest.raises(ValueError):
        dst_mem.import_memory_zip(bad_ext.getvalue(), mode="merge")

    with pytest.raises(ValueError):
        dst_mem.import_memory_zip(b"neplatny-zip", mode="merge")


def test_prune_old_daily_memory(tmp_path: Path) -> None:
    from datetime import date

    (tmp_path / "MEMORY.md").write_text("# Dlouhodobá paměť", encoding="utf-8")
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "2026-10-01.md").write_text("starý den", encoding="utf-8")
    (mem_dir / "2026-10-22.md").write_text("těsně starší než 30 dní", encoding="utf-8")
    (mem_dir / "2026-10-23.md").write_text("přesně hranice 30 dní", encoding="utf-8")
    (mem_dir / "2026-11-22.md").write_text("dnešek", encoding="utf-8")
    (mem_dir / "rozdelane.md").write_text("rozdělané úkoly", encoding="utf-8")
    (mem_dir / "archiv.md").write_text("archiv", encoding="utf-8")
    (mem_dir / "projekty.md").write_text("projekty", encoding="utf-8")

    mem = WorkspaceMemory(tmp_path)
    today = date(2026, 11, 22)

    assert mem.prune_old_daily_memory(0, today=today) == []
    assert (mem_dir / "2026-10-01.md").is_file()

    removed = mem.prune_old_daily_memory(30, today=today)
    assert removed == ["memory/2026-10-01.md", "memory/2026-10-22.md"]
    assert not (mem_dir / "2026-10-01.md").exists()
    assert not (mem_dir / "2026-10-22.md").exists()
    assert (mem_dir / "2026-10-23.md").is_file()
    assert (mem_dir / "2026-11-22.md").is_file()
    assert (mem_dir / "rozdelane.md").is_file()
    assert (mem_dir / "archiv.md").is_file()
    assert (mem_dir / "projekty.md").is_file()
    assert (tmp_path / "MEMORY.md").is_file()

