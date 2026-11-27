"""Tests for the claim gates (adw_modules/gates.py) and the unwired quality blocks.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import gates, permissions, process, quality  # noqa: E402
from adw_modules.data_types import (BuildOutput, PhaseParams, QualityCheckSpec,  # noqa: E402
                                    SSSFConfig)
from adw_modules.runner import Run  # noqa: E402
from adw_modules.tracer import Tracer  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    for name in ("app.py", "other.py", "old.py", "wip.py"):
        (root / "src" / name).write_text("committed\n")
    (root / ".gitignore").write_text("adws/adw_data/\nbuild/\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.chdir(root)
    return root


def _run(adw_id: str = "g0000001") -> Run:
    cfg = SSSFConfig()
    tracer = Tracer(cfg.observability.db,
                    f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl")
    tracer.session_start(adw_id, "t")
    return Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="t")


def _claims(run: Run, *files: str) -> dict[str, bool]:
    report = gates.diff_matches_claims(
        BuildOutput(status="success", changed_files=list(files)), run)
    return {check.item: check.ok for check in report.checks}


# ── diff_matches_claims ─────────────────────────────────────────────────────

def test_a_file_that_merely_exists_is_not_a_changed_file(repo):
    run = _run()
    run.tree_before = permissions.snapshot(run)          # the agent phase starts
    (repo / "src" / "app.py").write_text("edited\n")
    assert _claims(run, "src/app.py", "src/other.py") == {"src/app.py": True,
                                                          "src/other.py": False}


def test_created_deleted_and_committed_changes_all_count(repo):
    run = _run()
    run.tree_before = permissions.snapshot(run)
    (repo / "src" / "new.py").write_text("new\n")
    (repo / "src" / "old.py").unlink()
    (repo / "src" / "app.py").write_text("edited\n")
    _git(repo, "commit", "-qam", "the agent committed its own edit")
    assert _claims(run, "src/new.py", "src/old.py", "src/app.py") == {
        "src/new.py": True, "src/old.py": True, "src/app.py": True}


def test_a_missing_file_nobody_deleted_fails(repo):
    run = _run()
    run.tree_before = permissions.snapshot(run)
    assert _claims(run, "src/never_existed.py") == {"src/never_existed.py": False}


def test_work_from_an_earlier_phase_of_the_run_still_counts(repo):
    run = _run()
    (repo / "src" / "app.py").write_text("built in the build phase\n")
    run.tree_before = permissions.snapshot(run)          # a later fix phase starts
    (repo / "src" / "other.py").write_text("fixed\n")
    assert _claims(run, "src/app.py", "src/other.py") == {"src/app.py": True,
                                                          "src/other.py": True}


def test_the_operators_untouched_work_is_not_the_agents_change(repo):
    (repo / "src" / "wip.py").write_text("the operator's half-finished edit\n")
    run = _run()                                         # session baseline holds wip.py
    run.tree_before = permissions.snapshot(run)
    assert _claims(run, "src/wip.py") == {"src/wip.py": False}
    (repo / "src" / "wip.py").write_text("the agent changed it further\n")
    assert _claims(run, "src/wip.py") == {"src/wip.py": True}


def test_absolute_and_dotted_paths_are_read_as_repo_paths(repo):
    run = _run()
    run.tree_before = permissions.snapshot(run)
    (repo / "src" / "app.py").write_text("edited\n")
    absolute = str(repo / "src" / "app.py")
    assert _claims(run, absolute, "./src/app.py") == {absolute: True, "./src/app.py": True}


def test_what_git_cannot_see_falls_back_to_existence(repo):
    run = _run()
    assert _claims(run, "src/other.py") == {"src/other.py": True}     # no phase baseline
    run.tree_before = permissions.snapshot(run)
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("x\n")
    assert _claims(run, "build/out.js", "build/absent.js") == {      # gitignored
        "build/out.js": True, "build/absent.js": False}


def test_tests_pass_runs_from_the_repo_root_and_has_a_deadline(repo, monkeypatch):
    run = _run()
    monkeypatch.chdir(repo / "src")
    python = f'"{sys.executable}"'
    here = gates.tests_pass(f'{python} -c "import os, sys; sys.exit(not os.path.isdir(\'src\'))"')
    assert here(None, run).passed
    slow = gates.tests_pass(f'{python} -c "import time; time.sleep(30)"', timeout_seconds=1)
    report = slow(None, run)
    assert not report.passed and "timed out after 1s" in report.checks[0].note


# ── unwired quality blocks ──────────────────────────────────────────────────

def test_a_placeholder_block_never_passes_and_the_run_is_not_accepted(repo):
    run = _run()
    with run.phase(PhaseParams(name="test_1", kind="code", owner="quality",
                               description="Run the suite")):
        result = quality.run_tests(run)
    assert not result.passed and result.unwired == ["test"]
    assert result.checks[0].placeholder and "quality.py" in result.failures[0]
    assert "NOT RUN" in Path(result.artifacts[0]).read_text()
    assert run.phases[-1].status == "success"            # the phase did its job
    assert run.finish(accepted=True) == 1                # the run verified nothing
    status, = run.tracer.conn.execute(
        "select status from sessions where adw_id=?", (run.adw_id,)).fetchone()
    assert status == "fail"


def test_a_wired_block_runs_and_is_accepted(repo, monkeypatch):
    run = _run()
    spec = QualityCheckSpec(name="test", area="backend", operation="build",
                            argv=[sys.executable, "-c", "print('1 passed')"])
    monkeypatch.setattr(quality, "test", lambda run: quality._run(spec, run))
    with run.phase(PhaseParams(name="test_1", kind="code", owner="quality",
                               description="Run the suite")):
        result = quality.run_tests(run)
    assert result.passed and result.unwired == [] and run.unwired == []
    assert "1 passed" in result.checks[0].output_tail
    assert run.finish(accepted=True) == 0


def test_only_the_shipped_echo_counts_as_a_placeholder():
    assert quality.is_placeholder(quality._placeholder("lint"))
    assert not quality.is_placeholder(["echo", "hello"])
    assert not quality.is_placeholder(["uv", "run", "pytest", "-q"])


# ── killing the agent's process tree on Windows ─────────────────────────────

def test_windows_kills_the_whole_tree_with_taskkill(monkeypatch):
    calls = []

    class Child:
        pid = 4242
        killed = False

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 1

        def kill(self):
            self.killed = True

    monkeypatch.setattr(process, "WINDOWS", True)
    monkeypatch.setattr(process.subprocess, "run",
                        lambda argv, **kwargs: calls.append(argv))
    child = Child()
    process.kill_tree(child)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]] and not child.killed
