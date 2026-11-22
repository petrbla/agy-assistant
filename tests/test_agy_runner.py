from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.agy_runner import (
    AgyRunner,
    extract_semver,
    has_pending_assistant_name,
    has_pending_onboarding,
    is_version_up_to_date,
    parse_agy_details,
    parse_agy_output,
    parse_models_output,
)
from app.config import Settings
from app.db import AssistantDB


def test_parse_agy_output_json_with_conversation_id() -> None:
    raw = json.dumps({"response": "Ahoj světe", "conversationId": "conv-123"})
    text, conv_id = parse_agy_output(raw)
    assert text == "Ahoj světe"
    assert conv_id == "conv-123"


def test_parse_agy_output_json_after_log_lines() -> None:
    raw = "Loading MCP...\n" + json.dumps({"text": "Hotovo", "session_id": "s-42"})
    text, conv_id = parse_agy_output(raw)
    assert text == "Hotovo"
    assert conv_id == "s-42"


def test_parse_agy_output_plain_text_fallback() -> None:
    text, conv_id = parse_agy_output("Prostá textová odpověď")
    assert text == "Prostá textová odpověď"
    assert conv_id is None


def test_build_command_and_context_prompt(tmp_path: Path) -> None:
    default_settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
    )
    default_runner = AgyRunner(default_settings, AssistantDB(default_settings.db_path))
    assert "--dangerously-skip-permissions" in default_runner.build_command("test", "conv-9")

    settings = Settings(
        workspace_dir=tmp_path,
        data_dir=tmp_path / "data",
        agy_mode="accept-edit",
        agy_dangerously_skip_permissions=False,
    )
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)

    cmd = runner.build_command("test", "conv-9")
    assert cmd == [
        "agy",
        "-p",
        "test",
        "--output-format",
        "json",
        "--conversation",
        "conv-9",
        "--mode",
        "accept-edit",
    ]

    formatted = runner.format_prompt("Check invoices", "pracovni", "telegram")
    assert "Context: Work" in formatted
    assert "work MCP" in formatted
    assert "English" in formatted

    formatted_cs = runner.format_prompt("Zkontroluj faktury", "pracovni", "telegram", language="cs")
    assert "Kontext: Pracovní" in formatted_cs
    assert "pracovní MCP" in formatted_cs
    assert "česky" in formatted_cs

    (tmp_path / "SOUL.md").write_text("- **Name:** [TODO: Ask the user]", encoding="utf-8")
    assert has_pending_assistant_name(tmp_path) is True
    assert has_pending_onboarding(tmp_path) is True
    name_prompt = runner.format_prompt("Hi", "auto", "web", assistant_name_pending=True)
    assert "SOUL.md" in name_prompt
    assert "Max" in name_prompt
    (tmp_path / "SOUL.md").write_text("- **Name:** Nova", encoding="utf-8")
    assert has_pending_assistant_name(tmp_path) is False

    (tmp_path / "USER.md").write_text("- **Jméno:** [DOPLNIT: Doptej se na jméno]", encoding="utf-8")
    assert has_pending_onboarding(tmp_path) is True
    onb_prompt = runner.format_prompt("Ahoj", "auto", "web", language="cs", onboarding_pending=True)
    assert "USER.md" in onb_prompt
    (tmp_path / "USER.md").write_text("- **Jméno:** Jan", encoding="utf-8")
    assert has_pending_onboarding(tmp_path) is False


def test_parse_agy_details_empty_response_with_denied_actions() -> None:
    raw = json.dumps(
        {
            "conversation_id": "9b4df63f-dbdc-4201-90f1-2673106ef9f6",
            "status": "SUCCESS",
            "response": "",
            "duration_seconds": 48.5,
            "num_turns": 2,
            "usage": {
                "input_tokens": 87743,
                "output_tokens": 1340,
                "total_tokens": 89083,
            },
            "denied_actions": [{"action": "read_file", "display_name": "ReadFile"}],
        }
    )
    text, conv_id, usage = parse_agy_details(raw)
    assert conv_id == "9b4df63f-dbdc-4201-90f1-2673106ef9f6"
    assert usage is not None
    assert usage.total_tokens == 89083
    assert "ReadFile" in text
    assert not text.startswith("{")


