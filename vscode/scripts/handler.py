#!/usr/bin/env python3
"""handler.py — memsearch VSCode/Claude 双宿主 hook 协议主入口。

仅做 stdin→stdout 协议、事件分派与轻量注入；重活全部经 tasks/backend 模块
落为 detached 一次性子进程（--consume / maintenance.py / index）。

事件分派（恒恰好一个合法 JSON 响应 + exit 0）：
- SessionStart     → 最近记忆预览注入 additionalContext（stdout 协议按宿主分叉）
                     + detached 后台索引点火（--index-and-stamp, skip-if-running）
- UserPromptSubmit → 双宿主 RAG 注入（search top-2，对齐 workbuddy；协议分叉）
                     + [仅 vscode] 打断兜底：上一轮未闭合 turn 入队补总结
- Stop/SubagentStop/PreCompact → 幂等入队 .pending.jsonl → spawn detached --consume
- 其它（SessionEnd 等）→ 空响应放行

双宿主判别单射：transcript_path 含 "GitHub.copilot-chat" → vscode，否则 claude
（判别失败默认 claude 语义 = 安全回退）。
"""

import json
import os
import re
import sys

import backend
import tasks
from parser import VS_CODE_MARK, parse_turn

# UserPromptSubmit RAG（对齐 workbuddy：search top-2 注入；VSCode stdout 同样注入上下文）
PROMPT_MIN_LEN = 10
SEARCH_TOP_K = 2
SEARCH_TIMEOUT_S = 6  # hook 预算 10s，留余量给打断扫描与进程创建
SEARCH_QUERY_MAX = 500  # 查询截断，约束 embedding 成本
MEM_CTX_MAX = 400  # 单条注入正文上限


def _respond(obj):
    """向 stdout 打印恰好一个 JSON 对象并 flush；EPIPE 兜底。"""
    try:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        try:
            sys.stdout.write('{"continue":true}')
            sys.stdout.flush()
        except Exception:
            pass


def _project_dir(payload):
    """与上游 common.sh 对齐：候选目录落在 git 仓库内时上溯到仓库根。"""
    for cand in (payload.get("cwd"), os.environ.get("CLAUDE_PROJECT_DIR")):
        if isinstance(cand, str) and cand and os.path.isdir(cand):
            base = os.path.abspath(cand)
            return backend.git_toplevel(base) or base
    return os.getcwd()


def _host(payload):
    tp = payload.get("transcript_path") or ""
    return "vscode" if VS_CODE_MARK in str(tp) else "claude"


def meta_key(payload):
    return str(payload.get("session_id") or "unknown-session")


# ---------------------------------------------------------------------------
# SessionStart：最近记忆预览（纯文件读）+ 协议分叉 + 后台索引点火
# ---------------------------------------------------------------------------

