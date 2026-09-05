# Extraction Patterns by Language (v4.6)

This lists what `scripts/codegraph_builder.py` actually matches today, not an
aspirational list. Two extraction paths exist: **tree-sitter** (real AST, optional
dependency — JS/TS/Java since v4.0, Go/Rust/C/C++/PHP added v4.2, see below) and
**regex** (`PATTERNS` dict, everything else, and every tree-sitter-capable language too
whenever the corresponding grammar isn't installed). All
regex-based extraction is line-by-line and heuristic: it has no notion of scope, and it
can misfire inside a string or a comment that happens to look like a declaration. Where
a language's patterns are unanchored (no leading `^`), matching uses `re.search` with a
leading `\b` guard so a keyword embedded inside a longer identifier (e.g. the "function"
inside `run_function_call`) isn't matched; anchored patterns (`^\s*...` or `^[ \t]*...`)
match only when the keyword is the first token on the line, which is both faster and
safer for languages where that convention holds.

## JavaScript / TypeScript / Java — tree-sitter (v4.0, optional)

`pip install "tree-sitter>=0.25,<0.26" tree-sitter-javascript tree-sitter-typescript
tree-sitter-java` (any subset — each grammar is loaded independently in the script; a
missing package for one language never disables the others). The core version matters:
`<0.25` rejects the ABI-15 wheels that `tree-sitter-go`/`-rust`/`-c`/`-php` now ship,
and `0.26.0` segfaults mid-parse on real source — `0.25.x` is the verified line. A
grammar that's installed but can't load (ABI mismatch) is recorded and surfaced by a
build-time `[!]` warning and by `python codegraph_builder.py --doctor`, not silently
dropped. When available for a given file's language, `extract_tree_sitter()` runs
instead of the regex path; on any parse exception, or when the language's grammar isn't
installed/loadable, the script falls back to `extract_regex()` for that file, logged at
`--verbose` as "tree-sitter extraction
failed, falling back to regex". `.codegraph/custom_patterns.json` entries for these
languages still run as a separate pass either way (same as Python's AST path), so
nothing you add there is affected by which engine handled the built-in extraction.

Why a real parser closes this gap where regex couldn't: a `method_definition` (JS/TS) or
`method_declaration` (Java) node is only ever emitted by the grammar for an actual class
member — a real parse tree has no ambiguity between that and `if (...) {`, a `for (...)
{`, or an object-literal method shorthand outside any class the way source text does.
This is the exact limitation the v3 sections below describe as needing "a real parser,
not a regex" — tree-sitter is that parser.

**JS/TS node-type mapping** (walked recursively from the file's root node, tracking
enclosing-class and module-scope state):
- `class_declaration` → `class` node; bases/implements read from the `class_heritage`
  child (no stable named field across the grammar for this — both `extends` and
  `implements` targets are plain `identifier`/`type_identifier` tokens inside it).
- `method_definition` (inside a class) → `function` node, `metadata.kind` one of
  `"constructor"`/`"getter"`/`"setter"`/`"method"` (from sibling `get`/`set` token
  children; `constructor` from the name itself), `metadata.class` set to the enclosing
  class name, `metadata.is_static`/`is_async` from sibling token children. TS visibility
  (`private`/`protected`/`public`) is a distinct `accessibility_modifier` child node
  (not a bare token like `static`/`async`/`get`/`set`), checked separately for
  `metadata.is_public`; a leading `#` on the name means private regardless.
- `public_field_definition`/`field_definition` whose value is an arrow/function (`foo =
  (x) => {...}` inside a class body — the common React/Node auto-bound-method pattern,
  invisible to the regex engine's top-level-only arrow pattern) → `function` node,
  `metadata.kind: "method"`, same class/visibility handling as above.
- `function_declaration` → `function` node (top-level, always `is_public: true`).
- `lexical_declaration` at module scope, `variable_declarator` with an arrow/function
  value → `function` node, `metadata.kind: "arrow"` — deliberately narrower than the
  regex engine's line-based arrow pattern (which is scope-blind and also fires on a
  `const helper = () => {}` defined *inside* a function body); a real scope means this
  distinction is now exact rather than approximate. A `variable_declarator` whose value
  is a plain literal, when the whole declaration is `export`ed (checked via
  `node.parent.type == 'export_statement'`, since there's no named field for it) → a
  `variable` node — matching the regex engine's `export const X = ...` fallback exactly,
  so this path is not a narrower regression versus v3 for that case.
- `interface_declaration` → `type` node, `metadata.kind: "interface"`.
- `type_alias_declaration` → `type` node, `metadata.kind: "type"`.
- `import_statement` → `import` node (`metadata.style: "import"`).
- `call_expression` where the callee is the identifier `require` → `import` node
  (`metadata.style: "require"`), same as the regex engine's `require()` handling.
- **Every `call_expression` and `new_expression` → a call site** `{name, recv, line}`
  fed to `resolve_references()` (v4.5, JS/TS only for now — see CALLSITE_TS_LANGS).
  `foo(...)` → `name "foo"`, `recv None`; `a.b.foo(...)` → `name "foo"`, `recv "a.b"`;
  `new Foo(...)` → `name "Foo"`. A chained/computed callee (`f()()`, `arr[k]()`, a
  tagged template) can't be named against the symbol table and is skipped. Because
  these are real syntax nodes, `if (`/`while (`/`switch (`/`catch (`/a parenthesised
  group never register as calls, and a `name(` written in a comment or string literal
  is invisible — both of which the regex body-scan gets wrong. Each call site is then
  attributed to the innermost `function` node whose line span covers it, so a call
  inside an anonymous callback is credited to the enclosing named function/method, and
  `this.m()` can resolve to a same-class method (`resolved_by: "this_method"`). See
  `query_protocol.md`.
- A signature/annotation gotcha worth knowing if extending this: a method/function
  node's own span can start on a preceding decorator/annotation line (`@Component()` in
  Angular/NestJS-style TS); reading the node's own first line for `metadata.signature`
  would then return the decorator text, not the real declaration. The script instead
  anchors on the declared *name* node's own source line (`decl_line()`), which is always
  the actual signature line regardless of what precedes the node's span.
- `.tsx` files use the dedicated `tsx` grammar (`tree_sitter_typescript.language_tsx()`)
  rather than the plain `typescript` one, imported as a third, independent optional
  grammar.

**Java node-type mapping**:
- `class_declaration`/`interface_declaration` → `class`/`type` node; bases from the
  named `superclass` field, implemented interfaces from the named `interfaces` field.
- `method_declaration`/`constructor_declaration` (inside a class) → `function` node,
  `metadata.kind: "method"` or `"constructor"`, `metadata.class` set, `is_static`/
  `is_public` read from the `modifiers` child node's own children (package-private —
  no modifier keyword at all — is treated as public, matching Java's actual visibility
  rules). Same annotation-line gotcha as JS/TS above: `@Override` above a method is part
  of the node's own span, so `metadata.signature` is anchored on the name's line, not
  the node's first line, via the same `decl_line()` helper.
- `import_declaration` → `import` node, including wildcard (`import foo.bar.*;`)
  imports.

Both language paths still update the same `symtab` the regex engine uses, so community
detection, `god_nodes`, entrypoint detection, and every query command
(`--callers`/`--find-path`/`--trace-entrypoints`/`--impact`) work identically regardless
of which extraction engine produced a given node. For `calls` edges specifically, JS/TS
now goes through the AST call-site path above rather than the regex body-scan — measured
on a real TS codebase (`sindresorhus/got`, ~25 files) it recovered ~30 genuine
private-method call edges (`#a() → #b()`, invisible to `\bname(` since `#` breaks the
word boundary), reclassified ~115 method calls from a vague `same_file` match to a
precise `this_method` one, and dropped ~9 false edges that the regex scan had matched
inside comments.

## Go — tree-sitter (v4.2, optional)

`pip install tree-sitter tree-sitter-go`. Same fallback contract as JS/TS/Java: on any
parse exception, or when `tree_sitter_go` isn't installed, the script falls back to
`extract_regex()` for that file.

- `function_declaration` → `function` node, `metadata.is_public` from capitalization of
  the name (Go has no visibility keyword — this is the language spec, not a heuristic,
  and applies identically to every Go node type below).
- `method_declaration` → `function` node, `metadata.kind: "method"`, `metadata.class`
  set to the receiver's type name. The `receiver` field is a `parameter_list` holding
  one `parameter_declaration` whose `type` field is either a bare `type_identifier`
  (value receiver, `func (g Greeter) ...`) or a `pointer_type` wrapping one (pointer
  receiver, `func (g *Greeter) ...` — the idiomatic form); both are unwrapped to the
  same receiver-type string.
- `type_declaration` → for each `type_spec` child: a `struct_type` becomes a `class`
  node (`metadata.kind: "struct"`), an `interface_type` becomes a `type` node
  (`metadata.kind: "interface"`). Other `type_spec` shapes (plain aliases, e.g. `type ID
  = string`) are deliberately not surfaced as their own kind, matching the regex
  engine's existing Go coverage.
- `import_declaration` → `import` node(s). This is the concrete gain over the regex
  path: a single `import "pkg"` has an `import_spec` directly under the declaration, but
  a grouped `import (...)` block — the normal way any real Go file with more than one
  import writes it — wraps each spec in an `import_spec_list` instead. The regex
  engine's `single_import` pattern only ever matches the first shape, so **every**
  grouped import block was invisible before v4.2 (zero import nodes, not a partial
  miss); tree-sitter normalizes both shapes to the same per-spec iteration.
- No struct-embedding → `inherits` resolution: Go doesn't have classical inheritance,
  and struct embedding (an anonymous field) is a distinct enough relationship from
  "bases" that mapping it onto the generic `inherits` edge would be misleading rather
  than useful — left unmodeled, same as v3's Go coverage.

## Rust — tree-sitter (v4.2, optional)

`pip install tree-sitter tree-sitter-rust`. Same fallback contract as above.

- `struct_item`/`enum_item`/`trait_item` → `class`/`symbol`/`type` node respectively
  (mirroring the regex engine's own type choices), `metadata.is_public` from a
  `visibility_modifier` child being present (`pub`/`pub(crate)`/etc. — presence alone is
  checked, not the specific scope keyword).
- `impl_item` (`impl Type {...}` or `impl Trait for Type {...}`) is not itself turned
  into a node — instead it sets the "enclosing type" context for the `function_item`s
  inside it, and, for a trait impl specifically, appends the trait's name to the
  target struct's own `metadata.bases`, so the existing project-wide
  `bases` → `inherits` resolver (unchanged, generic across every language) turns it
  into a real `inherits` edge from the struct to the trait. **Documented limitation**:
  this only works when the struct's own node was already created earlier in the *same
  file's* walk — tracked in a per-file `name -> node` dict, not a project-wide one. The
  overwhelmingly common case (the struct and its impl block live in the same file, impl
  after the definition) works correctly; a struct defined in one file with its trait
  impl written in a different file is a known gap, not silently claimed to work. Fixing
  it properly needs the same kind of project-wide, after-all-files-are-parsed
  resolution pass the `bases` resolver itself already uses — a reasonable v4.3+
  candidate, scoped out of this round deliberately rather than by oversight.
- `function_item` → `function` node, `metadata.class` set only when inside an
  `impl_item` (both inherent and trait impls scope their methods the same way).
  `metadata.is_async` requires checking a `function_modifiers` child for an `async`
  token specifically — `async` is not a bare sibling token the way `pub` is, so the
  same "check for a token child" approach used elsewhere would silently miss it without
  this extra level.
- `use_declaration` → `import` node, capturing the full `argument` field text verbatim.
  This closes a real regex gap: the regex engine's `use` pattern (`[\w:]+`) stops at the
  first non-path character, so a grouped `use std::fmt::{self, Display};` is truncated
  to `std::fmt` and the grouped names are lost — tree-sitter's `argument` field is the
  complete expression including the `{...}` group.

## C — tree-sitter (v4.2, optional)

`pip install tree-sitter tree-sitter-c`. Same fallback contract as above.

- `function_definition` → `function` node. The actual `function_declarator` (holding
  the name) isn't always the direct `declarator` field — a pointer or reference return
  type (`char* get_name()`, `int& foo()`) wraps it in a `pointer_declarator`/
  `reference_declarator` first — so the extractor searches the whole subtree for the
  first `function_declarator` node rather than assuming it's one hop away.
  `metadata.is_public` is `not is_static`, where `is_static` is a `storage_class_specifier`
  child whose text is `static`.
- `struct_specifier` → `class` node (`metadata.kind: "struct"`), but only when the node
  has a `field_declaration_list` child — a bare `struct Point p;` variable declaration
  also parses as a `struct_specifier` node (referencing, not defining), and without this
  guard every reference site would produce a spurious duplicate node for the same
  struct name.
- `preproc_include` → `import` node (`metadata.style: "include"`), path read from the
  named `path` field with surrounding `<>`/`"` stripped.
- The concrete gain over the regex path: C's regex pattern requires the return type,
  name, parameter list, and opening `{` to all appear on one physical line — a real
  parser has no such restriction, so a multi-line function signature (params split
  across lines, common with long parameter lists) is now captured correctly instead of
  silently missed.

## C++ — tree-sitter (v4.2, optional)

`pip install tree-sitter tree-sitter-cpp`. Same fallback contract as above; shares the
whole C branch above (`function_definition`/`preproc_include`/multi-line-signature
handling apply identically) plus:

- `class_specifier` → `class` node, `metadata.bases` populated from the
  `base_class_clause` child's `type_identifier` children — this covers multiple
  inheritance and mixed access specifiers (`class Derived : public Base, private
  Helper`) as a flat list of base names, same shape as every other language's `bases`
  list, resolved into `inherits` edges by the same generic project-wide resolver.
- `struct_specifier` also gets `cpp_bases()` applied in C++ files (a C++ struct can
  inherit too), on top of the same forward-declaration guard described in the C section.
- Methods defined inside a class body are ordinary `function_definition` nodes — there's
  no separate "method" node type in the cpp grammar the way Java/JS have
  `method_declaration`/`method_definition` — distinguished purely by walk context
  (whether an enclosing `class_specifier`/`struct_specifier` was seen on the way down),
  which is also how `calls` edges originating in one method body and targeting a sibling
  method resolve correctly (same generic text-scan-over-node-span mechanism as every
  other language, see below).
- **Documented limitation**: member visibility (`public:`/`private:`/`protected:`
  section labels) is not tracked at all — every method's `is_public` defaults to `True`
  regardless of which section it's declared in. Tracking it correctly needs propagating
  state across a class body's ordered children (each `access_specifier` labels
  everything until the next one), which is a real but self-contained addition; skipped
  this round as a deliberate scope call rather than an oversight, consistent with this
  project's practice of documenting a gap rather than shipping a value that looks
  authoritative but isn't.

## PHP — tree-sitter (v4.2, optional)

`pip install tree-sitter tree-sitter-php`. Same fallback contract as above. Uses
`tree_sitter_php.language_php()` specifically (not a plain `.language()`) — this
package's API mirrors TypeScript's own dual-grammar shape
(`language_typescript()`/`language_tsx()`), just for the plain-PHP vs. PHP-with-inline-
HTML template variants; only the plain grammar is used here since extraction runs on
`.php` files' PHP code, not embedded HTML.

- `class_declaration`/`interface_declaration` → `class`/`type` node.
  `metadata.bases` combines **both** an `extends` (`base_clause`) and an `implements`
  (`class_interface_clause`) list into one flat list — a PHP class can only extend one
  parent but implement several interfaces, and both relationships resolve into
  `inherits` edges via the same generic resolver, so `AuthService extends BaseService
  implements Speaker` produces two separate `inherits` edges (one to `BaseService`, one
  to `Speaker`) from a single class declaration.
- `method_declaration` (only when inside a class/interface body) → `function` node,
  `metadata.kind: "method"`, `metadata.class` set, `is_static` from a `static_modifier`
  child being present, `is_public` from a `visibility_modifier` child — absent entirely
  (no `public`/`private`/`protected` keyword at all) defaults to public, matching PHP's
  own default visibility rule.
- `function_definition` (top-level, not inside a class) → `function` node,
  always `is_public: True`.
- Four distinct node types cover PHP's include/require family —
  `require_expression`, `require_once_expression`, `include_expression`, and
  `include_once_expression` — handled with one generic check
  (`type.endswith('_expression') and type.startswith(('require', 'include'))`) rather
  than four separate branches, since all four have the identical shape (a `string`
  child holding the path) and differ only in `metadata.style`, which is derived directly
  from the node's own type name.

## Regex fallback (v3, used when tree-sitter is unavailable or for every other language)

## Python — AST preferred (stdlib `ast` module)
Precise: real syntax tree, so no comment/string false positives, and `line_end` is
exact via `end_lineno`. Falls back to the same regex path as everything else only on a
`SyntaxError`. `function`/`class`/`import` nodes; class bases are resolved into
`inherits` edges once the whole project's symbol table is known (not file-by-file).
A dedicated AST pass also locates the top-level `if __name__ == "__main__":` block
precisely, for exact entrypoint node placement — this is why Python entrypoints get an
exact line range while every other language's is approximate.

`.codegraph/custom_patterns.json` entries under `"python"` still run (as a separate,
regex-only pass) even when AST parsing succeeds, since AST extraction has no notion of
e.g. a `@app.route(...)` decorator's argument.

## JavaScript / TypeScript (regex, unanchored, fallback only)
This is the fallback path used when tree-sitter (see above) isn't installed for
JS/TS/TSX, or a specific file fails to parse. On this path there are no AST call sites,
so `calls` edges come from the same `\bname(` body scan every non-tree-sitter language
uses — no receivers, no `this.m()` resolution, and ES private methods (`#m()`) invisible.
- Functions: `\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)`
- Classes: `\b(?:export\s+)?(?:default\s+)?class\s+(\w+)`
- Imports: `\bimport\s+.*?from\s+["']([^"']+)["']`
- `require()`: `\brequire\s*\(\s*["']([^"']+)["']\s*\)` — produces an `import` node in
  both `.js` and `.ts` files (v2 matched this pattern and then silently dropped every
  match; fixed for `.js` in v3, and for `.ts` in v3.1 — the `require` entry was
  present in the `javascript` pattern set but missing from `typescript`'s, so
  `const fs = require("fs")` was silently invisible in a `.ts` file while the
  identical line in a `.js` file worked; found by testing a `.ts` file that used
  `require()`, not by inspection)
- Exported consts/lets/vars with nothing else on the line (`export const X = ...`):
  produce a `variable`-type node instead of vanishing (v2 also matched and dropped
  these)
- Arrow function assigned to a `const`, exported or not: `\b(?:export\s+)?(?:default\s+)?
  const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_]\w*)\s*(?::\s*[^=]+?)?=>`
  (TS variant; JS drops the optional return-type group since JS has none) — produces a
  `function`-type node (`metadata.kind: "arrow"`) since v3.3. Anchored on actually
  seeing `=>` on the line, not just "starts with a paren", so a parenthesized
  non-function expression (`const x = (a + b) * c;`) can't false-positive; verified
  against that and several other near-miss shapes (comparisons using `>=`/`<=`, a
  callback passed inline to another call, a destructured-param arrow, a default value
  containing a nested call) before landing this. This is what most React components
  and Node handler/service code actually look like (`const Foo = () => {}`, `const
  handler = async (req, res) => {}`), and previously required the line to *also* match
  the `export const X = ...` pattern above to be captured at all — a bare, non-exported
  `const handler = () => {}`, or anything reached only via CommonJS `module.exports`
  (no `export` keyword at all), was invisible before v3.3.
- TypeScript adds: interfaces (`\b(?:export\s+)?interface\s+(\w+)`), type aliases
  (`\b(?:export\s+)?type\s+(\w+)\s*=`)
- A decorator line above a class (`@Injectable()`, `@Component({...})`) does not
  interfere with the class pattern on the following line — verified, since decorated
  classes are the norm in Angular/NestJS-style TypeScript.

Known gaps (this regex path only — see the tree-sitter section above for the engine
that closes the method one when installed): multi-line declarations aren't captured (a
signature spanning several lines), including a multi-line arrow function signature
(params split across lines). **Class methods are not extracted at all** on this path,
in either language: `isAdmin(): boolean {` / `async send(msg) {` style shorthand (the
normal way to write a method in JS/TS — nobody writes `methodName = function() {}`
inside a class) never matches the `function` pattern, which requires the literal
keyword `function`. This isn't a narrow miss to patch with one more regex: a bare
`name(...) {` is indistinguishable by text alone from `if (...) {` / `for (...) {` / a
call expression / an object-literal method shorthand outside any class — unlike C's
pattern, TS has no reliable prefix token before the name (the return type comes *after*
the parens: `foo(): void {}`) to anchor on. Same call as Java's method gap and for the
same reason (see below): a regex that tried would misfire often enough to actively
mislead a "what calls this method" query, which is worse than not answering — which is
exactly why v4.0 reaches for tree-sitter instead of a hand-rolled fix here. Net effect,
on this fallback path only: JS/TS extraction is reliable for top-level and class-level
*declarations* (what exists, and its shape) but blind to method bodies and to any
`calls` edge that would originate from inside one — a class's own methods calling each
other, or calling other services, will not show up in `--callers`/`--find-path`
results. If a project needs that without installing tree-sitter,
`.codegraph/custom_patterns.json` can add a scoped pattern for a specific project's
method-naming convention, at the same false-positive-risk tradeoff.

