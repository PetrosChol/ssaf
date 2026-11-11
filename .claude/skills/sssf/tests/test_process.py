"""Tests for agent child-process handling (adw_modules/process.py) and the fix loops.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

SKILL = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude.py"
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import agent_cc, process  # noqa: E402
from adw_modules.data_types import PiRequest  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of a still-living parent lingers as a zombie until reaped.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][0] != "Z"
    except OSError:
        return True


def _gone(pid: int, within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_megabyte_of_stderr_does_not_deadlock():
    """The pipe holds 64KB. Unread, the child blocks on it and never reaches stdout."""
    lines = []
    code, stderr = process.stream(
        _python("import sys; sys.stderr.write('e' * 1_000_000 + 'THE END'); "
                "sys.stderr.flush(); print('made it')"),
        ".", dict(os.environ), lines.append, timeout_s=30)
    assert code == 0 and lines == ["made it\n"]
    assert stderr.endswith("THE END") and len(stderr) <= process.STDERR_TAIL_CHARS


def test_timeout_kills_the_child_and_what_it_started(tmp_path):
    pids = tmp_path / "pids"
    code = ("import os, subprocess, time; c = subprocess.Popen(['sleep', '600']); "
            f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{c.pid}}'); "
            "print('started', flush=True); time.sleep(600)")
    started = time.monotonic()
    with pytest.raises(process.AgentTimeout, match="killed after 1s"):
        process.stream(_python(code), ".", dict(os.environ), lambda line: None, timeout_s=1)
    assert time.monotonic() - started < 15
    parent, child = (int(pid) for pid in pids.read_text().split())
    assert _gone(parent) and _gone(child)


def test_leaving_the_read_loop_any_other_way_kills_the_child_too(tmp_path):
    seen = {}
    exited = []

    class Boom(Exception):
        pass

    def on_line(line):
        raise Boom

    with pytest.raises(Boom):
        process.stream(_python("import time; print('x', flush=True); time.sleep(600)"),
                       ".", dict(os.environ), on_line,
                       on_spawn=lambda pid: seen.setdefault("pid", pid),
                       on_exit=exited.append)
    assert _gone(seen["pid"]) and exited == [seen["pid"]]


def test_no_deadline_by_default():
    code, _ = process.stream(_python("print('ok')"), ".", dict(os.environ), lambda line: None)
    assert code == 0


def test_claude_code_backend_honours_the_request_timeout(tmp_path, monkeypatch):
    pids = tmp_path / "pids"
    monkeypatch.setattr(agent_cc, "CLAUDE_PATH", str(FAKE_CLAUDE))
    monkeypatch.setenv("FAKE_CLAUDE_HANG", str(pids))
    request = PiRequest(prompt="p", system_prompt="s", model="anthropic/haiku",
                        session_id="11111111-2222-3333-4444-555555555555",
                        session_dir=str(tmp_path / "s"), cwd=str(tmp_path),
                        raw_output_path=str(tmp_path / "raw.jsonl"), timeout_s=2)
    with pytest.raises(process.AgentTimeout):
        agent_cc.run(request)
    assert all(_gone(int(pid)) for pid in pids.read_text().split())


# ── through a stamped repo ──────────────────────────────────────────────────

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


@pytest.fixture
def repo(tmp_path):
    """A stamped repo whose scout and builder are Claude Code agents (the fake)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")], cwd=root,
                   check=True, capture_output=True)
    config_path = root / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    config = yaml.safe_load(config_path.read_text())
    for agent in config["agents"]:
        if agent["name"] in ("scout", "builder"):
            agent.update(coding_agent="claude_code", model="anthropic/haiku",
                         harness_engineering=[],
                         tools=[t for t in agent["tools"]
                                if not t.startswith(("subagent_", "mcp__"))])
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    subprocess.run(GIT + ["add", "-A"], cwd=root, check=True)
    subprocess.run(GIT + ["commit", "-qm", "init"], cwd=root, check=True)
    return root


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "adws" / "adw_data" / "sssf.db")


