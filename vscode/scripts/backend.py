#!/usr/bin/env python3
"""backend.py — memsearch 后端定位与子进程原语（backend/tasks/handler 三层共享）。

- 后端仅认 pip/conda 装进当前解释器的 memsearch 包（python -m 入口）；
  不用 uvx / PATH 上的 memsearch.exe——console-subsystem shim 在无控制台父进程
  下会弹黑窗。缺包时调用方以 MS_INSTALL_HINT 报错指引安装。
- 一切子进程：同步路径 CREATE_NO_WINDOW；detached 路径另加
  DETACHED_PROCESS|start_new_session；超时→负码返回，绝不上抛。
"""

import importlib.util
import os
import subprocess
import sys

MS_INSTALL_HINT = (
    "memsearch 包不可导入 — 请先全局安装：pip install 'memsearch[onnx]' "
    "或 conda 安装至当前解释器环境（勿用 uvx/uv tool add），"
    "验证：python -c \"import memsearch\""
)

INDEX_PID_NAME = ".index.pid"
CONSUME_PID_NAME = ".consume.pid"
MAINT_PID_NAME = ".maintenance.pid"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
MAINTENANCE_SCRIPT = os.path.join(SCRIPTS_DIR, "maintenance.py")

# memsearch CLI 进程内 bootstrap：给该进程的一切子孙 spawn（milvus_lite、
# maintenance 工具循环的 memsearch.exe/git 等）默认注入 CREATE_NO_WINDOW——
# 否则 PATH 上 console-subsystem 的 memsearch.exe/git.exe 会弹控制台黑窗。
# Popen 必须以子类替换（lambda 会炸掉 asyncio 的 class Popen(subprocess.Popen) 继承）。
_CLI_BOOTSTRAP = (
    "import os, subprocess, sys\n"
    "if os.name == 'nt':\n"
    "    _f = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
    "    _run = subprocess.run\n"
    "    def _run_nw(*a, **k):\n"
    "        k.setdefault('creationflags', _f)\n"
    "        return _run(*a, **k)\n"
    "    class _Popen(subprocess.Popen):\n"
    "        def __init__(self, *a, **k):\n"
    "            k.setdefault('creationflags', _f)\n"
    "            super().__init__(*a, **k)\n"
    "    subprocess.run = _run_nw\n"
    "    subprocess.Popen = _Popen\n"
    "sys.argv = ['memsearch'] + sys.argv[1:]\n"
    "from memsearch.cli import cli\n"
    "cli()\n"
)


def find_memsearch():
    """包可导入性判定（真值）。要 spawn 请用 memsearch_argv（含窗口抑制 bootstrap）。"""
    try:
        return bool(importlib.util.find_spec("memsearch"))
    except Exception:
        return False


def memsearch_argv(*extra):
    """memsearch CLI 完整 argv（含窗口抑制 bootstrap）；包不可导入返回 None。
    -c 模式下进程内 sys.argv[1:] 即 extra，bootstrap 重置为 ['memsearch', *extra]。"""
    try:
        if not importlib.util.find_spec("memsearch"):
            return None
    except Exception:
        return None
    return [sys.executable, "-c", _CLI_BOOTSTRAP, *extra]


def child_env(memsearch_dir):
    env = os.environ.copy()
    env["MEMSEARCH_NO_WATCH"] = "1"
    env["MEMSEARCH_DIR"] = memsearch_dir
    env["MEMSEARCH_DISABLE"] = "1"  # 防重入：消费链子进程不再触发本插件
    return env


def run(args, input_text=None, timeout=60, env=None):
    """同步跑后端子进程，返回 (rc, stdout)；异常/超时返回负码。绝不抛出。"""
    try:
        r = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode, r.stdout or ""
    except subprocess.TimeoutExpired:
        return -124, ""
    except Exception:
        return -1, ""


def git_toplevel(path):
    """沿 path 向上找 git 仓库根；不在仓库内或 git 不可用时返回 None。

    对齐上游 plugins/claude-code/hooks/common.sh:41-44 的 git 归一化：落在 git
    仓库内时上溯到仓库根，避免每-session/子目录各自生成一个 collection。
    """
    rc, out = run(["git", "-C", path, "rev-parse", "--show-toplevel"], timeout=5)
    if rc != 0:
        return None
    top = (out or "").strip()
    if top and os.path.isdir(top):
        return os.path.abspath(top)
    return None


def pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000 | 0x100000, False, pid)
            if not h:
                return ctypes.get_last_error() == 5  # 权限不足保守视为存活
            try:
                st = k32.WaitForSingleObject(h, 0)
                return True if st == 0xFFFFFFFF else st == 0x102
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(pidfile):
    try:
        with open(pidfile, encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def bg_run(pid_name, argv, memsearch_dir):
    """detached 子进程通用点火：pidfile 存活探针 skip-if-running（防抖）。"""
    pidfile = os.path.join(memsearch_dir, pid_name)
    if pid_alive(read_pid(pidfile)):
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    try:
        p = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
            close_fds=True,
        )
        write_text(pidfile, str(p.pid))
    except Exception:
        pass


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def append_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