## Go (regex, line-anchored, fallback only)
This is the fallback path used when tree-sitter (see above) isn't installed for Go, or a
specific file fails to parse.
- Functions: `^\s*func\s+(?:\([^\)]+\)\s+)?(\w+)\s*\(`
- Structs: `^\s*type\s+(\w+)\s+struct`
- Interfaces: `^\s*type\s+(\w+)\s+interface`
- Single-line imports: `^\s*import\s+["']([^"']+)["']`

Multi-line `import (...)` blocks are not expanded into individual import nodes — only
single-line `import "pkg"` statements are captured. This is exactly the gap the
tree-sitter path above closes; install `tree-sitter-go` if a project relies on grouped
imports (nearly all real Go code does).

## Rust (regex, line-anchored, fallback only)
This is the fallback path used when tree-sitter (see above) isn't installed for Rust, or
a specific file fails to parse.
- Functions: `^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)`
- Structs: `^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(\w+)`
- Enums: `^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(\w+)`
- Traits: `^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)` (node type `type`, kind=trait)
- Modules: `^\s*(?:pub\s+)?mod\s+(\w+)`
- Uses: `^\s*use\s+([\w:]+)` — truncates at the first non-path character, so a grouped
  `use std::fmt::{self, Display};` is captured as `std::fmt` only.

