"""file -> file dependency aggregation (graph.json `file_deps`) and the --file-deps
query. calls/inherits edges are collapsed into one weighted edge per (source file,
target file) pair, stored separately from `edges` so nothing else is affected.
"""
import json

from conftest import write_files


def _project(root):
    write_files(root, {
        "src/util.py": "def fmt(v):\n    return str(v)\n\ndef parse(s):\n    return s.strip()\n",
        "src/core.py": (
            "from src.util import fmt, parse\n\n"
            "def render(v):\n"
            "    return fmt(v)\n\n"
            "def load(s):\n"
            "    return parse(s)\n"
        ),
        "src/app.py": (
            "from src.core import render\n\n"
            "def main():\n"
            "    return render(1)\n"
        ),
    })


def test_graph_json_has_file_deps(tmp_path, run_cli, graph_json):
    _project(tmp_path)
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    assert "file_deps" in g
    nm = {n["id"]: n["path"] for n in g["nodes"]}
    pairs = {(nm[d["source"]], nm[d["target"]]): d["weight"] for d in g["file_deps"]}
    # core.py calls into util.py (fmt + parse) -> weight 2
    assert pairs.get(("src/core.py", "src/util.py")) == 2
    # app.py calls into core.py (render) -> weight 1
    assert pairs.get(("src/app.py", "src/core.py")) == 1
    # no self edges, no edge for a pair with no cross-file call
    assert all(s != t for (s, t) in pairs)


def test_file_deps_not_mixed_into_edges(tmp_path, run_cli, graph_json):
    _project(tmp_path)
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    assert all(e["type"] in ("contains", "calls", "inherits") for e in g["edges"])
    assert "depends_on" not in {e["type"] for e in g["edges"]}


def test_file_deps_query_by_file(tmp_path, run_cli):
    _project(tmp_path)
    run_cli(tmp_path)
    data = json.loads(run_cli(tmp_path, "--file-deps", "core.py", "--json").stdout)
    assert data["file"] == "src/core.py"
    assert {d["path"] for d in data["depends_on"]} == {"src/util.py"}
    assert {d["path"] for d in data["depended_on_by"]} == {"src/app.py"}


def test_file_deps_query_by_symbol_resolves_to_its_file(tmp_path, run_cli):
    _project(tmp_path)
    run_cli(tmp_path)
    data = json.loads(run_cli(tmp_path, "--file-deps", "render", "--json").stdout)
    assert data["file"] == "src/core.py"


def test_file_deps_whole_project_list(tmp_path, run_cli):
    _project(tmp_path)
    run_cli(tmp_path)
    data = json.loads(run_cli(tmp_path, "--file-deps", "--json").stdout)
    deps = {(d["source"], d["target"]): d["weight"] for d in data["file_deps"]}
    assert deps[("src/core.py", "src/util.py")] == 2
    # heaviest first
    weights = [d["weight"] for d in data["file_deps"]]
    assert weights == sorted(weights, reverse=True)
