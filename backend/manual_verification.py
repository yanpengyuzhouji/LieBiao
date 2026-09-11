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
from websockets.exceptions import WebSocketException


class ManualVerificationError(RuntimeError):
    pass


@dataclass
class BrowserSession:
    port: int
    process: subprocess.Popen | None
    host: str
    profile: Path | None = None
    url: str | None = None


_sessions: dict[int, BrowserSession] = {}
_lock = Lock()


def _session_metadata_path(profile: Path) -> Path:
    return profile / "session.json"


def _save_session_metadata(session: BrowserSession) -> None:
    if not session.profile:
        return
    try:
        session.profile.mkdir(parents=True, exist_ok=True)
        _session_metadata_path(session.profile).write_text(
            json.dumps({
                "port": session.port,
                "host": session.host,
                "url": session.url,
                "profile": str(session.profile),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        # Session metadata is a recovery aid. It must never break verification.
        pass


def _load_session_metadata(profile: Path) -> dict[str, object]:
    try:
        value = json.loads(_session_metadata_path(profile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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


def _attach_session(site_id: int, port: int, expected_host: str | None = None,
                    profile: Path | None = None, url: str | None = None) -> BrowserSession:
    """Reattach to a still-running Edge instance after the API process restarted."""
    try:
        targets = httpx.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise ManualVerificationError("专用验证窗口已关闭，请重新打开人工验证") from exc
    target = next((item for item in targets if item.get("type") == "page"
                   and item.get("webSocketDebuggerUrl")
                   and (urlparse(item.get("url", "")).hostname or "").lower()
                   and (not expected_host or
                        (urlparse(item.get("url", "")).hostname or "").lower() == expected_host.lower())), None)
    if not target:
        raise ManualVerificationError("未找到验证页面，请保持专用验证窗口打开")
    host = (urlparse(target.get("url", "")).hostname or "").lower()
    session = BrowserSession(port=port, process=None, host=host, profile=profile, url=url)
    with _lock:
        _sessions[site_id] = session
    _save_session_metadata(session)
    return session


def _live_session(site_id: int, port: int | None = None, expected_host: str | None = None,
                  closed_message: str = "专用采集窗口已关闭，请在“平台与账号”重新打开后采集") -> BrowserSession:
    """Return a usable session, treating the debug port rather than the Popen handle as truth.

    On Windows ``msedge.exe`` is frequently only a launcher: it spawns the real browser
    process and exits immediately, so the handle we keep goes stale while the window and
    its debug port stay perfectly usable. Judging liveness by that handle rejects a live
    window, so fall back to reattaching on the port the session was opened with.
    """
    with _lock:
        session = _sessions.get(site_id)
    if session is not None and session.process is not None and not _process_alive(session.process):
        expected_host = expected_host or session.host
        port = port or session.port
        session = None
    if session is None and port:
        session = _attach_session(site_id, port, expected_host)
    if session is None:
        raise ManualVerificationError(closed_message)
    return session


def _edge_args(port: int, profile: Path, url: str, hidden: bool = False) -> list[str]:
    args = [
        _edge_path(), f"--remote-debugging-port={port}", "--remote-allow-origins=*",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
    ]
    if hidden:
        # Keep a normal (non-headless) browser so platform anti-bot checks see
        # the same verified profile, but keep its window out of the user's way.
        args.extend(["--start-minimized", "--window-position=-32000,-32000"])
    args.append(url)
    return args


def _wait_for_debug_port(port: int, process: subprocess.Popen | None = None, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=0.5).status_code == 200:
                return
        except (httpx.HTTPError, OSError):
            pass
        if process is not None and process.poll() is not None:
            break
        time.sleep(0.2)
    if process is not None and _process_alive(process):
        process.terminate()
    raise ManualVerificationError("验证窗口启动失败，请检查 Edge 是否可正常运行")


def _launch_session(site_id: int, url: str, profile: Path, port: int | None = None,
                    hidden: bool = False) -> BrowserSession:
    profile.mkdir(parents=True, exist_ok=True)
    if _profile_is_locked(profile):
        raise ManualVerificationError("专用浏览器 Profile 正被其他窗口占用，请关闭旧验证窗口后重试")
    port = port or _free_port()
    process = subprocess.Popen(
        _edge_args(port, profile, url, hidden=hidden),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        _wait_for_debug_port(port, process)
    except ManualVerificationError:
        if _process_alive(process):
            process.terminate()
        raise
    host = (urlparse(url).hostname or "").lower()
    session = BrowserSession(port, process, host, profile, url)
    with _lock:
        previous = _sessions.pop(site_id, None)
        _sessions[site_id] = session
    _save_session_metadata(session)
    if previous and _process_alive(previous.process):
        previous.process.terminate()
    return session


def _profile_for(profile_root: Path, site_id: int) -> Path:
    root = profile_root.resolve()
    profile = (root / str(site_id)).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def ensure_persistent_session(site_id: int, url: str, profile_root: Path,
                              port: int | None = None) -> BrowserSession:
    """Reconnect or quietly relaunch the verified Edge profile for automation."""
    profile = _profile_for(profile_root, site_id)
    metadata = _load_session_metadata(profile)
    saved_port = metadata.get("port")
    if not isinstance(saved_port, int) or not 1 <= saved_port <= 65535:
        saved_port = None
    candidates = []
    for candidate in (port, saved_port):
        if isinstance(candidate, int) and candidate not in candidates:
            candidates.append(candidate)
    expected_host = (urlparse(url).hostname or "").lower()
    for candidate in candidates:
        try:
            return _attach_session(site_id, candidate, expected_host, profile, url)
        except ManualVerificationError:
            continue
    # Reuse the persisted port whenever possible. This lets the account
    # credential remain stable across application restarts.
    launch_port = candidates[0] if candidates else None
    session = _launch_session(site_id, url, profile, launch_port, hidden=True)
    return session


def open_verification(site_id: int, url: str, profile_root: Path) -> dict[str, object]:
    if not sys.platform.startswith("win"):
        raise ManualVerificationError("人工验证窗口目前仅支持 Windows 桌面版")
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ManualVerificationError("平台验证地址无效")
    profile = _profile_for(profile_root, site_id)
    metadata = _load_session_metadata(profile)
    saved_port = metadata.get("port")
    if isinstance(saved_port, int) and 1 <= saved_port <= 65535:
        try:
            _attach_session(site_id, saved_port, host, profile, url)
            return {"opened": True, "port": saved_port, "message": "已连接正在运行的专用验证窗口，请完成验证后返回系统点击“验证完成”"}
        except ManualVerificationError:
            pass
    _launch_session(site_id, url, profile, hidden=False)
    return {
        "opened": True,
        "port": _sessions[site_id].port,
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
                    storage_query: dict[str, str] | None = None,
                    authorization_cookie: str | None = None) -> dict | str:
    """Run a same-origin public request inside an open verified Edge page."""
    expected_host = (urlparse(url).hostname or "").lower()
    session = _live_session(site_id, port, expected_host)
    if expected_host != session.host:
        raise ManualVerificationError("浏览器采集请求地址与验证平台不一致")
    try:
        targets = httpx.get(f"http://127.0.0.1:{session.port}/json", timeout=3).json()
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise ManualVerificationError("专用浏览器调试端口暂时无响应，将在下次采集时自动恢复") from exc
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
    auth_setup = ""
    if authorization_cookie:
        auth_setup = (
            "const n=" + json.dumps(authorization_cookie) + ";"
            + "const c=document.cookie.split('; ').find(x=>x.startsWith(n+'='));"
            + "if(c)o.headers.Authorization='Bearer '+c.slice(n.length+1);"
        )
    expression = (
        "(async()=>{const o=" + json.dumps(options, ensure_ascii=False) + ";" + auth_setup
        + "const c=new AbortController();const t=setTimeout(()=>c.abort(),25000);"
        + "o.signal=c.signal;let r;try{r=await fetch(" + request_url + ",o);"
        + "return JSON.stringify({status:r.status,text:await r.text()})}"
        + "catch(e){return JSON.stringify({status:0,text:e.name==='AbortError'?'timeout':String(e)})}"
        + "finally{clearTimeout(t)}})()"
    )
    try:
        with connect(target["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{session.port}", open_timeout=5) as websocket:
            websocket.send(json.dumps({"id": 9, "method": "Page.bringToFront"}))
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
                    message = "专用浏览器请求超时（30秒），将在下次采集时自动唤醒重试" if result.get("status") == 0 and result.get("text") == "timeout" else f"浏览器采集请求返回 {result.get('status') or '未知状态'}"
                    raise ManualVerificationError(message)
                text = result.get("text") or ""
                return json.loads(text or "{}") if parse_json else text
    except ManualVerificationError:
        raise
    except (TimeoutError, OSError, WebSocketException, ValueError) as exc:
        raise ManualVerificationError("专用浏览器请求超时或已断开，将在下次采集时自动恢复") from exc


def browser_page_json(site_id: int, page_url: str, api_marker: str,
                      port: int | None = None) -> dict:
    """Navigate the verified page and return one JSON API response made by the site itself."""
    expected_host = (urlparse(page_url).hostname or "").lower()
    session = _live_session(site_id, port, expected_host)
    if expected_host != session.host:
        raise ManualVerificationError("浏览器采集请求地址与验证平台不一致")
    targets = httpx.get(f"http://127.0.0.1:{session.port}/json", timeout=3).json()
    target = next((item for item in targets if item.get("type") == "page"
                   and item.get("webSocketDebuggerUrl")
                   and (urlparse(item.get("url", "")).hostname or "").lower() == session.host), None)
    if not target:
        raise ManualVerificationError("未找到平台公告页面，请保持专用窗口打开")
    request_id = None
    deadline = time.monotonic() + 30
    # Hash-router pages can reuse the detail component without issuing XHR.
    # Rebuild the document first; the persistent browser profile keeps login
    # storage while every notice gets its own observable detail request.
    separator = "&" if "?" in urlparse(page_url).fragment else "?"
    nonce = time.time_ns()
    navigation_url = f"{page_url}{separator}_scout={nonce}"
    detail_navigation_started = False
    with connect(target["webSocketDebuggerUrl"], origin=f"http://127.0.0.1:{session.port}", open_timeout=5) as websocket:
        websocket.send(json.dumps({"id": 1, "method": "Network.enable"}))
        websocket.send(json.dumps({"id": 5, "method": "Page.enable"}))
        websocket.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": "about:blank"}}))
        while time.monotonic() < deadline:
            try:
                event = json.loads(websocket.recv(timeout=max(1, deadline - time.monotonic())))
            except TimeoutError:
                break
            params = event.get("params") or {}
            if event.get("method") == "Page.loadEventFired" and not detail_navigation_started:
                detail_navigation_started = True
                websocket.send(json.dumps({"id": 4, "method": "Page.navigate", "params": {"url": navigation_url}}))
            if event.get("method") == "Network.responseReceived":
                response_url = ((params.get("response") or {}).get("url") or "")
                if api_marker in response_url:
                    request_id = params.get("requestId")
            if request_id and event.get("method") == "Network.loadingFinished" and params.get("requestId") == request_id:
                websocket.send(json.dumps({"id": 3, "method": "Network.getResponseBody", "params": {"requestId": request_id}}))
            if event.get("id") == 3:
                try:
                    return json.loads((event.get("result") or {}).get("body") or "{}")
                except (TypeError, ValueError) as exc:
                    raise ManualVerificationError("会员详情响应格式异常") from exc
    raise ManualVerificationError("会员详情加载超时，请刷新专用窗口后重试")


def complete_verification(site_id: int, port: int | None = None) -> str:
    session = _live_session(
        site_id, port, closed_message="没有正在运行的验证窗口，请先点击“打开人工验证”"
    )
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


def background_persistent_session(site_id: int) -> BrowserSession | None:
    """Switch a verified visible window to a minimized persistent process."""
    with _lock:
        current = _sessions.get(site_id)
    if current is None or current.profile is None:
        return None
    profile = current.profile
    url = current.url or f"https://{current.host}/"
    close_verification(site_id)
    deadline = time.monotonic() + 10
    while _profile_is_locked(profile) and time.monotonic() < deadline:
        time.sleep(0.2)
    try:
        return _launch_session(site_id, url, profile, hidden=True)
    except ManualVerificationError:
        # A browser build may ignore the minimized/off-screen flags. Keep the
        # verified profile usable rather than turning a successful validation
        # into a failed account state.
        return _launch_session(site_id, url, profile, hidden=False)


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


def replace_browser_session_port(session_cookie: str | None, port: int) -> str:
    """Keep persisted credentials aligned when a recovered Edge port changes."""
    if not session_cookie:
        return f"__scout_browser_port={port}"
    parts = [item.strip() for item in session_cookie.split(";") if item.strip()]
    replaced = False
    for index, item in enumerate(parts):
        if item.startswith("__scout_browser_port="):
            parts[index] = f"__scout_browser_port={port}"
            replaced = True
            break
    if not replaced:
        parts.append(f"__scout_browser_port={port}")
    return "; ".join(parts)