`impl` blocks are not extracted as their own nodes/edges on this path (v2 had an `impl`
pattern that was collected but, like several other kinds, never had a handler — v3 did
not try to resurrect it, since a correct `impl Trait for Type` → method mapping really
needs a parser, not a regex). This is exactly what the tree-sitter path above adds;
install `tree-sitter-rust` for method extraction and trait-impl → `inherits` edges.

## Java (regex, line-anchored, fallback only) — classes, interfaces, imports only
This is the fallback path used when tree-sitter (see above) isn't installed for Java,
or a specific file fails to parse.
- Classes: `^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:abstract\s+|final\s+)?class\s+(\w+)`
- Interfaces: `^\s*(?:public\s+)?interface\s+(\w+)`
- Imports: `^\s*import\s+(?:static\s+)?([\w.]+);`

v2 also had a `method` pattern
(`^\s*(?:public|private|protected|static|final|abstract|\s)+[\w<>\[\]]+\s+(\w+)\s*\(`)
that matches essentially any line shaped like `<modifiers> <type> <name>(` — in
practice that means every getter, setter, and multi-line generic signature, with a high
false-positive rate and no way to distinguish a real method from a field declaration
that happens to end in a parenthesized initializer. v3 deliberately did not carry this
forward on the regex path: coverage there is scoped to classes/interfaces/imports,
which the pattern above gets right, rather than method extraction that would be wrong
often enough to actively mislead a "which functions call this" query — this is the same
reasoning that led v4.0 to reach for a real parser (tree-sitter, see above) instead of
ever trying to patch this with a smarter regex. `public static void main(...)` is still
detected specifically, via the entrypoint check below, independent of this gap. If your
project needs method-level Java data without installing tree-sitter, add a pattern to
`custom_patterns.json` scoped to your own conventions.

