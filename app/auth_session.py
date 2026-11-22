from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows nemá POSIX pty/fcntl/termios
    fcntl = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]

_ANSI_RE = re.compile(r"\x1B(?:\][^\x07\x1B]*(?:\x07|\x1B\\)|[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CURSOR_NEWLINE_RE = re.compile(r"\x1B\[(?:[0-9;]*[HfE]|2J)")
_CURSOR_RIGHT_RE = re.compile(r"\x1B\[(\d*)C")
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_OSC8_URL_RE = re.compile(r"\x1B\]8;[^;\x07\x1B]*;(https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+)(?:\x07|\x1B\\)")
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+")
_URL_CHAR_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+$")
_TRAILING_URL_SEP_RE = re.compile(r"(?:%[0-9A-Fa-f]?|[&=+/\-_.:~])$")
_BOX_BORDER_RE = re.compile(r"^[│|┃║\s]+$")
_GOOGLE_OAUTH_PREFIXES = (
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
)


def clean_terminal_output(raw: str) -> str:
    """Odstraní ANSI a řídicí sekvence z výstupu terminálu se zachováním řádkování kurzoru."""
    normalized = _CURSOR_NEWLINE_RE.sub("\n", raw)
    normalized = _CURSOR_RIGHT_RE.sub(lambda m: " " * min(int(m.group(1) or 1), 8), normalized)
    cleaned = _ANSI_RE.sub("", normalized)
    cleaned = _CTRL_CHARS_RE.sub("", cleaned)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


def _merge_unique_urls(candidates: list[str]) -> list[str]:
    """Sloučí URL při zachování pořadí a nahradí kratší/duplicitní Google OAuth URL plnou URL."""
    kept: list[str] = []
    for raw_url in candidates:
        url = raw_url.strip().rstrip(".,;:!?")
        if not url:
            continue
        if any(existing == url or existing.startswith(url) for existing in kept):
            continue
        replaced = False
        for idx, existing in enumerate(kept):
            if url.startswith(existing) and len(url) > len(existing):
                kept[idx] = url
                replaced = True
                break
        if not replaced:
            kept.append(url)
    google_idxs = [
        idx for idx, u in enumerate(kept) if any(u.startswith(p) for p in _GOOGLE_OAUTH_PREFIXES)
    ]
    if len(google_idxs) > 1:
        longest_google = max((kept[idx] for idx in google_idxs), key=len)
        first_idx = google_idxs[0]
        kept = [
            longest_google if idx == first_idx else u
            for idx, u in enumerate(kept)
            if idx == first_idx or idx not in google_idxs
        ]
    return kept


def extract_osc8_urls(raw: str) -> list[str]:
    """Extrahuje plné hyperlinky z ANSI OSC-8 sekvencí ještě před očištěním terminálu."""
    return _merge_unique_urls([match for match in _OSC8_URL_RE.findall(raw)])


def _strip_box_line(line: str) -> str:
    """Očistí řádek terminálu o okrajové mezery a případný jeden rámečkový znak zleva/zprava."""
    s = line.strip()
    if s and s[0] in "│|┃║":
        s = s[1:].lstrip()
    if s and s[-1] in "│|┃║":
        s = s[:-1].rstrip()
    return s.strip()


def _is_url_continuation(current_url: str, cont: str, prev_visible_len: int) -> bool:
    """Rozpozná, zda další řádek terminálu navazuje na zalomenou URL z předchozího řádku."""
    if not cont or cont.lower().startswith(("http://", "https://")) or cont.endswith(":"):
        return False
    if not _URL_CHAR_RE.fullmatch(cont):
        return False
    if _TRAILING_URL_SEP_RE.search(current_url):
        return True
    if any(ch in cont for ch in "%=&/+~"):
        return True
    if len(cont) >= 16 and any(ch.isdigit() or ch in "-_." for ch in cont):
        return True
    if prev_visible_len >= 60 and (
        any(ch.isdigit() for ch in cont)
        or any(not ch.isalnum() for ch in cont)
        or any(ch.isupper() for ch in cont[1:])
    ):
        return True
    return False


def extract_urls(text: str) -> list[str]:
    """Najde unikátní URL odkazy v textu a spojí dlouhé URL zalomené na více řádků terminálu."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    raw_urls: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        matches = list(_URL_RE.finditer(line))
        if not matches:
            i += 1
            continue
        prev_len = len(_strip_box_line(line))
        for m_idx, match in enumerate(matches):
            url = match.group(0)
            is_last_on_line = m_idx == len(matches) - 1
            trailing = line[match.end() :]
            reaches_end = is_last_on_line and (trailing == "" or bool(_BOX_BORDER_RE.fullmatch(trailing)))
            while reaches_end and i + 1 < len(lines):
                cont = _strip_box_line(lines[i + 1])
                if not _is_url_continuation(url, cont, prev_len):
                    break
                url += cont
                i += 1
                prev_len = len(cont)
            raw_urls.append(url)
        i += 1
    return _merge_unique_urls(raw_urls)


class _BasePtySession:
    """Sdílená správa PTY procesu, WebSocket odběratelů, čtení výstupu a změny velikosti okna."""

    _raw_history_limit: int = 32000
    _clean_history_limit: int = 16000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._raw_output = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        self._last_activity: float = time.monotonic()

    def _is_running_flag(self) -> bool:
        return False

    def _on_pty_chunk(self, raw_text: str, clean_text: str) -> None:
        pass

    def _on_pty_exit(self, exit_code: int | None) -> None:
        pass

    def subscribe(self) -> tuple[str, asyncio.Queue[str | None]]:
        """Přihlásí odběratele k živému PTY proudu a vrátí dosavadní surový výstup."""
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return self._raw_output, queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        """Odhlásí odběratele z živého PTY proudu."""
        self._subscribers.discard(queue)

    def _broadcast(self, data: str | None) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(data)

    def write_raw(self, data: str) -> None:
        """Zapíše surové bajty klávesnice z WebSocket terminálu přímo do PTY."""
        if not data or not self._is_running_flag() or self._master_fd is None:
            return
        self._last_activity = time.monotonic()
        with contextlib.suppress(OSError):
            os.write(self._master_fd, data.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        """Upraví velikost PTY okna podle rozměrů frontendového terminálu."""
        eff_cols = max(40, min(int(cols), 300))
        eff_rows = max(12, min(int(rows), 100))
        self._last_activity = time.monotonic()
        if self._master_fd is not None and fcntl is not None and termios is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(
                    self._master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", eff_rows, eff_cols, 0, 0),
                )
            if (
                self._proc is not None
                and self._proc.poll() is None
                and hasattr(os, "killpg")
                and hasattr(signal, "SIGWINCH")
            ):
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGWINCH)

    def _close_fd(self) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def _terminate_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            if hasattr(os, "killpg") and hasattr(signal, "SIGTERM"):
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(self._proc.pid, signal.SIGTERM)
            with contextlib.suppress(OSError):
                self._proc.terminate()

    def _spawn_pty_process(
        self,
        *,
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> str | None:
        assert pty is not None
        eff_cols = max(40, min(int(cols), 300))
        eff_rows = max(12, min(int(rows), 100))
        master_fd, slave_fd = pty.openpty()
        if fcntl is not None and termios is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(
                    slave_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", eff_rows, eff_cols, 0, 0),
                )
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(cwd),
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            return f"Nepodařilo se spustit agy: {exc}"

        os.close(slave_fd)
        self._proc = proc
        self._master_fd = master_fd
        self._raw_output = ""
        self._last_activity = time.monotonic()
        return None

    async def _read_loop(self) -> None:
        assert self._master_fd is not None
        assert self._proc is not None
        loop = asyncio.get_running_loop()

        try:
            while True:
                try:
                    chunk = await loop.run_in_executor(None, os.read, self._master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                raw_text = chunk.decode("utf-8", errors="replace")
                self._broadcast(raw_text)
                self._raw_output = (self._raw_output + raw_text)[-self._raw_history_limit :]
                clean_text = clean_terminal_output(self._raw_output)[-self._clean_history_limit :]
                self._on_pty_chunk(self._raw_output, clean_text)
                if self._proc.poll() is not None:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        finally:
            rc: int | None = None
            if self._proc is not None:
                rc = self._proc.poll()
                if rc is None:
                    rc = await loop.run_in_executor(None, self._proc.wait)
            self._on_pty_exit(rc)
            self._close_fd()
            self._broadcast(None)


@dataclass
class AuthSessionState:
    """Aktuální stav interaktivního přihlášení Antigravity CLI."""

    running: bool = False
    command: str = ""
    output: str = ""
    urls: list[str] = field(default_factory=list)
    exit_code: int | None = None
    error: str | None = None


class AgyAuthManager(_BasePtySession):
    """Umožňuje provést interaktivní OAuth přihlášení `agy` (URL + kód) přes Web UI."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._state = AuthSessionState()

    def _is_running_flag(self) -> bool:
        return self._state.running

    def _on_pty_chunk(self, raw_history: str, clean_text: str) -> None:
        self._state.output = clean_text
        self._state.urls = _merge_unique_urls(
            [*extract_osc8_urls(raw_history), *extract_urls(clean_text)]
        )

    def _on_pty_exit(self, exit_code: int | None) -> None:
        self._state.exit_code = exit_code
        self._state.running = False

    def get_state(self) -> AuthSessionState:
        """Vrátí aktuální stav interaktivního přihlášení."""
        if self._proc is not None and self._proc.poll() is not None:
            self._state.running = False
            self._state.exit_code = self._proc.returncode
        return AuthSessionState(
            running=self._state.running,
            command=self._state.command,
            output=self._state.output,
            urls=list(self._state.urls),
            exit_code=self._state.exit_code,
            error=self._state.error,
        )

    async def start(
        self,
        mode: str = "interactive",
        *,
        cols: int = 100,
        rows: int = 26,
    ) -> AuthSessionState:
        """Spustí interaktivní `agy` přihlášení v PTY."""
        await self.stop()

        if pty is None:
            self._state = AuthSessionState(
                running=False,
                error="Interaktivní PTY přihlášení v prohlížeči je podporováno v Linux/Docker kontejneru.",
            )
            return self.get_state()

        eff_home = self.settings.effective_home_dir
        eff_ws = self.settings.workspace_dir
        eff_home.mkdir(parents=True, exist_ok=True)
        eff_ws.mkdir(parents=True, exist_ok=True)
        args = [self.settings.agy_bin]
        eff_cols = max(40, min(int(cols), 300))
        eff_rows = max(12, min(int(rows), 100))

        env = self.settings.subprocess_env(gemini_api_key="", home_dir=eff_home)
        env.setdefault("TERM", "xterm-256color")
        env["COLUMNS"] = str(eff_cols)
        env["LINES"] = str(eff_rows)

        err = self._spawn_pty_process(
            args=args, cwd=eff_ws, env=env, cols=eff_cols, rows=eff_rows
        )
        if err is not None:
            self._state = AuthSessionState(running=False, error=err)
            return self.get_state()

        self._state = AuthSessionState(running=True, command=" ".join(args))
        self._reader_task = asyncio.create_task(self._read_loop())
        return self.get_state()

    async def send_input(self, value: str = "") -> AuthSessionState:
        """Pošle vstup (nebo samotný Enter) do běžící relace `agy`."""
        if not self._state.running or self._master_fd is None:
            raise RuntimeError("Přihlašovací proces právě neběží.")
        payload = (value.rstrip("\r\n") + "\r").encode("utf-8")
        os.write(self._master_fd, payload)
        await asyncio.sleep(0.5)
        return self.get_state()

    async def stop(self) -> None:
        """Ukončí běžící přihlašovací relaci."""
        await self._terminate_proc()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._close_fd()
        self._proc = None
        self._reader_task = None
        self._raw_output = ""
        self._state.running = False
        self._broadcast(None)


@dataclass
class TerminalSessionState:
    """Aktuální stav samostatného interaktivního `agy` terminálu ve Web UI."""

    running: bool = False
    context_mode: str = "auto"
    command: str = ""
    output: str = ""
    exit_code: int | None = None
    error: str | None = None


class AgyTerminalManager(_BasePtySession):
    """Spouští samostatný interaktivní `agy` TUI terminál ve workspace s idle watchdogem."""

    _raw_history_limit = 64000
    _clean_history_limit = 16000

    def __init__(self, settings: Settings, *, idle_timeout_seconds: int = 1800) -> None:
        super().__init__(settings)
        self.idle_timeout_seconds = max(1, int(idle_timeout_seconds))
        self._state = TerminalSessionState()
        self._idle_task: asyncio.Task[None] | None = None

    def _is_running_flag(self) -> bool:
        return self._state.running

    def _on_pty_chunk(self, raw_history: str, clean_text: str) -> None:
        self._state.output = clean_text

    def _on_pty_exit(self, exit_code: int | None) -> None:
        self._state.exit_code = exit_code
        self._state.running = False
        idle = self._idle_task
        self._idle_task = None
        if idle is not None and idle is not asyncio.current_task() and not idle.done():
            idle.cancel()

    def get_state(self) -> TerminalSessionState:
        """Vrátí aktuální stav `agy` terminálu."""
        if self._proc is not None and self._proc.poll() is not None:
            self._state.running = False
            self._state.exit_code = self._proc.returncode
        return TerminalSessionState(
            running=self._state.running,
            context_mode=self._state.context_mode,
            command=self._state.command,
            output=self._state.output,
            exit_code=self._state.exit_code,
            error=self._state.error,
        )

    async def _idle_watchdog(self) -> None:
        poll_sec = min(30.0, max(0.25, self.idle_timeout_seconds / 2))
        try:
            while self._state.running:
                await asyncio.sleep(poll_sec)
                if not self._state.running:
                    break
                if (time.monotonic() - self._last_activity) >= self.idle_timeout_seconds:
                    await self.stop()
                    break
        except asyncio.CancelledError:
            return

    async def start(
        self,
        *,
        context_mode: str = "auto",
        home_dir: Path | None = None,
        model: str = "",
        gemini_api_key: str = "",
        cols: int = 120,
        rows: int = 32,
    ) -> TerminalSessionState:
        """Spustí interaktivní `agy` ve workspace s nastaveným profilem, MCP a modelem."""
        await self.stop()

        if pty is None:
            self._state = TerminalSessionState(
                running=False,
                context_mode=context_mode,
                error="Interaktivní terminál agy v prohlížeči je podporován v Linux/Docker kontejneru.",
            )
            return self.get_state()

        eff_home = home_dir or self.settings.effective_home_dir
        eff_ws = self.settings.workspace_dir
        eff_home.mkdir(parents=True, exist_ok=True)
        eff_ws.mkdir(parents=True, exist_ok=True)

        args = [self.settings.agy_bin]
        clean_model = (model or "").strip()
        if clean_model:
            args.extend(["--model", clean_model])

        eff_cols = max(40, min(int(cols), 300))
        eff_rows = max(12, min(int(rows), 100))

        env = self.settings.subprocess_env(gemini_api_key=gemini_api_key, home_dir=eff_home)
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["COLUMNS"] = str(eff_cols)
        env["LINES"] = str(eff_rows)

        err = self._spawn_pty_process(
            args=args, cwd=eff_ws, env=env, cols=eff_cols, rows=eff_rows
        )
        if err is not None:
            self._state = TerminalSessionState(
                running=False, context_mode=context_mode, error=err
            )
            return self.get_state()

        self._state = TerminalSessionState(
            running=True,
            context_mode=context_mode,
            command=" ".join(args),
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._idle_task = asyncio.create_task(self._idle_watchdog())
        return self.get_state()

    async def stop(self) -> None:
        """Ukončí běžící interaktivní `agy` terminál."""
        current = asyncio.current_task()
        idle = self._idle_task
        self._idle_task = None
        if idle is not None and idle is not current and not idle.done():
            idle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle

        await self._terminate_proc()
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not current and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader

        self._close_fd()
        self._proc = None
        self._raw_output = ""
        self._state.running = False
        self._broadcast(None)
