#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# 数据源（2026-11-03 解耦）：默认只扫 pi 会话。OpenClaw 历史归档由
# openclaw_archive.py 只读适配器提供，只有 --sources openclaw|all 时才加载。
# 一次性历史回填入口：migrate_openclaw_archive.py。
# ---------------------------------------------------------------------------
"""
memory_capture.py v1.3 — 会话自动捕获器（含 Triage Gate，三源扫描）
=========================================================
扫最近的 session JSONL → 抽取对话 → Triage 门控（正则前置+LLM 兜底）→ 提取事实 → 写入记忆。

用法：
  python3 scripts/memory_capture.py                    # 分析最近 60min 的会话
  python3 scripts/memory_capture.py --hours 24          # 分析 24h（夜间回溯模式）
  python3 scripts/memory_capture.py --dry-run           # 预览不写入

v1.4 变更（2026-09-11 通道链韧性修复，.bak.20260911-chan）：
  - _llm_chat 通道链重排：local-omlx-flash（本机 omlx，零配额/零402/零超时）→
    bailian deepseek-v4-flash-0731（timeout 240s）→ scnet-tp DeepSeek-V4-Flash →
    bailian qwen3.8-flash。每通道独立 timeout + 单次重试。
  - deepseek-official 退役注释（HTTP 402 余额尽，2026-09-11 实证多轮全链失败）。
  - omlx 端点从 ~/.boss-agent/llm-policy.json 读（local_model / local_base），
    现由 coremem providers 配置 / MEM_OMLX_BASE_URL 提供，缺省 127.0.0.1:9999/v1。
  - 测试钩子：MEMORY_CAPTURE_SKIP_CHANNELS="local-omlx-flash,bailian" 可跳过指定通道
    （断网演练/降级验证用，正常运行不设置）。
  - 提取 prompt、JSON 输出格式、Triage Gate、调度参数一律未动（unified-facts 下游格式锁死）。

v1.3 变更（2026-09-11 记忆系统断供修复）：
  - 扫描源从单一 OpenClaw jsonl 扩展为三源：
    ① OpenClaw 旧版 *.jsonl（历史归档）
    ② OpenClaw 2026.8.1+ SQLite transcript_events（09-01 升级后 jsonl 不再产生，只读连接）
    ③ pi 会话 ~/.pi/agent/sessions/<project>/*.jsonl（日常主战场，格式与 OpenClaw 兼容）
  - 背景：09-01 后 cron 断供（modelPolicy 拦截）+ jsonl 源枯竭，双重断供导致 10 天零捕获

v1.2 变更（2026-08-12 P3，qwen3.8-max 审核后实施）：
  - Triage Gate：正则前置拦截纯确认/emoji/系统输出/脚本日志类 meta-noise，LLM 兜底二次判断
  - gate 留痕：被拦截内容追加写入当日 daily log（可追溯）
  - 修复 extract_facts.py 死引用（该脚本不存在，直接走 extract_via_openclaw）
  - 审计：--audit-skipped 列出最近 gate 记录

设计目标：
  - 根治 Aisix 型失忆：会话结束后自动提取事实，不靠 agent 纪律
  - 止住 meta-noise 污染：cron 元对话（"脚本输出✅时回复NO_REPLY"）不入 semantic 层
  - 用 v4-flash（免费）提取，零成本
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

PI_SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
VALID_SOURCES = ("pi", "openclaw", "all")


def _openclaw_archive():
    """Load the optional legacy adapter only when explicitly requested."""
    from openclaw_archive import OpenClawArchive
    return OpenClawArchive()


def _providers_config() -> dict:
    """Optional coremem-local provider config; never reads OpenClaw config."""
    import os
    path = Path(os.environ.get("MEM_PROVIDERS_CONFIG", str(Path.home() / ".coremem/config/providers.json")))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
REMEMBER_SCRIPT = Path.home() / ".coremem" / "scripts" / "remember.py"
STATE_FILE = Path.home() / ".coremem" / "data" / "memory_capture_state.json"
GATE_LOG_DIR = Path.home() / ".coremem" / "logs" / "memory-gate"
TZ = timezone(timedelta(hours=8))

# ============ Triage Gate 正则规则（v1.2 P3） ============
# 命中任一规则 → 直接 gate（拦截），不送 LLM 提取。
# 设计原则：保守化，只拦纯确认/emoji/系统输出/脚本日志类，绝不拦决策/偏好/数字/实体。
GATE_REGEX_PATTERNS = [
    # 纯确认类
    r"^(好的|好|收到|知道了|明白|可以|行|嗯|对|是的|OK|ok|了解|没问题|没问题了|收到，明白了|好的明白了|嗯嗯|好嘞|好滴)[！!。.~～]*$",
    r"^(谢谢|感谢|辛苦了|晚安|早安|再见|拜拜)[！!。.~～]*$",
    # 纯 emoji / 符号
    r"^[✅☑️✔️👍👌🙏🎉✨🔥💯❤️😊🙂😂🤔⚠️🟢🔴🟡❌✔️➡️→←↑↓~～—…·•*]+$",
    r"^[\s✅☑️✔️👍👌🙏🎉✨🔥💯❤️😊🙂😂🤔⚠️🟢🔴🟡❌✔️➡️→←↑↓~～—…·•*0-9.、，。！!?？]{1,10}$",
    # NO_REPLY / HEARTBEAT 类（系统静默标记）
    r"^NO_REPLY$",
    r"^HEARTBEAT_OK$",
    r"^(NO_REPLY|HEARTBEAT_OK)(\s|\n|$)",
    # 脚本输出 / 日志类
    r"^\[(memory_capture|diagnostic|self_check|healthcheck)[^\n]*",
    r"^(✅|❌|⚠️|🔴|🟢|🟡)?\s*(脚本|cron|任务|job)[^\n]*(执行|运行|完成|失败|输出)[^\n]*",
    r"^\[(.*?)\] (✅|❌|⚠️) .{0,60}$",  # 单行系统状态（[xxx] ✅ ...）
    r"^\s*(Process exited with code|Command still running|Traceback|Error|✔|✖)[^\n]*",
]
GATE_REGEX = re.compile("|".join(f"(?:{p})" for p in GATE_REGEX_PATTERNS), re.MULTILINE)

# 提取 prompt：让便宜模型从对话中抽取可记住的事实
EXTRACT_PROMPT = """你是记忆提取助手。从以下对话中抽取值得长期记住的事实，每行一条。

