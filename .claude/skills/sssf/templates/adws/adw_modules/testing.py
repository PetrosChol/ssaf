"""Testing an existing file: the logic behind adw_test_plan and adw_test_write.

The workflow is split in two on purpose. A headless ADW cannot stop and ask, so
the engineer's confirmation is the gap between two runs of the same session:

    adw_test_plan.py <file>          test_planner analyses the file, proposes tests
        (the engineer reads, edits if they like, and approves)
    adw_test_write.py --adw-id <id>  tester writes them; code runs them; failures
                                     go back to the tester, bounded

Three rules make "never touch the original code" a fact rather than a request:

  * the tester's `writes:` in sssf.config.yaml is limited to test paths, and
    permissions.py rolls back anything else and fails the phase;
  * the file under test is snapshotted before the tester runs and compared
    after every tester phase, so even a target that happens to match a test
    glob cannot be edited (`guard_target`);
  * a failing test the tester believes is CORRECT is reported as a suspected
    bug and left failing. Rewriting a test until it agrees with the code would
    turn a found bug into a passing test that guarantees the bug stays.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import agents, permissions
from .data_types import (ApprovedPlan, GateReport, SSSFConfig, TargetSnapshot, TesterOutput,
                         TestPlanOutput)

PLAN_FILE = "test_plan.md"
PLAN_DIGEST = "test_plan.sha256"      # the planner's version, to tell if the engineer edited it
BUG_REPORT = "suspected_bugs.md"
PLANNER = "test_planner"
TESTER = "tester"


# ── the file under test ──────────────────────────────────────────────────────

def target_path(arg: str, root: Path | None = None) -> str:
    """The file to test, repo-relative and POSIX, or a SystemExit saying why not."""
    root = (root or Path.cwd()).resolve()
    path = Path(arg)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_file():
        raise SystemExit(f"not a file: {arg}")
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise SystemExit(f"{arg} is outside this repo ({root}) — run from the repo root")


def request_text(target: str, focus: str = "") -> str:
    """The request every agent of the workflow sees as {{prompt}}."""
    text = f"Write tests for the existing file `{target}`. Do not change that file."
    return f"{text}\n\nEngineer's focus: {focus.strip()}" if focus.strip() else text


def snapshot_target(run, target: str) -> TargetSnapshot:
    path = Path(run.repo_root) / target
    return TargetSnapshot(path=target, content=path.read_bytes() if path.is_file() else None)


def guard_target(run, snap: TargetSnapshot) -> None:
    """Restore the file under test and fail the phase if anything changed it."""
    path = Path(run.repo_root) / snap.path
    now = path.read_bytes() if path.is_file() else None
    if now == snap.content:
        return
    if snap.content is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(snap.content)
    raise permissions.PermissionBreach(
        f"the file under test, {snap.path}, was modified during a tester phase — "
        f"restored to its original content. Tests must be fixed in the tests, never in "
        f"the code they test.")


# ── the plan and its approval ────────────────────────────────────────────────

def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_path(run) -> Path:
    return Path(run.context_handoff_dir) / PLAN_FILE


def record_plan(run) -> str:
    """Remember the plan as the planner wrote it; returns its digest."""
    digest = _digest(plan_path(run))
    (Path(run.context_handoff_dir) / PLAN_DIGEST).write_text(digest + "\n", encoding="utf-8")
    return digest


def approved_plan(cfg: SSSFConfig, adw_id: str) -> ApprovedPlan:
    """Load the plan of session `adw_id`, or a SystemExit before anything starts.

    Checked BEFORE the session is joined: `session.ensure` creates a session
    that does not exist, and a mistyped id should cost nothing, not leave an
    empty session in the trace.
    """
    session = Path(cfg.defaults.data_dir) / "sessions" / adw_id
    plan = session / "context_handoff" / PLAN_FILE
    envelope = session / PLANNER / "envelope.json"
    if not (plan.is_file() and envelope.is_file()):
        raise SystemExit(f"session {adw_id} has no test plan ({plan}). Plan first:\n"
                         f"  uv run adws/adw_test_plan.py <file>")
    parsed = TestPlanOutput.model_validate(json.loads(envelope.read_text(encoding="utf-8-sig")))
    digest = _digest(plan)
    recorded = session / "context_handoff" / PLAN_DIGEST
    original = recorded.read_text(encoding="utf-8").strip() if recorded.is_file() else digest
    return ApprovedPlan(envelope=parsed, path=str(plan), sha256=digest, edited=digest != original)


# ── gates ────────────────────────────────────────────────────────────────────

def plan_complete(envelope: TestPlanOutput, run) -> GateReport:
    """The proposal must be something the engineer can approve and the tester can carry out."""
    report = GateReport()
    plan = plan_path(run)
    report.check(str(plan), plan.is_file() and plan.stat().st_size > 0,
                 "written" if plan.is_file() and plan.stat().st_size > 0
                 else f"write the plan to {plan}")
    report.check("cases", bool(envelope.cases),
                 f"{len(envelope.cases)} case(s)" if envelope.cases else "the plan names no test case")
    edge = [c for c in envelope.cases if c.kind in ("edge", "error")]
    report.check("edge cases", bool(edge),
                 f"{len(edge)} edge/error case(s)" if edge
                 else "no case has kind 'edge' or 'error' — plan at least one")
    report.check("test_files", bool(envelope.test_files),
                 ", ".join(envelope.test_files) or "name the test file(s) the tests will live in")
    tester = agents.resolve(run.cfg, TESTER)
    for path in envelope.test_files:
        ok = permissions.permitted(path, tester, run.cfg)
        report.check(path, ok, "the tester may write here" if ok else
                     f"the tester may not write here — its `writes:` allows {tester.writes}")
    return report


def tests_declared(envelope: TesterOutput, run) -> GateReport:
    """Every test file the tester names must exist and hold something."""
    report = GateReport()
    report.check("test_files", bool(envelope.test_files),
                 f"{len(envelope.test_files)} file(s)" if envelope.test_files
                 else "list every test file this run wrote in test_files")
    for name in envelope.test_files:
        path = Path(run.repo_root) / name
        ok = path.is_file() and path.stat().st_size > 0
        report.check(name, ok, "exists" if ok else "declared test file is missing or empty")
    return report


def failures_accounted_for(envelope: TesterOutput, run) -> GateReport:
    """After a red run, the tester must either have repaired a test or named a bug.

    Doing neither leaves the suite exactly as red as it was, with no reason
    given — the one outcome that is never acceptable.
    """
    report = GateReport()
    acted = bool(envelope.changed_files or envelope.suspected_bugs)
    report.check("triage", acted,
                 f"{len(envelope.changed_files)} file(s) repaired, "
                 f"{len(envelope.suspected_bugs)} suspected bug(s)" if acted else
                 "the suite failed, but you neither repaired a test nor reported a suspected bug")
    for bug in envelope.suspected_bugs:
        report.check(bug.test, bool(bug.evidence.strip()),
                     "evidence given" if bug.evidence.strip()
                     else "a suspected bug needs evidence: file:line, docstring, or plan entry")
    return report


# ── the outcome ──────────────────────────────────────────────────────────────

def write_bug_report(run, target: str, tester: TesterOutput, exhausted: bool) -> Path:
    """The engineer-facing record of a red suite. Deterministic: rendered from the envelope."""
    lines = [f"# Suspected bugs in `{target}`", "",
             f"Session `{run.adw_id}`. These tests fail, and the tester believes the TESTS are "
             f"right and the code is wrong. They were left failing on purpose and nothing was "
             f"committed. Confirm each one, then fix the code or correct the test.", ""]
    if exhausted:
        lines += ["> The repair loop ran out of attempts, so some failures below may still be "
                  "test mistakes rather than bugs. Read the last test log before trusting this list.", ""]
    for bug in tester.suspected_bugs:
        lines += [f"## `{bug.test}`", "", f"- **Observed:** {bug.observed}",
                  f"- **Expected:** {bug.expected}", f"- **Evidence:** {bug.evidence}", ""]
    if not tester.suspected_bugs:
        lines += ["No suspected bug was named. The failures are unexplained — read the last test log.", ""]
    path = Path(run.context_handoff_dir) / BUG_REPORT
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
