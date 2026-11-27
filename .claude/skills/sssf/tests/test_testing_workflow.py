"""Tests for the testing workflow: adw_test_plan -> engineer approval -> adw_test_write.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SKILL = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_tester.py"
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import agents, permissions, quality, testing  # noqa: E402
from adw_modules.data_types import PlannedCase, TestPlanOutput, TestRequest  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]

CALC = '''def add(a, b):
    return a + b


def divide(a, b):
    """Divide a by b. Raises ZeroDivisionError when b is 0."""
{body}
'''
CORRECT = "    return a / b"
BUGGY = "    if b == 0:\n        return 0\n    return a / b"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(GIT + list(args), cwd=root, check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A stamped repo with a real pytest test command and fake Claude Code agents."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")], cwd=root,
                   check=True, capture_output=True)
    config_path = root / "adws" / "adw_sssf_config" / "sssf.config.yaml"
    config = yaml.safe_load(config_path.read_text())
    for agent in config["agents"]:
        if agent["name"] in ("test_planner", "tester"):
            agent.update(coding_agent="claude_code", model="anthropic/haiku", harness_engineering=[])
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    quality_py = root / "adws" / "adw_modules" / "quality.py"
    source = quality_py.read_text()
    placeholder = 'argv=_placeholder("test"),'
    assert placeholder in source
    # -B: a repair can rewrite a test with the same size in the same second, and
    # pytest's rewritten-bytecode cache is keyed on exactly that.
    argv = [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    quality_py.write_text(source.replace(placeholder, f"argv={argv!r},"))
    (root / "src").mkdir()
    (root / "src" / "calc.py").write_text(CALC.format(body=CORRECT))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _env(scenario: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return {**env, "CLAUDE_PATH": str(FAKE), "FAKE_TESTER_SCENARIO": scenario}


def _run(root: Path, scenario: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True,
                          timeout=180, env=_env(scenario))


def _plan(root: Path, scenario: str, adw_id: str, folder: str = "tests") -> subprocess.CompletedProcess:
    result = _run(root, scenario, "adws/adw_test_plan.py", "src/calc.py", folder, "--adw-id", adw_id)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _write(root: Path, scenario: str, adw_id: str) -> subprocess.CompletedProcess:
    return _run(root, scenario, "adws/adw_test_write.py", "--adw-id", adw_id)


def _db(root: Path) -> sqlite3.Connection:
    return sqlite3.connect(root / "adws" / "adw_data" / "sssf.db")


def _phases(root: Path, adw_id: str) -> list[tuple[str, str]]:
    return list(_db(root).execute(
        "select name, status from phases where adw_id=? order by seq", (adw_id,)))


def _handoff(root: Path, adw_id: str) -> Path:
    return root / "adws" / "adw_data" / "sessions" / adw_id / "context_handoff"


# ── end to end ───────────────────────────────────────────────────────────────

def test_plan_changes_nothing_and_stops_for_approval(repo):
    result = _plan(repo, "green", "plan0001")
    assert (_handoff(repo, "plan0001") / "test_plan.md").is_file()
    assert "adw_test_write.py --adw-id plan0001" in result.stdout
    assert _git(repo, "status", "--porcelain") == ""             # nothing written yet
    assert [name for name, _ in _phases(repo, "plan0001")] == ["request", "plan", "present"]


def test_a_wrong_test_is_repaired_and_the_tests_are_committed(repo):
    _plan(repo, "broken_test", "fix00001")
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = _write(repo, "broken_test", "fix00001")
    assert result.returncode == 0, result.stdout + result.stderr
    assert [name for name, _ in _phases(repo, "fix00001")][3:] == [
        "approve", "write", "test_1", "triage_1", "test_2", "commit"]
    committed = _git(repo, "diff", "--name-only", head, "HEAD").split()
    assert committed == ["tests/test_calc.py"]                     # tests only, never the code
    assert (repo / "src" / "calc.py").read_text() == CALC.format(body=CORRECT)


def test_any_folder_the_engineer_names_is_where_the_tests_land(repo):
    """Not one of the config's test globs — the named folder is the permission."""
    _plan(repo, "green", "qa000001", folder="qa/checks")
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = _write(repo, "green", "qa000001")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "diff", "--name-only", head, "HEAD").split() == ["qa/checks/test_calc.py"]


