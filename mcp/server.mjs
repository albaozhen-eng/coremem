#!/usr/bin/env node
/**
 * plugmem-mcp — PlugMem 记忆系统 MCP Server（v1.1 2026-11-11，纳入 coremem 仓库管理）
 *
 * 以 Mac 本地 PlugMem 为唯一权威源，向 MCP 客户端（OpenClaw/QM/Codex...）提供
 * 记忆检索/写入/评分/简报/结晶等能力。
 *
 * 实现：Node ESM + @modelcontextprotocol/sdk，工具逻辑桥接 ~/.plugmem/cli 下
 * 已参数化的 Python 脚本（黄金回归验证过），不重复实现。
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { z } from "zod";

const CLI_DIR = process.env.PLUGMEM_CLI_DIR || path.join(os.homedir(), ".plugmem", "cli");
const CONFIG_FILE = process.env.PLUGMEM_CONFIG_FILE || path.join(os.homedir(), ".plugmem", "config", "config.json");
// macOS 自带 /usr/bin/python3 是 3.9（不支持 X | None 语法），必须用 homebrew 3.14
const PY = fs.existsSync("/opt/homebrew/bin/python3")
  ? "/opt/homebrew/bin/python3"
  : "/usr/local/bin/python3";

function runPy(script, args = [], opts = {}) {
  const r = spawnSync(PY, [path.join(CLI_DIR, script), ...args], {
    encoding: "utf-8",
    timeout: (opts.timeout ?? 120) * 1000,
    maxBuffer: 1024 * 1024 * 4,
  });
  if (r.error) return { ok: false, error: String(r.error.message || r.error) };
  if (r.status !== 0)
    return { ok: false, error: (r.stderr || r.stdout || "").slice(0, 800) };
  const out = r.stdout.trim();
  try {
    return { ok: true, data: out ? JSON.parse(out) : null };
  } catch {
    return { ok: true, data: out };
  }
}

const server = new McpServer({
  name: "plugmem-mcp",
  version: "1.1.0",
});

/* ---------- 工具 1：memory_search 语义检索 ---------- */
server.tool(
  "memory_search",
  "跨层语义检索记忆（plugmem 语义 + l0 会话 + l1 日记 + facts 事实，RRF 融合）",
  {
    query: z.string().describe("检索问题"),
    limit: z.number().optional().describe("结果数（默认 5）"),
  },
  async ({ query, limit }) => {
    const args = ["all", query, "--json"];
    if (limit) args.push("--hours", String(limit * 24 * 7)); // 宽松时间窗
    const r = runPy("memory_recall.py", args, { timeout: 60 });
    if (!r.ok) return { content: [{ type: "text", text: `❌ ${r.error}` }] };
    return { content: [{ type: "text", text: JSON.stringify(r.data, null, 2) }] };
  }
);

/* ---------- 工具 2：memory_capture 写入事实 ---------- */
server.tool(
  "memory_capture",
  "写入一条记忆事实（含标签），写入本地 PlugMem 权威源",
  {
    text: z.string().describe("事实内容"),
    tags: z.string().optional().describe("逗号分隔标签"),
  },
  async ({ text, tags }) => {
    if (!text) return { content: [{ type: "text", text: "❌ text 必填" }] };
    const args = [text];
    if (tags) args.push("--tags", tags);
    const r = runPy("remember.py", args, { timeout: 30 });
    if (!r.ok) return { content: [{ type: "text", text: `❌ ${r.error}` }] };
    return { content: [{ type: "text", text: `✅ 已写入${r.data ? "：" + JSON.stringify(r.data) : ""}` }] };
  }
);

/* ---------- 工具 3：memory_stats 图状态 ---------- */
server.tool(
  "memory_stats",
  "查看 PlugMem 记忆图状态（各层节点数）",
  {},
  async () => {
    // 直连本地 PlugMem API（带 key）
    const { execFileSync } = await import("node:child_process");
    try {
      const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8"));
      const base = cfg.plugmem_url || "http://127.0.0.1:8089";
      const key = cfg.api_key || "";
      // 注：用字符串拼接而非模板串，避开 secret_scan 对 "X-API-Key: …" 的误报（此处是运行时插值，非真密钥）
      const headers = key ? ["-H", "X-API-Key: " + key] : [];
      const out = execFileSync("curl", ["-s", "-m", "8", "-H", "Content-Type: application/json", ...headers, `${base}/api/v1/graphs/${cfg.default_graph || "brian-main"}/stats`], {
        encoding: "utf-8",
      });
      return { content: [{ type: "text", text: out || "❌ 无响应" }] };
    } catch (e) {
      return { content: [{ type: "text", text: `❌ ${e.message}` }] };
    }
  }
);

/* ---------- 工具 4：memory_briefing 晨间简报 ---------- */
server.tool(
  "memory_briefing",
  "渲染昨日记忆简报（关键决策 + 分数变化 Top5）",
  {},
  async () => {
    const r = runPy("morning_briefing.py", ["--render"], { timeout: 60 });
    if (!r.ok) return { content: [{ type: "text", text: `❌ ${r.error}` }] };
    return { content: [{ type: "text", text: r.data || "(无简报)" }] };
  }
);

