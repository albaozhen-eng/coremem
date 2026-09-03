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
