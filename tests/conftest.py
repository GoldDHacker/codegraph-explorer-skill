"""Shared fixtures for the codegraph_builder.py test suite.

Two ways of exercising the script, used deliberately for different purposes:

- `run_cli()` shells out to the real script (`python codegraph_builder.py ...`), exactly
  the way a user or Claude invokes it. This is the backbone of the regression tests --
  every real bug found during this project's development (the Java @Override line, the
  TS accessibility_modifier miss, the dropped `export const` literal, the graph_db
  rmtree-on-a-file crash, the shrink-guard threshold) was found by running the actual
  CLI against a real fixture and inspecting real output, not by unit-testing an internal
  function in isolation. Prefer this for anything that should never silently regress.
- `import_cb()` imports the module in-process for direct unit tests of a specific
  function (e.g. `gitignore_matches`, `redact`) where subprocess overhead buys nothing.

Both are provided so each test can pick whichever is the more honest reflection of what
it's actually verifying.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "codegraph_builder.py"


@pytest.fixture
def run_cli():
    def _run(*args, cwd=None, timeout=30):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *[str(a) for a in args]],
            capture_output=True, text=True, cwd=cwd, timeout=timeout,
        )
        return proc
    return _run


@pytest.fixture
def graph_json():
    """Load .codegraph/graph.json for a project root as a dict."""
    def _load(project_root):
        return json.loads((Path(project_root) / ".codegraph" / "graph.json").read_text(encoding="utf-8"))
    return _load


@pytest.fixture(scope="session")
def cb():
    """Import codegraph_builder.py in-process, once per test session."""
    sys.path.insert(0, str(SCRIPT.parent))
    import codegraph_builder as _cb
    return _cb


def write_files(root: Path, files: dict):
    """files: {"relative/path.py": "content", ...} -- creates parent dirs as needed."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
