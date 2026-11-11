#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_heartbeat_sentinel.py — 记忆链心跳哨兵（2026-09-11 总复核 P7 落地）

为什么要有它：本机记忆链 09-01~09-11 断供整整 10 天无人察觉（所有 cron delivery=none、
failureAlert=false，坏了不吭声）。这是「静默失败」的直接根治——一个独立、零模型依赖的哨兵，
定时体检记忆链，一旦 stale 就飞书告警到人。

双信号（任一 stale 即告警）：
  ① 调度活性 SCHED：memory_capture_30m.log 的 mtime。launchd ai.boss.memory-capture-30m
     每 30min 直跑该脚本、必写 stdout → mtime 应 <90min。超龄=job 没跑（被 bootout / launchd 坏）。
  ② 管道产能 FACT：workspace-main/memory/unified-facts.jsonl 最后一条 timestamp。调度在跑但
     扫不到源 / LLM 全通道死 / embedding 死时，log 照写「完成: 0 事实」却没有事实落库——
     这正是本次事故的死法（jsonl 源枯竭 + modelPolicy 拦截）。>26h 无新事实=产能断。

告警去重：state 记 {reason: lastAlertEpoch}。同一 stale 原因 6h 内不重发；持续 stale 每 24h
再提醒一次。恢复后自动清该 reason 的 state（下次 stale 重新计）。

铁律：不调任何模型；只读记忆文件、只写自己的 state；不碰记忆/台账数据；不抢浏览器。
飞书通道由 coremem 自有 notify.py 提供；不复用会屏蔽基础设施标题的招聘通知脚本。

2026-09-11 通道修复追加（.bak.20260911-chan）：
  ③ LLM 本地服务体检：omlx（环境变量/llm-policy/coremem providers 配置，缺省 127.0.0.1:9999）+
     PlugMem（127.0.0.1:8089/api/v1/health）。任一挂 → 复用同一飞书通道告警（独立 reason 去重）。
     只 GET /v1/models 与 /health，不发起任何模型推理，不违反「不调模型」铁律。

用法：
  python3 memory_heartbeat_sentinel.py            # 正常巡检（launchd 调）
  python3 memory_heartbeat_sentinel.py --test     # 强制发一条自检告警（验证通道），不改 state
  python3 memory_heartbeat_sentinel.py --status   # 只打印当前体检结果，不告警不写 state
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import notify

HOME = Path.home()
MEM_WS = HOME / ".coremem/logs"
MEM_MAIN = HOME / ".coremem/memory"
CAPTURE_LOG = MEM_WS / "memory_capture_30m.log"          # 调度活性信号源
FACTS = MEM_MAIN / "unified-facts.jsonl"                  # 管道产能信号源（remember.py 实际落库处）
PI_SESSIONS = HOME / ".pi" / "agent" / "sessions"          # 输入信号源（capture 实际扫描处）
STATE_F = MEM_WS / "heartbeat-sentinel-state.json"

SCHED_STALE_MIN = 90          # 30min 调度，留 3 倍余量
FACT_STALE_H = 26             # 报告口径：>26h 无 capture 即告警
REALERT_H = 6                 # 同一原因 6h 内不重发
REMIND_H = 24                 # 持续 stale 每 24h 再提醒
PLUGMEM_HEALTH = "http://127.0.0.1:8089/api/v1/health"   # PlugMem 服务端点
SVC_PROBE_TIMEOUT = 5         # 本地端点探活超时（秒）


def _providers_map() -> dict:
    """与 memory_capture 同源：读 coremem 自有 providers 配置（env > ~/.coremem/config/providers.json）。"""
    path = Path(os.environ.get("MEM_PROVIDERS_CONFIG", str(HOME / ".coremem/config/providers.json")))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return (raw.get("models", {}).get("providers", {}) or raw.get("providers", {}) or {})


def omlx_disabled() -> bool:
    """本机没有（或不想要）本地 omlx：capture 里也是这两条开关。
    没关时哨兵会每轮报一条假告警「omlx_down」——机器本来就没装 omlx，不是故障。"""
    if str(os.environ.get("MEM_OMLX_DISABLED", "")).lower() in ("1", "true", "yes"):
        return True
    return _providers_map().get("omlx", {}).get("enabled") is False


def omlx_base_url() -> str:
    """Resolve the same local endpoint as capture, without OpenClaw config."""
    env_base = os.environ.get("MEM_OMLX_BASE_URL")
    if env_base:
        return env_base.rstrip("/")
    try:
        policy = json.loads((HOME / ".boss-agent/llm-policy.json").read_text())
        base = policy.get("local_base") or policy.get("omlx_base")
        if base:
            return base.rstrip("/")
    except Exception:
        pass
    path = Path(os.environ.get("MEM_PROVIDERS_CONFIG", str(HOME / ".coremem/config/providers.json")))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        providers = raw.get("models", {}).get("providers", {}) or raw.get("providers", {}) or raw
        base = providers.get("omlx", {}).get("baseUrl", "")
        if base:
            return base.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:9999/v1"


