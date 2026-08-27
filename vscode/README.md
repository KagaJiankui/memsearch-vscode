# memsearch-vscode

VSCode Copilot 专用的 memsearch 语义记忆插件（自动捕获会话 → 摘要入记忆库 → 跨会话检索）。

## 安装

### 前置：memsearch Python 包

插件只认 **pip/conda 安装进当前解释器** 的 memsearch 包（`python -m` 入口）。
不用 uvx / PATH 上的 memsearch.exe——console-subsystem exe shim 会弹控制台黑窗。

```bash
pip install "memsearch[onnx]"     # 或 conda 安装至当前解释器环境
python -c "import memsearch"      # 验证可导入
```

首次使用还需要全局配置 `~/.memsearch/config.toml`
（`[milvus.uri]` + `[llm.providers.<name>]` + `[plugins.claude-code.summarize] provider=<name>`，
复用上游 claude-code 配置槽，与 Claude Code 端互通同一记忆集合）。

### 插件注册

本地开发：

```jsonc
// settings.json
{ "chat.pluginLocations": { "<本目录绝对路径>": true } }
```

发布后（owner/repo 拉取，整仓 clone 至 agentPlugins 目录，root `.claude-plugin/plugin.json`
触发 Claude 格式识别——实测优先级最高的格式且唯一支持 `${CLAUDE_PLUGIN_ROOT}` 替换）：

```jsonc
// settings.json
{ "chat.plugins.marketplaces": ["<you>/<this-repo>"] }
```

重启 VSCode 生效。

## 行为

| 事件 | 行为 |
|------|------|
| `SessionStart` | 注入最近记忆预览（顶层 `additionalContext`）+ detached 后台增量索引 |
| `UserPromptSubmit` | RAG 注入（search top-2 相关记忆）+ 打断兜底（未闭合 `turn_start` 补摘要） |
| `Stop` / `SubagentStop` / `PreCompact` | 幂等入队 → 秒回；后台一次性进程消费：summarize → 追加 `memory/YYYY-MM-DD.md` → index |

- hook 同步段 <200ms（无 `async`、全事件 timeout=10s），不会阻塞会话
- 三层窗口抑制：自家 spawn 带 `CREATE_NO_WINDOW`；memsearch CLI 进程经 `-c` bootstrap 对其内部 spawn（git/milvus_lite/工具循环的 exe shim）注入同款 flags；maintenance 进程同款运行时补丁
- 幂等键 `(session_id, turn 标记)` 防打断补写与 Stop 摘要重复
- 手动打断不触发 Stop（协议设计使然），由 UserPromptSubmit 兜底覆盖

## 目录结构

```text
.claude-plugin/plugin.json   manifest（Claude 格式）
hooks/hooks.json             5 事件同指 scripts/handler.py（嵌套格式 + 占位符替换）
scripts/handler.py           协议主入口：stdin→stdout 分派、事件 handlers、--selftest
scripts/backend.py           后端定位与子进程原语（find/-m 化、CREATE_NO_WINDOW、pid 防抖）
scripts/tasks.py             任务管线：幂等集、.pending.jsonl 认领式队列、消费与 maintenance 点火
scripts/parser.py            transcript 行级解析（vscode 7-type / claude 宽松 → 统一行协议）
scripts/maintenance.py       到期维护任务 runner（detached，剪枝自上游 592→110 行）
prompts/                     summarize 等 LLM 提示词
skills/memory-recall/        跨会话检索技能（对话内 "搜记忆"）
```
