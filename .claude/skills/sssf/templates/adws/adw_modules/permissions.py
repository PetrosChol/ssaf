"""What an agent may CHANGE, enforced in code after the fact.

`tools:` is a capability list, not a sandbox, and two holes make it
unenforceable on its own:

  * `bash` runs anything. A builder handed bash to run a test suite can also
    run `git checkout adws/` — which is not hypothetical: one did, discarding
    uncommitted changes to the very quality check it was about to be judged by.
  * `write` reaches any path, not just the one report file an agent was given
    it for. A reviewer configured with "no edit, so it cannot quietly fix"
    could still rewrite the code it was reviewing.

So permission is verified the way every other claim in this system is —
after the fact, against the repo itself. `snapshot()` fingerprints the working
tree's change-set before an agent runs; `enforce()` compares it afterwards and
fails the phase if the agent touched anything outside its allowlist.

Comparing change-sets, rather than watching for writes, is what catches the
`git checkout` case: a path that was modified before the agent ran and is clean
afterwards has been reverted, and a reversion is a modification. Appearing,
disappearing, and changing all count.

Both snapshots are measured against the commit the phase STARTED on, and each
path is fingerprinted by content. So neither `git commit` (which empties a diff
against the new HEAD) nor a same-sized edit to an already-dirty file hides a
change.

A breach is NOT a gate violation. Gates are for work an agent can be asked to
redo; a breach cannot be corrected by re-prompting, because the write already
happened. It aborts the phase and names every offending path.

Two keys drive it, both in sssf.config.yaml:
    defaults.protected_files   paths no agent may touch unless it names them itself
    agents[].writes      None = unrestricted · [] = read-only · [...] = only these
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from .data_types import AgentConfig, SSSFConfig


class PermissionBreach(RuntimeError):
    """An agent modified a path it was not permitted to modify."""


def _git(args: list[str], cwd) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


class Snapshot(dict):
    """path -> content fingerprint, plus the commit the tree was measured against.

    `head` is what makes a commit visible. An agent with bash can write a file
    and `git commit` it; measured against the NEW HEAD that change is gone from
    the diff. So the snapshot taken afterwards is measured against the commit
    the phase started on, and a committed change shows up like any other.
    """

    head: str = ""


def _fingerprint(path: Path) -> str:
    """What is at `path` right now: a content hash, never a line count.

    Line counts (numstat) cannot tell one edit from another of the same size:
    rewriting a line in an already-dirty file leaves `+1 -0` exactly as it was.
    """
    try:
        if path.is_symlink():
            return "link:" + os.readlink(path)
        if path.is_dir():
            return "dir"                           # a submodule / nested repo
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"{digest}:{'x' if os.access(path, os.X_OK) else '-'}"
    except FileNotFoundError:
        return "absent"                            # deleted from the working tree
    except OSError as error:
        return f"unreadable:{error.errno}"


def snapshot(run, base: str = "") -> Snapshot:
    """Fingerprint every path the working tree currently differs on.

    The tree is compared against `base` (a commit; default: the current HEAD).
    `enforce` passes the HEAD recorded BEFORE the agent ran, so the two
    snapshots of a phase are measured against the same commit even when the
    agent moved HEAD in between. Untracked files are included; gitignored paths
    never appear, which is why the session runtime under `data_dir` — where
    handoff files legitimately land — needs no special case.
    """
    root = Path(run.repo_root)
    shot = Snapshot()
    shot.head = base or _git(["rev-parse", "--verify", "--quiet", "HEAD"], root).strip()
    paths = _git(["diff", shot.head or "HEAD", "--name-only", "--no-renames", "-z"],
                 root).split("\0")
    paths += _git(["ls-files", "--others", "--exclude-standard", "-z"], root).split("\0")
    for path in paths:
        if path:
            shot[path] = _fingerprint(root / path)
    return shot


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Every path whose state differs — appeared, vanished, or was rewritten."""
    return sorted({p for p in set(before) | set(after)
                   if before.get(p) != after.get(p)})


def _glob(pattern: str) -> re.Pattern:
    """Translate a pattern, with `*` stopping at a path separator.

    fnmatch would let `*` cross `/`, which quietly widens every pattern:
    `adws/adw_*.py` would match `adws/adw_data/sessions/x/y.py` as well as the
    ADW scripts it means. `**` is the way to say "cross directories".
    """
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):                      # directory prefix
        return path.startswith(pattern)
    if "*" in pattern or "?" in pattern:
        return _glob(pattern).fullmatch(path) is not None
    return path == pattern


