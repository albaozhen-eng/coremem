#!/usr/bin/env python3
"""难题集不变量回归（P2-A，2026-11-16）。

背景：KIS 300 题已 99.7% 饱和，golden n=33（1 题=3 点、CI±12），
两套都只能防退步、测不出改进。根因是 KIS 的 query 取自目标行本身
（`query = " ".join(picked)`），词面重合即可命中。

第一版难题集**失败**：只剔除答案词，剩下的上下文词仍是原文逐字词，
AND 匹配唯一锁定目标行（实测 54/60 题唯一命中）→ 99.4%，与 KIS 无差。

第二版改用**高频（低区分度）上下文词** + 歧义度门槛，实测 50.6% / 46.7%，
终于有约 50 点空间可测改进。

本测试锁住三条不变量（缺了任一条，这个集就退化成 KIS）：
  1 答案 token 不出现在 query 里
  2 每题歧义度 ≥ min_ambig（AND 命中行数；=1 就是词面唯一锁定）
  3 target_sha 与现文件一致（否则测的是漂移不是检索）

运行：python3 tests/test_hard_set.py
无部署题集时（干净 clone）跳过，只做代码层断言。
"""
import importlib.util
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))

spec = importlib.util.spec_from_file_location("hb", SRC / "hard_build.py")
hb = importlib.util.module_from_spec(spec)
sys.modules["hb"] = hb
spec.loader.exec_module(hb)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


print("=== 代码层：判据函数存在且行为正确 ===")
check("有 build_df（文档频率）", hasattr(hb, "build_df"))
check("有 ambiguity（歧义度）", hasattr(hb, "ambiguity"))
lines = [("a.md:1", "系统部署在服务器甲"), ("a.md:2", "系统部署在服务器乙"),
         ("a.md:3", "完全无关的一行内容")]
check("ambiguity 正确计数", hb.ambiguity(["系统", "部署"], lines) == 2,
      f"实得 {hb.ambiguity(['系统', '部署'], lines)}")
df = hb.build_df(lines)
# 统一分词后「系统」是 bigram，应出现在 2 行；「完全」只在第 3 行
check("build_df 按词而非整句统计", df.get("系统") == 2 and df.get("完全") == 1,
      f"系统={df.get('系统')} 完全={df.get('完全')}")

SET = pathlib.Path.home() / ".coremem/memory/golden/hard-v2.json"
if not SET.exists():
    print("  [SKIP] 无部署题集（干净 clone），仅代码层断言")
else:
    d = json.loads(SET.read_text(encoding="utf-8"))
    items = d["items"]
    min_amb = d.get("min_ambig", 5)
    print(f"=== 题集层：{len(items)} 题，min_ambig={min_amb} ===")

    bad_ans = [it["id"] for it in items
               if any(a.lower() in it["query"].lower() for a in it["answer_tokens"])]
    check("答案词不出现在 query 里", not bad_ans, f"违例 {len(bad_ans)}: {bad_ans[:3]}")

    low = [it["id"] for it in items if it.get("ambiguity", 0) < min_amb]
    check(f"每题歧义度 ≥{min_amb}", not low, f"违例 {len(low)}: {low[:3]}")

    import mo_common
    P = re.compile(r"^-\s*\d{4}-\d{2}-\d{2}T[^\s]*\s*(\[[^\]]*\])?\s*")
    MEM = mo_common.MEMORY_DIR
    bad_sha = []
    for it in items:
        fn, ln = it["target_ref"].rsplit(":", 1)
        f = MEM / fn
        if not f.exists():
            bad_sha.append(it["id"]); continue
        ls = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        if int(ln) > len(ls):
            bad_sha.append(it["id"]); continue
        if mo_common.line_sha(P.sub("", ls[int(ln) - 1]).strip()) != it["target_sha"]:
            bad_sha.append(it["id"])
    check("target_sha 与现文件一致", not bad_sha, f"漂移 {len(bad_sha)}: {bad_sha[:3]}")

    check("题量 ≥150（CI 约 ±5 点）", len(items) >= 150, f"实际 {len(items)}")

print()
print("全部通过" if not FAILS else f"失败 {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
