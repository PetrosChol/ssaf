"""Tests for the write boundary (adw_modules/permissions.py), against real git repos.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import permissions  # noqa: E402
from adw_modules.data_types import AgentConfig, PromptEngineering, SSSFConfig  # noqa: E402

PROTECTED = "adws/adw_modules/quality.py"


def _agent(writes) -> AgentConfig:
    return AgentConfig(name="a", model="m", purpose="p", writes=writes,
                       prompt_engineering=PromptEngineering(system="s.md", user="u.md"))


READ_ONLY = _agent([])
UNRESTRICTED = _agent(None)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "adws" / "adw_modules").mkdir(parents=True)
    (root / "src").mkdir()
    (root / PROTECTED).write_text("checks = 1\n")
    (root / "src" / "app.py").write_text("one\ntwo\n")
    (root / ".gitignore").write_text("adws/adw_data/\n")
    _sh(root, "git init -q && git add -A && git commit -qm init")
    return root


def _sh(root: Path, command: str) -> str:
    git = "git -c user.name=t -c user.email=t@t"
    return subprocess.run(command.replace("git ", git + " ", 1) if command.startswith("git ")
                          else command.replace("&& git ", f"&& {git} "),
                          shell=True, cwd=root, check=True, capture_output=True, text=True).stdout


def _run(root: Path) -> SimpleNamespace:
    cfg = SSSFConfig(defaults={"protected_files": ["adws/adw_modules/"]})
    return SimpleNamespace(repo_root=root, cfg=cfg)


def _status(root: Path) -> str:
    return _sh(root, "git status --porcelain").strip()


def test_permitted_change_is_reported_not_raised(repo):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / "src" / "app.py").write_text("changed\n")
    (repo / "src" / "new.py").write_text("new\n")
    assert permissions.enforce(run, None, UNRESTRICTED, before) == ["src/app.py", "src/new.py"]


def test_edit_outside_the_boundary_is_rolled_back(repo):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / "src" / "app.py").write_text("changed\n")
    with pytest.raises(permissions.PermissionBreach, match="src/app.py — rolled back"):
        permissions.enforce(run, None, READ_ONLY, before)
    assert (repo / "src" / "app.py").read_text() == "one\ntwo\n"
    assert _status(repo) == ""


def test_protected_file_is_closed_even_to_an_unrestricted_agent(repo):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / PROTECTED).write_text("checks = 0\n")
    with pytest.raises(permissions.PermissionBreach, match="rolled back"):
        permissions.enforce(run, None, UNRESTRICTED, before)
    assert (repo / PROTECTED).read_text() == "checks = 1\n"


def test_committing_a_change_does_not_hide_it(repo):
    """`git commit` empties the diff against the NEW head; the old head still sees it."""
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / PROTECTED).write_text("checks = 0\n")
    _sh(repo, "git commit -qam sneaky")
    with pytest.raises(permissions.PermissionBreach) as breach:
        permissions.enforce(run, None, UNRESTRICTED, before)
    assert f"{PROTECTED} — rolled back" in str(breach.value)
    assert "HEAD moved during this phase" in str(breach.value)
    assert (repo / PROTECTED).read_text() == "checks = 1\n"


def test_committing_a_permitted_change_is_still_seen(repo):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / "src" / "app.py").write_text("changed\n")
    _sh(repo, "git commit -qam fine")
    assert permissions.enforce(run, None, UNRESTRICTED, before) == ["src/app.py"]


def test_same_sized_edit_to_an_already_dirty_file_is_seen(repo):
    """numstat is +1 -0 before and after; only the content tells them apart."""
    run = _run(repo)
    target = repo / "src" / "app.py"
    target.write_text("one\ntwo\nmine\n")            # the operator's uncommitted work
    before = permissions.snapshot(run)
    target.write_text("one\ntwo\nEVIL\n")
    with pytest.raises(permissions.PermissionBreach, match="left as-is"):
        permissions.enforce(run, None, READ_ONLY, before)
    assert target.read_text() == "one\ntwo\nEVIL\n"     # not ours to reconstruct


def test_untouched_dirty_file_is_not_a_change(repo):
    run = _run(repo)
    (repo / "src" / "app.py").write_text("one\ntwo\nmine\n")
    (repo / "notes.txt").write_text("scratch\n")
    before = permissions.snapshot(run)
    assert permissions.enforce(run, None, READ_ONLY, before) == []


def test_reverting_uncommitted_work_is_a_breach(repo):
    run = _run(repo)
    (repo / "src" / "app.py").write_text("one\ntwo\nmine\n")
    before = permissions.snapshot(run)
    _sh(repo, "git checkout -- src/app.py")
    with pytest.raises(permissions.PermissionBreach, match="REVERTED-BY-AGENT"):
        permissions.enforce(run, None, READ_ONLY, before)


@pytest.mark.parametrize("stage", [False, True])
def test_new_file_is_removed_staged_or_not(repo, stage):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / "adws" / "adw_modules" / "extra.py").write_text("x = 1\n")
    if stage:
        _sh(repo, "git add adws/adw_modules/extra.py")
    with pytest.raises(permissions.PermissionBreach, match="extra.py — deleted"):
        permissions.enforce(run, None, UNRESTRICTED, before)
    assert not (repo / "adws" / "adw_modules" / "extra.py").exists()
    assert _status(repo) == ""


def test_staged_edit_and_deletion_are_rolled_back(repo):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / "src" / "app.py").write_text("changed\n")
    _sh(repo, "git add src/app.py && git rm -q adws/adw_modules/quality.py")
    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(run, None, READ_ONLY, before)
    assert (repo / "src" / "app.py").read_text() == "one\ntwo\n"
    assert (repo / PROTECTED).read_text() == "checks = 1\n"
    assert _status(repo) == ""


def test_session_runtime_is_always_writable(repo):
    run = _run(repo)
    _sh(repo, "rm .gitignore && git commit -qam unignore")     # not hanging on .gitignore
    before = permissions.snapshot(run)
    handoff = repo / "adws" / "adw_data" / "sessions" / "x" / "context_handoff"
    handoff.mkdir(parents=True)
    (handoff / "findings.md").write_text("found\n")
    assert permissions.enforce(run, None, READ_ONLY, before) == [
        "adws/adw_data/sessions/x/context_handoff/findings.md"]


def test_own_writes_entry_unlocks_a_protected_path(repo):
    run = _run(repo)
    before = permissions.snapshot(run)
    (repo / PROTECTED).write_text("checks = 2\n")
    assert permissions.enforce(run, None, _agent(["adws/adw_modules/"]), before) == [PROTECTED]


def test_glob_star_stops_at_a_directory():
    assert permissions._matches("adws/adw_x.py", "adws/adw_*.py")
    assert not permissions._matches("adws/adw_data/x.py", "adws/adw_*.py")
    assert permissions._matches("a/b/c.md", "**/*.md")
