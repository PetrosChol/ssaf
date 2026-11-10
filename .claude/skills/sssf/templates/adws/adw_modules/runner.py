"""The Run object: config + adw_id + agent_map + tracer + console, bound once.

`run.phase(PhaseParams(...))` is the ONE phase primitive — a context manager
for all three kinds (engineer, agent, code). Success must be earned: every
phase defaults to fail; only a clean exit flips it (agent phases additionally
require a parsed envelope + green gates, enforced inside ph.call).
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from . import agents, git_helper, permissions
from .console import Console
from .data_types import AgentCall, EnvelopeBase, EventRecord, Phase, PhaseParams
from .utils import ensure_dir, now_iso


class PhaseHandle:
    def __init__(self, run: "Run", phase: Phase):
        self.run = run
        self.phase = phase

    def log(self, **payload) -> None:
        self.run.tracer.event(EventRecord(adw_id=self.run.adw_id,
                                          phase_id=self.phase.phase_id,
                                          type="log", name=self.phase.params.name,
                                          payload=payload))
        self.run.console.note(", ".join(f"{k}: {v}" for k, v in payload.items()))
        if self.phase.params.kind == "engineer" and "input" in payload:
            self.run.tracer.session_request(self.run.adw_id, str(payload["input"]))

    def call(self, call: AgentCall) -> EnvelopeBase:
        if self.phase.params.kind != "agent":
            raise RuntimeError("ph.call() is only valid inside an agent phase")
        return agents.execute(self.run, self.phase, call)

    def commit(self, message: str) -> str:
        """Commit what THIS RUN changed, and log exactly what that was.

        `git add -A` alone would also commit whatever the operator had lying
        uncommitted in the tree when the run started — a half-finished edit, a
        scratch file — under an agent's commit message. Those paths are left
        out, untouched, for as long as they are byte-for-byte what the operator
        left; the moment the run changes one, it is the run's work and goes in.
        """
        left_out = self.run.operator_paths()
        sha = git_helper.commit_all(message, exclude=left_out)
        payload = {"sha": sha, "branch": git_helper.current_branch(), "message": message}
        if left_out:
            payload["left_uncommitted"] = ", ".join(left_out)
        self.log(**payload)
        return sha


class Run:
    def __init__(self, cfg, adw_id: str, tracer, engineer: str):
        self.cfg = cfg
        self.adw_id = adw_id
        self.tracer = tracer
        self.console = Console(tracer, adw_id)
        self.engineer = engineer
        self.phases: list[Phase] = []
        self.tokens = 0
        self.cost = 0.0
        self._seq = tracer.max_phase_seq(adw_id)   # a joined run continues the sequence
        self.repo_root = git_helper.repo_root()    # where every agent is spawned to work
        self.session_dir = ensure_dir(Path(cfg.defaults.data_dir) / "sessions" / adw_id)
        self.context_handoff_dir = ensure_dir(self.session_dir / "context_handoff")
        self._agent_map_path = self.session_dir / "agent_map.json"
        self.agent_map: dict = (json.loads(self._agent_map_path.read_text(encoding="utf-8-sig"))
                                if self._agent_map_path.exists() else {})
        self.tree_baseline: dict[str, str] = self._load_tree_baseline()

    # ── tree baseline (what was already uncommitted when the session began) ─
    def _load_tree_baseline(self) -> dict[str, str]:
        """path -> content fingerprint of every uncommitted path at session start.

        Written once, when the session is created, and reused by every ADW that
        joins it with --adw-id: a plan left uncommitted by `adw_plan` is the
        SESSION's work, and a later `adw_build` in the same session must still
        be able to commit it.
        """
        path = self.session_dir / "tree_baseline.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}
        baseline = dict(permissions.snapshot(self))
        path.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
        return baseline

    def operator_paths(self) -> list[str]:
        """Baseline paths that are still exactly as the operator left them."""
        return sorted(path for path, fingerprint in self.tree_baseline.items()
                      if permissions.fingerprint(self.repo_root / path) == fingerprint)

    # ── agent map (adw_id -> per-agent coding-agent session ids) ────────────
    def save_agent_map(self, agent: str, entry: dict) -> None:
        self.agent_map[agent] = entry
        self._agent_map_path.write_text(json.dumps(self.agent_map, indent=2), encoding="utf-8")

    # ── usage (run totals mirror what the tracer accumulates in sqlite) ─────
    def add_usage(self, tokens: int, cost: float) -> None:
        self.tokens += tokens
        self.cost += cost
        self.tracer.session_add_usage(self.adw_id, tokens, cost)

    # ── the phase primitive ─────────────────────────────────────────────────
    @contextmanager
    def phase(self, params: PhaseParams):
        self._seq += 1
        phase = Phase(phase_id=f"{self.adw_id}_{self._seq:02d}_{params.name}",
                      adw_id=self.adw_id, seq=self._seq, params=params,
                      status="running", started_at=now_iso())
        self.phases.append(phase)
        self.tracer.phase_upsert(phase)
        self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                      type="phase_start", name=params.name,
                                      payload={"kind": params.kind, "owner": params.owner,
                                               "description": params.description}))
        self.console.phase_started(phase)
        clock = time.monotonic()
        try:
            yield PhaseHandle(self, phase)
        except BaseException as error:
            phase.status = "fail"                      # success must be earned
            phase.error = str(error)[:1000]
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="error", name=params.name,
                                          payload={"error": phase.error}))
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "fail"}))
            self.tracer.phase_upsert(phase)
            self.tracer.session_finish(self.adw_id, ok=False)
            self.console.phase_ended(phase, time.monotonic() - clock)
            self.console.session_finished(False, self.tokens, self.cost,
                                          self.cfg.observability.db)
            raise
        else:
            phase.status = "success"
            phase.ended_at = now_iso()
            self.tracer.event(EventRecord(adw_id=self.adw_id, phase_id=phase.phase_id,
                                          type="phase_end", name=params.name,
                                          payload={"status": "success"}))
            self.tracer.phase_upsert(phase)
            self.console.phase_ended(phase, time.monotonic() - clock)

    # ── run outcome ─────────────────────────────────────────────────────────
    def finish(self, accepted: bool = True, reason: str = "") -> int:
        """Finalize the run and return its exit code. Call this exactly once.

        Two criteria, not one. Every phase must have passed, AND the ADW's own
        acceptance test must hold. They are different questions on purpose: a
        test phase that ran the suite did its job even when the suite came back
        red, so the PHASE succeeds while the RUN must not.

        This replaces a `succeeded` property that answered only the first
        question — and, being a property with side effects, wrote the session
        status and printed the banner before the caller's `and test.passed` was
        ever evaluated. A run whose suite never passed was recorded green in the
        db, on the terminal, and in the UI while exiting 1. Anyone reading the
        trace saw success; only a CI job checking `$?` saw the truth. One call
        now settles the db, the banner, and the exit code together, so the three
        cannot disagree.
        """
        phases_ok = bool(self.phases) and all(p.status == "success" for p in self.phases)
        ok = phases_ok and accepted
        if phases_ok and not accepted:
            note = reason or "the run's acceptance criterion was not met"
            self.tracer.event(EventRecord(
                adw_id=self.adw_id,
                phase_id=self.phases[-1].phase_id if self.phases else "",
                type="error", name="not_accepted", payload={"reason": note}))
            self.console.note(f"not accepted: {note}")
        self.tracer.session_finish(self.adw_id, ok=ok)
        self.console.session_finished(ok, self.tokens, self.cost, self.cfg.observability.db)
        return 0 if ok else 1
