#!/usr/bin/env bash
# review-preflight.sh — 独立复核前的探活与降级
#
# 为什么需要：2026-11-15 审计时两次长 prompt 复核都被服务端拒
# （403 预扣费额度失败：剩 $4.76 需 $5.01；换 sonnet 后剩 $2.15 需 $2.43）。
# 我是跑完长任务拿到一行错误才发现的，白烧两轮。更糟的是：若不看那行错误，
# 很容易产出一份"看似完整"实则没有独立复核的报告。
#
# 做法：用极小 prompt 逐个候选探活（成本远低于长 prompt），选第一个可用的。
# 全不可用则退出码 2，逼调用方显式声明"未做独立复核"，而不是静默跳过。
#
# 判据用**正向断言**：必须真回出「可用」。
# 只过滤已知错误词不够——实测未知模型时 CLI 回 "Claude Code doesn't recognize..."，
# 不含认证/额度关键词，第一版就是这样把它误判为可用的。
#
# 用法：
#   MODEL=$(bash review-preflight.sh) || echo "独立复核未完成：无可用模型"
#   claude -p "$(cat BRIEF.md)" --model "$MODEL" ...
# 退出码：0=找到可用模型（stdout 打印模型名）/ 2=全部不可用
set -uo pipefail
CANDIDATES="${REVIEW_MODELS:-claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5}"
PROBE_TIMEOUT="${REVIEW_PROBE_TIMEOUT:-120}"
PF_LOG="${REVIEW_PREFLIGHT_LOG:-$HOME/.coremem/logs/review-preflight.log}"
mkdir -p "$(dirname "$PF_LOG")" 2>/dev/null || true

log() { echo "[$(date '+%F %T')] $*" >>"$PF_LOG"; }

if ! command -v claude >/dev/null 2>&1; then
    log "claude CLI 不存在"
    echo "claude CLI 不可用" >&2
    exit 2
fi

for m in $CANDIDATES; do
    out=$(timeout "$PROBE_TIMEOUT" claude -p "只回复两个字：可用" --model "$m" 2>&1 | head -5)
    if echo "$out" | grep -qiE "Failed to authenticate|API Error|预扣费|额度|403|401|rate.?limit"; then
        log "$m 不可用（认证/额度）: $(echo "$out" | head -1 | cut -c1-70)"
        continue
    fi
    if ! echo "$out" | grep -q "可用"; then
        log "$m 未回出「可用」: $(echo "$out" | head -1 | cut -c1-70)"
        continue
    fi
    log "$m 可用"
    echo "$m"
    exit 0
done

log "全部候选不可用: ${CANDIDATES}"
echo "全部候选模型不可用（详见 ${PF_LOG}）；必须显式声明未做独立复核" >&2
exit 2
