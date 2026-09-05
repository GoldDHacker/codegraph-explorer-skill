"""Regression tests for the v4.6 Phase 6 `--subgraph` command: bounded-neighborhood
extraction for Claude to reason over directly, plus the script-computed structural
facts (cut vertices via Tarjan, cycles through the focus node, component count on
removal) that exist so that reasoning is grounded in exact graph theory instead of
Claude eyeballing a JSON blob for it. Every graph shape below was checked by hand
before being asserted, per this project's standing discipline of measuring instead
of assuming (see the confidence-tier value used in the filtering tests, which was
read off a real run before being hard-coded here)."""
import json

from conftest import write_files


# a -> b -> d, a -> c -> d, d2 -> a : a undirected cycle a-b-d-c-a, plus a pendant
# d2 hanging off a only. Removing 'a' splits {b, d, c} from {d2} -> 2 components,
# so 'a' is a cut vertex of this neighborhood and the cycle a-b-d-c-a should be found.
CYCLE_FIXTURE = {
    "a.py": "from b import b_func\nfrom c import c_func\n\n"
            "def a_func():\n    b_func()\n    c_func()\n",
    "b.py": "from d import d_func\n\ndef b_func():\n    d_func()\n",
    "c.py": "from d import d_func\n\ndef c_func():\n    d_func()\n",
    "d.py": "from a import a_func\n\ndef d_func():\n    pass\n\ndef d_func2():\n    a_func()\n",
}


