# memsearch-vscode

[English](README.md) | 简体中文

[![VSCode Copilot](https://img.shields.io/badge/VSCode-Copilot-007ACC?logo=visualstudiocode&logoColor=white)](https://code.visualstudio.com/docs/copilot/customization/agent-plugins)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://pypi.org/project/memsearch/)
[![memsearch](https://img.shields.io/badge/powered%20by-memsearch-FF6900)](https://github.com/zilliztech/memsearch)
[![Backend](https://img.shields.io/badge/vector%20store-Milvus-00A1E0?logo=milvus)](https://milvus.io)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/KagaJiankui/memsearch-vscode)
[![Format](https://img.shields.io/badge/plugin%20format-Claude--compatible-8A2BE2)](https://github.com/KagaJiankui/memsearch-vscode)

给 **VSCode Copilot** 一个语义记忆——自动捕获智能体会话，总结进每日 Markdown
记忆并索引进 Milvus，提问时通过语义搜索召回相关历史。基于
[memsearch](https://github.com/zilliztech/memsearch) Python 包的独立 VSCode 插件。

## 亮点

- **零阻塞 hook**——每个 hook 都在 10 秒预算内返回；重活（总结/索引/维护）全部
  以后台一次性进程运行
- **无控制台弹窗**——三层 `CREATE_NO_WINDOW` 抑制，连 memsearch CLI 进程内部也
  覆盖（不用 `memsearch.exe`，不用 `uvx`）
- **打断安全**——手动打断的轮次（不触发 `Stop`）会在下一次 `UserPromptSubmit`
  时被找回并恰好总结一次
- **每问必检索**——每次提问前注入最相关的两条历史记忆，与 WorkBuddy 插件行为
  对齐
- **维护内置**——`project_review` / `user_profile` / `memory_to_skill` 蒸馏经
  memsearch 维护管线后台运行（due-state 节流）

## 安装

**1. 装 memsearch Python 包**（装进全局解释器——**不要**用 `uvx`）：

```bash
pip install "memsearch[onnx]"
python -c "import memsearch"   # 验证可导入
```

**2. 在 VSCode 里安装插件**：打开 **扩展** > **智能体插件（Agent Plugins）**，
点 **"+"** 输入 `KagaJiankui/memsearch-vscode`——或者按 **Ctrl+Shift+P** 运行
**"从源提供程序安装插件"**（Install Plugin from Source）填同样的 name/repo。
然后重启扩展宿主（或直接开一个新对话）——仓库会被克隆到 agentPlugins 目录，
`.claude-plugin/plugin.json` 清单自动生效。

## 要求

| 项目 | 值 |
|------|-----|
| Python | ≥ 3.11，可导入 `memsearch[onnx]` |
| 向量库 | Milvus（Zilliz Cloud serverless 或 Milvus Lite） |
| LLM | `~/.memsearch/config.toml` 里配置好的任意 provider |

## 事件

| Hook | 行为 |
|------|------|
| `SessionStart` | 注入最近记忆预览 + 后台增量索引 |
| `UserPromptSubmit` | RAG 注入 top-2 记忆 + 打断轮次找回 |
| `Stop` / `SubagentStop` / `PreCompact` | 幂等入队 → 后台消费（总结 → 每日 markdown → 索引） |

## 出处

[`vscode/prompts/`](vscode/prompts/) 中的提示词文件**逐字复制**自
[zilliztech/memsearch](https://github.com/zilliztech/memsearch) 的
[`plugins/claude-code/prompts/`](https://github.com/zilliztech/memsearch/tree/main/plugins/claude-code/prompts)。

> memsearch 以 MIT 许可证发布。版权所有 (c) Zilliz。

本仓库是一个基于 memsearch Python 包的独立 VSCode 插件，与上游项目无隶属关系，
也未获得其背书。

## 许可证

MIT —— 基于 [zilliztech/memsearch](https://github.com/zilliztech/memsearch) 构建。
