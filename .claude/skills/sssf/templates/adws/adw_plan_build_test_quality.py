#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich", "mcp==2.3.0"]
# ///
"""ADW Plan Build Test Quality — full agent chain plus deterministic quality.

Usage:
    uv run adws/adw_plan_build_test_quality.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> [code(docs)] -> planner -> builder -> [code(verify) -> code(test) -> builder(fix)] bounded -> git(commit)

Verify and test are CODE, not agents. Their commands are known, so running them
needs no judgement — only repairing them does. A failing block does not fail its
phase: the runner did its job, the code is what failed. The failure becomes an
envelope and flows back into the builder, and only an exhausted repair loop
fails the run.
"""

import argparse
import sys

from adw_modules import agents, docs, gates, quality, session, utils
from adw_modules.data_types import AgentCall, BuildOutput, PhaseParams, PlanOutput

REQUIRED_AGENTS = ["planner", "builder"]
MAX_FIX_LOOPS = 3


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS, docs_phase=True)
    run = session.ensure(cfg, adw_id)

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt)

    docs_envelope = None              # Layer 2: only when context7.enabled
    if cfg.context7.enabled:
        with run.phase(PhaseParams(name="docs", kind="code", owner="context7",
                                   description="Fetch version-pinned library docs so no agent "
                                               "plans against stale APIs")) as ph:
            fetched = docs.capture(run, prompt)
            ph.log(**docs.summary(fetched))
            docs_envelope = docs.as_envelope(fetched)
            docs.check(run, docs_envelope)

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt, previous=docs_envelope,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan exactly")) as ph:
        previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=plan,
                                     gates=[gates.diff_matches_claims]))

    def record(ph, result) -> None:
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    test_result = None
    quality_result = None
    # MAX_FIX_LOOPS counts FIXES, and every fix is followed by a verify pass:
    # verify, fix, verify, fix, ... verify. The loop therefore verifies
    # one more time than it repairs, so the last fix is verified, never wasted.
    for i in range(1, MAX_FIX_LOOPS + 2):
        with run.phase(PhaseParams(name=f"verify_{i}", kind="code", owner="quality",
                                   description="Lint, typecheck, and build before testing")) as ph:
            quality_result = quality.run_quality(run)
            record(ph, quality_result)

        # run_quality() already includes the test block; a repo that wants tests
        # in their own phase can split them out the way this comment does.
        test_result = quality_result

        if (quality_result.passed and test_result.passed) or i > MAX_FIX_LOOPS:
            break

        # Whichever block failed becomes the builder's spec — verbatim command
        # output, no parser standing between the failure and the fix.
        broken = quality_result if not quality_result.passed else test_result
        what = "verification" if not quality_result.passed else "tests"
        with run.phase(PhaseParams(name=f"fix_{i}", kind="agent", owner="builder", retries=1,
                                   description=f"Resolve the reported {what} failures")) as ph:
            previous = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                                         previous=quality.as_envelope(broken, what),
                                         gates=[gates.diff_matches_claims]))

    verified = (quality_result is not None and quality_result.passed
                and test_result is not None and test_result.passed)
    if verified:
        with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                                   description="Commit the tested and quality-verified working tree")) as ph:
            message = previous.commit_message or f"sssf({run.adw_id}): {previous.summary}"
            ph.commit(message)

    return run.finish(accepted=verified,
                      reason=f"verify/test never came back clean after {MAX_FIX_LOOPS} fix attempt(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