def always_writable(cfg: SSSFConfig) -> list[str]:
    """The session runtime, which EVERY agent must be able to write.

    `context_handoff/` is the one place agents hand work to each other, and an
    agent's own prompts, raw_output.jsonl, and envelope.json land beside it.
    Scout writes its findings there, the reviewer its review, the planner its
    plan — a read-only agent is read-only with respect to the REPO, never with
    respect to its own report.

    This is granted from `data_dir` rather than left to .gitignore. The runtime
    is normally ignored, so it never even appears in a snapshot — but an agent's
    ability to record its work must not hang on a gitignore entry that someone
    can delete or that a changed `data_dir` can outgrow.
    """
    return [cfg.defaults.data_dir.rstrip("/") + "/"]


def permitted(path: str, agent: AgentConfig, cfg: SSSFConfig) -> bool:
    """Session runtime first, then the agent's own list, then what is protected."""
    if any(_matches(path, p) for p in always_writable(cfg)):
        return True
    if any(_matches(path, p) for p in (agent.writes or [])):
        return True                      # naming a path is what unlocks a protected one
    if any(_matches(path, p) for p in cfg.defaults.protected_files):
        return False
    return agent.writes is None          # None = unrestricted, [] = no repo writes


def _in_commit(run, commit: str, path: str) -> bool:
    if not commit:
        return False
    result = subprocess.run(["git", "cat-file", "-e", f"{commit}:{path}"],
                            cwd=run.repo_root, capture_output=True, text=True)
    return result.returncode == 0


def _roll_back(run, path: str, before: dict[str, str], after: dict[str, str]) -> str:
    """Undo one unauthorized change. Returns a word describing what happened.

    Only changes the agent INTRODUCED are undone. A path that was already dirty
    when the agent started is left exactly as it is: the operator had
    uncommitted work there, and discarding it to tidy up would be the same harm
    this module exists to prevent, committed by the cleanup instead of the agent.

    A clean path goes back to what the commit the phase started on holds — in
    the index as well as the working tree, so a change the agent staged (or a
    new file it `git add`ed) is undone too, not just reported as undone.
    """
    if path in before:
        # Already dirty beforehand. If it is gone from the diff now, the agent
        # reverted an engineer's uncommitted work and the content is not ours
        # to reconstruct — say so loudly rather than pretend it was handled.
        return "REVERTED-BY-AGENT (uncommitted work lost, cannot restore)" \
            if path not in after else \
            "left as-is (was already modified before the agent changed it further)"
    head = getattr(before, "head", "")
    if _in_commit(run, head, path):
        result = subprocess.run(["git", "checkout", head, "--", path],
                                cwd=run.repo_root, capture_output=True, text=True)
        return "rolled back" if result.returncode == 0 else "could not roll back"
    # The path did not exist when the phase started: remove it, staged or not.
    subprocess.run(["git", "rm", "--cached", "-q", "-f", "--ignore-unmatch", "--", path],
                   cwd=run.repo_root, capture_output=True, text=True)
    try:
        (Path(run.repo_root) / path).unlink(missing_ok=True)
        return "deleted"
    except OSError as error:
        return f"could not delete ({error})"


def enforce(run, phase, agent: AgentConfig, before: dict[str, str]) -> list[str]:
    """Compare the tree against `before`; undo and raise if the agent overstepped.

    Returns the paths it legitimately changed, so the trace records what an
    agent actually touched rather than only what it claimed in its envelope.

    Detection alone would leave the repo holding the unauthorized change while
    reporting a failure, so anything the agent introduced outside its allowlist
    is rolled back before the phase dies. What it cannot undo, it names.
    """
    head = getattr(before, "head", "")
    after = snapshot(run, base=head)
    touched = changed_paths(before, after)
    breaches = [p for p in touched if not permitted(p, agent, run.cfg)]
    if not breaches:
        return touched

    outcomes = {p: _roll_back(run, p, before, after) for p in breaches}
    scope = ("read-only" if agent.writes == []
             else f"limited to {agent.writes}" if agent.writes
             else f"barred from {run.cfg.defaults.protected_files}")
    detail = "\n".join(f"  - {p} — {outcome}" for p, outcome in outcomes.items())
    now = _git(["rev-parse", "--verify", "--quiet", "HEAD"], run.repo_root).strip()
    if now != head:
        # The working tree is restored; history is not ours to rewrite.
        detail += (f"\n  HEAD moved during this phase ({head[:7] or 'no commit'} -> "
                   f"{now[:7] or 'no commit'}): the agent committed or reset. The paths "
                   f"above are restored in the working tree, but its commits are still "
                   f"in history — inspect with `git log {head[:7]}..{now[:7]}`.")
    raise PermissionBreach(
        f"{agent.name} is {scope} but modified {len(breaches)} path(s):\n{detail}")
