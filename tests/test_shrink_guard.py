"""Shrink-guard (v3.4): build() must refuse to overwrite a good graph with one built
from a collapsed file scan. This is a regression test for the exact bug that motivated
the feature -- a broken .gitignore matcher once made discover() silently report "0
files found" on a real project, which would have overwritten a correct graph.json with
an empty one."""
from conftest import write_files


def _make_project(root, n_files):
    write_files(root, {f"src/mod{i}.py": f"def f{i}(): pass\n" for i in range(n_files)})


def test_shrink_guard_blocks_a_collapsed_rescan(tmp_path, run_cli):
    _make_project(tmp_path, 60)
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr

    # Delete all but 3 files -- .file_cache.json still remembers 60 tracked files, so
    # the next full build should see a >90% drop and refuse rather than overwrite.
    for i in range(3, 60):
        (tmp_path / "src" / f"mod{i}.py").unlink()

    proc = run_cli(tmp_path)
    assert proc.returncode == 1
    assert "refusing to continue" in proc.stdout

    # graph.json must be untouched -- still reflects the original 60-file build.
    graph = __import__("json").loads((tmp_path / ".codegraph" / "graph.json").read_text())
    assert graph["total_nodes"] >= 60  # 60 functions + file nodes, well above the 3-file count


def test_shrink_guard_does_not_misfire_under_the_floor(tmp_path, run_cli):
    """With fewer than 5 previously-tracked files, a full drop must NOT trigger the
    guard -- small projects can't be false-positived by the 10% threshold."""
    _make_project(tmp_path, 3)
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr

    for i in range(3):
        (tmp_path / "src" / f"mod{i}.py").unlink()
    write_files(tmp_path, {"src/only_one.py": "def solo(): pass\n"})

    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "refusing to continue" not in proc.stdout


def test_shrink_guard_does_not_misfire_under_ten_percent_threshold(tmp_path, run_cli):
    """8 tracked files -> 1 remaining is a 12.5% survival rate, which is > 10% and
    should NOT trigger the guard (this exact boundary was mis-tested once during
    development -- worth pinning down precisely)."""
    _make_project(tmp_path, 8)
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr

    for i in range(1, 8):
        (tmp_path / "src" / f"mod{i}.py").unlink()

    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_force_rebuild_bypasses_the_guard(tmp_path, run_cli):
    _make_project(tmp_path, 60)
    run_cli(tmp_path)
    for i in range(3, 60):
        (tmp_path / "src" / f"mod{i}.py").unlink()

    proc = run_cli(tmp_path, "--force-rebuild")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    graph = __import__("json").loads((tmp_path / ".codegraph" / "graph.json").read_text())
    func_nodes = [n for n in graph["nodes"] if n["type"] == "function"]
    assert len(func_nodes) == 3
