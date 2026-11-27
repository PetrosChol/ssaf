"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import permissions
from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text(encoding="utf-8-sig"))
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def _repo_path(claim: str, root: Path) -> str:
    """A claimed path as git names it: relative to the repo root, forward slashes."""
    path = Path(claim)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root.resolve())
        except ValueError:
            return claim                   # outside the repo: no diff can hold it
    return Path(os.path.normpath(path)).as_posix()


def _ignored(path: str, root: Path) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", "--", path], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.returncode == 0


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must actually have been changed by this run.

    Existing is not the same as changed: every file in the repo exists, so a
    builder that names a file it never touched would pass an existence check.
    A claim holds when the path is one the agent changed in this phase
    (created, edited, deleted, or committed), or one the run left changed in
    an earlier phase and that is still uncommitted. A path that is only the
    operator's own untouched uncommitted work does not count.

    Where git cannot answer (no repository, a gitignored path, or a gate called
    outside an agent phase) the check falls back to existence and says so.
    """
    report = GateReport()
    claims = list(getattr(envelope, "changed_files", []))
    if not claims:
        return report
    root = Path(run.repo_root)
    before = getattr(run, "tree_before", None)
    verifiable = before is not None and bool(getattr(before, "head", ""))
    if verifiable:
        after = permissions.snapshot(run, base=before.head)
        in_phase = set(permissions.changed_paths(before, after))
        operator = set(run.operator_paths())
    for claim in claims:
        path = _repo_path(claim, root)
        exists = (root / path).exists()
        if not verifiable or _ignored(path, root):
            why = "gitignored" if verifiable else "no git baseline"
            report.check(claim, exists,
                         f"exists, {_size(root / path)} ({why}: change not verifiable)" if exists
                         else "claimed changed file does not exist")
        elif path in in_phase:
            report.check(claim, True, "changed in this phase" if exists else "deleted in this phase")
        elif path in after and path not in operator:
            report.check(claim, True, "changed earlier in this run, still uncommitted")
        else:
            report.check(claim, False,
                         "claimed changed, but this run did not change it" if exists
                         else "claimed changed file does not exist and was not deleted by this run")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]

    report.check("approved vs blocking", not (approved and blocking),
                 "no blocking items" if not blocking
                 else f"{len(blocking)} blocking item(s) while approved=true"
                 if approved else f"{len(blocking)} blocking item(s), not approved")
    report.check("approved vs findings", not (approved and unmet),
                 "every requirement met" if not unmet
                 else f"{len(unmet)} unmet requirement(s) while approved=true"
                 if approved else f"{len(unmet)} unmet requirement(s), not approved")
    report.check("rejection names a problem", approved or bool(blocking or unmet),
                 "verdict is supported" if approved or blocking or unmet
                 else "approved=false but no blocking item or unmet requirement was given")
    return report


def tests_pass(command: str, timeout_seconds: int = 600):
    """Gate factory: the given shell command must exit 0, run from the repo root."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    cwd=run.repo_root, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return GateReport().check(command, False, f"timed out after {timeout_seconds}s")
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
