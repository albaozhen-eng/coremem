#!/usr/bin/env python3
"""
memory_recall.py v3.0 — 记忆检索工具（全层搜索 + Skill 层 + RRF 重排 + 衰减）
===========================================================================
全层搜索工具：支持 L0 会话日志、L1 每日笔记、L2 语义索引、L3 编译缓存、L5 知识投影。

用法：
  python3 src/memory_recall.py all "关键词"                    # 全层搜索（推荐）
  python3 src/memory_recall.py all "关键词" --json             # JSON 输出
  python3 src/memory_recall.py test                            # 黄金回归集
  python3 src/memory_recall.py search "关键词" --hours 720     # L0 搜索
  python3 src/memory_recall.py list                            # 列出最近会话

配置：
  所有路径和连接信息通过环境变量配置。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from plugmem_config import plugmem_cfg

# === 配置（通过环境变量覆盖） ===
MEMORY_HOME = Path(os.environ.get("MEMORY_HOME", Path.home() / ".coremem"))
SESSIONS_DIR = Path(os.environ.get("MEM_SESSIONS_DIR", MEMORY_HOME / "sessions"))
MEMORY_DIR = Path(os.environ.get("MEM_DAILY_NOTES_DIR", MEMORY_HOME / "memory"))
UNIFIED_FACTS = MEMORY_DIR / "unified-facts.jsonl"
MEMORY_MD = Path(os.environ.get("MEM_MEMORY_MD", MEMORY_HOME / "MEMORY.md"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", MEMORY_HOME / "data"))
METRICS_FILE = DATA_DIR / "search_metrics.jsonl"
ALIASES_FILE = DATA_DIR / "search_aliases.json"
SCORES_FILE = DATA_DIR / "memory_scores" / "scores.json"
DECAY_POLICY_FILE = DATA_DIR / "decay_policy.json"

_PC = plugmem_cfg()  # env > ~/.openclaw/openclaw.json > default
SEMANTIC_BASE_URL = _PC["base_url"]
SEMANTIC_API_KEY = _PC["api_key"]
SEMANTIC_GRAPH_ID = _PC["graph_id"]

DEFAULT_HOURS = 48
ESCALATE_HOURS = 720
PER_FILE_CAP = 3
MAX_RESULTS_DEFAULT = 15
MAX_TOKENS = 12  # W3-5c：3 会把多词查询的 bigram 全截掉（#140 按此截断）
TZ = timezone(timedelta(hours=8))

# RRF 参数
RRF_K = 60
LAYER_WEIGHTS = {"skills": 1.0, "facts": 0.8, "evidence": 0.5}
SCORE_RERANK_COEFFICIENT = 0.25

DAILY_LOG_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}.*\.md$")
FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---', re.DOTALL)

STOPWORDS = set("""
怎么 如何 什么 为什么 请问 帮我 一下 这个 那个 哪个 哪里 多少 几个 是否 能否
可以 应该 需要 现在 今天 昨天 明天 时候 的话 我们 你们 他们 大家
the a an how what why is are do does did was were be to of in on for with
关于 一下 一个 一种 相关 问题 情况 事情 东西
""".split())


# --- W3-3：确定性查询改写（不调 LLM）---
_QW = ["是多少", "多少", "是什么", "什么", "哪一个", "哪个", "哪台", "哪些", "哪里",
       "为什么", "怎么", "如何", "是否", "有没有", "是不是", "能不能", "可不可以",
       "请问", "帮我", "一下", "还记得", "告诉我", "描述"]
_PUNCT = "？?！!。，,、；;:“”\"'（）()「」《》【】…—"


def normalize_query(q: str) -> str:
    """把整句问题压成检索词：去疑问词/标点/语气尾缀，保留名词与数字。
    只用于 L1/L0/事实等子串检索层；语义层仍用原句。"""
    s = q
    # W3-7b：日期表达归一化（"9月21日" → "09-21"），L1 行内用的是 ISO 日期
    def _date(mo):
        mm, dd = int(mo.group(1)), int(mo.group(2))
        return f" {mm:02d}-{dd:02d} "
    s = re.sub(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", _date, s)
    s = re.sub(r"(\d{4})\s*年", r" \1 ", s)
    for w in sorted(_QW, key=len, reverse=True):
        s = s.replace(w, " ")
    s = "".join(" " if ch in _PUNCT else ch for ch in s)
    s = re.sub(r"(了吗|了呢|的吧|吗|呢|吧|啊)\s*$", " ", s)
    parts = [p for p in s.split() if p and p.lower() not in STOPWORDS]
    parts = [p for p in parts if len(p) >= 2 or p.isdigit() or re.search(r"[A-Za-z]", p)]
    return " ".join(parts) if parts else q


def load_aliases() -> dict:
    try:
        if ALIASES_FILE.exists():
            return json.loads(ALIASES_FILE.read_text())
    except Exception:
        pass
    return {}


def expand_keywords(keyword: str) -> list[str]:
    aliases = load_aliases()
    kws = [keyword]
    kws.extend(aliases.get(keyword.lower(), []))
    for k, v in aliases.items():
        if keyword.lower() in v and k not in kws:
            kws.append(k)
    seen = set()
    return [k for k in kws if not (k.lower() in seen or seen.add(k.lower()))]


def split_tokens(keyword: str) -> list[str]:
    parts = [t for t in re.split(r"[\s,，。、/;；:：]+", keyword) if t]
    tokens = [t for t in parts if len(t) >= 2 and t.lower() not in STOPWORDS]
    if not tokens:
        tokens = [keyword]

    expanded = list(tokens)
    for t in tokens:
        if len(t) < 4:
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]+", t):
            for i in range(len(t) - 1):
                bg = t[i:i+2]
                if bg not in STOPWORDS:
                    expanded.append(bg)
        # 2026-10-26 修：原条件要求「中文 + 数字」，于是「中文 + 英文」
        # （硬编码空间ID、boss-sender 修复）整条不展开，只剩整句一个 token，
        # 整句几乎不可能在正文里逐字出现 → 该查询 facts 层直接为空。
        # 改为：只要含中文且非纯中文，就按「ASCII 段 / 中文段」切开分别成 token。
        elif re.search(r"[\u4e00-\u9fff]", t):
            for seg in re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+", t):
                if len(seg) >= 2 and seg not in STOPWORDS:
                    expanded.append(seg)
                if re.fullmatch(r"[\u4e00-\u9fff]+", seg) and len(seg) >= 4:
                    for i in range(len(seg) - 1):
                        bg = seg[i:i+2]
                        if bg not in STOPWORDS:
                            expanded.append(bg)
    seen = set()
    return [t for t in expanded if not (t in seen or seen.add(t))][:MAX_TOKENS]


def parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, TZ) if raw > 1e11 else datetime.fromtimestamp(raw, TZ)
    s = str(raw).strip()
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(TZ)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).replace(tzinfo=TZ)
    except ValueError:
        return None


def grep_candidates(directory: Path, keywords: list[str], exclude_sessions: set,
                    limit_files: int = 200) -> list[Path]:
    if not directory.exists():
        return []
    try:
        cmd = ["grep", "-li"]
        for kw in keywords:
            cmd += ["-e", kw]
        cmd += ["--", str(directory)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode not in (0, 1):
            return []
        files = []
        for line in r.stdout.splitlines():
            p = Path(line.strip())
            if p.suffix != ".jsonl" or ".trajectory." in p.name:
                continue
            if p.stem in exclude_sessions:
                continue
            files.append(p)
            if len(files) >= limit_files:
                break
        return files
    except Exception:
        return []


def parse_session_file(filepath: Path) -> list[dict]:
    messages = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "message":
                    continue
                msg = d.get("message", {})
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") if isinstance(c, dict) else str(c)
                        for c in content
                    )
                if content and role in ("user", "assistant"):
                    messages.append({
                        "role": role,
                        "content": str(content),
                        "ts": parse_ts(d.get("timestamp")),
                    })
    except Exception:
        pass
    return messages


def count_token_hits(content: str, tokens: list[str]) -> int:
    cl = content.lower()
    return sum(1 for t in tokens if t.lower() in cl)


def snippet_and_pos(content: str, tokens: list[str], window: int = 150) -> tuple[str, int]:
    """返回 (片段, 最佳匹配位置)；pos<0 表示未命中。"""
    cl = content.lower()
    best_pos = -1
    best_count = 0
    for t in tokens:
        idx = cl.find(t.lower())
        if idx >= 0:
            cnt = sum(1 for t2 in tokens if t2.lower() in cl[max(0, idx-window):idx+len(t)+window])
            if cnt > best_count:
                best_count = cnt
                best_pos = idx
    if best_pos < 0:
        return content[: window * 2], -1
    start = max(0, best_pos - window)
    end = min(len(content), best_pos + window * 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:end]}{suffix}", best_pos


def snippet_around(content: str, tokens: list[str], window: int = 150) -> str:
    return snippet_and_pos(content, tokens, window)[0]


def line_of(content: str, pos: int) -> int:
    """1-based 行号；pos<0 归 1。"""
    return 1 if pos < 0 else content.count("\n", 0, pos) + 1


def search_sessions(keyword: str, hours: int = DEFAULT_HOURS,
                    max_results: int = MAX_RESULTS_DEFAULT,
                    exclude_sessions: set = None, escalate: bool = True) -> list[dict]:
    exclude_sessions = exclude_sessions or set()
    tokens = split_tokens(keyword)
    kws = expand_keywords(keyword) + [t for t in tokens if t not in keyword]
    search_keys = list(dict.fromkeys(kws + tokens))

    def _run(hours_to_use):
        cutoff = datetime.now(TZ) - timedelta(hours=hours_to_use)
        hits = []
        for f in sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            if ".trajectory." in f.name:
                continue
            if f.stem in exclude_sessions:
                continue
            if f.stat().st_mtime < cutoff.timestamp():
                continue
            for msg in parse_session_file(f):
                n = count_token_hits(msg["content"], search_keys)
                if n > 0:
                    hits.append({
                        "file": f.stem,
                        "role": msg["role"],
                        "content": snippet_around(msg["content"], search_keys),
                        "ts": msg["ts"],
                        "hits": n,
                    })
        hits.sort(key=lambda h: (-h["hits"], -(h["ts"].timestamp() if h["ts"] else 0)))
        per_file = {}
        spread = []
        for h in hits:
            per_file[h["file"]] = per_file.get(h["file"], 0) + 1
            if per_file[h["file"]] <= PER_FILE_CAP:
                spread.append(h)
        return spread

    results = _run(hours)
    if not results and hours < ESCALATE_HOURS and escalate:
        results = _run(ESCALATE_HOURS)
        if results:
            results.append({"file": "__escalated__", "role": "system",
                            "content": f"⚠️ {hours}h 内无结果，已自动扩展到 {ESCALATE_HOURS}h（30天）",
                            "ts": None})
    return results[:max_results]


def search_daily_logs(keyword: str, max_results: int = 10) -> list[dict]:
    """L1 检索（W3-5b：行级 + IDF）
    L1 的事实是「一行一条」，所以按行建索引、按行打分：
      score(line) = Σ log(1 + N/(1+df(t)))  —— 稀有词权重高
    这样长文件不会因为"哪里都出现过"而霸榜，返回的也就是证据行本身。
    """
    tokens = split_tokens(keyword)
    if not tokens or not MEMORY_DIR.exists():
        return []

    # W3-8a：别名扩展（记忆↔memory、待决↔待拍板…），只加词不改排序权重
    _al = load_aliases()
    _extra = []
    for t in tokens:
        for a in _al.get(t.lower(), []):
            if a and a.lower() not in {x.lower() for x in tokens + _extra}:
                _extra.append(a)
    tokens = (tokens + _extra)[:16]

    docs = []
    for f in sorted(MEMORY_DIR.glob("*.md"), reverse=True):
        if not DAILY_LOG_PATTERN.match(f.name):
            continue
        try:
            docs.append((f, f.read_text(encoding="utf-8")))
        except Exception:
            continue
    if not docs:
        return []

    N = len(docs)
    lowered = [(f, c, c.lower()) for f, c in docs]
    df = {t: 0 for t in tokens}
    for _, _, cl in lowered:
        for t in tokens:
            if t.lower() in cl:
                df[t] += 1
    weights = {t: math.log(1 + N / (1 + df[t])) for t in tokens}

    hits = []
    for f, content, cl in lowered:
        for i, (line, lcl) in enumerate(zip(content.splitlines(), cl.splitlines()), 1):
            if not line.strip():
                continue
            present = [t for t in tokens if t.lower() in lcl]
            if not present:
                continue
            score = sum(weights[t] for t in present) + 0.3 * (len(present) - 1)
            hits.append((score, f.name, i, line))   # P1：存带扩展名的文件名，供 file 字段与回指使用
    hits.sort(key=lambda x: -x[0])

    out = []
    for score, fname, lineno, line in hits[:max_results]:
        text = line.strip()
        if len(text) > 600:
            text = text[:600] + "…"
        out.append({
            "file": f"{fname}:{lineno}",   # P1：带 .md 扩展名，与 golden 锚点格式一致
            "role": "log",
            "prov_class": "source",        # P2：L1 就是事实源本身
            "content": text,
            "ts": None,
            "hits": round(score, 2),
        })
    return out


_DERIVED_BEGIN = "<!-- DERIVED:BEGIN"
_DERIVED_END = "<!-- DERIVED:END"


def _md_units(content: str) -> list[tuple[int, str]]:
    """把 MEMORY.md 切成**可独立检索的单元**，返回 [(起始行号, 文本)]。

    为什么需要：原先整份 MEMORY.md 当一个文档打分、只取一个片段，
    于是「问编制冲突，返回的片段里夹着未读积压的规则」——
    规则粒度被文档粒度掩盖。现在 DERIVED 区里**每条规则单独成单元**，
    MANUAL 等其余内容仍合并成一个单元（它们的价值在于整段上下文）。
    """
    lines = content.splitlines()
    bi = next((i for i, l in enumerate(lines) if _DERIVED_BEGIN in l), None)
    ei = next((i for i, l in enumerate(lines) if _DERIVED_END in l), None)
    if bi is None or ei is None or ei <= bi:
        return [(1, content)]                      # 没有派生区：行为与改动前一致

    units: list[tuple[int, str]] = []
    head = "\n".join(lines[:bi])
    if head.strip():
        units.append((1, head))

    cur_start, cur_lines = None, []
    for i in range(bi + 1, ei):
        l = lines[i]
        if l.strip().startswith("- **"):           # 一条规则的开始
            if cur_start is not None:
                units.append((cur_start, "\n".join(cur_lines)))
            cur_start, cur_lines = i + 1, [l.strip()]
        elif cur_start is not None and l.strip().startswith("- "):
            cur_lines.append(l.strip())            # 规则下的证据行
        elif cur_start is not None and not l.strip():
            if cur_lines:
                units.append((cur_start, "\n".join(cur_lines)))
            cur_start, cur_lines = None, []
    if cur_start is not None:
        units.append((cur_start, "\n".join(cur_lines)))

    tail = "\n".join(lines[ei + 1:])
    if tail.strip():
        units.append((ei + 2, tail))
    return units or [(1, content)]


def search_fact_files(keyword: str, max_results: int = 10) -> list[dict]:
    tokens = split_tokens(keyword)
    results = []
    targets = []
    if UNIFIED_FACTS.exists():
        targets.append(("unified-facts", UNIFIED_FACTS))
    if MEMORY_MD.exists():
        targets.append(("MEMORY.md", MEMORY_MD))

    for label, path in targets:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        # MEMORY.md 按「规则」切开打分；unified-facts 保持整文件行为
        units = _md_units(content) if label == "MEMORY.md" else [(1, content)]
        for start_ln, text in units:
            n = count_token_hits(text, tokens)
            if n <= 0:
                continue
            snip, pos = snippet_and_pos(text, tokens, window=100)
            results.append({
                "file": f"{label}:{start_ln + (line_of(text, pos) - 1)}",
                "role": "fact",
                "prov_class": "derived",
                "content": snip,
                "ts": None,
                "hits": n,
                # 复核指出：密度若用片段长度（≈302 字）当分母，短单元会被系统性抬高，
                # 相当于给 DERIVED 区加 ~3 倍权重。改为用**单元自身长度**。
                "_unit_len": len(text),
            })
        if len(results) >= max_results * 3:
            break
    # 排序：主键是**查询词覆盖率**（命中了几个不同的查询词），次键才是命中密度。
    #
    # 演进过程（两次都被实测/复核否掉）：
    #   ① 按命中总数：一段 302 字日志命中 4 次，压过 75 字规则命中 3 次；
    #   ② 按密度（分母片段长度）：相当于给短单元加 ~3 倍权重，
    #      于是"空间 续费"这种查询会把一条只沾到"空间"的无关规则顶到第 1；
    #   ③ 密度改分母为单元长度：偏向又反了过来，大单元被过度惩罚。
    # 真正该衡量的是"这个候选回答了多少个查询词"，即覆盖率；长度只做次键。
    def _coverage(r):
        txt = (r.get("content") or "")
        return sum(1 for t in set(tokens) if t and t in txt)

    def _density(r):
        return r.get("hits", 1) * 100.0 / max(r.get("_unit_len") or len(r.get("content") or ""), 50)

    results.sort(key=lambda r: (-_coverage(r), -_density(r), -r.get("hits", 1)))
    for r in results:
        r.pop("_unit_len", None)
    return results[:max_results]


_PROV_IDX = [None]   # 进程级缓存：L1 前缀索引只建一次
_EXCLUDE_SYNTH = (os.environ.get("MEM_EXCLUDE_SYNTH", "").strip().lower() in ("1", "true", "yes", "on"))


def _parse_prov_session_id(session_id):
    """P1：从 PlugMem 的 session_id 还原 (source_line, sha)。

    写入侧格式：l1:<file>:<line>#<sha16>（见 remember.py）。
    历史节点 session_id 为 null → 返回 (None, None)，行为与改动前一致。
    """
    if not session_id or not isinstance(session_id, str) or not session_id.startswith("l1:"):
        return None, None
    body = session_id[3:]
    ref, _, sha = body.partition("#")
    if ":" not in ref:
        return None, None
    return ref, (sha or None)


def search_plugmem(keyword: str, max_results: int = 5) -> list[dict]:
    # 每个 token 一次 HTTP 往返，服务端串行处理：10 个 token ≈ 500ms。
    # MEM_PM_TOKEN_MODE 用于 A/B：
    #   all(默认) = 整句 + 全部二元组；query = 只发整句；query2 = 整句 + 2 个二元组
    #
    # 2026-10-25 实测：整句 + 9 个二元组 = 10 次往返 ≈ 570ms；
    # 只发整句 = 1 次 ≈ 290ms，而 golden（33 题）与 KIS（300 题）的召回完全不变
    # （KIS: 0 退步 / 0 进步，p=1）。因此默认改为只发整句。
    # 保留 all，并通过 MEM_PM_TOKEN_MODE 可回退。
    _mode = os.environ.get("MEM_PM_TOKEN_MODE", "query")
    _all = split_tokens(keyword)
    # 复核指出（2026-10-26）：原写 `_all[:1]`，而 `_all[0]` 是**按空格/标点切的第一段**，
    # 不是整句 —— 40 题里 29 题发出去的是 `BOSS`、`SCNet`、`10-02` 这种片段。
    # 现改为把归一化后的整句作为第一个查询（语义检索本就该吃整句）。
    _full = (normalize_query(keyword) or keyword).strip()
    _first = [_full] if _full else _all[:1]
    if _mode == "all":
        _toks = _all
    elif _mode == "query2":
        _toks = _first + _all[1:3]
    else:                     # query / 默认
        _toks = _first
    tokens = _toks
    # 兜底：整句检索 0 命中时，再补二元组（避免短查询/新词因整句嵌入差而空手而归）
    _fallback = _all[1:4] if _mode in ("query", "query2") else []
    results = []
    seen_ids = set()

    def _run(ts):
        results = []
        seen_ids = set()
        for tok in ts[:MAX_TOKENS]:
            url = f"{SEMANTIC_BASE_URL}/api/v1/graphs/{SEMANTIC_GRAPH_ID}/search?q={urllib.parse.quote(tok)}&limit={max_results}"
            try:
                req = urllib.request.Request(url, headers={"X-API-Key": SEMANTIC_API_KEY} if SEMANTIC_API_KEY else {})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                for n in data.get("nodes", []):
                    nid = n.get("semantic_id", n.get("id"))
                    text = n.get("text", "")
                    if n.get("is_active") is False:
                        continue
                    conf = n.get("confidence")
                    if conf is not None and conf < get_min_confidence():
                        continue
                    if nid in seen_ids or not text:
                        continue
                    seen_ids.add(nid)
                    _src, _sha = _parse_prov_session_id(n.get("session_id"))
                    # P2：标注这条 L2 记忆有没有事实源
                    #   derived     = 文本能在 L1 找到（可核对）
                    #   synthesized = 找不到（是生成/汇总内容，不可核对）
                    # 大样本诊断显示 synthesized 中位长度是可回溯节点的 5 倍，
                    # 是增值的合成层 —— 所以不删、不重建，只如实标注。
                    try:
                        from l1_prov_index import classify as _pv_classify, build as _pv_build
                        if _PROV_IDX[0] is None:
                            _PROV_IDX[0] = _pv_build()
                        _prov_class = _pv_classify(text, _PROV_IDX[0])
                    except Exception:  # noqa: BLE001
                        _prov_class = "unknown"
                    # 门控实验：剔除「无事实源」的合成记忆（默认关闭，生产行为不变）
                    # 用于回答「历史合成层是否在占用 top-k 槽位、是否损害召回」
                    if _EXCLUDE_SYNTH and _prov_class == "synthesized":
                        continue
                    results.append({
                        "file": f"semantic#{nid}",
                        "source_line": _src,          # P1：可回溯坐标（历史节点为 None）
                        "prov_sha": _sha,
                        "prov_session_id": n.get("session_id"),
                        "prov_class": _prov_class,
                        "role": "fact",
                        "content": snippet_around(text, tokens, window=100),
                        "ts": None,
                        "layer": "facts",
                        "node_id": nid,
                        "confidence": conf,
                        "event_time": n.get("event_time") or None,
                        "created_at": n.get("created_at") or None,
                    })
            except Exception:
                continue
        return results

    results = _run(tokens)
    # 兜底：整句 0 命中时补二元组（短查询/新词场景）
    if not results and _fallback:
        results = _run(_fallback)
    return results[:max_results]


# ============================================================
# Skill 层搜索
# ============================================================

def parse_frontmatter(text: str) -> dict:
    result = {}
    for line in text.split("\n"):
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().strip('"')
            val = val.strip().strip('"').strip("'")
            if key in ("name", "description", "version"):
                result[key] = val
            elif key == "read_when" and not result.get("read_when"):
                result["read_when"] = []
            elif key == "read_priority":
                result[key] = val
    return result


def search_skills(keyword: str, max_results: int = 5) -> list[dict]:
    """搜索 Skill 目录的 frontmatter + 正文"""
    tokens = split_tokens(keyword)
    results = []

    skill_dirs = [
        MEMORY_HOME / "skills",
        MEMORY_HOME / "plugin-skills",
    ]

    for skill_dir in skill_dirs:
        if not skill_dir.exists():
            continue
        for skill_dir_entry in skill_dir.iterdir():
            if not skill_dir_entry.is_dir():
                continue
            skill_md = skill_dir_entry / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue

            fm_match = FRONTMATTER_RE.search(content)
            fm = parse_frontmatter(fm_match.group(1)) if fm_match else {}

            name = fm.get("name", skill_dir_entry.name)
            desc = fm.get("description", "")

            search_text = f"{name} {desc} {content[fm_match.end():fm_match.end()+500] if fm_match else content[:500]}"
            n = count_token_hits(search_text, tokens)

            if n > 0:
                results.append({
                    "file": f"skills/{name}",
                    "prov_class": "curated",   # P2：技能文档是人工维护，不派生自 L1
                    "role": "skill",
                    "content": f"[{name}] {desc[:150]}",
                    "ts": None,
                    "layer": "skills",
                    "hits": n,
                    "skill_name": name,
                })

    results.sort(key=lambda r: -r.get("hits", 1))
    return results[:max_results]


# ============================================================
# 分数重排 + 时效衰减
# ============================================================

def load_scores() -> dict:
    try:
        if SCORES_FILE.exists():
            return json.loads(SCORES_FILE.read_text())
    except Exception:
        pass
    return {}


def load_decay_policy() -> dict:
    try:
        if DECAY_POLICY_FILE.exists():
            return json.loads(DECAY_POLICY_FILE.read_text())
    except Exception:
        pass
    return {"categories": [], "unmatched_behavior": "no_decay"}


def get_min_confidence() -> float:
    policy = load_decay_policy()
    return float(policy.get("min_confidence", 0.3))


def get_norm_score(node_id: int, scores: dict) -> float:
    node_scores = scores.get(str(node_id), {})
    score = node_scores.get("score", 0.0)
    return score / 3.0


def apply_rerank(results: list[dict], layer: str, keyword: str, scores: dict, decay_policy: dict) -> list[dict]:
    if not results:
        return results

    weight = LAYER_WEIGHTS.get(layer, 0.5)
    tokens = split_tokens(keyword)

    for i, r in enumerate(results):
        rrf = 1.0 / (RRF_K + (i + 1))
        score_boost = 1.0
        node_id = r.get("node_id")
        if node_id and layer == "facts":
            norm = get_norm_score(node_id, scores)
            score_boost = 1.0 + SCORE_RERANK_COEFFICIENT * norm
        decay = 1.0
        r["_rrf"] = rrf
        r["_weight"] = weight
        r["_score_boost"] = score_boost
        r["_decay"] = decay
        r["_sort_key"] = rrf * weight * score_boost * decay

    results.sort(key=lambda r: -r["_sort_key"])
    return results


# ============================================================
# 遥测日志
# ============================================================

def log_metrics(query: str, cmd: str, per_layer: dict, total: int,
                elapsed_ms: int, exit_code: int, rerank_applied: bool = False):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "query": query,
            "cmd": cmd,
            "per_layer_counts": per_layer,
            "total": total,
            "elapsed_ms": elapsed_ms,
            "exit_code": exit_code,
            "rerank_applied": rerank_applied,
            "layers_hit": [k for k, v in per_layer.items() if v > 0],
        }
        with open(METRICS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ============================================================
# 时间意图门控
# ============================================================

TIME_INTENT_PATTERNS = [
    (r"(20[0-9]{2})年", "year"),
    (r"(20[0-9]{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12][0-9]|3[01]))?", "date"),
    (r"去年", "last_year"),
    (r"今年", "this_year"),
    (r"上个月", "last_month"),
    (r"这个月|本月", "this_month"),
    (r"上周", "last_week"),
    (r"这周|本周", "this_week"),
    (r"昨天", "yesterday"),
    (r"今天|今日", "today"),
]


def detect_time_intent(query: str) -> dict | None:
    import re as _re
    for pattern, kind in TIME_INTENT_PATTERNS:
        m = _re.search(pattern, query)
        if m:
            return {"kind": kind, "value": m.group(0)}
    return None


def _eff_date(node: dict) -> float | None:
    et = node.get("event_time") or ""
    if et:
        try:
            return datetime.fromisoformat(et).timestamp()
        except (ValueError, TypeError):
            pass
    ca = node.get("created_at")
    if ca:
        try:
            return float(ca)
        except (TypeError, ValueError):
            pass
    return None


def _time_bounds(intent: dict) -> tuple[float | None, float | None]:
    import re as _re
    now_dt = datetime.now(TZ)
    kind, val = intent["kind"], intent["value"]
    if kind == "date":
        m = _re.search(r"(20[0-9]{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12][0-9]|3[01]))?", val)
        if m:
            y, mo = int(m.group(1)), int(m.group(2))
            d = int(m.group(3)) if m.group(3) else 1
            start = datetime(y, mo, d, tzinfo=TZ).timestamp()
            end = start + 86400 if m.group(3) else (datetime(y, mo + 1, 1, tzinfo=TZ).timestamp() if mo < 12 else datetime(y + 1, 1, 1, tzinfo=TZ).timestamp())
            return start, end
    if kind == "year":
        m = _re.search(r"(20[0-9]{2})", val)
        if m:
            y = int(m.group(1))
            return datetime(y, 1, 1, tzinfo=TZ).timestamp(), datetime(y + 1, 1, 1, tzinfo=TZ).timestamp()
    if kind == "last_year":
        y = now_dt.year - 1
        return datetime(y, 1, 1, tzinfo=TZ).timestamp(), datetime(y + 1, 1, 1, tzinfo=TZ).timestamp()
    if kind == "this_year":
        y = now_dt.year
        return datetime(y, 1, 1, tzinfo=TZ).timestamp(), datetime(y + 1, 1, 1, tzinfo=TZ).timestamp()
    if kind == "last_month":
        first = datetime(now_dt.year, now_dt.month, 1, tzinfo=TZ)
        prev_end = first.timestamp()
        prev_start = (first - timedelta(days=1)).replace(day=1, tzinfo=TZ).timestamp()
        return prev_start, prev_end
    if kind == "this_month":
        return datetime(now_dt.year, now_dt.month, 1, tzinfo=TZ).timestamp(), (datetime(now_dt.year, now_dt.month + 1, 1, tzinfo=TZ) if now_dt.month < 12 else datetime(now_dt.year + 1, 1, 1, tzinfo=TZ)).timestamp()
    if kind == "last_week":
        end = now_dt - timedelta(days=now_dt.weekday() + 1)
        start = end - timedelta(days=7)
        return start.timestamp(), end.timestamp()
    if kind == "this_week":
        start = now_dt - timedelta(days=now_dt.weekday())
        return start.timestamp(), now_dt.timestamp() + 1
    if kind == "yesterday":
        d = now_dt - timedelta(days=1)
        return datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp(), datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() + 86400
    if kind == "today":
        return datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=TZ).timestamp(), now_dt.timestamp() + 1
    return None, None


def apply_time_gate(pm_results: list[dict], keyword: str) -> tuple[list[dict], bool]:
    intent = detect_time_intent(keyword)
    if not intent:
        return pm_results, False
    start, end = _time_bounds(intent)
    if start is None:
        return pm_results, False
    filtered = []
    for r in pm_results:
        ts = _eff_date(r)
        if ts is None:
            continue
        if start <= ts < end:
            filtered.append(r)
    if not filtered:
        return pm_results, True
    return filtered, True


# ============================================================
# 输出辅助
# ============================================================

def result_to_json(r: dict) -> dict:
    ts = r.get("ts")
    ts_str = ts.isoformat() if isinstance(ts, datetime) else (str(ts) if ts else None)
    out = {
        "file": r.get("file"),
        "role": r.get("role"),
        "content": r.get("content"),
        "ts": ts_str,
    }
    # provenance 字段必须显式透传：这个函数是白名单式的，漏掉就等于整条链路空转。
    # （P1/P2 都曾在这里被静默丢弃：search_plugmem 里加好了字段，输出时却没了。）
    for k in ("source_line", "prov_sha", "prov_session_id", "prov_class", "node_id",
              "layer", "confidence", "event_time"):
        if k in r:
            out[k] = r[k]
    return out


def format_ts(dt) -> str:
    if not dt:
        return "?"
    try:
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def print_results(results: list[dict], max_out: int = 15):
    seen = set()
    count = 0
    for r in results:
        key = (r.get("file", ""), r.get("content", "")[:60])
        if key in seen:
            continue
        seen.add(key)
        tag = {"user": "🧑", "assistant": "🤖", "log": "📄", "fact": "💾", "system": "ℹ️"}.get(r.get("role", ""), "•")
        ts_str = format_ts(r.get("ts"))
        print(f"{count+1}. {tag} [{ts_str}] {r.get('content','')[:220]}")
        print(f"   📁 {r.get('file')}")
        print()
        count += 1
        if count >= max_out:
            break
    return count


# ============================================================
# 命令实现
# ============================================================

_PUNCT_RE = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()【】\[\]、/\\|·…—-]+")


def _norm_text(text: str) -> str:
    return _PUNCT_RE.sub("", (text or "").lower())


def collapse_near_duplicates(rows: list[dict], query: str, threshold: float = 0.75) -> list[dict]:
    """把语义层里几乎同义的条目折叠成一条（2026-10-30）。

    为什么：同一件事常被 capture/x 次重复写成 5–6 条变体（例如「坑位/名额 永久禁用」
    有 6 条几乎同义的条目，全部 confidence=0.6），它们会挤满各层的前 3 个槽位，
    把携带完整锚词的旧条目挤出 top-3，表现为 golden 退步，而数据其实没丢。

    折叠规则：
      * 归一化后（去标点/空白/大小写）文本相似度 >= threshold 的视为一组；
      * 组内保留「命中查询词更多、其次文本更长」的那条；
      * 被折叠条目的独特文本会拼进来（最多 3 条），避免折叠反而丢掉锚词。
    只影响本次召回的展示，不改图里的数据。
    """
    import difflib

    if len(rows) < 2:
        return rows
    q = (query or "").strip()
    q_grams = [t for t in re.split(r"[\s,，。、；;:：]+", q) if t]

    def coverage(text: str) -> int:
        t = (text or "")
        return sum(1 for token in q_grams if token and token in t)

    normed = [_norm_text(r.get("content") or "") for r in rows]
    used = [False] * len(rows)
    out: list[dict] = []
    for i in range(len(rows)):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        for j in range(i + 1, len(rows)):
            if used[j]:
                continue
            if difflib.SequenceMatcher(None, normed[i], normed[j]).ratio() >= threshold:
                group.append(j)
                used[j] = True
        if len(group) == 1:
            out.append(rows[i])
            continue
        members = [rows[k] for k in group]
        best = max(members, key=lambda m: (coverage(m.get("content") or ""), len(m.get("content") or "")))
        merged = dict(best)
        extra = []
        for m in members:
            text = (m.get("content") or "").strip()
            if text and text not in (merged.get("content") or "") and len(extra) < 2:
                extra.append(text)
        if extra:
            merged["content"] = ((merged.get("content") or "").rstrip() + " ｜ " + " ｜ ".join(extra))[:800]
        merged["merged_from"] = len(group)
        out.append(merged)
    return out


def do_search_all(keyword: str, hours: int, exclude: set) -> dict:
    kw = normalize_query(keyword)          # W3-3：子串层用改写后的检索词
    skills = search_skills(keyword, max_results=5)
    pm = search_plugmem(keyword, max_results=10)
    # 先折叠近重复再截断：否则 5 个同义变体会把锚点挤出槽位（2026-10-30）
    pm = collapse_near_duplicates(pm, keyword)[:5]
    pm, time_gate_applied = apply_time_gate(pm, keyword)
    sess = search_sessions(kw, hours=hours, max_results=10, exclude_sessions=exclude)
    logs = search_daily_logs(kw, max_results=5)
    facts = search_fact_files(kw, max_results=5)
    total = len(skills) + len(pm) + len(sess) + len(logs) + len(facts)
    return {"skills": skills, "pm": pm, "sess": sess, "logs": logs, "facts": facts, "total": total, "time_gate_applied": time_gate_applied}


def cmd_search(args):
    exclude = set(args.exclude_session.split(",")) if args.exclude_session else set()
    t0 = time.time()
    results = search_sessions(args.keyword, hours=args.hours, max_results=args.max,
                              exclude_sessions=exclude)
    elapsed = int((time.time() - t0) * 1000)
    print(f"🔍 L0 session 搜索 '{args.keyword}'（{args.hours}h 内）...\n")
    n = print_results(results, max_out=args.max)
    if n == 0:
        print("❌ 未找到匹配。")
    log_metrics(args.keyword, "search", {"l0": len(results)}, len(results), elapsed, 0 if n else 1)
    return 0 if n else 1


def cmd_all(args):
    exclude = set(args.exclude_session.split(",")) if args.exclude_session else set()
    t0 = time.time()
    data = do_search_all(args.keyword, args.hours, exclude)
    elapsed = int((time.time() - t0) * 1000)

    scores = load_scores()
    decay_policy = load_decay_policy()
    rerank_applied = bool(scores)

    skills = apply_rerank(data["skills"], "skills", args.keyword, scores, decay_policy)
    pm = apply_rerank(data["pm"], "facts", args.keyword, scores, decay_policy)
    sess = apply_rerank(data["sess"], "evidence", args.keyword, scores, decay_policy)
    logs = apply_rerank(data["logs"], "evidence", args.keyword, scores, decay_policy)
    facts = apply_rerank(data["facts"], "facts", args.keyword, scores, decay_policy)
    total = len(skills) + len(pm) + len(sess) + len(logs) + len(facts)

    if args.json:
        out = {
            "query": args.keyword,
            "layers": {
                "skills": [result_to_json(r) for r in skills],
                "semantic": [result_to_json(r) for r in pm],
                "l0": [result_to_json(r) for r in sess],
                "l1": [result_to_json(r) for r in logs],
                "facts": [result_to_json(r) for r in facts],
            },
            "total": total,
            "elapsed_ms": elapsed,
            "rerank_applied": rerank_applied,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        per_layer = {"skills": len(skills), "semantic": len(pm), "l0": len(sess),
                     "l1": len(logs), "facts": len(facts)}
        log_metrics(args.keyword, "all", per_layer, total, elapsed, 0 if total else 1, rerank_applied)
        return 0 if total else 1

    print(f"🔍 全层搜索 '{args.keyword}'...\n")
    if skills:
        print(f"── 🎯 Skills 技能层 ({len(skills)} 条) ──")
        print_results(skills, max_out=5)
        print()
    if pm:
        print(f"── 🧠 语义索引 ({len(pm)} 条) ──")
        print_results(pm, max_out=5)
        print()
    if sess:
        print(f"── 💬 L0 会话记录 ({len(sess)} 条) ──")
        print_results(sess, max_out=10)
        print()
    if logs:
        print(f"── 📄 L1 daily log ({len(logs)} 条) ──")
        print_results(logs, max_out=5)
        print()
    if facts:
        print(f"── 📚 事实文件 ({len(facts)} 条) ──")
        print_results(facts, max_out=5)
        print()

    if total == 0:
        print("❌ 所有层均未找到匹配。")
    else:
        print(f"✅ 共 {total} 条匹配（Skills {len(skills)} / 语义索引 {len(pm)} / L0 {len(sess)} / L1 {len(logs)} / 事实 {len(facts)}）")
        if rerank_applied:
            print(f"   📊 分数重排已应用")
    per_layer = {"skills": len(skills), "semantic": len(pm), "l0": len(sess),
                 "l1": len(logs), "facts": len(facts)}
    log_metrics(args.keyword, "all", per_layer, total, elapsed, 0 if total else 1, rerank_applied)
    return 0 if total else 1


def cmd_test():
    cases = [
        ("项目迁移", ["semantic", "l0"], "项目迁移记录"),
        ("记忆系统", ["semantic"], "记忆系统架构"),
        ("语义索引", ["l0", "semantic"], "语义索引故障"),
        ("智能路由", ["semantic"], "智能路由对比"),
        ("配置变更", ["l1", "semantic"], "配置变更查询"),
        ("嵌入模型", ["l0", "facts"], "嵌入模型"),
        ("2020年项目", ["semantic"], "P0: 年份时间查询"),
        ("昨天讨论", ["l0"], "P0: 相对时间-昨天"),
        ("这周记录", ["semantic", "l0"], "P0: 相对时间-本周"),
        ("今天记录", ["l0", "semantic"], "P0: 相对时间-今天"),
        ("产品选购", ["skills"], "skill-first: 选购指南"),
        ("对账", ["facts", "l0"], "skill-first: 对账能力"),
    ]
    print(f"🧪 黄金回归集（{len(cases)} 个 query）\n")
    failures = []
    total_time = 0.0
    for i, (query, expect_layers, desc) in enumerate(cases, 1):
        t0 = time.time()
        data = do_search_all(query, DEFAULT_HOURS, set())
        elapsed = (time.time() - t0) * 1000
        total_time += elapsed
        layer_map = {"skills": len(data["skills"]), "semantic": len(data["pm"]),
                     "l0": len(data["sess"]), "l1": len(data["logs"]), "facts": len(data["facts"])}
        hits = [lay for lay in expect_layers if layer_map.get(lay, 0) > 0]
        ok = len(hits) >= 1
        mark = "✅" if ok else "❌"
        print(f"{mark} {i:2d}. '{query}' ({desc})")
        print(f"     预期{expect_layers} 命中{hits or '无'} | {elapsed:.0f}ms | "
              f"层计数={layer_map}")
        if not ok:
            failures.append(query)
    print(f"\n{'✅ 全部通过' if not failures else '❌ 失败 ' + str(len(failures)) + ' 个: ' + str(failures)}")
    print(f"总耗时 {total_time/1000:.1f}s，平均 {total_time/len(cases):.0f}ms/query")
    log_metrics("__regression__", "test", {}, len(cases) - len(failures),
                int(total_time), 0 if not failures else 1, rerank_applied=False)
    return 0 if not failures else 1


def cmd_list(args):
    cutoff = datetime.now(TZ) - timedelta(hours=args.hours)
    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if ".trajectory." in f.name:
            continue
        if f.stat().st_mtime < cutoff.timestamp():
            continue
        msgs = parse_session_file(f)
        user_msgs = [m for m in msgs if m["role"] == "user"]
        if user_msgs:
            sessions.append({
                "file": f.stem,
                "messages": len(msgs),
                "first": user_msgs[0]["content"][:100],
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, TZ).strftime("%m-%d %H:%M"),
            })
        if len(sessions) >= args.max:
            break
    print(f"📋 最近 {args.hours}h 的会话（共 {len(sessions)} 个）:\n")
    for i, s in enumerate(sessions, 1):
        print(f"{i}. [{s['mtime']}] {s['first']}")
        print(f"   📁 {s['file']} | {s['messages']} 条消息")
        print()


def main():
    import os  # noqa: F811 (re-import for clarity in main)
    parser = argparse.ArgumentParser(description="记忆检索工具")
    sub = parser.add_subparsers(dest="cmd")

    s = sub.add_parser("search", help="L0 session 关键词搜索")
    s.add_argument("keyword")
    s.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    s.add_argument("--max", type=int, default=10)
    s.add_argument("--exclude-session", type=str, default=None)

    a = sub.add_parser("all", help="全层搜索（推荐）")
    a.add_argument("keyword")
    a.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    a.add_argument("--exclude-session", type=str, default=None)
    a.add_argument("--json", action="store_true", help="JSON 输出")

    t = sub.add_parser("test", help="黄金回归集")

    l = sub.add_parser("list", help="列出最近会话")
    l.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    l.add_argument("--max", type=int, default=20)

    args = parser.parse_args()

    if args.cmd == "search":
        sys.exit(cmd_search(args))
    elif args.cmd == "all":
        sys.exit(cmd_all(args))
    elif args.cmd == "test":
        sys.exit(cmd_test())
    elif args.cmd == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
