"""Keep pytest out of the factory.

pytest's default file pattern is `test_*.py` AND `*_test.py`, so a plain
`pytest` from the repo root would collect `adw_build_test.py` and
`adw_plan_build_test.py` as test modules. Importing them fails — their
dependencies (pyyaml, pydantic, ...) come from their own `uv run` script
block, not the project's environment — and the whole run errors before a
single project test executes. That includes the test phase in quality.py.

Nothing under adws/ is a project test, so nothing under it is collected.
This file is in `protected_files`: a conftest can hook into pytest itself,
and an agent must not be able to edit what grades its work.
"""

collect_ignore_glob = ["*"]
