#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""config-drift-check.py — 配置漂移与静默停摆检查（防复发核心工具）

诞生原因（两次同类事故）：
  · 2026-09-16 登录桥 run.sh 改 agentId 未重启 → 文件/运行不一致，悬空 26 天
  · 2026-11-12 入站 plist 改后未 bootstrap → 任务从调度器消失，停摆 50 小时
  · 2026-11-14 巡检查旧日志路径 → 假告警（审批台其实正常）

本工具做四件对账（全部只读）：
  ① launchd：plist 存在 vs 是否真加载 vs 真实日志新鲜度（自动从脚本里解析 LOG 路径）
  ② systemd：配置文件里的关键 env vs 进程实际持有的 env
  ③ 监控自身：巡检脚本引用的日志路径 vs 被监控脚本实际写入的路径
  ④ 容器：应跑 vs 实跑（含"人为停止"识别）

用法：
  python3 config-drift-check.py            # 全量
  python3 config-drift-check.py --json     # 机器可读
退出码：0=无漂移；1=发现漂移
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

HOME = os.path.expanduser("~")
UID = str(os.getuid())
LA = os.path.join(HOME, "Library/LaunchAgents")
# 2026-11-15：远端主机名不入库（coremem 是公开仓库）。真值只放活体 plist 的
# EnvironmentVariables.MEM_DRIFT_SSH_HOST；未配置时远端检查整段跳过，不报错。
SSH_HOST = os.environ.get("MEM_DRIFT_SSH_HOST", "").strip()
SSH_KEY = os.environ.get("MEM_DRIFT_SSH_KEY", os.path.join(HOME, ".ssh/id_ed25519"))
SSH = (["/usr/bin/ssh", "-i", SSH_KEY,
        "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", SSH_HOST] if SSH_HOST else [])

# ── 必须常驻的关键任务（未加载 = 真漂移）──
MUST_LOAD = {
    "ai.boss.inbound-2h",            # 入站车道（11-12 停摆 50h 的那个）
    "ai.boss.approve-doc",           # 审批台轮询
    "ai.boss.sender-watchdog",       # 发送看门狗
    "com.lealway.omlx-server",       # 本地模型（知识库向量化依赖）
    "com.lealway.weknora-tunnel",    # 知识库隧道
    "com.lealway.wecom-identity-health",  # 身份巡检
}
# ── 明确忽略：更新器/一次性/实验性任务，未加载属正常 ──
IGNORE_PAT = re.compile(
    r"(updater|keystone|GoogleUpdater|EgoUpdater|citrolabs|"
    r"oneshot|harvest|drain|refill|exchange-execute|halfday-clear|"
    r"recommend-greet|queue-refresh|loginwatch|session-keepalive|"
    r"sender-health|pplx-decider|jev-keepwarm|roon-gate|mem0-daemon|"
    r"clash|闪电说)", re.I)
BASELINE = os.path.join(HOME, ".coremem/data/config-drift-baseline.json")

findings = []


def rec(kind, name, status, detail):
    findings.append({"kind": kind, "name": name, "status": status, "detail": detail})
    tag = {"OK": "✅", "DRIFT": "🔴", "WARN": "⚠️", "INFO": "ℹ️"}.get(status, "?")
    print(f"{tag} [{kind}] {name}: {detail}")