def test_killing_the_adw_takes_its_agent_down_with_it(repo, tmp_path):
    pids = tmp_path / "pids"
    adw = subprocess.Popen([sys.executable, "adws/adw_prompt.py", "look", "--agent", "scout",
                            "--adw-id", "kill0001"], cwd=repo,
                           env={**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE),
                                "FAKE_CLAUDE_HANG": str(pids)},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 60
        while not (pids.exists() and len(pids.read_text().split()) == 2):
            assert adw.poll() is None and time.monotonic() < deadline, "agent never started"
            time.sleep(0.1)
        agent, tool = (int(pid) for pid in pids.read_text().split())
        adw.send_signal(signal.SIGTERM)
        assert adw.wait(timeout=30) == 128 + signal.SIGTERM
    finally:
        adw.kill()
    assert _gone(agent) and _gone(tool)
    db = _db(repo)
    assert db.execute("select status from sessions where adw_id='kill0001'").fetchone() == ("fail",)
    assert db.execute("select count(*) from processes where adw_id='kill0001' "
                      "and ended_at is null").fetchone() == (0,)


def _suite_that_passes_on_run(root: Path, number: int) -> None:
    """Point quality.test at a command that fails until its Nth invocation."""
    counter = root / "adws" / "adw_data" / "suite_runs"
    script = ("import pathlib, sys; p = pathlib.Path(sys.argv[1]); "
              "n = int(p.read_text()) + 1 if p.exists() else 1; p.write_text(str(n)); "
              "sys.exit(0 if n >= int(sys.argv[2]) else 1)")
    quality = root / "adws" / "adw_modules" / "quality.py"
    source = quality.read_text()
    placeholder = 'argv=_placeholder("test"),'
    assert placeholder in source
    quality.write_text(source.replace(
        placeholder, f"argv={[sys.executable, '-c', script, str(counter), str(number)]!r},"))
    subprocess.run(GIT + ["commit", "-qam", "wire the suite"], cwd=root, check=True)


def _phases(root: Path, adw_id: str) -> list[str]:
    return [name for (name,) in _db(root).execute(
        "select name from phases where adw_id=? order by seq", (adw_id,))]


def _build_test(root: Path, adw_id: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "adws/adw_build_test.py", "make it pass",
                           "--adw-id", adw_id], cwd=root, capture_output=True, text=True,
                          timeout=180, env={**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE)})


def test_the_last_fix_is_tested(repo):
    """Green only on the 4th run of the suite: three fixes, and the third one counts."""
    _suite_that_passes_on_run(repo, 4)
    result = _build_test(repo, "loop0001")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _phases(repo, "loop0001") == ["request", "build", "test_1", "fix_1", "test_2",
                                         "fix_2", "test_3", "fix_3", "test_4"]


def test_a_suite_that_never_passes_ends_on_a_test_not_a_fix(repo):
    _suite_that_passes_on_run(repo, 99)
    result = _build_test(repo, "loop0002")
    assert result.returncode == 1
    phases = _phases(repo, "loop0002")
    assert phases[-1] == "test_4" and phases.count("fix_1") == 1 and "fix_4" not in phases
    assert _db(repo).execute("select status from sessions where adw_id='loop0002'"
                             ).fetchone() == ("fail",)


def test_an_unwired_suite_stops_the_loop_and_fails_the_run(repo):
    """A fresh install has no test command: no fix is attempted, nothing is accepted."""
    result = _build_test(repo, "loop0003")
    assert result.returncode == 1
    assert "nothing was verified" in result.stdout + result.stderr
    assert _phases(repo, "loop0003") == ["request", "build", "test_1"]
    assert _db(repo).execute("select status from sessions where adw_id='loop0003'"
                             ).fetchone() == ("fail",)


@pytest.mark.parametrize("name", ["adw_build_test.py", "adw_plan_build_test.py",
                                  "adw_plan_build_test_quality.py", "adw_simple_sdlc.py"])
def test_every_fix_loop_runs_one_more_check_than_fixes(name):
    source = (SKILL / "templates" / "adws" / name).read_text()
    assert "for i in range(1, MAX_FIX_LOOPS + 2):" in source
    assert "i > MAX_FIX_LOOPS:" in source
    assert ".unwired or i > MAX_FIX_LOOPS:" in source