## C (regex, brace-terminated, fallback only)
This is the fallback path used when tree-sitter (see above) isn't installed for C, or a
specific file fails to parse.
- Functions: `^[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;{]*\)\s*\{` — requires the return
  type, name, parameter list and opening `{` all on one line; verified not to
  false-positive on `if (...) {` / `for (...) {` / `while (...) {` / `switch (...) {`
  (the character class between the return type and the name can't cross a `(`, so
  control-flow keywords immediately followed by `(` never match this pattern), but a
  multi-line signature is invisible to it. tree-sitter has no such restriction — see
  above.
- Structs: `^\s*struct\s+(\w+)`
- Includes: `^\s*#include\s+[<"]([^>"]+)[>"]`

## C++ (regex, brace-terminated, fallback only)
This is the fallback path used when tree-sitter (see above) isn't installed for C++, or
a specific file fails to parse. Same shape as C, plus `class`. Same multi-line-signature
caveat, and no inheritance (`bases`) extraction at all on this path — the tree-sitter
path above is the only one that populates `class`/`struct` bases for C++.

## Ruby (regex, line-anchored)
- Methods: `^\s*def\s+(?:self\.)?(\w+)`
- Classes: `^\s*class\s+(\w+)`
- Modules: `^\s*module\s+(\w+)` (captured as an `import`-kind node today, alongside
  `require`/`require_relative` — treat `module` matches here as a declaration marker,
  not literally an import)
