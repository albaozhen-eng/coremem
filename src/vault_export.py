#!/usr/bin/env python3
"""vault_export.py — L5 知识投影：把 PlugMem 记忆图谱导出为 Obsidian 图谱 vault。

每节点一个 md 文件（frontmatter 可被 Dataview 查询），标签成页、链接互通——
Obsidian graph view 直接变成「人类可读的记忆图谱」。

用法:
  python3 vault_export.py                 # 全量刷新（默认上限 400 活跃节点）
  python3 vault_export.py --limit 100     # 小批量试运行
  python3 vault_export.py --check         # 只报告将导出多少节点

配置（与其余 coremem 脚本同通道）:
  MEM_VAULT_DIR       导出目标 vault 目录（默认 ~/.coremem/vault/Memory）
  MEM_PLUGMEM_BASE    PlugMem API 地址（默认自动发现: env > openclaw.json > 8089）
  MEM_PLUGMEM_API_KEY / graph 同 plugmem_config 规则

⚠️ 隐私红线：导出的 vault 含真实记忆内容，只应存在于本机——
   绝不允许同步进任何公开站点/业务知识库（发布纪律同 secret_scan）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plugmem_config  # noqa: E402

VAULT_DIR = Path(os.environ.get("MEM_VAULT_DIR", Path.home() / ".coremem" / "vault" / "Memory"))
TYPE_DIRS = {"semantic": "Semantic", "procedural": "Procedural", "subgoal": "Subgoal", "episodic": "Episodic"}
EXPORT_TYPES = ("semantic", "procedural", "subgoal")


def api(path: str):
    cfg = plugmem_config.plugmem_cfg()
    url = f"{cfg['base_url']}{path}"
    req = urllib.request.Request(url)
    if cfg.get("api_key"):
        req.add_header("X-API-Key", cfg["api_key"])
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def slug(text: str, limit: int = 26) -> str:
    s = re.sub(r"[\[\]#|^:：/\\\n\r\t\"'’‘“”]+", "", text or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:limit] or "untitled")


def node_filename(node_id: str, label: str) -> str:
    return f"{node_id}-{slug(label)}.md"


def extract_date(text: str) -> str:
    m = re.match(r"^(20\d{2}-\d{2}-\d{2})", text or "")
    return m.group(1) if m else ""


def fetch_topo(node_limit: int):
    gid = plugmem_config.plugmem_cfg()["graph_id"]
    return api(f"/api/v1/graphs/{gid}/topology?node_limit={node_limit}")


def select_nodes(topo, limit: int):
    """活跃 + credibility 降序 + 新（大 id）优先，跨层取 top N。"""
    cand = []
    for n in topo.get("nodes", []):
        d = n.get("data", {})
        t = d.get("type", "")
        if t in EXPORT_TYPES and d.get("is_active"):
            cand.append(d)
    cand.sort(key=lambda d: (-(d.get("credibility") or 0), -int(re.sub(r"\D", "", str(d.get("id", 0))) or 0)))
    return cand[:limit]


def build_links(topo, keep_ids: set):
    """source -> [(kind, target_labelish)]，仅保留两端都在导出集内的边。"""
    out = {}
    for e in topo.get("edges", []):
        d = e.get("data", {})
        s, t, kind = d.get("source", ""), d.get("target", ""), d.get("kind", "")
        if s in keep_ids and t in keep_ids:
            out.setdefault(s, []).append((kind, t))
    return out


def fm_escape(v: str) -> str:
    return str(v).replace('"', "'").replace("\n", " ")


def export(vault: Path, limit: int, check_only: bool = False):
    topo = fetch_topo(max(limit * 8, 3000))
    state_file = vault / ".vault_export_state.json"
    vault.mkdir(parents=True, exist_ok=True)
    nodes = select_nodes(topo, limit)
    id2file = {d["id"]: (TYPE_DIRS[d["type"]], node_filename(d["id"], d.get("label", ""))) for d in nodes}
    links = build_links(topo, set(id2file.keys()))
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    new_state, written = dict(state), 0
    tag_members = {}

    if check_only:
        print(f"将导出 {len(nodes)} 节点（topology {len(topo.get('nodes', []))} 可见，"
              f"边 {len(links)} 个节点有关联）→ {vault}")
        return

    (vault / "总览.md").parent.mkdir(parents=True, exist_ok=True)
    for d in nodes:
        nid, ntype = d["id"], d["type"]
        folder, fname = id2file[nid]
        fpath = vault / folder / fname
        detail = {}
        try:
            raw = api(f"/api/v1/graphs/{plugmem_config.plugmem_cfg()['graph_id']}"
                      f"/node/{ntype}/{re.sub(r'[^0-9]', '', nid)}")
            detail = raw.get("node", {}) or {}
        except Exception:
            pass
        text = detail.get("text") or detail.get(f"{ntype}_memory") or d.get("label", "")
        tags = detail.get("tags") or []
        cred = detail.get("credibility", d.get("credibility", 0))
        day = extract_date(text) or extract_date(d.get("label", ""))
        fingerprint = f"{hash(text)}|{cred}|{','.join(sorted(tags))}"
        if state.get(nid) == fingerprint and fpath.exists():
            continue  # 未变，跳过

        lines = ["---",
                 f'id: "{nid}"',
                 f"type: {ntype}",
                 f"credibility: {cred}",
                 f"date: {day}" if day else "date: null",
                 "tags: [" + ", ".join(f'"{fm_escape(t)}"' for t in tags) + "]",
                 f'source: plugmem/{plugmem_config.plugmem_cfg()["graph_id"]}',
                 f"exported: {time.strftime('%Y-%m-%d')}", "---", "", text, ""]

        rel = []
        for kind, tgt in links.get(nid, []):
            if tgt in id2file:
                rel.append(f"- {kind} → [[{Path(id2file[tgt][1]).stem}|{slug(tgt)}]]")
        for t in tags:
            tag_members.setdefault(t, []).append(fname)
            rel.append(f"- tagged → [[标签/{slug(t, 40)}|{slug(t, 40)}]]")
        if rel:
            lines += ["## 关联", ""] + rel + [""]
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text("\n".join(lines), encoding="utf-8")
        new_state[nid] = fingerprint
        written += 1

    # 标签页
    tdir = vault / "标签"
    tdir.mkdir(parents=True, exist_ok=True)
    for t, members in tag_members.items():
        tp = tdir / f"{slug(t, 40)}.md"
        head = "---\ntype: tag\n---\n\n# " + slug(t, 40) + "\n\n## 记忆成员\n\n"
        body = "".join(f"- [[{Path(m).stem}]]\n" for m in members)
        tp.write_text(head + body, encoding="utf-8")

    # 总览 MOC
    moc = ["---\ntype: moc\n---\n\n# 记忆图谱总览\n",
           "> 由 vault_export.py 生成 — Obsidian 打开本目录即得人类可读记忆图谱。\n",
           "## Dataview 面板（需安装 Dataview 插件）\n",
           "```dataview\nTABLE credibility AS 可信度, date AS 日期, type AS 层\nWHERE type != \"tag\"\nSORT credibility DESC\nLIMIT 20\n```",
           "\n## 按层浏览\n"]
    for folder in sorted(set(f for f, _ in id2file.values())):
        cnt = sum(1 for _, (fd, _) in id2file.items() if fd == folder)
        moc.append(f"- [[{folder}]] 索引（{cnt} 条）")
    (vault / "总览.md").write_text("\n".join(moc) + "\n", encoding="utf-8")

    state_file.write_text(json.dumps(new_state, ensure_ascii=False))
    print(f"✓ 导出 {written}/{len(nodes)} 节点（增量）｜标签页 {len(tag_members)} 个 → {vault}")
    print("⚠️ vault 含真实记忆，仅限本机，勿同步任何公开渠道。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--vault", default=str(VAULT_DIR))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    export(Path(args.vault).expanduser(), args.limit, args.check)
