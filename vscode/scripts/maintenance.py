#!/usr/bin/env python3
"""maintenance.py — VSCode 插件到期维护任务 runner（detached 后台进程，不在 hook 预算内）。

剪枝自上游 _shared/scripts/maintenance_runner.py（592→~120 行）：
- 删 opencode JSONC/隔离 config/路径重写（~310 行）与五平台 native provider 胶水
  （~100 行）——VSCode 无 headless agent CLI，native 分支不可达；
- 删 ensure_memsearch_importable 的 shebang re-exec 与 uv bootstrap——单解释器 +
  禁 uvx 公理，缺包直接报 MS_INSTALL_HINT（pip/conda 全局 import 指引）；
- platform 固定 "claude-code"（上游原生主槽，占位 backend 类型与 summarize 链
  一致），provider 为空/native 时回落 claude-code summarize 托管 provider，
  绝不触本地 CLI。

已知现象（非本插件缺陷）：维护任务 LLM 偶发不返回 JSON → run_task_llm 的
_parse_task_response 解析失败，任务记 failed/reason。上游已提 issue，彻底修复
方案 = JSON 约束解码（structured output / guided decoding）+ 手写工具调用的
micro agent loop（替代自由文本→解析）。本插件不改上游行为，待上游版本落地；
失败任务经 due-state 节流自动重试。

Usage: maintenance.py [--project-dir D] [--memsearch-dir D] [--force] [--json-output]
"""

import argparse
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

MAINT_PID_NAME = ".maintenance.pid"
MS_INSTALL_HINT = (
    "memsearch 包不可导入 — 请先全局安装：pip install 'memsearch[onnx]' "
    "或 conda 安装至当前解释器环境（勿用 uvx/uv tool add），"
    "验证：python -c \"import memsearch\""
)


def _suppress_child_windows():
    """本进程一切子孙 spawn（memsearch 工具循环的 memsearch.exe、skills 的 git、
    milvus_lite 等）默认注入 CREATE_NO_WINDOW——包内部 spawn 均无 flags，
    console-subsystem exe 会在无控制台父进程下弹黑窗。
    Popen 以子类替换：lambda 会炸掉 asyncio 的 class Popen(subprocess.Popen) 继承。"""
    if os.name != "nt":
        return
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _run = subprocess.run

    def run(*a, **k):
        k.setdefault("creationflags", flag)
        return _run(*a, **k)

    class Popen(subprocess.Popen):
        def __init__(self, *a, **k):
            k.setdefault("creationflags", flag)
            super().__init__(*a, **k)

    subprocess.run = run
    subprocess.Popen = Popen


def _memsearch_importable():
    try:
        return bool(importlib.util.find_spec("memsearch"))
    except Exception:
        return False


def apply_plugin_prompt_defaults(cfg):
    # scripts/ 在插件根下一层，parent.parent 恰好解析到 vscode/prompts/
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    for task in ("project_review", "user_profile", "memory_to_skill"):
        if getattr(cfg.prompts, task, ""):
            continue
        prompt_file = prompts_dir / f"{task}.txt"
        if prompt_file.is_file():
            setattr(cfg.prompts, task, str(prompt_file))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run memsearch maintenance tasks (VSCode plugin).")
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--memsearch-dir", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()

    if not _memsearch_importable():
        sys.stderr.write(MS_INSTALL_HINT + "\n")
        return 1
    _suppress_child_windows()

    from memsearch.config import resolve_config
    from memsearch.maintenance import run_due_tasks, run_task_llm
    from memsearch.skills import distill as distill_skills

    project_dir = Path(args.project_dir or Path.cwd()).expanduser().resolve()
    memsearch_dir = args.memsearch_dir
    old_cwd = Path.cwd()
    try:
        os.chdir(project_dir)
        cfg = resolve_config()
        apply_plugin_prompt_defaults(cfg)
        try:
            # [plugins.claude-code] 槽含连字符，cfg 对象属性访问不可行 → tomllib 直读
            import tomllib

            with open(os.path.expanduser("~/.memsearch/config.toml"), "rb") as f:
                _slot = (
                    tomllib.load(f).get("plugins", {}).get("claude-code", {}).get("summarize", {})
                )
            fallback = str(_slot.get("provider") or "")
        except Exception:
            fallback = ""

        def llm_runner(ctx, prompt):
            provider = (ctx.task_config.provider or "native").strip()
            if provider in ("", "native") and fallback:
                ctx = replace(ctx, task_config=replace(ctx.task_config, provider=fallback))
            return run_task_llm(ctx, prompt, cfg)

        results = run_due_tasks(
            platform="claude-code",
            project_dir=project_dir,
            memsearch_dir=memsearch_dir,
            cfg=cfg,
            force=args.force,
            llm_runner=llm_runner,
        )
        skill_result = distill_skills(
            platform="claude-code",
            project_dir=project_dir,
            memsearch_dir=memsearch_dir,
            cfg=cfg,
            force=args.force,
            llm_runner=llm_runner,
        )
    except Exception as exc:  # noqa: BLE001 — detached 后台进程只报错不 crash 窗口
        sys.stderr.write("Maintenance error: %s\n" % exc)
        return 1
    finally:
        if memsearch_dir:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(memsearch_dir, MAINT_PID_NAME))
        with contextlib.suppress(OSError):
            os.chdir(old_cwd)

    if args.json_output:
        payload = [r.__dict__ for r in results]
        payload.append({"task": "memory_to_skill", **skill_result.__dict__})
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    else:
        for r in results:
            detail = f": {r.reason}" if r.reason else ""
            sys.stdout.write(f"{r.task}: {r.action}{detail}\n")
        skill_detail = f": {skill_result.reason}" if skill_result.reason else ""
        sys.stdout.write(f"memory_to_skill: {skill_result.action}{skill_detail}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