def _probe(url: str) -> tuple[bool, str]:
    """GET 探活（直连绕系统代理）。返回 (ok, detail)。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request(url), timeout=SVC_PROBE_TIMEOUT) as resp:
            body = resp.read(500).decode("utf-8", "replace")
            if resp.status == 200:
                return True, body[:120]
            return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)


def log(msg):
    line = f"{time.strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with (MEM_WS / "heartbeat-sentinel.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        return json.loads(STATE_F.read_text())
    except Exception:
        return {"alerts": {}, "lastOk": None}


def save_state(st):
    try:
        tmp = STATE_F.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        tmp.replace(STATE_F)
    except Exception as e:
        log(f"⚠️ state 写入失败: {e!r}")


def sched_age_min():
    """① 调度活性：capture 30m log mtime 距今分钟数。文件不存在→None（视为 stale）。"""
    try:
        return (time.time() - CAPTURE_LOG.stat().st_mtime) / 60.0
    except OSError:
        return None


def fact_age_h():
    """② 管道产能：unified-facts 最后一条 timestamp 距今小时数。读不到→None（视为 stale）。"""
    try:
        last = None
        with FACTS.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return None
        ts = json.loads(last).get("timestamp")
        if not ts:
            return None
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return None


def active_session_count(since_epoch: float) -> int:
    """自某时刻之后被修改过的会话文件数（= 那之后有没有新的输入）。

    2026-11-06 加：用来区分「没有输入」和「管道坏了」。
    原先只凭 unified-facts 的 mtime 判红，于是 56 小时没人跟 agent 说话
    也会报「产能断供：LLM 全通道死/embedding 死」—— 三个假设全是故障，
    唯独漏掉最可能的一种：没人提问，本就没有事实可提取。

    判据用「最后一条事实的时间」而不是「最近 N 小时」：
    后者会被"刚刚才开始的新会话"误判成有输入（本次就踩到了）。
    """
    cutoff = since_epoch
    n = 0
    try:
        for f in PI_SESSIONS.glob("*/*.jsonl"):
            try:
                if f.stat().st_mtime >= cutoff:
                    n += 1
            except OSError:
                continue
    except OSError:
        return -1          # 扫不了 ≠ 没有输入
    return n


def fact_last_ts():
    """最后一条事实的时间戳（tz-aware）。读不到→None。"""
    try:
        last = None
        with FACTS.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = line
        if not last:
            return None
        ts = json.loads(last).get("timestamp")
        if not ts:
            return None
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def capture_channel_names():
    """问 memory_capture 要当前通道链；导入失败返回 None（未知，不当成空链告警）。"""
    try:
        import memory_capture as mc
        return [c["name"] for c in mc._build_llm_channels(30)]
    except Exception:
        return None


def check():
    """返回 [(reason, detail)] 需要告警的 stale 项；空列表=健康。"""
    problems = []
    idle_note = None
    sa = sched_age_min()
    fa = fact_age_h()
    if sa is None:
        problems.append(("sched_missing", f"capture 日志不存在（{CAPTURE_LOG}）— launchd job 从未跑/被移除"))
    elif sa > SCHED_STALE_MIN:
        problems.append(("sched_stale", f"调度停摆：memory_capture_30m.log 已 {sa/60:.1f}h 未更新（阈值 {SCHED_STALE_MIN}min）— launchd ai.boss.memory-capture-30m 没在跑"))
    if fa is None:
        problems.append(("fact_missing", f"unified-facts 读不到（{FACTS}）— 事实池丢失/空"))
    elif fa > FACT_STALE_H:
        # 关键区分：这段时间有没有输入？
        lts = fact_last_ts()
        since = lts.timestamp() if lts else (time.time() - FACT_STALE_H * 3600)
        nsess = active_session_count(since)
        if nsess == 0:
            # 最后一条事实之后没有任何新会话 = 没有可提取的内容。不是故障。
            idle_note = (f"无输入：最后一条事实（{fa:.1f}h 前）之后没有任何会话活动，"
                         f"产能停滞属正常，非故障")
        elif nsess < 0:
            problems.append(("fact_stale", f"产能断供：最后一条事实 {fa:.1f}h 前，且无法扫描会话目录（{PI_SESSIONS}）"))
        else:
            problems.append(("fact_stale",
                             f"产能断供：最后一条事实 {fa:.1f}h 前（阈值 {FACT_STALE_H}h），"
                             f"但此后有 {nsess} 个会话活动却没产出事实 —— 是真断供，查 capture 提取链路/LLM 通道"))
    # ③ LLM 服务体检（2026-09-11 通道修复；2026-11-11 跟随通道配置化）
    #    本地 omlx 被显式关闭的机器不探它（否则每轮一条假 omlx_down），改为直接看
    #    capture 能不能建出通道——链空才是真故障（提取会全链失败、事实永不落地）。
    if omlx_disabled():
        names = capture_channel_names()
        if names is not None and not names:
            problems.append(("no_llm_channel",
                             "capture 通道链为空：providers.json 里没有任何带 key 的可用通道，"
                             "提取将全链失败（事实留待下轮重试，但永远不会长记忆）"))
    else:
        omlx_base = omlx_base_url()
        ok, detail = _probe(omlx_base + "/models")
        if not ok:
            problems.append(("omlx_down", f"本地 LLM omlx 挂：{omlx_base}/models 探活失败（{detail}）— memory_capture 主力通道不可用；无云端备胎时提取会失败并留待下轮"))
    ok, detail = _probe(PLUGMEM_HEALTH)
    if not ok:
        problems.append(("plugmem_down", f"PlugMem 挂：{PLUGMEM_HEALTH} 探活失败（{detail}）— 事实写入将落 buffer，召回不可用"))
    elif '"status": "ok"' not in detail and '"status":"ok"' not in detail:
        problems.append(("plugmem_degraded", f"PlugMem 降级：health 返回非 ok（{detail}）"))
    return problems, sa, fa, idle_note


def should_alert(st, reason):
    """去重：同一 reason 6h 内不重发；持续 stale 每 24h 提醒一次。"""
    now = time.time()
    last = (st.get("alerts") or {}).get(reason)
    if last is None:
        return True
    age_h = (now - last) / 3600.0
    return age_h >= REALERT_H   # 6h 后可再发（持续 stale 会周期性提醒）


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--test":
        ok = notify.send("🫀 记忆链心跳哨兵自检：通道连通正常。\n（这是 --test 强制发送，非真实告警）", title="记忆哨兵自检")
        log(f"--test 飞书自检 {'✅ 送达' if ok else '🔴 失败'}")
        return 0 if ok else 1

    problems, sa, fa, idle_note = check()
    sched_s = "n/a" if sa is None else f"{sa:.0f}min"
    fact_s = "n/a" if fa is None else f"{fa:.1f}h"
    svc_bad = {r for r, _ in problems if r in ("omlx_down", "plugmem_down", "plugmem_degraded")}
    svc_s = "omlx✅/plugmem✅" if not svc_bad else "/".join(sorted(svc_bad))

    if mode == "--status":
        log(f"--status 调度活性={sched_s}（阈 {SCHED_STALE_MIN}min）｜产能={fact_s}（阈 {FACT_STALE_H}h）｜服务={svc_s}｜问题={len(problems)}")
        for r, d in problems:
            log(f"   ⚠️ {r}: {d}")
        return 0

    st = load_state()
    if not problems:
        # 健康：清 alerts（下次 stale 重新计），记 lastOk
        if st.get("alerts"):
            log(f"💚 记忆链恢复健康（调度 {sched_s}｜产能 {fact_s}），清历史告警 state")
        st["alerts"] = {}
        st["lastOk"] = datetime.now(timezone.utc).isoformat()
        save_state(st)
        return 0

    # 有 stale：挑需要发的（去重后），合成一条飞书告警
    to_send = [(r, d) for r, d in problems if should_alert(st, r)]
    log(f"🫀 体检：调度活性={sched_s}｜产能={fact_s}｜服务={svc_s}｜问题 {len(problems)} 个，本轮需告警 {len(to_send)} 个")
    if not to_send:
        log("   （均在去重窗口内，本轮不重发）")
        return 0

    body = "🫀 **记忆链心跳哨兵告警**\n" + "\n".join(f"• [{r}] {d}" for r, d in to_send)
    body += f"\n\n体检快照：调度活性 {sched_s}（阈 {SCHED_STALE_MIN}min）｜事实产能 {fact_s}（阈 {FACT_STALE_H}h）"
    body += "\n排查：launchctl list | grep memory-capture｜tail memory_capture_30m.log｜PlugMem :8089/api/v1/health"
    try:
        ok = notify.send(body, title="🔴 记忆链断供告警")
    except Exception as e:
        log(f"🔴 飞书告警发送异常: {e!r}")
        ok = False
    if ok:
        now = time.time()
        for r, _ in to_send:
            st.setdefault("alerts", {})[r] = now
        save_state(st)
        log("   飞书告警 ✅ 已推送")
    else:
        # 失败不得写去重时间：下一轮巡检应立即重试，而不是静默 6 小时。
        log("   飞书告警 🔴 推送失败（未记去重时间，下轮立即重试）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
