---
name: codegraph
description: Turn any codebase into a queryable knowledge graph so structure and dependency questions are answered from the graph, not by re-reading source. A local, dependency-free Python script parses the project once into .codegraph/graph.json; Claude then runs small query commands (--explain, --callers, --find-path, --trace-entrypoints, --impact, --file-deps) or reads GRAPH_REPORT.md, never loading the whole JSON or opening source to reason about structure. Engage it at the START of any task in a codebase — implementing, fixing, refactoring, reviewing, or asking how it is organized, what calls or subclasses a symbol, what depends on it, what a change would break, how two parts connect, where entry points are, which tests to re-run, which files depend on which. Build or refresh the graph first, then query it INSTEAD OF grep/glob/opening files for anything relational. Any time you would otherwise open several files to see how code fits together, that is this skill. Also triggers on /graph and /codegraph.
license: MIT
---

# CodeGraph Explorer — Script-First Architecture (v4.9)

## Description
Turn any codebase into a queryable knowledge graph. Inspired by Graphify and Understand Anything.
**NEVER parse source code with LLM tokens. ALWAYS generate/run the local script once, then query the JSON.**

## Core Principle (NON-NEGOTIABLE)
```
┌─────────────┐     generate once      ┌──────────────────┐
│   Claude    │ ─────────────────────→ │  Local Script    │
│             │                        │  (Python)        │
│             │ ←───────────────────── │                  │
│             │   reads graph.json     │  Parses AST/regex│
│             │                        │  Updates live    │
└─────────────┘                        └──────────────────┘
```

Claude's role: **Orchestrator + Query Engine**
Claude NEVER: reads raw source files to build the graph, uses regex in reasoning, parses code token-by-token.

## Invocation
The skill is `codegraph` — it auto-triggers on the architecture/dependency questions in
the description above, **and at the start of any task that touches an established
codebase** (see Rule 1), or the user can call `/codegraph`. Its own commands are written
`/graph <command> [args]` throughout this file (e.g. `/graph build`, `/graph explain
UserService`) — that's the skill's command vocabulary, map it to the flags shown.

For the build to be reliably *already done* when a session starts, wire
`scripts/codegraph_session_hook.py` as a `SessionStart` hook — see
[`references/hook_setup.md`](references/hook_setup.md). It is optional; without it,
Rule 1 still says to build on first contact, it just depends on Claude remembering to.

## What changed in v3

v2 of this skill shipped `codegraph_builder.py` with a real syntax error (five lines
mixed `'` and `"` inside a single-quoted raw string, which is a Python `SyntaxError` —
the script could not even be imported), a `NameError` on any Python class with a base
class, and an `IndexError` on the first JS/TS/Java class it found. It also silently
dropped `require()`/TS `type`/`export` matches on the floor, had no real state reset
between builds (watch mode duplicated every edge on every rebuild), scanned whole files
instead of function bodies for "calls" edges (attributing every call in a file to every
function in it), and documented several things — C/C++/Ruby/PHP/Swift/Kotlin support,
`.gitignore` handling, secret redaction, `--update`/`--report` as distinct from a full
build, `custom_patterns.json` — that the code never actually did.

v3 is a full rewrite of `scripts/codegraph_builder.py` that fixes all of the above and
was verified by actually running it (a small hand-built multi-language project, and a
206-file slice of the Python standard library) rather than by inspection alone. The
sections below describe what the current script actually does; where a v2 promise
turned out not to be worth keeping (Java method-level extraction in particular — see
`references/extraction_patterns.md`) it has been scoped down honestly instead of left
as a false claim.

## What changed in v3.2 / v3.3

v3.2 fixed a real behavior gap found by testing a TypeScript project directly:
`require()` was recognized in `.js` files but not `.ts` files, because the `require`
pattern existed only in the `javascript` entry of the internal `PATTERNS` dict, not
`typescript`'s.

v3.3 added three things and, in testing them, found and fixed a second real bug:
- JS/TS arrow functions assigned to a `const` (`const Foo = () => {}`) are now
  captured regardless of `export`, closing a gap that made most React components and a
  lot of Node handler/service code invisible before.
- `calls`-edge `confidence` now gets a soft boost when the caller's file actually
  imports something matching the candidate callee's file — real local evidence, not
  just name-cardinality (see `references/query_protocol.md`).
- `--trace-entrypoints SYMBOL` walks the graph backward to the nearest entrypoint(s) in
  one call, replacing what used to be a manual "call `--callers` repeatedly" workflow.
