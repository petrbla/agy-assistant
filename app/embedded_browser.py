from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets


def find_chromium_binary() -> str | None:
    """Najde spustitelnou binárku Chromium/Chrome v systému."""
    for name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
    ):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class EmbeddedBrowserState:
    """Aktuální stav vestavěného OAuth prohlížeče."""

    available: bool = False
    running: bool = False
    current_url: str = ""
    callback_reached: bool = False
    callback_url: str = ""
    error: str | None = None


class EmbeddedBrowserManager:
    """Lehký headless Chromium ovládaný přes CDP a streamovaný do Web UI přes WebSocket."""

    VIEWPORT_WIDTH = 1024
    VIEWPORT_HEIGHT = 720

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._cdp_ws: Any = None
        self._cdp_task: asyncio.Task[None] | None = None
        self._cdp_lock = asyncio.Lock()
        self._cmd_id = 0
        self._state = EmbeddedBrowserState(available=self.is_available())
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._callback_port: int | None = None
        self._callback_path: str = "/callback"
        self._expected_state: str = ""
        self._last_frame: str = ""
        self._callback_task: asyncio.Task[None] | None = None

    @staticmethod
    def is_available() -> bool:
        """Vrátí True, pokud je v systému dostupný Chromium/Chrome."""
        return find_chromium_binary() is not None

    def get_last_frame(self) -> str:
        """Vrátí poslední přijatý JPEG snímek obrazovky (base64)."""
        return self._last_frame

    def get_state(self) -> EmbeddedBrowserState:
        """Vrátí aktuální stav relace vestavěného prohlížeče."""
        if self._proc is not None and self._proc.returncode is not None:
            self._state.running = False
        return EmbeddedBrowserState(
            available=self.is_available(),
            running=self._state.running,
            current_url=self._state.current_url,
            callback_reached=self._state.callback_reached,
            callback_url=self._state.callback_url,
            error=self._state.error,
        )

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        """Přihlásí WebSocket klienta k příjmu snímků obrazovky a událostí."""
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=16)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        """Odhlásí WebSocket klienta."""
        self._subscribers.discard(queue)

    def _broadcast(self, payload: dict[str, Any] | None) -> None:
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def start(
        self,
        url: str,
        *,
        callback_port: int | None = None,
        callback_path: str = "/callback",
        expected_state: str = "",
    ) -> EmbeddedBrowserState:
        """Spustí izolovaný headless Chromium na zadané OAuth URL."""
        await self.stop()
        self._last_frame = ""

        parsed = urlparse((url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Neplatná URL pro vestavěný prohlížeč.")

        binary = find_chromium_binary()
        if not binary:
            self._state = EmbeddedBrowserState(
                available=False,
                running=False,
                error="Vestavěný prohlížeč (Chromium) není v tomto prostředí nainstalován.",
            )
            return self.get_state()

        self._callback_port = callback_port
        self._callback_path = callback_path or "/callback"
        self._expected_state = expected_state or ""

        cdp_port = _find_free_port()
        self._temp_dir = tempfile.TemporaryDirectory(prefix="agy-oauth-browser-")
        args = [
            binary,
            "--headless=new",
            f"--remote-debugging-port={cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={self._temp_dir.name}",
            f"--window-size={self.VIEWPORT_WIDTH},{self.VIEWPORT_HEIGHT}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-networking",
            "--disable-sync",
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ]
        if os.name != "nt":
            args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
        args.append(url.strip())

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            await self.stop()
            self._state = EmbeddedBrowserState(
                available=True,
                running=False,
                error=f"Nepodařilo se spustit Chromium: {exc}",
            )
            return self.get_state()

        ws_url = await self._wait_for_cdp_ws_url(cdp_port)
        if not ws_url:
            await self.stop()
            self._state = EmbeddedBrowserState(
                available=True,
                running=False,
                error="Vestavěný prohlížeč neodpověděl na CDP portu.",
            )
            return self.get_state()

        self._state = EmbeddedBrowserState(
            available=True,
            running=True,
            current_url=url.strip(),
        )
        self._cdp_task = asyncio.create_task(self._run_cdp_loop(ws_url))
        return self.get_state()

    async def _wait_for_cdp_ws_url(self, port: int, timeout: float = 8.0) -> str | None:
        deadline = asyncio.get_running_loop().time() + timeout
        list_url = f"http://127.0.0.1:{port}/json/list"
        async with httpx.AsyncClient(timeout=1.5) as client:
            while asyncio.get_running_loop().time() < deadline:
                if self._proc is not None and self._proc.returncode is not None:
                    return None
                with contextlib.suppress(Exception):
                    resp = await client.get(list_url)
                    if resp.status_code == 200:
                        targets = resp.json()
                        if isinstance(targets, list):
                            for target in targets:
                                if (
                                    isinstance(target, dict)
                                    and target.get("type") == "page"
                                    and target.get("webSocketDebuggerUrl")
                                ):
                                    return str(target["webSocketDebuggerUrl"])
                await asyncio.sleep(0.2)
        return None

    async def _send_cdp(self, method: str, params: dict[str, Any] | None = None) -> None:
        ws = self._cdp_ws
        if ws is None:
            return
        async with self._cdp_lock:
            self._cmd_id += 1
            payload = {"id": self._cmd_id, "method": method, "params": params or {}}
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(payload))

    def _is_matching_callback_url(self, candidate: str) -> bool:
        if not candidate or self._callback_port is None:
            return False
        parsed = urlparse(candidate)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            return False
        try:
            port = parsed.port
        except ValueError:
            return False
        if port != self._callback_port:
            return False
        if (parsed.path or "/") != (self._callback_path or "/callback"):
            return False
        query = parsed.query or ""
        return "code=" in query or "error=" in query

    async def _trigger_callback_reached(self, url: str, *, delay: float = 0.0) -> None:
        if self._state.callback_reached:
            return
        self._state.callback_reached = True
        self._state.callback_url = url
        if delay > 0:
            await asyncio.sleep(delay)
        self._broadcast({"type": "callback_reached", "url": url})

    async def _run_cdp_loop(self, ws_url: str) -> None:
        try:
            async with websockets.connect(ws_url, max_size=12 * 1024 * 1024) as ws:
                self._cdp_ws = ws
                await self._send_cdp("Page.enable")
                await self._send_cdp("Network.enable")
                await self._send_cdp("Runtime.enable")
                await self._send_cdp(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": self.VIEWPORT_WIDTH,
                        "height": self.VIEWPORT_HEIGHT,
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
                await self._send_cdp(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": 75,
                        "maxWidth": self.VIEWPORT_WIDTH,
                        "maxHeight": self.VIEWPORT_HEIGHT,
                        "everyNthFrame": 1,
                    },
                )

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    method = msg.get("method")
                    params = msg.get("params") or {}

                    if method == "Page.screencastFrame":
                        session_id = params.get("sessionId")
                        if session_id is not None:
                            await self._send_cdp(
                                "Page.screencastFrameAck", {"sessionId": session_id}
                            )
                        data = params.get("data")
                        if isinstance(data, str) and data:
                            self._last_frame = data
                            self._broadcast(
                                {
                                    "type": "frame",
                                    "data": data,
                                    "url": self._state.current_url,
                                }
                            )
                    elif method == "Network.requestWillBeSent":
                        req_url = str((params.get("request") or {}).get("url") or "").strip()
                        if (
                            req_url
                            and not self._state.callback_reached
                            and self._is_matching_callback_url(req_url)
                        ):
                            self._state.current_url = req_url
                            self._broadcast({"type": "url", "url": req_url})
                            self._callback_task = asyncio.create_task(
                                self._trigger_callback_reached(req_url, delay=0.8)
                            )
                    elif method in ("Page.frameNavigated", "Page.navigatedWithinDocument"):
                        frame = params.get("frame") or {}
                        new_url = str(
                            params.get("url") or frame.get("url") or ""
                        ).strip()
                        if new_url and not new_url.startswith("about:"):
                            self._state.current_url = new_url
                            self._broadcast({"type": "url", "url": new_url})
                            if (
                                not self._state.callback_reached
                                and self._is_matching_callback_url(new_url)
                            ):
                                await self._trigger_callback_reached(new_url, delay=0.0)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._state.running and not self._state.callback_reached:
                self._state.error = f"Spojení s vestavěným prohlížečem bylo ukončeno: {exc}"
        finally:
            self._cdp_ws = None
            self._state.running = False
            self._broadcast(None)

    async def handle_client_event(self, msg: dict[str, Any]) -> None:
        """Zpracuje událost myši, klávesnice nebo navigace z Web UI."""
        if not self._state.running or self._cdp_ws is None:
            return
        ev_type = str(msg.get("type") or "").strip()

        if ev_type == "mouse":
            action = str(msg.get("action") or "mouseMoved")
            if action not in {"mouseMoved", "mousePressed", "mouseReleased", "mouseWheel"}:
                return
            x = max(0, min(int(msg.get("x", 0)), self.VIEWPORT_WIDTH))
            y = max(0, min(int(msg.get("y", 0)), self.VIEWPORT_HEIGHT))
            params: dict[str, Any] = {"type": action, "x": x, "y": y}
            if action in ("mousePressed", "mouseReleased"):
                button = str(msg.get("button") or "left")
                if button not in {"left", "middle", "right"}:
                    button = "left"
                params["button"] = button
                params["clickCount"] = max(1, min(int(msg.get("clickCount", 1)), 3))
            elif action == "mouseWheel":
                params["deltaX"] = int(msg.get("deltaX", 0))
                params["deltaY"] = int(msg.get("deltaY", 0))
            await self._send_cdp("Input.dispatchMouseEvent", params)

        elif ev_type == "key":
            action = str(msg.get("action") or "keyDown")
            if action not in {"keyDown", "keyUp", "char"}:
                return
            params = {"type": action}
            for field_name in ("key", "code", "text"):
                val = msg.get(field_name)
                if isinstance(val, str) and val:
                    params[field_name] = val[:32]
            if action == "keyDown" and params.get("key") == "Enter" and "text" not in params:
                params["text"] = "\r"
                params["unmodifiedText"] = "\r"
            if isinstance(msg.get("windowsVirtualKeyCode"), int):
                params["windowsVirtualKeyCode"] = int(msg["windowsVirtualKeyCode"])
            if isinstance(msg.get("modifiers"), int):
                params["modifiers"] = int(msg["modifiers"])
            await self._send_cdp("Input.dispatchKeyEvent", params)

        elif ev_type == "text":
            text = msg.get("text")
            if isinstance(text, str) and 0 < len(text) <= 4000:
                await self._send_cdp("Input.insertText", {"text": text})

        elif ev_type == "back":
            await self._send_cdp("Runtime.evaluate", {"expression": "window.history.back()"})

        elif ev_type == "reload":
            await self._send_cdp("Page.reload")

    async def stop(self) -> None:
        """Bezpečně ukončí Chromium proces a smaže dočasný profil."""
        self._state.running = False
        self._last_frame = ""
        if self._callback_task is not None and not self._callback_task.done():
            self._callback_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._callback_task
        self._callback_task = None
        if self._cdp_task is not None and not self._cdp_task.done():
            self._cdp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cdp_task
        self._cdp_task = None
        self._cdp_ws = None

        if self._proc is not None:
            if self._proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError, OSError):
                        self._proc.kill()
                    with contextlib.suppress(Exception):
                        await self._proc.wait()
            self._proc = None

        if self._temp_dir is not None:
            with contextlib.suppress(OSError):
                self._temp_dir.cleanup()
            self._temp_dir = None

        self._broadcast(None)
