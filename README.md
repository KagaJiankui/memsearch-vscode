# memsearch-vscode

[![VSCode Copilot](https://img.shields.io/badge/VSCode-Copilot-007ACC?logo=visualstudiocode&logoColor=white)](https://code.visualstudio.com/docs/copilot/customization/agent-plugins)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/memsearch/)
[![memsearch](https://img.shields.io/badge/powered%20by-memsearch-FF6900)](https://github.com/zilliztech/memsearch)
[![Backend](https://img.shields.io/badge/vector%20store-Milvus-00A1E0?logo=milvus)](https://milvus.io)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/KagaJiankui/memsearch-vscode)
[![Format](https://img.shields.io/badge/plugin%20format-Claude--compatible-8A2BE2)](https://github.com/KagaJiankui/memsearch-vscode)

Automatic semantic memory for **VSCode Copilot** — captures agent sessions, summarizes
them into daily markdown memory, indexes into Milvus, and recalls relevant history via
semantic search. Standalone VSCode plugin powered by the
[memsearch](https://github.com/zilliztech/memsearch) Python package.

## Highlights

- **Zero-blocking hooks** — every hook returns within its 10 s budget; heavy work
  (summarize / index / maintenance) runs as detached one-shot processes
- **No console popups** — three-layer `CREATE_NO_WINDOW` suppression, including the
  memsearch CLI process itself (no `memsearch.exe`, no `uvx`)
- **Interrupt-safe** — user-interrupted turns (no `Stop` fired) are recovered on the
  next `UserPromptSubmit` and summarized exactly once
- **RAG on every prompt** — top-2 related memories injected into context before each
  answer, aligned with the WorkBuddy plugin behavior
- **Maintenance built-in** — `project_review` / `user_profile` / `memory_to_skill`
  distillation via the memsearch maintenance pipeline (detached, due-state throttled)

## Install

**1. memsearch Python package** (global interpreter install — do **not** use `uvx`):

```bash
pip install "memsearch[onnx]"
python -c "import memsearch"   # verify
```

**2. Register the plugin** in VSCode `settings.json`:

```jsonc
{ "chat.plugins.marketplaces": ["KagaJiankui/memsearch-vscode"] }
```

Restart VSCode — the repo is cloned to the agentPlugins directory and the
`.claude-plugin/plugin.json` manifest is picked up automatically.

## Requirements

| Item | Value |
|------|-------|
| Python | ≥ 3.11 with `memsearch[onnx]` importable |
| Vector store | Milvus (Zilliz Cloud serverless or Milvus Lite) |
| LLM | Any provider configured in `~/.memsearch/config.toml` |

## Events

| Hook | Behavior |
|------|----------|
| `SessionStart` | Inject recent-memory preview + detached incremental index |
| `UserPromptSubmit` | RAG top-2 memory injection + interrupted-turn recovery |
| `Stop` / `SubagentStop` / `PreCompact` | Idempotent enqueue → detached consume (summarize → daily markdown → index) |

## License

MIT — built on [zilliztech/memsearch](https://github.com/zilliztech/memsearch).