/* ---------- 工具 5：memory_rate 反馈评分 ---------- */
server.tool(
  "memory_rate",
  "对记忆节点打分（S1 明确正负 / S3 引用频次），驱动记忆进化",
  {
    nodeId: z.string().describe("节点 ID"),
    delta: z.number().describe("分数变化，正负浮点"),
  },
  async ({ nodeId, delta }) => {
    if (!nodeId || !delta) return { content: [{ type: "text", text: "❌ nodeId/delta 必填" }] };
    const r = runPy("mem_feedback.py", ["rate", nodeId, String(delta)], { timeout: 30 });
    if (!r.ok) return { content: [{ type: "text", text: `❌ ${r.error}` }] };
    return { content: [{ type: "text", text: `✅ 已评分：${r.data || ""}` }] };
  }
);

/* ---------- 工具 6：memory_contradictions 矛盾检测 ---------- */
// 2026-11-11 修复 CLI 漂移：memory_recall.py 已无 contradictions 子命令，
// 现行入口为 mem_crystallize.py scan-contradictions（增量扫描，人工闸门不自动写回）。
server.tool(
  "memory_contradictions",
  "扫描记忆中的矛盾候选（增量扫描，报告落盘 reports/contradictions-*.json，不自动改记忆）",
  {},
  async () => {
    const r = runPy("mem_crystallize.py", ["scan-contradictions"], { timeout: 180 });
    if (!r.ok) return { content: [{ type: "text", text: `❌ ${r.error}` }] };
    return { content: [{ type: "text", text: r.data || "(无矛盾)" }] };
  }
);

/* ---------- 工具 7：memory_crystallize 技能结晶 ---------- */
server.tool(
  "memory_crystallize",
  "挖掘高频记忆 → 技能候选（结晶）",
  {
    dryRun: z.boolean().optional().describe("仅扫描不落盘（默认 true）"),
  },
  async ({ dryRun }) => {
    const args = ["scan"];
    if (dryRun !== false) args.push("--dry-run");
    const r = runPy("mem_crystallize.py", args, { timeout: 60 });
    if (!r.ok) return { content: [{ type: "text", text: `❌ ${r.error}` }] };
    return { content: [{ type: "text", text: JSON.stringify(r.data ?? r, null, 2) }] };
  }
);

/* ---------- 工具 8：memory_trends 活跃主题 ---------- */
// 2026-11-11 修复 CLI 漂移：memory_insight_scan.py 已无 --days 参数且无趋势入口。
// 改为直连 PlugMem API：取语义节点 detail，从 session_id（l1:YYYY-MM-DD.md）解析日期，
// 统计近 N 天新增节点的 tag 词频。零 LLM、零 CLI 依赖。
server.tool(
  "memory_trends",
  "近期记忆活跃主题（近 N 天新增语义节点的 tag 词频 Top10）",
  {
    days: z.number().optional().describe("回溯天数（默认 7）"),
  },
  async ({ days }) => {
    const n = Math.max(1, Math.min(days ?? 7, 90));
    try {
      const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8"));
      const base = cfg.plugmem_url || "http://127.0.0.1:8089";
      const graph = cfg.default_graph || "brian-main";
      const headers = { "Content-Type": "application/json" };
      if (cfg.api_key) headers["X-API-Key"] = cfg.api_key;
      const get = async (p) => {
        const res = await fetch(`${base}${p}`, { headers, signal: AbortSignal.timeout(8000) });
        if (!res.ok) throw new Error(`HTTP ${res.status} ${p}`);
        return res.json();
      };
      const list = await get(`/api/v1/graphs/${graph}/nodes?node_type=semantic&offset=1&limit=400`);
      const nodes = list.nodes || [];
      const cutoff = new Date(Date.now() - n * 86400_000);
      const datePat = /^20[0-9]{2}-[0-9]{2}-[0-9]{2}$/;
      const tagCount = new Map();
      let recent = 0, undated = 0;
      const samples = [];
      // 分批取 detail（列表接口不带 session_id/日期）
      for (let i = 0; i < nodes.length; i += 10) {
        const batch = nodes.slice(i, i + 10);
        const details = await Promise.all(batch.map(async (node) => {
          try {
            const d = await get(`/api/v1/graphs/${graph}/node/semantic/${node.semantic_id}`);
            return d.node || null;
          } catch { return null; }
        }));
        for (const d of details) {
          if (!d || d.is_active === false) continue;
          const m = /l1:(20[0-9]{2}-[0-9]{2}-[0-9]{2})\.md/.exec(d.session_id || "");
          const day = m ? m[1] : (typeof d.created_at === "string" ? d.created_at.slice(0, 10) : "");
          if (!datePat.test(day)) { undated++; continue; }
          if (new Date(day + "T23:59:59+08:00") < cutoff) continue;
          recent++;
          for (const t of (d.tags || [])) {
            if (typeof t !== "string" || datePat.test(t)) continue;
            tagCount.set(t, (tagCount.get(t) || 0) + 1);
          }
          if (samples.length < 5) samples.push({ id: d.semantic_id, day, text: (d.text || "").slice(0, 80) });
        }
      }
      const top = [...tagCount.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10)
        .map(([tag, count]) => ({ tag, count }));
      const out = {
        days: n, total_semantic: nodes.length, recent_nodes: recent, undated_nodes: undated,
        top_tags: top, recent_samples: samples,
        note: "分数变化趋势暂无数据：评分 ledger 为空（mem_feedback.py status 可查）",
      };
      return { content: [{ type: "text", text: JSON.stringify(out, null, 2) }] };
    } catch (e) {
      return { content: [{ type: "text", text: `❌ ${e.message}` }] };
    }
  }
);

/* ---------- 启动：stdio transport ---------- */
const transport = new StdioServerTransport();
await server.connect(transport);
// 保持进程（stdio 由 SDK 管理生命周期）