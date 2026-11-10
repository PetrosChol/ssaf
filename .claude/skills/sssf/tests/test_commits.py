"""Tests for run-scoped commits (PhaseHandle.commit) and the installer's --force.

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

from adw_modules.data_types import PhaseParams, SSSFConfig  # noqa: E402
from adw_modules.runner import Run  # noqa: E402
from adw_modules.tracer import Tracer  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("one\n")
    (root / "src" / "wip.py").write_text("committed\n")
    (root / ".gitignore").write_text("adws/adw_data/\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    monkeypatch.chdir(root)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    return root


def _run(adw_id: str = "c0000001") -> Run:
    cfg = SSSFConfig()
    tracer = Tracer(cfg.observability.db,
                    f"{cfg.defaults.data_dir}/sessions/{adw_id}/events.jsonl")
    tracer.session_start(adw_id, "t")
    return Run(cfg=cfg, adw_id=adw_id, tracer=tracer, engineer="t")


def _commit(run: Run, message: str = "agent work") -> str:
    with run.phase(PhaseParams(name=f"commit_{len(run.phases)}", kind="code", owner="git",
                               description="Land what this run changed")) as ph:
        return ph.commit(message)


def _committed(root: Path) -> list[str]:
    return sorted(_git(root, "show", "--name-only", "--format=", "HEAD").split())


def _dirty(root: Path) -> list[str]:
    return sorted(line[3:] for line in _git(root, "status", "--porcelain").splitlines())


def test_operators_uncommitted_work_stays_out_of_the_runs_commit(repo):
    (repo / "src" / "wip.py").write_text("half-finished\n")       # the operator's
    (repo / "scratch.txt").write_text("notes\n")
    run = _run()
    (repo / "src" / "app.py").write_text("one\ntwo\n")            # the run's
    (repo / "src" / "new.py").write_text("new\n")
    _commit(run)
    assert _committed(repo) == ["src/app.py", "src/new.py"]
    assert _dirty(repo) == ["scratch.txt", "src/wip.py"]
    assert (repo / "src" / "wip.py").read_text() == "half-finished\n"
    assert "left_uncommitted" in (repo / "adws/adw_data/sessions/c0000001/events.jsonl").read_text()


def test_a_dirty_file_the_run_changed_further_is_the_runs_work(repo):
    (repo / "src" / "wip.py").write_text("half-finished\n")
    run = _run()
    (repo / "src" / "wip.py").write_text("half-finished\nfinished by the agent\n")
    _commit(run)
    assert _committed(repo) == ["src/wip.py"] and _dirty(repo) == []


def test_only_operator_work_in_the_tree_is_nothing_to_commit(repo):
    (repo / "scratch.txt").write_text("notes\n")
    run = _run()
    with pytest.raises(RuntimeError, match="nothing to commit"):
        _commit(run)
    assert _dirty(repo) == ["scratch.txt"]


def test_a_joined_session_commits_what_an_earlier_adw_left(repo):
    """adw_plan writes a plan and commits nothing; a later ADW in the session must."""
    (repo / "scratch.txt").write_text("notes\n")
    first = _run("joined01")
    (repo / "plan.md").write_text("the plan\n")
    first.tracer.conn.close()

    second = _run("joined01")                # same session: the baseline is the first one's
    (repo / "src" / "app.py").write_text("built\n")
    _commit(second)
    assert _committed(repo) == ["plan.md", "src/app.py"]
    assert _dirty(repo) == ["scratch.txt"]


def test_clean_tree_commits_everything_and_logs_the_branch(repo):
    run = _run()
    (repo / "src" / "new.py").write_text("new\n")
    sha = _commit(run, "add new")
    assert _git(repo, "rev-parse", "--short", "HEAD").strip() == sha
    events = (repo / "adws/adw_data/sessions/c0000001/events.jsonl").read_text()
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() in events
    assert "left_uncommitted" not in events


# ── install.py --force ──────────────────────────────────────────────────────

def _install(root: Path, *args: str) -> str:
    return subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py"), *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout


def test_force_leaves_a_projects_own_justfile_alone(tmp_path):
    (tmp_path / "justfile").write_text("deploy:\n    ./deploy.sh\n")
    _install(tmp_path)
    output = _install(tmp_path, "--force")
    assert (tmp_path / "justfile").read_text() == "deploy:\n    ./deploy.sh\n"
    assert "kept (not an sssf file" in output


def test_force_refreshes_the_justfile_it_stamped(tmp_path):
    _install(tmp_path)
    stamped = (tmp_path / "justfile").read_text()
    (tmp_path / "justfile").write_text(stamped + "\n# local edit\n")
    _install(tmp_path, "--force")
    assert (tmp_path / "justfile").read_text() == stamped
