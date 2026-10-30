"""memory-ops 公共工具（P0 新增）。

约束：
  * 只依赖标准库，**Python 3.9 兼容**（launchd 用 /usr/bin/python3 = 3.9.6）
  * 不做网络调用、不加载 embedding 模型（探针每 15 min 跑一次，必须轻）
  * 所有路径都可用环境变量覆盖，便于评测时指向子集语料
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path

HOME = Path.home()
MEMORY_DIR = Path(os.environ.get("MEM_DAILY_NOTES_DIR", HOME / ".coremem/memory"))
GOLDEN_DIR = MEMORY_DIR / "golden"
HEALTH_DIR = Path(os.environ.get("MEM_HEALTH_DIR", HOME / ".coremem/health"))
DATA_DIR = Path(os.environ.get("MEM_DATA_DIR", HOME / ".coremem/data"))
LEDGER = DATA_DIR / "write_ledger.jsonl"
DECISIONS = DATA_DIR / "recall_decisions.jsonl"

DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}.*\.md$")
REF_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+)$")


def norm(s: str) -> str:
    """NFKC 归一 + 折叠空白。用于行内容比对与哈希。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


LINE_PREFIX = re.compile(r"^-\s+\d{4}-\d\d-\d\dT[\d:+\-]+\s*(\[[^\]]*\])?\s*")


def content_of(line: str) -> str:
    """剥掉 L1 行的 '- <ts> [tags] ' 前缀，只留正文。

    build 与 eval 必须用同一个函数算 sha —— 曾经 build 用正文、eval 用整行，
    导致 300 题里 272 题被判成「行被编辑过」（假 drift）。
    """
    return LINE_PREFIX.sub("", (line or "").strip()).strip()


def line_sha(s: str) -> str:
    """行内容指纹（16 位）。用于「行号漂移」与「文本被改写」检测。"""
    return hashlib.sha256(norm(s).encode("utf-8")).hexdigest()[:16]


def parse_ref(ref: str):
    """'2026-10-21.md:286' -> ('2026-10-21.md', 286)；不合法返回 None。

    注意两种并存的历史格式：
      * golden 锚点 / 文档：'2026-10-18.md:284'（带扩展名）
      * memory_recall L1 输出：'2026-10-20:450'（stem，无扩展名）
    parse_ref 保持原样返回；resolve 侧做扩展名兜底。
    """
    m = REF_RE.match((ref or "").strip())
    if not m:
        return None
    return m.group("file"), int(m.group("line"))


def ref_has_ext(ref: str) -> bool:
    p = parse_ref(ref)
    return bool(p and p[0].endswith(".md"))


def _candidates(fname: str, memory_dir=None):
    """按扩展名兜底，返回该 ref 可能对应的文件路径序列。"""
    d = Path(memory_dir or MEMORY_DIR)
    yield d / fname
    if not fname.endswith(".md"):
        yield d / (fname + ".md")


def daily_files(memory_dir: Path | None = None) -> list:
    d = Path(memory_dir or MEMORY_DIR)
    if not d.exists():
        return []
    return sorted((f for f in d.glob("*.md") if DAILY_RE.match(f.name)), reverse=True)


def read_lines(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def resolve(ref: str, memory_dir: Path | None = None):
    """把 'file.md:line' 解析回原始行文本；解析不到返回 None。"""
    p = parse_ref(ref)
    if not p:
        return None
    f, n = p
    for path in _candidates(f, memory_dir):
        lines = read_lines(path)
        if lines and 1 <= n <= len(lines):
            return lines[n - 1]
    return None


def resolve_fuzzy(ref: str, sha: str, radius: int = 50, memory_dir: Path | None = None):
    """行号漂移兜底：在 [n-radius, n+radius] 内按 sha 找回该行。

    返回 (行号, 行文本)；找不到返回 (None, None)。
    """
    p = parse_ref(ref)
    if not p:
        return None, None
    f, n = p
    lines = []
    for path in _candidates(f, memory_dir):
        lines = read_lines(path)
        if lines:
            break
    if not lines:
        return None, None
    lo, hi = max(1, n - radius), min(len(lines), n + radius)
    for i in range(lo, hi + 1):
        if line_sha(lines[i - 1]) == sha:
            return i, lines[i - 1]
    return None, None


def estimate_tokens(text: str) -> int:
    """轻量 token 估算（探针用，非计费口径）。

    规则：CJK 每字 1 token，其余按 4 字符 1 token。只用于趋势比较，
    需要精确计数时用 golden_eval.py 的 count_tokens()（真 tokenizer）。
    """
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff]", text or ""))
    rest = max(0, len(text or "") - cjk)
    return cjk + (rest + 3) // 4


def read_jsonl(path: Path, limit: int | None = None):
    out = []
    if not Path(path).exists():
        return out
    try:
        for i, line in enumerate(Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()):
            if limit is not None and i >= limit:
                break
            line = line.strip()
            if not line:
                continue
            import json
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out


# ---- 探针级别语义（关键：不要让未就绪的探针污染会话注入）----
#
# pi 的 memory-health 扩展会注入所有 level != "green" 的项。
# 因此「只记录、不告警」的探针必须报 green，并带 record_only=True，
# 而不是引入新 level —— 否则新探针会在每次会话首条消息刷红。
def check(cid, label, level, value=None, unit="", detail="", warn_at=None,
          red_at=None, record_only=False):
    if record_only:
        level = "green"
    return {"id": cid, "label": label, "level": level, "value": value, "unit": unit,
            "warn_at": warn_at, "red_at": red_at, "detail": detail,
            "record_only": bool(record_only)}


def threshold_level(value, warn, red, higher_is_bad=True):
    """统一的阈值判定；value 为 None 视为无数据（返回 green，由调用方决定）。"""
    if value is None:
        return "green"
    if higher_is_bad:
        if red is not None and value >= red:
            return "red"
        if warn is not None and value >= warn:
            return "yellow"
    else:
        if red is not None and value <= red:
            return "red"
        if warn is not None and value <= warn:
            return "yellow"
    return "green"