class TestCutVertexAndCycle:
    def test_focus_cut_vertex_and_component_count(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--depth", "3", "--json")
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        assert result["focus_is_cut_vertex"] is True
        assert result["components_if_focus_removed"] == 2
        assert result["node_count"] == 5  # a_func, b_func, c_func, d_func, d_func2

    def test_cycle_through_focus_reported(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--depth", "3", "--json")
        result = json.loads(proc.stdout)
        cycles = result["cycles_through_focus"]
        assert len(cycles) >= 1
        names = set(cycles[0])
        assert names == {"a_func", "b_func", "c_func", "d_func"}
        assert "d_func2" not in names  # the pendant is not part of any cycle

    def test_non_cut_vertex_reports_false(self, tmp_path, run_cli):
        # b_func only touches a_func and d_func on a single chain from the focus's
        # perspective at depth 1 -- but the real check is a leaf: d_func2 has one
        # edge (to a_func), so from d_func2's own neighborhood it's never a cut vertex.
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "d_func2", "--depth", "1", "--json")
        result = json.loads(proc.stdout)
        assert result["focus_is_cut_vertex"] is False
        assert result["components_if_focus_removed"] in (0, 1)


class TestDepthBound:
    def test_depth_1_excludes_second_hop_node(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--depth", "1", "--json")
        result = json.loads(proc.stdout)
        names = {n["name"] for n in result["nodes"]}
        assert names == {"a_func", "b_func", "c_func", "d_func2"}
        assert "d_func" not in names  # two hops away (a_func -> b_func -> d_func)

    def test_depth_3_includes_second_hop_node(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--depth", "3", "--json")
        result = json.loads(proc.stdout)
        names = {n["name"] for n in result["nodes"]}
        assert "d_func" in names
        d_func_node = next(n for n in result["nodes"] if n["name"] == "d_func")
        assert d_func_node["depth"] == 2


class TestConfidenceFiltering:
    # 5 same-named candidates -> the pre-v4.2 fallback tier's k<=8 bucket -> a
    # measured 0.35 confidence (read off a real run before being hard-coded here,
    # not assumed), which sits below the default --min-confidence of 0.5.
    AMBIGUOUS_FIXTURE = {
        "e.py": "def shared():\n    return 'e'\n",
        "f.py": "def shared():\n    return 'f'\n",
        "h.py": "def shared():\n    return 'h'\n",
        "i.py": "def shared():\n    return 'i'\n",
        "j.py": "def shared():\n    return 'j'\n",
        "g.py": "def caller():\n    return shared()\n",
    }

    def test_low_confidence_edge_excluded_by_default(self, tmp_path, run_cli):
        write_files(tmp_path, self.AMBIGUOUS_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "caller", "--depth", "2", "--json")
        result = json.loads(proc.stdout)
        assert result["edge_count"] == 0
        assert result["min_confidence_applied"] == 0.5

    def test_include_low_confidence_flag_restores_it(self, tmp_path, run_cli):
        write_files(tmp_path, self.AMBIGUOUS_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "caller", "--depth", "2",
                        "--include-low-confidence", "--json")
        result = json.loads(proc.stdout)
        assert result["edge_count"] == 5  # one per same-named candidate
        assert result["min_confidence_applied"] is None

    def test_custom_min_confidence_threshold(self, tmp_path, run_cli):
        write_files(tmp_path, self.AMBIGUOUS_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "caller", "--depth", "2",
                        "--min-confidence", "0.3", "--json")
        result = json.loads(proc.stdout)
        assert result["edge_count"] == 5  # 0.35 >= 0.3, so kept this time


class TestResolutionEdgeCases:
    def test_no_match_prints_clear_message(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "nonexistent_symbol_xyz")
        assert proc.returncode == 0
        assert "no node matches" in proc.stdout.lower()

    def test_ambiguous_symbol_lists_candidates_json(self, tmp_path, run_cli):
        write_files(tmp_path, {
            "a.py": "def dup():\n    return 1\n",
            "b.py": "def dup():\n    return 2\n",
        })
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "dup", "--json")
        result = json.loads(proc.stdout)
        assert len(result["ambiguous"]) == 2

    def test_isolated_symbol_has_no_edges_and_is_not_a_cut_vertex(self, tmp_path, run_cli):
        write_files(tmp_path, {"a.py": "def lonely():\n    return 1\n"})
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "lonely", "--json")
        result = json.loads(proc.stdout)
        assert result["node_count"] == 1
        assert result["edge_count"] == 0
        assert result["focus_is_cut_vertex"] is False
        assert result["cycles_through_focus"] == []


class TestNodeCap:
    # A star: hub calls 20 distinct leaves. Depth 1 around hub naturally reaches all
    # 21 nodes (hub + 20 leaves) -- capping max_nodes at 10 must cut it off and say so.
    def test_max_nodes_cap_truncates_and_flags_it(self, tmp_path, run_cli):
        leaves = "\n".join(f"def leaf{i}():\n    return {i}\n" for i in range(20))
        calls = "\n".join(f"    leaf{i}()" for i in range(20))
        write_files(tmp_path, {
            "leaves.py": leaves,
            "hub.py": f"from leaves import *\n\ndef hub():\n{calls}\n",
        })
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "hub", "--depth", "1", "--max-nodes", "10", "--json")
        result = json.loads(proc.stdout)
        assert result["truncated"] is True
        assert result["node_count"] == 10
        assert result["max_nodes"] == 10

    def test_below_cap_is_not_flagged_truncated(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--depth", "3", "--json")
        result = json.loads(proc.stdout)
        assert result["truncated"] is False


class TestJsonShape:
    def test_json_has_expected_top_level_keys(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--json")
        result = json.loads(proc.stdout)
        for key in ("focus", "focus_id", "depth", "min_confidence_applied",
                    "node_count", "edge_count", "truncated", "max_nodes",
                    "nodes", "edges", "cut_vertices",
                    "focus_is_cut_vertex", "components_if_focus_removed",
                    "cycles_through_focus"):
            assert key in result, f"missing key: {key}"

    def test_default_depth_is_2(self, tmp_path, run_cli):
        write_files(tmp_path, CYCLE_FIXTURE)
        run_cli(tmp_path)
        proc = run_cli(tmp_path, "--subgraph", "a_func", "--json")
        result = json.loads(proc.stdout)
        assert result["depth"] == 2
