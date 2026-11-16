#!/usr/bin/env python3
"""退化输出闸门回归（2026-11-16）。

背景：11-16 有 107 条形如「用户确认三层漏斗结构需支持多层多层多层多层错误处理」
的重复退化文本被当成事实写进 L1（事实源）。

成因之一是**自我污染回路**：排查时打印的垃圾行进入会话记录，
capture 下一轮又把它提取回 L1。所以不能只靠"上游别产出垃圾"——
事实写入前必须有确定性闸门。

本测试同时锁住两侧：
  该拦的拦住（重复退化）
  不该拦的别拦（正常事实、正常叠词「哈哈哈哈」）

运行：python3 tests/test_degenerate_gate.py
"""
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "memory_capture.py"
assert SRC.exists(), f"必须测仓库代码，找不到 {SRC}"

spec = importlib.util.spec_from_file_location("mc_degen", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["mc_degen"] = m
spec.loader.exec_module(m)

FAILS = []


def check(text, expect, why):
    got = m.is_degenerate(text)
    ok = got == expect
    print(f"  [{'PASS' if ok else 'FAIL'}] {'拦' if expect else '放'}：{why}")
    if not ok:
        FAILS.append(f"{why}（期望 {expect} 实得 {got}）")


print("=== 该拦：重复退化 ===")
check("用户确认三层漏斗结构需支持多层多层多层多层错误处理", True, "真实污染样本（多层×4）")
check("测试测试测试测试测试测试测试", True, "整词重复")
check("系统状态正常正常正常正常正常正常", True, "尾部退化")

print("=== 不该拦：曾误判的合法技术文本（2026-11-16 收紧）===")
check("The binary file begins with the magic bytes GGUF at offset 0x00000000, identifying 0x00000000",
      False, "十六进制偏移（清理 L2 时曾误判 5 个合法节点）")
check("哈希值 0000000000000000 与校验位 0000000000000000", False, "纯数字重复不算退化")

print("=== 该拦：英文词退化 ===")
check("the the the the the model output", True, "英文词连续重复")

print("=== 不该拦：正常事实与正常叠词 ===")
check("用户决定改用中文嵌入模型 bge-small-zh-v1.5", False, "普通决策事实")
check("用户说哈哈哈哈这个有意思", False, "正常叠词（3 次重复不触发）")
check("系统已修复因共享页签导航导致的sender会话查找失败问题", False, "长句无重复")
check("短", False, "过短不判（避免误拦）")
check("会议定在明天下午三点，参会人有张三李四王五", False, "含列举但非退化")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