- Requires: `^\s*require(?:_relative)?\s+["']([^"']+)["']`

## PHP (regex, line-anchored, fallback only)
This is the fallback path used when tree-sitter (see above) isn't installed for PHP, or
a specific file fails to parse.
- Functions: `^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)\s*\(`
- Classes: `^\s*(?:abstract\s+|final\s+)?class\s+(\w+)`
- Includes: `^\s*(?:include|require)(?:_once)?\s*\(?\s*["']([^"']+)["']`

No `extends`/`implements` → `inherits` extraction on this path (the class pattern above
captures only the class's own name); install `tree-sitter-php` for that.

Entrypoint detection for PHP is `^<?php` at the top of the file — see the caveat under
"Entry point detection" below; it treats the whole file as the entrypoint's body, which
is defensible for a plain PHP script but means every top-level function/class
declaration in that file can show up as something the entrypoint "calls".

## Swift (regex, line-anchored)
- Functions: `^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+)?func\s+(\w+)\s*\(`
- Structs: `^\s*(?:public\s+|private\s+|internal\s+)?struct\s+(\w+)`
- Classes: `^\s*(?:public\s+|private\s+|internal\s+)?class\s+(\w+)`
- Imports: `^\s*import\s+(\w+)`

## Kotlin (regex, line-anchored)
- Functions: `^\s*(?:public\s+|private\s+|internal\s+)?fun\s+(\w+)\s*\(`
- Classes: `^\s*(?:public\s+|private\s+|internal\s+|open\s+|abstract\s+|data\s+)*class\s+(\w+)`
- Imports: `^\s*import\s+([\w.]+)`

## Entry point detection

Separate from the patterns above — `ENTRY_CHECKS` in the script, one regex per
extension, checked with `re.search(..., re.MULTILINE)` against the whole file:

| Ext | Pattern | Body span used for `calls` resolution |
|---|---|---|
| `.py` | `if __name__ == "__main__":` | Exact, via a dedicated AST lookup for that `if` block |
| `.js`/`.ts` | `.listen(`, `createServer(`, (TS also) `NestFactory.create` | Whole file (lines 1..EOF) |
| `.go` | `^func main(` | The matching `function` node's own (approximate) span, if one was extracted at the same line — otherwise whole file |
| `.rs` | `^fn main(` | Same as Go |
| `.java` | `^public static void main(` | Whole file on the regex fallback path (no method node to borrow a narrower span from — see above); with tree-sitter active, `main`'s own `method_declaration` span is used instead, same as Go/Rust |
| `.rb` | `if __FILE__ == $0` | Whole file |
| `.php` | `^<?php` | Whole file |

"Whole file" means lines 1..EOF, not "from the marker line to EOF" — that distinction
matters because the marker line is routinely *not* near the top of the file. An
Express app's `app.listen(...)` is idiomatically the very last line, after every route
and middleware is registered; Ruby's `if __FILE__ == $0` guard is conventionally at the
bottom of the script too. An earlier version of this script scanned from the marker
line to EOF for every "whole file" case, which for a typical Express app meant missing
essentially everything the entrypoint actually does — found via testing a multi-hop
call chain (`--trace-entrypoints`, added in v3.3) that should have reached the
entrypoint and silently didn't. Fixed in v3.3 by always scanning the true whole file
for these cases (`line_start` is `1`, not the marker's own line).

That fix reintroduced, specifically for Java, a different false positive that had
already been found and fixed once for Go/Rust: when the marker line *is* itself a
function-shaped self-mention (`public static void main(`, `func main(`, `fn main(`),
leaving it in the scanned text means its own literal text matches the "calls" regex,
misreading it as a call to "main" — anywhere else in the whole project (this is how
`main:SomeFile.java` once appeared to call `main` in a completely unrelated Rust file,
purely from the shared bare word "main" in its own signature). Go/Rust avoid this by
scanning a narrower span that already excludes the declaration line; but Java's "whole
file" span *includes* the marker line, so once whole-file scanning was corrected to
start at line 1, Java's own `main(` line came back into the scan and the exact same
false positive returned through a different path. The actual fix, since v3.3: the
marker's own line number is tracked separately (`metadata.marker_line`) and that one
line is blanked out of the text handed to the "calls" scan, regardless of whether the
overall span is a narrow function block or the whole file — this is what makes both
cases correct at once, rather than needing separate start/end logic for each. Anyone
extending `ENTRY_CHECKS` with a new declaration-shaped marker should keep this in mind:
if the pattern text itself could match `\b(\w+)\s*\(` in the "calls" regex, it needs to
be excluded from its own scan, one way or another.
