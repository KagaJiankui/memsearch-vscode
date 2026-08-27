#!/usr/bin/env python3
"""parser.py — transcript 行级解析器（双宿主格式 → 统一 (idx, role, text, id) 流）。

- vscode：7-type 记录（user.message / assistant.message / tool.execution_complete），
  结构经 docs/recorded_*/hook-schema.json 实测钉死；打断痕迹 = 未配对
  assistant.turn_start。
- claude：公开 JSONL 形态宽松解析，解析不出即空跳过不阻塞。
输出收敛到 [User]/[Assistant]/[System] 行协议，供 summarize 消费。
"""

import json
import re

VS_CODE_MARK = "GitHub.copilot-chat"

VS_NOISE_TAGS = (
    "system-reminder", "user_context", "additional_data", "current_time",
    "user_references", "user_info", "identity_context",
)
_RE_NOISE_PAIRED = re.compile(
    r"<(%s)\b[^>]*>.*?<\s*/\s*\1\s*>" % "|".join(VS_NOISE_TAGS), re.DOTALL
)
_RE_NOISE_DANGLING = re.compile(r"<(?:%s)\b[^>]*>.*$" % "|".join(VS_NOISE_TAGS), re.DOTALL)
_RE_NOISE_TAG = re.compile(r"<(?:%s)\b[^>]*/?>" % "|".join(VS_NOISE_TAGS))


def clean(text):
    out = _RE_NOISE_PAIRED.sub("", text or "")
    out = _RE_NOISE_DANGLING.sub("", out)
    return _RE_NOISE_TAG.sub("", out).strip()


def _rows_vscode(lines):
    rows = []
    for i, line in enumerate(lines):
        try:
            o = json.loads(line)
        except Exception:
            continue
        t, d = o.get("type"), o.get("data") or {}
        rid = o.get("id") or ""
        if t == "user.message":
            txt = clean(str(d.get("content", "")))
            if txt:
                rows.append((i, "user", txt, rid))
        elif t == "assistant.message":
            parts = []
            rt = clean(str(d.get("reasoningText", "")))
            if rt:
                parts.append("[Reasoning]: " + rt)
            c = clean(str(d.get("content", "")))
            if c:
                parts.append(c)
            tools = [tr.get("name") for tr in d.get("toolRequests", []) if tr.get("name")]
            if tools:
                parts.append("[Tools]: " + ", ".join(tools))
            if parts:
                rows.append((i, "assistant", "\n".join(parts), rid))
        elif t == "tool.execution_complete" and not d.get("success"):
            rows.append((i, "system", "[Tool failed]: %s" % d.get("toolCallId", ""), rid))
    return rows


def _content_blocks_text(content):
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
    return "\n".join(parts)


def _rows_claude(lines):
    # ponytail: claude JSONL 按公开形态宽松解析，解析不出即空跳过不阻塞
    rows = []
    for i, line in enumerate(lines):
        try:
            o = json.loads(line)
        except Exception:
            continue
        t, msg = o.get("type"), o.get("message") or {}
        if t in ("user", "assistant"):
            role = "user" if t == "user" else "assistant"
            txt = clean(_content_blocks_text(msg.get("content")))
            if txt:
                rows.append((i, role, txt, o.get("uuid") or ""))
    return rows


def parse_turn(transcript_path, interrupted=False, host="vscode"):
    """提取末尾回合扁平文本。
    - 正常路径：最后一条 user 起到文件尾。
    - 打断兜底：最早未闭合 turn_start 起、从其后第一条 user 行开始到文件尾。
    返回 dict(text, last_user_id, line_count, open_turns)。"""
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()]
    except Exception:
        return {"text": "", "last_user_id": "", "line_count": 0, "open_turns": []}
    line_count = len(lines)
    rows = (_rows_vscode if host == "vscode" else _rows_claude)(lines)

    open_turns = []
    start_idx = None
    if interrupted and host == "vscode":
        opened = {}
        for i, line in enumerate(lines):
            try:
                o = json.loads(line)
            except Exception:
                continue
            tid = (o.get("data") or {}).get("turnId")
            if o.get("type") == "assistant.turn_start" and tid is not None:
                opened[tid] = i
            elif o.get("type") == "assistant.turn_end" and tid is not None:
                opened.pop(tid, None)
        if opened:
            open_turns = sorted(opened, key=lambda x: int(x))
            start_idx = min(opened.values())

    body = [r for r in rows if start_idx is None or r[0] >= start_idx]
    u = next((k for k, r in enumerate(body) if r[1] == "user"), None)
    if u is None:
        return {"text": "", "last_user_id": "", "line_count": line_count,
                "open_turns": open_turns}
    sel = body[u:]
    text = "\n".join("[%s]: %s" % (r[1].capitalize(), r[2]) for r in sel)
    return {
        "text": text,
        "last_user_id": sel[0][3],
        "line_count": line_count,
        "open_turns": open_turns,
    }
