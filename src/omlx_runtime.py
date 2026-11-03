#!/usr/bin/env python3
"""Local omlx health/wake helper owned by coremem.

No OpenClaw modules are imported. The launchd label and plist can be overridden
for other machines. All wake-helper failures are fail-open: the caller's normal
HTTP retry/degradation path remains authoritative.
"""
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

HOME = Path.home()
LAST_CALL_FILE = Path(os.environ.get("OMLX_LAST_CALL_FILE", str(HOME / ".boss-agent/cache/omlx-last-call")))
LOG_FILE = HOME / ".coremem/logs/omlx-wake.log"


def _log(msg: str):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except OSError:
        pass


def touch_last_call():
    """Keep the existing idle watchdog's timestamp contract, if it is installed."""
    try:
        LAST_CALL_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_CALL_FILE.write_text(str(int(time.time())))
    except OSError:
        pass


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urls(base_url: str):
    base = (base_url or "http://127.0.0.1:9999/v1").rstrip("/")
    parts = urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    return base, origin + "/health", base + "/chat/completions"


def _health(base_url: str, timeout: float = 3.0):
    try:
        _, health_url, _ = _urls(base_url)
        with _opener().open(health_url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _port_open(base_url: str, timeout: float = 2.0) -> bool:
    parts = urlsplit(base_url)
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_service():
    uid = os.getuid()
    label = os.environ.get("OMLX_LAUNCHD_LABEL", "com.lealway.omlx-server")
    plist = Path(os.environ.get("OMLX_LAUNCHD_PLIST", str(HOME / "Library/LaunchAgents/com.lealway.omlx-server.plist")))
    loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                            capture_output=True, text=True, timeout=10).returncode == 0
    cmd = (["launchctl", "kickstart", f"gui/{uid}/{label}"] if loaded
           else ["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
    subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def _warmup(deadline: float, base_url: str, model: str) -> bool:
    remain = deadline - time.time()
    if remain <= 0:
        return False
    _, _, chat_url = _urls(base_url)
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(chat_url, data=payload, headers={
        "Content-Type": "application/json", "Authorization": "Bearer omlx-local",
    })
    try:
        with _opener().open(req, timeout=remain) as resp:
            resp.read()
        return True
    except Exception as e:
        _log(f"warmup 未完成: {e}")
        return False


def ensure_omlx(timeout: int = 60, base_url: str = None, model: str = None, warm: bool = True) -> bool:
    """Return True when the local endpoint is usable; never raise to the caller."""
    base_url = base_url or os.environ.get("MEM_OMLX_BASE_URL") or "http://127.0.0.1:9999/v1"
    model = model or "Qwen3.8-Flash-Next-oQ4e-mtp"
    started = time.time()
    try:
        touch_last_call()
        health = _health(base_url)
        if health and health.get("status") == "healthy":
            if warm and (health.get("engine_pool") or {}).get("loaded_count") == 0:
                _warmup(started + timeout, base_url, health.get("default_model") or model)
            return True
        if not _port_open(base_url):
            try:
                _start_service()
            except Exception as e:
                _log(f"launchctl 唤醒失败: {e}")
        deadline = started + timeout
        while time.time() < deadline:
            health = _health(base_url, timeout=2)
            if health and health.get("status") == "healthy":
                model = health.get("default_model") or model
                break
            time.sleep(1.5)
        else:
            return False
        if warm and not _warmup(deadline, base_url, model) and not _port_open(base_url):
            return False
        return True
    except Exception as e:
        _log(f"ensure_omlx 内部异常（fail-open）: {e}")
        return True


if __name__ == "__main__":
    import sys
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    ok = ensure_omlx(timeout=timeout)
    print(f"ensure_omlx -> {ok}")
    sys.exit(0 if ok else 1)