async def test_runner_persists_conversation_id_and_logs_run(tmp_path: Path) -> None:
    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)

    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(
            json.dumps({"response": "Úkol splněn", "conversationId": "c-777"}).encode(),
            b"",
        )
    )

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        res = await runner.run("Ahoj", session_key="tg:1", source="telegram", context_mode="osobni")

    assert res.success is True
    assert res.text == "Úkol splněn"
    assert res.conversation_id == "c-777"

    saved_id, saved_ctx = db.get_session("tg:1")
    assert saved_id == "c-777"
    assert saved_ctx == "osobni"

    runs = db.list_runs(limit=5)
    assert len(runs) == 1
    assert runs[0].source == "telegram"
    assert runs[0].success is True
    assert runs[0].tokens_estimated is True
    assert runs[0].total_tokens > 0


async def test_runner_records_exact_tokens_from_json_usage(tmp_path: Path) -> None:
    text, cid, usage = parse_agy_details(
        json.dumps(
            {
                "response": "Hotovo",
                "conversationId": "c-1",
                "stats": {"models": {"gemini": {"tokens": {"input": 120, "candidates": 45, "total": 165}}}},
            }
        )
    )
    assert text == "Hotovo"
    assert cid == "c-1"
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.estimated) == (
        120,
        45,
        165,
        False,
    )

    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "response": "Výsledek",
                    "conversationId": "c-99",
                    "usage": {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280},
                }
            ).encode(),
            b"",
        )
    )
    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        res = await runner.run("Dotaz", session_key="web:auto", source="web")

    assert res.input_tokens == 200
    assert res.output_tokens == 80
    assert res.total_tokens == 280
    assert res.tokens_estimated is False
    saved = db.list_runs(limit=1)[0]
    assert (saved.input_tokens, saved.output_tokens, saved.total_tokens, saved.tokens_estimated) == (
        200,
        80,
        280,
        False,
    )


def test_parse_models_output_and_build_command_with_model(tmp_path: Path) -> None:
    text_out = (
        "Fetching available models...\r\n"
        "Available models:\n"
        "- gemini-4-argon-medium  Fast\n"
        "- gemini-3.8-flash-high\n"
    )
    assert parse_models_output(text_out) == ["gemini-4-argon-medium", "gemini-3.8-flash-high"]
    json_out = json.dumps({"models": [{"name": "gemini-2.5-pro"}, {"displayName": "Gemini 3.5 Flash"}]})
    assert parse_models_output(json_out) == ["gemini-2.5-pro", "Gemini 3.5 Flash"]

    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    runner = AgyRunner(settings, AssistantDB(settings.db_path))
    cmd = runner.build_command("ahoj", None, model="Gemini 3.5 Flash (High)")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "Gemini 3.5 Flash (High)"


async def test_version_comparison_and_get_version_info(tmp_path: Path) -> None:
    assert extract_semver("agy 1.5.9") == (1, 5, 9)
    assert is_version_up_to_date("1.5.12", "1.5.12") is True
    assert is_version_up_to_date("1.5.13", "1.5.12") is True
    assert is_version_up_to_date("1.5.9", "1.5.12") is False
    assert is_version_up_to_date(None, "1.5.12") is None

    settings = Settings(workspace_dir=tmp_path, data_dir=tmp_path / "data")
    runner = AgyRunner(settings, AssistantDB(settings.db_path))
    with (
        patch.object(runner, "check_version", AsyncMock(return_value="1.5.9")),
        patch("app.agy_runner._fetch_manifest_version_sync", return_value="1.5.12"),
    ):
        info = await runner.get_version_info(force_refresh=True)
    assert info == {
        "agy_available": True,
        "agy_version": "1.5.9",
        "agy_latest_version": "1.5.12",
        "agy_up_to_date": False,
        "agy_update_available": True,
    }


