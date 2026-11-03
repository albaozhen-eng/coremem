#!/usr/bin/env python3
"""Small Feishu notifier for coremem infrastructure alerts.

Credentials stay in the local bridge config. Unlike the recruiting notifier,
this module has no title-based blocklist: a memory-outage alert must not be
reported as delivered when it was deliberately suppressed elsewhere.
"""
import json
import os
import time
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("COREMEM_FEISHU_CONFIG", str(Path.home() / ".pi/feishu-bridge/override.json")))
LOG_FILE = Path.home() / ".coremem/logs/notify.log"


def _log(message: str):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {message}\n")
    except OSError:
        pass


def load_config(path: Path = None) -> dict:
    path = Path(path or CONFIG_FILE)
    return json.loads(path.read_text(encoding="utf-8"))


def _post(path: str, body: dict, token: str = "", opener=None) -> dict:
    req = urllib.request.Request(
        "https://open.feishu.cn" + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read())


def send(text: str, title: str = "coremem 告警", config: dict = None, opener=None) -> bool:
    """Send one alert. Raise on configuration/network errors; return API success."""
    cfg = config if config is not None else load_config()
    if not cfg.get("appId") or not cfg.get("appSecret"):
        raise RuntimeError(f"飞书配置缺少 appId/appSecret: {CONFIG_FILE}")
    recipients = cfg.get("allowUsers") or []
    if not recipients:
        raise RuntimeError("飞书配置缺少 allowUsers，无法确定接收人")
    token_response = _post("/open-apis/auth/v3/tenant_access_token/internal",
                           {"app_id": cfg["appId"], "app_secret": cfg["appSecret"]}, opener=opener)
    if token_response.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: code={token_response.get('code')}")
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}],
    }
    response = _post(
        "/open-apis/im/v1/messages?receive_id_type=open_id",
        {"receive_id": recipients[0], "msg_type": "interactive",
         "content": json.dumps(card, ensure_ascii=False)},
        token_response["tenant_access_token"], opener=opener,
    )
    ok = response.get("code") == 0
    _log(f"{'✅' if ok else '🔴'} {title} code={response.get('code')}")
    return ok