- Building and testing `--trace-entrypoints` surfaced a real bug in entrypoint "calls"
  scanning: JS/TS/Ruby/PHP/Java entrypoints are documented as scanning "the whole
  file", but the code actually scanned from the marker line (`app.listen(...)`, `if
  __FILE__ == $0`, etc.) to end-of-file — which, since that marker is idiomatically
  near the *end* of the file for Express-style apps, meant missing nearly everything
  the entrypoint does. Fixed; see `references/extraction_patterns.md`'s "Entry point
  detection" section for the full story, including a second false positive the first
  attempt at this fix reintroduced for Java specifically before it was caught.

## What changed in v3.4

Four additions, evaluated (not just borrowed wholesale) against what Graphify and
Understand Anything do, then implemented and empirically tested the same way as every
prior version — real runs against hand-built and real-world test projects, not just
code review:

- **Shrink-guard**: `build()` now refuses to proceed (exit code 1, nothing written) when
  `discover()` finds fewer than 10% of the files `.codegraph/.file_cache.json` last
  tracked (with a floor of ≥5 previously-tracked files, so small projects can't
  false-positive). This is the exact failure mode a real bug caused earlier in this
  project's own development — a broken `.gitignore` matcher made `discover()` report "0
  files found," which would have silently overwritten a correct `graph.json` with an
  empty one. Use `--force-rebuild` when the drop is real and expected (a deliberate
  prune of the project).
- **"0 LLM tokens" reporting**: every build now logs `LLM tokens spent on this build: 0`,
  and `GRAPH_REPORT.md`'s header carries the same line — the token-economy claim this
  skill is built around, stated as a fact about *this* run rather than left implicit.
- **`.codegraphignore`**: a second, root-level ignore file, same syntax as `.gitignore`,
  merged into the same matcher (`should_skip()` skips a path matched by either file). For
  excluding things from the graph that should still be tracked by git — fixtures, a large
  generated/vendored folder you don't own — without touching `.gitignore` itself.
- **`/graph impact <symbol>`** (`--impact`): full transitive closure of everything that
  calls/inherits from a symbol, directly or indirectly — "if I change/break this, what
  else might be affected" — as opposed to `--callers` (one hop) or
  `--trace-entrypoints` (stops at the nearest entrypoint). Also reports which impacted
  nodes are themselves entrypoints, as a fast read on user-facing blast radius.
- **Obsidian vault export** (`--export-obsidian`, `/graph obsidian`): writes
  `.codegraph/obsidian_vault/` — one Markdown note per node, `[[wikilinked]]` to every
  connected node — from the existing `graph.json`, the same read-only-on-JSON contract
  as `/graph report`. Point Obsidian at that folder and its native graph view works with
  zero plugins and no custom Canvas file. Regenerated fresh on every export (stale notes
  from renamed/deleted nodes are cleaned up), but only `*.md` files directly in that
  folder are touched — Obsidian's own `.obsidian/` config folder, created the first time
  the user opens the directory as a vault, is left alone.

Leiden-based community clustering (replacing the current heuristic grouping) was
considered and deliberately deferred here; it shipped as an optional alternative
algorithm in v4.4 — see "What changed in v4.4" below and `--community-algo`.

## What changed in v4.0

Two parallel additions, both optional and both fallback-safe — neither one changes what
`build()`/`--report`/`--explain`/`--callers`/`--find-path`/`--trace-entrypoints`/
`--impact` do when the new dependency isn't installed, following the same pattern
`watchdog` already established for watch mode:

- **tree-sitter as the extraction engine for JS/TS/Java** (`pip install tree-sitter
  tree-sitter-javascript tree-sitter-typescript tree-sitter-java`): closes the exact gap
  v3's docs called out honestly — class methods in JS/TS/Java were undetectable by regex
  because a bare `name(...) {` is indistinguishable by text alone from `if (...) {` or an
  object-literal method shorthand. A real parse tree has no such ambiguity: a
  `method_definition`/`method_declaration` node is only ever emitted by the grammar for
  an actual class member. Each grammar (`javascript`, `typescript`, `tsx`, `java`) is
  imported independently, so a missing single grammar package doesn't disable the
  others, and extraction falls back to the v3 regex engine per-file whenever tree-sitter
  isn't installed for that language or a parse throws. Class methods (including
  TS/Java's `private`/`protected`/getter/setter/static/constructor distinctions, and
  React/Node's class-field arrow-method pattern, `foo = () => {...}`) are now real
  `function`-type nodes with `metadata.kind` (`"method"`/`"getter"`/`"setter"`/
  `"constructor"`) and `metadata.class` set — which also means `calls` edges originating
  *inside* a class method now resolve, closing the "class methods calling each other
  invisible to `--callers`" gap `extraction_patterns.md` used to document as permanent.
  Verified on the project's standing multi-language fixture: node/edge counts went from
  54/53 (regex-only baseline) to 57/56 with tree-sitter active — 3 new method nodes
  captured, zero regressions (confirmed by diffing node-id sets between a tree-sitter
  build and a forced-regex-only build of the same fixture, not just comparing totals,
  since a coincidental gain/loss of equal size would otherwise mask a real regression —
  which is in fact what an early version of this diff caught: a dropped
  `export const X = <literal>` case, fixed before landing). Plain-value exported consts,
  the entrypoint/community/god-node/`--impact` logic, and every existing query command
  needed zero changes — tree-sitter only feeds the same node/edge shape v3 already
  produced. See `references/extraction_patterns.md` for the exact node-type mapping per
  language and the remaining gaps (C/C++/Go/Rust/Ruby/PHP/Swift/Kotlin are unaffected —
  still regex, as before; multi-line signatures across all languages remain approximate).
- **Ladybug/Cypher as a parallel query layer, never a replacement** (`pip install
  ladybug`): `--sync-graphdb` exports the existing `graph.json` into a local embedded
  graph database at `.codegraph/graph_db/` (two generic tables — `Symbol` and `Edge`,
  see `references/graph_schema.md` — rebuilt fresh each time, never merged), and
  `--cypher "<query>"` runs arbitrary read-only Cypher against it. This is explicitly
  additive: the JSON + BFS/Dijkstra engine that powers `--explain`/`--callers`/
  `--find-path`/`--trace-entrypoints`/`--impact` is unchanged and remains the
  default/preferred path for the questions it already answers well (no dependency
  needed, purpose-formatted output). `--cypher` is the escape hatch for what those
  commands don't cover — multi-hop pattern matches, aggregations, ad-hoc filters across
  many nodes at once — see `references/query_protocol.md` for when to reach for it
  instead, plus a real Cypher gotcha found while testing it (`all()` over a
  variable-length relationship binding needs `relationships(path)`, not the raw edge
  variable). Ladybug is Kùzu's successor package (same team, embedded/serverless,
  MIT-licensed) — absence of either optional dependency never affects anything else this
  skill does.

## What changed in v4.1

A hardening round on top of v4.0's two additions, prioritized by asking "of the gaps
this project itself hasn't closed yet, which is actually limiting real usage" rather
than by novelty — plus the test suite that should have existed since the script passed
2500 lines:

- **Nested `.gitignore`/`.codegraphignore`**: previously only the project root's file
  was read. Every such file anywhere under the project now contributes its own
  patterns, scoped to its own directory (a `generated/` pattern in a nested file only
  ignores that directory's own `generated/`, never a sibling's) — the single most
  requested gap in a real monorepo, where per-package ignore files are the norm rather
  than the exception. See the `.gitignore`/`.codegraphignore` section below for the
  exact scoping/negation semantics.
- **`graph_db/` staleness warning**: `.codegraph/graph_db/` (the Ladybug/Cypher layer)
  was never kept in sync automatically after a rebuild, and nothing said so — a
  `/graph cypher` query could silently answer from a graph that no longer matched
  `graph.json`. `--sync-graphdb` now records which build it synced from; `--cypher`
  checks this on every call and prints a `graph_db/ may be stale -- ...` warning (a
  `warning` key under `--json`) when `graph.json` has been rebuilt since — informational
  only, it never blocks the query. This was identified as the most limiting real gap
  after v4.0 shipped, ahead of anything novel, so it's the one this round leads with.
- **Fixed a re-sync crash**: `--sync-graphdb` run a second time against an existing
  `graph_db/` crashed with `NotADirectoryError` — the currently-tested Ladybug version
  (0.20.2) stores the database as a single file, not a directory, and the cleanup code
  assumed a directory unconditionally. This was never caught before because no prior
  testing had actually re-run `--sync-graphdb` against one that already existed; found
  while building the staleness warning above, which is the first feature that made
  re-syncing routine rather than a one-off.
- **Automated test suite** (`tests/`, pytest, dev-only — see `tests/requirements-dev.txt`):
  covers the shrink-guard, incremental cache/dedup behavior, nested-ignore scoping, the
  three real tree-sitter bugs found and fixed while building v4.0 (the Java `@Override`
  annotation-line miss, TS's `accessibility_modifier` visibility miss, the dropped
  exported-literal-const case) as standing regressions, the tree-sitter→regex fallback
  on unparseable input, the Cypher `all()`/`relationships(p)` gotcha, the graph_db
  staleness warning and the resync fix above, the query commands, and Obsidian export.
  Every verification on this project before now was a manual run inspected by eye in
  whatever session made the change — real, but not something a future change could be
  checked against automatically. Deferred to a later round: real static-type-resolution
  based `calls`-edge disambiguation, extending tree-sitter to more languages, and
  Leiden-based community clustering — all evaluated and intentionally not bundled here.

## What changed in v4.2

Extends v4.0's optional tree-sitter path from 3 languages to 8 — same
install-only-what-you-need, fall-back-to-regex-per-file contract, no change to the
graph shape or to any query command:

- **tree-sitter for Go, Rust, C, C++, and PHP** (`pip install tree-sitter-go
  tree-sitter-rust tree-sitter-c tree-sitter-cpp tree-sitter-php`, any subset — each
  grammar is independent, exactly like JS/TS/Java). Concrete gains over each language's
  regex fallback: Go's grouped `import (...)` blocks (previously invisible entirely,
  not just partially — the regex engine only ever matched single-line imports), Go
  method extraction scoped to the receiver type; Rust's `impl`/`impl Trait for Type`
  (methods, plus a real `inherits` edge from the trait impl — same-file only, see
  `extraction_patterns.md`), Rust's grouped `use` declarations (the regex engine
  truncates these at the first `{`); C and C++ multi-line function signatures
  (invisible to the brace-terminated regex pattern, which requires the whole signature
  on one line); C++ class inheritance (`bases`/`inherits`, not extracted by regex at
  all); PHP `extends`/`implements` (both now resolve to `inherits` edges) and method
  visibility/`static`. Full per-language node-type mapping and every known limitation
  (Rust's same-file-only impl linkage, C++'s member-visibility not being tracked) are in
  `references/extraction_patterns.md`.
- **19 new regression tests** (`tests/test_tree_sitter_extended_languages.py`), one
  class per language, each skipped automatically when that language's grammar package
  isn't installed — same pattern as the existing tree-sitter regression tests from
  v4.1's suite.
- Every new language's extraction was built by first writing small standalone probe
  scripts against `tree_sitter.Language`/`Parser` on realistic code samples to discover
  the grammar's actual node types and field names, rather than guessing — the same
  discipline that avoided repeating the exact class of bug (a wrong assumption about
  node shape) that v4.0's original JS/TS/Java work hit three times before landing.
  Result: zero extraction bugs found in testing across all 5 new languages, unlike v4.0.

## What changed in v4.3

Phase 4: a real precision upgrade to `calls`-edge resolution, not just a bigger
confidence number — this is the "actual root of the `confidence` problem" gap flagged
after v4.1, and the one improvement in this round that changes what Claude should
actually trust, not just what it can extract:

- **Two new resolution tiers, checked before the old heuristic, tagged `RESOLVED`
  instead of `INFERRED`**:
  1. **Same-file** (`confidence: 0.97`): when exactly one of the name-matched call
     candidates is defined in the caller's own file, that's the target — works for
     every language, no import parsing needed at all.
  2. **Filesystem-verified import resolution** (`confidence: 0.93`): when the caller's
     own import statement resolves, via real path arithmetic against files that
     actually exist under the project root — not filename-stem similarity, an actual
     resolved path — to exactly one name-matched candidate's file. Implemented for JS/
     TS/JSX/TSX relative imports (`./foo`, `../bar/baz`) and Python dotted module paths
     (`import pkg.sub.mod` / `from pkg.sub import x`). Deliberately **not** attempted
     for Go/Rust/Java/PHP/C/C++: resolving those correctly needs build-tool metadata
     this script doesn't parse (`go.mod`, a Java classpath/source-root convention,
     `composer.json`'s PSR-4 map, an include path, Rust's own `mod`-declaration tree,
     which doesn't have to mirror the directory layout at all) — guessing without that
     would trade one honestly-labeled heuristic for one that only looks more precise.
     Those languages, and any import this tier can't resolve, keep using the pre-v4.2
     filename-stem soft boost as before.
  3. Falls back to the pre-v4.2 candidate-count tiers otherwise — but now counted
     against whatever the import-resolution tier above narrowed the candidate set to,
     when it narrowed without fully resolving, so even an unresolved call can end up
     more precise than before.
- Concretely, on a real Python or JS/TS project, most `--callers`/`--find-path`/
  `--impact` answers involving a project-local, non-generically-named function should
  now come back `RESOLVED` at ~0.93-0.97 rather than `INFERRED` at 0.55-0.85 — the
  actual, felt improvement mentioned in the phase-3 proposal ("qualitative improvement
  in real usage, not a demo artifact").
- **7 new regression tests** (`tests/test_calls_resolution.py`): same-file resolution
  correctly excludes the non-local same-named candidate, both Python import forms
  (`from x.y import z` and `import x.y.z`), a JS/TS relative import resolved across a
  missing/different extension, a bare package specifier (`require("lodash")`)
  confirmed to never be treated as a resolvable path, and the pre-v4.2 fallback tier
  confirmed unchanged (still `INFERRED`, still the same confidence numbers) when
  nothing resolves.
- Full detail, including the exact confidence numbers and metadata shape
  (`metadata.resolved_by: "same_file" | "import"`), is in `references/query_protocol.md`
  and `references/graph_schema.md`.

## What changed in v4.4

Phase 5 — the "nice-to-have, deferrable" bucket from the original 5-phase proposal,
implemented as three independent, individually-optional additions. None of them change
what `build()` produces by default unless explicitly asked for (a new flag, or an
optional dependency actually being installed):

- **Leiden community clustering** (`--community-algo {auto,directory,leiden}`,
  optional — `pip install python-igraph leidenalg`, both install from prebuilt wheels
  on every common platform, no compiler needed): real graph clustering over
  `calls`/`inherits` edges instead of "group by which directory the file happens to be
  in". Catches relationships the directory heuristic structurally cannot see (a
  `handlers/` file and the `services/` file it calls into every time, grouped together
  even though they live in different directories) and can split a single directory
  that actually holds two unrelated feature areas. `auto` (the default) uses Leiden
  when both packages are importable, silently falling back to the pre-v4.4 directory
  heuristic otherwise — same transparent-fallback contract as tree-sitter/ladybug.
  `directory` forces the old heuristic regardless of what's installed; `leiden` forces
  Leiden and errors immediately (not a silent fallback) if the packages are missing.
  Every vertex touched by no `calls`/`inherits` edge at all gets folded into one shared
  "ungrouped" community rather than reported as dozens of one-node communities. A graph
  with fewer than 5 real (`calls`/`inherits`) edges total falls back to the directory
  heuristic automatically — too little signal for clustering to mean anything.
  `self.communities`' shape is unchanged either way, so the report template, `graph_db`
  sync, and every query command work identically regardless of which algorithm ran.
- **Graph versioning / diff** (`--snapshot NAME`, `--diff [NAME]`): `save()` now
  rotates a single-slot `.codegraph/.graph_prev.json` before every write, so
  `--diff` with no name always has "the state before the most recent build/update" to
  compare against, with zero setup. `--snapshot NAME` explicitly checkpoints the
  current graph.json to `.codegraph/snapshots/NAME.json`, which survives any number of
  further builds until `--diff NAME` is used against it or it's explicitly re-taken.
  Both report added/removed/changed nodes (by node id) and added/removed/changed edges
  (by `(source, target, type)`, since an edge's own `id` field is just a per-build list
  index, never stable across rebuilds). Honestly documented limitation: node ids embed
  the symbol's own line number, so a symbol that merely *moved* (an unrelated line
  added above it, nothing about the symbol itself changed) shows up as one removed id
  and one added id, never as a single "changed" entry — a real move-aware diff would
  need matching by `(type, name, path)` when an exact id match fails, which risks
  misreporting a genuine add+remove as a move (e.g. two same-named overloads shifting
  past each other) and wasn't attempted this round for exactly that reason.
- **Broader secret redaction**: `redact()` grew from 3 patterns (generic
  keyword-named assignment, AWS access key, bearer token) to also cover GitHub/GitLab/
  Slack/Stripe/npm token prefixes (literal, publicly-documented formats these services
  actually issue — not guesses), Slack incoming-webhook URLs, PEM private-key block
  markers, and JWTs (the `eyJ` header-segment prefix is the base64 encoding of `{"`,
  which is what every real JWT header starts with — a near-certain tell, not a
  heuristic). It also added a generic high-entropy-assignment detector for a secret
  with neither a recognizable keyword name nor a known prefix: Shannon entropy ≥ 4.3
  bits/char (chosen because it sits *above* the mathematical maximum entropy of any
  16-symbol alphabet — log2(16) = 4.0 — so no pure-hex string, meaning no git SHA,
  checksum, or hash constant, can ever cross it, while real base64-ish secrets, up to
  64 symbols and max entropy 6.0, clear it comfortably) plus a same-length no-spaces
  charset requirement (rules out natural-language strings, which is what most
  assigned string literals actually are) plus a 2-of-3 upper/lower/digit diversity
  check (rules out UUIDs) plus an explicit UUID-shape exclusion. Verified empirically
  against realistic non-secrets (a sha256 hash, a UUID, a camelCase identifier, a
  version string, a lorem-ipsum-style sentence) before landing the threshold, not
  picked and hoped for — still explicitly documented as a heuristic, not a real
  secret-scanning tool, and expected to occasionally over- or under-fire.
- **19 new tests** across `tests/test_community_leiden.py` (5, Leiden-vs-directory
  behavior and the sparse-graph fallback), `tests/test_graph_diff.py` (10, including
  the documented moved-symbol limitation locked in as an explicit regression guard),
  and additions to `tests/test_unit_helpers.py`'s `TestRedact` (16 new cases: every new
  known-prefix shape, the generic detector firing, and — the more important half —
  five realistic non-secret values confirmed to NOT be redacted).

## What changed in v4.5

Five independent changes, each verified by running the script and frozen as a pytest
regression. Nothing here changes what a build produces by default for a language that
isn't JS/TS.

- **`calls` resolution for JS/TS is AST-driven, not a text scan.** The tree-sitter
  JS/TS walk now emits a real call site (`{name, recv, line}`) for every
  `call_expression` / `new_expression`, and `resolve_references()` attributes each to
  the innermost function node whose span covers it before running the same
  same-class / same-file / import / ambiguity tiers. Consequences: `if (` / `while (`
  / `switch (` / `catch (` / a parenthesised group are structurally not calls and
  never register; a `name(` inside a comment or string is invisible; `a.b.foo()` is
  captured with its receiver; a call inside an anonymous callback is credited to the
  enclosing named function; and `this.m()` / `self.m()` inside a method resolves to a
  same-class method (`resolved_by: "this_method"`, confidence 0.97). Measured on
  `sindresorhus/got` (~25 files): +~30 real private-method edges (`#a() → #b()`, which
  `\bname(` can't match because `#` breaks the word boundary), ~115 method calls moved
  from a vague `same_file` match to a precise `this_method` one, ~9 comment/string
  false positives dropped. Every other language (Python, the regex languages, and the
  not-yet-converted tree-sitter languages Go/Rust/Java/C/C++/PHP) keeps the `\bname(`
  body scan unchanged; a file whose tree-sitter parse fails falls back to it too. One
  incremental build re-parses already-cached JS/TS files once to pick up call sites;
  a full `/graph build` covers them all at once.
- **File-to-file dependency edges (#C).** After `calls` resolution, the symbol-level
  `calls`/`inherits` edges are collapsed into one weighted edge per (source file →
  target file) pair and stored as `graph.json`'s `file_deps` — kept **separate** from
  `edges` so communities, metrics, `--impact` and `graph.html` are untouched.
  Low-confidence `calls` edges (< 0.4, i.e. a name shared by several unrelated symbols)
  are excluded from the aggregation so it doesn't invent a dependency between files
  that don't reference each other. New `/graph file-deps [FILE|SYMBOL]` command: with
  an argument, what that file depends on and what depends on it; with none, the whole
  project's file-dependency list heaviest-first. Answers "which file depends on which"
  as a lookup over a few hundred entries instead of a walk over thousands of function
  nodes.

- **`--impact` is test-aware (#D).** Every `file` node is tagged
  `metadata.role` — `"test"` when its path has a test directory segment
  (`tests/`, `__tests__/`, `spec/`, ...) or a test filename shape (`test_*.py`,
  `*_test.go`, `*.test.ts`, `*Test.java`, `*_spec.rb`, ...), else `"prod"`. `--impact`
  uses it to separate the blast radius into prod code (the hop-by-hop list) and a
  `tests_to_run` list — the test files whose code transitively exercises the symbol
  being changed, i.e. exactly what to re-run. Purely a path heuristic; it does not
  change extraction or `calls` resolution.

- **Content hash as a secondary cache key.** `.file_cache.json` entries now carry a
  `sha` (sha256 of the file's text). On an incremental build, a file whose `mtime`
  moved but whose `sha` still matches the cache is served from cache instead of
  re-parsed, and its stored `mtime` is refreshed so the next run takes the plain
  fast path — this covers `git checkout`, `git stash pop`, `rsync`, `touch`, and
  branch switches, all of which move timestamps without changing bytes. The hash is
  only computed on the slow path (mtime already disagrees), so an unchanged tree
  costs nothing extra. Old cache entries without a `sha` just re-parse once.

- **`graph.html` is genuinely offline again.** Between v3.4 and v4.4 the renderer
  regressed to loading D3 from `https://d3js.org/d3.v7.min.js` — a network dependency
  that blanks the page the moment it's opened without connectivity, directly
  contradicting this skill's local-first premise (and the `SKILL.md` line that still
  called the file "self-contained"). Restored the v3.4 dependency-free renderer: a
  small hand-rolled velocity-Verlet force simulation, SVG built by hand, vanilla
  pan/zoom/drag/search/legend, no `<script src>`, no CDN, no build step. It also
  brings back the neighbour-focus behaviour the D3 version had dropped — selecting a
  node now fades everything except that node and its direct neighbours. Same feature
  set as the D3 version otherwise, ~290 lines of embedded JS. Verified by building a
  graph and opening the result with `window.d3 === undefined` and no console errors.

## What changed in v4.6

Three things: a reasoning-mode addition ("Phase 6"), a tree-sitter robustness fix, and
housekeeping (SKILL.md frontmatter, a bilingual README).

### tree-sitter: pinned to `>=0.25,<0.26`, and load failures are no longer silent

The version matrix turned out to be narrow: `tree-sitter` core `<0.25` rejects the
ABI-15 grammar wheels that `tree-sitter-go`/`-rust`/`-c`/`-php` now ship (`ValueError:
Incompatible Language version 15`), and core `0.26.0` loads everything but segfaults
partway through parsing a real TS tree — so `0.25.x` is the only line that both loads
every current grammar and stays up. Two code changes go with the pin: (1) each grammar
is loaded through one loop that catches *any* exception, not just `ImportError`, and
probes with a one-byte parse — so an ABI-incompatible grammar is recorded in
`TREE_SITTER_LOAD_ERRORS` and the language falls back to regex instead of the whole
script crashing at import; (2) every build prints a `[!]` line naming the affected
languages when that happens, and a new `--doctor` flag (`python codegraph_builder.py
--doctor`) prints the full engine status — which grammars loaded, which are
installed-but-incompatible with the exact error, which aren't installed — plus the
verified-good `pip install` line. Before this, an ABI mismatch meant "silently less
precise than this file claims" with nothing to explain why. `tests/test_doctor.py`.

### Housekeeping

- **SKILL.md now starts with YAML frontmatter** (`name: codegraph`, `description`,
  `license`) — the skill loader rejects a SKILL.md without it. `.gitattributes` pins
  this file to `eol=lf` so a CRLF checkout can't trip the frontmatter parser.
- **Bilingual README**: `README.md` is English (GitHub's default view), `README.fr.md`
  is the French version, each linking to the other; both carry the `docs/graph-preview`
  illustration.

### Phase 6 — the reasoning subgraph

Proposed as a genuine architectural question (does Claude only ever *read verdicts*
about the graph, or can it *reason over the structure directly*), evaluated critically
rather than adopted wholesale, then implemented and measured like every prior phase:

- **`--subgraph SYMBOL --depth N --json`**: extracts a bounded neighborhood as
  structured data for Claude to reason over directly, for open-ended questions
  (`Rule 2 bis` above) that don't reduce to one of Rule 2's precomputed commands. The
  original proposal suggested letting Claude spot cycles and "what breaks this apart"
  by reading the raw node/edge JSON — rejected on the same grounds this whole skill is
  built on: cycle detection and articulation points are exact, well-known O(V+E) graph
  algorithms, not interpretation, and an LLM reconstructing them by eye is a real
  failure mode past a handful of nodes, strictly worse *and* more expensive than the
  script just computing them. So the script computes them: `cut_vertices`,
  `focus_is_cut_vertex`, `components_if_focus_removed` (Tarjan's articulation-point
  algorithm) and `cycles_through_focus` (a bounded representative-cycle search) ship as
  precomputed fields, scoped honestly to the extracted neighborhood only, never claimed
  as a whole-project fact. Low-confidence `calls` edges are filtered by a script-side
  default (`min_confidence=0.5`, `--include-low-confidence` to override) rather than
  left to a SKILL.md reminder Claude might apply inconsistently.
- **Node cap, added after measuring, not guessing**: the original proposal estimated
  "30-80 nodes" as a reasonable size without running anything. A real run against a
  genuine hub class (degree 42) in a 807-node/2012-edge project (Python's own `email`
  package, not a toy fixture) at `--depth 2` came back at 100 nodes/258 edges and
  ~104KB of JSON — tens of thousands of tokens for one call, defeating the entire
  point versus reading `graph.json` directly. `--max-nodes` (default 60, BFS order)
  caps this, with `"truncated": true` reported explicitly rather than silently
  returning a smaller neighborhood than what's actually reachable; the same hub-class
  query capped at 60 nodes still measured ~38KB — real, meaningfully more expensive
  than `--explain`, which is exactly why Rule 2 bis says to prefer the cheaper commands
  whenever one already answers the question.
- **15 new regression tests** (`tests/test_subgraph.py`): a hand-verified cycle+cut-
  vertex fixture (four functions in an undirected 4-cycle plus a pendant node, checked
  by hand before being asserted), depth-bounding, the confidence-filtering default and
  its override (using a real measured confidence value — 0.35 for a 5-candidate
  ambiguous call — rather than an assumed one), the node-cap truncation flag, and
  JSON-shape/edge-case coverage (no match, ambiguous symbol, an isolated node).

## What changed in v4.7

Two changes, both aimed at the same gap: the skill was written as "answer architecture
*questions* from the graph", and in practice it only fired when someone asked one. When
Claude was told to *implement* or *fix* something, it explored the repo the old way —
dozens of `grep`/`glob`/file-read round trips — and never engaged the graph at all.

### The trigger is now "working on code", not "being asked about it"

Rule 1 and the frontmatter `description` now say explicitly: the graph is built or
refreshed at the **start of any task in an established codebase** — implementing,
fixing, refactoring, reviewing — and a new **Rule 1 bis** says to reach for a `/graph`
query *before* `grep`/`glob`/opening files whenever the question is relational (who calls
this, what depends on it, how do A and B connect, where does execution start, which
tests does this change touch). `grep` and file reads stay for what the graph is not: a
specific known file, or a literal string / config value / comment.

### A hook so the graph is already there

`scripts/codegraph_session_hook.py` (new, stdlib-only) wires as a Claude Code
`SessionStart` hook (or a git `post-checkout`/`post-merge` hook, or runs by hand). On a
session start it builds `.codegraph/graph.json` if missing, or `--update`s it if older
than `CODEGRAPH_AUTOBUILD_MAX_AGE` (default 1800 s), always in a **detached background
process** — it prints one line and never blocks the session. Opt out with
`CODEGRAPH_AUTOBUILD=0`. A `.codegraph/.autobuild.lock` keeps two session starts from
building at once. Full setup and knobs: [`references/hook_setup.md`](references/hook_setup.md).
`tests/test_session_hook.py` (8 tests: opt-out, non-code dir, missing/stale/fresh graph,
the lock, the background worker).

## What changed in v4.8

One fix, for a crash that made the builder unusable on a whole class of real repo.

### Discovery prunes during the walk instead of after it

`discover()`, `load_gitignore_tree()`, and `poll_mode()` used `Path.rglob()`, which walks
the entire subtree eagerly and only then lets the per-path skip filter run. On a monorepo
that vendors its dependencies — pnpm's deeply nested `node_modules/`, or a package with a
self-referential directory symlink — `rglob()` raised before `discover()` could skip
anything: `OSError [WinError 1921]` on Windows, `OSError: [Errno 40] Too many levels of
symbolic links` elsewhere. The build died with zero files found.

All three now walk with `os.walk()` through a new `_walk_pruned()` helper that drops
`SKIP_DIRS` entries (`node_modules`, `.git`, `.venv`, `vendor`, `dist`, …) and
dot-directories from `dirnames` **in place**, so `os.walk` never descends into them and
the symlink cycle is never reached. The set of files ultimately discovered is identical
to before — `should_skip()` already filtered exactly these — only the eager crash is
gone. `followlinks=False` keeps `os.walk` itself from following directory symlinks.
`tests/test_discovery_pruning.py` (4 tests: SKIP_DIR pruning, a 40-level nested
`node_modules`, a symlink cycle inside `node_modules`, and nested ignore files still
being found after pruning).

## What changed in v4.9

One precision upgrade to `calls`-edge resolution, for monorepos specifically.

### A workspace-package resolution tier

The filesystem-verified import tier (v4.3) deliberately gives up on a **bare** JS/TS
specifier — `import { x } from '@scope/pkg'` — because resolving one in general needs
build-tool metadata the script doesn't parse. But in a monorepo there's one bare-specifier
case where the metadata *is* unambiguous and *is* right there: when the specifier is the
`name` of a sibling workspace package. `pnpm-workspace.yaml` (or a root `package.json`
`workspaces`) lists the member globs; each member's `package.json` `name` maps that
specifier to exactly one directory.

New **tier 2.5**, between the verified-import tier and the ambiguity-scored INFERRED tier:
a bare specifier that matches a workspace package name resolves to *that package's own
source tree*. One name-matched candidate in the package → `RESOLVED`,
`resolved_by: "workspace_import"`, confidence **0.9** (a notch under tier 2's 0.93 because
it's directory-scoped, not a single verified file — re-exports through the package
entrypoint, `export * from './x'`, aren't tracked as edges, so the export can be in any
file of the package). More than one → the INFERRED candidate set is narrowed to the
package instead of the whole repo. No-op when the root isn't a workspace.

Measured on a real pnpm monorepo (~46.6k nodes, 311 workspace packages): **4,670**
cross-package `calls` edges moved from `INFERRED` (~0.2–0.55, and fanned out across every
same-named symbol in the repo) to a single `RESOLVED` edge each — a quarter of all
RESOLVED `calls` edges in the graph, and ~90k fanned-out INFERRED edges collapsed away.
`tests/test_workspace_resolution.py` (5 tests: pnpm + npm `workspaces` config, subpath
specifiers, the no-workspace no-op, and the narrow-don't-leak case).

## Auto-Build Protocol (Script-First)

### Rule 1: First Contact → Generate + Run
At the **start of any task that touches an established codebase** — a question about it,
*or* implementing / fixing / refactoring / reviewing code in it — not only when a
structure question is asked:
1. Check `.codegraph/graph.json`. If present and fresh → skip to Rule 2. (If the
   `SessionStart` hook is wired, this is already done — it prints `graph ready (N nodes,
   M edges)` and you can go straight to querying.)
2. If absent → run `python codegraph_builder.py <project_root>` (copy the script from
   this skill's `scripts/` folder into the project, or invoke it by full path — it needs
   no adaptation per project, it detects languages by extension on its own).
3. The script produces `.codegraph/graph.json`, `.codegraph/GRAPH_REPORT.md` and
   `.codegraph/graph.html`.
4. Notify: "Graph built. {N} nodes, {M} edges." (use the numbers the script prints).

### Rule 1 bis: Query the graph before you grep
Any time you are about to use `grep`/`glob`/`Read` to answer a **relational** question
about the code — who calls or subclasses a symbol, what depends on it, how two parts
connect, where execution starts, what a change would break, which tests to re-run —
stop and run the matching `/graph` command instead (Rule 2). It is complete where grep
is not (it follows indirect calls, re-exports, inheritance) and it returns the *impact*
view grep never gives. Reserve `grep`/`glob`/`Read` for what the graph genuinely is not:
opening one specific file you already know you need, or searching for a literal string,
a config value, an error message, or a comment.

### Rule 2: Query → Never Load the Whole JSON
When the user asks anything about the codebase:
1. Check `.codegraph/graph.json` exists.
2. If not → apply Rule 1.
3. For a **targeted** question about one symbol or a relationship between two symbols
   ("what does X do", "what calls X", "how does A connect to B", "who subclasses X") —
   use the script's query commands (below) and read only their (small) output. Do
   **not** open `graph.json` yourself for these — on anything past a small project it
   can be tens of megabytes, and reading it defeats the entire point of this skill. This
   is measured, not a guess: a 206-file / ~10.5k-node slice of the Python standard
   library produced a 31.7MB `graph.json`; the same question answered via
   `--explain`/`--callers`/`--find-path` costs a few hundred tokens regardless of
   project size, and both commands returned in under a second on that project.
4. For a **broad** architecture question ("what does this codebase do", "how is it
   organized") — read `GRAPH_REPORT.md` (a few KB, human-written-summary-sized) instead
   of `graph.json`.
5. Only read `graph.json` directly when you need to traverse or aggregate across many
   nodes/edges in a way the query commands don't cover (e.g. "list every entrypoint") —
   and even then, prefer piping through `python -c` or similar run by you rather than
   loading the file into your own context, exactly as you would with any other large
   local data file.
6. **NEVER** open raw source files to answer architecture questions.

### Rule 2 bis: Open/Interpretive Question → `--subgraph`, Not a Guess (v4.6)
Rule 2's query commands are the *answer* to a question that reduces to one precomputed
thing (a caller list, a blast radius, a shortest path). Some real questions don't reduce
to one thing: "is this module well isolated", "why does this design hold up", "what's
the shape of this cluster", "detect a problem in this area" — no single flag computes
"well isolated". For these:
1. Run `python codegraph_builder.py <path> --subgraph SYMBOL --depth 2 --json`
   (`/graph subgraph SYMBOL`) instead of guessing from `--explain`/`--impact` output or,
   worse, reading source files.
2. You now have curated raw material — a bounded neighborhood (default cap 60 nodes,
   `"truncated": true` if it hit that cap) — to reason over directly: which edges are
   `RESOLVED` vs low-confidence coincidences, which communities the nodes fall into,
   what connects to what.
3. **Do not** try to spot cycles or "if this breaks, does the graph split apart"
   yourself by eyeballing the node/edge list — that is exact graph theory (Tarjan's
   articulation points, cycle detection), not interpretation, and an LLM reconstructing
   it by reading a list of edges is a well-known place to miscount past a handful of
   nodes. The script already computed it: use `cut_vertices`, `focus_is_cut_vertex`,
   `components_if_focus_removed`, and `cycles_through_focus` directly from the JSON as
   given facts, and spend your own reasoning on what they *mean* for the question asked
   (e.g. "AuthService is a cut vertex with 2 components on removal — Payment and Billing
   only reach each other through it, so yes, it's a real chokepoint, not just a name").
4. Low-confidence `calls` edges (a name shared by unrelated symbols) are excluded by
   default (`min_confidence` 0.5) — don't second-guess this by asking for
   `--include-low-confidence` unless you have a specific reason to suspect a real edge
   was filtered; the default exists because a coincidental name match is noise for this
   kind of reasoning, not signal.
5. Still prefer Rule 2's commands whenever one of them already answers the question —
   `--subgraph` costs meaningfully more tokens (a real hub-node query on a genuine
   807-node project measured ~38KB of JSON even after the node cap, vs a few hundred
   tokens for `--callers`) for a reason: it's material to *think with*, not a lookup.
   Reaching for it on "who calls X" would be paying analysis-sized cost for a
   lookup-sized question.

### Rule 3: Stale Detection → Incremental Re-run
If `.codegraph/graph.json`'s `generated_at` predates the newest source file's mtime:
1. Run `python codegraph_builder.py --update <project_root>`.
2. This is a genuine incremental build: the script keeps a per-file cache
   (`.codegraph/.file_cache.json`) keyed on path + mtime, with a sha256 content hash as
   a secondary key (a file whose mtime moved but whose bytes didn't — `git checkout`,
   `stash pop`, `rsync`, `touch` — is served from cache, not re-parsed), and only
   re-parses files that actually changed since the last run; everything else
   (cross-reference resolution, communities, metrics) is still recomputed in memory
   from the merged result, which is cheap and is what keeps repeated runs from ever
   duplicating nodes or edges.
3. Notify briefly: "Graph refreshed ({X} files re-parsed, {Y} served from cache)."

### Rule 4: Continuous Mode (Optional)
If the user says "keep the graph live" or "watch mode":
1. Run `python codegraph_builder.py --watch <project_root> &`
2. The script debounces file-system events (via `watchdog` if installed, otherwise a
   5-second polling fallback) and re-runs the same incremental logic as `--update` on
   each detected change — not a full rebuild, and not a plain re-append, so it stays
   correct run after run.
3. Claude reads the updated JSON on demand; `/graph stop` kills the background process.

## Script Generation Strategy

There is normally nothing to generate: `scripts/codegraph_builder.py` in this skill is
already a complete, dependency-free (stdlib only; `watchdog` is optional, for watch mode
only) implementation covering the languages listed below. Copy or reference it directly.
Only write a variant script if the project needs a language or extraction rule this one
doesn't cover and a `.codegraph/custom_patterns.json` override (see below) isn't enough.

### If the script fails to run
Fix it (one retry max), then fall back to reading key files manually only if the script
absolutely cannot work for this project.

## Commands

### `/graph build [path]`
Force a **full** rebuild (`python codegraph_builder.py <path>`, no `--update`): every
discovered file is re-parsed regardless of the cache. Use when the graph seems wrong, a
new language was added, or the user explicitly asks.

### `/graph explain <symbol>`
Run `python codegraph_builder.py <path> --explain <symbol>`. The script (not Claude)
resolves the symbol against the existing `graph.json`, and prints:
- Metadata (type, path, line range, signature/kind)
- Incoming edges (what uses it) and outgoing edges (what it uses)
- Community membership and degree
- For `calls` edges specifically, the `confidence` value (see
  `references/query_protocol.md` — it is not decoration, it tells you how much to trust
  the edge)

If the name is ambiguous (several symbols share it across files/languages), the command
prints a short candidate list with exact ids instead of guessing — re-run with the exact
id shown. Add `--json` for structured output when you need to post-process it further
rather than just relaying it.

### `/graph callers <symbol>`
Run `python codegraph_builder.py <path> --callers <symbol>`: everything that
calls/subclasses the symbol, sorted by confidence, without the rest of `--explain`'s
output. Use this over `--explain` when the user specifically asked "what calls X" /
"what depends on X" and you don't need the symbol's own metadata.

### `/graph path <symbolA> <symbolB>`
Run `python codegraph_builder.py <path> --find-path <symbolA> <symbolB>`. The script
runs a weighted shortest-path search over the existing graph (edges through a shared
file are penalized relative to `calls`/`inherits` edges, so a real code relationship is
preferred over "they happen to live in the same file" when both exist at the same hop
count) and prints the path with edge types, tags, and confidence.

### `/graph entrypoints <symbol>`
Run `python codegraph_builder.py <path> --trace-entrypoints <symbol>` (added v3.3). The
script walks `calls`/`inherits` edges backward from the symbol (never `contains` — a
file-mate isn't a caller) until it reaches node(s) of type `entrypoint`, and prints up
to 5 distinct chains with every hop detailed, the same format as `--find-path`. This
replaces the old workflow of calling `--callers` by hand, reading the result, and
calling it again on each caller until an `entrypoint` turned up — the script now does
that walk itself in one call. If nothing is found, it says so explicitly rather than
returning an empty list silently; treat that as "not reached from a detected
entrypoint, or the call chain uses something this script's regex heuristics don't
catch", not as proof the symbol is dead code.

### `/graph impact <symbol>`
Run `python codegraph_builder.py <path> --impact <symbol>` (added v3.4). The script
walks `calls`/`inherits` edges backward from the symbol, the same direction as
`--callers`/`--trace-entrypoints`, but does **not** stop at one hop or at the nearest
entrypoint — it collects the full transitive closure (BFS, capped at depth 15 and 5000
visited nodes so it stays sub-second on any project) and reports every node reached,
grouped by hop depth, plus which of those are themselves entrypoints. Use this for "if I
change/break X, what else might break" — `--callers` answers "who calls X directly",
`--trace-entrypoints` answers "how do I reach X from the outside", `--impact` answers
"how far does a change to X actually propagate". If nothing depends on the symbol, it
says so explicitly rather than an empty list.

The blast radius is split **prod vs test** (v4.5, #D): the detailed hop-by-hop list is
prod code only, and a separate `tests_to_run` field / "test files to re-run" line names
every test file holding a symbol in the closure — i.e. the tests that actually exercise
what you're about to change. Test files are recognised by directory segment (`tests/`,
`__tests__/`, `spec/`, `e2e/`, ...) or filename shape (`test_*.py`, `*_test.go`,
`*.test.ts`, `*Test.java`, `*_spec.rb`, ...); the classification also lands on every
`file` node as `metadata.role` (`"test"` / `"prod"`), so `--explain` on a file shows it
too. This is a path heuristic, not a test-framework analysis — a helper module living
under `tests/` counts as test code.

### `/graph file-deps [<file or symbol>]`
Run `python codegraph_builder.py <path> --file-deps [<file or symbol>]` (v4.5, #C). Reads
`graph.json`'s `file_deps` — the `calls`/`inherits` edges aggregated into one weighted
edge per (source file → target file) pair. With an argument (a file path/substring, or
a symbol name that resolves to its file): prints what that file depends on and what
depends on it, each with a call-count weight. With no argument: the whole project's
file-dependency list, heaviest first. Use this for "what does this file pull in", "what
breaks if I touch this file", or "what are the hub files" — it's a lookup over a few
hundred file pairs, not a traversal of every function node, so it stays instant on a
large project where `--impact` at the symbol level would return thousands of nodes.

### `/graph subgraph <symbol> [--depth N]` (v4.6)
Run `python codegraph_builder.py <path> --subgraph <symbol> --depth N --json` (default
depth 2). Returns a bounded neighborhood around `symbol` — nodes + edges within N hops
over `calls`/`inherits` only — as structured data for **you to reason over directly**,
not a pre-formatted verdict. This is the command Rule 2 bis routes to for open-ended
"why"/"how is this shaped" questions that `--explain`/`--callers`/`--impact` don't
reduce to. The script precomputes, over the extracted neighborhood only (never claimed
to be true of the whole project graph — a wider `--depth` can reveal a path this call
didn't see):
- `cut_vertices` / `focus_is_cut_vertex` — Tarjan's articulation-point algorithm; a true
  cut vertex has no other path within this neighborhood, computed exactly, not eyeballed
- `components_if_focus_removed` — how many disconnected pieces the neighborhood splits
  into without the focus node (only meaningful when `focus_is_cut_vertex` is true)
- `cycles_through_focus` — up to 8 example simple cycles containing the focus node
  (shortest representative per neighbor pair, not exhaustive cycle enumeration)
Low-confidence `calls` edges (< 0.5) are excluded by default — `--min-confidence F` to
change the floor, `--include-low-confidence` to see them all. Hard-capped at 60 nodes
by default (`--max-nodes N`); a capped result sets `"truncated": true` rather than
silently describing a smaller neighborhood than what's actually reachable — narrow
`--depth` or start from a less central symbol if you hit it. This is real graph-theory
computation, not a bigger read: a genuine hub-class query on an 807-node/2012-edge
project (a real Python stdlib package, not a toy) measured ~38KB of JSON even after the
cap — meaningfully more than `--explain`'s few hundred tokens, so reach for `--explain`/
`--callers`/`--impact` first whenever one of them already answers the question (see
Rule 2 bis, point 5).

### `/graph query "<question>"`
Free-form questions don't map to a single CLI flag — this is where Claude's own
reasoning earns its keep, not the script's. Decide which primitive(s) answer the
question, run those, and synthesize:
- "What connects auth to the database?" → `--find-path` from an auth entrypoint/class to
  a database-related one
- "Which services depend on payment?" → `--callers` on the relevant payment symbol(s)
- "Find entry points to UserRepository" → `--trace-entrypoints UserRepository` directly
- "What would break if I changed X?" → `--impact X` directly
- "Is X well isolated / what's the shape of this cluster / detect a problem here" →
  `--subgraph X --depth 2 --json` and reason over the result directly (Rule 2 bis)
- Anything broader ("how is this organized") → `GRAPH_REPORT.md`, not the query commands

### `/graph community`
Read `graph.json["communities"]` and summarize (each community now carries a
heuristically generated one-line `description` — say plainly that it's a heuristic, not
a verified architectural label). Since v4.4, which algorithm produced the grouping
depends on `--community-algo` at build time (default `auto`): real Leiden clustering
over `calls`/`inherits` edges when `python-igraph`+`leidenalg` are installed (a
community can legitimately span several directories — that's the point, not a bug),
else the pre-v4.4 directory-grouping heuristic. Nothing in `graph.json`'s shape
indicates which one ran; if it matters to the user, note that a community name spanning
multiple directories (`"services (+2 more dirs)"`) is a tell that Leiden ran.

### `/graph report`
Run `python codegraph_builder.py --report <path>`. This regenerates
`GRAPH_REPORT.md`/`graph.html` from the **existing** `graph.json` only — it does not
touch a single source file and is fast even on a large project. If there is no
`graph.json` yet, it prints a message and does nothing; run Rule 1 first.

### `/graph watch` / `/graph stop`
Start/stop continuous mode (Rule 4).

### `/graph snapshot <name>`
Run `python codegraph_builder.py --snapshot <name> <path>` (added v4.4). Copies the
**existing** `graph.json` to `.codegraph/snapshots/<name>.json` — a checkpoint that
survives any number of further builds, for `/graph diff <name>` to compare against
later. Requires a `graph.json` to already exist; run Rule 1 first if not.

### `/graph diff [name]`
Run `python codegraph_builder.py --diff [<name>] <path>` (added v4.4). Reports
added/removed/changed nodes and edges between the current `graph.json` and either: no
name given — the state immediately before the most recent build/`--update` (rotated
automatically into `.codegraph/.graph_prev.json` on every save, so this always works
with zero setup as long as at least two builds have happened); or a name — the named
`/graph snapshot` taken earlier, which stays comparable no matter how many builds
happened since. Useful for "what did that last build actually change" or "what's
changed since I started this refactor". Tell the user plainly about the one real
limitation: node ids embed the symbol's own line number, so a symbol that only moved
(an unrelated line added above it) shows up as one removed id + one added id, never as
a single "changed" entry — don't read a burst of adds/removes at the same names as
necessarily a real rewrite without checking whether the file just gained or lost a
blank line above them.

### `/graph obsidian`
Run `python codegraph_builder.py --export-obsidian <path>` (added v3.4). Regenerates
`.codegraph/obsidian_vault/` — one Markdown note per node with YAML frontmatter (type,
path, line, community, degree) and `[[wikilinked]]` "Uses"/"Used by" sections — from the
**existing** `graph.json` only, same contract as `/graph report`: no source files
touched, nothing re-parsed. Tell the user to open that folder directly as an Obsidian
vault, or copy/symlink it into an existing one, for a native graph view with zero
plugins. Like the other outputs, this is for the user, not for Claude — never read the
generated notes yourself for context, query the graph via the commands above instead.

### `/graph sync-graphdb`
Run `python codegraph_builder.py --sync-graphdb <path>` (added v4.0, requires
`pip install ladybug`). Exports the **existing** `graph.json` into a local Ladybug
embedded graph database at `.codegraph/graph_db/` — same read-only-on-JSON contract as
`/graph report`/`/graph obsidian`, nothing re-parsed. Fully rebuilt each time (never
merged), so a renamed/deleted node never leaves a stale row. Run this before
`/graph cypher`; re-run it after any rebuild/`--update` whose changes the Cypher queries
need to see — it is **not** kept in sync automatically, but `/graph cypher` will warn
if it detects `graph_db/` predates the current `graph.json` (see below), so this is
hard to forget silently. If `ladybug` isn't installed, this prints a one-line message
and does nothing else — build/report/every other query command are unaffected.

### `/graph cypher "<query>"`
Run `python codegraph_builder.py --cypher "<query>" <path>` (added v4.0). Executes
arbitrary read Cypher against `.codegraph/graph_db/` (built by `/graph sync-graphdb`)
and prints the result as columns + rows (`--json` for structured output). Schema:
`Symbol(id, name, ntype, path, line_start, line_end, community, degree, meta)` and
`Edge(FROM Symbol TO Symbol, etype, tag, confidence, meta)` — `ntype`/`etype` hold this
graph's own type vocabulary (`function`/`class`/...; `contains`/`calls`/`inherits`),
`meta` is a JSON string. Reach for this only for what `--explain`/`--callers`/
`--find-path`/`--trace-entrypoints`/`--impact` don't already cover well — multi-hop
pattern matches, aggregations, ad-hoc filters across many nodes at once — since those
commands need no dependency and already print purpose-formatted output for the
questions they answer. **Gotcha**: filtering a variable-length relationship
(`[e:Edge*1..2]`) with `all(x IN e WHERE ...)` fails (`RECURSIVE_REL` isn't a `LIST`);
bind a path variable and use `relationships(p)` instead:
```cypher
MATCH p = (a:Symbol {name:'AuthService'})-[:Edge*1..2]->(b:Symbol)
WHERE all(x IN relationships(p) WHERE x.etype IN ['calls','inherits'])
RETURN DISTINCT b.name, b.ntype
```
If `ladybug` isn't installed, or `graph_db/` doesn't exist yet, this prints a clear
message (install command, or "run `--sync-graphdb` first") rather than crashing.
**Staleness**: every call compares `graph_db/`'s last-synced `graph.json` against the
current one; if `graph.json` has been rebuilt since the last `/graph sync-graphdb`, a
`graph_db/ may be stale -- ...` line prints before the results (or a `warning` key in
`--json` output) — relay this to the user rather than presenting stale-graph results as
current, and suggest re-running `/graph sync-graphdb`. This is a warning, not a block:
the command still runs and returns whatever `graph_db/` currently holds.

## `graph.html`: for the human, not for Claude

`graph.html` is a **fully self-contained** clickable exploration view — a small
dependency-free force renderer (no D3, no CDN, no build step), so it opens with zero
network access, the same local-first guarantee as everything else here. It is meant
for the *user* to open in their own browser — never read it yourself for context, it
embeds the same data as `graph.json` and is at least as large. Clicking a node opens a
side panel with the same information `--explain` prints and fades everything but that
node's neighbours; the legend toggles node types on/off; there's a name search that
fades non-matching nodes; drag a node to pin it, drag the background to pan, scroll to
zoom. On a project with more
nodes than a comfortable force-directed layout can show at once, it renders only the
top ~700 by degree by default, with a banner to raise that limit — the underlying data
for every node is still there, only the initial simulation is capped. If the user asks
to *see* the graph (as opposed to asking a question about it), point them at this file
rather than trying to describe the graph in words.

## Graph Schema

See `references/graph_schema.md` for the full JSON shapes. Node types actually produced:
`file`, `function`, `class`, `type`, `variable`, `import`, `entrypoint`, `symbol`
(generic fallback for anything matched by a pattern that doesn't fit the other
categories, including everything from `custom_patterns.json`). Edge types: `contains`,
`imports` (folded into `contains`/`import` nodes today, not a separate edge type — see
schema doc), `calls`, `inherits`. Edge tags: `EXTRACTED` (found directly by AST/regex)
or `INFERRED` (derived by script logic — entry-point detection, `calls` resolution,
inheritance linking).

## Query Protocol (Claude-side)

See `references/query_protocol.md`. In short: prefer the script's query commands over
loading `graph.json` yourself (see Rule 2 above — this is the difference between a
query costing a few hundred tokens and one costing millions on a real project); when
citing a `calls` edge, mention its confidence — don't present a 0.2-confidence edge (a
name shared by a dozen unrelated functions) the same way as a 0.85-or-higher one (a
name unique in the whole project, possibly boosted further since v3.3 when the
caller's file imports something matching the candidate's file — see the schema/protocol
docs for how that boost works and why it doesn't override the base tier). Never fall
back to reading source files unless the graph explicitly says
`"[INCOMPLETE: manual review needed]"` — nothing currently writes that marker, so in
practice this means: never. For a question none of the query commands cover — a
multi-hop pattern, an aggregation, a filter across many nodes at once — `/graph cypher`
(v4.0, requires `--sync-graphdb` first) is the escape hatch; it never replaces the
commands above for the questions they already answer. For an open-ended "why"/"how is
this shaped" question that isn't a lookup at all — see Rule 2 bis and `/graph subgraph`
(v4.6) — reason over the returned neighborhood yourself, but treat `cut_vertices`,
`cycles_through_focus`, and `components_if_focus_removed` as computed facts to cite,
never as something to re-derive by eye from the node/edge list.

## Privacy & Safety

- The script runs 100% locally with zero network calls.
- `graph.json` contains structural metadata (names, paths, line numbers) plus small
  source snippets for regex-matched declaration lines and raw import strings — not full
  file contents.
- Those snippets pass through a best-effort redaction pass (`redact()` in the script)
  that blanks out obvious `password=`/`api_key=`/`token=`-style assignments, AWS-style
  access key literals, and bearer tokens. This is a regex pass over short strings, not a
  secret scanner — don't rely on it as your only safeguard on a codebase that might
  contain real credentials in source.
- The script never sees or sends source code anywhere outside the local filesystem;
  Claude only ever reads the resulting JSON/Markdown/HTML for graph operations.

## Language Coverage

| Language | Method | Notes |
|---|---|---|
| Python | `ast` (stdlib), regex fallback on `SyntaxError` | Most precise; also the only language with an exact `if __name__ == "__main__":` span via AST |
| JavaScript / TypeScript (incl. `.tsx`) | **tree-sitter (v4.0) if installed, regex fallback otherwise** | With tree-sitter: real AST — functions, arrow functions, classes (with bases/implements), interfaces/types, imports, `require()`, exported consts, **and class methods** (incl. getters/setters/constructors/static/`private`/`protected`, and class-field arrow methods) with working `calls` edges from inside method bodies. Without it (or on a parse error, per-file): same regex-only behavior as v3 — top-level/class-level declarations only, no method bodies, no `calls` edges originating inside a method. `pip install tree-sitter tree-sitter-javascript tree-sitter-typescript` |
| Go | **tree-sitter (v4.2) if installed, regex fallback otherwise** | With tree-sitter: functions, methods (scoped to their receiver type via `metadata.class`, exported-ness read from capitalization per the language spec, not a keyword), structs, interfaces, **grouped `import (...)` blocks** (invisible to the regex engine, which only matches single-line `import "pkg"`), with working `calls` edges from inside method bodies. Without it: v3 behavior — same node kinds except no receiver scoping and no grouped imports. `pip install tree-sitter tree-sitter-go` |
| Rust | **tree-sitter (v4.2) if installed, regex fallback otherwise** | With tree-sitter: functions, structs, enums, traits, **`impl`/`impl Trait for Type` blocks** — methods scoped to their type via `metadata.class`, and a trait impl becomes a `bases`/`inherits` entry on the type (same-file only — see `references/extraction_patterns.md`), full grouped `use {...}` text (the regex engine's `[\w:]+` pattern truncates at the brace). Without it: v3 behavior — no method-to-type linkage, no `impl`-as-inherits, truncated grouped `use`. `pip install tree-sitter tree-sitter-rust` |
| Java | **tree-sitter (v4.0) if installed, regex fallback otherwise** | With tree-sitter: classes, interfaces, imports, **and methods/constructors** (with modifiers, static, bases/implements) with working `calls` edges from inside method bodies. Without it: v3 behavior — classes/interfaces/imports only, no methods. `pip install tree-sitter tree-sitter-java` |
| C | **tree-sitter (v4.2) if installed, regex fallback otherwise** | With tree-sitter: functions (**including multi-line signatures**, invisible to the regex engine, which requires the whole signature on one line), structs, `#include`, `static` read as not-public. Without it: v3 behavior — single-line signatures only. `pip install tree-sitter tree-sitter-c` |
| C++ | **tree-sitter (v4.2) if installed, regex fallback otherwise** | With tree-sitter: everything C has, plus classes with base-class lists (→ `bases`/`inherits`) and **class methods** (scoped via `metadata.class`) with working `calls` edges from inside method bodies; member visibility (`public:`/`private:` sections) is **not** tracked, `is_public` defaults to `true` for all members regardless of section — see `references/extraction_patterns.md`. Without it: v3 behavior — free functions and bare `class`/`struct` declarations only, no methods, no base-class list. `pip install tree-sitter tree-sitter-cpp` |
| Ruby | regex, line-anchored | methods, classes, modules, `require`/`require_relative` |
| PHP | **tree-sitter (v4.2) if installed, regex fallback otherwise** | With tree-sitter: functions, classes/interfaces with `extends`+`implements` (both → `bases`/`inherits`), **class methods** (visibility, `static`, scoped via `metadata.class`) with working `calls` edges from inside method bodies, all four `require`/`require_once`/`include`/`include_once` variants. Without it: v3 behavior — top-level functions/classes/includes only, no methods. `pip install tree-sitter tree-sitter-php` |
| Swift | regex, line-anchored | functions, structs, classes, imports |
| Kotlin | regex, line-anchored | functions, classes, imports |

Anything else gets a bare `file` node (language recorded, no symbol extraction) unless
you add patterns for it in `.codegraph/custom_patterns.json`. Tree-sitter is entirely
optional and evaluated once per language at import time: `pip install tree-sitter
tree-sitter-javascript tree-sitter-typescript tree-sitter-java tree-sitter-go
tree-sitter-rust tree-sitter-c tree-sitter-cpp tree-sitter-php` (any subset — a missing
package for one language never disables the others, it just leaves that one language on
the regex path).

## Extending: `.codegraph/custom_patterns.json`

Optional file, same shape as the script's internal `PATTERNS` dict:
```json
{
  "python": {
    "route": "@app\\.route\\(['\"]([^'\"]+)['\"]"
  }
}
```
Each entry is `{"kind_name": "regex with one capture group"}`. Matches are merged into
that language's pattern set for regex-based languages, and — because Python normally
goes through `ast` and would otherwise never see custom patterns at all — also run as a
dedicated pass over every Python file regardless of whether AST parsing succeeded.
Anything matched that isn't one of the script's built-in kinds becomes a generic
`symbol` node tagged with `{"kind": "<your kind name>"}`, so nothing you add is ever
silently dropped.

## `.gitignore` / `.codegraphignore`

The script reads `.gitignore` and skips matching paths in addition to the hardcoded
skip-list (`node_modules`, `venv`, `.git`, `dist`, `build`, ...). Since v4.0, **nested**
`.gitignore` files are read too, not just the project root's — every `.gitignore` found
anywhere under the project (except inside a dot-directory or an already-skipped
directory, which are never walked into for graph purposes anyway) contributes its own
patterns, scoped to its own directory: a `generated/` pattern in `src/module_a/.gitignore`
only ignores `src/module_a/generated/`, never a sibling `src/module_b/generated/`
without its own matching rule. Matching is a pragmatic subset of gitignore syntax (glob
patterns, directory patterns ending in `/`); `!` negation resolves within each file's own
pattern set but does not reach across directory levels (a nested file can't un-ignore
something a shallower one already matched), and some of gitignore's exact `**` edge
cases are not spec-complete. Files over 2MB are skipped regardless (bundles, vendored
dumps, minified assets) — this is configurable via `MAX_FILE_BYTES` at the top of the
script.

`.codegraphignore` (added v3.4; nested support added v4.0, same as `.gitignore` above),
same syntax, is read and merged the same way — for excluding paths from the graph
specifically, independent of what git tracks. Useful when something should stay in
version control but has no business in the knowledge graph (fixtures, a large
vendored/generated folder) and you don't want to touch `.gitignore` to express that.

## Shrink-guard

Since v3.4, `build()` refuses to proceed — no files written, exit code 1 — if
`discover()` finds fewer than 10% of the files `.codegraph/.file_cache.json` last
tracked (only once ≥5 files were previously tracked, so this can't misfire on a tiny
project). This catches the class of bug that silently collapses the file scan (a wrong
path, a `.gitignore`/`.codegraphignore` pattern that ends up matching everything, a
permissions problem) before it overwrites a good graph with a near-empty one. If the
drop is real and intended (you actually deleted most of the project), re-run with
`--force-rebuild`.

## Troubleshooting

### "Script fails to run"
1. Check Python version ≥ 3.8 (needed for `ast.end_lineno`).
2. Check write permissions in the project root (it needs to create `.codegraph/`).
3. Force a full rebuild: `/graph build --force` (i.e. run without `--update`).

### "OSError [WinError 1921] / Too many levels of symbolic links during discovery"
Fixed in v4.8. Older versions used `Path.rglob()`, which walked into a vendored
`node_modules` (pnpm nests them deeply) or a self-referential directory symlink and
crashed before the skip filter ran. v4.8 prunes `SKIP_DIRS` and dot-directories during
the walk, so the descent stops at the boundary. Upgrade the script.

### "Script refuses to build / exits 1 with a message about file count collapsing"
That's the shrink-guard (v3.4), not a crash: `discover()` found under 10% of the files
last tracked in `.file_cache.json`. Run with `--verbose` to see what's being skipped and
why (usually a `.gitignore`/`.codegraphignore` pattern matching more than intended, or
the wrong path). If the drop is genuinely expected, re-run with `--force-rebuild`.

### "Graph is incomplete / missing a language"
1. The script may lack pattern support for this language — check the coverage table
   above.
2. Run with `--verbose` to see which files were skipped and why, and which files fell
   back from AST to regex.
3. Add patterns to `.codegraph/custom_patterns.json` (see above).

### "Watch mode not working"
1. Install `watchdog`: `pip install watchdog` for real filesystem events; without it the
   script automatically falls back to 5-second polling, which still works, just less
   promptly.

### "JS/TS/Java class methods still missing from the graph"
1. tree-sitter (v4.0) isn't installed for that language, or a per-file parse failed and
   silently fell back to regex (run `--verbose` to see "tree-sitter extraction failed,
   falling back to regex" per file). Install the relevant grammar package(s):
   `pip install tree-sitter tree-sitter-javascript tree-sitter-typescript
   tree-sitter-java`, then re-run a full build (`--update` alone won't re-parse files
   already in the cache — use a full rebuild, or delete the affected entries from
   `.codegraph/.file_cache.json`).
2. This is expected, not a bug, if tree-sitter is deliberately not installed: the script
   still works fully via the v3 regex engine, just without method-level JS/TS/Java data
   — see the Language Coverage table above.

### "`--sync-graphdb`/`--cypher` says ladybug not installed / graph_db missing"
1. `pip install ladybug` for the first message. This is entirely optional — build,
   `--report`, `--export-obsidian`, and every `--explain`/`--callers`/`--find-path`/
   `--trace-entrypoints`/`--impact` query are unaffected either way.
2. For the second: run `--sync-graphdb` at least once first — `--cypher` only reads an
   existing `.codegraph/graph_db/`, it never builds one implicitly.
3. A Cypher query filtering a variable-length relationship with `all(x IN e WHERE ...)`
   fails with a `RECURSIVE_REL`/`LIST` type error — bind a path variable and filter
   `relationships(p)` instead (see `/graph cypher` above and
   `references/query_protocol.md`).