async def test_startup_memory_preloaded_only_on_new_chat(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "memory").mkdir(parents=True)
    (ws / "SOUL.md").write_text("Jmenuji se Jarvis.", encoding="utf-8")
    (ws / "MEMORY.md").write_text("Aktivni projekt: Alfa.", encoding="utf-8")
    (ws / "memory" / "rozdelane.md").write_text("- Dokoncit fakturu", encoding="utf-8")
    (ws / "memory" / "server.md").write_text("VPS detaily", encoding="utf-8")
    (ws / "memory" / "2020-01-01.md").write_text("Stara poznamka", encoding="utf-8")

    settings = Settings(
        workspace_dir=ws, data_dir=tmp_path / "data", agy_persistent_session=False
    )
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)

    captured_cmds: list[tuple[str, ...]] = []
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(json.dumps({"response": "OK", "conversationId": "c-fast"}).encode(), b"")
    )

    async def _fake_exec(*cmd, **kwargs):
        captured_cmds.append(tuple(str(c) for c in cmd))
        return fake_proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        await runner.run("Ahoj", session_key="tg:1:auto", source="telegram")
        await runner.run("Druha zprava", session_key="tg:1:auto", source="telegram")

    first_prompt = captured_cmds[0][captured_cmds[0].index("-p") + 1]
    second_prompt = captured_cmds[1][captured_cmds[1].index("-p") + 1]

    assert "Preloaded memory" in first_prompt
    assert "Aktivni projekt: Alfa." in first_prompt
    assert "Dokoncit fakturu" in first_prompt
    assert "memory/server.md" in first_prompt
    assert "2020-01-01.md" not in first_prompt
    assert "Preloaded memory" not in second_prompt


async def test_persistent_warm_session_reuses_single_process(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path / "ws",
        data_dir=tmp_path / "data",
        agy_persistent_session=True,
    )
    settings.workspace_dir.mkdir(parents=True)
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)

    written_lines: list[dict[str, object]] = []
    stdout_queue: list[bytes] = [
        json.dumps({"event": "init", "init": {"conversation_id": "warm-1"}}).encode() + b"\n",
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Ahoj "}}).encode() + b"\n",
        json.dumps(
            {
                "event": "result",
                "result": {
                    "response": "Ahoj Jane!",
                    "conversation_id": "warm-1",
                    "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                },
            }
        ).encode()
        + b"\n",
        json.dumps(
            {
                "event": "result",
                "result": {
                    "response": "Druhá odpověď bleskově.",
                    "conversation_id": "warm-1",
                    "usage": {"input_tokens": 130, "output_tokens": 25, "total_tokens": 155},
                },
            }
        ).encode()
        + b"\n",
    ]

    class _FakeStdin:
        def write(self, data: bytes) -> None:
            written_lines.append(json.loads(data.decode("utf-8")))

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeStdout:
        async def readline(self) -> bytes:
            return stdout_queue.pop(0) if stdout_queue else b""

    class _FakeStderr:
        async def read(self) -> bytes:
            return b""

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    spawn_calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*cmd, **kwargs):
        spawn_calls.append(tuple(str(c) for c in cmd))
        return _FakeProc()

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_spawn):
        r1 = await runner.run("První zpráva", session_key="tg:1:auto", source="telegram")
        r2 = await runner.run("Druhá zpráva", session_key="tg:1:auto", source="telegram")
        await runner.close_session("tg:1:auto")

    assert len(spawn_calls) == 1
    assert "--input-format" in spawn_calls[0]
    assert "stream-json" in spawn_calls[0]
    assert len(written_lines) == 2
    assert written_lines[0]["event"] == "user"
    assert isinstance(written_lines[0]["message"], dict)
    assert "První zpráva" in str(written_lines[0]["message"].get("content", ""))
    assert r1.success and r1.text == "Ahoj Jane!" and r1.conversation_id == "warm-1"
    assert r1.total_tokens == 120
    assert r2.success and r2.text == "Druhá odpověď bleskově." and r2.total_tokens == 155


