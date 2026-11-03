#!/usr/bin/env python3
"""Regression tests for pi-default capture and the optional OpenClaw adapter."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import memory_capture as mc
from openclaw_archive import OpenClawArchive

FAILS = []


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not condition:
        FAILS.append(name)


def message(role, text, ts=None):
    return {"type": "message", "timestamp": ts or int(time.time() * 1000),
            "message": {"role": role, "content": text}}


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    pi = root / "pi/proj"
    pi.mkdir(parents=True)
    session = pi / "session.jsonl"
    session.write_text("\n".join(json.dumps(message(r, t)) for r, t in (("user", "决定采用方案A"), ("assistant", "收到"))) + "\n")
    mc.PI_SESSIONS_DIR = root / "pi"

    def forbidden_archive():
        raise AssertionError("default pi source must not load OpenClaw")

    original_archive = mc._openclaw_archive
    mc._openclaw_archive = forbidden_archive
    recent = mc.get_recent_sessions(hours=1)
    check("默认只返回 pi 会话", len(recent) == 1 and recent[0]["stem"] == "pi-session")
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    check("默认时间窗也不加载 OpenClaw", len(mc.get_sessions_in_window(since, until)) == 1)
    check("pi 对话提取保持用户/助手结构", "用户: 决定采用方案A" in mc.extract_conversation(recent[0]))

    class FakeArchive:
        def get_recent_sessions(self, hours):
            return [{"stem": "oc-old", "kind": "sqlite", "session_id": "old", "mtime": 1, "hash": "1:1"}]
        def get_sessions_in_window(self, since, until):
            return self.get_recent_sessions(1)
        def extract_conversation(self, src, since=None, until=None):
            return "用户: 历史消息"

    mc._openclaw_archive = FakeArchive
    check("all 显式合并历史源", {s["stem"] for s in mc.get_recent_sessions(1, sources="all")} == {"pi-session", "oc-old"})
    check("openclaw 显式排除 pi", [s["stem"] for s in mc.get_recent_sessions(1, sources="openclaw")] == ["oc-old"])
    check("SQLite 对话委托历史适配器", mc.extract_conversation({"kind": "sqlite", "session_id": "old"}) == "用户: 历史消息")
    mc._openclaw_archive = original_archive

    # Real archive adapter: both legacy JSONL and read-only SQLite.
    ocroot = root / "openclaw"
    sessions_dir = ocroot / "agents/main/sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "legacy.jsonl").write_text(json.dumps(message("user", "旧版jsonl消息")) + "\n")
    db = ocroot / "agents/main/agent/openclaw-agent.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER)")
    con.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
                ("s1", 1, json.dumps(message("user", "SQLite历史消息")), int(time.time() * 1000)))
    con.commit()
    con.close()
    archive = OpenClawArchive(ocroot)
    legacy = archive.get_recent_sessions(1)
    check("历史适配器读取 JSONL + SQLite", {s["stem"] for s in legacy} == {"legacy", "oc-s1"})
    sqlite_src = next(s for s in legacy if s["kind"] == "sqlite")
    check("历史适配器 SQLite 只读提取", archive.extract_conversation(sqlite_src) == "用户: SQLite历史消息")
    jsonl_src = next(s for s in legacy if s["kind"] == "jsonl")
    check("历史适配器 JSONL 提取", archive.extract_conversation(jsonl_src) == "用户: 旧版jsonl消息")

    with patch.dict(os.environ, {"MEM_OMLX_BASE_URL": "http://127.0.0.1:9999/v1"}, clear=False):
        channels = mc._build_llm_channels(30)
    check("无 OpenClaw 配置仍保留本地 omlx 通道", channels[0]["name"] == "local-omlx-flash")

    # A preview must not mark a session processed and suppress the real backfill.
    long_session = pi / "long.jsonl"
    long_session.write_text(json.dumps(message("user", "这是一个需要长期记住的项目决策。" * 20)) + "\n")
    mc.STATE_FILE = root / "capture-state.json"
    original_recent = mc.get_recent_sessions
    mc.get_recent_sessions = lambda **kwargs: [{"stem": "pi-long", "kind": "jsonl", "path": long_session,
                                               "mtime": time.time(), "hash": None}]
    mc.capture(hours=1, dry_run=True)
    check("dry-run 不写 processed 状态", not mc.STATE_FILE.exists())
    mc.get_recent_sessions = original_recent

print("\n全部通过" if not FAILS else f"\n失败: {FAILS}")
sys.exit(1 if FAILS else 0)
