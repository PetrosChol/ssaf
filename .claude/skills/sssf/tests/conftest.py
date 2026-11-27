"""Shared test setup: a git identity for every test, and the `fake_claude` marker."""

from __future__ import annotations

import os

import pytest

# The tests create repositories and commit in them, both directly and through
# ADWs they run as subprocesses. A machine with no global git identity (a CI
# runner) refuses to commit, so one is supplied through the environment, which
# every child process inherits.
for _name, _value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                      ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
    os.environ.setdefault(_name, _value)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "fake_claude: the test executes tests/fixtures/fake_claude.py as the "
                   "`claude` binary, which only a POSIX system can do (it is a shebang script)")


def pytest_collection_modifyitems(items):
    if os.name == "posix":
        return
    skip = pytest.mark.skip(reason="fake_claude.py is a shebang script; this OS cannot execute it")
    for item in items:
        if item.get_closest_marker("fake_claude"):
            item.add_marker(skip)
