"""Python extraction via the stdlib ast module -- the most precise path (real syntax
tree, exact line_end via end_lineno), and the one every other language's regex path
falls back toward in spirit. Covers class inheritance resolution, the exact
if __name__ == "__main__": entrypoint span, and the ast->regex fallback on a real
SyntaxError."""
from conftest import write_files


def _node(graph, name, ntype=None):
    matches = [n for n in graph["nodes"] if n["name"] == name and (ntype is None or n["type"] == ntype)]
    assert matches, f"no node named {name!r} (type={ntype}) found"
    return matches[0]


def test_function_and_class_extraction(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {"src/app.py": (
        "class Base:\n"
        "    pass\n\n"
        "class Derived(Base):\n"
        "    def method(self):\n"
        "        return 1\n"
    )})
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    derived = _node(g, "Derived", "class")
    assert derived["metadata"]["bases"] == ["Base"]


def test_inheritance_edge_resolved(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {"src/app.py": (
        "class Base:\n"
        "    pass\n\n"
        "class Derived(Base):\n"
        "    pass\n"
    )})
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    base = _node(g, "Base", "class")
    derived = _node(g, "Derived", "class")
    inherits = [e for e in g["edges"] if e["type"] == "inherits"
                and e["source"] == derived["id"] and e["target"] == base["id"]]
    assert inherits, "expected an inherits edge from Derived to Base"


def test_main_guard_gets_exact_span(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {"src/app.py": (
        "def helper():\n"
        "    pass\n\n"
        "if __name__ == \"__main__\":\n"
        "    helper()\n"
    )})
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    entry = [n for n in g["nodes"] if n["type"] == "entrypoint"][0]
    assert entry["line_start"] == 4  # the "if __name__..." line itself
    assert entry["line_end"] == 5    # the "helper()" line


def test_calls_edge_from_main_guard_resolves(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {"src/app.py": (
        "def helper():\n"
        "    pass\n\n"
        "if __name__ == \"__main__\":\n"
        "    helper()\n"
    )})
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    helper = _node(g, "helper", "function")
    entry = [n for n in g["nodes"] if n["type"] == "entrypoint"][0]
    calls = [e for e in g["edges"] if e["type"] == "calls"
             and e["source"] == entry["id"] and e["target"] == helper["id"]]
    assert calls


def test_syntax_error_falls_back_to_regex(tmp_path, run_cli, graph_json):
    """A real SyntaxError must not crash the build -- extract_python_ast() returns
    False and extract_regex() picks up what it can instead."""
    write_files(tmp_path, {"src/broken.py": (
        "def totally_broken(\n"
        "    this is not valid python at all !!!\n"
    )})
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_custom_patterns_json_runs_alongside_ast(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "src/views.py": "@app.route('/health')\ndef health():\n    pass\n",
        ".codegraph/custom_patterns.json": '{"python": {"route": "@app\\\\.route\\\\([\'\\"]([^\'\\"]+)[\'\\"]"}}',
    })
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    route_nodes = [n for n in g["nodes"] if n.get("metadata", {}).get("kind") == "route"]
    assert route_nodes, "expected a custom_patterns.json 'route' match"
    assert route_nodes[0]["name"] == "/health"
