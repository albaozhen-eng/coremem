# coremem

**Git-diffable memory for AI agents.** A six-layer reference architecture where plain-text files are the source of truth — your agent's memory can be read, edited, versioned, audited, and *shown* like code.

> 中文文档见 [README.zh-CN.md](README.zh-CN.md)

```
L0 conversations (.jsonl, immutable)
L1 daily notes   (.md, source of truth)        ← humans and agents write here
L2 semantic index (vector search, PlugMem-compatible)
L3 compiled cache (MEMORY.md)
L5 knowledge projection (Obsidian graph vault)  ← humans *see* memory here
```

One-way data flow. Single write entry point. Fan-out is code, not vibes.

## Why this exists

Every team that ships an AI agent eventually meets the same monster: **the amnesiac agent**. Chat context evaporates; vector DBs become black holes nobody can audit; SaaS memory layers lock your data behind an API you can't grep. coremem takes the opposite bet:

- **Files are truth.** Daily notes and facts are Markdown/JSONL. `git diff` your agent's brain. Review it in a PR. Recover it from a backup you already have.
- **Human-in-the-loop graph.** One command projects the memory graph into an [Obsidian](https://obsidian.md) vault — frontmatter for Dataview, `[[wikilinks]]` for edges. Your agent's memory becomes a nebula a human can actually inspect and rate.
- **Local-first, no lock-in.** Works with any [PlugMem-compatible](https://github.com/TIMAN-group/PlugMem) semantic engine (self-hosted, ~800 lines of server code) or with none — L1 still *is* the truth.
- **Battle-tested degradation.** Every failed write lands in an on-disk buffer and replays automatically. In production we lost zero writes across network outages, rate-limit storms and process kills.

## Show me

```bash
pip install -r requirements.txt

python3 src/remember.py "Migrated checkout to v2; keep old API until Oct" --tags decision,eng
python3 src/memory_recall.py all "checkout v2"
python3 src/remember.py --status

# the party trick: project your memory graph into Obsidian
python3 src/vault_export.py --limit 400 --vault ~/memory-vault/Memory
# → open ~/memory-vault as a vault → graph view → 400 nodes, 370+ tag pages
```

```
📊 memory status
   daily log: ✅ 2026-09-06.md
   semantic index: ✅ ok
   buffer: ✅ empty (45/45 degraded writes replayed)
```

## Benchmarks (real graph, 49k nodes)

Hybrid (cosine × BM25) candidate scoring, vectorized with numpy `argpartition`:

| | p50 | p95 | top-5 consistency |
|---|---|---|---|
| legacy loop | 18.4 ms | 19.4 ms | — |
| coremem/PlugMem path | **1.4 ms** | **1.7 ms** | **100%** (5/5 queries) |

Reproduce: `python3 scripts/bench_p0_2b.py` (read-only, runs offline).

## How it fits the ecosystem

| | Mem0 / Zep / Letta | **coremem** |
|---|---|---|
| Source of truth | database | **plain-text files** |
| Human audit | via API/console | `git diff`, Obsidian graph, Dataview |
| Deployment | SaaS or server+DB | files first; optional self-hosted semantic engine |
| Failure mode | silent write loss | **on-disk buffer + auto replay** |
| Role | memory *service* | memory *architecture you own* |

Position it as: **the reference implementation for the "files-as-memory" school**, with a semantic engine ([PlugMem](https://github.com/TIMAN-group/PlugMem)) behind L2 when you need vectors.

## Six layers, in detail

| Layer | Medium | Mutability | Who writes |
|---|---|---|---|
| L0 session log | `.jsonl` | immutable | harness |
| L1 daily notes | `.md` | append/merge | `remember.py` (the **only** write entry) |
| L2 semantic index | vectors | rebuilt from L1 | buffer flush / live write |
| L3 compiled cache | `MEMORY.md` | regenerated | consolidation |
| L4 (reserved) | — | — | — |
| L5 projection | Obsidian vault | regenerated | `vault_export.py` |

Deployment across machines is a first-class flow: `bash deploy/deploy.sh <repo> <dest> <profile>` patches defaults to each machine's live paths and runs a smoke test. Pre-commit `secret_scan` keeps personal data out of anything public — the same discipline the repo practices itself.

## Status

Early but production-honest: the architecture, write path, degradation buffer, recall, consolidation and Obsidian projection all run daily in a real small-company deployment (agent workloads incl. recruitment pipelines and customer service). Docs, packaging and the MCP server are the current focus — see [good-first-issues](https://github.com/albaozhen-eng/coremem/issues).

## License

MIT

## Disclaimer

coremem stores *memories*. Treat L1/L5 as sensitive as your database. The Obsidian projection is for local human review — never sync a real vault to a public channel.
