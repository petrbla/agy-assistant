from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.config import ContextMode, Settings
from app.db import AssistantDB
from app.user_env import prepare_agy_home

logger = logging.getLogger(__name__)
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:\][^\x07\x1B]*(?:\x07|\x1B\\)|[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_DAILY_MEMORY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_AGY_MANIFEST_BASE_URL = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
)


def extract_semver(raw: str | None) -> tuple[int, int, int] | None:
    """Extrahuje trojici (major, minor, patch) z řetězce verze."""
    if not raw:
        return None
    match = _SEMVER_RE.search(raw)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_version_up_to_date(installed: str | None, latest: str | None) -> bool | None:
    """Porovná nainstalovanou a nejnovější verzi `agy` (vrátí None, pokud nelze určit)."""
    if not installed or not latest:
        return None
    inst_sem = extract_semver(installed)
    lat_sem = extract_semver(latest)
    if inst_sem is not None and lat_sem is not None:
        return inst_sem >= lat_sem
    return installed.strip() == latest.strip()


def detect_agy_manifest_platform() -> str:
    """Vrátí název platformy pro JSON manifest Antigravity CLI."""
    mach = platform.machine().lower()
    arch = "arm64" if mach in {"arm64", "aarch64"} else "amd64"
    if sys.platform.startswith("darwin"):
        return f"darwin_{arch}"
    return f"linux_{arch}"


def _fetch_manifest_version_sync(timeout: float = 4.0) -> str | None:
    """Synchronně načte nejnovější verzi `agy` z oficiálního manifestu Googlu."""
    url = f"{_AGY_MANIFEST_BASE_URL}/{detect_agy_manifest_platform()}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "asistent-antigravity/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            ver = data.get("version")
            if isinstance(ver, str) and ver.strip():
                return ver.strip()
    except Exception:
        return None
    return None


CONTEXT_HINTS: dict[str, str] = {
    "pracovni": (
        "[Kontext: Pracovní – tento chat je určen výhradně pro pracovní úkoly. "
        "Používej výhradně pracovní MCP nástroje a drž pracovní agendu odděleně od osobní.]"
    ),
    "osobni": (
        "[Kontext: Osobní – tento chat je určen výhradně pro osobní záležitosti. "
        "Používej výhradně osobní MCP nástroje a drž soukromou agendu odděleně od pracovní.]"
    ),
}


