#!/usr/bin/env python3
"""L3 派生规则层：从 L1 事实里归纳「行为规律」，四道闸全过才落库。

为什么需要它：现在有事实层（L1/L2）和手工决策层（MEMORY.md MANUAL），
中间缺一层「从大量事实归纳出的稳定规律」。缺这层，每次都要回读原文才能做判断。

为什么不能直接照搬现成框架的 System 2：那些实现全量扫描、且归纳结果没有 provenance。
本实现只做一件事 —— **归纳，并且要求每条规则都能指回具体行**。

四道闸（借自 Hy-Memory 的 validate_new_schema_evidence，2026-10-26 评估）：
  G1 kind == behavior_pattern   显式状态（"用户住深圳"）不是规律 → 打回事实层
  G2 evidence 全部可解析         引用的 ref 必须存在且 sha 对得上
  G3 subject 一致                 证据必须讲同一个主体（人/公司）
  G4 来源 ≥2 个独立日期           同一天说两遍不算规律

成本控制：只跑最近 N 天（默认 7），不是全量。全量 19k 条 = 每次约 800 次 LLM 调用。

用法：
  python3 l3_schema_induct.py --days 7 --dry-run          # 只看会写出什么
  python3 l3_schema_induct.py --days 7 --write             # 写入 MEMORY.md 的 DERIVED 区
  python3 l3_schema_induct.py --revalidate                 # 只复核已有规则的证据是否失效
退出码：0=正常，1=有规则被拒（有日志），2=配置/路径问题
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import mo_common as M
except ImportError:  # 允许从别处运行
    M = None

MEMORY_DIR = Path(os.environ.get("MEM_DAILY_NOTES_DIR", Path.home() / ".coremem/memory"))
L3 = Path(os.environ.get("MEM_MEMORY_MD", Path.home() / ".coremem/MEMORY.md"))
LLM_BASE = os.environ.get("MEM_LLM_BASE_URL", "http://127.0.0.1:9999/v1")
LLM_MODEL = os.environ.get("MEM_LLM_MODEL", "Qwen3.8-Flash-Next-oQ4e-mtp")
EMBED_BASE = os.environ.get("MEM_EMBED_BASE_URL", "http://127.0.0.1:9999/v1")
EMBED_MODEL = os.environ.get("MEM_EMBED_MODEL", "Qwen3-Embedding-0.6B")

BEGIN = "<!-- DERIVED:BEGIN 自动生成，勿手改 -->"
END = "<!-- DERIVED:END -->"

LINE_PREFIX = re.compile(r"^-\s*\d{4}-\d{2}-\d{2}T[^\s]*\s*(\[[^\]]*\])?\s*")


# ---------------- 数据 ----------------

@dataclass
class Fact:
    ref: str          # 2026-10-24.md:592
    sha: str
    text: str
    day: str          # 2026-10-24


def line_sha(line: str) -> str:
    if M:
        return M.line_sha(line)
    import hashlib
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def load_facts(days: int) -> list[Fact]:
    cutoff = date.today() - timedelta(days=days)
    out: list[Fact] = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", p.name)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < cutoff:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            body = LINE_PREFIX.sub("", line).strip()
            if len(body) < 12:
                continue
            out.append(Fact(f"{p.name}:{i}", line_sha(line), body, m.group(1)))
    return out


def embed(texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request(
        f"{EMBED_BASE}/embeddings",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return [x["embedding"] for x in d["data"]]


def cluster(facts: list[Fact], min_size: int, thresh: float = 0.80) -> list[list[Fact]]:
    """朴素贪心聚类：够用即可，不引第三方库。"""
    if not facts:
        return []
    vecs = embed([f.text for f in facts])
    used = [False] * len(facts)
    clusters: list[list[Fact]] = []
    for i in range(len(facts)):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        for j in range(i + 1, len(facts)):
            if used[j]:
                continue
            dot = sum(a * b for a, b in zip(vecs[i], vecs[j]))
            na = sum(a * a for a in vecs[i]) ** 0.5
            nb = sum(b * b for b in vecs[j]) ** 0.5
            if na and nb and dot / (na * nb) >= thresh:
                group.append(j)
                used[j] = True
        if len(group) >= min_size:
            clusters.append([facts[k] for k in group])
    return clusters


# ---------------- LLM 归纳 ----------------

PROMPT = """下面是从记忆里取出的若干条事实。请判断：它们是否能归纳出一条**稳定的行为规律**。

