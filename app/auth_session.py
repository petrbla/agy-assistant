from __future__ import annotations

import asyncio
import contextlib
import os
import re
import struct
import subprocess
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
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")


def clean_terminal_output(raw: str) -> str:
    """Odstraní ANSI sekvence z výstupu terminálu."""
    cleaned = _ANSI_RE.sub("", raw)
    return cleaned.replace("\r\n", "\n").replace("\r", "\n")


def extract_urls(text: str) -> list[str]:
    """Najde unikátní URL odkazy v textu při zachování pořadí."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.findall(text):
        url = match.rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


@dataclass
class AuthSessionState:
    """Aktuální stav interaktivního přihlášení Antigravity CLI."""

    running: bool = False
    command: str = ""
    output: str = ""
    urls: list[str] = field(default_factory=list)
    exit_code: int | None = None
    error: str | None = None


class AgyAuthManager:
    """Umožňuje provést interaktivní OAuth přihlášení `agy` (URL + kód) přes Web UI."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._state = AuthSessionState()
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._reader_task: asyncio.Task[None] | None = None

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

    async def start(self, mode: str = "interactive") -> AuthSessionState:
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

        master_fd, slave_fd = pty.openpty()
        if fcntl is not None and termios is not None:
            with contextlib.suppress(OSError):
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 400, 0, 0))
        env = self.settings.subprocess_env(gemini_api_key="", home_dir=eff_home)
        env.setdefault("TERM", "xterm-256color")

        try:
            proc = subprocess.Popen(
                args,
                cwd=str(eff_ws),
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
            self._state = AuthSessionState(
                running=False, error=f"Nepodařilo se spustit agy: {exc}"
            )
            return self.get_state()

        os.close(slave_fd)
        self._proc = proc
        self._master_fd = master_fd
        self._state = AuthSessionState(running=True, command=" ".join(args))
        self._reader_task = asyncio.create_task(self._read_loop())
        return self.get_state()

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
                text = clean_terminal_output(chunk.decode("utf-8", errors="replace"))
                self._state.output = (self._state.output + text)[-16000:]
                self._state.urls = extract_urls(self._state.output)
                if self._proc.poll() is not None:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return
        finally:
            if self._proc is not None:
                rc = self._proc.poll()
                if rc is None:
                    rc = await loop.run_in_executor(None, self._proc.wait)
                self._state.exit_code = rc
            self._state.running = False
            self._close_fd()

    async def send_input(self, value: str = "") -> AuthSessionState:
        """Pošle vstup (nebo samotný Enter) do běžící relace `agy`."""
        if not self._state.running or self._master_fd is None:
            raise RuntimeError("Přihlašovací proces právě neběží.")
        payload = (value.rstrip("\r\n") + "\r").encode("utf-8")
        os.write(self._master_fd, payload)
        await asyncio.sleep(0.5)
        return self.get_state()

    def _close_fd(self) -> None:
        if self._master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._master_fd)
            self._master_fd = None

    async def stop(self) -> None:
        """Ukončí běžící přihlašovací relaci."""
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(OSError):
                self._proc.terminate()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._close_fd()
        self._proc = None
        self._reader_task = None
        self._state.running = False
