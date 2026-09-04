"""--sync-graphdb / --cypher (v4.0, Ladybug): the parallel query layer. Covers the
schema round-trip, graceful degradation when ladybug/graph_db is missing, the
graph_db-staleness warning, and the exact all()-over-variable-length-relationship
Cypher gotcha found while building this."""
import json

import pytest

from conftest import write_files


def _require_ladybug(cb):
    if not cb.LADYBUG_AVAILABLE:
        pytest.skip("ladybug not installed")


def _make_calls_project(root):
    write_files(root, {
        "src/auth.py": (
            "class BaseService:\n"
            "    def run(self):\n"
            "        pass\n\n"
            "class AuthService(BaseService):\n"
            "    def login(self):\n"
            "        self.run()\n\n"
            "if __name__ == '__main__':\n"
            "    AuthService().login()\n"
        ),
    })


def test_cypher_without_ladybug_message(tmp_path, run_cli, cb):
    if cb.LADYBUG_AVAILABLE:
        pytest.skip("this test only applies when ladybug is NOT installed")
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--cypher", "MATCH (n) RETURN n LIMIT 1")
    assert "ladybug not installed" in proc.stdout


def test_cypher_without_sync_message(tmp_path, run_cli, cb):
    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--cypher", "MATCH (n) RETURN n LIMIT 1")
    assert "run --sync-graphdb first" in proc.stdout


def test_sync_then_cypher_roundtrip(tmp_path, run_cli, cb):
    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--sync-graphdb")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "synced to" in proc.stdout

    proc = run_cli(tmp_path, "--cypher", "MATCH (n:Symbol {name:'AuthService'}) RETURN n.name", "--json")
    out = json.loads(proc.stdout)
    assert out["rows"] == [["AuthService"]]


def test_resync_after_existing_sync_does_not_crash(tmp_path, run_cli, cb):
    """Regression: this Ladybug version stores the database as a single file, not a
    directory -- shutil.rmtree() on it raised NotADirectoryError on any re-sync. Found
    by actually re-running --sync-graphdb against an existing graph_db/, which prior
    testing had never exercised."""
    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    proc1 = run_cli(tmp_path, "--sync-graphdb")
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr
    proc2 = run_cli(tmp_path, "--sync-graphdb")
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert "Traceback" not in proc2.stdout and "Traceback" not in proc2.stderr


def test_malformed_cypher_fails_gracefully(tmp_path, run_cli, cb):
    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    run_cli(tmp_path, "--sync-graphdb")
    proc = run_cli(tmp_path, "--cypher", "NOT VALID CYPHER AT ALL")
    assert "Cypher error" in proc.stdout
    assert "Traceback" not in proc.stdout


def test_all_over_recursive_rel_fails_use_relationships_instead(tmp_path, run_cli, cb):
    """Documents the exact Cypher gotcha found while building this: filtering a
    variable-length relationship binding with all(x IN e WHERE ...) fails because `e`
    binds as a RECURSIVE_REL, not the LIST all() expects. The fix is to name the path
    and filter relationships(p) instead -- this test pins down both halves so neither
    silently changes (a Ladybug upgrade fixing the first case, or breaking the second,
    should surface here rather than only in a user's own query)."""
    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    run_cli(tmp_path, "--sync-graphdb")

    broken = run_cli(tmp_path, "--cypher", (
        "MATCH (a:Symbol {name:'AuthService'})-[e:Edge*1..2]->(b:Symbol) "
        "WHERE all(x IN e WHERE x.etype IN ['calls','inherits']) "
        "RETURN DISTINCT b.name"
    ))
    assert "Cypher error" in broken.stdout

    fixed = run_cli(tmp_path, "--cypher", (
        "MATCH p = (a:Symbol {name:'AuthService'})-[:Edge*1..2]->(b:Symbol) "
        "WHERE all(x IN relationships(p) WHERE x.etype IN ['calls','inherits']) "
        "RETURN DISTINCT b.name"
    ))
    assert "Cypher error" not in fixed.stdout
    assert "BaseService" in fixed.stdout


def test_staleness_warning_appears_after_rebuild_and_clears_after_resync(tmp_path, run_cli, cb):
    import time

    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    run_cli(tmp_path, "--sync-graphdb")

    fresh = run_cli(tmp_path, "--cypher", "MATCH (n:Symbol) RETURN count(n)")
    assert "may be stale" not in fresh.stdout

    time.sleep(1.1)  # ensure the next build's mtime is strictly newer (1s filesystem resolution)
    write_files(tmp_path, {"src/extra.py": "def extra(): pass\n"})
    run_cli(tmp_path, "--force-rebuild")

    stale = run_cli(tmp_path, "--cypher", "MATCH (n:Symbol) RETURN count(n)")
    assert "may be stale" in stale.stdout

    stale_json = run_cli(tmp_path, "--cypher", "MATCH (n:Symbol) RETURN count(n)", "--json")
    out = json.loads(stale_json.stdout)
    assert "warning" in out and "may be stale" in out["warning"]

    run_cli(tmp_path, "--sync-graphdb")
    fresh_again = run_cli(tmp_path, "--cypher", "MATCH (n:Symbol) RETURN count(n)")
    assert "may be stale" not in fresh_again.stdout


def test_no_false_staleness_warning_without_sync_meta(tmp_path, run_cli, cb):
    """A graph_db/ that predates the sync-meta tracking (no .graphdb_sync_meta.json)
    must stay silent -- absence of data means 'unknown', never 'stale'."""
    _require_ladybug(cb)
    _make_calls_project(tmp_path)
    run_cli(tmp_path)
    run_cli(tmp_path, "--sync-graphdb")
    (tmp_path / ".codegraph" / ".graphdb_sync_meta.json").unlink()

    proc = run_cli(tmp_path, "--cypher", "MATCH (n:Symbol) RETURN count(n)")
    assert "may be stale" not in proc.stdout
    assert "Cypher error" not in proc.stdout
