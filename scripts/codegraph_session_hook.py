#!/usr/bin/env python3
"""
codegraph_session_hook.py -- make the code graph *already there* when a coding
session starts, instead of building it lazily on the first architecture question.

Wire it as a Claude Code ``SessionStart`` hook (see ``references/hook_setup.md``),
or as a git ``post-checkout`` / ``post-merge`` hook, or run it by hand. It is
deliberately cheap and non-blocking:

  * no ``.codegraph/graph.json``  -> spawn a full build in a detached background
    process and return immediately;
  * a graph older than ``CODEGRAPH_AUTOBUILD_MAX_AGE`` seconds (default 1800)
    -> spawn ``codegraph_builder.py --update`` in the background;
  * a recent graph -> print one line with its node/edge counts and exit.

It never blocks the session, never exits non-zero, and never touches the network.
It only shells out to ``codegraph_builder.py`` (found next to this file, on
``$CODEGRAPH_BUILDER``, or on ``PATH``) -- the builder does all the real work.

Opt out entirely with ``CODEGRAPH_AUTOBUILD=0``.

Stdlib only. Python 3.8+.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Extensions the builder can parse -- used only to decide "is this a code repo".
_CODE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".java", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".kt", ".kts", ".swift", ".scala", ".m", ".mm", ".lua", ".dart",
}
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", "target", "out", ".next", ".nuxt", "vendor", ".codegraph",
    ".pytest_cache", ".mypy_cache", ".tox", ".gradle", "bin", "obj",
}
_DEFAULT_MAX_AGE = 1800  # seconds; a graph younger than this is "fresh enough"
_LOCK_STALE_AFTER = 3600  # seconds; ignore a lock older than this (crashed build)


def _log(msg: str) -> None:
    # One short line on stdout is all a SessionStart hook should ever emit.
    sys.stdout.write("codegraph: " + msg + "\n")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def _resolve_root() -> Path:
    # 1) an explicit path argument that exists (git hooks pass ref hashes, not
    #    paths, so those fall through);
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        p = Path(arg).expanduser()
        if p.is_dir():
            return p.resolve()
    # 2) Claude Code passes a JSON blob on stdin with "cwd";
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                cwd = json.loads(raw).get("cwd")
                if cwd and Path(cwd).is_dir():
                    return Path(cwd).resolve()
        except Exception:
            pass
    # 3) the env var Claude Code also exports;
    cpd = os.environ.get("CLAUDE_PROJECT_DIR")
    if cpd and Path(cpd).is_dir():
        return Path(cpd).resolve()
    # 4) fall back to the working directory.
    return Path.cwd().resolve()


def _looks_like_code_repo(root: Path, budget: int = 4000) -> bool:
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() in _CODE_EXT:
                return True
            seen += 1
            if seen >= budget:
                return False
    return False


def _find_builder() -> Path | None:
    env = os.environ.get("CODEGRAPH_BUILDER")
    if env and Path(env).is_file():
        return Path(env).resolve()
    sibling = Path(__file__).resolve().parent / "codegraph_builder.py"
    if sibling.is_file():
        return sibling
    from shutil import which
    found = which("codegraph_builder.py") or which("codegraph_builder")
    return Path(found).resolve() if found else None


def _graph_summary(graph_json: Path) -> str:
    try:
        data = json.loads(graph_json.read_text(encoding="utf-8"))
    except Exception:
        return "graph present"
    nodes = data.get("nodes")
    edges = data.get("edges")
    n = len(nodes) if isinstance(nodes, list) else data.get("stats", {}).get("nodes", "?")
    m = len(edges) if isinstance(edges, list) else data.get("stats", {}).get("edges", "?")
    return f"graph ready ({n} nodes, {m} edges)"


def _lock_is_live(lock: Path) -> bool:
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    return age < _LOCK_STALE_AFTER


def _spawn_detached(argv: list[str], cwd: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "ab", buffering=0)
    kwargs: dict = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=logf,
                        stderr=subprocess.STDOUT, close_fds=True)
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _run_worker(root: Path, mode: str) -> int:
    """Detached child: run the builder, then drop the lock. Never raises."""
    lock = root / ".codegraph" / ".autobuild.lock"
    try:
        builder = _find_builder()
        if builder is None:
            return 0
        argv = [sys.executable, str(builder), str(root)]
        if mode == "update":
            argv.insert(2, "--update")
        subprocess.run(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        pass
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--_worker":
        return _run_worker(Path(sys.argv[2]), sys.argv[3])

    if _env_flag("CODEGRAPH_AUTOBUILD"):
        return 0

    root = _resolve_root()
    codegraph_dir = root / ".codegraph"
    graph_json = codegraph_dir / "graph.json"

    if not graph_json.exists() and not _looks_like_code_repo(root):
        return 0  # not a code repo, and no graph to keep fresh -- stay silent

    builder = _find_builder()
    if builder is None:
        _log("codegraph_builder.py not found; set $CODEGRAPH_BUILDER or add it to PATH")
        return 0

    try:
        max_age = int(os.environ.get("CODEGRAPH_AUTOBUILD_MAX_AGE", _DEFAULT_MAX_AGE))
    except ValueError:
        max_age = _DEFAULT_MAX_AGE

    if graph_json.exists():
        age = time.time() - graph_json.stat().st_mtime
        if age <= max_age:
            _log(_graph_summary(graph_json))
            return 0
        mode = "update"
    else:
        mode = "build"

    lock = codegraph_dir / ".autobuild.lock"
    if lock.exists() and _lock_is_live(lock):
        if graph_json.exists():
            _log(_graph_summary(graph_json) + "; refresh already running")
        else:
            _log("build already running")
        return 0

    codegraph_dir.mkdir(parents=True, exist_ok=True)
    try:
        lock.write_text(f"{os.getpid()} {int(time.time())}\n", encoding="utf-8")
    except OSError:
        pass

    _spawn_detached(
        [sys.executable, str(Path(__file__).resolve()), "--_worker", str(root), mode],
        cwd=root,
        log_path=codegraph_dir / ".autobuild.log",
    )

    if mode == "build":
        _log("building graph in background; it will be ready to query shortly")
    else:
        _log(_graph_summary(graph_json) + "; refreshing in background")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never fail a session start
        sys.stdout.write(f"codegraph: session hook skipped ({exc})\n")
        sys.exit(0)
