"""Regression tests for the tree-sitter extraction engine (v4.0, JS/TS/Java), pinning
down the exact bugs found and fixed while building it -- each of these silently produced
wrong data before the fix, none of them crashed, which is exactly why they need a
standing test rather than relying on someone noticing by eye again.

Skipped automatically wherever the corresponding grammar package isn't installed --
these are regression tests for the tree-sitter path specifically, not for the regex
fallback (see test_python_ast.py and the language coverage notes in extraction_patterns.md
for what's expected when tree-sitter is absent)."""
import pytest

from conftest import write_files


def _node(graph, name, ntype=None):
    matches = [n for n in graph["nodes"] if n["name"] == name and (ntype is None or n["type"] == ntype)]
    assert matches, f"no node named {name!r} (type={ntype}) in graph; names present: {[n['name'] for n in graph['nodes']]}"
    return matches[0]


def test_java_available(cb):
    if "java" not in cb.TREE_SITTER_LANGS:
        pytest.skip("tree-sitter-java not installed")


def test_ts_available(cb):
    if "typescript" not in cb.TREE_SITTER_LANGS:
        pytest.skip("tree-sitter-typescript not installed")


@pytest.mark.usefixtures("cb")
class TestJava:
    def _require(self, cb):
        if "java" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-java not installed")

    def test_annotation_line_does_not_leak_into_signature(self, tmp_path, run_cli, graph_json, cb):
        """Bug: a method/constructor node's own span can start on a preceding
        @Annotation line, so reading the node's first line for `signature` returned
        the annotation text ('@Override') instead of the real declaration. Fixed via
        decl_line(), which anchors on the declared name's own source line instead."""
        self._require(cb)
        write_files(tmp_path, {"src/Service.java": (
            "public class Service extends Base {\n"
            "    @Override\n"
            "    public void run() {\n"
            "        helper();\n"
            "    }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        run_node = _node(g, "run", "function")
        assert run_node["metadata"]["signature"] == "public void run() {"
        assert "@Override" not in run_node["metadata"]["signature"]

    def test_method_is_public_and_class_metadata(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/Widget.java": (
            "public class Widget {\n"
            "    private int helper() { return 1; }\n"
            "    public int pub() { return helper(); }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        helper = _node(g, "helper", "function")
        pub = _node(g, "pub", "function")
        assert helper["metadata"]["is_public"] is False
        assert pub["metadata"]["is_public"] is True
        assert helper["metadata"]["class"] == "Widget"

    def test_main_entrypoint_gets_a_narrow_span_not_whole_file(self, tmp_path, run_cli, graph_json, cb):
        """With tree-sitter active, Java's main() is a real function node, so the
        generic entrypoint-span-borrowing logic should give it whole_file: false --
        unlike the regex-only fallback, which never has a method node to borrow from."""
        self._require(cb)
        write_files(tmp_path, {"src/Main.java": (
            "public class Main {\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(\"hi\");\n"
            "    }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        entry = [n for n in g["nodes"] if n["type"] == "entrypoint"][0]
        assert entry["metadata"]["whole_file"] is False


@pytest.mark.usefixtures("cb")
class TestTypeScript:
    def _require(self, cb):
        if "typescript" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-typescript not installed")

    def test_accessibility_modifier_sets_is_public_false(self, tmp_path, run_cli, graph_json, cb):
        """Bug: TS wraps private/protected/public in a distinct accessibility_modifier
        child node rather than a bare token type like 'static'/'async', so the initial
        `is_public = not name.startswith('#')` check missed it entirely and marked
        `private buildMessage(...)` as public. Fixed via js_is_public()."""
        self._require(cb)
        write_files(tmp_path, {"src/greeter.ts": (
            "export class Greeter {\n"
            "    private buildMessage(name: string): string {\n"
            "        return 'hi ' + name;\n"
            "    }\n"
            "    public greet(name: string): string {\n"
            "        return this.buildMessage(name);\n"
            "    }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        build_msg = _node(g, "buildMessage", "function")
        greet = _node(g, "greet", "function")
        assert build_msg["metadata"]["is_public"] is False
        assert greet["metadata"]["is_public"] is True

    def test_exported_literal_const_not_dropped(self, tmp_path, run_cli, graph_json, cb):
        """Bug: the tree-sitter lexical_declaration handler only captured arrow/
        function-valued declarators, silently dropping a plain-literal exported const
        that the regex engine's 'export' fallback pattern used to catch (e.g.
        `export const DEFAULT_CURRENCY = 'USD';`). Found by diffing a tree-sitter build
        against a forced-regex-only build of the same fixture -- a node-count-only
        check would have missed this, since an unrelated gain (new method nodes) made
        the total come out equal by coincidence."""
        self._require(cb)
        write_files(tmp_path, {"src/config.ts": (
            "export const DEFAULT_CURRENCY = 'USD';\n"
            "export const helper = () => DEFAULT_CURRENCY;\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        const_node = _node(g, "DEFAULT_CURRENCY", "variable")
        assert const_node["metadata"]["is_public"] is True
        arrow_node = _node(g, "helper", "function")
        assert arrow_node["metadata"]["kind"] == "arrow"

    def test_class_field_arrow_method_captured(self, tmp_path, run_cli, graph_json, cb):
        """React/Node auto-bound-method pattern: `foo = (x) => {...}` inside a class
        body -- invisible to the regex engine's top-level-only arrow pattern."""
        self._require(cb)
        write_files(tmp_path, {"src/component.tsx": (
            "export class Button {\n"
            "    onClick = (e) => {\n"
            "        this.handle(e);\n"
            "    }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        on_click = _node(g, "onClick", "function")
        assert on_click["metadata"]["kind"] == "method"
        assert on_click["metadata"]["class"] == "Button"

    def test_calls_edge_resolves_from_inside_a_method_body(self, tmp_path, run_cli, graph_json, cb):
        """The whole point of tree-sitter here: a calls edge originating *inside* a
        class method body, invisible to the pre-v4.0 regex engine entirely."""
        self._require(cb)
        write_files(tmp_path, {"src/greeter.ts": (
            "export class Greeter {\n"
            "    private buildMessage(name: string): string {\n"
            "        return 'hi ' + name;\n"
            "    }\n"
            "    public greet(name: string): string {\n"
            "        return this.buildMessage(name);\n"
            "    }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        greet = _node(g, "greet", "function")
        build_msg = _node(g, "buildMessage", "function")
        calls = [e for e in g["edges"] if e["type"] == "calls"
                 and e["source"] == greet["id"] and e["target"] == build_msg["id"]]
        assert calls, "expected a calls edge from greet() to buildMessage(), originating inside greet()'s body"


def test_tree_sitter_falls_back_to_regex_on_unparseable_content(tmp_path, run_cli, graph_json, cb):
    """A file with genuinely broken syntax must not crash the build -- tree-sitter's
    error-tolerant parser rarely raises, but extract_file() must still degrade to the
    regex path (or simply extract what a partial tree yields) rather than losing the
    file's other, valid declarations."""
    if not cb.TREE_SITTER_LANGS:
        pytest.skip("no tree-sitter grammars installed")
    write_files(tmp_path, {"src/bad.js": (
        "function wellFormed() { return 1; }\n"
        "function broken( {{{ this is not valid js at all\n"
    )})
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    g = graph_json(tmp_path)
    names = {n["name"] for n in g["nodes"]}
    assert "wellFormed" in names
