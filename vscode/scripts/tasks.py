#!/usr/bin/env python3
"""tasks.py — 任务管线：幂等集 / 待处理队列（原子认领）/ 任务消费 / maintenance 点火。

队列语义：多消费者并发时由 os.replace 整队认领保证单写，宁丢勿重
（ponytail ceiling：消费者中途崩溃则本轮丢摘要）。
pending 与 summarized 的"运行时合并"：pending.jsonl 仅在有积压时存在——
认领即从磁盘消失，消费键并入 .summarized-turns，队列为 summarized 的临时增量；
空队列不留 0B 残留，enqueue 按需重建。
"""

import hashlib
import json
import os
import re
import sys
import time

import backend
import parser as transcript_parser

LAG_WINDOW_S = int(os.environ.get("MEMSEARCH_WB_LAG_WINDOW", "1200") or 1200)
INDEX_TS_NAME = ".last-index-completed"
PENDING_NAME = ".pending.jsonl"
SUMMARIZED_NAME = ".summarized-turns"

_PROVIDER_ERR = ""


def derive_collection(project_dir):
    """复刻 derive-collection.sh：ms_<sanitized>_<sha256前8位>。
    哈希输入必须是 MSYS Unix 形态绝对路径（D:\\a\\b → /d/a/b），否则分叉。"""
    p = os.path.abspath(project_dir).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        p = "/%s/%s" % (m.group(1).lower(), m.group(2))
    base = os.path.basename(p.rstrip("/"))
    sanitized = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", base.lower())).strip("_")[:40]
    digest = hashlib.sha256(p.encode("utf-8")).hexdigest()[:8]
    return "ms_%s_%s" % (sanitized, digest)


def write_index_ts(memsearch_dir):
    try:
        backend.write_text(
            os.path.join(memsearch_dir, INDEX_TS_NAME), str(int(time.time()))
        )
    except Exception:
        pass


