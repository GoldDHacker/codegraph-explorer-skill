"""v4.9: the workspace-package `calls`-resolution tier. A bare JS/TS specifier that is
a sibling workspace package's `name` (pnpm-workspace.yaml, or a root package.json
`workspaces`) resolves to that package's own source tree -- tag RESOLVED,
`resolved_by: "workspace_import"`, confidence 0.9 -- instead of falling into the
project-wide ambiguity heuristic (INFERRED). Directory-scoped, because re-exports
through the package entrypoint aren't tracked as edges.
"""
from conftest import write_files


def _calls(graph, src_name, tgt_name):
    id_to_node = {n["id"]: n for n in graph["nodes"]}
    return [
        (id_to_node[e["source"]], id_to_node[e["target"]], e)
        for e in graph["edges"]
        if e["type"] == "calls"
        and id_to_node[e["source"]]["name"] == src_name
        and id_to_node[e["target"]]["name"] == tgt_name
    ]


_PKG_A = {
    "packages/pkg-a/package.json": '{"name": "@scope/pkg-a", "version": "1.0.0"}\n',
    "packages/pkg-a/src/index.ts": "export { doThing } from './impl.ts'\n",
    "packages/pkg-a/src/impl.ts": "export function doThing() { return 'a' }\n",
}
# an unrelated package that also exports a `doThing` -- the distractor the old
# project-wide heuristic could not tell apart from the real target.
_PKG_C = {
    "packages/pkg-c/package.json": '{"name": "@scope/pkg-c", "version": "1.0.0"}\n',
    "packages/pkg-c/src/thing.ts": "export function doThing() { return 'c' }\n",
}


def test_pnpm_workspace_bare_specifier_resolves_to_the_imported_package(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n",
        **_PKG_A,
        **_PKG_C,
        "packages/pkg-b/package.json": '{"name": "@scope/pkg-b", "version": "1.0.0"}\n',
        "packages/pkg-b/src/run.ts": (
            "import { doThing } from '@scope/pkg-a'\n"
            "export function run() { return doThing() }\n"
        ),
    })
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    g = graph_json(tmp_path)

    edges = _calls(g, "run", "doThing")
    assert len(edges) == 1, [(t["path"], e["tag"]) for _s, t, e in edges]
    _src, tgt, e = edges[0]
    assert tgt["path"] == "packages/pkg-a/src/impl.ts"
    assert e["tag"] == "RESOLVED"
    assert e["metadata"]["resolved_by"] == "workspace_import"
    assert e["confidence"] == 0.9


def test_npm_workspaces_array_in_root_package_json_also_works(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "package.json": '{"name": "root", "private": true, "workspaces": ["packages/*"]}\n',
        **_PKG_A,
        **_PKG_C,
        "packages/pkg-b/package.json": '{"name": "@scope/pkg-b", "version": "1.0.0"}\n',
        "packages/pkg-b/src/run.ts": (
            "import { doThing } from '@scope/pkg-a'\n"
            "export function run() { return doThing() }\n"
        ),
    })
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    edges = _calls(g, "run", "doThing")
    assert len(edges) == 1
    _s, tgt, e = edges[0]
    assert tgt["path"] == "packages/pkg-a/src/impl.ts"
    assert e["metadata"]["resolved_by"] == "workspace_import"


def test_subpath_specifier_resolves_to_the_package(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n",
        **_PKG_A,
        **_PKG_C,
        "packages/pkg-b/package.json": '{"name": "@scope/pkg-b", "version": "1.0.0"}\n',
        "packages/pkg-b/src/run.ts": (
            "import { doThing } from '@scope/pkg-a/dist/impl'\n"
            "export function run() { return doThing() }\n"
        ),
    })
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    edges = _calls(g, "run", "doThing")
    assert len(edges) == 1
    _s, tgt, e = edges[0]
    assert tgt["path"] == "packages/pkg-a/src/impl.ts"
    assert e["metadata"]["resolved_by"] == "workspace_import"


def test_no_workspace_config_leaves_the_bare_specifier_unresolved(tmp_path, run_cli, graph_json):
    """Without pnpm-workspace.yaml / workspaces, the tier is a no-op: the ambiguous
    call falls back to the pre-v4.9 INFERRED heuristic over both same-named symbols."""
    write_files(tmp_path, {
        **_PKG_A,
        **_PKG_C,
        "packages/pkg-b/package.json": '{"name": "@scope/pkg-b", "version": "1.0.0"}\n',
        "packages/pkg-b/src/run.ts": (
            "import { doThing } from '@scope/pkg-a'\n"
            "export function run() { return doThing() }\n"
        ),
    })
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    edges = _calls(g, "run", "doThing")
    assert edges, "expected the pre-v4.9 fallback edge(s)"
    assert all(e["tag"] == "INFERRED" for _s, _t, e in edges)
    assert all(e["metadata"].get("resolved_by") != "workspace_import" for _s, _t, e in edges)


def test_ambiguous_within_the_imported_package_narrows_but_does_not_leak(tmp_path, run_cli, graph_json):
    """Two `doThing`s inside pkg-a, one in pkg-c. Importing @scope/pkg-a narrows the
    INFERRED set to pkg-a's two -- pkg-c's must not receive an edge."""
    write_files(tmp_path, {
        "pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n",
        "packages/pkg-a/package.json": '{"name": "@scope/pkg-a", "version": "1.0.0"}\n',
        "packages/pkg-a/src/one.ts": "export function doThing() { return 1 }\n",
        "packages/pkg-a/src/two.ts": "export function doThing() { return 2 }\n",
        **_PKG_C,
        "packages/pkg-b/package.json": '{"name": "@scope/pkg-b", "version": "1.0.0"}\n',
        "packages/pkg-b/src/run.ts": (
            "import { doThing } from '@scope/pkg-a'\n"
            "export function run() { return doThing() }\n"
        ),
    })
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    edges = _calls(g, "run", "doThing")
    hit_paths = {t["path"] for _s, t, _e in edges}
    assert hit_paths <= {"packages/pkg-a/src/one.ts", "packages/pkg-a/src/two.ts"}
    assert "packages/pkg-c/src/thing.ts" not in hit_paths
