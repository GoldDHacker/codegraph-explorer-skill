"""Regression tests for the v4.4 Phase 5 graph versioning/diff feature: the automatic
single-slot .graph_prev.json rotation (--diff with no name), named snapshots
(--snapshot NAME / --diff NAME), and the honestly-documented line-number-in-node-id
limitation (a moved-but-unchanged symbol shows as remove+add, never as "changed")."""
import json

from conftest import write_files


class TestAutoPrevDiff:
    def test_diff_with_no_snapshot_errors_clearly(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
        run_cli(tmp_path)  # first build -- no .graph_prev.json exists yet
        proc = run_cli(tmp_path, "--diff")
        assert proc.returncode != 0
        assert "no previous-build state" in (proc.stdout + proc.stderr).lower()

    def test_diff_detects_added_node_and_edges_since_last_build(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n\ndef bar():\n    return foo()\n"})
        run_cli(tmp_path)
        write_files(tmp_path, {"a.py": (
            "def foo():\n    return 1\n\ndef bar():\n    return foo()\n\n"
            "def baz():\n    return bar()\n"
        )})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--diff", "--json")
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert any("baz" in nid for nid in result["nodes"]["added"])
        assert result["nodes"]["removed"] == []
        assert any(e["target"].startswith("func:a.py:baz") for e in result["edges"]["added"])

    def test_diff_detects_removed_node(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n\ndef bar():\n    return 2\n"})
        run_cli(tmp_path)
        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--diff", "--json")
        result = json.loads(proc.stdout)
        assert any("bar" in nid for nid in result["nodes"]["removed"])
        assert result["nodes"]["added"] == []

    def test_diff_detects_changed_node_same_line(self, tmp_path, run_cli):
        """Signature changes while the symbol stays on the same line (so its node id
        is unchanged) must show up as 'changed', not remove+add."""
        write_files(tmp_path, {"a.py": "def foo(x):\n    return x\n"})
        run_cli(tmp_path)
        write_files(tmp_path, {"a.py": "def foo(x, y):\n    return x + y\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--diff", "--json")
        result = json.loads(proc.stdout)
        assert any("foo" in nid for nid in result["nodes"]["changed"])
        assert result["nodes"]["added"] == []
        assert result["nodes"]["removed"] == []

    def test_moved_symbol_shows_as_remove_add_not_changed(self, tmp_path, run_cli):
        """Documented limitation: node ids embed the line number, so a symbol that
        merely moved (an unrelated line added above it) shows as one removed id and
        one added id, never as 'changed' -- this test locks in that this is what
        actually happens, so a future change to _nid() that silently alters this
        behavior gets caught."""
        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n"})
        run_cli(tmp_path)
        write_files(tmp_path, {"a.py": "# a new comment line\n\ndef foo():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--diff", "--json")
        result = json.loads(proc.stdout)
        assert result["nodes"]["changed"] == []
        assert any("foo" in nid for nid in result["nodes"]["added"])
        assert any("foo" in nid for nid in result["nodes"]["removed"])

    def test_no_op_rebuild_produces_empty_diff(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n"})
        run_cli(tmp_path)
        run_cli(tmp_path, "--update")  # nothing changed on disk
        proc = run_cli(tmp_path, "--diff", "--json")
        result = json.loads(proc.stdout)
        assert result["nodes"] == {"added": [], "removed": [], "changed": []}
        assert result["edges"] == {"added": [], "removed": [], "changed": []}


class TestNamedSnapshot:
    def test_snapshot_without_build_errors(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
        proc = run_cli(tmp_path, "--snapshot", "checkpoint")
        assert proc.returncode != 0

    def test_snapshot_and_diff_by_name_survives_multiple_builds(self, tmp_path, run_cli):
        """A named snapshot must stay comparable even after several intervening
        builds, unlike --diff (no name) which only ever sees the immediately prior
        build."""
        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--snapshot", "checkpoint")
        assert proc.returncode == 0, proc.stderr

        write_files(tmp_path, {"a.py": "def foo():\n    return 1\n\ndef bar():\n    return 2\n"})
        run_cli(tmp_path)
        write_files(tmp_path, {"a.py": (
            "def foo():\n    return 1\n\ndef bar():\n    return 2\n\n"
            "def baz():\n    return 3\n"
        )})
        run_cli(tmp_path)  # two builds happened since the snapshot

        proc = run_cli(tmp_path, "--diff", "checkpoint", "--json")
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        added_names = {nid.split(":")[2] for nid in result["nodes"]["added"] if nid.startswith("func:")}
        assert added_names == {"bar", "baz"}

    def test_diff_unknown_snapshot_name_errors_clearly(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--diff", "does-not-exist")
        assert proc.returncode != 0
        assert "no snapshot named" in (proc.stdout + proc.stderr).lower()

    def test_snapshot_name_sanitized_to_safe_filename(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--snapshot", "weird/name with spaces!")
        assert proc.returncode == 0, proc.stderr
        snap_dir = tmp_path / ".codegraph" / "snapshots"
        assert list(snap_dir.glob("*.json"))
