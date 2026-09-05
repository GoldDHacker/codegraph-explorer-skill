# Graph Schema Reference (v4.6)

This describes the actual shape `codegraph_builder.py` writes to `.codegraph/graph.json`
— every field below is produced by the current script; nothing here is aspirational.
This shape is identical whether a node/edge came from the AST/regex engines described
here or from tree-sitter (JS/TS/Java since v4.0; Go/Rust/C/C++/PHP added v4.2 —
all optional, per-language grammar packages) — see
`references/extraction_patterns.md` for which engine produces what per language. A
second, parallel representation of the same data also exists in
`.codegraph/graph_db/` (Ladybug/Cypher, v4.0, optional) — see "Parallel graph database"
at the bottom of this document.

## Node Object
```json
{
  "id": "func:src/main.py:main:10",
  "type": "file|function|class|type|variable|import|entrypoint|symbol",
  "name": "main",
  "path": "src/main.py",
  "line_start": 10,
  "line_end": 12,
  "metadata": {
    "signature": "def main() -> None:",
    "is_async": false,
    "is_public": true,
    "args": ["argv"]
  },
  "community": 1,
  "degree": 15
}
```

Notes:
- `line_end` is precise for Python (from `ast`'s `end_lineno`) and approximate for every
  regex-extracted language (the next declaration's start line, or end of file) — it
  exists to scope "calls" resolution to a symbol's own body, not to be a reliable
  block-boundary in general.
- `type: "symbol"` is the generic fallback used for anything matched by a pattern that
  doesn't map to one of the other categories — every `.codegraph/custom_patterns.json`
  match. Its `metadata.kind` records which pattern matched.
- `export const X = 5;` (a plain exported value, not a function) becomes a
  `type: "variable"` node, not `"symbol"` — corrected here since an earlier version of
  this doc said `"symbol"`. `export const X = () => {...}` (or without `export` at all)
  is different again: it's a `type: "function"` node with `metadata.kind: "arrow"`, same
  as any other function, since v3.3 — see the `javascript`/`typescript` note below.
- There is no `directory` node type in the current output, unlike some earlier drafts of
  this skill implied — communities are computed from directory paths as strings (see
  below), not as their own graph nodes.
- `file` nodes carry `metadata.language` and `metadata.role` (`"test"` when the path
  has a test directory segment — `tests/`, `__tests__/`, `spec/`, `e2e/`, ... — or a
  test filename shape — `test_*.py`, `*_test.go`, `*.test.ts`, `*Test.java`,
  `*_spec.rb`, ... — else `"prod"`). It's a path heuristic used by `--impact` to split
  the blast radius into prod code vs "tests to re-run"; a plain helper module under
  `tests/` counts as test code.
- `metadata` fields vary by node type: `bases` for classes, `kind` for
  interface/type/struct/symbol/arrow-function nodes, `style`/`raw` for import nodes,
  `detected_by`/`whole_file`/`marker_line` for entrypoints (`whole_file`: true when the
  body scanned for "calls" is the entire file rather than a single function's span —
  true for JS/TS, Ruby, PHP, and Java when no method node exists to borrow a span from,
  false for Go/Rust/Python where a precise block is used instead; `marker_line`: the
  line the entrypoint pattern itself matched on, excluded from the "calls" scan either
  way so a self-referencing declaration like `public static void main(` doesn't misread
  as a call to some unrelated same-named function elsewhere in the project).
- JS/TS arrow functions assigned to a `const` are captured as ordinary `function` nodes
  (`metadata.kind: "arrow"`, `metadata.is_async` set correctly for `async (...) =>`)
  regardless of whether the line has `export` on it — this is how most React components
  and Node handler/service code is written, and was invisible before v3.3 unless the
  line also happened to match the `export const X = ...` pattern.
- Class methods in JS/TS/Java (`type: "function"`, since v4.0, tree-sitter only — see
  `extraction_patterns.md`) carry `metadata.class` (the enclosing class name) and
  `metadata.kind` set to `"method"`, `"getter"`, `"setter"`, or `"constructor"`, plus
  `metadata.is_static`. JS/TS additionally sets `metadata.is_public` from either a `#`
  private-field prefix or a TS `accessibility_modifier`; Java sets it from the
  `modifiers` node (package-private, i.e. no modifier keyword, counts as public,
  matching actual Java visibility rules). A class-field arrow method (`foo = (x) =>
  {...}`, the common React/Node auto-bound-method pattern) is captured the same way,
  `metadata.kind: "method"`. Without tree-sitter installed for that language (or on a
  per-file parse failure), these nodes simply don't exist — the regex fallback only ever
  produces top-level/class-level *declaration* nodes for JS/TS/Java, never methods; see
  the Language Coverage table in `SKILL.md`.
- Since v4.2, the same tree-sitter-only method extraction (`type: "function"`,
  `metadata.class` + `metadata.kind: "method"`) also applies to Go (`metadata.class` is
  the receiver type), Rust (`metadata.class` is the enclosing `impl` block's target
  type, for both inherent and trait impls), C/C++ (`metadata.class` set only when the
  function is textually inside a `class`/`struct` body), and PHP. `metadata.bases` on
  `class`/`struct` nodes is populated by tree-sitter for Go (interface satisfaction is
  *not* modeled — Go has no `implements` keyword to anchor on), Rust (a trait impl
  appends the trait name — same-file only, see `extraction_patterns.md`), C++ (from
  `class X : public Base1, private Base2`), and PHP (both `extends` and `implements`
  targets, as separate entries in the same flat list). None of this exists on the regex
  fallback path for these five languages — see `extraction_patterns.md` for exactly
  what each fallback captures instead.

## Edge Object
```json
{
  "id": "e:42",
  "source": "func:src/main.py:main:10",
  "target": "func:src/db.py:connect:5",
  "type": "contains|calls|inherits",
  "tag": "EXTRACTED|INFERRED|RESOLVED",
  "confidence": 0.85,
  "metadata": {}
}
```

Notes:
- Edge types actually produced: `contains` (file → symbol, always `EXTRACTED` except for
  entrypoints which are `INFERRED`), `calls` (`INFERRED` or, since v4.2, `RESOLVED` —
  see below), `inherits` (always `EXTRACTED`). There is no separate
  `imports`/`references`/`depends_on`/`tests`/`documents` edge type today — an import
  shows up as an `import`-typed *node* reached by a `contains` edge from its file, not
  as its own edge type. Don't query for edge types this script doesn't emit.
- `calls` **call sites** come from two places: real tree-sitter `call_expression` /
  `new_expression` nodes for JS/TS (structural — control-flow keywords, comments and
  string literals never register; `a.b.foo()` keeps its receiver `a.b`; a call in an
  anonymous callback is credited to the enclosing named function), and a `\bname(` scan
  of the function body's text for every other language and any file tree-sitter can't
  parse.
- `confidence` on `calls` edges is not a fixed 0.7. Each call site is checked against
  the resolution tiers below before falling back to the pre-v4.2 heuristic:
  0. **Same-class via receiver** (`tag: "RESOLVED"`, `confidence: 0.97`,
     `metadata.resolved_by: "this_method"`, JS/TS only): the call is `this.m()` /
     `self.m()` inside a method and exactly one method named `m` on the caller's own
     class exists in the file.
  1. **Same-file** (`tag: "RESOLVED"`, `confidence: 0.97`, `metadata.resolved_by:
     "same_file"`): exactly one same-named candidate is defined in the caller's own
     file. Works for every language, no import parsing needed. A bare `name(...)` call
     (no receiver) drops class methods from this count when a free function also matches.
  2. **Filesystem-verified import** (`tag: "RESOLVED"`, `confidence: 0.93`,
     `metadata.resolved_by: "import"`): the caller's import resolves, via real
     filesystem lookup (not filename-stem similarity), to exactly one same-named
     candidate's file. Implemented for JS/TS/JSX/TSX relative imports and Python dotted
     module paths only — see `query_protocol.md` for exactly why Go/Rust/Java/PHP/C/C++
     aren't attempted here.
  3. Otherwise `tag: "INFERRED"`, scaling with how many same-named callable candidates
     remain (1 candidate → 0.85, 2-3 → 0.55, 4-8 → 0.35, 9+ → 0.2) — note "remain": when
     tier 2's import resolution narrows the candidate set without reducing it to
     exactly one, this tier counts against that narrowed set, not the whole project —
     then gets a further `+0.25` (capped at `0.95`) if the caller's own file imports
     something whose path looks like the specific candidate's file (matched by filename
     stem — the older, weaker signal, still the only one available for
     Go/Rust/Java/PHP/C/C++) — since v3.3. Neither boost is a different scale: a name
     shared by nine other symbols project-wide still only reaches ~0.45 even with a
     matching import. See `query_protocol.md` for how to weigh all of this when
     answering a query.
- Edges are deduplicated by `(source, target, type)` within a build, so re-running the
  script — full, `--update`, or via watch mode — never produces duplicate edges.

## Graph Object
```json
{
  "version": "4.6",
  "generated_at": "2026-09-01T00:00:00+00:00",
  "project_root": "/path/to/project",
  "stats": {"node_function": 120, "node_class": 45},
  "total_nodes": 1247,
  "total_edges": 3892,
  "total_communities": 12,
  "entrypoints": ["entry:src/main.py"],
  "god_nodes": ["class:src/core.py:Engine:20", "func:src/utils.py:log:5"],
  "nodes": [...],
  "edges": [...],
  "file_deps": [
    {"source": "file:src/core.py", "target": "file:src/util.py", "weight": 4, "via": ["calls"]}
  ],
  "communities": [
    {
      "id": 1,
      "name": "src/auth",
      "nodes": ["id1", "id2"],
      "size": 45,
      "description": "6 function, 2 class, 1 import across files in `src/auth`"
    }
  ]
}
```

Notes:
- `stats` keys are `node_<type>` counts, not a fixed list — read them dynamically.
- `file_deps` (v4.5, #C) is the `calls`/`inherits` edges aggregated to one weighted entry
  per (source file → target file) pair: `source`/`target` are `file` node ids, `weight`
  is the number of distinct symbol-level edges behind the pair, `via` lists which edge
  types contributed. `calls` edges below `confidence` 0.4 are dropped from the
  aggregation (a name shared by several unrelated symbols would otherwise invent a
  file dependency); `inherits` and every `calls` edge at 0.4+ count. It is a *derived
  view*, kept out of `edges` on purpose so every existing edge consumer (communities,
  `degree`, `--impact`, `graph.html`) is unchanged. Absent from graphs built before v4.5.
  Query it with `--file-deps`, don't hand-walk it.
- `god_nodes` excludes `file`-type nodes on purpose: every symbol has a `contains` edge
  back to its file, so file nodes would otherwise trivially dominate by raw degree and
  bury the actually interesting highly-connected functions/classes.
- `communities[].description` is generated by counting node types per grouped node set
  — it is a heuristic summary, not a verified architectural description. Say so if you
  quote it. Since v4.4, `communities[].name`/`nodes` themselves can come from either
  algorithm `--community-algo` selected at build time: the pre-v4.4 directory heuristic
  (`name` is always a directory path, every node in a community lives under that exact
  directory) or, when `python-igraph`+`leidenalg` are installed and `auto`/`leiden`
  selected them, real Leiden clustering over `calls`/`inherits` edges (`name` is the
  most common directory among the cluster's members plus a `"(+N more dirs)"` suffix
  when it spans more than one — a label for humans, not a claim that every member lives
  there; a dedicated `"ungrouped (no calls/inherits connections)"` community holds every
  node the clustering algorithm couldn't connect to anything). `graph.json` itself
  carries no flag saying which algorithm ran — a community name with a `"(+N more
  dirs)"` suffix is the tell that it was Leiden.
- `generated_at` is what Rule 3 (stale detection) in `SKILL.md`/`auto_build_protocol.md`
  should compare against the newest source file's mtime. `graph.json`'s own file mtime
  happens to track this reliably too (verified: only a real build/`save()` ever writes
  `graph.json` -- `--report`/`--export-obsidian`/`--sync-graphdb` all read it via
  `load_existing()` without rewriting it), which is what `--sync-graphdb`'s staleness
  tracking below relies on instead of parsing `generated_at` out of a potentially
  tens-of-MB file on every `--cypher` call.

## Parallel graph database (`.codegraph/graph_db/`, v4.0, optional)

`--sync-graphdb` (requires `pip install ladybug`) exports the same nodes/edges above
into a local Ladybug embedded graph database, queryable with real Cypher via
`--cypher`. This is a second representation of the same data, not a different or richer
one — it exists purely so multi-hop pattern matches and aggregations can be expressed
in Cypher instead of walked by hand in Python, and it never replaces or changes
`graph.json`/the query commands above. The schema is deliberately generic (two tables,
a type-discriminator property on each) rather than one table per node/edge type, so the
graph's evolving type vocabulary — new languages, `custom_patterns.json` matches — never
needs a schema migration:

```cypher
CREATE NODE TABLE Symbol(
  id STRING, name STRING, ntype STRING, path STRING,
  line_start INT64, line_end INT64, community INT64, degree INT64,
  meta STRING,               -- the node's `metadata` object, JSON-encoded as a string
  PRIMARY KEY(id)
)
CREATE REL TABLE Edge(
  FROM Symbol TO Symbol,
  etype STRING, tag STRING, confidence DOUBLE,
  meta STRING                -- the edge's `metadata` object, JSON-encoded as a string
)
```

`ntype`/`etype` hold exactly the same `type`/`tag` vocabulary as `graph.json`'s
`nodes[].type` and `edges[].type` (`"function"`/`"class"`/... ; `"contains"`/`"calls"`/
`"inherits"`) — nothing is renamed or remapped between the two representations.
`line_start`/`line_end`/`community` are `-1` where `graph.json` would have `null`
(Cypher's node-table columns aren't nullable the way JSON fields are). `graph_db/` is
fully rebuilt on every `--sync-graphdb` run (dropped and recreated, bulk-loaded via
`COPY ... FROM <csv> (HEADER=false)` rather than row-by-row `CREATE`, for speed on large
graphs) — note that on the currently-tested Ladybug version (0.20.2) this path is a
single database *file*, not a directory, despite the name; `--sync-graphdb` handles
both shapes when clearing a previous one (an earlier version of this feature used
`shutil.rmtree()` unconditionally, which crashed with `NotADirectoryError` the first
time anyone actually re-ran `--sync-graphdb` against an existing one — fixed by
checking `is_dir()` first).

**Staleness (v4.1)**: `graph_db/` is **not** kept in sync automatically after a
rebuild/`--update` — re-run `--sync-graphdb` whenever `graph.json` changes and Cypher
queries need to see the change. To make this hard to forget silently, every
`--sync-graphdb` records the source `graph.json`'s file mtime (plus a timestamp and
node/edge counts) in `.codegraph/.graphdb_sync_meta.json`; `--cypher` compares that
against the current `graph.json` mtime on every call and, if `graph.json` has been
rebuilt since the last sync, prints a `graph_db/ may be stale -- ...` warning before the
results (folded into the `warning` key of the output when `--json` is used) rather than
silently answering from outdated data. This is informational only — it never blocks the
query, since `--cypher` is a read-only escape hatch, not another shrink-guard. Absence
of the meta file (a `graph_db/` from before this tracking existed, or one whose meta
file was manually removed) means "unknown," not "stale" — no warning is printed either
way in that case.

Known Cypher gotcha: filtering a variable-length relationship pattern
(`[e:Edge*1..2]`) with `all(x IN e WHERE ...)` fails — `e` binds as a `RECURSIVE_REL`,
not the `LIST` the `all()` function expects. Bind a path variable and filter
`relationships(p)` instead:
```cypher
MATCH p = (a:Symbol {name:'AuthService'})-[:Edge*1..2]->(b:Symbol)
WHERE all(x IN relationships(p) WHERE x.etype IN ['calls','inherits'])
RETURN DISTINCT b.name, b.ntype
```

## Graph versioning / diff (`.codegraph/.graph_prev.json`, `.codegraph/snapshots/`, v4.4, no dependency)

Two more files can exist alongside `graph.json`, both plain copies of the same Graph
Object shape described above, never a different schema:

- **`.codegraph/.graph_prev.json`**: rotated automatically by every `save()` — right
  before writing a new `graph.json`, the previous one (if any) is copied here first.
  Single slot, not a log: it only ever holds the one build immediately before the most
  recent one. `--diff` with no name compares the current `graph.json` against this file.
- **`.codegraph/snapshots/<name>.json`**: written only on an explicit `--snapshot
  <name>` — never touched by a normal build. Stays comparable across any number of
  further builds until re-taken. `<name>` is sanitized to `[A-Za-z0-9_.-]` before
  becoming a filename (anything else becomes `_`); `--diff <name>` applies the same
  sanitization when looking the file up, so the two always agree on what a given name
  maps to.

`--diff [<name>]` (add `--json` for structured output) computes:
- **Nodes**: `added`/`removed` by set difference on node `id`; `changed` when the same
  id exists in both but `(line_start, line_end, metadata)` differs.
- **Edges**: `added`/`removed`/`changed` keyed by `(source, target, type)`, **not** the
  edge's own `id` field — that field is just `f"e:{index}"`, a per-build list position,
  and is never meaningful to compare across two separate builds. `changed` means the
  same `(source, target, type)` triple exists in both graphs but `(tag, confidence)`
  differs — e.g. a call that resolved `INFERRED` at 0.55 in one build and `RESOLVED` at
  0.97 in the next, after the code changed enough for the v4.3 resolution tiers
  (`references/query_protocol.md`) to kick in.

Known, deliberately unaddressed limitation: because every node id embeds the symbol's
own line number (see `_nid()` and every `extract_*` call site), a symbol that only
*moved* — an unrelated comment or import added above it, nothing about the symbol
itself changed — produces one `removed` id and one `added` id at the new line, never a
single `changed` entry. `file`/`entrypoint` node ids don't carry a line number and are
unaffected. A move-aware diff would need to fall back to matching by `(type, name,
path)` when an exact id match fails, which risks misreporting a genuine add+remove pair
as a move (two same-named overloads shifting past each other, for instance) — not
attempted this round specifically to avoid that kind of confidently-wrong match.

## Reasoning subgraph (`--subgraph SYMBOL --depth N --json`, v4.6)

Not a persisted file — printed to stdout only, on demand, never written to
`.codegraph/`. A different shape from the Graph Object above: this is curated,
bounded material for Claude to reason over directly (see `SKILL.md`'s Rule 2 bis and
`query_protocol.md`), not a copy of the full graph.

```json
{
  "focus": "AuthService",
  "focus_id": "class:src/auth.py:AuthService:12",
  "depth": 2,
  "min_confidence_applied": 0.5,
  "node_count": 5,
  "edge_count": 5,
  "truncated": false,
  "max_nodes": 60,
  "nodes": [
    {"id": "class:src/auth.py:AuthService:12", "name": "AuthService", "type": "class",
     "path": "src/auth.py", "depth": 0, "community": "src/auth", "is_cut_vertex": true}
  ],
  "edges": [
    {"source": "PermissionChecker", "source_id": "class:src/auth.py:PermissionChecker:40",
     "target": "AuthService", "target_id": "class:src/auth.py:AuthService:12",
     "type": "calls", "tag": "RESOLVED", "confidence": 0.93}
  ],
  "cut_vertices": ["AuthService"],
  "focus_is_cut_vertex": true,
  "components_if_focus_removed": 2,
  "cycles_through_focus": [["AuthService", "PermissionChecker", "AuthController"]]
}
```

Notes:
- `nodes`/`edges` are restricted to `calls`/`inherits` only (never `contains` — a
  file-mate of the focus isn't a structural relationship worth reasoning about here,
  same exclusion `--impact`/`--trace-entrypoints` already make) and, unless
  `--include-low-confidence` was passed, exclude `calls` edges below
  `min_confidence_applied` (default 0.5) — a coincidental name match adds tokens and
  risk of a wrong conclusion, not signal, for this kind of open-ended reasoning.
- `truncated: true` means the BFS hit `max_nodes` (default 60) before exhausting
  `depth` hops — the neighborhood is real but incomplete; a smaller `--depth` or a
  less-central starting symbol gives a complete one instead. This cap exists because
  an uncapped query against a genuine hub node in a real 807-node/2012-edge project
  measured 100 nodes/258 edges and ~104KB of JSON at `--depth 2` — capped at 60 nodes,
  the same query still measured ~38KB, which is the honest cost of this command, not
  a rounding error.
- `cut_vertices`/`focus_is_cut_vertex`/`components_if_focus_removed` come from Tarjan's
  articulation-point algorithm run **on the extracted subgraph only** — a true
  statement about this neighborhood's shape, never a whole-project guarantee. The same
  scoping applies to `cycles_through_focus`: up to 8 example simple cycles (shortest
  path between each pair of the focus's neighbors, with the focus removed, via BFS),
  not an exhaustive enumeration of every cycle in the neighborhood.
- These structural fields are computed facts, not suggestions for Claude to verify by
  re-reading the node/edge lists — see the "explicit prohibition" in
  `query_protocol.md` on reconstructing cycles/cut-vertices by eye instead of citing
  these fields directly.
