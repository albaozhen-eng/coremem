#!/usr/bin/env node
/**
 * MCP 接口回归测试 — 部署 cli 或升级 MCP 后跑一遍，防「server.mjs 调的 CLI 参数已删」类漂移。
 * 用法: node test-mcp-fix.mjs [server.mjs 路径]
 *   默认 ~/.plugmem/mcp/server.mjs；也可用 MCP_SERVER_PATH 环境变量指定。
 * 覆盖: memory_trends / memory_contradictions / memory_stats（2026-11-11 漂移事故的两个工具 + 服务冒烟）。
 * 判定: 任一工具返回 ❌ 或抛错 → 退出码 1。
 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import os from "node:os";
import path from "node:path";

const serverPath = process.argv[2] || process.env.MCP_SERVER_PATH ||
  path.join(os.homedir(), ".plugmem", "mcp", "server.mjs");
const t = new StdioClientTransport({ command: "node", args: [serverPath] });
const c = new Client({ name: "mcp-regression", version: "1.0.0" });
await c.connect(t);

const cases = [
  ["memory_trends", { days: 7 }],
  ["memory_contradictions", {}],
  ["memory_stats", {}],
];
let failed = 0;
for (const [name, args] of cases) {
  try {
    const r = await c.callTool({ name, arguments: args });
    const text = r.content?.[0]?.text || "";
    const ok = text && !text.startsWith("❌");
    console.log(`${ok ? "✅" : "❌"} ${name}: ${text.slice(0, 120).replace(/\n/g, " ")}`);
    if (!ok) failed++;
  } catch (e) {
    console.log(`❌ ${name}: ${e.message}`);
    failed++;
  }
}
await c.close();
if (failed) {
  console.error(`回归失败：${failed}/${cases.length} 个工具异常（CLI 接口可能又漂移了）`);
  process.exit(1);
}
console.log(`回归通过：${cases.length}/${cases.length}`);