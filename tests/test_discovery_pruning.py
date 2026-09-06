"""v4.8: discovery walks the tree with os.walk and prunes SKIP_DIRS / dot-directories
*in place*, so a pathological nested node_modules tree -- or a symlink cycle inside one
-- is never descended into. Path.rglob() walked the whole subtree eagerly and raised
(OSError WinError 1921 / "too many levels of symbolic links") before any per-path skip
filter could run, which made the builder unusable on real monorepos that vendor their
dependencies (pnpm's nested node_modules, a self-referential symlink in a package).
"""
import os

import pytest

from conftest import write_files


def _func_paths(graph):
    return {n["path"] for n in graph["nodes"] if n["type"] == "function"}


def test_walk_pruned_never_descends_into_skip_dirs(cb, tmp_path):
    write_files(tmp_path, {
        "src/a.py": "x\n",
        "node_modules/pkg/index.js": "y\n",
        "src/module/.git/HEAD": "z\n",
    })
    walked = {str(d) for d, _names in cb._walk_pruned(tmp_path)}
    assert str(tmp_path / "src") in walked
    assert str(tmp_path / "node_modules") not in walked
    assert str(tmp_path / "node_modules" / "pkg") not in walked
    assert str(tmp_path / "src" / "module" / ".git") not in walked


def test_deeply_nested_node_modules_does_not_break_discovery(tmp_path, run_cli, graph_json):
    """The exact shape that crashed rglob: node_modules nested inside node_modules,
    many levels deep. Pruning stops at the first node_modules, so depth is irrelevant."""
    deep = tmp_path
    for _ in range(40):
        deep = deep / "node_modules" / "pkg"
    deep.mkdir(parents=True)
    (deep / "buried.js").write_text("function buried() {}\n", encoding="utf-8")
    write_files(tmp_path, {"src/real.py": "def real(): pass\n"})

    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    paths = _func_paths(graph_json(tmp_path))
    assert "src/real.py" in paths
    assert not any("node_modules" in p for p in paths)


def test_symlink_cycle_inside_node_modules_is_survived(tmp_path, run_cli, graph_json):
    nm = tmp_path / "node_modules" / "self"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("function s() {}\n", encoding="utf-8")
    try:
        os.symlink(tmp_path / "node_modules", nm / "loop", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks in this environment")
    write_files(tmp_path, {"src/real.py": "def real(): pass\n"})

    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "src/real.py" in _func_paths(graph_json(tmp_path))


def test_nested_ignore_files_are_still_discovered_after_pruning(tmp_path, run_cli, graph_json):
    """Pruning must not stop load_gitignore_tree from finding ignore files in ordinary
    (non-dot, non-SKIP) subdirectories."""
    write_files(tmp_path, {
        "src/module_a/main.py": "def a_main(): pass\n",
        "src/module_a/generated/gen.py": "def a_generated(): pass\n",
        "src/module_a/.gitignore": "generated/\n",
    })
    run_cli(tmp_path)
    paths = _func_paths(graph_json(tmp_path))
    assert "src/module_a/main.py" in paths
    assert "src/module_a/generated/gen.py" not in paths
