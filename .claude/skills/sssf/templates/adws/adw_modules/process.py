"""Running a coding agent as a child process, without the three ways that hangs.

Both backends (agent_pi, agent_cc) tail a child's JSONL stdout line by line.
Done naively that has three failure modes, and each one is silent:

  * stderr deadlock. With stderr on a pipe that is only read after stdout
    closes, a child that writes more than the pipe holds (64KB on Linux) blocks
    forever on that write — while the parent blocks forever reading stdout.
    Here a thread drains stderr the whole time and keeps its tail.
  * no deadline. A child that stops producing output is indistinguishable from
    one that is thinking. `timeout_s` puts a ceiling on one agent send; when it
    passes, the child is killed and `AgentTimeout` fails the phase.
  * orphans. `kill <adw pid>` (or Ctrl-C) ends the ADW, not the agent it
    spawned, which keeps running — and spending — with nobody reading it. The
    child gets its own process group, and whatever way the read loop is left
    (timeout, signal, exception) the whole group is killed on the way out, so
    the shell commands the agent started die with it. On Windows, where there
    is no group to signal, `taskkill /T /F` ends the same tree.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections import deque
from typing import Callable, Optional

STDERR_TAIL_CHARS = 8_000        # what a failure message can quote
KILL_GRACE_S = 5.0               # SIGTERM first; SIGKILL if it is still there


class AgentTimeout(RuntimeError):
    """One agent send ran past its `timeout_s` and was killed."""


WINDOWS = os.name == "nt"


def _spawn_options() -> dict:
    """Give the child a group of its own, so it can be stopped as a unit.

    POSIX: a new session, whose process group `os.killpg` reaches. Windows has
    no such group to signal; the new process group only keeps the operator's
    Ctrl-C from reaching the child directly, so the ADW decides how it ends.
    """
    if WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _kill_tree_windows(process: subprocess.Popen) -> None:
    """`taskkill /T` walks the parent links and ends every descendant.

    `process.terminate()` alone ends only the direct child: the shell commands
    an agent started would keep running, and spending, with nobody reading them.
    """
    try:
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=KILL_GRACE_S)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        process.kill()                           # taskkill missing or refused


def kill_tree(process: subprocess.Popen) -> None:
    """Stop the child and everything it started. Safe to call on a dead one."""
    if process.poll() is not None:
        return
    if WINDOWS:
        _kill_tree_windows(process)
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)          # its own group: start_new_session
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=KILL_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def stream(cmd: list[str], cwd: str, env: dict[str, str],
           on_line: Callable[[str], None],
           on_spawn: Optional[Callable[[int], None]] = None,
           on_exit: Optional[Callable[[int], None]] = None,
           timeout_s: float = 0) -> tuple[int, str]:
    """Run `cmd`, hand each stdout line to `on_line`, return (returncode, stderr tail).

    stdin is DEVNULL, deliberately: the prompt travels in argv, and a child that
    inherits a non-TTY stdin can sit forever waiting for input that never comes.
    `timeout_s` of 0 (or less) means no deadline.
    """
    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=cwd, env=env,
                               encoding="utf-8", errors="replace",
                               **_spawn_options())
    if on_spawn:
        on_spawn(process.pid)

    tail: deque[str] = deque()
    size = 0

    def drain() -> None:
        nonlocal size
        assert process.stderr is not None
        for chunk in process.stderr:
            tail.append(chunk)
            size += len(chunk)
            while size > STDERR_TAIL_CHARS and len(tail) > 1:
                size -= len(tail.popleft())

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()

    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        kill_tree(process)

    timer = threading.Timer(timeout_s, expire) if timeout_s and timeout_s > 0 else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            on_line(line)
        returncode = process.wait()
    finally:
        if timer:
            timer.cancel()
        kill_tree(process)                       # no-op when it exited by itself
        drainer.join(timeout=KILL_GRACE_S)
        if on_exit:
            on_exit(process.pid)
    stderr = "".join(tail)[-STDERR_TAIL_CHARS:]
    if timed_out.is_set():
        raise AgentTimeout(f"{os.path.basename(cmd[0])} was killed after {timeout_s:g}s "
                           f"without finishing (timeout_s)"
                           f"{' — ' + stderr.strip()[-400:] if stderr.strip() else ''}")
    return returncode, stderr