async def test_persistent_warm_session_falls_back_to_oneshot_on_eof(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path / "ws",
        data_dir=tmp_path / "data",
        agy_persistent_session=True,
    )
    settings.workspace_dir.mkdir(parents=True)
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)

    warm_proc = AsyncMock()
    warm_proc.returncode = None
    warm_proc.stdin = AsyncMock()
    warm_proc.stdin.write = lambda _data: None
    warm_proc.stdin.close = lambda: None
    warm_proc.stdout = AsyncMock()
    warm_proc.stdout.readline = AsyncMock(return_value=b"")
    warm_proc.stderr = AsyncMock()
    warm_proc.stderr.read = AsyncMock(return_value=b"unknown flag: --input-format")
    warm_proc.wait = AsyncMock(return_value=2)

    oneshot_proc = AsyncMock()
    oneshot_proc.returncode = 0
    oneshot_proc.communicate = AsyncMock(
        return_value=(json.dumps({"response": "Fallback OK", "conversationId": "c-fb"}).encode(), b"")
    )

    with patch("asyncio.create_subprocess_exec", side_effect=[warm_proc, oneshot_proc]):
        res = await runner.run("Test fallback", session_key="tg:1:auto", source="telegram")

    assert res.success is True
    assert res.text == "Fallback OK"
    assert res.conversation_id == "c-fb"
    assert runner._stream_json_supported is False


async def test_soul_name_syncs_to_owner_after_chat_turn(tmp_path: Path) -> None:
    from app.agy_runner import extract_assistant_name_from_soul

    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    (ws / "SOUL.md").write_text(
        "# Assistant Identity (SOUL.md)\n- **Name:** [TODO: Ask the user]\n",
        encoding="utf-8",
    )
    assert extract_assistant_name_from_soul(ws) is None

    settings = Settings(
        workspace_dir=ws,
        data_dir=tmp_path / "data",
        agy_persistent_session=False,
    )
    db = AssistantDB(settings.db_path)
    owner = db.ensure_owner(settings)
    assert owner.assistant_name == "Assistant"
    assert owner.language == "en"

    runner = AgyRunner(settings, db)
    fake_proc = AsyncMock()
    fake_proc.returncode = 0

    async def _fake_communicate():
        (ws / "SOUL.md").write_text(
            "# Assistant Identity (SOUL.md)\n- **Name:** Nova\n",
            encoding="utf-8",
        )
        return (json.dumps({"response": "I'll go by Nova.", "conversationId": "c-n1"}).encode(), b"")

    fake_proc.communicate = AsyncMock(side_effect=_fake_communicate)

    with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
        res = await runner.run("Call yourself Nova", session_key="web:auto", source="web")

    assert res.success is True
    assert extract_assistant_name_from_soul(ws) == "Nova"
    updated_owner = db.get_owner()
    assert updated_owner is not None
    assert updated_owner.assistant_name == "Nova"

    (ws / "SOUL.md").write_text("# Osobnost\n- **Jméno:** Viktor\n", encoding="utf-8")
    assert extract_assistant_name_from_soul(ws) == "Viktor"


async def test_warm_session_does_not_rerun_oneshot_after_turn_started(tmp_path: Path) -> None:
    settings = Settings(
        workspace_dir=tmp_path / "ws",
        data_dir=tmp_path / "data",
        agy_persistent_session=True,
    )
    settings.workspace_dir.mkdir(parents=True)
    db = AssistantDB(settings.db_path)
    runner = AgyRunner(settings, db)

    stdout_queue: list[bytes] = [
        json.dumps({"event": "init", "init": {"conversation_id": "warm-mid"}}).encode() + b"\n",
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Spoustim ukol..."}}).encode()
        + b"\n",
    ]

    class _FakeStdin:
        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _FakeStdout:
        async def readline(self) -> bytes:
            return stdout_queue.pop(0) if stdout_queue else b""

    class _FakeStderr:
        async def read(self) -> bytes:
            return b"connection dropped mid-turn"

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = _FakeStdin()
            self.stdout = _FakeStdout()
            self.stderr = _FakeStderr()

        async def wait(self) -> int:
            self.returncode = 1
            return 1

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = -9

    spawn_mock = AsyncMock(return_value=_FakeProc())
    with patch("asyncio.create_subprocess_exec", side_effect=spawn_mock):
        res = await runner.run("Spust akci", session_key="tg:1:auto", source="telegram")

    assert spawn_mock.await_count == 1
    assert res.success is False
    assert "connection dropped mid-turn" in (res.error or "")


