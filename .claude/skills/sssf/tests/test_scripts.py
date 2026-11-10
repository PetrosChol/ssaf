"""Tests for the skill's scripts and the visualizer server's network exposure.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL / "scripts"
VISUALIZER = SKILL / "apps" / "visualizer"
sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules.runner import Run  # noqa: E402


def _dependencies(script: Path) -> set[str]:
    match = re.search(r"^# dependencies = (\[.*\])$", script.read_text(), re.M)
    assert match, f"{script.name} has no inline dependency list"
    return set(ast.literal_eval(match.group(1)))


@pytest.fixture
def generated(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([sys.executable, str(SCRIPTS / "install.py")], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run([sys.executable, str(SCRIPTS / "make_adw.py"),
                    "--name", "demo", "--agents", "scout,builder,scout"],
                   cwd=repo, check=True, capture_output=True)
    return repo / "adws" / "adw_demo.py"


def test_generated_adw_declares_what_the_modules_import(generated):
    """It runs under `uv run`, so a dependency missing here is an ImportError there."""
    stamped = generated.parent / "adw_scout.py"
    assert _dependencies(generated) == _dependencies(stamped)


def test_generated_adw_imports_and_parses_its_arguments(generated):
    result = subprocess.run([sys.executable, str(generated), "--help"],
                            cwd=generated.parent.parent, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--adw-id" in result.stdout


def test_generated_adw_only_calls_what_run_has(generated):
    """The generator once ended every ADW in `run.succeeded`, long after it was removed."""
    used = {node.attr for node in ast.walk(ast.parse(generated.read_text()))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "run"}
    instance_attrs = {"cfg", "adw_id", "tracer", "console", "engineer", "phases",
                      "repo_root", "session_dir", "context_handoff_dir", "agent_map"}
    missing = {name for name in used if not hasattr(Run, name)} - instance_attrs
    assert not missing, f"generated ADW uses attributes Run does not have: {missing}"
    assert "finish" in used


# ── a stamped repo and the project's own pytest ─────────────────────────────

def _plain_pytest(repo: Path) -> subprocess.CompletedProcess:
    """`pytest` from the repo root with no pytest config, like a fresh project."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=repo, env=env, capture_output=True, text=True, timeout=120)


def test_plain_pytest_in_a_stamped_repo_never_collects_adws(tmp_path):
    """pytest's default pattern includes `*_test.py`, which adw_build_test.py and
    adw_plan_build_test.py match. Collected, they fail to import (their deps live
    in their `uv run` block), so every project test run — the test phase too —
    errored before running a single test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run([sys.executable, str(SCRIPTS / "install.py")], cwd=repo,
                   check=True, capture_output=True)
    assert (repo / "adws" / "adw_build_test.py").is_file()       # the name that matched
    (repo / "tests").mkdir()
    (repo / "tests" / "test_project.py").write_text("def test_project():\n    assert True\n")
    # Stands in for the stamped *_test.py ADWs, whose import would happen to
    # succeed here: this environment has their dependencies installed.
    (repo / "adws" / "adw_probe_test.py").write_text(
        "def test_must_not_run():\n    raise AssertionError('adws/ was collected')\n")

    result = _plain_pytest(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout

    (repo / "adws" / "conftest.py").unlink()                    # without the fix
    assert _plain_pytest(repo).returncode != 0


def test_the_pytest_guard_is_protected_from_agents():
    """A conftest can hook into pytest, so it grades the test phase."""
    import yaml
    from adw_modules import permissions
    from adw_modules.data_types import AgentConfig, PromptEngineering, SSSFConfig

    cfg = SSSFConfig()
    builder = AgentConfig(name="builder",
                          prompt_engineering=PromptEngineering(system="s.md", user="u.md"))
    assert not permissions.permitted("adws/conftest.py", builder, cfg)
    assert permissions.permitted("tests/conftest.py", builder, cfg)
    stamped = yaml.safe_load((SKILL / "templates" / "sssf.config.yaml").read_text())
    assert stamped["defaults"]["protected_files"] == cfg.defaults.protected_files


# ── visualizer server ───────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _lan_address() -> str | None:
    """A non-loopback address of this machine, if it has one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))            # no packet is sent
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


@pytest.mark.skipif(not shutil.which("bun"), reason="bun is not installed")
def test_visualizer_listens_on_loopback_only(tmp_path):
    from adw_modules.tracer import Tracer
    db = tmp_path / "sssf.db"
    Tracer(db, tmp_path / "events.jsonl").conn.close()

    port = _free_port()
    server = subprocess.Popen(["bun", "run", "server/index.ts"], cwd=VISUALIZER,
                              env={**os.environ, "PORT": str(port), "SSSF_DB": str(db)},
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                            timeout=2) as response:
                    assert response.status == 200
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"server did not come up: {server.stdout.read()}")
                time.sleep(0.2)

        address = _lan_address()
        if address is None:
            pytest.skip("no non-loopback interface to probe")
        with pytest.raises(OSError):
            socket.create_connection((address, port), timeout=2).close()
    finally:
        server.terminate()
        server.wait(timeout=10)
