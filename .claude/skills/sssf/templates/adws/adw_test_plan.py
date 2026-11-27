#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Test Plan — analyse an existing file and propose tests for the engineer to approve.

Template: <test command> <file to examine> <folder the tests go in>

Usage:
    uv run adws/adw_test_plan.py <path/to/file> <tests/folder> [--focus "what matters most"] [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> test_planner -> code(present)

Step one of two. The planner reads the file, works out what it is for, and
writes a test plan (basic, edge, and error cases), with every test file inside
the named tests folder, to
`context_handoff/test_plan.md`. It changes nothing in the repo (`writes: []`).
The run then stops: approving is the engineer's call, and a headless run cannot
wait for it. Read the plan, edit it if you like, then approve it by running

    uv run adws/adw_test_write.py --adw-id <the id this run printed>

`just test-file <path> <tests/folder>` does both, with a y/N prompt in between.
"""

import argparse
import sys

from adw_modules import agents, quality, session, testing
from adw_modules.data_types import AgentCall, PhaseParams, TestPlanOutput, TestRequest

# The tester is validated too: the plan is checked against where it may write.
REQUIRED_AGENTS = ["test_planner", "tester"]


def main(target: str, tests_dir: str, focus: str = "",
         config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    target = testing.target_path(target)
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    tests_dir = testing.tests_dir_path(tests_dir, target, cfg)
    run = session.ensure(cfg, adw_id)
    prompt = testing.request_text(target, tests_dir, focus)
    runner = (f"The project's test command is `{quality.test_command()}`; the tests you plan "
              f"must run under it, with the test file paths appended."
              if quality.test_configured() else
              "No test command is configured yet (adws/adw_modules/quality.py still has the "
              "placeholder). Plan for the runner this repo already uses, and say which in `framework`.")

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture which file to test, where its tests go, "
                                           "and any focus the engineer gave")) as ph:
        ph.log(input=prompt, target=target, tests_dir=tests_dir)
        testing.record_request(run, TestRequest(target=target, tests_dir=tests_dir, focus=focus))

    with run.phase(PhaseParams(name="plan", kind="agent", owner="test_planner",
                               description="Read the file, work out what it is for, and propose "
                                           "basic, edge and error cases — writing no tests yet")) as ph:
        snap = testing.snapshot_target(run, target)
        plan = ph.call(AgentCall(output_type=TestPlanOutput, prompt=f"{prompt}\n\n{runner}",
                                 gates=[testing.plan_complete]))
        testing.guard_target(run, snap)

    with run.phase(PhaseParams(name="present", kind="code", owner="approval",
                               description="Hand the plan to the engineer, who approves it by "
                                           "running adw_test_write — nothing is written until then")) as ph:
        digest = testing.record_plan(run)
        ph.log(plan=str(testing.plan_path(run)), cases=len(plan.cases), test_files=", ".join(plan.test_files),
               sha256=digest[:12])
        if not quality.test_configured():
            ph.log(warning="wire the test command in adws/adw_modules/quality.py before approving — "
                           "adw_test_write refuses to run on the placeholder")

    code = run.finish()
    if code == 0:
        print(f"\nTest plan ready for review: {testing.plan_path(run)}\n"
              f"Approve it (edit the file first if you want changes) with:\n"
              f"  uv run adws/adw_test_write.py --adw-id {run.adw_id}")
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="the existing file to write tests for")
    parser.add_argument("tests_dir", help="the folder the tests go in — the only place the tester may write")
    parser.add_argument("--focus", default="", help="optional: what matters most to test")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(args.target, args.tests_dir, args.focus, args.config, args.adw_id))
