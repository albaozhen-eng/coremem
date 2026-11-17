/**
 * 会话注入契约测试（2026-11-17）。
 *
 * 驱动**真实的** memory-health.ts（node 24 类型剥离），不复刻逻辑 ——
 * 复核批评过「测试自己改的东西而不是目标」。
 *
 * 锁住：
 *   无未决 + 戳新鲜 → 完全沉默
 *   戳缺失 / 过期 / 损坏 → 必须报失活（不得默认健康）
 *   有未决 → 只报具体 ref，不报颜色百分比
 *   探针崩溃 → 必须报
 *
 * 运行：node tests/test_session_inject.mjs
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const FAILS = [];
function check(name, cond, detail = "") {
  console.log(`  [${cond ? "PASS" : "FAIL"}] ${name}${detail ? "  " + detail : ""}`);
  if (!cond) FAILS.push(name);
}

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "inject-"));
process.env.HOME = TMP;               // os.homedir() 在 POSIX 读 HOME
const DIR = path.join(TMP, ".coremem", "health");
fs.mkdirSync(DIR, { recursive: true });
const FILE = path.join(DIR, "memory-health.json");
const STAMP = path.join(DIR, ".reconcile-stamp.json");

const extPath = path.join(os.homedir.call(null) === TMP ? TMP : process.cwd(), "_");
// 用真实仓库里的扩展文件（不受 HOME 改写影响）
const REAL = path.join(process.cwd(), "..", ".pi", "agent", "extensions", "memory-health.ts");
const SRC = fs.existsSync(REAL)
  ? REAL
  : path.join(process.env.PI_EXT_DIR || "", "memory-health.ts");
if (!fs.existsSync(SRC)) {
  console.log("  [SKIP] 找不到扩展源码，设 PI_EXT_DIR 指向其目录");
  process.exit(0);
}
// 复制到临时目录再 import，避免 HOME 改写影响模块解析
const LOCAL = path.join(TMP, "memory-health.ts");
fs.copyFileSync(SRC, LOCAL);
const mod = await import(pathToFileURL(LOCAL).href);

function makePi() {
  const handlers = {};
  return {
    on: (ev, fn) => { handlers[ev] = fn; },
    fire: (ev, arg) => handlers[ev]?.(arg),
    handlers,
  };
}

function setup({ stamp, checks }) {
  if (stamp === null) fs.rmSync(STAMP, { force: true });
  else if (typeof stamp === "string") fs.writeFileSync(STAMP, stamp);
  else fs.writeFileSync(STAMP, JSON.stringify(stamp));
  fs.writeFileSync(FILE, JSON.stringify({ checks: checks ?? [] }));
}

function inject() {
  const pi = makePi();
  mod.default(pi);
  pi.fire("session_start");
  const r = pi.fire("before_agent_start", { messageType: "user" });
  return r?.message?.content ?? null;
}

const fresh = { completed_at: new Date().toISOString(), overall: "green", probes_run: 18 };
const old = { completed_at: new Date(Date.now() - 30 * 3600000).toISOString(), overall: "green", probes_run: 18 };
const unresYellow = [{
  id: "unresolved", label: "未决集合（全量账本）", level: "yellow", value: 5, unit: "条",
  detail: "事实源核验失败 0 条；索引待补 5 条（L1 已核验，非数据丢失）：['2026-10-30.md:278', '2026-11-15.md:58']",
}];
const unresRed = [{
  id: "unresolved", label: "未决集合（全量账本）", level: "red", value: 1, unit: "条",
  detail: "事实源核验失败（真丢失）1 条：['2099-01-01.md:77777']；索引待补 5 条",
}];

console.log("=== 无未决 + 戳新鲜 → 完全沉默 ===");
setup({ stamp: fresh, checks: [] });
check("不注入任何内容", inject() === null);

console.log("=== fail-closed：证明不了健康就必须报 ===");
setup({ stamp: null, checks: [] });
let c = inject();
check("戳缺失 → 报失活", c !== null && /无法证明健康|失活/.test(c), (c ?? "").slice(0, 60));

setup({ stamp: old, checks: [] });
c = inject();
check("戳 30h 过期 → 报失活", c !== null && /失活/.test(c));
check("过期文案明说不代表正常", /不代表系统正常/.test(c ?? ""));

setup({ stamp: "{ 损坏", checks: [] });
c = inject();
check("戳损坏 → 报失活而非健康", c !== null && /无法证明健康/.test(c));

console.log("=== 有未决 → 只报具体 ref，不报颜色百分比 ===");
setup({ stamp: fresh, checks: unresYellow });
c = inject();
check("列出具体 ref", /2026-10-30\.md:278/.test(c ?? ""));
check("明说不是数据丢失", /不是数据丢失/.test(c ?? ""));
check("不出现 YELLOW/RED 颜色词", !/YELLOW|RED/.test(c ?? ""));
check("不出现百分比", !/\d+(\.\d+)?%/.test(c ?? ""));

setup({ stamp: fresh, checks: unresRed });
c = inject();
check("真丢失单独措辞且点名 ref", /真丢失/.test(c ?? "") && /2099-01-01\.md:77777/.test(c ?? ""));

console.log("=== 探针崩溃必须报 ===");
setup({ stamp: fresh, checks: [{ id: "kis", label: "kis（探针自身异常）", level: "red", value: null, detail: "探针抛出 RuntimeError" }] });
c = inject();
check("报出崩溃探针", /探针自身崩溃/.test(c ?? "") && /kis/.test(c ?? ""));

console.log("=== 每会话只注入一次 ===");
setup({ stamp: null, checks: [] });
{
  const pi = makePi();
  mod.default(pi);
  pi.fire("session_start");
  const a = pi.fire("before_agent_start", { messageType: "user" });
  const b = pi.fire("before_agent_start", { messageType: "user" });
  check("首次注入、第二次沉默", a != null && b == null);
}

console.log();
console.log(FAILS.length === 0 ? "全部通过" : `失败 ${FAILS.length}: ${FAILS.join(", ")}`);
process.exit(FAILS.length ? 1 : 0);
