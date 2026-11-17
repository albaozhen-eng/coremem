/**
 * 记忆系统「未决问题」注入器（2026-11-17 重写）
 *
 * 变更背景：原实现每会话注入「⚠️ 记忆心跳 yellow：写入保真=99%」这类颜色+百分比。
 * 四份独立模型复核一致指出那是错误架构：
 *   - 颜色/百分比说不出具体哪条数据有问题，诱导维护灯而不是维护数据；
 *   - 假黄注入模型上下文，会让助手去修监控而不是做事；
 *   - 主动推送 IM 通道已删除（单用户无值班，500 条假红被忽略 10.4 天）。
 *
 * 新契约（fail-closed，绝不「静默即健康」）：
 *   1 读 .reconcile-stamp.json：缺失 / 超过 MAX_AGE_H / 损坏 → 注入「检查失活」，
 *     不得因为读不到就默认健康（检查器崩溃时不写戳，见 memory_health.py）。
 *   2 未决问题只报**具体 ref**（来自 probe_unresolved 扫全量账本，不用滚动窗口）。
 *   3 无未决问题且戳新鲜 → **什么都不注入**。
 *
 * 纯只读，不改任何记忆数据。
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const DIR = path.join(os.homedir(), ".coremem", "health");
const FILE = path.join(DIR, "memory-health.json");
const STAMP = path.join(DIR, ".reconcile-stamp.json");
const MAX_AGE_H = 26; // 每日对账 + 2h 余量；超过即失活
const MAX_REFS = 6;

let injected = false;

type Check = {
  id: string;
  label: string;
  level: "green" | "yellow" | "red";
  value: number | null;
  unit?: string;
  detail?: string;
};

/** 从 detail 里抽出具体 ref（形如 2026-11-16.md:637）。 */
function extractRefs(detail: string): string[] {
  return Array.from(new Set(detail.match(/\d{4}-\d{2}-\d{2}\.md:\d+/g) ?? []));
}

/** 戳龄（小时）；缺失/损坏返回 null —— 调用方必须当作失活，不是健康。 */
function stampAgeHours(): number | null {
  try {
    const st = JSON.parse(fs.readFileSync(STAMP, "utf8"));
    const ts = Date.parse(st?.completed_at ?? "");
    if (!Number.isFinite(ts)) return null;
    return (Date.now() - ts) / 3600000;
  } catch {
    return null;
  }
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", () => {
    injected = false;
  });

  pi.on("before_agent_start", (event) => {
    if (injected || event.messageType !== "user") return;
    injected = true;

    const age = stampAgeHours();
    const stale = age === null || age > MAX_AGE_H;

    let health: { checks?: Check[] } = {};
    try {
      health = JSON.parse(fs.readFileSync(FILE, "utf8"));
    } catch {
      /* 读不到就按失活处理，下面统一报 */
    }
    const checks = health.checks ?? [];
    const unres = checks.find((c) => c.id === "unresolved");
    const detail = String(unres?.detail ?? "");
    const refs = extractRefs(detail);
    const dataLoss = /事实源核验失败（真丢失）/.test(detail);

    // 探针自身崩溃也算未决（不能因为探针挂了就说没事）
    const crashed = checks.filter((c) => /探针自身异常/.test(c.label ?? ""));

    const parts: string[] = [];

    if (stale) {
      parts.push(
        age === null
          ? "- 对账成功戳缺失或损坏：**无法证明健康**。请跑 `python3 ~/.coremem/scripts/memory_health.py`（崩溃不会写戳）"
          : `- 对账已 ${age.toFixed(0)}h 未成功完成（阈值 ${MAX_AGE_H}h）：**检查失活**，不代表系统正常`,
      );
    }
    if (dataLoss) {
      parts.push(`- **事实源核验失败（真丢失）**：${refs.slice(0, MAX_REFS).join(", ")}`);
    } else if (refs.length > 0) {
      parts.push(
        `- 索引待补 ${refs.length} 条（L1 已核验，**不是数据丢失**）：${refs.slice(0, MAX_REFS).join(", ")}`,
      );
    }
    if (crashed.length > 0) {
      parts.push(`- 探针自身崩溃 ${crashed.length} 个：${crashed.map((c) => c.id).join(", ")}`);
    }

    if (parts.length === 0) return; // 无未决 + 戳新鲜 → 完全沉默

    return {
      message: {
        customType: "memory-health",
        content:
          `coremem 未决事项：\n${parts.join("\n")}\n` +
          `诊断：\`python3 ~/.coremem/scripts/memory_health.py\`（按需，不轮询）。\n` +
          `处理约定：一句话告知用户，然后继续当前任务；不要去调监控阈值或让灯变绿。`,
        display: true,
      },
    };
  });
}
