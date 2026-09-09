from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import httpx
from websockets.sync.client import connect


class ManualVerificationError(RuntimeError):
    pass


@dataclass
class BrowserSession:
    port: int
    process: subprocess.Popen
    host: str
    profile: Path | None = None


_sessions: dict[int, BrowserSession] = {}
_lock = Lock()


def _edge_path() -> str:
    candidates = [
        shutil.which("msedge.exe"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise ManualVerificationError("未找到 Microsoft Edge，无法打开专用验证窗口")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def open_verification(site_id: int, url: str, profile_root: Path) -> dict[str, object]:
    if not sys.platform.startswith("win"):
        raise ManualVerificationError("人工验证窗口目前仅支持 Windows 桌面版")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ManualVerificationError("平台验证地址无效")
    port = _free_port()
    profile = (profile_root / str(site_id)).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            _edge_path(), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
            f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check", url,
        ],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        process.terminate()
        raise ManualVerificationError("验证窗口启动失败，请检查 Edge 是否可正常运行")
    with _lock:
        previous = _sessions.pop(site_id, None)
        _sessions[site_id] = BrowserSession(port, process, host, profile)
    if previous and previous.process.poll() is None:
        previous.process.terminate()
    return {"opened": True, "message": "验证窗口已打开，请完成验证后返回系统点击“验证完成”"}


def _cookie_header(cookies: list[dict[str, object]], host: str) -> str:
    usable = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain and (host == domain or host.endswith(f".{domain}")):
            usable.append(f"{cookie.get('name')}={cookie.get('value')}")
    return "; ".join(usable)


def browser_request(site_id: int, url: str, method: str = "GET", payload: dict | None = None) -> dict:
    """Run a same-origin public request inside an open verified Edge page."""
    with _lock:
        session = _sessions.get(site_id)
    if not session or session.process.poll() is not None:
        raise ManualVerificationError("华能专用采集窗口已关闭，请在“平台与账号”重新打开后采集")
    if (urlparse(url).hostname or "").lower() != session.host:
        raise ManualVerificationError("浏览器采集请求地址与验证平台不一致")
    targets = httpx.get(f"http://127.0.0.1:{session.port}/json", timeout=3).json()
    target = next((item for item in targets if item.get("type") == "page"
                   and item.get("webSocketDebuggerUrl")
                   and (urlparse(item.get("url", "")).hostname or "").lower() == session.host), None)
    if not target:
        raise ManualVerificationError("未找到华能公告页面，请保持专用窗口打开")
    options = {"method": method.upper(), "credentials": "include", "headers": {"Content-Type": "application/json"}}
    if payload is not None:
        options["body"] = json.dumps(payload, ensure_ascii=False)
    expression = (
        "(async()=>{const r=await fetch(" + json.dumps(url) + "," + json.dumps(options, ensure_ascii=False)
        + ");return JSON.stringify({status:r.status,text:await r.text()})})()"
    )
    with connect(target["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{session.port}", open_timeout=5) as websocket:
        websocket.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            response = json.loads(websocket.recv(timeout=30))
            if response.get("id") != 1:
                continue
            if response.get("exceptionDetails"):
                raise ManualVerificationError("华能浏览器请求执行失败，请刷新公告页后重试")
            value = (((response.get("result") or {}).get("result") or {}).get("value"))
            result = json.loads(value or "{}")
            if result.get("status") != 200:
                raise ManualVerificationError(f"华能浏览器请求返回 {result.get('status') or '未知状态'}")
            return json.loads(result.get("text") or "{}")


def complete_verification(site_id: int) -> str:
    with _lock:
        session = _sessions.get(site_id)
    if not session or session.process.poll() is not None:
        raise ManualVerificationError("没有正在运行的验证窗口，请先点击“打开人工验证”")
    try:
        targets = httpx.get(f"http://127.0.0.1:{session.port}/json", timeout=3).json()
        target = next((item for item in targets if item.get("type") == "page"
                       and item.get("webSocketDebuggerUrl")
                       and (urlparse(item.get("url", "")).hostname or "").lower() == session.host), None)
        if not target:
            raise ManualVerificationError("未找到验证页面，请保持验证窗口打开")
        with connect(target["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{session.port}", open_timeout=5) as websocket:
            websocket.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
            websocket.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "navigator.userAgent", "returnByValue": True}}))
            # Capture the user's actual public page for adapter diagnosis. No
            # cookies, localStorage, or form values are included in this file.
            websocket.send(json.dumps({"id": 3, "method": "Runtime.evaluate", "params": {
                "expression": "JSON.stringify({title:document.title,url:location.origin+location.pathname,links:Array.from(document.querySelectorAll('a')).map(a=>({text:a.innerText,url:a.href})),scripts:Array.from(document.scripts).filter(s=>s.src).map(s=>s.src),text:document.body?document.body.innerText.slice(0,100000):'',resources:performance.getEntriesByType('resource').map(r=>{let u=new URL(r.name,location.href);return u.origin+u.pathname})})",
                "returnByValue": True}}))
            cookies = []
            user_agent = ""
            received = set()
            while True:
                response = json.loads(websocket.recv(timeout=5))
                if response.get("id") == 1:
                    cookies = (response.get("result") or {}).get("cookies") or []
                    received.add(1)
                elif response.get("id") == 2:
                    user_agent = str((((response.get("result") or {}).get("result") or {}).get("value") or ""))
                    received.add(2)
                elif response.get("id") == 3:
                    snapshot = (((response.get("result") or {}).get("result") or {}).get("value"))
                    if session.profile and isinstance(snapshot, str):
                        try:
                            (session.profile / 'last-public-page.json').write_text(snapshot, encoding='utf-8')
                        except OSError:
                            pass  # Diagnostics must not prevent session capture.
                    received.add(3)
                if received == {1, 2, 3}:
                    break
        header = _cookie_header(cookies, session.host)
        if not header:
            raise ManualVerificationError("未读取到平台会话，请在验证页面完成操作后重试")
        if user_agent:
            encoded = base64.urlsafe_b64encode(user_agent.encode("utf-8")).decode("ascii")
            header = f"{header}; __scout_user_agent={encoded}"
        return header
    except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
        raise ManualVerificationError(f"读取验证会话失败：{exc}") from exc


def close_verification(site_id: int) -> None:
    with _lock:
        session = _sessions.pop(site_id, None)
    if not session or session.process.poll() is not None:
        return
    try:
        version = httpx.get(f"http://127.0.0.1:{session.port}/json/version", timeout=2).json()
        with connect(version["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{session.port}", open_timeout=2) as websocket:
            websocket.send(json.dumps({"id": 1, "method": "Browser.close"}))
    except (httpx.HTTPError, KeyError, ValueError, OSError, TimeoutError):
        session.process.terminate()
