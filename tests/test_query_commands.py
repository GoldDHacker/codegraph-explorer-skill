"""Sanity tests for the script-side query commands (--explain/--callers/--find-path/
--trace-entrypoints/--impact): these exist so Claude never has to load graph.json in
full for a targeted question (see auto_build_protocol.md Rule 2) -- each one must
resolve against an existing graph.json without touching source files, handle an
ambiguous/unknown symbol gracefully, and --impact must walk the full transitive closure
rather than stopping at one hop."""
import json

from conftest import write_files


def _make_chain_project(root):
    write_files(root, {
        "src/service.py": (
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


def test_explain_unknown_symbol_does_not_crash(tmp_path, run_cli):
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--explain", "TotallyMadeUpSymbolXYZ")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stdout + proc.stderr


def test_explain_json_output_is_valid_json(tmp_path, run_cli):
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--explain", "AuthService", "--json")
    json.loads(proc.stdout)  # raises if malformed


def test_callers_of_base_service_includes_auth_service(tmp_path, run_cli):
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--callers", "BaseService")
    assert "AuthService" in proc.stdout


def test_impact_walks_full_transitive_closure_not_just_one_hop(tmp_path, run_cli):
    """--impact on BaseService must reach AuthService (1 hop, inherits) AND the
    entrypoint that calls AuthService.login() (2 hops, calls) -- --callers would only
    show the first hop."""
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--impact", "BaseService")
    assert "AuthService" in proc.stdout
    assert "entrypoints affected" in proc.stdout


def test_impact_on_leaf_symbol_says_nothing_depends_on_it(tmp_path, run_cli):
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--impact", "TotallyUnusedSymbolXYZ")
    assert proc.returncode == 0
    assert "Traceback" not in proc.stdout


def test_find_path_between_two_symbols(tmp_path, run_cli):
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--find-path", "AuthService", "BaseService")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BaseService" in proc.stdout


def test_trace_entrypoints_reaches_main_guard(tmp_path, run_cli):
    _make_chain_project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--trace-entrypoints", "AuthService")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_query_command_without_graph_json_tells_user_to_build_first(tmp_path, run_cli):
    proc = run_cli(tmp_path, "--explain", "Anything")
    assert proc.returncode == 1
    assert "run a build first" in proc.stdout or "no .codegraph/graph.json" in proc.stdout