只抽取以下类型的信息：
- 决策：用户做了选择、定了方案、拍板了什么
- 偏好：用户说了喜欢/不喜欢什么、有什么习惯
- 数字：金额、日期、数量、配置参数
- 实体：新的项目名、产品名、人名、公司名
- 事件：完成了什么、发生了什么重要的事

格式要求：
- 每行一条，不要编号
- 用简洁的中文陈述句
- 不要重复对话中已有的信息
- 如果没有值得记忆的事实，输出"无"

P0 双时钟（2026-08-12）：如果某条事实明确指向一个过去/特定的时间（如"3月15日签了合同""2024年涨价""上个月定了方案"），
在该行末尾附加标记 `【ET:YYYY-MM-DD】`（只写确定的日期，不确定就不写，精确到年/月也可如 【ET:2024】）。
没有明确时间指向的事实不加标记。

对话内容：
---
{conversation}
---

请提取值得长期记住的事实："""

# LLM 兜底判断 prompt（v1.2 P3）：正则未命中但疑似 meta-noise 时调用
TRIAGE_LLM_PROMPT = """你是对话质量筛选器。判断以下对话是否包含值得长期记住的事实（决策/偏好/数字/实体/事件）。

对话内容：
---
{conversation}
---

只输出一个词：
- PASS：包含值得记住的事实，应继续提取
- GATE：纯确认/emoji/系统输出/脚本日志/元对话，无值得记住的事实
"""


def now():
    return datetime.now(TZ)


def _parse_iso_dt(ts) -> Optional[datetime]:
    """把 ISO 时间字符串（含 Z / 偏移）解析为 tz-aware datetime；失败返回 None。
    Python 3.9 的 fromisoformat 不认 'Z' 后缀，故先替换为 +00:00。"""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _event_timestamp(d: dict) -> Optional[datetime]:
    """从事件字典取 tz-aware 时间戳（jsonl 的 timestamp / created_at；epoch 毫秒或 ISO 字符串）。"""
    ts = d.get("timestamp") or d.get("created_at")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(ts, str):
        return _parse_iso_dt(ts)
    return None


def _parse_local_dt(s: str) -> Optional[datetime]:
    """解析本地（+8）日期或日期时间，如 '2026-08-12' 或 '2026-08-12 04:00'；失败返回 None。"""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def triage_gate(conversation: str) -> tuple[bool, str]:
    """Triage Gate：判断对话是否值得提取。返回 (是否通过, 拦截原因)。
    - 正则前置：零成本拦截 60%+ meta-noise
    - LLM 兜底：正则未命中但内容极短/疑似元对话时，用便宜模型二次判断
    """
    conv = conversation.strip()
    if not conv:
        return False, "empty"
    # 正则前置：整段匹配或按行匹配（只要大量行命中即 gate）
    lines = [l.strip() for l in conv.split("\n") if l.strip()]
    if not lines:
        return False, "empty"
    hit_lines = [l for l in lines if GATE_REGEX.fullmatch(l) or GATE_REGEX.match(l)]
    # 若 >60% 行命中正则 → 判定为 meta-noise
    if len(hit_lines) / len(lines) > 0.6:
        return False, f"regex: {len(hit_lines)}/{len(lines)} 行命中 meta-noise 模式"
    # 若对话非常短（<30 字）且未命中正则 → LLM 兜底
    if len(conv) < 30:
        verdict = _triage_llm(conv)
        if verdict == "GATE":
            return False, "llm: 短内容判定为无事实价值"
    return True, ""


def _triage_llm(conversation: str) -> str:
    """LLM 兜底：用便宜模型判断短内容是否值得提取。失败时默认 PASS（保守，不误杀）。"""
    prompt = TRIAGE_LLM_PROMPT.format(conversation=conversation[:2000])
    text = _llm_chat(prompt, max_tokens=100, temperature=0, timeout=30)
    if text is None:
        return "PASS"
    text = text.strip().upper()
    return "GATE" if "GATE" in text else "PASS"


def gate_log(conversation: str, reason: str, session_stem: str = ""):
    """被 gate 的内容留痕：追加写入当日 daily log（可追溯，防误杀可回查）"""
    try:
        GATE_LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = now().strftime("%Y-%m-%d")
        log_file = GATE_LOG_DIR / f"{today}.md"
        ts = now().strftime("%H:%M")
        snippet = conversation.strip().replace("\n", " ⏎ ")[:300]
        entry = f"\n## {ts} memory_capture GATE（{reason}）{f'[{session_stem}]' if session_stem else ''}\n- 内容: {snippet}\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass


def load_state() -> dict:
    """加载已处理 session 的状态（v1.2：兼容旧 state 无 gate_stats 键）"""
    defaults = {"processed_sessions": {}, "last_run": None, "gate_stats": {"total": 0, "gated": 0, "passed": 0}}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                for k, v in defaults.items():
                    data.setdefault(k, v)
                return data
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    return defaults


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_run"] = now().isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def get_recent_sessions(hours: float = 1, sources: str = "pi") -> list[dict]:
    """获取最近 N 小时有活动的 session；默认只读 pi，历史源需显式启用。"""
    cutoff = (now() - timedelta(hours=hours)).timestamp()
    sessions = []
    # pi 会话 jsonl（<project-dir>/<ts>_<uuid>.jsonl）
    if sources in ("pi", "all"):
        try:
            for f in PI_SESSIONS_DIR.glob("*/*.jsonl"):
                if f.stat().st_mtime >= cutoff:
                    sessions.append({"stem": f"pi-{f.stem}", "kind": "jsonl", "path": f,
                                     "mtime": f.stat().st_mtime, "hash": None})
        except Exception as e:
            print(f"[memory_capture] ⚠️ pi 会话目录扫描失败: {e}", file=sys.stderr)
    if sources in ("openclaw", "all"):
        sessions.extend(_openclaw_archive().get_recent_sessions(hours))
    return sorted(sessions, key=lambda d: d["mtime"], reverse=True)


def _message_time_range_jsonl(filepath: Path) -> tuple:
    """返回 jsonl 文件中 message 事件的最小/最大 tz-aware 时间戳（无 message 则 (None, None)）。"""
    tmin = tmax = None
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
                dt = _event_timestamp(d)
                if dt is None:
                    continue
                if tmin is None or dt < tmin:
                    tmin = dt
                if tmax is None or dt > tmax:
                    tmax = dt
    except Exception:
        pass
    return tmin, tmax


def get_sessions_in_window(since_dt, until_dt, sources: str = "pi") -> list[dict]:
    """v1.5 定点回溯：按会话内真实消息时间戳筛选（非 mtime）。

    默认只扫描 pi；OpenClaw 历史源仅在 sources=openclaw|all 时经适配器扫描。
    只保留消息时间戳与 [since_dt, until_dt] 重叠的 session。since_dt/until_dt 为 tz-aware（UTC）。
    """
    sessions = []
    # pi 会话 jsonl
    if sources in ("pi", "all"):
        try:
            for f in PI_SESSIONS_DIR.glob("*/*.jsonl"):
                tmin, tmax = _message_time_range_jsonl(f)
                if tmin is None:
                    continue
                if tmax < since_dt or tmin > until_dt:
                    continue
                sessions.append({"stem": f"pi-{f.stem}", "kind": "jsonl", "path": f,
                                 "mtime": f.stat().st_mtime, "hash": None})
        except Exception as e:
            print(f"[memory_capture] ⚠️ pi 会话目录扫描失败: {e}", file=sys.stderr)
    if sources in ("openclaw", "all"):
        sessions.extend(_openclaw_archive().get_sessions_in_window(since_dt, until_dt))
    return sorted(sessions, key=lambda d: d["mtime"])


def extract_conversation(src, since=None, until=None) -> str:
    """从 session 源提取人机对话文本（最近 20 轮）。src 为 get_recent_sessions 描述符或 Path（兼容旧调用）。
    since/until 为 tz-aware（UTC）时，只取窗口内消息并返回全部（不截 40 行）。"""
    if isinstance(src, (str, Path)):
        src = {"kind": "jsonl", "path": Path(src)}
    if src["kind"] == "sqlite":
        return _openclaw_archive().extract_conversation(src, since, until)
    return _conversation_from_jsonl(Path(src["path"]), since, until)


def _conversation_from_jsonl(filepath: Path, since=None, until=None) -> str:
    lines = []
    windowed = since is not None or until is not None
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
                if windowed:
                    dt = _event_timestamp(d)
                    if dt is not None:
                        if since is not None and dt < since:
                            continue
                        if until is not None and dt > until:
                            continue
                _append_message_line(d, lines)
    except Exception:
        pass
    # 定点回溯时窗口已限定，返回窗口内全部消息；否则维持“最近 40 行”原行为
    return "\n".join(lines if windowed else lines[-40:])


def main():
    parser = argparse.ArgumentParser(description="会话自动捕获器")
    parser.add_argument("--hours", type=float, default=1, help="扫描最近 N 小时（默认 1h）")
    parser.add_argument("--since", type=str, default=None,
                        help="定点回溯起点（本地日期/时间，如 2026-08-12 或 '2026-08-12 04:00'），按会话内消息时间戳筛选")
    parser.add_argument("--until", type=str, default=None,
                        help="定点回溯终点（同上，如 '2026-08-12 15:17'）")
    parser.add_argument("--dry-run", action="store_true", help="预览不写入")
    parser.add_argument("--force", action="store_true", help="强制重新处理所有 session")
    parser.add_argument("--max-sessions", type=int, default=3, help="单次最多处理 session 数（默认 3，防超时）")
    parser.add_argument("--continue-on-error", action="store_true", help="补跑模式：单 session 提取失败跳过续跑（连续≥5次失败才停），默认失败即停防连环限流")
    parser.add_argument("--audit-skipped", action="store_true", help="审计最近 gate 记录（默认近 7 天）")
    parser.add_argument("--sources", choices=VALID_SOURCES, default="pi",
                        help="数据源：pi（默认）/ openclaw（仅历史归档）/ all")
    args = parser.parse_args()
    
    try:
        if args.audit_skipped:
            audit_skipped()
        else:
            since = until = None
            if args.since or args.until:
                since = _parse_local_dt(args.since) if args.since else None
                until = _parse_local_dt(args.until) if args.until else None
                if args.since and since is None:
                    print(f"[memory_capture] 错误: 无法解析 --since '{args.since}'（期望 YYYY-MM-DD 或 'YYYY-MM-DD HH:MM'）", file=sys.stderr)
                    sys.exit(1)
                if args.until and until is None:
                    print(f"[memory_capture] 错误: 无法解析 --until '{args.until}'", file=sys.stderr)
                    sys.exit(1)
                # 只给一侧时另一侧补 1 天，形成有界窗口（防 --since 单独用扫到“现在”引发大批量回溯）
                if since is not None and until is None:
                    until = since + timedelta(days=1)
                elif until is not None and since is None:
                    since = until - timedelta(days=1)
                # 统一转 UTC，与消息时间戳（UTC）比较
                since = since.astimezone(timezone.utc) if since else None
                until = until.astimezone(timezone.utc) if until else None
            capture(hours=args.hours, dry_run=args.dry_run, force=args.force, max_sessions=args.max_sessions,
                    continue_on_error=args.continue_on_error, since=since, until=until, sources=args.sources)
    except KeyboardInterrupt:
        print("\n中断")
    except Exception as e:
        print(f"[memory_capture] 错误: {e}", file=sys.stderr)
        sys.exit(1)


def _append_message_line(d: dict, lines: list):
    """OpenClaw 与 pi 的 message 事件结构兼容：{type:'message', message:{role, content(str|list)}}"""
    if d.get("type") != "message":
        return
    msg = d.get("message", {})
    role = msg.get("role", "")
    content = msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") if isinstance(c, dict) else str(c)
            for c in content
        )
    if role in ("user", "assistant") and content:
        prefix = "用户" if role == "user" else "助手"
        lines.append(f"{prefix}: {content}")


def extract_facts_with_llm(conversation: str) -> list[str]:
    """用 v4-flash 从对话中提取事实（v1.2：去掉 extract_facts.py 死引用）"""
    if len(conversation) < 100:
        return []
    return extract_facts_via_llm(conversation)


# provider 名 → 端点/模型默认。云端 provider 只有拿到 key 时才挂链（没 key 挂了也是
# 每轮白撞一次 401）；顺序由 capture_channels 决定，缺省按这里定义的顺序。
_LLM_PROVIDERS = {
    "omlx": {
        "name": "local-omlx-flash",          # _llm_chat 与回归测试按此名识别本地通道
        "base": "http://127.0.0.1:9999/v1",
        "models": ["Qwen3.8-Flash-Next-oQ4e-mtp"],
        "local": True,                        # 本地服务：无配额、无 402，key 只是占位
        "key": "omlx-local",
    },
    "bailian": {   # 阿里云百炼 TokenPlan（订阅内免费）。2026-10-03 曾因 4/4 全 403 退役，
                   # 2026-11-03 实测 qwen4-max-preview / qwen3.8-flash 均正常出文后恢复挂链。
        "name": "bailian",
        "base": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen4-max-preview", "qwen3.8-flash"],
        # qwen4/qwen3 是推理模型：不关思考链时 max_tokens 会被 reasoning_content 吃空，
        # triage（max_tokens=100）实测 100% 报 empty content。百炼兼容模式认这个顶层参数。
        "extra": {"enable_thinking": False},
    },
    "scnet-tp": {   # 国家超算互联网 TokenPlan（订阅制，免费额度内）。
                   # 2026-10-03 与 2026-11-03 实测均 HTTP 403「Token Plan subscription is
                   # expired」——订阅未续费时这条链每轮都会被自动跳过（403 不重试），
                   # 续费后改 providers.json 即可挂回，不用动代码。
        "name": "scnet-tp",
        "base": "https://api.scnet.cn/api/llm/v1",
        "models": ["Qwen3.8-Max", "DeepSeek-V4-Flash-0731", "Kimi-K3"],
    },
    "deepseek": {   # 官网按量付费：能不走就不走（本任务每 30min 高频）。
        "name": "deepseek-official",
        "base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat"],
    },
}
_LLM_DEFAULT_ORDER = ["omlx", "bailian", "scnet-tp", "deepseek"]
_CLOUD_TIMEOUT = 240   # 云端固定 240s：推理模型的 reasoning 要时间，60s 档实测频繁 read timeout


def _env_provider(name: str) -> dict:
    """环境变量取某 provider 的 key/base/model：MEM_LLM_<名>_API_KEY 或 <名>_API_KEY。"""
    up = name.upper().replace("-", "_")
    out = {}
    for suffix, field in (("_API_KEY", "key"), ("_BASE_URL", "base"), ("_MODEL", "model")):
        v = os.environ.get(f"MEM_LLM_{up}{suffix}") or os.environ.get(f"{up}{suffix}")
        if v:
            out[field] = v
    return out


def _build_llm_channels(default_timeout: int) -> list[dict]:
    """构建 fallback 通道链（v1.5：通道由配置决定，不再写死在代码里）。

    为什么要配置化：通道的取舍（哪家充值了、哪个模型退役、哪个端点老超时）是每周都变的
    运维事实，写死在代码里等于每次调整都要改代码 → 部署 → 过漂移检查。现在只改
    `~/.coremem/config/providers.json`（或 MEM_PROVIDERS_CONFIG 指过去的那份）：

      {
        "capture_channels": ["omlx", "bailian:qwen4-max-preview", "deepseek"],
        "providers": {
          "bailian":  {"baseUrl": "…", "apiKey": "sk-…",
                       "models": ["qwen4-max-preview", "qwen3.8-flash"], "timeout": 240},
          "deepseek": {"apiKey": "sk-…"},
          "omlx":     {"enabled": false}
        }
      }

    规则：
      - `capture_channels` 决定顺序与取舍，条目写 `provider` 或 `provider:model`；
        省略它 = 按 _LLM_DEFAULT_ORDER 用各 provider 的默认模型列表。
      - `providers.<名>` 覆盖端点/密钥/模型/timeout/enabled；`enabled: false` 直接不挂。
      - 环境变量优先于文件：`<PROVIDER>_API_KEY` / `_BASE_URL` / `_MODEL`
        （连字符转下划线，如 SCNET_TP_API_KEY）；本地 omlx 另认 MEM_OMLX_BASE_URL /
        MEM_OMLX_MODEL / MEM_OMLX_API_KEY / MEM_OMLX_DISABLED。
      - 一个 provider 列多个模型 = 多条通道，通道名带模型后缀，失败按序降级。
      - 未知 provider 名也能用（自带 baseUrl/apiKey/model 即可），不必改代码。
      - 提取 prompt、JSON 输出格式、Triage Gate、调度参数一律不动（unified-facts
        下游格式锁死）；这里只决定「问谁」。

    历史实证（结论留着，别再踩）：
      2026-09-11 bailian-qwen read timeout 频发 + deepseek-official HTTP 402 余额尽 → 全链
                失败：于是把本机 omlx 提为通道 1，并给每通道独立 timeout + 单次重试。
      2026-10-03 百炼 4/4 全 403「Access to model denied」、scnet 订阅过期 → 云端通道退役，
                只剩本地单通道；本地不可用则 fail-loud、不标 processed、下轮重试。
      2026-11-03 只接 DeepSeek 的机器用 omlx.enabled=false（或 MEM_OMLX_DISABLED=1）关掉本地
                通道，否则每轮白等一次唤醒超时（实测 90s）；同日百炼恢复正常，qwen4 可用。
    """
    raw = _providers_config()
    providers = (raw.get("models", {}).get("providers", {})
                 or raw.get("providers", {}) or {})
    order = raw.get("capture_channels") or _LLM_DEFAULT_ORDER

    # llm-policy.json：历史遗留的本机模型/端点来源，仅作兼容，不再是必需。
    try:
        policy = json.loads((Path.home() / ".boss-agent" / "llm-policy.json").read_text())
    except Exception:
        policy = {}

    if str(os.environ.get("MEM_OMLX_DISABLED", "")).lower() in ("1", "true", "yes"):
        providers = {**providers, "omlx": {**(providers.get("omlx") or {}), "enabled": False}}
    if policy.get("local_base") or policy.get("omlx_base"):
        om = dict(providers.get("omlx") or {})
        om.setdefault("baseUrl", policy.get("local_base") or policy.get("omlx_base"))
        if policy.get("local_model"):
            om.setdefault("model", policy["local_model"])
        providers = {**providers, "omlx": om}

    channels: list[dict] = []
    for spec in order:
        pname, _, want_model = str(spec).partition(":")
        pname = pname.strip()
        proto = _LLM_PROVIDERS.get(pname) or {"name": pname, "base": "", "models": [], "key": ""}
        pcfg = providers.get(pname) or {}
        if pcfg.get("enabled") is False:
            continue

        env = _env_provider(pname)
        if pname == "omlx":
            env["base"] = os.environ.get("MEM_OMLX_BASE_URL") or env.get("base") or ""
            env["model"] = os.environ.get("MEM_OMLX_MODEL") or env.get("model") or ""
            env["key"] = os.environ.get("MEM_OMLX_API_KEY") or env.get("key") or ""

        base = (env.get("base") or pcfg.get("baseUrl") or pcfg.get("base_url")
                or proto.get("base") or "")
        if not base:
            continue  # 没有端点就无从建通道（不挂必撞空的死端点）

        models = ([want_model] if want_model else
                  [env["model"]] if env.get("model") else
                  [pcfg["model"]] if pcfg.get("model") else
                  (pcfg.get("models") or proto.get("models") or []))
        key = (env.get("key") or pcfg.get("apiKey") or pcfg.get("api_key") or proto.get("key") or "")
        if not key:
            continue  # 云端无 key 不挂链：账户级 401 每轮白打一次，没有意义

        for model in models:
            channels.append({
                "name": f"{proto['name']}:{model}" if (want_model or len(models) > 1) else proto["name"],
                "provider": pname,
                "url": base.rstrip("/") + "/chat/completions",
                "base_url": base.rstrip("/"),
                "model": model,
                "key": key,
                "local": bool(proto.get("local")),
                # provider 级附加请求参数（如百炼的 enable_thinking）；文件里可覆盖
                "extra": (pcfg.get("extra") if pcfg.get("extra") is not None
                          else proto.get("extra") or {}),
                # 本地通道用调用方 timeout（提取 90s / triage 30s 足够，实测 6000 字 <70s）
                "timeout": (max(int(default_timeout), 30) if proto.get("local")
                            else int(pcfg.get("timeout") or proto.get("timeout") or _CLOUD_TIMEOUT)),
            })

    # 测试钩子（断网演练）：MEMORY_CAPTURE_SKIP_CHANNELS 支持 provider 名或通道名，
    # 例如 "omlx" / "local-omlx-flash" / "bailian:qwen4-max-preview"。
    skip = {s.strip() for s in os.environ.get("MEMORY_CAPTURE_SKIP_CHANNELS", "").split(",") if s.strip()}
    if skip:
        channels = [c for c in channels if c["name"] not in skip and c["provider"] not in skip]
    return channels


def _llm_chat(prompt: str, max_tokens: int, temperature: float, timeout: int = 25) -> Optional[str]:
    """按 fallback 链调用 LLM（v1.4 通道链见 _build_llm_channels）。

    韧性规则：每通道独立 timeout + 单次重试（同通道最多 2 次尝试），仍失败切下一通道；
    全链失败返回 None（fail-loud：调用方不标记 processed，下轮自动重试）。
    2026-08-17：不做 3 次退避重试拖垮 cron。2026-09-11：本地优先根治云端 402/超时连坐。
    """
    import urllib.request
    import urllib.error

    channels = _build_llm_channels(timeout)
    if not channels:
        return None

    # coremem 自有唤醒器：不再向 OpenClaw 脚本目录注入 sys.path。
    local_channel = next((c for c in channels if c.get("local")), None)
    if local_channel:
        try:
            from omlx_runtime import ensure_omlx
            if not ensure_omlx(timeout=90, base_url=local_channel["base_url"], model=local_channel["model"]):
                print("[memory_capture] omlx 唤醒超时(90s)，剔除本地通道", file=sys.stderr)
                channels = [c for c in channels if not c.get("local")]
        except Exception as _e:
            print(f"[memory_capture] ensure_omlx 异常（fail-open，按原通道链继续）: {_e}", file=sys.stderr)

    last_err = None
    for ch in channels:
        payload = {
            "model": ch["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # 本地 omlx 思考链必须关（与 boss-v4-pipeline 2026-09-08 既定惯例同源，经人力批准）：
        # 不关时 reasoning 泄入 content 污染提取结果（09-11 实弹 251 条中大量“Need maybe include”噪声）。
        # 仅通道级参数，不动 prompt/gate/解析。
        if ch.get("local"):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if ch.get("extra"):
            payload.update(ch["extra"])
        body = json.dumps(payload).encode("utf-8")
        ch_timeout = int(ch.get("timeout") or timeout)
        for attempt in (1, 2):   # 每通道单次重试
            req = urllib.request.Request(ch["url"], data=body, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ch['key']}",
            })
            try:
                # 强制直连：绕过 macOS 系统代理（否则 urllib 走 127.0.0.1:7897 等代理端口，
                # 代理未运行时全部 Connection refused。这些 API 端点本就该直连。）
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=ch_timeout) as resp:
                    result = json.loads(resp.read())
                    content = result["choices"][0]["message"].get("content", "")
                    # deepseek-v4-flash 是推理模型：max_tokens 可能全被 reasoning_content 吃掉，
                    # 导致 content 为空串。空响应视为失败，不当作“无事实”结果。
                    if not content or not content.strip():
                        raise ValueError("empty content (reasoning consumed max_tokens)")
                    if not ch.get("local") or attempt > 1:
                        print(f"[memory_capture] LLM 通道成功: {ch['name']}（第 {attempt} 次尝试）", file=sys.stderr)
                    return content
            except urllib.error.HTTPError as e:
                last_err = f"{ch['name']} HTTP {e.code}"
                # 402/401/403 属账户级错误，重试无意义，直接切下一通道
                if e.code in (401, 402, 403):
                    print(f"[memory_capture] {ch['name']} 不可用: HTTP {e.code}，切下一通道", file=sys.stderr)
                    break
                print(f"[memory_capture] {ch['name']} HTTP {e.code}（第 {attempt} 次）"
                      + ("，重试" if attempt == 1 else "，切下一通道"), file=sys.stderr)
            except Exception as e:
                last_err = f"{ch['name']} {e}"
                print(f"[memory_capture] {ch['name']} 调用失败: {e}（第 {attempt} 次）"
                      + ("，重试" if attempt == 1 else "，切下一通道"), file=sys.stderr)
    print(f"[memory_capture] 所有 LLM 通道均失败: {last_err}", file=sys.stderr)
    return None


def extract_facts_via_llm(conversation: str) -> list[str]:
    import urllib.request
    import urllib.error

    if len(conversation) < 200:
        return []

    prompt = EXTRACT_PROMPT.format(conversation=conversation[:6000])
    # 2026-09-11 总复核：max_tokens 2000→4000（推理模型 reasoning 占额，2000 实测被吃空致全链失败）
    text = _llm_chat(prompt, max_tokens=4000, temperature=0.3, timeout=90)
    if text is None:
        return None

    # 解析提取结果（v1.2 P0：支持 【ET:YYYY-MM-DD】 时间标记）
    facts = []
    import re as _re
    et_pattern = _re.compile(r"【ET:([0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?)】")
    for line in text.strip().split("\n"):
        line = line.strip()
        if line and line != "无" and len(line) > 5:
            # 去掉可能的编号
            line = line.lstrip("-*•1234567890. )").strip()
            if not line:
                continue
            # 提取时间标记并剥离
            m = et_pattern.search(line)
            event_time = m.group(1) if m else None
            line = et_pattern.sub("", line).strip()
            if not line:
                continue
            facts.append({"text": line, "event_time": event_time})
    return facts


# Backward-compatible name for older callers; extraction itself no longer uses OpenClaw.
extract_via_openclaw = extract_facts_via_llm


# 2026-11-11 审计：凭据闸门。回填时 LLM 把宝塔面板密码当事实写进了记忆（事后三层清除）。
# 教训：不能指望 LLM 自觉——在事实写入前做确定性脱敏，保留事实骨架、只抹凭据值。
CRED_VALUE = re.compile(
    r"((?:密码|口令|password|passwd|pwd)\s*(?:为|是|[:：=])\s*)([^\s，。;,]{4,})", re.IGNORECASE)
CRED_SK = re.compile(r"\bsk-[A-Za-z0-9_\-\.]{16,}")
CRED_KV = re.compile(
    r"((?:api[_-]?key|access[_-]?token|secret)\s*[:：=]\s*['\"]?)([A-Za-z0-9_\-\.]{12,})", re.IGNORECASE)


def redact_credentials(text: str) -> tuple[str, bool]:
    """返回 (脱敏后文本, 是否发生脱敏)。只抹凭据值，保留前缀便于追溯。"""
    redacted = False

    def _keep_prefix(m):
        nonlocal redacted
        redacted = True
        return m.group(1) + "[已脱敏]"

    text = CRED_VALUE.sub(_keep_prefix, text)
    text = CRED_KV.sub(_keep_prefix, text)
    if CRED_SK.search(text):
        redacted = True
        text = CRED_SK.sub("[已脱敏]", text)
    return text, redacted


def get_bailian_api_key() -> str:
    """兼容旧调用方；百炼已退役，默认无 key。"""
    providers = _providers_config().get("providers", {})
    return providers.get("bailian", {}).get("apiKey", "")


# P3：确定性事件时间推断（不依赖 LLM 打标）
#
# 背景：P0「双时钟」的 event_time 在 L2 里 0% 落库。链路本身是通的
# （提示词【ET:YYYY-MM-DD】→ 解析 → remember.py --event-time → PlugMem 持久化），
# 但提取提示词规定「只在这条事实明确指向过去/特定时间时才加标记」，
# 而自动捕获的事实基本是「当下发生」→ LLM 从不输出标记 → event_time 恒为 null。
#
# 这里补一个**确定性**兜底：事实文本里若出现明确日期，就取它当事件时间。
# 只认明确日期（不做「上周/前几天」这类相对推断，避免猜错）。
ISO_DATE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?")
YM_DATE = re.compile(r"(20\d{2})[-/年](\d{1,2})月?")
CJK_MD = re.compile(r"(?<!\d)(\d{1,2})月(\d{1,2})日")


def infer_event_time(text: str, ref_dt=None):
    """从事实文本里推断事件时间，返回 (YYYY-MM-DD 或 YYYY-MM，精度) 或 (None, None)。

    只做确定性提取；相对时间（"上周""前几天"）不猜。
    """
    if not text:
        return None, None
    m = ISO_DATE.search(text)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y}-{mo:02d}-{d:02d}", "day"
    m = CJK_MD.search(text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            y = ref_dt.year if ref_dt else None
            if y:
                return f"{y}-{mo:02d}-{d:02d}", "day"
    m = YM_DATE.search(text)
    if m:
        y, mo = m.group(1), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y}-{mo:02d}", "month"
    return None, None


def file_hash(filepath: Path) -> str:
    """计算文件的快速哈希（mtime+size）"""
    stat = filepath.stat()
    return hashlib.md5(f"{stat.st_mtime}:{stat.st_size}".encode()).hexdigest()


def capture(hours: float = 1, dry_run: bool = False, force: bool = False, max_sessions: int = 3,
            continue_on_error: bool = False, since: Optional[datetime] = None,
            until: Optional[datetime] = None, sources: str = "pi"):
    """主捕获流程

    v1.1 修复：
      - 加 max_sessions 上限（默认 3），防止串行 LLM 提取超时
      - dry-run 输出处理计数，但不持久化 processed 状态
    v1.2 P3：
      - Triage Gate：会话过门控（正则+LLM）才提取，meta-noise 直接拦截并留痕
      - gate_stats 记入 state（total/gated/passed）
    v1.5 定点回溯：
      - since/until（tz-aware UTC）非空时走 get_sessions_in_window（按消息时间戳筛选），
        否则走 get_recent_sessions（mtime，原行为）。缺省完全不变。
    """
    if sources not in VALID_SOURCES:
        raise ValueError(f"未知数据源 {sources!r}，可选 {VALID_SOURCES}")
    state = load_state()
    window_mode = since is not None or until is not None
    if window_mode:
        print(f"[memory_capture] 定点回溯模式：按消息时间戳筛选 "
              f"[{since.isoformat() if since else '-∞'}, {until.isoformat() if until else '+∞'}]")
        sessions = get_sessions_in_window(since, until, sources=sources)
    else:
        sessions = get_recent_sessions(hours=hours, sources=sources)

    if not sessions:
        if window_mode:
            print("[memory_capture] 时间窗内无活跃 session")
        else:
            print(f"[memory_capture] 最近 {hours}h 无活跃 session")
        return
    
    total_facts = 0
    processed = 0
    gated = 0
    consec_fail = 0   # 2026-09-11 总复核：连续提取失败计数（补跑续跑判定真断供用）
    
    for f in sessions:
        if processed >= max_sessions:
            print(f"[memory_capture] 已达单次上限 {max_sessions} 个 session，剩余留待下次")
            break
        
        fh = f.get("hash") or file_hash(Path(f["path"]))
        stem = f["stem"]
        if stem in state["processed_sessions"] and state["processed_sessions"][stem] == fh and not force:
            continue  # 已处理，跳过
        
        conversation = extract_conversation(f, since, until)
        if len(conversation) < 200:
            continue
        
        print(f"[memory_capture] 分析 {stem} ({len(conversation)} 字, {f['kind']})")
        
        # v1.2 P3：Triage Gate
        passed, reason = triage_gate(conversation)
        if not passed:
            print(f"  ⛔ GATE: {reason}")
            gate_log(conversation, reason, stem)
            state["gate_stats"]["gated"] = state["gate_stats"].get("gated", 0) + 1
            state["processed_sessions"][stem] = fh  # 已 gate 的也标记，避免每 30min 重复扫同一 meta-noise
            gated += 1
            continue
        state["gate_stats"]["passed"] = state["gate_stats"].get("passed", 0) + 1
        state["gate_stats"]["total"] = state["gate_stats"].get("total", 0) + 1
        
        if dry_run:
            print(f"  DRY RUN: 将提取事实但不写入")
            state["processed_sessions"][stem] = fh
            processed += 1
            continue
        
        # 提取事实
        facts = extract_facts_via_llm(conversation)
        
        if facts is None:
            # LLM 调用失败（如限流/推理模型吃空 tokens）：不标记 processed，下次自动重试。
            # 默认（增量 30m job）：停止本轮扫描避免连环触发限流（原行为，fail-fast）。
            # 补跑模式（--continue-on-error）：跳过该 session 续跑下一个，但连续失败 ≥5 次
            # 判定为真断供（LLM 全通道死），停止以免空转浪费配额。
            consec_fail += 1
            if continue_on_error and consec_fail < 5:
                print(f"  ⏭️ 提取失败（第 {consec_fail} 次连续），跳过标记、续跑下一个 session")
                continue
            print(f"  ⏭️ 提取失败，跳过标记，本轮停止"
                  + (f"（连续 {consec_fail} 次失败，判定真断供）" if continue_on_error else ""))
            break
        
        for fact in facts:
            text = fact["text"] if isinstance(fact, dict) else fact
            # 凭据闸门：密码/key/token 不入记忆（2026-11-11 审计）
            text, was_redacted = redact_credentials(text)
            if was_redacted:
                print("  🔒 凭据已脱敏")
            et = fact.get("event_time") if isinstance(fact, dict) else None
            et_prec = "day" if et else None
            if not et:
                # P3：LLM 没打标时的确定性兜底
                # 传参考时间：CJK「9月21日」这类需要年份，用捕获时刻补
                et, et_prec = infer_event_time(text, ref_dt=now())
            print(f"  📝 {text}" + (f" [ET:{et}]" if et else ""))
            try:
                cmd = ["python3", str(REMEMBER_SCRIPT), text, "--tags", "自动捕获", "--confidence", "0.6"]
                if et:
                    cmd += ["--event-time", et]
                    if et_prec:
                        cmd += ["--event-time-precision", et_prec]
                subprocess.run(cmd, timeout=30)
                total_facts += 1
            except Exception as e:
                print(f"  ⚠️ 写入失败: {e}", file=sys.stderr)

        consec_fail = 0   # 提取成功→重置连续失败计数
        state["processed_sessions"][stem] = fh
        processed += 1
    
    # dry-run 只预览，不落 processed/gate 状态；否则正式回填会误跳过已预览会话。
    if not dry_run:
        save_state(state)
    print(f"\n[memory_capture] 完成: {processed} 个 session, {total_facts} 条事实, {gated} 个被 gate")


def audit_skipped(hours: float = 168):
    """审计最近 gate 记录（P3 验收项：每周抽样审计 skipped 列表）"""
    print(f"=== memory_capture GATE 审计（最近 {hours}h）===")
    print(f"日志位置: {GATE_LOG_DIR}/YYYY-MM-DD.md 中的 'memory_capture GATE' 条目\n")
    # 读 state 统计
    state = load_state()
    gs = state.get("gate_stats", {})
    print(f"gate_stats: total={gs.get('total',0)} gated={gs.get('gated',0)} passed={gs.get('passed',0)}")
    print(f"gate 率: {gs.get('gated',0)/max(gs.get('total',0),1)*100:.0f}%")


if __name__ == "__main__":
    main()
