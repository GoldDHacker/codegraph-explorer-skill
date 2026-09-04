"""Incremental cache (.codegraph/.file_cache.json) and edge/node dedup: repeated builds
-- full, --update, or watch's internal re-run -- must never duplicate nodes or edges,
and --update must actually skip re-parsing files whose mtime hasn't changed."""
import json

from conftest import write_files


def test_repeated_full_builds_produce_identical_counts(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "src/a.py": "def a(): pass\n",
        "src/b.py": "def b():\n    a()\n",
    })
    run_cli(tmp_path)
    g1 = graph_json(tmp_path)
    run_cli(tmp_path)
    g2 = graph_json(tmp_path)
    run_cli(tmp_path)
    g3 = graph_json(tmp_path)

    assert g1["total_nodes"] == g2["total_nodes"] == g3["total_nodes"]
    assert g1["total_edges"] == g2["total_edges"] == g3["total_edges"]


def test_update_skips_unchanged_files(tmp_path, run_cli):
    write_files(tmp_path, {"src/a.py": "def a(): pass\n"})
    run_cli(tmp_path)

    proc = run_cli(tmp_path, "--update")
    assert "1 served from cache" in proc.stdout or "0 file(s) (re)parsed" in proc.stdout, proc.stdout


def test_update_reparses_only_changed_files(tmp_path, run_cli):
    write_files(tmp_path, {
        "src/a.py": "def a(): pass\n",
        "src/b.py": "def b(): pass\n",
    })
    run_cli(tmp_path)

    (tmp_path / "src" / "a.py").write_text("def a():\n    pass\ndef a2(): pass\n", encoding="utf-8")

    proc = run_cli(tmp_path, "--update")
    assert "1 file(s) (re)parsed" in proc.stdout, proc.stdout
    assert "1 served from cache" in proc.stdout, proc.stdout


def test_update_drops_deleted_files_from_cache(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "src/a.py": "def a(): pass\n",
        "src/b.py": "def b(): pass\n",
    })
    run_cli(tmp_path)
    (tmp_path / "src" / "b.py").unlink()

    proc = run_cli(tmp_path, "--update")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    g = graph_json(tmp_path)
    paths = {n["path"] for n in g["nodes"]}
    assert "src/b.py" not in paths
    assert "src/a.py" in paths

    cache = json.loads((tmp_path / ".codegraph" / ".file_cache.json").read_text())
    assert "src/b.py" not in cache