@dataclass(frozen=True)
class TokenUsage:
    """Počet vstupních/výstupních tokenů jednoho volání (přesný nebo odhad)."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False


@dataclass(frozen=True)
class AgyResult:
    """Výsledek jednoho volání Antigravity CLI."""

    text: str
    conversation_id: str | None
    duration_ms: int
    success: bool
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tokens_estimated: bool = False


_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "inputTokens",
    "prompt_tokens",
    "promptTokens",
    "promptTokenCount",
    "prompt_token_count",
    "input",
    "prompt",
)
_OUTPUT_TOKEN_KEYS = (
    "output_tokens",
    "outputTokens",
    "completion_tokens",
    "completionTokens",
    "candidatesTokenCount",
    "candidates_token_count",
    "output",
    "candidates",
)
_TOTAL_TOKEN_KEYS = (
    "total_tokens",
    "totalTokens",
    "totalTokenCount",
    "total_token_count",
    "total",
)


def build_startup_memory_block(
    workspace_dir: Path,
    tz_name: str = "Europe/Prague",
) -> str:
    """Sestaví kompaktní přednačtený blok paměti pro první zprávu nové konverzace.

    Ušetří agentovi 4–5 úvodních tool-callů (`ReadFile` / `list_dir`) při startu chatu.
    """
    if not workspace_dir.is_dir():
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    today_name = f"{datetime.now(tz).strftime('%Y-%m-%d')}.md"

    targets: list[tuple[str, int]] = [
        ("SOUL.md", 3000),
        ("USER.md", 3000),
        ("MEMORY.md", 6000),
        ("memory/rozdelane.md", 2500),
        (f"memory/{today_name}", 2500),
    ]
    sections: list[str] = []
    preloaded_memory_names: set[str] = {"rozdelane.md", today_name}
    resolved_ws = workspace_dir.resolve()

    for rel, limit in targets:
        path = workspace_dir / rel
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if not path.resolve().is_relative_to(resolved_ws):
                continue
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not content:
            continue
        if len(content) > limit:
            content = content[:limit].rstrip() + f"\n… [zkráceno – zbytek je v {rel}]"
        sections.append(f"### {rel}\n{content}")

    mem_dir = workspace_dir / "memory"
    thematic_files: list[str] = []
    if mem_dir.is_dir() and not mem_dir.is_symlink():
        try:
            for item in sorted(mem_dir.iterdir(), key=lambda p: p.name.lower()):
                if not item.is_file() or item.suffix.lower() != ".md":
                    continue
                if item.name in preloaded_memory_names:
                    continue
                if _DAILY_MEMORY_RE.match(item.name):
                    continue
                thematic_files.append(f"memory/{item.name}")
        except OSError:
            pass

    if not sections and not thematic_files:
        return ""

    out = [
        "[Přednačtená paměť pro start konverzace – tyto soubory už máš níže, "
        "nemusíš je znovu číst přes ReadFile, pokud je nepotřebuješ upravit:]"
    ]
    out.extend(sections)
    if thematic_files:
        out.append(
            "Další tematické soubory v paměti (čti je jen na vyžádání, když se jich týká dotaz): "
            + ", ".join(thematic_files)
        )
    return "\n\n".join(out)


def _extract_first_int(data: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        val = data.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)) and val >= 0:
            return int(val)
    return None


def _extract_usage_from_block(block: dict[str, Any]) -> TokenUsage | None:
    in_tok = _extract_first_int(block, _INPUT_TOKEN_KEYS) or 0
    out_tok = _extract_first_int(block, _OUTPUT_TOKEN_KEYS) or 0
    tot_tok = _extract_first_int(block, _TOTAL_TOKEN_KEYS) or (in_tok + out_tok)
    if in_tok == 0 and out_tok == 0 and tot_tok == 0:
        return None
    return TokenUsage(
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok or (in_tok + out_tok),
        estimated=False,
    )


def extract_token_usage(parsed: dict[str, Any]) -> TokenUsage | None:
    """Extrahuje přesné počty tokenů z běžných JSON formátů odpovědi CLI."""
    for container_key in ("usage", "usageMetadata", "tokenUsage", "token_usage", "tokens"):
        sub = parsed.get(container_key)
        if isinstance(sub, dict):
            found = _extract_usage_from_block(sub)
            if found is not None:
                return found

    stats = parsed.get("stats")
    if isinstance(stats, dict):
        models = stats.get("models")
        if isinstance(models, dict):
            in_sum = 0
            out_sum = 0
            tot_sum = 0
            for model_info in models.values():
                if not isinstance(model_info, dict):
                    continue
                tok_block = model_info.get("tokens")
                if isinstance(tok_block, dict):
                    u = _extract_usage_from_block(tok_block)
                    if u is not None:
                        in_sum += u.input_tokens
                        out_sum += u.output_tokens
                        tot_sum += u.total_tokens
            if in_sum or out_sum or tot_sum:
                return TokenUsage(
                    input_tokens=in_sum,
                    output_tokens=out_sum,
                    total_tokens=tot_sum or (in_sum + out_sum),
                    estimated=False,
                )
        found = _extract_usage_from_block(stats)
        if found is not None:
            return found

    return _extract_usage_from_block(parsed)


def estimate_token_usage(prompt: str, response: str) -> TokenUsage:
    """Orientační odhad počtu tokenů (~4 znaky / token), pokud CLI nevrátí přesná čísla."""
    p_clean = prompt.strip()
    r_clean = response.strip()
    in_tok = max(1, (len(p_clean) + 3) // 4) if p_clean else 0
    out_tok = max(1, (len(r_clean) + 3) // 4) if r_clean else 0
    return TokenUsage(
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=in_tok + out_tok,
        estimated=True,
    )


def _extract_denied_actions(parsed: dict[str, Any]) -> list[str]:
    """Extrahuje názvy akcí zablokovaných oprávněními (`denied_actions`)."""
    raw = parsed.get("denied_actions")
    if raw is None:
        raw = parsed.get("deniedActions")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            name = _extract_first_str(item, ("display_name", "displayName", "action", "name", "tool"))
            if name:
                names.append(name)
    return names


def parse_agy_details(raw_stdout: str) -> tuple[str, str | None, TokenUsage | None]:
    """Extrahuje text odpovědi, conversation_id a případnou statistiku tokenů z výstupu agy."""
    stripped = raw_stdout.strip()
    if not stripped:
        return "", None, None

    parsed = _try_parse_json(stripped)
    if isinstance(parsed, dict):
        response_keys = ("response", "text", "result", "output", "message", "content")
        text = _extract_first_str(parsed, response_keys)
        has_str_response = any(isinstance(parsed.get(k), str) for k in response_keys)
        conv_id = _extract_first_str(
            parsed, ("conversationId", "conversation_id", "sessionId", "session_id", "id")
        )
        usage = extract_token_usage(parsed)
        denied = _extract_denied_actions(parsed)
        if text is not None:
            if denied:
                text = f"{text}\n\n⚠️ Zablokované akce (chybí oprávnění): {', '.join(denied)}"
            return text, conv_id, usage
        if has_str_response or conv_id is not None or usage is not None or denied:
            if denied:
                msg = (
                    f"⚠️ Antigravity CLI zablokoval akci kvůli oprávněním: {', '.join(denied)}. "
                    "Pro headless provoz zapněte AGY_DANGEROUSLY_SKIP_PERMISSIONS=true."
                )
                return msg, conv_id, usage
            return "", conv_id, usage

    return stripped, None, None


def parse_agy_output(raw_stdout: str) -> tuple[str, str | None]:
    """Extrahuje text odpovědi a conversation_id z JSON výstupu agy."""
    text, conv_id, _ = parse_agy_details(raw_stdout)
    return text, conv_id


def parse_models_output(raw_stdout: str) -> list[str]:
    """Extrahuje seznam dostupných modelů z výstupu `agy models`."""
    cleaned = _ANSI_ESCAPE_RE.sub("", raw_stdout).strip()
    if not cleaned:
        return []

    seen: set[str] = set()
    models: list[str] = []

    def _add(name: str) -> None:
        item = name.strip().strip("`\"'")
        if item and item not in seen:
            seen.add(item)
            models.append(item)

    parsed = _try_parse_json(cleaned)
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, str):
                _add(entry)
            elif isinstance(entry, dict):
                found = _extract_first_str(entry, ("name", "displayName", "model", "id"))
                if found:
                    _add(found)
        return models
    if isinstance(parsed, dict):
        items = parsed.get("models")
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, str):
                    _add(entry)
                elif isinstance(entry, dict):
                    found = _extract_first_str(entry, ("name", "displayName", "model", "id"))
                    if found:
                        _add(found)
            return models

    for raw_line in cleaned.replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if (
            low.startswith("available models")
            or low.startswith("models:")
            or low.startswith("fetching ")
            or low.startswith("loading ")
            or low.startswith("retrieving ")
            or low.startswith("checking ")
            or line.endswith("...")
            or line.endswith("…")
            or set(line) <= {"-", "=", "─", " "}
        ):
            continue
        line = re.sub(r"^(?:[-*•]+|\d+[.)])\s+", "", line)
        col = re.split(r"\t|\s{2,}|\s+\|\s+", line, maxsplit=1)[0].strip()
        if col and len(col) <= 80:
            _add(col)
    return models


def _try_parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None


def _extract_first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _file_has_placeholder(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        lowered = path.read_text(encoding="utf-8").lower()
    except OSError:
        return False
    return "doptej se" in lowered or "[doplnit" in lowered


def has_pending_assistant_name(workspace_dir: Path | None) -> bool:
    if workspace_dir is None:
        return False
    return _file_has_placeholder(workspace_dir / "SOUL.md")


def extract_assistant_name_from_soul(workspace_dir: Path) -> str | None:
    """Přečte zvolené jméno asistenta ze souboru `SOUL.md`, pokud již bylo doplněno."""
    soul_path = workspace_dir / "SOUL.md"
    if not soul_path.is_file() or has_pending_assistant_name(workspace_dir):
        return None
    try:
        content = soul_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r"^\s*(?:[-*]\s*)?\*\*Jméno:\*\*\s*(.+?)\s*$",
        content,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return None
    candidate = match.group(1).strip().strip("*_\"' ").rstrip(".")
    lowered = candidate.lower()
    if not (1 <= len(candidate) <= 60) or "doplnit" in lowered or "doptej" in lowered:
        return None
    return candidate


def has_pending_onboarding(workspace_dir: Path | None) -> bool:
    if workspace_dir is None:
        return False
    return _file_has_placeholder(workspace_dir / "SOUL.md") or _file_has_placeholder(
        workspace_dir / "USER.md"
    )


def has_schedule_instructions(workspace_dir: Path | None) -> bool:
    if workspace_dir is None:
        return True
    agents_md = workspace_dir / "AGENTS.md"
    if not agents_md.is_file():
        return True
    try:
        return "add_tasks" in agents_md.read_text(encoding="utf-8")
    except OSError:
        return True


class _WarmSessionFallbackError(Exception):
    """Signalizuje, že teplý `stream-json` proces nelze použít a má proběhnout one-shot fallback."""

    def __init__(self, message: str, *, disable_stream: bool = False) -> None:
        super().__init__(message)
        self.disable_stream = disable_stream


class _WarmAgyProcess:
    """Trvale spuštěný (`stream-json`) proces `agy` pro jednu relaci bez cold startu."""

    def __init__(
        self,
        *,
        session_key: str,
        proc: asyncio.subprocess.Process,
        signature: tuple[str, str, str, str],
        conversation_id: str | None = None,
    ) -> None:
        self.session_key = session_key
        self.proc = proc
        self.signature = signature
        self.conversation_id = conversation_id

    def is_alive(self) -> bool:
        return (
            self.proc.returncode is None
            and self.proc.stdin is not None
            and self.proc.stdout is not None
        )

    async def close(self) -> None:
        if self.proc.returncode is None:
            if self.proc.stdin is not None:
                with contextlib.suppress(Exception):
                    self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.5)
            except Exception:
                with contextlib.suppress(Exception):
                    self.proc.terminate()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=1.5)
                except Exception:
                    with contextlib.suppress(Exception):
                        self.proc.kill()
                    with contextlib.suppress(Exception):
                        await self.proc.wait()

    async def send_turn(
        self, prompt: str, *, timeout: int
    ) -> tuple[str, str | None, TokenUsage | None]:
        """Odešle jednu zprávu přes NDJSON `stdin` a přečte odpověď až po `event=result`."""
        if not self.is_alive():
            raise _WarmSessionFallbackError("Teplý proces agy již neběží.")
        assert self.proc.stdin is not None and self.proc.stdout is not None

        payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False)
        try:
            self.proc.stdin.write((payload + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise _WarmSessionFallbackError(f"Zápis do teplého procesu selhal: {exc}") from exc

        deadline = time.monotonic() + max(1, timeout)
        deltas: list[str] = []
        conv_id: str | None = self.conversation_id

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            raw_line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
            if not isinstance(raw_line, (bytes, bytearray)) or not raw_line:
                stderr_hint = ""
                if self.proc.stderr is not None:
                    with contextlib.suppress(Exception):
                        err_b = await asyncio.wait_for(self.proc.stderr.read(), timeout=0.5)
                        if isinstance(err_b, (bytes, bytearray)):
                            stderr_hint = err_b.decode("utf-8", errors="replace").strip()
                disable = any(
                    tok in stderr_hint.lower()
                    for tok in ("unknown flag", "unrecognized", "invalid option", "input-format")
                )
                raise _WarmSessionFallbackError(
                    stderr_hint or "Teplý proces agy ukončil proud před výsledkem.",
                    disable_stream=disable,
                )

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            ev = str(obj.get("event") or obj.get("type") or "").strip().lower()
            if ev == "init":
                init_block = obj.get("init") if isinstance(obj.get("init"), dict) else obj
                found_cid = _extract_first_str(
                    obj, ("conversation_id", "conversationId", "session_id", "sessionId")
                ) or _extract_first_str(
                    init_block, ("conversation_id", "conversationId", "session_id", "sessionId")
                )
                if found_cid:
                    conv_id = found_cid
                    self.conversation_id = found_cid
                continue

            if ev == "step_update":
                step_block = (
                    obj.get("step_update") if isinstance(obj.get("step_update"), dict) else obj
                )
                delta = step_block.get("text_delta") or step_block.get("textDelta")
                if isinstance(delta, str) and delta:
                    deltas.append(delta)
                continue

            if ev == "result":
                res_block = obj.get("result") if isinstance(obj.get("result"), dict) else obj
                found_cid = _extract_first_str(
                    res_block, ("conversation_id", "conversationId", "session_id", "sessionId")
                ) or _extract_first_str(
                    obj, ("conversation_id", "conversationId", "session_id", "sessionId")
                )
                if found_cid:
                    conv_id = found_cid
                    self.conversation_id = found_cid

                text = _extract_first_str(
                    res_block, ("response", "text", "result", "output", "message", "content")
                )
                if text is None:
                    text = "".join(deltas).strip()

                status_str = str(res_block.get("status") or obj.get("status") or "").upper()
                if not text and status_str and status_str != "SUCCESS":
                    err_detail = (
                        _extract_first_str(res_block, ("error", "message", "detail")) or status_str
                    )
                    raise _WarmSessionFallbackError(f"stream-json result status={err_detail}")

                usage = extract_token_usage(res_block) or extract_token_usage(obj)
                denied = _extract_denied_actions(res_block) or _extract_denied_actions(obj)
                if denied:
                    warn = f"⚠️ Zablokované akce (chybí oprávnění): {', '.join(denied)}"
                    text = f"{text}\n\n{warn}" if text else warn
                return text, conv_id, usage

            if ev == "error":
                err_block = obj.get("error")
                err_msg = (
                    err_block
                    if isinstance(err_block, str)
                    else _extract_first_str(
                        err_block if isinstance(err_block, dict) else obj,
                        ("message", "error", "detail"),
                    )
                    or "Neznámá chyba stream-json."
                )
                raise _WarmSessionFallbackError(err_msg)


class AgyRunner:
    """Single-user asynchronní spouštěč Antigravity CLI (`agy`) s teplým `stream-json` režimem."""

    def __init__(
        self,
        settings: Settings,
        db: AssistantDB,
        *,
        on_schedule_updated: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.on_schedule_updated = on_schedule_updated
        self._lock = asyncio.Lock()
        self._models_cache: tuple[float, list[str]] | None = None
        self._latest_version_cache: tuple[float, str | None] | None = None
        self._warm_procs: dict[str, _WarmAgyProcess] = {}
        self._stream_json_supported: bool = True

    def invalidate_models_cache(self) -> None:
        self._models_cache = None

    async def close_session(self, session_key: str) -> None:
        """Bezpečně ukončí teplý `agy` proces pro konkrétní `session_key`."""
        warm = self._warm_procs.pop(session_key, None)
        if warm is not None:
            await warm.close()

    async def close_all(self) -> None:
        """Bezpečně ukončí všechny teplé `agy` procesy."""
        for key in list(self._warm_procs.keys()):
            await self.close_session(key)

    def build_command(
        self,
        prompt: str,
        conversation_id: str | None,
        *,
        model: str | None = None,
    ) -> list[str]:
        """Sestaví bezpečný seznam argumentů pro jednorázové `agy -p`."""
        cmd = [self.settings.agy_bin, "-p", prompt, "--output-format", "json"]
        effective_model = (model if model is not None else self.settings.agy_model).strip()
        if effective_model:
            cmd.extend(["--model", effective_model])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if self.settings.agy_dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        elif self.settings.agy_mode:
            cmd.extend(["--mode", self.settings.agy_mode])
        return cmd

    def build_stream_command(
        self,
        conversation_id: str | None,
        *,
        model: str | None = None,
    ) -> list[str]:
        """Sestaví příkaz pro trvale běžící (`stream-json`) proces `agy`."""
        cmd = [
            self.settings.agy_bin,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]
        effective_model = (model if model is not None else self.settings.agy_model).strip()
        if effective_model:
            cmd.extend(["--model", effective_model])
        if conversation_id:
            cmd.extend(["--conversation", conversation_id])
        if self.settings.agy_dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        elif self.settings.agy_mode:
            cmd.extend(["--mode", self.settings.agy_mode])
        return cmd

    def format_prompt(
        self,
        prompt: str,
        context_mode: ContextMode,
        source: str,
        *,
        onboarding_pending: bool = False,
        assistant_name_pending: bool = False,
        include_schedule_hint: bool = False,
        startup_memory: str = "",
    ) -> str:
        """Připojí lehkou hlavičku kontextu, zdroje a případnou přednačtenou paměť."""
        parts = [f"[Zdroj: {source}]"]
        hint = CONTEXT_HINTS.get(context_mode)
        if hint:
            parts.append(hint)
        if include_schedule_hint:
            parts.append(
                "[Plánovač: Pokud uživatel žádá připomínku nebo opakovanou automatickou úlohu/zprávu v určitý čas, "
                "zapiš JSON do schedule_request.json v kořenu workspace: "
                '{"add_tasks":[{"name":"Název","prompt":"Text/úkol","schedule_type":"cron|once",'
                '"cron_expr":"0 12 * * *","run_at":"YYYY-MM-DDTHH:MM:SS","action_type":"message|agent"}]} '
                "(pro změnu briefingu/heartbeatu klíče morning_briefing_cron/enabled, heartbeat_interval_minutes/enabled).]"
            )
        if assistant_name_pending:
            parts.append(
                "[Úvodní nastavení: V SOUL.md ještě nemáš vybrané vlastní jméno. "
                "Začni konverzaci tím, že se uživatele zeptáš, jak se máš jmenovat, "
                "nabídni mu několik návrhů (např. Max, Viktor, Nova, Eliška, Hugo – nikdy nenavrhuj Jarvis) "
                "a zvolené jméno rovnou zapiš do SOUL.md. Poté postupně doplň i USER.md podle AGENTS.md.]"
            )
        elif onboarding_pending:
            parts.append(
                "[Profil uživatele: V USER.md jsou zatím nevyplněné položky označené 'Doptej se' / '[DOPLNIT]'. "
                "Řiď se pokyny v AGENTS.md – přirozeně se doptávej (1–3 stručné otázky, příp. až po vyřízení úkolu) "
                "a zjištěné údaje rovnou doplň do USER.md / MEMORY.md.]"
            )
        if startup_memory.strip():
            parts.append(startup_memory.strip())
        parts.append(prompt.strip())
        return "\n".join(parts)

    async def check_version(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.settings.agy_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            out = stdout.decode("utf-8", errors="replace").strip()
            return out if proc.returncode == 0 else None
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            return None

    async def check_latest_version(self, *, force_refresh: bool = False) -> str | None:
        now = time.monotonic()
        if not force_refresh and self._latest_version_cache is not None:
            cached_at, cached_ver = self._latest_version_cache
            ttl = 900.0 if cached_ver is not None else 60.0
            if (now - cached_at) < ttl:
                return cached_ver
        latest = await asyncio.to_thread(_fetch_manifest_version_sync)
        self._latest_version_cache = (now, latest)
        return latest

    async def get_version_info(self, *, force_refresh: bool = False) -> dict[str, Any]:
        installed, latest = await asyncio.gather(
            self.check_version(),
            self.check_latest_version(force_refresh=force_refresh),
        )
        up_to_date = is_version_up_to_date(installed, latest)
        return {
            "agy_available": installed is not None,
            "agy_version": installed,
            "agy_latest_version": latest,
            "agy_up_to_date": up_to_date,
            "agy_update_available": up_to_date is False,
        }

    async def update_cli(self) -> dict[str, Any]:
        wrapper = Path("/usr/local/sbin/agy-cli-update")
        if wrapper.is_file() and sys.platform != "win32":
            cmd = ["sudo", "-n", str(wrapper)]
        else:
            cmd = [self.settings.agy_bin, "update"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            out = _ANSI_ESCAPE_RE.sub("", stdout.decode("utf-8", errors="replace")).strip()
            ok = proc.returncode == 0
        except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
            out = f"Aktualizace agy selhala: {exc}"
            ok = False

        await self.close_all()
        self._latest_version_cache = None
        self.invalidate_models_cache()
        info = await self.get_version_info(force_refresh=True)
        return {"success": ok, "output": out, **info}

    async def list_models(self, *, force_refresh: bool = False) -> list[str]:
        now = time.monotonic()
        if not force_refresh and self._models_cache is not None:
            cached_at, cached_models = self._models_cache
            if (now - cached_at) < 180:
                return list(cached_models)

        owner = self.db.get_owner()
        eff_key = owner.gemini_api_key if owner is not None else self.settings.gemini_api_key
        env = self.settings.subprocess_env(gemini_api_key=eff_key)
        try:
            proc = await asyncio.create_subprocess_exec(
                self.settings.agy_bin,
                "models",
                cwd=str(self.settings.workspace_dir) if self.settings.workspace_dir.is_dir() else None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return []
            models = parse_models_output(stdout.decode("utf-8", errors="replace"))
            if models:
                self._models_cache = (now, models)
            return models
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            return []

    def _apply_schedule_request(self, workspace_dir: Path) -> None:
        req_file = workspace_dir / "schedule_request.json"
        if not req_file.exists() or req_file.is_symlink():
            return
        try:
            resolved_ws = workspace_dir.resolve()
            resolved_req = req_file.resolve()
            if not resolved_req.is_relative_to(resolved_ws):
                return
            raw = json.loads(resolved_req.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Neplatný schedule_request.json: %s", exc)
            return
        finally:
            with contextlib.suppress(OSError):
                req_file.unlink(missing_ok=True)

        if not isinstance(raw, dict):
            return

        owner = self.db.get_owner()
        tz_name = (owner.tz if owner else "") or self.settings.tz
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        updates: dict[str, Any] = {}
        if isinstance(raw.get("morning_briefing_enabled"), bool):
            updates["morning_briefing_enabled"] = raw["morning_briefing_enabled"]
        if isinstance(raw.get("morning_briefing_cron"), str):
            cron_expr = raw["morning_briefing_cron"].strip()
            if cron_expr:
                try:
                    CronTrigger.from_crontab(cron_expr, timezone=tz)
                    updates["morning_briefing_cron"] = cron_expr
                except ValueError as exc:
                    logger.warning("Neplatný cron v schedule_request.json (%s): %s", cron_expr, exc)
        if isinstance(raw.get("heartbeat_enabled"), bool):
            updates["heartbeat_enabled"] = raw["heartbeat_enabled"]
        hb_interval = raw.get("heartbeat_interval_minutes")
        if isinstance(hb_interval, int) and not isinstance(hb_interval, bool) and hb_interval >= 1:
            updates["heartbeat_interval_minutes"] = min(10080, hb_interval)

        changed = False
        if updates and owner is not None:
            self.db.update_owner(**updates)
            changed = True

        remove_tasks = raw.get("remove_tasks")
        if isinstance(remove_tasks, list):
            for name in remove_tasks:
                if isinstance(name, str) and name.strip():
                    if self.db.delete_scheduled_task_by_name(name):
                        changed = True

        add_tasks = raw.get("add_tasks")
        if isinstance(add_tasks, list):
            for item in add_tasks:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                task_prompt = str(item.get("prompt") or "").strip()
                if not name or not task_prompt:
                    continue
                sched_type = "once" if item.get("schedule_type") == "once" else "cron"
                action_type = "agent" if item.get("action_type") == "agent" else "message"
                cron_val = str(item.get("cron_expr") or "").strip()
                run_at_val = str(item.get("run_at") or "").strip()
                if sched_type == "cron":
                    try:
                        CronTrigger.from_crontab(cron_val, timezone=tz)
                    except ValueError as exc:
                        logger.warning("Neplatný cron úlohy '%s' (%s): %s", name, cron_val, exc)
                        continue
                else:
                    try:
                        datetime.fromisoformat(run_at_val)
                    except ValueError as exc:
                        logger.warning("Neplatný čas run_at úlohy '%s' (%s): %s", name, run_at_val, exc)
                        continue
                self.db.upsert_scheduled_task(
                    name=name,
                    prompt=task_prompt,
                    schedule_type=sched_type,
                    cron_expr=cron_val,
                    run_at=run_at_val,
                    action_type=action_type,
                    enabled=True,
                )
                changed = True

        if changed and self.on_schedule_updated is not None:
            try:
                self.on_schedule_updated()
            except Exception as exc:
                logger.warning("Synchronizace plánovače selhala: %s", exc)

    def _record_error(
        self,
        *,
        source: str,
        session_key: str,
        context_mode: ContextMode,
        prompt: str,
        conversation_id: str | None,
        started: float,
        err_msg: str,
        user_prefix: str = "",
    ) -> AgyResult:
        duration_ms = int((time.monotonic() - started) * 1000)
        self.db.log_run(
            source=source,
            session_key=session_key,
            context_mode=context_mode,
            prompt=prompt,
            response="",
            conversation_id=conversation_id,
            duration_ms=duration_ms,
            success=False,
            error=err_msg,
        )
        return AgyResult(
            text=f"{user_prefix}{err_msg}",
            conversation_id=conversation_id,
            duration_ms=duration_ms,
            success=False,
            error=err_msg,
        )

    def _sync_assistant_name_from_soul(self, workspace_dir: Path) -> None:
        """Synchronizuje jméno zvolené asistentem v `SOUL.md` zpět do databáze."""
        soul_name = extract_assistant_name_from_soul(workspace_dir)
        if not soul_name:
            return
        owner = self.db.get_owner()
        if owner is not None and owner.assistant_name != soul_name:
            self.db.update_owner(assistant_name=soul_name)

    async def run(
        self,
        prompt: str,
        *,
        session_key: str,
        source: str,
        context_mode: ContextMode | None = None,
        use_conversation: bool = True,
        model: str | None = None,
        gemini_api_key: str | None = None,
    ) -> AgyResult:
        """Spustí jeden požadavek přes `agy` CLI (teplý `stream-json` s fallbackem na one-shot)."""
        async with self._lock:
            owner = self.db.get_owner()
            fallback_ctx: ContextMode = (
                owner.default_context_mode if owner is not None else self.settings.default_context_mode
            )  # type: ignore[assignment]
            saved_conv_id, saved_ctx = self.db.get_session(
                session_key, default_context=fallback_ctx
            )
            effective_ctx: ContextMode = context_mode or saved_ctx  # type: ignore[assignment]
            conv_id = saved_conv_id if use_conversation else None

            eff_cwd = self.settings.workspace_dir
            eff_cwd.mkdir(parents=True, exist_ok=True)
            eff_key = (
                gemini_api_key
                if gemini_api_key is not None
                else (owner.gemini_api_key if owner is not None else self.settings.gemini_api_key)
            )
            eff_model = (
                model
                if model is not None
                else (owner.agy_model if owner is not None else self.settings.agy_model)
            ).strip()
            eff_tz = (owner.tz if owner is not None else self.settings.tz) or "Europe/Prague"
            prepare_agy_home(self.settings, self.db, context_mode=effective_ctx)

            is_new_chat = conv_id is None and source in ("web", "telegram")
            assistant_name_pending = is_new_chat and has_pending_assistant_name(eff_cwd)
            onboarding_pending = is_new_chat and has_pending_onboarding(eff_cwd)
            include_schedule_hint = source in ("web", "telegram") and not has_schedule_instructions(
                eff_cwd
            )
            startup_memory = build_startup_memory_block(eff_cwd, tz_name=eff_tz) if is_new_chat else ""
            full_prompt = self.format_prompt(
                prompt,
                effective_ctx,
                source,
                onboarding_pending=onboarding_pending,
                assistant_name_pending=assistant_name_pending,
                include_schedule_hint=include_schedule_hint,
                startup_memory=startup_memory,
            )
            env = self.settings.subprocess_env(gemini_api_key=eff_key)

            started = time.monotonic()
            use_warm = (
                self.settings.agy_persistent_session
                and self._stream_json_supported
                and use_conversation
                and source in ("web", "telegram", "nightly_reset")
            )
            if use_warm:
                sig = (eff_model, effective_ctx, eff_key or "", str(eff_cwd))
                warm = self._warm_procs.get(session_key)
                if warm is not None and (not warm.is_alive() or warm.signature != sig):
                    await self.close_session(session_key)
                    warm = None
                try:
                    if warm is None:
                        stream_cmd = self.build_stream_command(conv_id, model=eff_model)
                        warm_proc = await asyncio.create_subprocess_exec(
                            *stream_cmd,
                            cwd=str(eff_cwd),
                            env=env,
                            stdin=asyncio.subprocess.PIPE,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            limit=8 * 1024 * 1024,
                        )
                        warm = _WarmAgyProcess(
                            session_key=session_key,
                            proc=warm_proc,
                            signature=sig,
                            conversation_id=conv_id,
                        )
                        self._warm_procs[session_key] = warm
                        logger.info(
                            "Spuštěn teplý proces agy (stream-json) pro relaci %s.", session_key
                        )

                    text, new_conv_id, parsed_usage = await warm.send_turn(
                        full_prompt, timeout=self.settings.agy_timeout_seconds
                    )
                    duration_ms = int((time.monotonic() - started) * 1000)
                    usage = parsed_usage or estimate_token_usage(prompt, text)
                    final_conv_id = new_conv_id or conv_id
                    if final_conv_id:
                        self.db.set_conversation_id(session_key, final_conv_id, effective_ctx)
                    if source in ("web", "telegram"):
                        self._apply_schedule_request(eff_cwd)
                        self._sync_assistant_name_from_soul(eff_cwd)
                    self.db.log_run(
                        source=source,
                        session_key=session_key,
                        context_mode=effective_ctx,
                        prompt=prompt,
                        response=text,
                        conversation_id=final_conv_id,
                        duration_ms=duration_ms,
                        success=True,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                        tokens_estimated=usage.estimated,
                    )
                    return AgyResult(
                        text=text or "(Prázdná odpověď)",
                        conversation_id=final_conv_id,
                        duration_ms=duration_ms,
                        success=True,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        total_tokens=usage.total_tokens,
                        tokens_estimated=usage.estimated,
                    )
                except asyncio.TimeoutError:
                    await self.close_session(session_key)
                    return self._record_error(
                        source=source,
                        session_key=session_key,
                        context_mode=effective_ctx,
                        prompt=prompt,
                        conversation_id=conv_id,
                        started=started,
                        err_msg=f"Vypršel časový limit ({self.settings.agy_timeout_seconds} s).",
                    )
                except FileNotFoundError:
                    await self.close_session(session_key)
                    return self._record_error(
                        source=source,
                        session_key=session_key,
                        context_mode=effective_ctx,
                        prompt=prompt,
                        conversation_id=conv_id,
                        started=started,
                        err_msg=f"Binárka '{self.settings.agy_bin}' nebyla nalezena.",
                    )
                except _WarmSessionFallbackError as exc:
                    await self.close_session(session_key)
                    if exc.disable_stream:
                        self._stream_json_supported = False
                    logger.warning(
                        "Teplý proces agy selhal (%s) – přepínám na jednorázové volání.", exc
                    )

            cmd = self.build_command(full_prompt, conv_id, model=eff_model)
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(eff_cwd),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.settings.agy_timeout_seconds
                )
                stdout_s = stdout_b.decode("utf-8", errors="replace")
                stderr_s = stderr_b.decode("utf-8", errors="replace").strip()

                if proc.returncode != 0:
                    err_msg = stderr_s or stdout_s.strip() or f"agy skončil s kódem {proc.returncode}"
                    logger.warning("agy error (rc=%s): %s", proc.returncode, err_msg)
                    return self._record_error(
                        source=source,
                        session_key=session_key,
                        context_mode=effective_ctx,
                        prompt=prompt,
                        conversation_id=conv_id,
                        started=started,
                        err_msg=err_msg,
                        user_prefix="Chyba Antigravity CLI: ",
                    )

                duration_ms = int((time.monotonic() - started) * 1000)
                text, new_conv_id, parsed_usage = parse_agy_details(stdout_s)
                usage = parsed_usage or estimate_token_usage(prompt, text)
                final_conv_id = new_conv_id or conv_id
                if use_conversation and final_conv_id:
                    self.db.set_conversation_id(session_key, final_conv_id, effective_ctx)
                if source in ("web", "telegram"):
                    self._apply_schedule_request(eff_cwd)
                    self._sync_assistant_name_from_soul(eff_cwd)

                self.db.log_run(
                    source=source,
                    session_key=session_key,
                    context_mode=effective_ctx,
                    prompt=prompt,
                    response=text,
                    conversation_id=final_conv_id,
                    duration_ms=duration_ms,
                    success=True,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    tokens_estimated=usage.estimated,
                )
                return AgyResult(
                    text=text or "(Prázdná odpověď)",
                    conversation_id=final_conv_id,
                    duration_ms=duration_ms,
                    success=True,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    tokens_estimated=usage.estimated,
                )
            except asyncio.TimeoutError:
                if proc is not None and proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                return self._record_error(
                    source=source,
                    session_key=session_key,
                    context_mode=effective_ctx,
                    prompt=prompt,
                    conversation_id=conv_id,
                    started=started,
                    err_msg=f"Vypršel časový limit ({self.settings.agy_timeout_seconds} s).",
                )
            except FileNotFoundError:
                return self._record_error(
                    source=source,
                    session_key=session_key,
                    context_mode=effective_ctx,
                    prompt=prompt,
                    conversation_id=conv_id,
                    started=started,
                    err_msg=f"Binárka '{self.settings.agy_bin}' nebyla nalezena.",
                )