严格区分两种情况：
- behavior_pattern：多次出现的行为倾向/偏好/工作方式/判断标准（例：倾向于先取证再下结论）
- explicit_state：某个具体的、一次性的状态或取值（例：某服务器 IP 是 x、某天决定用 A 方案）

规矩：
1. 如果只是具体状态/单次决定 → kind 必须是 "explicit_state"，rule 留空。
2. 如果是行为规律 → kind="behavior_pattern"，rule 一句话（不超过 40 字），
   并列出**支撑它的证据行号**（必须来自上面给出的 ref，至少 2 条，且来自不同日期）。
3. 证据不足就直接 kind="none"。

只输出 JSON，不要解释：
{{"kind":"behavior_pattern|explicit_state|none","rule":"","subject":"","evidence":["ref1","ref2"]}}

事实：
{facts}
"""


def ask_llm(cluster_facts: list[Fact]) -> dict:
    lines = "\n".join(f"- [{f.ref}] ({f.day}) {f.text}" for f in cluster_facts)
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(facts=lines)}],
        "max_tokens": 500,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(f"{LLM_BASE}/chat/completions", data=body,
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
    raw = (d["choices"][0]["message"].get("content") or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {"kind": "none", "rule": "", "subject": "", "evidence": [], "_raw": raw[:200]}


# ---------------- 四道闸 ----------------


_ENT_RE = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),      # 邮箱
    re.compile(r"https?://[^\s，。）)]+"),           # URL
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),  # IP
    re.compile(r"[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*"),  # 含数字的标识符
    re.compile(r"(?<![\d.])\d{1,3}(?![\d.])"),     # 短数字（人数/天数/阈值）
]


def _concrete_entities(text: str) -> list[str]:
    """抽出「具体值」——用来判断多条证据是不是在说同一件事。"""
    out = []
    for rx in _ENT_RE:
        out.extend(m.group(0) for m in rx.finditer(text or ""))
    return [x for x in dict.fromkeys(out) if x]      # 不再丢弃长度 1 的值（"9" 这类也要参与判重）

def _evidence_similarity(texts: list[str]) -> list[float]:
    """证据两两余弦（取每对的最小值）。嵌入不可用时返回 []，即跳过此闸。"""
    if len(texts) < 2:
        return []
    try:
        vecs = embed(texts)
    except Exception:
        return []
    out = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            dot = sum(a * b for a, b in zip(vecs[i], vecs[j]))
            na = sum(a * a for a in vecs[i]) ** 0.5
            nb = sum(b * b for b in vecs[j]) ** 0.5
            if na and nb:
                out.append(dot / (na * nb))
    return out


# 主体必须是**行为主体**（人/系统），而不是抽象概念。
# 复核 2026-10-26 的实测：LLM 会把 subject 填成"模型选择策略"这类名词短语，
# 证据里当然不含这个词 → G3 把两条真规律误杀。改为从证据反推主体。
ACTORS = ["用户", "我", "Brian", "助手", "执行器", "agent", "吉诚", "客户", "候选人"]
_ALIASES = [
    {"执行器", "助手", "assistant", "agent"},
    {"用户", "我", "brian"},
]


def _actors_in(text: str) -> set[str]:
    t = (text or "").lower()
    return {a for a in ACTORS if a.lower() in t}


def _infer_actor(texts: list[str]) -> str | None:
    """证据共同提到的行为主体。交集为空则返回 None。"""
    sets = [_actors_in(t) for t in texts]
    if not sets:
        return None
    common = set.intersection(*sets) if sets else set()
    if not common:
        return None
    # 归一到规范名（助手/执行器/agent 视为同一主体）
    for group in _ALIASES:
        hit = common & group
        if hit:
            return sorted(hit)[0]
    return sorted(common)[0]


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（用于判断两条证据是不是在说同一件事）。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _subject_matches(subject: str, text: str) -> bool:
    names = {subject}
    for group in _ALIASES:
        if subject in group:
            names |= group
    return any(n and n in text for n in names)


def gate(kind: str | None, rule: str, subject: str, evidence: list[str],
         facts_by_ref: dict[str, Fact],
         similarity: list[float] | None = None) -> tuple[bool, str]:
    """返回 (是否通过, 拒绝原因)。刻意保守：宁可漏收，不可乱收。"""
    # G1：状态不是规律
    if kind != "behavior_pattern":
        return False, "G1_not_pattern"
    if not rule.strip():
        return False, "G1_empty_rule"

    # G2：证据必须存在，且 **sha 对得上**（2026-10-26 复核指出：此前只查了 ref 存在）
    refs = [r for r in dict.fromkeys(evidence) if r]
    if not refs:
        return False, "G2_no_evidence"
    for raw in refs:
        ref, _, sha = raw.partition("#")
        if ref not in facts_by_ref:
            return False, f"G2_unknown_evidence:{ref}"
        if sha and sha != facts_by_ref[ref].sha:
            return False, f"G2_sha_mismatch:{ref}"

    # G3：**接地性** —— 规则的主体必须在证据里真实出现过。
    #
    # 演进：① 「只在一条证据里提到主体就放行」→ 太松（4 条证据里 2 条写"助手"也能过）；
    #       ② 「全部证据都必须提到主体」→ 太严：中文常省略主语
    #          （"确定执行类任务继续使用 deepseek-flash"），把真决策误杀；
    #       ③ 现在：从证据里推断主体，要求主体**至少被一条证据点名**。
    #          主体一致性交给 G5（重复判定）与 G1（状态/规律判定），
    #          因为"是否同一主体"在中文里无法用字符串可靠判定。
    refs_text = [facts_by_ref[r.partition("#")[0]].text for r in refs]
    named = set().union(*[_actors_in(t) for t in refs_text]) if refs_text else set()
    if not named:
        return False, "G3_no_actor_in_evidence"
    if subject.strip() and subject.strip() in ACTORS:
        if subject.strip() not in named:
            return False, f"G3_subject_not_grounded:{subject.strip()}"
    else:
        subject = sorted(named)[0]      # LLM 给了抽象名词 → 用证据里的主体

    # G4：≥2 个独立日期
    days = {facts_by_ref[r.partition("#")[0]].day for r in refs}
    if len(days) < 2:
        return False, "G4_needs_two_days"

    # G5：同一件事重复 ≠ 行为规律。判据演进记在这里，免得再走回头路：
    #   ① 嵌入余弦 ≥0.95 → 判反（真规律措辞几乎一样被误杀，重复任务措辞略变被放过）；
    #   ② 最长公共子串 ≥8 → 过严：把「同一原则在不同场景反复应用」也当重复
    #      （100 簇@0.65 实测：漏收 21 条里 12 条是这类）；
    #   ③ 现在：**具体物件重叠** —— 同一邮箱/同一批数字/同一 URL-ID 同时出现 ≥2 个才判重复。
    #      只共享抽象谓词（"硬编码""积压""私有仓库"）不算同一任务。
    # 注：①之前的实体版永不触发，根因是抽取时把长度 <2 的值丢掉了（已修）。
    ents = [set(_concrete_entities(facts_by_ref[r.partition("#")[0]].text)) for r in refs]
    if len(ents) >= 2:
        shared = set.intersection(*ents)
        if len(shared) >= 2:
            return False, "G5_same_task_repeated:" + ",".join(sorted(shared)[:2])
    # G6：敏感动作不得被写成"倾向"。
    # 实测（2026-10-26 草稿）出现过一条：
    #   "用户倾向于批量外发候选人隐私数据（微信/简历）至指定邮箱"
    # 它把一个具体导出任务固化成对用户的行为定性；一旦进入长期上下文，
    # 等于让 agent 认为「外发隐私是用户偏好」。
    _rule = rule or ""
    if re.search(r"倾向|习惯|偏好", _rule) and re.search(
            r"隐私|简历|微信|密码|密钥|token|凭据|身份证|手机号", _rule):
        return False, "G6_sensitive_as_preference"
    return True, ""


# ---------------- L3 读写 ----------------

def render_block(rules: list[dict]) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    out = [BEGIN,
           f"<!-- 生成于 {now}｜共 {len(rules)} 条｜状态：待审 -->",
           "<!-- 规则由机器归纳，仅供人复核参考；未经确认不要当作既定事实引用。 -->",
           ""]
    if not rules:
        out.append("_（本次没有归纳出通过闸门的规律）_")
    for r in rules:
        out.append(f"- **{r['rule']}**")
        ev = []
        for e in r.get("evidence", []):
            ev.append(e if "#" in e else e)      # gate 已把 sha 归一到 ref#sha
        out.append(f"  - 主体：{r['subject']}｜证据：{', '.join(ev)}")
    out.append(END)
    return "\n".join(out)


def write_l3(rules: list[dict]) -> None:
    """写入 DERIVED 区。**宁可失败，也不能误删 MANUAL。**

    复核发现（2026-10-26，已在副本上复现）：原实现用非贪婪正则
    `BEGIN.*?END` 整体替换。一旦标记位置错乱（比如 BEGIN 出现两次、
    或 MANUAL 被夹在两个标记之间），会把中间内容**整段删掉** ——
    实测 8 行 → 9 行、MANUAL 消失。这个函数每周自动跑，写的是生产 MEMORY.md。

    现在：
      1. 校验标记：BEGIN/END 各恰好出现一次且顺序正确，否则抛错（调用方退出码 2），不写入
      2. 用 find 切片替换，不走正则 —— 规则文本里的 `\\` 或 `\\g<0>` 不会变成替换语法
      3. 先写临时文件再 os.replace（原子）
      4. 写前备份 .bak-时间戳，保留最近 5 份
    """
    block = render_block(rules)
    if L3.exists():
        s = L3.read_text(encoding="utf-8")
    else:
        s = "# MEMORY.md - Durable Facts and Decisions\n"

    nb, ne = s.count(BEGIN), s.count(END)
    if nb == 0 and ne == 0:
        # 首次：追加一整个新区块
        s = s.rstrip("\n") + "\n\n## 派生规则（DERIVED，自动生成）\n\n" + block + "\n"
    else:
        if nb != 1 or ne != 1:
            raise ValueError(f"DERIVED 标记异常（BEGIN×{nb}, END×{ne}），拒绝写入以免丢失正文")
        i, j = s.find(BEGIN), s.find(END)
        if i < 0 or j < 0 or j < i:
            raise ValueError("DERIVED 标记顺序异常，拒绝写入")
        # 不变量：区块内只允许出现**我们自己生成的格式**。
        # 若有人把正文粘进了 BEGIN..END 之间（手工编辑最容易发生的形态），
        # 直接替换会把它删掉 —— 所以检测到异常内容就拒绝，交人工处理。
        inner = s[i + len(BEGIN): j]
        suspicious = [ln for ln in inner.splitlines()
                      if ln.strip() and (re.match(r"^#{1,6}\s", ln.strip()) or len(ln) > 300)]
        if suspicious:
            raise ValueError("DERIVED 区内含非生成内容（例：%s），拒绝写入以免丢失"
                             % suspicious[0].strip()[:40])
        s = s[:i] + block + s[j + len(END):]

    # 备份（保留最近 5 份）
    if L3.exists():
        import shutil, time as _t
        bak = L3.with_suffix(L3.suffix + ".bak-" + _t.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copy2(L3, bak)
            olds = sorted(L3.parent.glob(L3.name + ".bak-*"))
            for old in olds[:-5]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    tmp = L3.with_suffix(L3.suffix + ".tmp")
    tmp.write_text(s, encoding="utf-8")
    os.replace(tmp, L3)


def existing_rules() -> list[dict]:
    if not L3.exists():
        return []
    s = L3.read_text(encoding="utf-8")
    m = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), s, flags=re.S)
    if not m:
        return []
    rules = []
    for line in m.group(1).splitlines():
        mm = re.match(r"- \*\*(.+?)\*\*", line.strip())
        if mm:
            rules.append({"rule": mm.group(1)})
    return rules


def revalidate() -> int:
    """复核已有规则的证据是否失效。

    复核指出（2026-10-26）：原实现在**规则那一行**找 ref，而证据写在下一行，
    所以 ref 永远为空 —— 无论证据是否失效都报"有效"。现改为解析证据行，
    并同时校验 sha（行被编辑过 → sha 变 → 判失效）。
    """
    facts = {f.ref: f for f in load_facts(days=400)}
    if not L3.exists():
        print("  没有 MEMORY.md")
        return 0
    s = L3.read_text(encoding="utf-8")
    m = re.search(re.escape(BEGIN) + r"(.*?)" + re.escape(END), s, flags=re.S)
    if not m:
        print("  没有 DERIVED 区")
        return 0

    blocks, cur = [], None
    for line in m.group(1).splitlines():
        if line.strip().startswith("- **"):
            cur = {"rule": line.strip().strip("- *"), "ev": []}
            blocks.append(cur)
        elif cur is not None and "证据：" in line:
            cur["ev"] = re.findall(r"(\d{4}-\d{2}-\d{2}\.md:\d+(?:#[0-9a-f]+)?)", line)
    if not blocks:
        print("  没有已存规则")
        return 0

    stale = []
    for b in blocks:
        bad = []
        for item in b["ev"]:
            ref, _, sha = item.partition("#")
            f = facts.get(ref)
            if f is None:
                bad.append(ref + "(行不存在)")
            elif sha and sha != f.sha:
                bad.append(ref + "(sha 变了)")
        if bad:
            stale.append((b["rule"], bad))
    if stale:
        print(f"  ⚠️ {len(stale)}/{len(blocks)} 条规则的证据已失效：")
        for rule, bad in stale:
            print(f"    - {rule[:44]}  ← {bad[:2]}")
        return 1
    print(f"  ✓ {len(blocks)} 条规则证据均有效（ref 存在且 sha 一致）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-cluster", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=0.80, help="聚类相似度阈值")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--draft-out", default="", help="只写草稿文件，不碰 MEMORY.md（定时任务用这个）")
    ap.add_argument("--revalidate", action="store_true")
    ap.add_argument("--limit-clusters", type=int, default=20)
    ap.add_argument("--json", default="", help="把结果写成 JSON（供探针/定时任务消费）")
    a = ap.parse_args()

    if a.revalidate:
        return revalidate()
    if not MEMORY_DIR.exists():
        print(f"❌ 找不到语料目录 {MEMORY_DIR}", file=sys.stderr)
        return 2

    facts = load_facts(a.days)
    print(f"  取近 {a.days} 天：{len(facts)} 条事实")
    if not facts:
        return 0
    by_ref = {f.ref: f for f in facts}
    groups = cluster(facts, a.min_cluster, thresh=a.threshold)
    print(f"  聚类（阈值 {a.threshold}，≥{a.min_cluster} 条）：{len(groups)} 簇")

    passed, rejected = [], []
    for g in groups[: a.limit_clusters]:
        ans = ask_llm(g)
        # 关键：把 LLM 给的裸 ref 补成 ref#sha，否则 G2 的 sha 校验会被跳过
        # （复核 2026-10-26 指出"G2 声称校验 sha 但代码里没有"，根因就在这里）
        ev = []
        for r in (ans.get("evidence") or []):
            r = str(r).strip()
            if r in by_ref:
                ev.append(f"{r}#{by_ref[r].sha}")
            else:
                ev.append(r)
        ok, why = gate(ans.get("kind"), ans.get("rule", ""), ans.get("subject", ""),
                       ev, by_ref)
        rec = {"rule": ans.get("rule", ""), "subject": ans.get("subject", ""),
               "evidence": ev, "kind": ans.get("kind")}
        if ok:
            passed.append(rec)
        else:
            rec["reason"] = why
            rejected.append(rec)

    print(f"  通过 {len(passed)} 条 / 拒绝 {len(rejected)} 条")
    for r in rejected[:8]:
        print(f"    ✗ {r['reason']:26} {str(r.get('rule'))[:40]}")
    for r in passed[:8]:
        print(f"    ✓ {r['rule'][:46]}  ← {len(r['evidence'])} 条证据")

    if a.json:
        Path(a.json).write_text(json.dumps({
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "days": a.days, "facts": len(facts), "clusters": len(groups),
            "candidates": min(len(groups), a.limit_clusters),
            "passed": len(passed), "rejected": len(rejected),
            "reject_reasons": {r["reason"]: sum(1 for x in rejected if x["reason"] == r["reason"])
                               for r in rejected if "reason" in r},
            "rules": [{"rule": r["rule"], "subject": r["subject"], "evidence": r["evidence"]}
                      for r in passed],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    if a.draft_out:
        # 精确率实测约 57%：不足以自动进入检索层。定时任务只产草稿，人工确认后再 --write。
        draft = Path(a.draft_out)
        draft.parent.mkdir(parents=True, exist_ok=True)
        head = (f"# L3 归纳草稿（阈值 {a.threshold}，{len(passed)} 条，待人工确认）\n\n"
                f"> 本次：{len(facts)} 条事实 / {len(groups)} 簇 / 送审 {min(len(groups), a.limit_clusters)} 簇\n"
                f"> 实测精确率约 57%（100 簇校准），**不要当成事实引用**；确认后再 `--write`。\n\n")
        draft.write_text(head + render_block(passed) + "\n", encoding="utf-8")
        print(f"  已写草稿 {draft}（未改动 MEMORY.md）")
    elif a.write:
        write_l3(passed)
        print(f"  已写入 {L3} 的 DERIVED 区")
    elif a.dry_run:
        print("  （dry-run，未写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