def test_a_test_file_outside_the_folder_is_rolled_back(repo):
    """test_stray.py matches the config's globs, but it is not in the named folder."""
    _plan(repo, "stray", "stray001")
    result = _write(repo, "stray", "stray001")
    assert result.returncode != 0
    assert ("write", "fail") in _phases(repo, "stray001")
    assert not (repo / "test_stray.py").exists()


def test_a_real_bug_is_reported_not_hidden(repo):
    (repo / "src" / "calc.py").write_text(CALC.format(body=BUGGY))
    _git(repo, "commit", "-qam", "introduce a bug")
    _plan(repo, "bug", "bug00001")
    head = _git(repo, "rev-parse", "HEAD").strip()

    result = _write(repo, "bug", "bug00001")
    assert result.returncode == 1
    assert [name for name, _ in _phases(repo, "bug00001")][3:] == [
        "approve", "write", "test_1", "triage_1", "report"]       # stopped: triage changed nothing
    report = (_handoff(repo, "bug00001") / "suspected_bugs.md").read_text()
    assert "test_divide_by_zero_raises" in report and "src/calc.py:6" in report
    assert _git(repo, "rev-parse", "HEAD").strip() == head          # red is never committed
    assert "pytest.raises(ZeroDivisionError)" in (repo / "tests" / "test_calc.py").read_text()
    assert (repo / "src" / "calc.py").read_text() == CALC.format(body=BUGGY)
    assert _db(repo).execute("select status from sessions where adw_id='bug00001'"
                             ).fetchone() == ("fail",)


def test_editing_the_code_under_test_is_rolled_back_and_fails(repo):
    _plan(repo, "touch_source", "touch001")
    result = _write(repo, "touch_source", "touch001")
    assert result.returncode != 0
    assert ("write", "fail") in _phases(repo, "touch001")
    assert (repo / "src" / "calc.py").read_text() == CALC.format(body=CORRECT)


def test_a_triage_that_neither_repairs_nor_reports_fails_its_gate(repo):
    (repo / "src" / "calc.py").write_text(CALC.format(body=BUGGY))
    _git(repo, "commit", "-qam", "introduce a bug")
    _plan(repo, "weaken", "gate0001")
    result = _write(repo, "weaken", "gate0001")
    assert result.returncode != 0
    assert ("triage_1", "fail") in _phases(repo, "gate0001")
    assert "neither repaired a test nor reported" in result.stdout + result.stderr


def test_the_engineers_edit_to_the_plan_is_recorded(repo):
    _plan(repo, "green", "edit0001")
    plan = _handoff(repo, "edit0001") / "test_plan.md"
    plan.write_text(plan.read_text() + "\n- also test add(-1, 1)\n")
    result = _write(repo, "green", "edit0001")
    assert result.returncode == 0, result.stdout + result.stderr
    payloads = [json.loads(p) for (p,) in _db(repo).execute(
        "select payload_json from events where adw_id='edit0001' and type='log' and name='approve'")]
    assert any(p.get("edited_by_engineer") is True for p in payloads)


def test_write_without_a_plan_starts_nothing(repo):
    result = _write(repo, "green", "nope0001")
    assert result.returncode != 0 and "has no test plan" in result.stderr
    assert not (repo / "adws" / "adw_data" / "sessions" / "nope0001").exists()


def test_write_refuses_the_placeholder_test_command(repo):
    _plan(repo, "green", "hold0001")
    quality_py = repo / "adws" / "adw_modules" / "quality.py"
    quality_py.write_text((SKILL / "templates" / "adws" / "adw_modules" / "quality.py").read_text())
    result = _write(repo, "green", "hold0001")
    assert result.returncode != 0 and "placeholder" in result.stderr
    assert not (repo / "tests").exists()


# ── units ────────────────────────────────────────────────────────────────────

def _stamped_config():
    return agents.load_config(str(SKILL / "templates" / "sssf.config.yaml"))


