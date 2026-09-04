"""Regression tests for the Go/Rust/C/C++/PHP tree-sitter extraction added in this
round -- one class per language, covering the specific thing that made adding
tree-sitter for that language worthwhile (not just "it parses"): Go's grouped
`import (...)` block (invisible to the regex engine entirely), Rust's `impl Trait for
Type` -> inherits edge and same-file-only limitation, C's multi-line function signature
(invisible to the regex engine), C++ class inheritance and method-body calls, PHP's
extends+implements -> two inherits edges and its four require/include variants.

Skipped automatically wherever the corresponding grammar package isn't installed."""
import pytest

from conftest import write_files


def _node(graph, name, ntype=None):
    matches = [n for n in graph["nodes"] if n["name"] == name and (ntype is None or n["type"] == ntype)]
    assert matches, f"no node named {name!r} (type={ntype}) found; names present: {[n['name'] for n in graph['nodes']]}"
    return matches[0]


def _edge(graph, etype, src_name, tgt_name):
    src = [n for n in graph["nodes"] if n["name"] == src_name]
    tgt = [n for n in graph["nodes"] if n["name"] == tgt_name]
    assert src and tgt, f"couldn't find endpoints for {src_name}->{tgt_name}"
    src_ids = {n["id"] for n in src}
    tgt_ids = {n["id"] for n in tgt}
    return [e for e in graph["edges"] if e["type"] == etype and e["source"] in src_ids and e["target"] in tgt_ids]


