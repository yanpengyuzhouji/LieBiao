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
from urllib.parse import urlencode, urlparse

import httpx
from websockets.sync.client import connect


class ManualVerificationError(RuntimeError):
    pass


@dataclass
class BrowserSession:
    port: int
    process: subprocess.Popen | None
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


def _profile_is_locked(profile: Path) -> bool:
    """Return whether Edge is likely already using this profile directory."""
    return any((profile / name).exists() for name in ("lockfile", "SingletonLock", "SingletonSocket"))


def _process_alive(process: subprocess.Popen | None) -> bool:
    return process is not None and process.poll() is None


def _attach_session(site_id: int, port: int, expected_host: str | None = None) -> BrowserSession:
    """Reattach to a still-running Edge instance after the API process restarted."""
    try:
        targets = httpx.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ManualVerificationError("专用验证窗口已关闭，请重新打开人工验证") from exc
    target = next((item for item in targets if item.get("type") == "page"
                   and item.get("webSocketDebuggerUrl")
                   and (urlparse(item.get("url", "")).hostname or "").lower()), None)
    if not target:
        raise ManualVerificationError("未找到验证页面，请保持专用验证窗口打开")
    host = (urlparse(target.get("url", "")).hostname or "").lower()
    if expected_host and host != expected_host.lower():
        raise ManualVerificationError("浏览器验证页面与平台不一致，请打开正确的平台页面")
    session = BrowserSession(port=port, process=None, host=host)
    with _lock:
        _sessions[site_id] = session
    return session


def open_verification(site_id: int, url: str, profile_root: Path) -> dict[str, object]:
    if not sys.platform.startswith("win"):
        raise ManualVerificationError("人工验证窗口目前仅支持 Windows 桌面版")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ManualVerificationError("平台验证地址无效")
    port = _free_port()
    profile = (profile_root / str(site_id)).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    # If an older Edge process still owns the stable profile (for example after
    # an application update), launching with the same profile can hand the URL
    # to that process and exit without exposing the new debug port. Use an
    # isolated profile for this verification attempt instead.
    if _profile_is_locked(profile):
        profile = (profile_root / f"{site_id}-{int(time.time())}").resolve()
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
    if previous and _process_alive(previous.process):
        previous.process.terminate()
    return {
        "opened": True,
        "port": port,
        "message": "验证窗口已打开，请完成验证后返回系统点击“验证完成”",
    }


def _cookie_header(cookies: list[dict[str, object]], host: str) -> str:
    usable = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain and (host == domain or host.endswith(f".{domain}")):
            usable.append(f"{cookie.get('name')}={cookie.get('value')}")
    return "; ".join(usable)


def browser_request(site_id: int, url: str, method: str = "GET", payload: dict | None = None,
                    port: int | None = None, form_encoded: bool = False,
                    parse_json: bool = True,
                    storage_query: dict[str, str] | None = None) -> dict | str:
    """Run a same-origin public request inside an open verified Edge page."""
    with _lock:
        session = _sessions.get(site_id)
    expected_host = (urlparse(url).hostname or "").lower()
    if not session and port:
        session = _attach_session(site_id, port, expected_host)
    if not session or not _process_alive(session.process) and session.process is not None:
        raise ManualVerificationError("专用采集窗口已关闭，请在“平台与账号”重新打开后采集")
    if expected_host != session.host:
        raise ManualVerificationError("浏览器采集请求地址与验证平台不一致")
    targets = httpx.get(f"http://127.0.0.1:{session.port}/json", timeout=3).json()
    target = next((item for item in targets if item.get("type") == "page"
                   and item.get("webSocketDebuggerUrl")
                   and (urlparse(item.get("url", "")).hostname or "").lower() == session.host), None)
    if not target:
        raise ManualVerificationError("未找到平台公告页面，请保持专用窗口打开")
    content_type = "application/x-www-form-urlencoded;charset=UTF-8" if form_encoded else "application/json"
    options = {"method": method.upper(), "credentials": "include", "headers": {"Content-Type": content_type}}
    if payload is not None:
        options["body"] = urlencode(payload, doseq=True) if form_encoded else json.dumps(payload, ensure_ascii=False)
    request_url = json.dumps(url)
    if storage_query:
        request_url = (
            "(()=>{const u=new URL(" + request_url + ");const q="
            + json.dumps(storage_query, ensure_ascii=False)
            + ";for(const [p,k] of Object.entries(q)){const v=localStorage.getItem(k);if(v)u.searchParams.set(p,v)}"
            + "return u.toString()})()"
        )
    expression = (
        "(async()=>{const r=await fetch(" + request_url + "," + json.dumps(options, ensure_ascii=False)
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
                raise ManualVerificationError("浏览器采集请求执行失败，请刷新公告页后重试")
            value = (((response.get("result") or {}).get("result") or {}).get("value"))
            result = json.loads(value or "{}")
            if result.get("status") != 200:
                raise ManualVerificationError(f"浏览器采集请求返回 {result.get('status') or '未知状态'}")
            text = result.get("text") or ""
            return json.loads(text or "{}") if parse_json else text


def complete_verification(site_id: int, port: int | None = None) -> str:
    with _lock:
        session = _sessions.get(site_id)
    if not session and port:
        session = _attach_session(site_id, port)
    if not session or not _process_alive(session.process) and session.process is not None:
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
        header = f"{header}; __scout_browser_port={session.port}"
        return header
    except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
        raise ManualVerificationError(f"读取验证会话失败：{exc}") from exc


def close_verification(site_id: int) -> None:
    with _lock:
        session = _sessions.pop(site_id, None)
    if not session:
        return
    try:
        version = httpx.get(f"http://127.0.0.1:{session.port}/json/version", timeout=2).json()
        with connect(version["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{session.port}", open_timeout=2) as websocket:
            websocket.send(json.dumps({"id": 1, "method": "Browser.close"}))
    except (httpx.HTTPError, KeyError, ValueError, OSError, TimeoutError):
        if _process_alive(session.process):
            session.process.terminate()


def browser_session_port(session_cookie: str | None) -> int | None:
    """Read the persisted local Edge debug port from a session credential."""
    if not session_cookie:
        return None
    for item in session_cookie.split(";"):
        if "=" not in item:
            continue
        key, value = item.strip().split("=", 1)
        if key != "__scout_browser_port":
            continue
        try:
            port = int(value)
        except ValueError:
            return None
        return port if 1 <= port <= 65535 else None
    return None
