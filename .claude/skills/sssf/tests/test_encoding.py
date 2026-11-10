"""Every text file and pipe the factory touches names its encoding.

Python's default is the platform codepage. On Windows that is cp1252, cp1253
and so on, which cannot hold what agents and Context7 return: an emoji in
fetched docs crashed the docs phase on a Greek-locale machine, and anything
outside the codepage was silently garbled on the way in. So the stamped code
(and the install scripts) must say UTF-8 at every call site.

The end-to-end tests additionally run whole ADWs with EncodingWarning turned
into an error, which checks the real code path; this scan also covers branches
no test reaches.

Run from the repo root:
    uv run --with pytest,pydantic,pyyaml,python-dotenv,rich,mcp==2.3.0 pytest .claude/skills/sssf/tests -q
"""

from __future__ import annotations

import ast
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(SKILL / "templates" / "adws").rglob("*.py"),
                  *(SKILL / "scripts").glob("*.py")])


def _default_encoding_calls(path: Path) -> list[str]:
    """Calls that would use the platform encoding: text file I/O and text-mode
    subprocess pipes with no `encoding=`."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or any(k.arg == "encoding" for k in node.keywords):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else "")
        if name in ("read_text", "write_text"):
            bad = True
        elif name == "open":
            # builtin open(path, mode) or Path.open(mode); Session.open() is not I/O
            mode = (node.args[0] if isinstance(func, ast.Attribute)
                    else node.args[1] if len(node.args) > 1 else None) if node.args else None
            if isinstance(func, ast.Name) and mode is None:
                bad = True
            else:
                bad = (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                       and "b" not in mode.value)
        else:
            bad = (isinstance(func, ast.Attribute)
                   and name in ("run", "Popen", "check_output")
                   and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
                   and any(k.arg in ("text", "universal_newlines") for k in node.keywords))
        if bad:
            where = path.relative_to(SKILL) if path.is_relative_to(SKILL) else path.name
            found.append(f"{where}:{node.lineno} {name}()")
    return found


def test_the_scan_sees_the_sources():
    names = {p.name for p in SOURCES}
    assert {"docs.py", "agent_cc.py", "quality.py", "install.py"} <= names


def test_no_text_io_relies_on_the_platform_encoding():
    offenders = [hit for path in SOURCES for hit in _default_encoding_calls(path)]
    assert offenders == [], "pass encoding='utf-8' (reads: 'utf-8-sig'):\n" + "\n".join(offenders)


def test_the_scan_catches_what_it_is_for(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("import subprocess\nfrom pathlib import Path\n"
                   "Path('a').write_text('x')\nopen('f', 'a')\n"
                   "subprocess.run(['git'], text=True)\nPath('a').open('rb')\n"
                   "session.open()\n", encoding="utf-8")
    hits = sorted(hit.split()[-1] for hit in _default_encoding_calls(bad))
    assert hits == ["open()", "run()", "write_text()"]     # not rb, not session.open()