class TestGo:
    def _require(self, cb):
        if "go" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-go not installed")

    def test_grouped_import_block_captured(self, tmp_path, run_cli, graph_json, cb):
        """The regex engine's Go 'single_import' pattern only matches single-line
        `import "pkg"` -- a grouped `import (...)` block is documented as producing
        zero import nodes at all. tree-sitter closes this entirely."""
        self._require(cb)
        write_files(tmp_path, {"src/main.go": (
            "package main\n\n"
            "import (\n"
            "    \"fmt\"\n"
            "    \"os\"\n"
            ")\n\n"
            "func main() {\n"
            "    fmt.Println(os.Args)\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        imports = {n["name"] for n in g["nodes"] if n["type"] == "import"}
        assert imports == {"fmt", "os"}

    def test_method_scoped_to_receiver_type(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/svc.go": (
            "package main\n\n"
            "type Service struct{}\n\n"
            "func (s *Service) Run() {}\n\n"
            "func helper() {}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        run_node = _node(g, "Run", "function")
        assert run_node["metadata"]["class"] == "Service"
        assert run_node["metadata"]["kind"] == "method"
        helper_node = _node(g, "helper", "function")
        assert "class" not in helper_node["metadata"]

    def test_exported_vs_unexported_by_capitalization(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/x.go": (
            "package main\n\n"
            "func Exported() {}\n"
            "func unexported() {}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _node(g, "Exported", "function")["metadata"]["is_public"] is True
        assert _node(g, "unexported", "function")["metadata"]["is_public"] is False


class TestRust:
    def _require(self, cb):
        if "rust" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-rust not installed")

    def test_trait_impl_becomes_inherits_edge(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/lib.rs": (
            "pub trait Speaker { fn speak(&self) -> String; }\n\n"
            "pub struct Widget { pub id: u32 }\n\n"
            "impl Speaker for Widget {\n"
            "    fn speak(&self) -> String { format!(\"{}\", self.id) }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        edges = _edge(g, "inherits", "Widget", "Speaker")
        assert edges, "expected an inherits edge from Widget to Speaker via the trait impl"

    def test_inherent_impl_method_scoped_and_calls_resolve(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/lib.rs": (
            "pub struct Widget { pub id: u32 }\n\n"
            "impl Widget {\n"
            "    pub fn describe(&self) -> String { self.raw() }\n"
            "    fn raw(&self) -> String { format!(\"{}\", self.id) }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        describe = _node(g, "describe", "function")
        raw = _node(g, "raw", "function")
        assert describe["metadata"]["class"] == "Widget"
        assert raw["metadata"]["is_public"] is False
        calls = _edge(g, "calls", "describe", "raw")
        assert calls

    def test_async_fn_detected(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/lib.rs": "pub async fn fetch() -> String { String::new() }\n"})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _node(g, "fetch", "function")["metadata"]["is_async"] is True

    def test_grouped_use_declaration_captured_in_full(self, tmp_path, run_cli, graph_json, cb):
        """A regression-guard for the regex engine's limitation: its 'use' pattern
        ([\\w:]+ only) truncates a grouped `use std::fmt::{self, Display};` at the
        brace. tree-sitter captures the full argument text instead."""
        self._require(cb)
        write_files(tmp_path, {"src/lib.rs": "use std::fmt::{self, Display};\n\npub fn noop() {}\n"})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        imports = [n for n in g["nodes"] if n["type"] == "import"]
        assert imports and "Display" in imports[0]["name"]


class TestC:
    def _require(self, cb):
        if "c" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-c not installed")

    def test_multiline_signature_captured(self, tmp_path, run_cli, graph_json, cb):
        """The regex engine's C pattern requires the whole signature -- return type,
        name, params, opening brace -- on one line. A real parser has no such limit."""
        self._require(cb)
        write_files(tmp_path, {"src/util.c": (
            "int add(\n"
            "    int a,\n"
            "    int b\n"
            ") {\n"
            "    return a + b;\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _node(g, "add", "function")

    def test_static_function_is_not_public(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/util.c": "static void helper(void) {}\nint pub_fn(void) { return 0; }\n"})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _node(g, "helper", "function")["metadata"]["is_public"] is False
        assert _node(g, "pub_fn", "function")["metadata"]["is_public"] is True

    def test_pointer_return_type_declarator_unwrapped(self, tmp_path, run_cli, graph_json, cb):
        """`char* get_name(...)` wraps the function_declarator in a pointer_declarator
        -- the name must still be found, not silently dropped."""
        self._require(cb)
        write_files(tmp_path, {"src/util.c": "char* get_name(void) { return \"x\"; }\n"})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _node(g, "get_name", "function")


class TestCpp:
    def _require(self, cb):
        if "cpp" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-cpp not installed")

    def test_class_inheritance_captured(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/shape.cpp": (
            "class Base {\npublic:\n    virtual void run() {}\n};\n\n"
            "class Derived : public Base, private Helper {\npublic:\n    void run() override {}\n};\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        derived = _node(g, "Derived", "class")
        assert set(derived["metadata"]["bases"]) == {"Base", "Helper"}
        edges = _edge(g, "inherits", "Derived", "Base")
        assert edges

    def test_method_calls_resolve_through_class_body(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/service.cpp": (
            "class Service {\n"
            "public:\n"
            "    std::string login() { return greet(); }\n"
            "private:\n"
            "    std::string greet() { return \"hi\"; }\n"
            "};\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _edge(g, "calls", "login", "greet")

    def test_forward_declared_struct_not_duplicated(self, tmp_path, run_cli, graph_json, cb):
        """A bare `struct Point p;` reference also parses as struct_specifier but has
        no field_declaration_list body -- it must not produce a spurious extra node."""
        self._require(cb)
        write_files(tmp_path, {"src/x.cpp": (
            "struct Point { int x; int y; };\n\n"
            "void useIt() {\n"
            "    struct Point p;\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        points = [n for n in g["nodes"] if n["name"] == "Point"]
        assert len(points) == 1


class TestPhp:
    def _require(self, cb):
        if "php" not in cb.TREE_SITTER_LANGS:
            pytest.skip("tree-sitter-php not installed")

    def test_extends_and_implements_both_become_inherits(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/service.php": (
            "<?php\n"
            "class BaseService { public function run() {} }\n"
            "interface Speaker { public function speak(); }\n"
            "class AuthService extends BaseService implements Speaker {\n"
            "    public function speak() { return $this->run(); }\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        assert _edge(g, "inherits", "AuthService", "BaseService")
        assert _edge(g, "inherits", "AuthService", "Speaker")

    @pytest.mark.parametrize("keyword", ["require", "require_once", "include", "include_once"])
    def test_all_four_include_variants_captured(self, tmp_path, run_cli, graph_json, cb, keyword):
        self._require(cb)
        write_files(tmp_path, {"src/x.php": f"<?php\n{keyword} 'dep.php';\n"})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        imports = [n for n in g["nodes"] if n["type"] == "import"]
        assert imports and imports[0]["name"] == "dep.php"

    def test_static_and_visibility_on_methods(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        write_files(tmp_path, {"src/x.php": (
            "<?php\n"
            "class Thing {\n"
            "    private static function helper() { return true; }\n"
            "    public function pub() {}\n"
            "}\n"
        )})
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        helper = _node(g, "helper", "function")
        assert helper["metadata"]["is_static"] is True
        assert helper["metadata"]["is_public"] is False
        assert _node(g, "pub", "function")["metadata"]["is_public"] is True
