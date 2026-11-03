#!/usr/bin/env python3
"""Read-only OpenClaw session archive adapter.

This module is the only place that knows OpenClaw's on-disk layout. The normal
capture path does not import it unless the operator explicitly asks for the
legacy source. It never writes to the archive.
"""
import json
import sqlite3
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


class OpenClawArchive:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or os.environ.get("COREMEM_OPENCLAW_HOME") or (Path.home() / ".openclaw"))
        self.sessions_dir = self.root / "agents/main/sessions"
        self.db = self.root / "agents/main/agent/openclaw-agent.sqlite"
        self.import_archive_dir = self.root / "agents/main/session-sqlite-import-archive"

    @staticmethod
    def _parse_iso_dt(ts) -> Optional[datetime]:
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            return None

    @classmethod
    def _event_timestamp(cls, event: dict) -> Optional[datetime]:
        ts = event.get("timestamp") or event.get("created_at")
        if isinstance(ts, (int, float)):
            try:
                return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
        return cls._parse_iso_dt(ts) if isinstance(ts, str) else None

    @staticmethod
    def _append_message_line(event: dict, lines: list):
        if event.get("type") != "message":
            return
        msg = event.get("message", {})
        role, content = msg.get("role", ""), msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        if role in ("user", "assistant") and content:
            lines.append(f"{'用户' if role == 'user' else '助手'}: {content}")

    @classmethod
    def _message_time_range_jsonl(cls, filepath: Path) -> tuple:
        tmin = tmax = None
        try:
            with filepath.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if event.get("type") != "message":
                        continue
                    dt = cls._event_timestamp(event)
                    if dt is None:
                        continue
                    tmin = dt if tmin is None or dt < tmin else tmin
                    tmax = dt if tmax is None or dt > tmax else tmax
        except OSError:
            pass
        return tmin, tmax

    def get_recent_sessions(self, hours: float = 1) -> list[dict]:
        cutoff = (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=hours)).timestamp()
        sessions = []
        for f in self.sessions_dir.glob("*.jsonl"):
            if f.stat().st_mtime >= cutoff:
                sessions.append({"stem": f.stem, "kind": "jsonl", "path": f,
                                 "mtime": f.stat().st_mtime, "hash": None})
        if self.db.exists():
            try:
                con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
                rows = con.execute(
                    "SELECT session_id, COUNT(*), MAX(created_at) FROM transcript_events "
                    "GROUP BY session_id HAVING MAX(created_at) >= ?", (int(cutoff * 1000),),
                ).fetchall()
                con.close()
                for sid, cnt, max_ts in rows:
                    sessions.append({"stem": f"oc-{sid}", "kind": "sqlite", "session_id": sid,
                                     "mtime": max_ts / 1000, "hash": f"{max_ts}:{cnt}"})
            except Exception as e:
                print(f"[openclaw_archive] SQLite 扫描失败: {e}", file=sys.stderr)
        return sessions

    def get_sessions_in_window(self, since_dt, until_dt) -> list[dict]:
        sessions = []
        if self.import_archive_dir.exists():
            try:
                for f in self.import_archive_dir.glob("*.jsonl.imported-*"):
                    if "trajectory" in f.name:
                        continue
                    tmin, tmax = self._message_time_range_jsonl(f)
                    if tmin is None or tmax < since_dt or tmin > until_dt:
                        continue
                    sessions.append({"stem": f"ia-{f.stem}", "kind": "jsonl", "path": f,
                                     "mtime": f.stat().st_mtime, "hash": None})
            except Exception as e:
                print(f"[openclaw_archive] import-archive 扫描失败: {e}", file=sys.stderr)
        if self.db.exists():
            try:
                con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
                rows = con.execute(
                    "SELECT session_id, COUNT(*), MIN(created_at), MAX(created_at) FROM transcript_events "
                    "GROUP BY session_id").fetchall()
                con.close()
                for sid, cnt, min_ts, max_ts in rows:
                    tmin = datetime.fromtimestamp(min_ts / 1000, tz=timezone.utc)
                    tmax = datetime.fromtimestamp(max_ts / 1000, tz=timezone.utc)
                    if tmax < since_dt or tmin > until_dt:
                        continue
                    sessions.append({"stem": f"oc-{sid}", "kind": "sqlite", "session_id": sid,
                                     "mtime": max_ts / 1000, "hash": f"{max_ts}:{cnt}"})
            except Exception as e:
                print(f"[openclaw_archive] SQLite 扫描失败: {e}", file=sys.stderr)
        return sessions

    def extract_conversation(self, src, since=None, until=None) -> str:
        if src["kind"] == "sqlite":
            return self._conversation_from_sqlite(src["session_id"], since, until)
        return self._conversation_from_jsonl(Path(src["path"]), since, until)

    def _conversation_from_sqlite(self, session_id: str, since=None, until=None) -> str:
        lines, windowed = [], since is not None or until is not None
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
            columns = "event_json, created_at" if windowed else "event_json"
            rows = con.execute(
                f"SELECT {columns} FROM transcript_events WHERE session_id=? ORDER BY seq", (session_id,),
            ).fetchall()
            con.close()
            for row in rows:
                event_json = row[0]
                if windowed:
                    dt = datetime.fromtimestamp(row[1] / 1000, tz=timezone.utc)
                    if (since is not None and dt < since) or (until is not None and dt > until):
                        continue
                try:
                    event = json.loads(event_json)
                except (json.JSONDecodeError, TypeError):
                    continue
                self._append_message_line(event, lines)
        except Exception:
            pass
        return "\n".join(lines if windowed else lines[-40:])

    def _conversation_from_jsonl(self, filepath: Path, since=None, until=None) -> str:
        lines, windowed = [], since is not None or until is not None
        try:
            with filepath.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if windowed:
                        dt = self._event_timestamp(event)
                        if dt is not None and ((since is not None and dt < since) or (until is not None and dt > until)):
                            continue
                    self._append_message_line(event, lines)
        except OSError:
            pass
        return "\n".join(lines if windowed else lines[-40:])