def test_parse_usage_quota_output_json_and_tsv_fallback() -> None:
    from app.agy_runner import parse_usage_quota_output, quota_snapshot_to_dict

    json_payload = json.dumps(
        {
            "conversation_id": "",
            "status": "SUCCESS",
            "response": "Gemini Models\tWeekly Limit Remaining\t71%\t2026-11-28T10:48:15Z\n",
            "duration_seconds": 0,
            "num_turns": 0,
            "command": {
                "name": "usage",
                "data": {
                    "description": "Shared limits across groups.",
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "description": "Gemini models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "name": "Weekly Limit Remaining",
                                    "window": "weekly",
                                    "remaining_fraction": 0.7104,
                                    "reset_time": "2026-11-28T10:48:15Z",
                                },
                                {
                                    "id": "gemini-5h",
                                    "name": "Five Hour Limit Remaining",
                                    "window": "5h",
                                    "remaining_fraction": 0.9336,
                                    "reset_time": "2026-11-22T22:32:02Z",
                                },
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {
                                    "id": "3p-5h",
                                    "name": "Five Hour Limit Remaining",
                                    "window": "5h",
                                    "remaining_fraction": 1.25,
                                    "reset_time": "2026-11-22T23:42:52Z",
                                }
                            ],
                        },
                    ],
                },
            },
        }
    )
    snap = parse_usage_quota_output(json_payload, fetched_at="2026-11-22T18:42:52Z")
    assert snap.available is True
    assert snap.fetched_at == "2026-11-22T18:42:52Z"
    assert len(snap.groups) == 2
    assert snap.groups[0].buckets[0].window == "weekly"
    assert snap.groups[0].buckets[1].window == "5h"
    assert snap.groups[1].buckets[0].remaining_fraction == 1.0
    as_dict = quota_snapshot_to_dict(snap)
    assert as_dict["groups"][0]["buckets"][1]["remaining_percent"] == 93.4

    # Fallback na response TSV v JSON bez command.data
    resp_only = json.dumps(
        {
            "status": "SUCCESS",
            "response": (
                "Gemini Models\tWeekly Limit Remaining\t71%\t2026-11-28T10:48:15Z\n"
                "Gemini Models\tFive Hour Limit Remaining\t93%\t2026-11-22T22:32:02Z\n"
            ),
        }
    )
    snap_resp = parse_usage_quota_output(resp_only)
    assert snap_resp.available is True
    assert len(snap_resp.groups) == 1
    assert [b.window for b in snap_resp.groups[0].buckets] == ["weekly", "5h"]

    # Fallback na čistý TSV výstup (s ANSI barvami)
    raw_tsv = (
        "\x1b[32mGemini Models\tWeekly Limit Remaining\t71%\t2026-11-28T10:48:15Z\x1b[0m\n"
        "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-11-22T23:42:52Z\n"
    )
    snap_tsv = parse_usage_quota_output(raw_tsv)
    assert snap_tsv.available is True
    assert len(snap_tsv.groups) == 2

    # Chyba CLI vrátí available=False se zprávou
    err_json = json.dumps({"status": "ERROR", "error": "authentication required"})
    snap_err = parse_usage_quota_output(err_json)
    assert snap_err.available is False
    assert snap_err.error == "authentication required"


async def test_get_quota_usage_executes_agy_and_caches(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    settings = Settings(workspace_dir=ws, data_dir=tmp_path / "data")
    db = AssistantDB(settings.db_path)
    db.ensure_owner(settings)
    runner = AgyRunner(settings, db)

    payload = json.dumps(
        {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-5h",
                                    "name": "Five Hour Limit Remaining",
                                    "window": "5h",
                                    "remaining_fraction": 0.9,
                                    "reset_time": "2026-11-22T22:32:02Z",
                                }
                            ],
                        }
                    ]
                },
            },
        }
    ).encode()

    captured_cmds: list[tuple[str, ...]] = []

    async def _fake_exec(*cmd, **kwargs):
        captured_cmds.append(tuple(str(c) for c in cmd))
        p = AsyncMock()
        p.returncode = 0
        p.communicate = AsyncMock(return_value=(payload, b""))
        return p

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
        s1 = await runner.get_quota_usage()
        s2 = await runner.get_quota_usage()
        s3 = await runner.get_quota_usage(force_refresh=True)
        runner.invalidate_quota_cache()
        s4 = await runner.get_quota_usage()

    assert s1.available is True and s2.available is True and s3.available is True and s4.available is True
    assert len(captured_cmds) == 3
    assert captured_cmds[0] == ("agy", "-p", "/usage", "--output-format", "json")