def _recent_memory_preview(memory_dir, max_files=2, max_lines=40):
    if not os.path.isdir(memory_dir):
        return ""
    daily = sorted(
        (f for f in os.listdir(memory_dir) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f)),
        reverse=True,
    )[:max_files]
    sections = []
    for fname in daily:
        kept = []
        try:
            with open(os.path.join(memory_dir, fname), encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.rstrip("\n")
                    if re.match(r"^#{2,4}\s", s) or s.startswith("- "):
                        kept.append(s)
        except Exception:
            continue
        kept = kept[-max_lines:]
        if any(l.startswith("- ") for l in kept):
            sections.append("## %s\n%s" % (fname, "\n".join(kept)))
    return "# Recent Memory\n\n" + "\n\n".join(sections) if sections else ""


def on_session_start(payload, ctx):
    memsearch_dir = ctx["memsearch_dir"]
    memory_dir = os.path.join(memsearch_dir, "memory")
    status = "[memsearch-vscode] provider:%s backend:%s" % (
        "ok" if tasks.provider_ok() else "NOT CONFIGURED — " + backend.MS_INSTALL_HINT,
        "python -m memsearch" if backend.find_memsearch() else "MISSING — " + backend.MS_INSTALL_HINT,
    )
    if os.path.isdir(memory_dir):
        backend.bg_run(
            backend.INDEX_PID_NAME,
            [sys.executable, os.path.join(backend.SCRIPTS_DIR, "handler.py"),
             "--index-and-stamp", memory_dir, tasks.derive_collection(ctx["project_dir"]),
             memsearch_dir],
            memsearch_dir,
        )
    lag = tasks.lag_hint(memsearch_dir)
    if lag:
        status += " | " + lag
    context = _recent_memory_preview(memory_dir)
    if _host(payload) == "vscode":
        # VSCode 仅在 SessionStart 读顶层 additionalContext（实测 how-to §5.2）
        return {"additionalContext": context} if context else {}
    resp = {"continue": True, "systemMessage": status}
    if context:
        resp["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    return resp


# ---------------------------------------------------------------------------
# UserPromptSubmit：打断兜底（仅 vscode）+ RAG（双宿主）
# ---------------------------------------------------------------------------

def _search_memories(query, collection, memsearch_dir, timeout=SEARCH_TIMEOUT_S):
    """memsearch search -j → [(text, heading, source)]；任何失败返回 []。"""
    args = backend.memsearch_argv(
        "search", query[:SEARCH_QUERY_MAX], "-c", collection,
        "-k", str(SEARCH_TOP_K), "-j",
    )
    if args is None:
        return []
    rc, out = backend.run(
        args,
        timeout=timeout,
        env=backend.child_env(memsearch_dir),
    )
    if rc != 0 or not out.strip():
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    hits = []
    for r in rows if isinstance(rows, list) else []:
        text = re.sub(r"\s+", " ", str(r.get("content", ""))).strip()
        if text:
            hits.append(
                (
                    text[:MEM_CTX_MAX],
                    str(r.get("heading") or ""),
                    os.path.basename(str(r.get("source") or "")),
                )
            )
    return hits


def on_user_prompt_submit(payload, ctx):
    memsearch_dir = ctx["memsearch_dir"]
    host = _host(payload)
    prompt = (payload.get("prompt") or "").strip()
    # ① 打断兜底（仅 vscode；未闭合 turn 补总结与 RAG 同轮不冲突）
    if host == "vscode":
        transcript = payload.get("transcript_path") or ""
        if transcript and os.path.isfile(transcript):
            meta = parse_turn(transcript, interrupted=True, host="vscode")
            if meta["open_turns"]:
                marker = "interrupt:" + ",".join(meta["open_turns"])
                if ("%s:%s" % (meta_key(payload), marker)) not in tasks.summarized_set(memsearch_dir):
                    tasks.enqueue(
                        "interrupt",
                        memsearch_dir,
                        {
                            "session_id": meta_key(payload),
                            "turn_marker": marker,
                            "transcript_path": transcript,
                            "collection": tasks.derive_collection(ctx["project_dir"]),
                        },
                    )
                    tasks.spawn_consumer(memsearch_dir)
    # ② RAG 检索（双宿主；VSCode stdout 同样注入上下文，与 Claude Code 等一致）
    if len(prompt) < PROMPT_MIN_LEN:
        return {"continue": True} if host == "claude" else {}
    hits = _search_memories(
        prompt, tasks.derive_collection(ctx["project_dir"]), memsearch_dir
    )
    if not hits:
        return {"continue": True, "systemMessage": "[memsearch] Memory available"} if host == "claude" else {}
    lines = ["[memsearch] 相关历史记忆（按相关度排序，仅供参考）:"]
    for i, (text, heading, source) in enumerate(hits, 1):
        where = source + (" › " + heading if heading else "")
        lines.append("%d. (%s) %s" % (i, where, text))
    if host == "vscode":
        # 顶层协议（与 SessionStart vscode 分支同款先例）
        return {
            "additionalContext": "\n".join(lines),
            "systemMessage": "[memsearch] %d related memories" % len(hits),
        }
    return {
        "continue": True,
        "systemMessage": "[memsearch] %d related memories" % len(hits),
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        },
    }


# ---------------------------------------------------------------------------
# Stop / SubagentStop / PreCompact：幂等入队 + detached 消费
# ---------------------------------------------------------------------------

def on_stop(payload, ctx):
    if payload.get("stop_hook_active") is True:
        return {"continue": True}
    memsearch_dir = ctx["memsearch_dir"]
    transcript = payload.get("transcript_path") or ""
    sid = meta_key(payload)
    if transcript and os.path.isfile(transcript):
        # tail 标记取末行内容兜底行数，保证同会话多次 Stop 可区分
        try:
            with open(transcript, encoding="utf-8", errors="replace") as f:
                last = ""
                n = 0
                for raw in f:
                    last = raw
                    n += 1
            marker = "tail:" + (re.sub(r"[^0-9A-Za-z_-]", "", last.strip()[-48:]) or str(n))
        except Exception:
            marker = "tail:%d" % int(time.time())
        key = "%s:%s" % (sid, marker)
        if key not in tasks.summarized_set(memsearch_dir):
            tasks.enqueue(
                "stop",
                memsearch_dir,
                {
                    "session_id": sid,
                    "turn_marker": marker,
                    "transcript_path": transcript,
                    "collection": tasks.derive_collection(ctx["project_dir"]),
                },
            )
    tasks.spawn_consumer(memsearch_dir)
    return {"continue": True}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--index-and-stamp":
        if len(sys.argv) >= 5:
            return tasks.index_and_stamp(sys.argv[2], sys.argv[3], sys.argv[4])
        return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--consume":
        if len(sys.argv) >= 3:
            rc = tasks.consume_all(sys.argv[2])
            try:
                os.remove(os.path.join(sys.argv[2], backend.CONSUME_PID_NAME))
            except Exception:
                pass
            return rc
        return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        return _selftest()

    if os.environ.get("MEMSEARCH_DISABLE") == "1":
        _respond({"continue": True})
        return 0

    try:
        raw = sys.stdin.read() or ""
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    event = payload.get("hook_event_name") or "unknown"
    project_dir = _project_dir(payload)
    ctx = {
        "project_dir": project_dir,
        "memsearch_dir": os.path.join(project_dir, ".memsearch"),
    }
    try:
        if event == "SessionStart":
            response = on_session_start(payload, ctx)
        elif event == "UserPromptSubmit":
            response = on_user_prompt_submit(payload, ctx)
        elif event in ("Stop", "SubagentStop", "PreCompact"):
            response = on_stop(payload, ctx)
        else:
            response = {"continue": True}
    except Exception:
        response = {"continue": True}  # C2：任何异常都不卡死会话
    _respond(response)
    return 0


def _selftest():
    """最小自检：判别器/解析器/队列/派生 单元断言。"""
    assert _host({"transcript_path": r"c:\u\workspaceStorage\x"
                                  r"\GitHub.copilot-chat\transcripts\a.jsonl"}) == "vscode"
    assert _host({"transcript_path": "~/.claude/projects/p/a.jsonl"}) == "claude"
    assert _host({}) == "claude"
    vs_lines = [
        json.dumps({"type": "session.start"}),
        json.dumps({"type": "assistant.turn_start", "data": {"turnId": "2"}, "id": "r3"}),
        json.dumps({"id": "r4", "type": "user.message",
                    "data": {"content": "<system-reminder>x</system-reminder>帮我查 kicad"}}),
        json.dumps({"id": "r5", "type": "assistant.message",
                    "data": {"content": "好的", "reasoningText": "检索中",
                             "toolRequests": [{"name": "grep_search"}]}}),
        json.dumps({"id": "r6", "type": "tool.execution_complete",
                    "data": {"success": False, "toolCallId": "t1"}}),
    ]
    tmp = os.path.join(os.environ.get("TEMP", "."), "msvc_selftest.jsonl")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(vs_lines) + "\n")
    meta = parse_turn(tmp, interrupted=True, host="vscode")
    assert meta["open_turns"] == ["2"], meta
    assert "[User]: 帮我查 kicad" in meta["text"]
    assert "[Reasoning]: 检索中" in meta["text"] and "grep_search" in meta["text"]
    assert "[Tool failed]" in meta["text"]
    d = os.path.join(os.environ.get("TEMP", "."), "msvc_selftest_mem")
    backend.write_text(os.path.join(d, tasks.SUMMARIZED_NAME), "")
    assert tasks.summarized_set(d) == set()
    tasks.mark_summarized(d, "s1:t1")
    assert "s1:t1" in tasks.summarized_set(d)
    assert tasks.derive_collection(r"D:\a\MyProj").startswith("ms_myproj_")
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