def lag_hint(memsearch_dir):
    try:
        with open(os.path.join(memsearch_dir, INDEX_TS_NAME), encoding="utf-8") as f:
            ts = int(f.read().strip())
    except Exception:
        return ""
    age = int(time.time()) - ts
    if 0 <= age < LAG_WINDOW_S:
        return "远端索引统计追平中（上次索引完成于 %d 分钟前），新记忆可能暂时查无结果" % max(1, age // 60)
    return ""


# ---------------------------------------------------------------------------
# 幂等集合与待处理队列
# ---------------------------------------------------------------------------

def summarized_set(memsearch_dir):
    try:
        with open(os.path.join(memsearch_dir, SUMMARIZED_NAME), encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except Exception:
        return set()


def mark_summarized(memsearch_dir, key):
    backend.append_text(os.path.join(memsearch_dir, SUMMARIZED_NAME), key + "\n")


def enqueue(task, memsearch_dir, payload_extra):
    row = json.dumps({"task": task, **payload_extra}, ensure_ascii=False)
    backend.append_text(os.path.join(memsearch_dir, PENDING_NAME), row + "\n")


def _claim_pending(memsearch_dir):
    """原子认领积压任务：rename 整个队列文件，多消费者竞争中仅一者拿到内容，
    从根上排除同任务双写（幂等集是兜底不是并发屏障）。
    认领后源文件即消失（消费键并入 .summarized-turns，pending 仅在有积压时存在，
    空队列不留 0B 残留；enqueue 侧 append_text 会按需重建）。"""
    src = os.path.join(memsearch_dir, PENDING_NAME)
    dst = src + ".claimed"
    try:
        os.remove(dst)  # 清上次崩溃残留
    except OSError:
        pass
    try:
        os.replace(src, dst)
    except OSError:
        return []
    try:
        with open(dst, encoding="utf-8") as f:
            return [l for l in (raw.strip() for raw in f) if l]
    except OSError:
        return []


def _release_claimed(memsearch_dir):
    try:
        os.remove(os.path.join(memsearch_dir, PENDING_NAME) + ".claimed")
    except OSError:
        pass


def consume_all(memsearch_dir):
    tasks = _claim_pending(memsearch_dir)
    for line in tasks:
        try:
            task = json.loads(line)
        except Exception:
            continue
        try:
            run_task(task, memsearch_dir)
        except Exception as exc:  # 单任务失败不炸整队；失败也已记 stderr 供排障
            sys.stderr.write("[memsearch-vscode] task failed: %s\n" % exc)
    _release_claimed(memsearch_dir)  # ponytail: 中途崩溃则本轮丢摘要，宁缺勿重
    return 0


def run_task(task, memsearch_dir):
    session_id = task.get("session_id") or "unknown-session"
    marker = task.get("turn_marker") or task["task"]
    key = "%s:%s" % (session_id, marker)
    if key in summarized_set(memsearch_dir):
        return
    tp = task.get("transcript_path") or ""
    meta = (
        transcript_parser.parse_turn(
            tp, interrupted=task["task"] == "interrupt",
            host="vscode" if transcript_parser.VS_CODE_MARK in tp else "claude",
        )
        if tp and os.path.isfile(tp)
        else {"text": ""}
    )
    memory_dir = os.path.join(memsearch_dir, "memory")
    now = time.strftime("%H:%M")
    memory_file = os.path.join(memory_dir, "%s.md" % time.strftime("%Y-%m-%d"))

    ms = backend.find_memsearch()
    summary, failure = "", ""
    if not ms:
        failure = backend.MS_INSTALL_HINT
    elif not meta["text"]:
        failure = "transcript empty/unparsable; nothing to summarize"
    elif not provider_ok():
        failure = _PROVIDER_ERR
    else:
        rc, out = backend.run(
            backend.memsearch_argv("summarize", "--plugin", "claude-code"),
            input_text=meta["text"],
            timeout=110,
            env=backend.child_env(memsearch_dir),
        )
        if rc in (-124, 124):
            failure = "summarizer timed out"
        elif rc != 0:
            failure = "summarizer exited with status %d" % rc
        elif not out.strip():
            failure = "summarizer returned empty output"
        else:
            summary = out.strip()

    need_heading = True
    try:
        with open(memory_file, encoding="utf-8", errors="replace") as f:
            need_heading = ("session:%s" % session_id) not in f.read()
    except Exception:
        pass
    turn_label = (
        "interrupt:%s" % ",".join(meta["open_turns"]) if meta["open_turns"] else marker
    )
    parts = []
    if need_heading:
        parts.append("\n## Session %s\n" % now)
    parts.append("### %s" % now)
    parts.append(
        "<!-- session:%s turn:%s transcript:%s -->" % (session_id, turn_label, tp)
    )
    parts.append(
        summary if summary else
        "- Memory summary unavailable: %s; transcript content was omitted." % failure
    )
    parts.append("")
    backend.append_text(memory_file, "\n".join(parts))
    mark_summarized(memsearch_dir, key)

    if ms and summary:
        collection = task.get("collection") or ""
        args = backend.memsearch_argv("index", memory_dir)
        if collection:
            args += ["-c", collection]
        rc, _ = backend.run(args, timeout=300, env=backend.child_env(memsearch_dir))
        if rc == 0:
            write_index_ts(memsearch_dir)
            # detached 点火到期 maintenance（due-state 节流，未到期近似 no-op）
            backend.bg_run(
                backend.MAINT_PID_NAME,
                [
                    sys.executable,
                    backend.MAINTENANCE_SCRIPT,
                    "--project-dir",
                    os.path.dirname(os.path.abspath(memsearch_dir)),
                    "--memsearch-dir",
                    memsearch_dir,
                ],
                memsearch_dir,
            )


# ---------------------------------------------------------------------------
# --consume 主入口包装
# ---------------------------------------------------------------------------

def spawn_consumer(memsearch_dir):
    """一次性 detached 消费者：存在积压才点火（文件不存在/空均不点火）。"""
    pending = os.path.join(memsearch_dir, PENDING_NAME)
    try:
        if os.path.getsize(pending) == 0:
            return
    except OSError:
        return
    backend.bg_run(
        backend.CONSUME_PID_NAME,
        [sys.executable, os.path.join(backend.SCRIPTS_DIR, "handler.py"), "--consume", memsearch_dir],
        memsearch_dir,
    )


def index_and_stamp(memory_dir, collection, memsearch_dir):
    args = backend.memsearch_argv("index", memory_dir)
    if args is None:
        return 1
    if collection:
        args += ["-c", collection]
    backend.run(args, timeout=300, env=backend.child_env(memsearch_dir))
    write_index_ts(memsearch_dir)
    try:
        os.remove(os.path.join(memsearch_dir, backend.INDEX_PID_NAME))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# summarize provider 校验（对齐 cli.summarize：读 plugins.claude-code 槽）
# ---------------------------------------------------------------------------

def provider_ok():
    global _PROVIDER_ERR
    cfg_path = os.path.expanduser(os.path.join("~", ".memsearch", "config.toml"))
    try:
        import tomllib

        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        _PROVIDER_ERR = "global config unreadable (%s)" % e
        return False
    slot = cfg.get("plugins", {}).get("claude-code", {}).get("summarize", {})
    name = str(slot.get("provider") or "").strip() if isinstance(slot, dict) else ""
    if not name or name == "native":
        _PROVIDER_ERR = "plugins.claude-code.summarize.provider empty/native"
        return False
    prov = cfg.get("llm", {}).get("providers", {}).get(name)
    if not isinstance(prov, dict):
        _PROVIDER_ERR = "unknown provider %r — configure [llm.providers.%s]" % (name, name)
        return False
    return True