def sh(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 999, str(e)


def plist_get(path, key):
    """粗解析 plist 的 string 值（避免依赖 plistlib 对畸形文件的严格性）"""
    try:
        import plistlib
        with open(path, "rb") as f:
            d = plistlib.load(f)
        return d.get(key)
    except Exception:
        return None


def script_log_paths(plist_path):
    """从 plist 的 ProgramArguments 找到脚本，再解析脚本里的 LOG= 定义
    —— 这是防"巡检查错路径"的关键：以脚本实际写入为准，而非 launchd 的 .out"""
    args = plist_get(plist_path, "ProgramArguments") or []
    joined = " ".join(str(a) for a in args)
    paths = []
    # launchd 自身的 stdout/stderr
    for k in ("StandardOutPath", "StandardErrorPath"):
        v = plist_get(plist_path, k)
        if v:
            paths.append(("launchd:" + k, os.path.expanduser(str(v))))
    # 脚本内部 LOG
    m = re.search(r'([~/][^\s"\']*\.sh)', joined)
    if m:
        sp = os.path.expanduser(m.group(1))
        if not os.path.isabs(sp):
            sp = os.path.join(HOME, sp)
        if os.path.isfile(sp):
            try:
                txt = open(sp, encoding="utf-8", errors="replace").read()
                for lm in re.finditer(r'^\s*(?:LOG|LOGFILE|LOG_FILE)\s*=\s*"?([^"\n]+)"?', txt, re.M):
                    raw = lm.group(1).strip()
                    raw = raw.replace("$HOME", HOME).replace("${HOME}", HOME)
                    raw = raw.replace("$D", os.path.join(HOME, ".boss-agent/data"))
                    raw = raw.replace("${D}", os.path.join(HOME, ".boss-agent/data"))
                    if "$" not in raw:
                        paths.append(("script:LOG", raw))
            except Exception:
                pass
    return paths


def _load_baseline():
    try:
        return json.load(open(BASELINE, encoding="utf-8"))
    except Exception:
        return {}


def _save_baseline(states):
    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    tmp = BASELINE + ".tmp"
    json.dump(states, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, BASELINE)


def check_launchd():
    """① plist 存在 vs 真加载 vs 真实日志新鲜度"""
    if not os.path.isdir(LA):
        return
    states = {}
    for fn in sorted(os.listdir(LA)):
        if not fn.endswith(".plist"):
            continue
        label = fn[:-6]
        path = os.path.join(LA, fn)
        rc, _ = sh(["launchctl", "print", f"gui/{UID}/{label}"], timeout=15)
        loaded = (rc == 0)
        states[label] = loaded
        if not loaded:
            if label in MUST_LOAD:
                rec("launchd", label, "DRIFT",
                    "【关键任务】plist 存在但未加载 → 不会被调度（11-12 入站停摆即此类）")
            elif IGNORE_PAT.search(label):
                pass                      # 更新器/一次性任务，未加载属正常，不报
            else:
                base = _load_baseline()
                if base.get(label) is True:
                    rec("launchd", label, "DRIFT",
                        "基线中曾加载、现在未加载 → 疑似被误卸载或改配置后未重载")
                else:
                    rec("launchd", label, "INFO", "未加载（基线亦未加载，视为有意停用）")
            continue
        # 真实日志新鲜度：优先脚本内 LOG，其次 launchd .out
        paths = script_log_paths(path)
        freshest, used = None, None
        for kind, p in paths:
            if os.path.isfile(p):
                mt = os.path.getmtime(p)
                if freshest is None or mt > freshest:
                    freshest, used = mt, f"{kind} → {p.replace(HOME, '~')}"
        if freshest is None:
            rec("launchd", label, "INFO", "已加载；未找到可判定的日志文件")
        else:
            age_h = (time.time() - freshest) / 3600
            if age_h < 26:
                rec("launchd", label, "OK", f"已加载；最近日志 {age_h:.1f}h 前（{used}）")
            elif label in MUST_LOAD:
                rec("launchd", label, "WARN",
                    f"【关键任务】已加载但日志 {age_h:.1f}h 未更新 → 疑似空转（{used}）")
            elif not IGNORE_PAT.search(label):
                rec("launchd", label, "WARN", f"已加载；日志 {age_h:.1f}h 未更新（{used}）")


    _save_baseline(states)


def check_systemd_env():
    """② 配置文件 env vs 进程实际 env（09-16 登录桥事故的检测器）"""
    if not SSH:
        rec("remote", "skip", "OK", "未配置 MEM_DRIFT_SSH_HOST，跳过远端检查")
        return
    checks = [
        ("wecom-oidc-bridge", "/opt/qm/wecom-bridge/run.sh", "WECOM_AGENT_ID"),
        ("qm-wecom-callback", "/opt/qm-wecom-mcp/.msg-cb.env", "WECOM_AGENT_ID"),
    ]
    for unit, cfg, var in checks:
        rc, out = sh(SSH + [
            f'PID=$(systemctl show {unit} -p MainPID --value); '
            f'FILE=$(grep -hE "^(export )?{var}=" {cfg} 2>/dev/null | head -1 | sed -E "s/^(export )?{var}=//; s/\\"//g"); '
            f'PROC=$(ps eww $PID 2>/dev/null | tr " " "\\n" | grep -E "^{var}=" | head -1 | cut -d= -f2); '
            f'echo "FILE=$FILE"; echo "PROC=$PROC"; echo "ACTIVE=$(systemctl is-active {unit})"'
        ], timeout=40)
        f = re.search(r"FILE=(\S*)", out)
        p = re.search(r"PROC=(\S*)", out)
        a = re.search(r"ACTIVE=(\S*)", out)
        fv, pv, av = (f.group(1) if f else ""), (p.group(1) if p else ""), (a.group(1) if a else "?")
        if av != "active":
            rec("systemd", unit, "DRIFT", f"服务未运行（{av}）")
        elif not fv or not pv:
            rec("systemd", unit, "WARN", f"取值不全 file={fv!r} proc={pv!r}")
        elif fv != pv:
            rec("systemd", unit, "DRIFT",
                f"{var} 文件={fv} 但进程={pv} → 改了配置没重启（09-16 事故模式）")
        else:
            rec("systemd", unit, "OK", f"{var} 文件==进程={fv}")


def check_containers():
    """④ 应跑 vs 实跑（识别人为停止）"""
    if not SSH:
        rec("remote", "skip", "OK", "未配置 MEM_DRIFT_SSH_HOST，跳过远端检查")
        return
    rc, out = sh(SSH + ['docker ps -a --format "{{.Names}}|{{.Status}}|{{.State}}"'], timeout=40)
    expect_running = ["qm-jicheng-core", "qm-jicheng-pg", "qm-jicheng-auth"]
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, status = parts[0], parts[1]
        if name in expect_running:
            ok = status.startswith("Up")
            rec("container", name, "OK" if ok else "DRIFT", status)
        elif name.startswith("twenty"):
            rec("container", name, "INFO", f"{status}（Twenty 定位见 AI-CRM 升级方案）")


def check_monitor_paths():
    """③ 巡检脚本引用的路径 vs 被监控脚本实际写入的路径"""
    health = os.path.join(HOME, ".coremem/scripts/wecom-identity-health.py")
    if not os.path.isfile(health):
        return
    txt = open(health, encoding="utf-8", errors="replace").read()
    referenced = set(re.findall(r'"(\.[^"]*?\.log)"', txt)) | set(re.findall(r'"(\.[^"]*?\.out)"', txt))
    for rel in sorted(referenced):
        p = os.path.join(HOME, rel)
        if not os.path.isfile(p):
            rec("monitor", rel, "DRIFT", "巡检引用的日志文件不存在 → 会产生假告警")
        else:
            age_h = (time.time() - os.path.getmtime(p)) / 3600
            rec("monitor", rel, "OK" if age_h < 26 else "WARN",
                f"存在；{age_h:.1f}h 前更新")


def main():
    as_json = "--json" in sys.argv
    if as_json:
        sys.stdout = open(os.devnull, "w")
    print("=" * 66)
    print(f"配置漂移与静默停摆检查 · {datetime.now():%F %T}")
    print("=" * 66)
    for fn in (check_launchd, check_systemd_env, check_containers, check_monitor_paths):
        try:
            fn()
        except Exception as e:
            rec("self", fn.__name__, "WARN", f"检查异常：{e}")
    drift = [f for f in findings if f["status"] == "DRIFT"]
    warn = [f for f in findings if f["status"] == "WARN"]
    if as_json:
        sys.stdout = sys.__stdout__
        print(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                          "drift": len(drift), "warn": len(warn), "findings": findings},
                         ensure_ascii=False, indent=1))
    else:
        print(f"\n— 汇总：DRIFT={len(drift)} WARN={len(warn)} INFO/OK={len(findings)-len(drift)-len(warn)}")
        if drift:
            print("\n🔴 需处置的漂移：")
            for d in drift:
                print(f"   · [{d['kind']}] {d['name']}: {d['detail']}")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
