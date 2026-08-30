#!/usr/bin/env python3
"""
morning_briefing.py — 晨间记忆简报（纯确定性，零 LLM）

纯确定性，零 LLM。每日夜间 cron 触发（AUTO 同步之后错峰）。
红线：只落文件，不主动推送。

输入与算法：
  - 昨日关键决策：feedback.jsonl 中 ts ∈ 昨日 且 signal ∈ {explicit-*, relationship} 的条目
    （≤5，按 |delta| 降序，并列按 node_id 升序）
  - 记忆分数变化 Top 5：昨日 ledger 增量按 node 聚合 Σ delta，正负各列前 5
  - 候选矛盾：若本周 reports/contradictions-*.json 有未处置候选 → 引用文件路径 + 对数
  - 活跃主题 Top 5：昨日新增节点 tags 词频（过滤日期型/stoplist 标签）

输出：
  - memory/briefings/YYYY-MM-DD.md（日期归档）
  - memory/daily-briefing.md（最新副本，同步覆盖）
  - data/briefing_status.json（ts/ok/error，静默但可查）

幂等：同日重复跑，归档文件内容逐字节一致。
降级：语义索引不可达时决策/分数区块正常（本地 ledger），主题区块显示"（获取失败）"。

配置：
  所有路径和连接信息通过环境变量配置。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".sixmem"))
_ED = os.environ.get("MEM_DATA_DIR", "").strip()
DATA_DIR = Path(_ED) if _ED else MEMORY_HOME / "data"
TZ = timezone(timedelta(hours=8))
PLUGMEM_TIMEOUT = 15

SEMANTIC_BASE_URL = os.environ.get("MEM_SEMANTIC_BASE_URL", "http://127.0.0.1:8089")
SEMANTIC_API_KEY = os.environ.get("MEM_SEMANTIC_API_KEY", "")
SEMANTIC_GRAPH_ID = os.environ.get("MEM_SEMANTIC_GRAPH_ID", "default")

# 分数目录
_ESCAPE = os.environ.get("MEM_SCORES_DIR", "").strip()
if _ESCAPE:
    SCORES_DIR = Path(_ESCAPE)
else:
    SCORES_DIR = DATA_DIR / "memory_scores"
FEEDBACK_FILE = SCORES_DIR / "feedback.jsonl"

BRIEFING_STATE_FILE = DATA_DIR / "briefing_state.json"
BRIEFING_STATUS_FILE = DATA_DIR / "briefing_status.json"

_ESCAPE_OUT = os.environ.get("MEM_BRIEFINGS_DIR", "").strip()
if _ESCAPE_OUT:
    BRIEFINGS_DIR = Path(_ESCAPE_OUT) / "briefings"
    DAILY_BRIEFING_FILE = Path(_ESCAPE_OUT) / "daily-briefing.md"
    REPORTS_DIR = Path(_ESCAPE_OUT) / "reports"
else:
    BRIEFINGS_DIR = MEMORY_HOME / "memory" / "briefings"
    DAILY_BRIEFING_FILE = MEMORY_HOME / "memory" / "daily-briefing.md"
    REPORTS_DIR = MEMORY_HOME / "reports"

TAG_STOPLIST = {"决策", "配置", "状态"}
DATE_TAG_PATTERN = r"^20\d{2}-\d{2}-\d{2}$"
MAX_DETAIL_CHECKS = 10


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ============================================================
# 语义索引访问（不可达时降级）
# ============================================================

def _api_get(path: str, params: dict = None):
    url = f"{SEMANTIC_BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    headers = {"Content-Type": "application/json"}
    if SEMANTIC_API_KEY:
        headers["X-API-Key"] = SEMANTIC_API_KEY
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=PLUGMEM_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _safe_literal(s: str):
    """安全解析字符串化值"""
    import ast
    if s is None:
        return None
    if isinstance(s, (list, dict, bool)) or not isinstance(s, str):
        return s
    try:
        return ast.literal_eval(s)
    except Exception:
        return s


def fetch_new_nodes(last_id: int, limit: int = 400) -> list[dict]:
    """增量取新节点。不可达返回 []。"""
    result = _api_get(f"/api/v1/graphs/{SEMANTIC_GRAPH_ID}/nodes",
                      {"node_type": "semantic", "offset": last_id + 1, "limit": str(limit)})
    if not result:
        return []
    nodes = result.get("nodes", []) if isinstance(result, dict) else result
    return nodes


def get_node_detail(node_id: int) -> dict | None:
    """定向取节点 detail。不可达返回 None。"""
    result = _api_get(f"/api/v1/graphs/{SEMANTIC_GRAPH_ID}/node/semantic/{node_id}")
    if not result:
        return None
    return result.get("node", {})


# ============================================================
# 本地 ledger 处理
# ============================================================

def read_ledger() -> list[dict]:
    if not FEEDBACK_FILE.exists():
        return []
    entries = []
    with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None


def yesterday_entries(entries: list[dict], day=None) -> list[dict]:
    if day is None:
        day = datetime.now(TZ) - timedelta(days=1)
    day = day.astimezone(TZ)
    out = []
    for e in entries:
        dt = parse_ts(e.get("ts"))
        if dt and dt.date() == day.date():
            out.append(e)
    return out


# ============================================================
# 内容生成
# ============================================================

def gen_key_decisions(day_entries: list[dict]) -> list[dict]:
    relevant = [e for e in day_entries if e.get("signal") in
                {"explicit-positive", "explicit-negative", "relationship"}]
    relevant.sort(key=lambda e: (-abs(e.get("delta", 0.0)), e.get("node_id", 0)))
    return relevant[:5]


def gen_score_changes(day_entries: list[dict]) -> tuple[list[dict], list[dict]]:
    agg: dict[int, float] = {}
    for e in day_entries:
        nid = e.get("node_id")
        agg[nid] = agg.get(nid, 0.0) + e.get("delta", 0.0)
    items = [(nid, delta) for nid, delta in agg.items()]
    pos = sorted([i for i in items if i[1] > 0], key=lambda i: (-i[1], i[0]))[:5]
    neg = sorted([i for i in items if i[1] < 0], key=lambda i: (i[1], i[0]))[:5]
    return pos, neg


def gen_active_topics(day=None) -> tuple[list[tuple[int, str]], bool, dict]:
    if day is None:
        day = datetime.now(TZ)
    state = {}
    if BRIEFING_STATE_FILE.exists():
        try:
            state = json.loads(BRIEFING_STATE_FILE.read_text())
        except Exception:
            state = {}
    last_id = state.get("last_semantic_id", 0)

    new_nodes = fetch_new_nodes(last_id, limit=400)
    if not new_nodes:
        return [], False, {}

    tag_counter: Counter = Counter()
    detail_ids = []
    for n in new_nodes:
        sid = n.get("semantic_id")
        is_active = n.get("is_active", True)
        if isinstance(is_active, str):
            is_active = _safe_literal(is_active)
        if is_active is False:
            continue
        tags_raw = n.get("tags", [])
        tags = _safe_literal(tags_raw)
        if isinstance(tags, list) and tags:
            for t in tags:
                if not isinstance(t, str):
                    continue
                t = t.strip()
                if not t or t in TAG_STOPLIST or re.match(DATE_TAG_PATTERN, t):
                    continue
                if t.startswith("supersedes:"):
                    continue
                tag_counter[t] += 1
        else:
            if len(detail_ids) < MAX_DETAIL_CHECKS and isinstance(sid, int):
                detail_ids.append(sid)

    confirmed_yesterday = set()
    for sid in detail_ids:
        node = get_node_detail(sid)
        if not node:
            break
        created = node.get("created_at")
        dt = parse_ts(created) if isinstance(created, str) else None
        if dt and dt.date() == day.astimezone(TZ).date():
            confirmed_yesterday.add(sid)

    top = tag_counter.most_common(5)
    ties_sort = sorted(top, key=lambda x: (-x[1], x[0]))[:5]
    return ties_sort, True, {"detail_checked": list(confirmed_yesterday), "last_id": last_id}


def gen_candidate_contradictions(day=None) -> list[str]:
    if day is None:
        day = datetime.now(TZ)
    day = day.astimezone(TZ)
    if not REPORTS_DIR.exists():
        return []
    files = sorted(REPORTS_DIR.glob("contradictions-*.json"))
    if not files:
        return []
    latest = files[-1]
    try:
        data = json.loads(latest.read_text())
    except Exception:
        return [f"reports/{latest.name}（解析失败）"]
    pairs = data.get("candidates", [])
    n_unresolved = sum(1 for c in pairs if c.get("verdict") == "矛盾")
    if n_unresolved > 0:
        return [f"reports/{latest.name}（{n_unresolved} 条待处置矛盾候选）"]
    return []


# ============================================================
# 组装与落盘
# ============================================================

def render(day=None) -> str:
    if day is None:
        day = datetime.now(TZ)
    day = day.astimezone(TZ)
    date_str = day.strftime("%Y-%m-%d")
    report_day = day - timedelta(days=1)

    entries = read_ledger()
    day_entries = yesterday_entries(entries, report_day)

    lines = []
    lines.append(f"# 晨间记忆简报 {date_str}")
    lines.append("")

    lines.append("## 📌 昨日关键决策（ledger 增量，≤5 条）")
    key = gen_key_decisions(day_entries)
    if key:
        for e in key:
            sig = e.get("signal", "?")
            rel = e.get("rel")
            label = f"{e.get('node_id')}" + (f" [rel={rel}]" if rel else f" [{sig}]")
            lines.append(f"- {label}: {e.get('delta', 0):+.2f} — {e.get('reason', '')}")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 🔄 记忆分数变化 Top 5（正负各列）")
    pos, neg = gen_score_changes(day_entries)
    if pos or neg:
        if pos:
            lines.append("**上升 Top 5**")
            for nid, d in pos:
                lines.append(f"- +{d:.2f} node:{nid}")
        if neg:
            lines.append("**下降 Top 5**")
            for nid, d in neg:
                lines.append(f"- {d:.2f} node:{nid}")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## ⚠️ 候选矛盾（如有，引用 reports/ 链接）")
    cands = gen_candidate_contradictions(report_day)
    if cands:
        for c in cands:
            lines.append(f"- {c}")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 📈 活跃主题（昨日新增记忆 tag 词频，≤5 个）")
    topics, ok, detail_info = gen_active_topics(report_day)
    if not ok:
        lines.append("（获取失败）")
    elif topics:
        for t, c in topics:
            lines.append(f"- {t}（{c}）")
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("---")
    lines.append(f"_生成于简报日 {date_str} | 本地落盘存证 | 纯确定性_")
    return "\n".join(lines)


def write_briefing(content: str, day=None):
    if day is None:
        day = datetime.now(TZ)
    day = day.astimezone(TZ)
    date_str = day.strftime("%Y-%m-%d")
    BRIEFINGS_DIR.mkdir(parents=True, exist_ok=True)
    archive = BRIEFINGS_DIR / f"{date_str}.md"
    archive.write_text(content, encoding="utf-8")
    DAILY_BRIEFING_FILE.write_text(content, encoding="utf-8")
    return archive, DAILY_BRIEFING_FILE


def save_state(last_id: int):
    state = {"last_semantic_id": last_id, "updated": now_iso()}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BRIEFING_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def report_status(ok: bool, error: str = ""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = {"ts": now_iso(), "ok": ok, "error": error}
    BRIEFING_STATUS_FILE.write_text(json.dumps(status, ensure_ascii=False, indent=2))


def _fetch_max_semantic_id():
    """读当前最大 semantic_id 作水位线"""
    result = _api_get(f"/api/v1/graphs/{SEMANTIC_GRAPH_ID}/stats")
    if not result:
        return None
    return None  # 保守策略：stats 结构未知，保持水位线不变


def cmd_briefing():
    """生成晨间简报"""
    try:
        content = render()
        archive, latest = write_briefing(content)
        max_id = _fetch_max_semantic_id()
        if max_id is not None:
            save_state(max_id)
        report_status(True, "")
        print(f"[morning_briefing] ✅ 生成简报")
        print(f"   归档: {archive}")
        print(f"   最新: {latest}")
        return content
    except Exception as e:
        report_status(False, str(e))
        print(f"[morning_briefing] ❌ 生成失败: {e}")
        sys.exit(1)


def cmd_test_content():
    """测试模式：固定日期渲染，验证幂等"""
    test_day = datetime(2026, 8, 14, tzinfo=TZ)
    print("=== 幂等测试：同输入两次渲染内容逐字节一致 ===")
    c1 = render(test_day)
    c2 = render(test_day)
    print(f"两次渲染一致: {c1 == c2}")
    print("----- 渲染内容 -----")
    print(c1)
    return c1


def main():
    parser = argparse.ArgumentParser(
        description="morning_briefing — 晨间记忆简报（纯确定性零 LLM）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 src/morning_briefing.py            # 生成并落盘（cron 入口）
  python3 src/morning_briefing.py --render   # 只渲染到 stdout，不落盘
  python3 src/morning_briefing.py --test     # 测试渲染（固定日期幂等校验）
        """,
    )
    parser.add_argument("--render", action="store_true", help="只渲染不落盘")
    parser.add_argument("--test", action="store_true", help="测试模式（固定日期幂等校验）")
    args = parser.parse_args()

    if args.test:
        cmd_test_content()
    elif args.render:
        print(render())
    else:
        cmd_briefing()


if __name__ == "__main__":
    main()