#!/usr/bin/env python3
"""plugmem_config.py — 语义索引后端配置统一解析。
优先级：环境变量 MEM_* > ~/.openclaw/openclaw.json 的 plugins.entries.plugmem.config > 内置默认。
任何机器把 PlugMem 配进 OpenClaw 后，coremem 脚本零配置即可工作。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULTS = {
    "base_url": "http://127.0.0.1:8089",
    "api_key": "",
    "graph_id": "default",
}


def plugmem_cfg():
    file_cfg = {}
    try:
        d = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
        file_cfg = (d.get("plugins", {}).get("entries", {}).get("plugmem", {}) or {}).get("config", {}) or {}
    except Exception:
        pass
    return {
        "base_url": os.environ.get("MEM_SEMANTIC_BASE_URL", "").strip() or file_cfg.get("baseUrl") or _DEFAULTS["base_url"],
        "api_key": os.environ.get("MEM_SEMANTIC_API_KEY", "").strip() or file_cfg.get("apiKey") or _DEFAULTS["api_key"],
        "graph_id": os.environ.get("MEM_SEMANTIC_GRAPH_ID", "").strip() or file_cfg.get("defaultGraphId") or _DEFAULTS["graph_id"],
    }


# ============================================================
# P3：写入后回查（两个方向都靠它）
# ============================================================
#
# 背景：写入路径存在**双向歧义**
#   B1 报失败但其实已生效 → 重试即重复写入
#   B2 报成功但其实没落地 → 静默丢记忆
# 实测证据：账本 54 条 l2_status=ok 的写入里，有 1 条在 graph 里 count=0、文本也搜不到。
#
# GET /api/v1/graphs/{gid}/sessions/{session_id} 会返回该 session 实际插入的节点数，
# 这是**可靠且廉价**的存在性判据（不像 search，那个是相关性排序、不是子串匹配）。
def session_status(session_id, timeout=10):
    """返回 (count, error)。count=0 表示确实没落地；None 表示查不到/出错。"""
    import json as _json
    import urllib.parse as _parse
    import urllib.request as _req
    if not session_id:
        return None, "no session_id"
    cfg = plugmem_cfg()
    url = (f"{cfg['base_url']}/api/v1/graphs/{cfg['graph_id']}/sessions/"
           f"{_parse.quote(session_id)}")
    headers = {"X-API-Key": cfg["api_key"]} if cfg["api_key"] else {}
    try:
        with _req.urlopen(_req.Request(url, headers=headers), timeout=timeout) as r:
            return _json.loads(r.read()).get("count", 0), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