def test_the_tester_cannot_write_the_factory_or_source():
    cfg = _stamped_config()
    tester = agents.resolve(cfg, "tester")
    factory = [p.relative_to(SKILL / "templates").as_posix() for p in
               (SKILL / "templates" / "adws").rglob("*") if p.is_file()]
    assert factory and not [p for p in factory if permissions.permitted(p, tester, cfg)]
    for path in ("src/calc.py", "app/main.ts", "pyproject.toml", "README.md"):
        assert not permissions.permitted(path, tester, cfg), path
    for path in ("tests/test_calc.py", "src/test_calc.py", "web/app.test.ts",
                 "pkg/calc_test.go", "src/__tests__/calc.js"):
        assert permissions.permitted(path, tester, cfg), path
    assert agents.resolve(cfg, "test_planner").writes == []


def test_the_plan_gate_checks_cases_and_the_tests_folder(tmp_path):
    (tmp_path / "test_plan.md").write_text("# plan\n")
    run = SimpleNamespace(cfg=_stamped_config(), context_handoff_dir=tmp_path)
    testing.record_request(run, TestRequest(target="src/calc.py", tests_dir="tests"))
    good = TestPlanOutput(status="success", test_files=["tests/test_calc.py"], cases=[
        PlannedCase(name="a"), PlannedCase(name="b", kind="edge")])
    assert testing.plan_complete(good, run).passed
    bad = good.model_copy(update={"test_files": ["src/calc.py"],
                                  "cases": [PlannedCase(name="a")]})
    violations = " ".join(testing.plan_complete(bad, run).violations)
    assert "outside the tests folder tests/" in violations and "no case has kind" in violations


def test_target_path_is_repo_relative_and_must_exist(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x = 1\n")
    assert testing.target_path("src/calc.py", tmp_path) == "src/calc.py"
    assert testing.target_path(str(tmp_path / "src" / "calc.py"), tmp_path) == "src/calc.py"
    with pytest.raises(SystemExit, match="not a file"):
        testing.target_path("src/missing.py", tmp_path)


def test_the_stamped_test_command_is_known_to_be_a_placeholder():
    assert not quality.test_configured()
    assert quality._test_spec(["tests/a.py", "tests/b.py"]).argv[-2:] == ["tests/a.py", "tests/b.py"]


def test_the_target_guard_restores_a_file_even_on_a_test_path(tmp_path):
    """A helper under tests/ is writable by the tester's globs, but never when it is the target."""
    (tmp_path / "tests").mkdir()
    helper = tmp_path / "tests" / "helpers.py"
    helper.write_text("ORIGINAL = True\n")
    run = SimpleNamespace(repo_root=tmp_path)
    snap = testing.snapshot_target(run, "tests/helpers.py")
    testing.guard_target(run, snap)                                  # untouched: no-op
    helper.write_text("ORIGINAL = False\n")
    with pytest.raises(permissions.PermissionBreach, match="file under test"):
        testing.guard_target(run, snap)
    assert helper.read_text() == "ORIGINAL = True\n"


@pytest.mark.parametrize("folder, why", [
    (".", "repo root"),
    ("src", "contains the file under test"),
    ("adws", "overlaps"),
    ("adws/adw_modules/tests", "overlaps"),
    ("adws/adw_data/tests", "overlaps"),
    ("../elsewhere", "outside this repo"),
])
def test_a_tests_folder_that_would_grant_more_than_tests_is_refused(tmp_path, folder, why):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("x = 1\n")
    with pytest.raises(SystemExit, match=why):
        testing.tests_dir_path(folder, "src/calc.py", _stamped_config(), tmp_path)


def test_a_tests_folder_may_be_new_and_nested(tmp_path):
    cfg = _stamped_config()
    assert testing.tests_dir_path("qa/unit/", "src/calc.py", cfg, tmp_path) == "qa/unit"
    assert testing.tests_dir_path("tests", "src/calc.py", cfg, tmp_path) == "tests"
    testing.narrow_tester(cfg, "qa/unit")
    tester = agents.resolve(cfg, "tester")
    assert permissions.permitted("qa/unit/test_calc.py", tester, cfg)
    assert not permissions.permitted("tests/test_calc.py", tester, cfg)
    assert not permissions.permitted("test_calc.py", tester, cfg)
