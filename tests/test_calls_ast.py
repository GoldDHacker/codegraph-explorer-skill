r"""AST-driven `calls` resolution for JS/TS (the v5 line).

Instead of scanning a function body with `re.findall(r"\bname\(")`, the tree-sitter
JS/TS walk now emits real `call_expression` / `new_expression` call sites (name +
receiver + line), and resolve_references() attributes each to the innermost function
whose span covers it. This kills the classic false positives (`if (`, `while (`,
`switch (`, `catch (`) and lets `this.m()` resolve to a same-class method.

Every test skips cleanly when the tree-sitter JS/TS grammars aren't installed -- the
regex fallback still runs for those files and is covered by test_calls_resolution.py.
"""
import pytest

from conftest import write_files


def _require_ts(cb):
    if "typescript" not in cb.TREE_SITTER_LANGS:
        pytest.skip("tree-sitter-typescript not installed")


def _calls(graph):
    nm = {n["id"]: n for n in graph["nodes"]}
    out = []
    for e in graph["edges"]:
        if e["type"] == "calls":
            out.append((nm[e["source"]]["name"], nm[e["target"]]["name"], e["tag"],
                        e["confidence"], nm[e["target"]]["path"],
                        (e.get("metadata") or {}).get("resolved_by")))
    return out


def test_control_flow_keywords_are_not_calls(tmp_path, run_cli, graph_json, cb):
    _require_ts(cb)
    write_files(tmp_path, {
        "a.ts": (
            "export function run(x: number) {\n"
            "  if (x) { return 1; }\n"
            "  while (x > 0) { x--; }\n"
            "  switch (x) { case 1: break; }\n"
            "  try { doThing(); } catch (e) { handle(e); }\n"
            "  return 0;\n"
            "}\n"
            "function doThing() {}\n"
            "function handle(e: any) {}\n"
        ),
    })
    run_cli(tmp_path)
    calls = _calls(graph_json(tmp_path))
    targets = {t for _, t, *_ in calls}
    assert "if" not in targets and "while" not in targets
    assert "switch" not in targets and "catch" not in targets
    # the real calls inside the try/catch block are still found
    pairs = {(s, t) for s, t, *_ in calls}
    assert ("run", "doThing") in pairs
    assert ("run", "handle") in pairs


def test_this_method_resolves_to_same_class(tmp_path, run_cli, graph_json, cb):
    _require_ts(cb)
    write_files(tmp_path, {
        "svc.ts": (
            "export class A {\n"
            "  first() { return this.second(); }\n"
            "  second() { return 2; }\n"
            "}\n"
            "export function second() { return 'decoy'; }\n"  # same name, module scope
        ),
    })
    run_cli(tmp_path)
    calls = _calls(graph_json(tmp_path))
    match = [c for c in calls if c[0] == "first" and c[1] == "second"]
    assert len(match) == 1, calls
    assert match[0][2] == "RESOLVED"
    assert match[0][5] == "this_method"


def test_call_inside_callback_is_credited_to_enclosing_method(tmp_path, run_cli, graph_json, cb):
    _require_ts(cb)
    write_files(tmp_path, {
        "svc.ts": (
            "export class Repo {\n"
            "  load(ids: string[]) {\n"
            "    return ids.map(id => this.hydrate(id));\n"
            "  }\n"
            "  hydrate(id: string) { return id; }\n"
            "}\n"
        ),
    })
    run_cli(tmp_path)
    calls = _calls(graph_json(tmp_path))
    # the call sits lexically inside an arrow callback, which has no node of its own --
    # it must be credited to load(), not dropped
    assert ("load", "hydrate") in {(s, t) for s, t, *_ in calls}


def test_bare_call_prefers_free_function_over_same_name_method(tmp_path, run_cli, graph_json, cb):
    _require_ts(cb)
    write_files(tmp_path, {
        "m.ts": (
            "function build() { return 1; }\n"          # free function
            "export class Factory {\n"
            "  build() { return 2; }\n"                 # same-name method
            "  run() { return build(); }\n"             # bare call -> the free one
            "}\n"
        ),
    })
    run_cli(tmp_path)
    calls = _calls(graph_json(tmp_path))
    match = [c for c in calls if c[0] == "run" and c[1] == "build"]
    assert len(match) == 1, calls
    assert match[0][2] == "RESOLVED" and match[0][5] == "same_file"


def test_new_expression_links_to_constructor_class(tmp_path, run_cli, graph_json, cb):
    _require_ts(cb)
    write_files(tmp_path, {
        "main.ts": (
            "export class Widget {}\n"
            "export function make() { return new Widget(); }\n"
        ),
    })
    run_cli(tmp_path)
    calls = _calls(graph_json(tmp_path))
    assert ("make", "Widget") in {(s, t) for s, t, *_ in calls}


def test_import_resolved_call_across_files(tmp_path, run_cli, graph_json, cb):
    _require_ts(cb)
    write_files(tmp_path, {
        "util.ts": "export function fmt(v: number) { return String(v); }\n",
        "use.ts": (
            "import { fmt } from './util';\n"
            "export function show(n: number) { return fmt(n); }\n"
        ),
    })
    run_cli(tmp_path)
    calls = _calls(graph_json(tmp_path))
    match = [c for c in calls if c[0] == "show" and c[1] == "fmt"]
    assert len(match) == 1
    assert match[0][2] == "RESOLVED" and match[0][5] == "import"
    assert match[0][4] == "util.ts"
