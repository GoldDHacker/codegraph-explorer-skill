""".gitignore / .codegraphignore matching, including nested files (v4.0) -- each nested
file's patterns must be scoped to its own directory and never leak to a sibling."""
from conftest import write_files


def _func_paths(graph):
    return {n["path"] for n in graph["nodes"] if n["type"] == "function"}


def test_root_gitignore_still_works(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "src/a.py": "def keep(): pass\n",
        "vendor/b.py": "def drop(): pass\n",
        ".gitignore": "vendor/\n",
    })
    run_cli(tmp_path)
    paths = _func_paths(graph_json(tmp_path))
    assert "src/a.py" in paths
    assert "vendor/b.py" not in paths


def test_root_codegraphignore_independent_of_gitignore(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "src/a.py": "def keep(): pass\n",
        "fixtures/b.py": "def drop(): pass\n",
        ".codegraphignore": "fixtures/\n",
    })
    run_cli(tmp_path)
    paths = _func_paths(graph_json(tmp_path))
    assert "src/a.py" in paths
    assert "fixtures/b.py" not in paths


def test_nested_gitignore_scoped_to_its_own_directory(tmp_path, run_cli, graph_json):
    """A .gitignore inside src/module_a/ ignoring 'generated/' must not affect
    src/module_b/generated/, which has no ignore file of its own."""
    write_files(tmp_path, {
        "src/module_a/main.py": "def a_main(): pass\n",
        "src/module_a/generated/gen.py": "def a_generated(): pass\n",
        "src/module_a/.gitignore": "generated/\n",
        "src/module_b/main.py": "def b_main(): pass\n",
        "src/module_b/generated/gen.py": "def b_generated(): pass\n",
    })
    run_cli(tmp_path)
    paths = _func_paths(graph_json(tmp_path))
    assert "src/module_a/main.py" in paths
    assert "src/module_a/generated/gen.py" not in paths
    assert "src/module_b/main.py" in paths
    assert "src/module_b/generated/gen.py" in paths  # not ignored -- no .gitignore in module_b


def test_nested_and_root_gitignore_combine(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "src/a.py": "def keep(): pass\n",
        "vendor/b.py": "def drop_root(): pass\n",
        "src/module_a/generated/gen.py": "def drop_nested(): pass\n",
        "src/module_a/.gitignore": "generated/\n",
        ".gitignore": "vendor/\n",
    })
    run_cli(tmp_path)
    paths = _func_paths(graph_json(tmp_path))
    assert "src/a.py" in paths
    assert "vendor/b.py" not in paths
    assert "src/module_a/generated/gen.py" not in paths


def test_nested_ignore_inside_skip_dirs_is_not_consulted(tmp_path, run_cli, graph_json):
    """A .gitignore living inside node_modules/ shouldn't matter -- everything under
    node_modules/ is already excluded by the hardcoded skip-list regardless."""
    write_files(tmp_path, {
        "src/a.py": "def keep(): pass\n",
        "node_modules/pkg/.gitignore": "*\n",
        "node_modules/pkg/index.js": "function shouldNeverAppear() {}\n",
    })
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    paths = _func_paths(graph_json(tmp_path))
    assert "src/a.py" in paths
    assert not any("node_modules" in p for p in paths)
