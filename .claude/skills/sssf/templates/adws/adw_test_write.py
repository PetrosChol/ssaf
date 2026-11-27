#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Test Write — write the approved tests, run them, repair the TESTS, report real bugs.

Usage:
    uv run adws/adw_test_write.py --adw-id <id from adw_test_plan> [--config adws/adw_sssf_config/sssf.config.yaml]

Phases: engineer(approve) -> tester(write) -> code(test) [-> tester(triage) -> code(test) ... bounded]
        -> git(commit) when green · code(report) when red

Step two of two. Running this IS the approval: it joins the session
adw_test_plan created and follows `context_handoff/test_plan.md` as it stands
now, including any edits the engineer made.

The tester may write only test paths (`writes:` in sssf.config.yaml), and the
file under test is restored and the phase failed if anything changes it. A red
run goes back to the tester for triage, failure by failure:

  * the TEST is wrong (bad expectation, wrong import, typo) -> it repairs the test;
  * the CODE is wrong -> it reports a suspected bug and leaves the test failing.

The loop ends when the suite is green, when a triage round changes nothing
(every remaining failure is a suspected bug), or after MAX_FIX_LOOPS repairs.
Green commits the tests. Red commits nothing and writes
`context_handoff/suspected_bugs.md`; the run exits 1, because a suite that
found bugs is not a passing suite, and the bugs are what the engineer reads next.
"""

import argparse
import sys

from adw_modules import agents, gates, quality, session, testing
from adw_modules.data_types import AgentCall, PhaseParams, TesterOutput

REQUIRED_AGENTS = ["tester"]
MAX_FIX_LOOPS = 3


def main(adw_id: str, config: str = "adws/adw_sssf_config/sssf.config.yaml") -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    if not quality.test_configured():
        raise SystemExit("the test command in adws/adw_modules/quality.py is still the placeholder "
                         "echo, which passes without running anything. Wire the real one first.")
    approved = testing.approved_plan(cfg, adw_id)     # before joining: a bad id costs nothing
    run = session.ensure(cfg, adw_id)
    target = approved.envelope.target
    prompt = testing.request_text(target)

    def record(ph, result) -> None:
        ph.log(passed=result.passed, command=result.checks[0].command if result.checks else "",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="approve", kind="engineer", owner=run.engineer,
                               description="Record which plan the engineer approved, and whether "
                                           "they edited it, before any test is written")) as ph:
        ph.log(input=f"approved test plan for {target}", plan=approved.path,
               sha256=approved.sha256[:12], edited_by_engineer=approved.edited)
        snap = testing.snapshot_target(run, target)
        if snap.content is None:
            raise SystemExit(f"the file under test, {target}, no longer exists")

    with run.phase(PhaseParams(name="write", kind="agent", owner="tester", retries=1,
                               description="Write the tests the approved plan lists — test files "
                                           "only, the file under test stays untouched")) as ph:
        tester = ph.call(AgentCall(output_type=TesterOutput, prompt=prompt, previous=approved.envelope,
                                   gates=[testing.tests_declared, gates.diff_matches_claims]))
        testing.guard_target(run, snap)

    result = None
    exhausted = False
    # Same shape as every fix loop: the suite runs once more than the tester
    # repairs, so the last repair is always checked.
    for i in range(1, MAX_FIX_LOOPS + 2):
        with run.phase(PhaseParams(name=f"test_{i}", kind="code", owner="quality",
                                   description="Run only the tests this workflow wrote, with the "
                                               "project's own test command")) as ph:
            result = quality.run_tests(run, tester.test_files)
            record(ph, result)

        if result.passed:
            break
        if i > MAX_FIX_LOOPS:
            exhausted = True
            break

        with run.phase(PhaseParams(name=f"triage_{i}", kind="agent", owner="tester", retries=1,
                                   description="Sort each failure into a test mistake to repair or "
                                               "a real bug to report, never editing the code")) as ph:
            tester = ph.call(AgentCall(output_type=TesterOutput, prompt=prompt,
                                       previous=quality.as_envelope(result, "tests"),
                                       gates=[testing.failures_accounted_for, testing.tests_declared,
                                              gates.diff_matches_claims]))
            testing.guard_target(run, snap)
            ph.log(repaired=len(tester.changed_files), suspected_bugs=len(tester.suspected_bugs))

        if not tester.changed_files:
            break                  # nothing left to repair: every failure is a suspected bug

    green = result is not None and result.passed
    if green:
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Land the tests only once they pass against the "
                                               "untouched code")) as ph:
            ph.commit(tester.commit_message or f"Add tests for {target}")
    else:
        with run.phase(PhaseParams(name="report", kind="code", owner="quality",
                                   description="Write down the suspected bugs for the engineer, "
                                               "since a red suite is left uncommitted")) as ph:
            report = testing.write_bug_report(run, target, tester, exhausted)
            ph.log(report=str(report), suspected_bugs=len(tester.suspected_bugs), exhausted=exhausted)

    bugs = len(tester.suspected_bugs)
    reason = (f"the tests still failed after {MAX_FIX_LOOPS} repair attempt(s)" if exhausted else
              f"{bugs} suspected bug(s) in {target} — see context_handoff/{testing.BUG_REPORT}")
    code = run.finish(accepted=green, reason=reason)
    if not green:
        print(f"\nSuspected bugs: {run.context_handoff_dir / testing.BUG_REPORT}")
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adw-id", required=True, help="the session adw_test_plan created")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    args = parser.parse_args()
    sys.exit(main(args.adw_id, args.config))
