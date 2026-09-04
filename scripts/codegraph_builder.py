#!/usr/bin/env python3
"""
CodeGraph Builder v3.4 -- Local-first, token-free graph construction.

Generates .codegraph/graph.json by parsing source files locally.
Claude reads the JSON -- or better, asks this script a targeted question about it (see
--explain/--callers/--find-path/--trace-entrypoints/--impact below) instead of loading
the whole file. Claude never parses source code itself.

Usage:
    python codegraph_builder.py [path]              # Full build (re-parses every file)
    python codegraph_builder.py --update [path]      # Incremental build (only re-parses
                                                       # files whose mtime changed since the
                                                       # last run; unchanged files are served
                                                       # from .codegraph/.file_cache.json)
    python codegraph_builder.py --watch [path]        # Continuous incremental mode
    python codegraph_builder.py --report [path]       # Regenerate GRAPH_REPORT.md / graph.html
                                                       # from the existing graph.json, without
                                                       # touching source files at all
    python codegraph_builder.py --export-obsidian [path]  # Write .codegraph/obsidian_vault/
                                                       # (one wikilinked note per node) from
                                                       # the existing graph.json only
    python codegraph_builder.py --force-rebuild [path]  # Bypass the shrink-guard that refuses
                                                       # to build when discover() finds far
                                                       # fewer files than were last tracked
    python codegraph_builder.py --verbose [path]      # Extra diagnostics on stderr-safe stdout

    # Read-only queries against the existing graph.json -- do the traversal here, in the
    # script, instead of an LLM loading a JSON file that can be tens of megabytes on a
    # real project (measured: 31.7MB / ~10.5k nodes on a 206-file slice of the Python
    # stdlib). Add --json to any of these for structured output.
    python codegraph_builder.py [path] --explain SYMBOL         # metadata + in/out edges
    python codegraph_builder.py [path] --callers SYMBOL         # who calls/subclasses it
    python codegraph_builder.py [path] --find-path SYMBOL SYMBOL  # weighted shortest path
    python codegraph_builder.py [path] --trace-entrypoints SYMBOL  # nearest entrypoint(s)
                                                                    # reaching SYMBOL, in
                                                                    # one call instead of
                                                                    # repeated --callers
    python codegraph_builder.py [path] --impact SYMBOL          # full transitive blast
                                                                  # radius of everything that
                                                                  # calls/inherits from SYMBOL

Design notes (read this before changing extraction logic):

  * build() returns True on success, False if the shrink-guard refused to proceed (see
    below) -- one-shot callers (main()) exit(1) on False; watch_mode()/poll_mode() just
    let that rebuild's log message stand and keep watching, since the guard re-fires
    naturally on the next detected change rather than crashing the whole watcher.

  * Shrink-guard: build() refuses to overwrite a previously-tracked file set with one
    from a suspiciously collapsed discover() result (under 10% of what
    .codegraph/.file_cache.json last tracked, with a floor of >=5 prior files so tiny
    projects can't false-positive) -- this is exactly the failure mode a real bug once
    caused during this project's own development (a broken .gitignore matcher made
    discover() report "0 files found"), which would otherwise have silently overwritten
    a correct graph.json with an empty one. --force-rebuild bypasses it for a real,
    deliberate prune.

  * .codegraphignore sits next to .gitignore, same syntax, same best-effort matcher
    (see load_gitignore_tree/gitignore_matches below) -- for excluding things from the
    graph that you still want git to track (fixtures, vendored code, a large
    generated/ folder) without touching .gitignore itself. Both are merged in
    should_skip(): a path skipped by either one is skipped. v4.1: nested .gitignore/
    .codegraphignore files (not just the project root's) are now read too, each one's
    patterns scoped to its own directory -- see load_gitignore_tree()/_ignored_by().

  * Per-file state (nodes + "primary" edges: contains/inherits/entrypoint) is cached in
    `.codegraph/.file_cache.json`, keyed by relative path + mtime. A build/update always
    reconstructs self.nodes/self.edges from that cache from scratch, then layers derived
    data (calls edges, communities, metrics) back on top in memory. This is what makes
    repeated builds (CLI re-runs, watch mode, poll mode) idempotent: nothing is ever
    appended to a long-lived list across runs, so re-running never duplicates nodes,
    edges or communities. Previous versions of this script did not do this and would
    silently double their edge count on every rebuild in watch mode.

  * "calls" edges are inferred by scanning each function/entrypoint's own body span
    (line_start..line_end) for other known symbol names, not the whole file. Python gets
    a precise span from `ast` (`end_lineno`). Regex-extracted languages get an approximate
    span (declaration line to the next top-level declaration in the same file). Both are
    a big precision improvement over scanning the whole file, which used to attribute
    every symbol referenced anywhere in a file to every function in that file.

  * Nothing here shells out to `go list`, `cargo metadata`, or `javap`. Earlier drafts of
    this skill mentioned those as optional enhancements; they were never implemented, so
    that language has been dropped from the docs rather than left as a false promise.

  * v4.0: two independent, optional additions, neither of which changes anything above
    when their dependency isn't installed -- same "optional, graceful fallback" contract
    as `watchdog` for watch mode.
    - Tree-sitter extraction (javascript/typescript/java only): when `tree-sitter` plus
      the matching `tree-sitter-<lang>` grammar package is importable, extract_file()
      uses a real parse tree instead of the regex engine for those three languages --
      see extract_tree_sitter() below. This is what finally captures class methods for
      JS/TS/Java (the regex engine still can't, and is unchanged for the other 8
      languages and as the fallback when tree-sitter/a grammar isn't installed).
    - Ladybug/Cypher (`--sync-graphdb` / `--cypher`): a parallel, opt-in export of the
      *existing* graph.json into a local embedded graph database (Ladybug, formerly
      Kuzu -- same read-only-on-JSON contract as --report/--export-obsidian), queryable
      with real Cypher. This does not replace graph.json or the --explain/--callers/
      --find-path/--trace-entrypoints/--impact commands, which stay the primary,
      zero-setup interface; --cypher is an escape hatch for questions those fixed
      commands don't cover.
"""

import re
import os
import csv
import sys
import json
import time
import math
import heapq
import shutil
import argparse
import ast as ast_module
from pathlib import Path
from collections import defaultdict, deque, Counter
from datetime import datetime, timezone
from fnmatch import fnmatch

# == Configuration ============================================================
VERSION = "4.4"
GRAPH_DIR = ".codegraph"
GRAPH_FILE = "graph.json"
CACHE_FILE = ".file_cache.json"
REPORT_FILE = "GRAPH_REPORT.md"
HTML_FILE = "graph.html"
OBSIDIAN_DIR = "obsidian_vault"
GRAPHDB_DIR = "graph_db"
GRAPHDB_SYNC_META_FILE = ".graphdb_sync_meta.json"
CUSTOM_PATTERNS_FILE = "custom_patterns.json"
PREV_GRAPH_FILE = ".graph_prev.json"
SNAPSHOTS_DIR = "snapshots"
MAX_FILE_BYTES = 2 * 1024 * 1024  # skip anything bigger than 2MB (bundles, vendored dumps)

VERBOSE = False

# == Optional: tree-sitter (javascript/typescript/java/go/rust/c/cpp/php extraction) ===
# Each grammar is imported independently so that, say, tree-sitter-java missing
# doesn't also disable the javascript/typescript grammars -- matches per file,
# not all-or-nothing. Any language absent here simply falls back to the regex
# engine for that language, same as if tree-sitter weren't installed at all.
TREE_SITTER_LANGS = {}
_TS_PARSER_CLASS = None
try:
    from tree_sitter import Language as _TSLanguage, Parser as _TS_PARSER_CLASS
    try:
        import tree_sitter_javascript as _ts_js
        TREE_SITTER_LANGS['javascript'] = _TSLanguage(_ts_js.language())
    except ImportError:
        pass
    try:
        import tree_sitter_typescript as _ts_ts
        TREE_SITTER_LANGS['typescript'] = _TSLanguage(_ts_ts.language_typescript())
        TREE_SITTER_LANGS['tsx'] = _TSLanguage(_ts_ts.language_tsx())
    except ImportError:
        pass
    try:
        import tree_sitter_java as _ts_java
        TREE_SITTER_LANGS['java'] = _TSLanguage(_ts_java.language())
    except ImportError:
        pass
    try:
        import tree_sitter_go as _ts_go
        TREE_SITTER_LANGS['go'] = _TSLanguage(_ts_go.language())
    except ImportError:
        pass
    try:
        import tree_sitter_rust as _ts_rust
        TREE_SITTER_LANGS['rust'] = _TSLanguage(_ts_rust.language())
    except ImportError:
        pass
    try:
        import tree_sitter_c as _ts_c
        TREE_SITTER_LANGS['c'] = _TSLanguage(_ts_c.language())
    except ImportError:
        pass
    try:
        import tree_sitter_cpp as _ts_cpp
        TREE_SITTER_LANGS['cpp'] = _TSLanguage(_ts_cpp.language())
    except ImportError:
        pass
    try:
        import tree_sitter_php as _ts_php
        TREE_SITTER_LANGS['php'] = _TSLanguage(_ts_php.language_php())
    except ImportError:
        pass
except ImportError:
    pass  # tree-sitter core itself not installed -- TREE_SITTER_LANGS stays empty

# Languages whose tree-sitter walk emits real call_expression call sites (fed to
# resolve_references() instead of the regex `\bname(` body scan). Started as JS/TS in
# the v5 line; Go/Rust/Java/C/C++/PHP still use the body scan until their walks learn
# the same, and Ruby/Swift/Kotlin have no tree-sitter engine at all.
CALLSITE_TS_LANGS = {'javascript', 'typescript'}

# == Optional: Ladybug (formerly Kuzu) -- embedded graph DB, real Cypher =======
# Entirely separate from graph.json/extraction: this only powers --sync-graphdb and
# --cypher, both opt-in. Absence never affects build()/--report/--export-obsidian/
# the --explain/--callers/--find-path/--trace-entrypoints/--impact query commands.
try:
    import ladybug as _ladybug
    LADYBUG_AVAILABLE = True
except ImportError:
    _ladybug = None
    LADYBUG_AVAILABLE = False

# == Optional: Leiden clustering (python-igraph + leidenalg) -- v4.4 Phase 5 =====
# Entirely separate from extraction/resolve_references(): this only affects
# detect_communities(), which falls back to the pre-v4.3 directory-grouping heuristic
# automatically when either package is missing, exactly the same optional-dependency
# contract as tree-sitter/ladybug/watchdog above. Both packages install from prebuilt
# wheels on every common platform (verified: no compiler needed), but this script never
# assumes that's true everywhere, hence the plain try/except rather than a hard
# requirement.
try:
    import igraph as _igraph
    import leidenalg as _leidenalg
    LEIDEN_AVAILABLE = True
except ImportError:
    _igraph = None
    _leidenalg = None
    LEIDEN_AVAILABLE = False


def log(msg):
    print(msg)


def vlog(msg):
    if VERBOSE:
        print(f"   [v] {msg}")


# Language detection by extension
EXT_MAP = {
    '.py': 'python',
    '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript',
    '.go': 'go',
    '.rs': 'rust',
    '.java': 'java',
    '.c': 'c', '.h': 'c',
    '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
    '.rb': 'ruby',
    '.php': 'php',
    '.swift': 'swift',
    '.kt': 'kotlin', '.kts': 'kotlin',
}

SKIP_DIRS = {
    'node_modules', 'venv', '.venv', 'env', '__pycache__', '.git',
    'target', 'build', 'dist', 'out', '.next', '.turbo', 'vendor',
    '.idea', '.vscode', 'coverage', '.pytest_cache', '.mypy_cache',
    '.codegraph',
}

SKIP_FILES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'poetry.lock',
    'Cargo.lock', 'go.sum', 'Gemfile.lock', 'composer.lock',
}

MINIFIED_SUFFIXES = ('.min.js', '.min.css', '.bundle.js')

# Non-code files we still create a bare "file" node for, but never try to extract from.
PASSTHROUGH_EXTS = {'.md', '.json', '.yaml', '.yml', '.toml', '.sql', '.sh'}

# == Extraction patterns ======================================================
# Precise (AST) extraction is used for Python. Everything else is regex + heuristics,
# which is inherently approximate: it can misfire inside strings/comments, and it has no
# real notion of scope. That trade-off is what buys "zero dependencies, any language" --
# see references/extraction_patterns.md for the per-language caveats.
#
# Every value here is a *raw, double-quoted* Python string. Do not switch these to
# single-quoted strings without checking: several of these patterns contain a literal
# single quote inside a ['"] character class, which silently terminates a single-quoted
# Python string early and either breaks the file with a SyntaxError or truncates the
# pattern. That exact mistake shipped in v2 of this script on five separate lines.
PATTERNS = {
    'python': {
        'function': r"^\s*def\s+(\w+)\s*\(",
        'async_fn': r"^\s*async\s+def\s+(\w+)\s*\(",
        'class':    r"^\s*class\s+(\w+)(?:\(([^)]+)\))?:",
        'import':   r"^\s*(?:from\s+(\S+)\s+)?import\s+(.*)$",
    },
    # JS/TS patterns are intentionally *not* anchored to the start of the line (unlike
    # most other languages here) because a real declaration routinely follows other
    # tokens on the same line -- `const wrapped = function inner() {}`, a one-liner
    # `export default class Foo {}` etc. extract_regex() therefore matches these with
    # re.search rather than re.match. The leading `\b` on every entry is required to
    # keep that safe: without it, `re.search` would happily match "function" or
    # "export" as a substring inside an unrelated longer identifier (e.g. the
    # "function" inside `run_function_call(...)`).
    'javascript': {
        'function':  r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)",
        'class':     r"\b(?:export\s+)?(?:default\s+)?class\s+(\w+)",
        'import':    r"\bimport\s+.*?from\s+[\"']([^\"']+)[\"']",
        'require':   r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
        # Arrow function assigned to a const, export or not -- this is how most React
        # components and a lot of Node handler/service code is written
        # (`const Foo = () => {}`, `const handler = async (req, res) => {}`), and it
        # was previously invisible unless the line also happened to match the 'export'
        # pattern below. Anchored on seeing `=>` itself (with an optional TS return
        # type in between), not just "starts with a paren", so a parenthesized
        # non-function expression (`const x = (a + b) * c;`) can't false-positive --
        # verified against that and several other near-miss shapes before landing this.
        'arrow_fn':  r"\b(?:export\s+)?(?:default\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_]\w*)\s*=>",
        'export':    r"\bexport\s+(?:default\s+)?(?:const|let|var)\s+(\w+)",
    },
    'typescript': {
        'function':  r"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)",
        'class':     r"\b(?:export\s+)?(?:default\s+)?class\s+(\w+)",
        'interface': r"\b(?:export\s+)?interface\s+(\w+)",
        'type':      r"\b(?:export\s+)?type\s+(\w+)\s*=",
        'import':    r"\bimport\s+.*?from\s+[\"']([^\"']+)[\"']",
        # TS is valid CommonJS too (`const fs = require("fs")` compiles fine, and is
        # common in Node/Express backends) -- this was missing from this dict even
        # though the identical line in a .js file was handled, a real behavior gap
        # found by testing a .ts file with a require() call, not just inspection.
        'require':   r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)",
        # Same rationale as javascript's 'arrow_fn' above, plus an optional TS return
        # type annotation between the params and `=>` (`(u: User): string =>`).
        'arrow_fn':  r"\b(?:export\s+)?(?:default\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^()]*\)|[A-Za-z_]\w*)\s*(?::\s*[^=]+?)?=>",
        'export':    r"\bexport\s+(?:default\s+)?(?:const|let|var)\s+(\w+)",
    },
    'go': {
        'function':      r"^\s*func\s+(?:\([^\)]+\)\s+)?(\w+)\s*\(",
        'struct':        r"^\s*type\s+(\w+)\s+struct",
        'interface':     r"^\s*type\s+(\w+)\s+interface",
        'single_import': r"^\s*import\s+[\"']([^\"']+)[\"']",
    },
    'rust': {
        'function': r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
        'struct':   r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+(\w+)",
        'enum':     r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+(\w+)",
        'trait':    r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)",
        'mod':      r"^\s*(?:pub\s+)?mod\s+(\w+)",
        'use':      r"^\s*use\s+([\w:]+)",
    },
    'java': {
        'class':     r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:abstract\s+|final\s+)?class\s+(\w+)",
        'interface': r"^\s*(?:public\s+)?interface\s+(\w+)",
        'import':    r"^\s*import\s+(?:static\s+)?([\w.]+);",
    },
    'c': {
        'function': r"^[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;{]*\)\s*\{",
        'struct':   r"^\s*struct\s+(\w+)",
        'include':  r"^\s*#include\s+[<\"]([^>\"]+)[>\"]",
    },
    'cpp': {
        'function': r"^[A-Za-z_][\w\s\*&:<>]*?\b(\w+)\s*\([^;{]*\)\s*\{",
        'class':    r"^\s*class\s+(\w+)",
        'struct':   r"^\s*struct\s+(\w+)",
        'include':  r"^\s*#include\s+[<\"]([^>\"]+)[>\"]",
    },
    'ruby': {
        'function': r"^\s*def\s+(?:self\.)?(\w+)",
        'class':    r"^\s*class\s+(\w+)",
        'module':   r"^\s*module\s+(\w+)",
        'require':  r"^\s*require(?:_relative)?\s+[\"']([^\"']+)[\"']",
    },
    'php': {
        'function': r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)\s*\(",
        'class':    r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)",
        'include':  r"^\s*(?:include|require)(?:_once)?\s*\(?\s*[\"']([^\"']+)[\"']",
    },
    'swift': {
        'function': r"^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+)?func\s+(\w+)\s*\(",
        'class':    r"^\s*(?:public\s+|private\s+|internal\s+)?class\s+(\w+)",
        'struct':   r"^\s*(?:public\s+|private\s+|internal\s+)?struct\s+(\w+)",
        'import':   r"^\s*import\s+(\w+)",
    },
    'kotlin': {
        'function': r"^\s*(?:public\s+|private\s+|internal\s+)?fun\s+(\w+)\s*\(",
        'class':    r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|abstract\s+|data\s+)*class\s+(\w+)",
        'import':   r"^\s*import\s+([\w.]+)",
    },
}

# Kinds that map directly to a specific node type. Anything matched that is NOT listed
# here (including custom kinds loaded from .codegraph/custom_patterns.json) falls back
# to a generic "symbol" node instead of being silently discarded -- v2 matched `require`
# and TS `type`/`export` with regex and then dropped them on the floor because no branch
# handled those kind names.
FUNCTION_KINDS = {'function', 'async_fn', 'arrow_fn'}
CLASS_KINDS = {'class', 'struct'}
TYPE_KINDS = {'interface', 'type', 'trait'}
IMPORT_KINDS = {'import', 'require', 'single_import', 'include', 'use', 'module'}
# 'export' (JS/TS) is handled specially: it's redundant with function/class/interface/type
# on the same line in the common case, and only produces its own node when nothing else
# on the line already did (e.g. `export const handler = ...`).

# == Secret redaction ==========================================================
# SKILL.md promises that "sensitive values (passwords, tokens) are redacted by the
# script". v2 made that promise and never implemented it. This is a best-effort regex
# pass over the small text snippets we do keep (a matched declaration line, a raw import
# string) -- it is not a secret scanner and should not be relied on as one, but it means
# an inline `api_key = "sk-..."` on a line that also declares a function doesn't end up
# verbatim in graph.json.
#
# v4.4 Phase 5 widened this from 3 patterns (generic keyword=value, AWS access key,
# bearer token) to also cover: GitHub/GitLab/Slack/Stripe/npm token prefixes (these are
# literal, publicly-documented prefix formats each service actually issues, not
# guesses), JWTs (the header segment's `eyJ` prefix is a near-certain tell -- it's the
# base64 encoding of `{"`, which no other common token shape produces), PEM private-key
# markers, Slack incoming-webhook URLs, and a generic high-entropy assignment detector
# for a secret with no recognizable prefix and no keyword-shaped variable name either.
_REDACT_PATTERNS = [
    # Keyword-named assignment: api_key = "...", SECRET: '...', etc. Widened keyword
    # list vs pre-v4.3 (added secret_key/private_key/client_secret/auth_token/
    # credential/signing_key/session_token/webhook_url/dsn -- the last for Sentry-style
    # "https://<key>@sentry.io/..." DSN strings, which are secrets despite not
    # containing the word "secret" anywhere).
    re.compile(r"(?i)(api[_-]?key|apikey|secret[_-]?key|secret|private[_-]?key|"
               r"client[_-]?secret|password|passwd|pwd|token|auth[_-]?token|"
               r"access[_-]?key|credential|signing[_-]?key|session[_-]?token|"
               r"webhook[_-]?url|dsn)"
               r"(\s*[:=]\s*)([\"'])(?:(?!\3).){3,}\3"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ID
    re.compile(r"(?i)bearer\s+[a-z0-9\-_.=]{10,}"),
    # GitHub personal-access / OAuth / app / refresh tokens (documented prefix shapes:
    # ghp_/gho_/ghu_/ghs_/ghr_ are the classic-PAT-and-friends family; github_pat_ is
    # the newer fine-grained PAT format).
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # GitLab personal access tokens.
    re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
    # Slack tokens (bot/user/app/config/refresh -- xox[baprs]-) and incoming webhooks.
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+"),
    # Stripe secret/restricted keys (live and test -- a test key is still a real
    # credential for whatever Stripe test account it belongs to).
    re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    # npm automation/publish tokens.
    re.compile(r"npm_[A-Za-z0-9]{36}"),
    # PEM private-key block markers -- the marker line alone, even without the body
    # (which usually spans many lines this script never captures as one string, since
    # every redact() call site is a single matched declaration/import line -- but if a
    # key ever does end up inlined on one line, e.g. in a JSON/env-style value, this
    # still catches the giveaway marker).
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    # JWT: three dot-separated base64url segments, header segment starting `eyJ` --
    # base64 of `{"`, which is what every real JWT header starts with (`{"alg":...}`).
    # Not a guess: this is the actual, universal JWT shape, not a heuristic.
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
]

# Generic high-entropy assignment: catches a real secret that has neither a
# recognizable keyword name nor a known service prefix (a raw API key pasted into a
# config value under a name like `x` or `value`). Deliberately conservative -- see
# _looks_like_secret_value()'s own comment for why entropy alone is not enough and what
# additional guards this applies; even so, this is the one redaction rule in this file
# most likely to occasionally over- or under-fire, and it is documented as exactly
# that, not presented as reliable secret detection.
_GENERIC_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][\w.]{0,40})(\s*[:=]\s*)([\"'])([A-Za-z0-9+/_=-]{20,100})\3")
_ALREADY_KEYWORD_HINTED_RE = re.compile(
    r"(?i)key|secret|password|passwd|pwd|token|credential|auth|session|webhook|dsn")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_like_secret_value(value: str) -> bool:
    """Heuristic core of the generic high-entropy detector. Three guards, all
    required, chosen after empirically checking entropy against realistic non-secret
    strings (hex hashes, UUIDs, camelCase identifiers, version strings, lorem-ipsum-
    style words) rather than picking a threshold and hoping:
      1. Entropy >= 4.3 bits/char. Chosen specifically because it sits ABOVE the
         mathematical maximum entropy of any 16-symbol alphabet (log2(16) == 4.0) --
         so no pure-hex string (a git SHA, a checksum, an md5/sha digest, a hex color)
         can ever cross this bar, regardless of how "random" it looks. Real high-
         entropy secrets are almost always base64-ish (up to 64 symbols, max entropy
         6.0), which comfortably clears 4.3.
      2. At least 2 of {has lowercase, has uppercase, has digit}. Filters out UUIDs
         and git SHAs (lowercase+digit only, but already excluded by guard 1 anyway --
         belt and suspenders) and, more importantly, plain lowercase natural-language
         "words" that can still score deceptively high on entropy alone.
      3. The value must not look like a UUID -- explicit exclusion, since a random
         UUID's hex digits alone can occasionally approach the entropy bar and its
         format is otherwise completely unremarkable.
    None of this makes the check reliable in the way a real secret-scanning tool
    (gitleaks, trufflehog) is reliable -- those correlate against known token grammars
    across a whole file/history, not one already-short snippet. This exists to catch
    the case those keyword/prefix patterns above miss, at the accepted cost of an
    occasional false positive on a long, high-entropy-but-innocent literal (a hash-like
    test fixture ID, an encoded binary blob) -- redacting something harmless is a far
    smaller problem for this graph than leaving a real secret in graph.json."""
    entropy = _shannon_entropy(value)
    if entropy < 4.3:
        return False
    classes = sum([
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
    ])
    if classes < 2:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return False
    return True


def _redact_generic_high_entropy(text: str) -> str:
    def repl(m):
        ident, sep, quote, value = m.group(1), m.group(2), m.group(3), m.group(4)
        if _ALREADY_KEYWORD_HINTED_RE.search(ident):
            return m.group(0)  # the keyword-based pattern above already handles this
        if _looks_like_secret_value(value):
            return f"{ident}{sep}{quote}[REDACTED]{quote}"
        return m.group(0)
    return _GENERIC_ASSIGNMENT_RE.sub(repl, text)


def redact(text):
    if not text:
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(3)}", out)
        else:
            out = pat.sub("[REDACTED]", out)
    out = _redact_generic_high_entropy(out)
    return out


# == .gitignore / .codegraphignore (best effort) ================================
# Every .gitignore/.codegraphignore found anywhere under the project root is honored
# (v4.1: not just the root one -- see load_gitignore_tree()/_ignored_by() below), each
# scoped to its own directory. Matching itself is still a pragmatic subset (fnmatch per
# path component / suffix), not a spec-complete gitignore engine: `!` negation only
# resolves within the file that declares it (a deeper file can't un-ignore something a
# shallower one already excluded), and gitignore's exact "**" semantics aren't fully
# implemented. Combined with SKIP_DIRS this covers the common cases (node_modules/,
# *.log, build/, .env, generated/...).
#
# .codegraphignore uses the exact same syntax and is read the same way, but it's a
# second, independent file: things you want git to track but don't want in the graph
# (fixtures, vendored/generated code you don't own, a huge data/ folder that happens
# to be committed) without having to touch .gitignore itself. Both files are merged
# in should_skip() -- a path skipped by either one is skipped.
def _parse_ignore_file(p: Path):
    patterns = []
    try:
        for line in p.read_text(encoding='utf-8', errors='ignore').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            patterns.append(line)
    except OSError:
        pass
    return patterns


def load_gitignore_tree(root: Path, filename: str = '.gitignore'):
    """Find every <filename> under root (v4.1: not just the root one) and return a dict
    of directory (relative to root, posix, '' for root itself) -> patterns from that
    directory's own file. gitignore/codegraphignore patterns are anchored to the
    directory containing the file that declares them, so this is kept as a per-directory
    map rather than one flat list -- see should_skip()/_ignored_by() for how a file's
    ancestor directories are walked against it. Skips any <filename> that lives inside a
    dot-directory or a hardcoded SKIP_DIRS entry (node_modules, .git, venv, ...) since
    nothing under those is ever graphed anyway."""
    result = {}
    root_file = root / filename
    if root_file.exists():
        patterns = _parse_ignore_file(root_file)
        if patterns:
            result[''] = patterns
    try:
        candidates = root.rglob(filename)
    except OSError:
        candidates = []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            rel_dir_parts = p.relative_to(root).parts[:-1]
        except ValueError:
            continue
        if not rel_dir_parts:
            continue  # already handled as the root file above
        if any(part.startswith('.') for part in rel_dir_parts) or any(part in SKIP_DIRS for part in rel_dir_parts):
            continue
        dir_rel = '/'.join(rel_dir_parts)
        if dir_rel in result:
            continue  # root file already covered dir_rel == '' above; no duplicates otherwise
        patterns = _parse_ignore_file(p)
        if patterns:
            result[dir_rel] = patterns
    return result


def gitignore_matches(rel_posix: str, patterns):
    """Best-effort subset of gitignore matching. For each pattern we check the file's
    path both in full and as every path-suffix starting at each directory component
    (so an unanchored pattern like 'generated/' or '*.log' matches at any depth, the
    way real gitignore does), against both the bare pattern (exact segment/file match)
    and "pattern/*" (anything underneath, covering directory patterns) -- without a
    separate is_dir lookup, since a directory-only pattern is only ever reached here
    via a file living underneath it."""
    parts = rel_posix.split('/')
    matched = False
    for raw in patterns:
        neg = raw.startswith('!')
        pat = raw[1:] if neg else raw
        pat = pat.rstrip('/')
        anchored = pat.startswith('/')
        pat = pat.lstrip('/')
        if not pat:
            continue
        candidates = [rel_posix] if anchored else [rel_posix] + ['/'.join(parts[i:]) for i in range(1, len(parts))]
        hit = any(fnmatch(cand, pat) or fnmatch(cand, f"{pat}/*") for cand in candidates)
        if hit:
            matched = not neg
    return matched


# == Graph Builder =============================================================
class GraphBuilder:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.gitignore_tree = load_gitignore_tree(self.root)
        self.codegraphignore_tree = load_gitignore_tree(self.root, '.codegraphignore')
        self.custom_patterns = self._load_custom_patterns()
        self._ts_parsers = {}  # ts_key -> Parser instance, built lazily, reused across files

        # Authoritative per-file cache: rel_path -> {"mtime", "nodes": [...], "edges": [...]}
        # "edges" here only ever holds primary (contains/inherits/entrypoint) edges --
        # never "calls", which is always recomputed globally after the cache is merged.
        self.file_cache = {}

        # Rebuilt from file_cache (and then layered with derived data) on every build.
        self.nodes = []
        self.edges = []
        self.node_map = {}
        self.symbol_map = defaultdict(list)
        self.communities = []
        self.god_nodes = []
        self.stats = defaultdict(int)
        self.entrypoints = []
        self._edge_seen = set()
        # 'auto' (Leiden when installed, else directory grouping), 'directory' (force
        # the pre-v4.3 heuristic), or 'leiden' (force Leiden, erroring loudly instead of
        # silently falling back if the packages aren't installed -- useful for anyone
        # who wants to be sure which algorithm actually ran, e.g. while testing).
        self.community_algo = 'auto'

    # -- custom patterns ------------------------------------------------------
    def _load_custom_patterns(self):
        p = self.root / GRAPH_DIR / CUSTOM_PATTERNS_FILE
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as e:
            log(f"   [!] could not read {p}: {e}")
            return {}
        merged = {}
        for lang, kinds in data.items():
            if not isinstance(kinds, dict):
                continue
            merged[lang] = kinds
        return merged

    def patterns_for(self, lang):
        base = dict(PATTERNS.get(lang, {}))
        base.update(self.custom_patterns.get(lang, {}))
        return base

    # -- node/edge helpers ------------------------------------------------------
    def _nid(self, *parts):
        return ":".join(str(p) for p in parts)

    def add_node(self, bucket, nid, ntype, name, path, line_start=None, line_end=None, metadata=None):
        """bucket is the list to append to (per-file cache list during extraction)."""
        bucket.append({
            "id": nid, "type": ntype, "name": name, "path": str(path),
            "line_start": line_start, "line_end": line_end,
            "metadata": metadata or {},
            "community": None, "degree": 0,
        })
        return nid

    def add_edge(self, bucket, src, tgt, etype, tag, conf=1.0, meta=None):
        bucket.append({
            "source": src, "target": tgt, "type": etype, "tag": tag,
            "confidence": conf, "metadata": meta or {},
        })

    def get_node(self, nid):
        idx = self.node_map.get(nid)
        return self.nodes[idx] if idx is not None else None

    # -- discovery ---------------------------------------------------------
    def _ignored_by(self, rel: Path, tree: dict) -> bool:
        """v4.1: walk every ancestor directory of `rel` that declared its own ignore
        file (root included, key ''), applying each directory's patterns to the path
        fragment relative to *that* directory -- gitignore patterns are anchored to
        their own directory, so a nested file's 'build/' pattern must not match
        'build/' anywhere else in the project, only under its own directory. Each
        level is resolved independently (its own '!' negations apply within that
        level); a deeper directory's ignore file can add new ignores or override
        within its own scope, but cannot un-ignore something a shallower file already
        matched -- a pragmatic subset of real gitignore precedence, not a spec-complete
        implementation (consistent with the single-file matching this replaces)."""
        if not tree:
            return False
        parts = rel.parts
        for depth in range(len(parts)):
            dir_rel = '/'.join(parts[:depth])
            patterns = tree.get(dir_rel)
            if not patterns:
                continue
            sub_rel = '/'.join(parts[depth:])
            if gitignore_matches(sub_rel, patterns):
                return True
        return False

    def should_skip(self, path: Path):
        rel = path.relative_to(self.root)
        parts = rel.parts
        if any(part.startswith('.') and part not in ('.gitignore', '.codegraphignore') for part in parts):
            return True
        if any(part in SKIP_DIRS for part in parts):
            return True
        if path.name in SKIP_FILES:
            return True
        if path.name.endswith(MINIFIED_SUFFIXES):
            return True
        if path.suffix.lower() not in EXT_MAP and path.suffix.lower() not in PASSTHROUGH_EXTS:
            return True
        if self._ignored_by(rel, self.gitignore_tree):
            return True
        if self._ignored_by(rel, self.codegraphignore_tree):
            return True
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                vlog(f"skip (too large): {rel}")
                return True
        except OSError:
            return True
        return False

    def discover(self):
        files = []
        for p in self.root.rglob('*'):
            if not p.is_file():
                continue
            if self.should_skip(p):
                continue
            files.append(p)
        return files

    # -- Python AST extraction (precise) -------------------------------------
    def extract_python_ast(self, rel: str, content: str, bucket_nodes, bucket_edges, symtab):
        try:
            tree = ast_module.parse(content)
        except SyntaxError:
            return False

        file_id = self._nid("file", rel)

        def end_line(node):
            return getattr(node, 'end_lineno', None)

        for node in ast_module.walk(tree):
            if isinstance(node, (ast_module.FunctionDef, ast_module.AsyncFunctionDef)):
                nid = self._nid("func", rel, node.name, node.lineno)
                args = [a.arg for a in node.args.args]
                if node.args.vararg:
                    args.append("*" + node.args.vararg.arg)
                if node.args.kwarg:
                    args.append("**" + node.args.kwarg.arg)
                self.add_node(bucket_nodes, nid, "function", node.name, rel,
                              node.lineno, end_line(node),
                              {"is_async": isinstance(node, ast_module.AsyncFunctionDef), "args": args})
                self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                symtab[node.name].append(nid)
            elif isinstance(node, ast_module.ClassDef):
                bases = [self._py_name(b) for b in node.bases]
                nid = self._nid("class", rel, node.name, node.lineno)
                self.add_node(bucket_nodes, nid, "class", node.name, rel,
                              node.lineno, end_line(node), {"bases": [b for b in bases if b]})
                self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                symtab[node.name].append(nid)
                # Inheritance is resolved globally later, once the full symbol table exists.
            elif isinstance(node, ast_module.ImportFrom) and node.module:
                nid = self._nid("import", rel, node.lineno)
                self.add_node(bucket_nodes, nid, "import", node.module, rel, node.lineno, node.lineno,
                              {"names": [a.name for a in node.names]})
                self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
            elif isinstance(node, ast_module.Import):
                for alias in node.names:
                    nid = self._nid("import", rel, node.lineno, alias.name)
                    self.add_node(bucket_nodes, nid, "import", alias.name, rel, node.lineno, node.lineno)
                    self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
        return True

    def _py_name(self, node):
        if isinstance(node, ast_module.Name):
            return node.id
        if isinstance(node, ast_module.Attribute):
            base = self._py_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    # -- tree-sitter extraction (javascript/typescript/java, v4.0) ------------
    # Only used when TREE_SITTER_LANGS has an entry for this file's language (see the
    # optional-import block near the top of the file) -- extract_file() falls back to
    # extract_regex() otherwise, exactly like Python falls back to extract_regex() on a
    # SyntaxError. This is what finally captures class methods for these three
    # languages: nothing about it is a heuristic the way the regex engine's per-line
    # patterns are -- a `method_definition`/`method_declaration` node in a real parse
    # tree cannot be confused with `if (...) {`, because the parser itself only ever
    # emits that node type for an actual class member. Spans (line_start/line_end) come
    # directly from the AST too, so unlike extract_regex()'s "next declaration - 1"
    # approximation, they're exact -- including for the class nodes themselves.
    def _ts_parser(self, ts_key):
        parser = self._ts_parsers.get(ts_key)
        if parser is None:
            parser = _TS_PARSER_CLASS(TREE_SITTER_LANGS[ts_key])
            self._ts_parsers[ts_key] = parser
        return parser

    def extract_tree_sitter(self, rel: str, content: str, bucket_nodes, bucket_edges, symtab, lang: str, ts_key: str,
                            bucket_callsites=None):
        try:
            source = content.encode('utf-8', errors='ignore')
            tree = self._ts_parser(ts_key).parse(source)
        except Exception as e:
            vlog(f"tree-sitter parse failed for {rel}, falling back to regex: {e}")
            return False

        file_id = self._nid("file", rel)

        def text_of(node):
            return source[node.start_byte:node.end_byte].decode('utf-8', errors='ignore')

        def line_of(node):
            return node.start_point.row + 1

        def end_line_of(node):
            return node.end_point.row + 1

        def first_line(node):
            return text_of(node).split('\n', 1)[0].strip()

        _source_lines = None

        def decl_line(name_node, whole_node):
            # A method/function/constructor node's OWN span can start on a preceding
            # annotation/decorator line (`@Override`, Java; `@Component()`, TS) rather
            # than the actual declaration -- first_line(node) would then return the
            # annotation text, not the signature. Anchor on the declared name's own
            # source line instead, which is always the real declaration line.
            nonlocal _source_lines
            if _source_lines is None:
                _source_lines = source.decode('utf-8', errors='ignore').split('\n')
            idx = line_of(name_node) - 1
            if 0 <= idx < len(_source_lines):
                return _source_lines[idx].strip()
            return first_line(whole_node)

        def child(node, field, *fallback_types):
            n = node.child_by_field_name(field)
            if n is not None:
                return n
            for c in node.children:
                if c.type in fallback_types:
                    return c
            return None

        def find_child_type(node, *types):
            for c in node.children:
                if c.type in types:
                    return c
            return None

        def child_types(node):
            return {c.type for c in node.children}

        if lang in ('javascript', 'typescript'):
            def js_bases(node):
                # No stable named field for the extends/implements clause across the
                # javascript/typescript grammars -- both expose it as a class_heritage
                # child instead, containing one or more identifier/type_identifier
                # tokens for the base class and any TS `implements` interfaces.
                heritage = find_child_type(node, 'class_heritage')
                if not heritage:
                    return []
                names = []
                for c in heritage.children:
                    if c.type in ('identifier', 'type_identifier'):
                        names.append(text_of(c))
                    for gc in getattr(c, 'children', ()):
                        if gc.type in ('identifier', 'type_identifier'):
                            names.append(text_of(gc))
                return names

            def js_is_public(node, name):
                # TS wraps private/protected/public in an accessibility_modifier child
                # (its own node, not a bare token type like 'static'/'async'/'get'/
                # 'set') -- so it needs its text checked separately from child_types().
                # ES private fields/methods (`#name`) are unambiguous on the name alone.
                if name.startswith('#'):
                    return False
                mod = find_child_type(node, 'accessibility_modifier')
                return mod is None or text_of(mod) == 'public'

            def walk(node, enclosing_class, at_module_scope):
                t = node.type
                next_class = enclosing_class
                next_scope = at_module_scope and t != 'statement_block'

                if t == 'class_declaration':
                    name_node = child(node, 'name', 'identifier', 'type_identifier')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("class", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "class", name, rel, line_of(node), end_line_of(node),
                                      {"bases": js_bases(node), "kind": "class", "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)
                        next_class = name

                elif t == 'method_definition' and enclosing_class is not None:
                    name_node = child(node, 'name', 'property_identifier', 'private_property_identifier')
                    if name_node:
                        name = text_of(name_node)
                        kinds = child_types(node)
                        method_kind = ("constructor" if name == "constructor" else
                                       "getter" if 'get' in kinds else
                                       "setter" if 'set' in kinds else "method")
                        nid = self._nid("func", rel, f"{enclosing_class}.{name}", line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": 'async' in kinds,
                                       "is_public": js_is_public(node, name), "is_static": 'static' in kinds,
                                       "kind": method_kind, "class": enclosing_class})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t in ('public_field_definition', 'field_definition') and enclosing_class is not None:
                    # Class-field arrow method (`foo = (x) => {...}`) -- a common React/
                    # Node pattern for auto-bound methods, invisible to extract_regex()'s
                    # top-level-only arrow_fn pattern (which requires `const`).
                    value = child(node, 'value', 'arrow_function', 'function')
                    name_node = child(node, 'property', 'property_identifier', 'private_property_identifier')
                    if value is not None and value.type in ('arrow_function', 'function') and name_node:
                        name = text_of(name_node)
                        nid = self._nid("func", rel, f"{enclosing_class}.{name}", line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": 'async' in child_types(value),
                                       "is_public": js_is_public(node, name), "kind": "method", "class": enclosing_class})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'function_declaration':
                    name_node = child(node, 'name', 'identifier')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("func", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": 'async' in child_types(node),
                                       "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'lexical_declaration' and at_module_scope:
                    # Top-level `const X = (...) => {}` -- deliberately narrower than
                    # extract_regex()'s line-based arrow_fn pattern, which (being scope-
                    # blind) also fires on a `const helper = () => {}` defined *inside*
                    # a function body. Real scope info means we don't have to make that
                    # mistake here. A plain `export const X = <literal>` (no function
                    # value) still becomes a `variable` node, matching extract_regex()'s
                    # 'export' fallback pattern -- dropping it here would be a real
                    # regression versus the regex engine, not just a narrower match.
                    is_exported = node.parent is not None and node.parent.type == 'export_statement'
                    for decl in (c for c in node.children if c.type == 'variable_declarator'):
                        name_node = child(decl, 'name', 'identifier')
                        value = child(decl, 'value', 'arrow_function', 'function')
                        if not name_node:
                            continue
                        name = text_of(name_node)
                        if value is not None and value.type in ('arrow_function', 'function'):
                            nid = self._nid("func", rel, name, line_of(node))
                            self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                          {"signature": redact(decl_line(name_node, node)), "is_async": 'async' in child_types(value),
                                           "is_public": True, "kind": "arrow"})
                            self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                            symtab[name].append(nid)
                        elif is_exported:
                            nid = self._nid("var", rel, name, line_of(node))
                            self.add_node(bucket_nodes, nid, "variable", name, rel, line_of(node), line_of(node),
                                          {"signature": redact(decl_line(name_node, node)), "is_public": True})
                            self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                            symtab[name].append(nid)

                elif t == 'interface_declaration':
                    name_node = child(node, 'name', 'type_identifier')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("type", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "type", name, rel, line_of(node), end_line_of(node),
                                      {"kind": "interface", "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'type_alias_declaration':
                    name_node = child(node, 'name', 'type_identifier')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("type", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "type", name, rel, line_of(node), end_line_of(node),
                                      {"kind": "type", "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'import_statement':
                    src_node = find_child_type(node, 'string')
                    if src_node:
                        src = text_of(src_node).strip('"\'')
                        nid = self._nid("import", rel, line_of(node), "import")
                        self.add_node(bucket_nodes, nid, "import", src, rel, line_of(node), line_of(node),
                                      {"style": "import", "raw": redact(first_line(node))})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

                elif t == 'call_expression':
                    fn = child(node, 'function', 'identifier')
                    if fn is not None and fn.type == 'identifier' and text_of(fn) == 'require':
                        args = child(node, 'arguments', 'arguments')
                        str_arg = find_child_type(args, 'string') if args else None
                        if str_arg:
                            src = text_of(str_arg).strip('"\'')
                            nid = self._nid("import", rel, line_of(node), "require")
                            self.add_node(bucket_nodes, nid, "import", src, rel, line_of(node), line_of(node),
                                          {"style": "require", "raw": redact(first_line(node))})
                            self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                    _record_js_callsite(fn)

                elif t == 'new_expression':
                    _record_js_callsite(child(node, 'constructor', 'identifier', 'member_expression'))

                for c in node.children:
                    walk(c, next_class, next_scope)

            def _record_js_callsite(callee):
                # callee is the call_expression's `function` / new_expression's
                # `constructor` node. A real AST node here means this is genuinely a
                # call, so `if (`, `while (`, `switch (`, `catch (`, a bare
                # parenthesised group -- none of which are call_expression -- never
                # reach this, which is the whole point of doing it structurally rather
                # than with `re.findall(r"\bname\(")`. Two callee shapes are named:
                #   foo(...)        -> identifier            -> name "foo",  recv None
                #   a.b.foo(...)    -> member_expression     -> name "foo",  recv "a.b"
                # Anything else (foo()(), arr[k](), tagged templates) can't be named
                # against the symbol table and is skipped.
                if bucket_callsites is None or callee is None:
                    return
                if callee.type == 'identifier':
                    bucket_callsites.append({"name": text_of(callee), "recv": None, "line": line_of(callee)})
                elif callee.type == 'member_expression':
                    prop = callee.child_by_field_name('property')
                    obj = callee.child_by_field_name('object')
                    if prop is not None and prop.type in ('property_identifier', 'private_property_identifier'):
                        recv = text_of(obj).strip() if obj is not None else None
                        bucket_callsites.append({"name": text_of(prop),
                                                 "recv": recv if recv and len(recv) <= 60 else None,
                                                 "line": line_of(callee)})

            walk(tree.root_node, None, True)

        elif lang == 'java':
            def java_bases(node):
                names = []
                sup = node.child_by_field_name('superclass')
                if sup:
                    names.extend(text_of(c) for c in sup.children if c.type == 'type_identifier')
                ifaces = node.child_by_field_name('interfaces')
                if ifaces:
                    for tl in ifaces.children:
                        for c in getattr(tl, 'children', ()):
                            if c.type == 'type_identifier':
                                names.append(text_of(c))
                return names

            def java_is_public(node):
                mods = find_child_type(node, 'modifiers')
                if mods is None:
                    return True  # package-private: no modifier keyword present at all
                mod_types = {c.type for c in mods.children}
                return 'public' in mod_types or not ({'private', 'protected'} & mod_types)

            def walk(node, enclosing_class):
                t = node.type
                next_class = enclosing_class

                if t in ('class_declaration', 'interface_declaration'):
                    name_node = child(node, 'name', 'identifier')
                    if name_node:
                        name = text_of(name_node)
                        ntype = "class" if t == 'class_declaration' else "type"
                        nid = self._nid("class" if ntype == "class" else "type", rel, name, line_of(node))
                        meta = {"bases": java_bases(node), "kind": t.replace('_declaration', ''),
                                "is_public": java_is_public(node)}
                        self.add_node(bucket_nodes, nid, ntype, name, rel, line_of(node), end_line_of(node), meta)
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)
                        next_class = name

                elif t in ('method_declaration', 'constructor_declaration') and enclosing_class is not None:
                    name_node = child(node, 'name', 'identifier')
                    if name_node:
                        name = text_of(name_node)
                        mods = find_child_type(node, 'modifiers')
                        mod_types = {c.type for c in mods.children} if mods else set()
                        nid = self._nid("func", rel, f"{enclosing_class}.{name}", line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                       "is_public": java_is_public(node), "is_static": 'static' in mod_types,
                                       "kind": "constructor" if t == 'constructor_declaration' else "method",
                                       "class": enclosing_class})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'import_declaration':
                    parts = [c for c in node.children if c.type in ('identifier', 'scoped_identifier', 'asterisk')]
                    if parts:
                        src = text_of(parts[-1]) if parts[-1].type != 'asterisk' else text_of(node).strip().rstrip(';').replace('import ', '')
                        nid = self._nid("import", rel, line_of(node))
                        self.add_node(bucket_nodes, nid, "import", src, rel, line_of(node), line_of(node),
                                      {"style": "import", "raw": redact(first_line(node))})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

                for c in node.children:
                    walk(c, next_class)

            walk(tree.root_node, None)

        elif lang == 'go':
            # Go has no visibility keywords -- capitalization of the identifier IS
            # exported-ness, per the language spec, not a heuristic.
            def go_is_public(name):
                return name[:1].isupper()

            def go_receiver_type(node):
                # method_declaration's 'receiver' field is a parameter_list holding one
                # parameter_declaration whose 'type' field is either a type_identifier
                # directly or a pointer_type wrapping one (`func (g *Greeter) ...` is
                # the idiomatic pointer-receiver form).
                recv = node.child_by_field_name('receiver')
                if recv is None:
                    return None
                for c in recv.children:
                    if c.type != 'parameter_declaration':
                        continue
                    rt = c.child_by_field_name('type')
                    if rt is None:
                        continue
                    if rt.type == 'pointer_type':
                        inner = find_child_type(rt, 'type_identifier')
                        if inner:
                            return text_of(inner)
                    elif rt.type == 'type_identifier':
                        return text_of(rt)
                return None

            def walk(node):
                t = node.type

                if t == 'function_declaration':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("func", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                       "is_public": go_is_public(name)})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'method_declaration':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        recv_type = go_receiver_type(node)
                        nid = self._nid("func", rel, f"{recv_type}.{name}" if recv_type else name, line_of(node))
                        meta = {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                "is_public": go_is_public(name), "kind": "method"}
                        if recv_type:
                            meta["class"] = recv_type
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node), meta)
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'type_declaration':
                    # `type X struct {...}` / `type X interface {...}` -- other type_spec
                    # shapes (type aliases, e.g. `type ID = string`) are deliberately not
                    # surfaced as their own kind, matching the regex engine's Go coverage
                    # (function/struct/interface/import only -- see extraction_patterns.md).
                    for spec in (c for c in node.children if c.type == 'type_spec'):
                        name_node = spec.child_by_field_name('name')
                        type_node = spec.child_by_field_name('type')
                        if not name_node or type_node is None:
                            continue
                        name = text_of(name_node)
                        if type_node.type == 'struct_type':
                            nid = self._nid("class", rel, name, line_of(node))
                            self.add_node(bucket_nodes, nid, "class", name, rel, line_of(node), end_line_of(node),
                                          {"bases": [], "kind": "struct", "is_public": go_is_public(name)})
                            self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                            symtab[name].append(nid)
                        elif type_node.type == 'interface_type':
                            nid = self._nid("type", rel, name, line_of(node))
                            self.add_node(bucket_nodes, nid, "type", name, rel, line_of(node), end_line_of(node),
                                          {"kind": "interface", "is_public": go_is_public(name)})
                            self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                            symtab[name].append(nid)

                elif t == 'import_declaration':
                    # A single `import "pkg"` has an import_spec directly; a grouped
                    # `import (...)` wraps each spec in an import_spec_list -- normalize
                    # to the same spec-iteration either way.
                    specs = find_child_type(node, 'import_spec_list')
                    spec_nodes = list(specs.children) if specs else node.children
                    for spec in spec_nodes:
                        if spec.type != 'import_spec':
                            continue
                        path_node = spec.child_by_field_name('path')
                        if path_node is None:
                            continue
                        src_path = text_of(path_node).strip('"')
                        nid = self._nid("import", rel, line_of(spec), "import")
                        self.add_node(bucket_nodes, nid, "import", src_path, rel, line_of(spec), line_of(spec),
                                      {"style": "import", "raw": redact(first_line(spec))})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

                for c in node.children:
                    walk(c)

            walk(tree.root_node)

        elif lang == 'rust':
            def rust_is_pub(node):
                return find_child_type(node, 'visibility_modifier') is not None

            def rust_is_async(node):
                mods = find_child_type(node, 'function_modifiers')
                return mods is not None and find_child_type(mods, 'async') is not None

            rust_type_nodes = {}  # name -> node dict just added to bucket_nodes (same-file
                                   # only -- see the impl_item branch below for why)

            def walk(node, enclosing_type):
                t = node.type
                next_type = enclosing_type

                if t == 'struct_item':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("class", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "class", name, rel, line_of(node), end_line_of(node),
                                      {"bases": [], "kind": "struct", "is_public": rust_is_pub(node)})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)
                        rust_type_nodes[name] = bucket_nodes[-1]

                elif t == 'enum_item':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("sym", rel, "enum", name, line_of(node))
                        self.add_node(bucket_nodes, nid, "symbol", name, rel, line_of(node), end_line_of(node),
                                      {"kind": "enum", "is_public": rust_is_pub(node)})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'trait_item':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("type", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "type", name, rel, line_of(node), end_line_of(node),
                                      {"kind": "trait", "is_public": rust_is_pub(node)})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'impl_item':
                    # `impl Type {...}` (inherent) or `impl Trait for Type {...}` -- either
                    # way, methods inside are scoped to Type. For a trait impl, record the
                    # trait as one of Type's "bases" so the generic bases->inherits resolver
                    # (see the module-level note near add_edge/resolve time) picks it up --
                    # this only works when the struct's own node was already created
                    # earlier in THIS file's walk (the overwhelmingly common case: the
                    # struct/impl live in the same file, impl after the definition), since
                    # rust_type_nodes is per-file. A struct defined in one file with its
                    # trait impl in another is a known, documented gap (see
                    # extraction_patterns.md) rather than something silently claimed to work.
                    type_node = node.child_by_field_name('type')
                    trait_node = node.child_by_field_name('trait')
                    target_name = text_of(type_node) if type_node is not None else None
                    trait_name = text_of(trait_node) if trait_node is not None else None
                    if trait_name and target_name and target_name in rust_type_nodes:
                        rust_type_nodes[target_name]["metadata"].setdefault("bases", []).append(trait_name)
                    next_type = target_name

                elif t == 'function_item':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        is_async = rust_is_async(node)
                        is_pub = rust_is_pub(node)
                        if enclosing_type:
                            nid = self._nid("func", rel, f"{enclosing_type}.{name}", line_of(node))
                            meta = {"signature": redact(decl_line(name_node, node)), "is_async": is_async,
                                    "is_public": is_pub, "kind": "method", "class": enclosing_type}
                        else:
                            nid = self._nid("func", rel, name, line_of(node))
                            meta = {"signature": redact(decl_line(name_node, node)), "is_async": is_async,
                                    "is_public": is_pub}
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node), meta)
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'use_declaration':
                    arg = node.child_by_field_name('argument')
                    if arg is not None:
                        src_path = text_of(arg)
                        nid = self._nid("import", rel, line_of(node), "use")
                        self.add_node(bucket_nodes, nid, "import", src_path, rel, line_of(node), line_of(node),
                                      {"style": "use", "raw": redact(first_line(node))})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

                for c in node.children:
                    walk(c, next_type)

            walk(tree.root_node, None)

        elif lang in ('c', 'cpp'):
            def c_is_static(node):
                return any(c.type == 'storage_class_specifier' and text_of(c) == 'static' for c in node.children)

            def cpp_bases(node):
                bcc = find_child_type(node, 'base_class_clause')
                if not bcc:
                    return []
                return [text_of(c) for c in bcc.children if c.type == 'type_identifier']

            def walk(node, enclosing_class):
                t = node.type
                next_class = enclosing_class

                if lang == 'cpp' and t == 'class_specifier':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("class", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "class", name, rel, line_of(node), end_line_of(node),
                                      {"bases": cpp_bases(node), "kind": "class", "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)
                        next_class = name

                elif t == 'struct_specifier':
                    name_node = node.child_by_field_name('name')
                    # A struct with a field_declaration_list is a real definition; a bare
                    # `struct Point p;` reference also parses as struct_specifier but has
                    # no body -- skip those so a forward-declared/used-only struct name
                    # doesn't produce a spurious duplicate node at every reference site.
                    if name_node and find_child_type(node, 'field_declaration_list') is not None:
                        name = text_of(name_node)
                        nid = self._nid("class", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "class", name, rel, line_of(node), end_line_of(node),
                                      {"bases": cpp_bases(node) if lang == 'cpp' else [], "kind": "struct", "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)
                        if lang == 'cpp':
                            next_class = name

                elif t == 'function_definition':
                    # The return type can wrap the declarator in pointer_declarator/
                    # reference_declarator (`char* foo()`, `int& bar()`) -- search the
                    # whole subtree for the actual function_declarator rather than
                    # assuming it's the direct 'declarator' field.
                    fdecl = None
                    for n2 in _walk_all(node):
                        if n2.type == 'function_declarator':
                            fdecl = n2
                            break
                    if fdecl is not None:
                        name_node = fdecl.child_by_field_name('declarator')
                        if name_node is not None and name_node.type in ('identifier', 'field_identifier'):
                            name = text_of(name_node)
                            is_static = c_is_static(node)
                            if enclosing_class:
                                nid = self._nid("func", rel, f"{enclosing_class}.{name}", line_of(node))
                                meta = {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                        "is_public": not is_static, "kind": "method", "class": enclosing_class}
                            else:
                                nid = self._nid("func", rel, name, line_of(node))
                                meta = {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                        "is_public": not is_static}
                            self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node), meta)
                            self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                            symtab[name].append(nid)

                elif t == 'preproc_include':
                    path_node = node.child_by_field_name('path')
                    if path_node is not None:
                        src_path = text_of(path_node).strip('"<>')
                        nid = self._nid("import", rel, line_of(node), "include")
                        self.add_node(bucket_nodes, nid, "import", src_path, rel, line_of(node), line_of(node),
                                      {"style": "include", "raw": redact(first_line(node))})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

                for c in node.children:
                    walk(c, next_class)

            def _walk_all(node):
                yield node
                for c in node.children:
                    yield from _walk_all(c)

            walk(tree.root_node, None)

        elif lang == 'php':
            def php_is_public(node):
                mod = find_child_type(node, 'visibility_modifier')
                return mod is None or text_of(mod) == 'public'

            def php_bases(node):
                bases = []
                bc = find_child_type(node, 'base_clause')
                if bc:
                    bases.extend(text_of(c) for c in bc.children if c.type == 'name')
                ic = find_child_type(node, 'class_interface_clause')
                if ic:
                    bases.extend(text_of(c) for c in ic.children if c.type == 'name')
                return bases

            def walk(node, enclosing_class):
                t = node.type
                next_class = enclosing_class

                if t in ('class_declaration', 'interface_declaration'):
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        ntype = "class" if t == 'class_declaration' else "type"
                        nid = self._nid("class" if ntype == "class" else "type", rel, name, line_of(node))
                        meta = {"bases": php_bases(node), "kind": "class" if ntype == "class" else "interface",
                                "is_public": True}
                        self.add_node(bucket_nodes, nid, ntype, name, rel, line_of(node), end_line_of(node), meta)
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)
                        next_class = name

                elif t == 'method_declaration' and enclosing_class is not None:
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        kinds = child_types(node)
                        nid = self._nid("func", rel, f"{enclosing_class}.{name}", line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                       "is_public": php_is_public(node), "is_static": 'static_modifier' in kinds,
                                       "kind": "method", "class": enclosing_class})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t == 'function_definition':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        name = text_of(name_node)
                        nid = self._nid("func", rel, name, line_of(node))
                        self.add_node(bucket_nodes, nid, "function", name, rel, line_of(node), end_line_of(node),
                                      {"signature": redact(decl_line(name_node, node)), "is_async": False,
                                       "is_public": True})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                        symtab[name].append(nid)

                elif t.endswith('_expression') and (t.startswith('require') or t.startswith('include')):
                    str_node = find_child_type(node, 'string')
                    if str_node is not None:
                        src_path = text_of(str_node).strip('\'"')
                        nid = self._nid("import", rel, line_of(node), t)
                        self.add_node(bucket_nodes, nid, "import", src_path, rel, line_of(node), line_of(node),
                                      {"style": t.replace('_expression', ''), "raw": redact(first_line(node))})
                        self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

                for c in node.children:
                    walk(c, next_class)

            walk(tree.root_node, None)

        return True

    # -- regex fallback (all other languages) --------------------------------
    def extract_regex(self, rel: str, content: str, bucket_nodes, bucket_edges, symtab):
        ext = Path(rel).suffix.lower()
        lang = EXT_MAP.get(ext)
        patterns = self.patterns_for(lang) if lang else {}
        if not patterns:
            return

        file_id = self._nid("file", rel)
        lines = content.split('\n')
        # first pass: collect every declaration line (any kind) so we can compute an
        # approximate body span (start of this decl -> start of next decl) for scoping
        # the "calls" search later. Real end-of-block detection would need a real parser.
        decl_lines = []

        for i, line in enumerate(lines, 1):
            matches = {}
            for kind, pattern in patterns.items():
                # re.search, not re.match: several patterns (JS/TS in particular) are
                # deliberately unanchored because the declaration can follow other
                # tokens on the same line. Patterns that must start the line already
                # carry their own literal `^`, which behaves identically under both.
                m = re.search(pattern, line)
                if m and m.groups() and m.group(1):
                    matches[kind] = m

            if not matches:
                continue

            handled_primary = False

            for kind in FUNCTION_KINDS:
                if kind in matches:
                    m = matches[kind]
                    name = m.group(1).strip()
                    nid = self._nid("func", rel, name, i)
                    is_public = 'export' in matches or self._looks_public(lang, line)
                    # kind == 'async_fn' covers `async function foo()`; the second half
                    # covers `const foo = async (...) => {}`, which matches 'arrow_fn'
                    # (not 'async_fn') but is just as async -- check the line itself
                    # rather than trust the matched kind name alone.
                    is_async = kind == 'async_fn' or bool(re.search(r"\basync\b", line))
                    self.add_node(bucket_nodes, nid, "function", name, rel, i, None,
                                  {"signature": redact(line.strip()), "is_async": is_async,
                                   "is_public": is_public,
                                   **({"kind": "arrow"} if kind == 'arrow_fn' else {})})
                    self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                    symtab[name].append(nid)
                    decl_lines.append((i, nid))
                    handled_primary = True

            for kind in CLASS_KINDS:
                if kind in matches:
                    m = matches[kind]
                    name = m.group(1).strip()
                    nid = self._nid("class", rel, name, i)
                    bases = []
                    if m.re.groups >= 2 and m.group(2):
                        bases = [b.strip() for b in m.group(2).split(',') if b.strip()]
                    is_public = 'export' in matches or self._looks_public(lang, line)
                    self.add_node(bucket_nodes, nid, "class", name, rel, i, None,
                                  {"bases": bases, "kind": kind, "is_public": is_public})
                    self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                    symtab[name].append(nid)
                    decl_lines.append((i, nid))
                    handled_primary = True

            for kind in TYPE_KINDS:
                if kind in matches:
                    m = matches[kind]
                    name = m.group(1).strip()
                    nid = self._nid("type", rel, name, i)
                    is_public = 'export' in matches or self._looks_public(lang, line)
                    self.add_node(bucket_nodes, nid, "type", name, rel, i, None,
                                  {"kind": kind, "is_public": is_public})
                    self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                    symtab[name].append(nid)
                    decl_lines.append((i, nid))
                    handled_primary = True

            for kind in IMPORT_KINDS:
                if kind in matches:
                    m = matches[kind]
                    name = (m.group(1) or "").strip()
                    if not name:
                        continue
                    nid = self._nid("import", rel, i, kind)
                    self.add_node(bucket_nodes, nid, "import", name, rel, i, i,
                                  {"style": kind, "raw": redact(line.strip())})
                    self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")

            # 'export const X = ...' / 'export default X' etc: only matched kind on the
            # line, so it would previously vanish silently. Surface it as a variable node.
            if not handled_primary and 'export' in matches:
                m = matches['export']
                name = m.group(1).strip()
                nid = self._nid("var", rel, name, i)
                self.add_node(bucket_nodes, nid, "variable", name, rel, i, i,
                              {"signature": redact(line.strip()), "is_public": True})
                self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                symtab[name].append(nid)
                decl_lines.append((i, nid))

            # Anything from custom_patterns.json that isn't one of the known kinds above
            # (and isn't 'export', already handled) gets a generic node instead of being
            # dropped.
            known_kinds = FUNCTION_KINDS | CLASS_KINDS | TYPE_KINDS | IMPORT_KINDS | {'export'}
            for kind, m in matches.items():
                if kind in known_kinds:
                    continue
                name = (m.group(1) or "").strip() if m.groups() else ""
                if not name:
                    continue
                nid = self._nid("sym", rel, kind, name, i)
                self.add_node(bucket_nodes, nid, "symbol", name, rel, i, i,
                              {"kind": kind, "signature": redact(line.strip())})
                self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                symtab[name].append(nid)
                decl_lines.append((i, nid))

        # second pass: assign approximate line_end = next declaration's start - 1
        decl_lines.sort(key=lambda t: t[0])
        by_id = {n["id"]: n for n in bucket_nodes}
        for idx, (start, nid) in enumerate(decl_lines):
            node = by_id.get(nid)
            if node is None:
                continue
            end = decl_lines[idx + 1][0] - 1 if idx + 1 < len(decl_lines) else len(lines)
            node["line_end"] = max(end, start)

    def _looks_public(self, lang, line):
        stripped = line.strip()
        if lang in ('python',):
            name_part = stripped.split('(')[0]
            return not name_part.rsplit(' ', 1)[-1].startswith('_')
        if lang in ('java', 'c', 'cpp', 'kotlin', 'swift'):
            return 'public' in stripped or ('private' not in stripped and 'protected' not in stripped)
        if lang == 'rust':
            return stripped.startswith('pub') or 'pub ' in stripped[:12]
        return True

    # -- entry point detection ------------------------------------------------
    # NOTE: these run with re.search(..., re.MULTILINE) against the *whole file*, not
    # line-by-line. `^` anchors to a line start under MULTILINE, but a plain `\s*` right
    # after it can still cross the newline into a preceding blank line and make the match
    # (and therefore the computed line number) start one or more lines too early. Use
    # `[ \t]*` here, never `\s*`, to stay within the matched line.
    ENTRY_CHECKS = {
        '.py': [r"if\s+__name__\s*==\s*[\"']__main__[\"']"],
        '.js': [r"\.listen\s*\(", r"createServer\s*\("],
        '.ts': [r"\.listen\s*\(", r"createServer\s*\(", r"NestFactory\.create"],
        '.go': [r"^[ \t]*func\s+main\s*\("],
        '.rs': [r"^[ \t]*fn\s+main\s*\("],
        '.java': [r"^[ \t]*public\s+static\s+void\s+main\s*\("],
        '.rb': [r"^[ \t]*if\s+__FILE__\s*==\s*\$0"],
        '.php': [r"^<\?php"],
    }

    def _python_main_guard_span(self, content):
        """Precise (start, end) of the top-level `if __name__ == "__main__":` block,
        via AST rather than regex -- used so the entrypoint's body span for "calls"
        resolution is the guard block itself, not "rest of the file", which would
        wrongly attribute any code that happens to follow it (in the same file) to
        the entrypoint."""
        try:
            tree = ast_module.parse(content)
        except SyntaxError:
            return None
        for node in ast_module.iter_child_nodes(tree):
            if not isinstance(node, ast_module.If):
                continue
            test = node.test
            if (isinstance(test, ast_module.Compare)
                    and isinstance(test.left, ast_module.Name) and test.left.id == "__name__"):
                return node.lineno, getattr(node, 'end_lineno', None)
        return None

    def detect_entrypoints(self, rel: str, content: str, bucket_nodes, bucket_edges):
        ext = Path(rel).suffix.lower()
        file_id = self._nid("file", rel)
        for pat in self.ENTRY_CHECKS.get(ext, []):
            m = re.search(pat, content, re.MULTILINE)
            if not m:
                continue

            marker_line = content[:m.start()].count('\n') + 1
            line_start = marker_line
            line_end = None
            whole_file = True

            if ext == '.py':
                span = self._python_main_guard_span(content)
                if span:
                    line_start, line_end = span
                    whole_file = False
            else:
                # For a language where the entrypoint marker *is* a function
                # declaration (func main / fn main / public static void main), that
                # function was very likely already extracted as its own node with a
                # real (approximate) line_end -- reuse it instead of defaulting to
                # "rest of the file", which would misattribute any later top-level
                # code in the file to the entrypoint.
                for n in bucket_nodes:
                    if n["path"] == rel and n["line_start"] == line_start and n["type"] in ("function", "symbol"):
                        line_end = n.get("line_end")
                        whole_file = False
                        break
                if whole_file:
                    # The marker line is *not* itself a function declaration --
                    # `.listen(`/`createServer(` (JS/TS), `if __FILE__ == $0` (Ruby),
                    # `<?php` (PHP), or Java's `main(` when no method node exists to
                    # borrow a span from (Java methods aren't extracted at all -- see
                    # extraction_patterns.md). Every one of these is documented as
                    # "whole file" body, but the marker line is routinely *not* line 1
                    # -- an Express app's `app.listen(...)` is idiomatically the very
                    # last line, after all routes/middleware are registered, and
                    # Ruby's `if __FILE__ == $0` guard is conventionally at the
                    # bottom of the script too. Scanning from the marker line to EOF
                    # (the previous behavior) would silently miss everything written
                    # *before* it -- for a typical Express app, that's effectively
                    # everything the entrypoint does. Found via a multi-hop calls
                    # chain that should have reached the entrypoint and didn't.
                    line_start = 1

            nid = self._nid("entry", rel)
            # marker_line is stored separately from line_start/line_end so
            # resolve_references can blank out just that one line when scanning for
            # calls, regardless of whether the body span is "whole file" or a single
            # function's block -- Java's `public static void main(` line contains the
            # literal text "main(" and, left in the scan, self-matches as a call to
            # "main" anywhere else in the project (this is exactly the false positive
            # that was found and fixed once already for go/rust; it silently came
            # back for java specifically when whole_file scanning was added, since
            # java has no function node to borrow a narrower span from and the marker
            # line is therefore *inside* the "whole file" range instead of before it).
            self.add_node(bucket_nodes, nid, "entrypoint", f"main:{Path(rel).name}", rel,
                          line_start, line_end,
                          {"detected_by": pat, "whole_file": whole_file, "marker_line": marker_line})
            self.add_edge(bucket_edges, file_id, nid, "contains", "INFERRED")
            bucket_nodes[-1]["_entrypoint"] = True
            break

    # -- custom-patterns-only pass ---------------------------------------------
    # Python normally goes through extract_python_ast(), never extract_regex(), so a
    # custom_patterns.json entry under "python" (e.g. matching an @app.route(...)
    # decorator, which the AST walk above has no opinion on) would otherwise never run
    # against a syntactically valid Python file. This runs *only* the user-supplied
    # patterns -- never the built-in ones, which AST already covers more precisely --
    # so it's safe to call unconditionally alongside AST extraction.
    def extract_custom_only(self, rel: str, content: str, bucket_nodes, bucket_edges, symtab, lang):
        custom = self.custom_patterns.get(lang)
        if not custom:
            return
        file_id = self._nid("file", rel)
        for i, line in enumerate(content.split('\n'), 1):
            for kind, pattern in custom.items():
                m = re.search(pattern, line)
                if not m or not m.groups() or not m.group(1):
                    continue
                name = m.group(1).strip()
                nid = self._nid("sym", rel, kind, name, i)
                self.add_node(bucket_nodes, nid, "symbol", name, rel, i, i,
                              {"kind": kind, "signature": redact(line.strip())})
                self.add_edge(bucket_edges, file_id, nid, "contains", "EXTRACTED")
                symtab[name].append(nid)

    # -- per-file extraction --------------------------------------------------
    def extract_file(self, fp: Path):
        rel = str(fp.relative_to(self.root).as_posix())
        try:
            content = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            vlog(f"skip (unreadable) {rel}: {e}")
            return None

        bucket_nodes = []
        bucket_edges = []
        symtab = defaultdict(list)
        # Real call sites ({"name","recv","line"}) captured from a tree-sitter AST for
        # the languages that support it (JS/TS today -- see CALLSITE_TS_LANGS). Stays a
        # list only when such an engine actually ran; left None otherwise so
        # resolve_references() can tell "tree-sitter file, no calls" from "regex file,
        # use the body-text scan".
        bucket_callsites = []
        emitted_callsites = False

        file_id = self._nid("file", rel)
        self.add_node(bucket_nodes, file_id, "file", fp.name, rel, None, None,
                      {"language": EXT_MAP.get(fp.suffix.lower(), "unknown")})

        ext = fp.suffix.lower()
        lang = EXT_MAP.get(ext)
        ts_key = 'tsx' if ext == '.tsx' else lang

        if ext == '.py':
            ok = self.extract_python_ast(rel, content, bucket_nodes, bucket_edges, symtab)
            if not ok:
                vlog(f"AST parse failed, falling back to regex: {rel}")
                self.extract_regex(rel, content, bucket_nodes, bucket_edges, symtab)
            else:
                self.extract_custom_only(rel, content, bucket_nodes, bucket_edges, symtab, 'python')
        elif lang in ('javascript', 'typescript', 'java', 'go', 'rust', 'c', 'cpp', 'php') and ts_key in TREE_SITTER_LANGS:
            ok = self.extract_tree_sitter(rel, content, bucket_nodes, bucket_edges, symtab, lang, ts_key,
                                          bucket_callsites)
            if not ok:
                vlog(f"tree-sitter extraction failed, falling back to regex: {rel}")
                self.extract_regex(rel, content, bucket_nodes, bucket_edges, symtab)
            else:
                self.extract_custom_only(rel, content, bucket_nodes, bucket_edges, symtab, lang)
                emitted_callsites = lang in CALLSITE_TS_LANGS
        else:
            self.extract_regex(rel, content, bucket_nodes, bucket_edges, symtab)

        self.detect_entrypoints(rel, content, bucket_nodes, bucket_edges)

        try:
            mtime = fp.stat().st_mtime
        except OSError:
            mtime = time.time()

        entry = {
            "mtime": mtime,
            "nodes": bucket_nodes,
            "edges": bucket_edges,
        }
        if emitted_callsites:
            entry["callsites"] = bucket_callsites
        return entry

    # -- cache merge ------------------------------------------------------
    def _reset_derived_state(self):
        self.nodes = []
        self.edges = []
        self.node_map = {}
        self.symbol_map = defaultdict(list)
        self.communities = []
        self.god_nodes = []
        self.stats = defaultdict(int)
        self.entrypoints = []
        self._edge_seen = set()
        # {rel: [{"name","recv","line"}, ...]} for files parsed by a tree-sitter engine
        # that emits real call_expression callsites (JS/TS today). resolve_references()
        # uses these instead of the regex body-scan for those files -- see there.
        self.ast_callsites = {}

    def _merge_cache_into_graph(self):
        """Rebuild self.nodes/self.edges/self.symbol_map fresh from self.file_cache.
        Called after every extraction pass (full or incremental) -- this is what
        guarantees no duplication across repeated runs."""
        self._reset_derived_state()
        for rel, entry in self.file_cache.items():
            if "callsites" in entry:
                self.ast_callsites[rel] = entry["callsites"]
            for n in entry["nodes"]:
                if n["id"] in self.node_map:
                    continue  # defensive: shouldn't happen, ids are file-scoped
                self.node_map[n["id"]] = len(self.nodes)
                self.nodes.append(dict(n))
                self.stats[f"node_{n['type']}"] += 1
                self.symbol_map[n["name"]].append(n["id"])
                if n.get("_entrypoint"):
                    self.entrypoints.append(n["id"])
            for e in entry["edges"]:
                self._add_unique_edge(e["source"], e["target"], e["type"], e["tag"],
                                       e.get("confidence", 1.0), e.get("metadata"))

        # class inheritance: resolved globally now that the full symbol table exists
        for n in self.nodes:
            if n["type"] != "class":
                continue
            for base in n.get("metadata", {}).get("bases", []):
                base = base.split('.')[-1].strip()
                for bid in self.symbol_map.get(base, []):
                    if bid != n["id"]:
                        self._add_unique_edge(n["id"], bid, "inherits", "EXTRACTED")

    def _add_unique_edge(self, src, tgt, etype, tag, conf=1.0, meta=None):
        key = (src, tgt, etype)
        if key in self._edge_seen:
            return
        self._edge_seen.add(key)
        self.edges.append({
            "id": f"e:{len(self.edges)}", "source": src, "target": tgt,
            "type": etype, "tag": tag, "confidence": conf, "metadata": meta or {},
        })

    # -- filesystem-verified import resolution (v4.2 Phase 4) -----------------
    # `resolve_references()` below already had a *soft* import signal (stem-matching:
    # does the caller's file import something whose path string contains the
    # candidate's own filename stem?) -- that's a nudge, not a resolution, and two
    # unrelated files can share a stem. This is the real thing: given one import
    # statement's module string, does it actually resolve, via real path arithmetic
    # against files that genuinely exist under the project root, to one specific file?
    # Deliberately scoped to the two import syntaxes where that question has an
    # unambiguous answer without parsing build-tool metadata this script doesn't have:
    #   - JS/TS/JSX/TSX relative imports (./foo, ../bar/baz) -- the module string IS a
    #     filesystem path fragment, by language design.
    #   - Python dotted module paths (`import pkg.sub.mod` / `from pkg.sub import x`)
    #     -- resolved from the project root, which is right whenever the project root
    #     doubles as (or contains) the Python package root, the overwhelmingly common
    #     layout; when it doesn't, resolution just finds nothing and this tier is
    #     silently skipped for that import, falling back to the existing heuristic.
    # Rust's `crate::`/`self::`/`super::` paths and Go/Java/PHP/C/C++ imports are NOT
    # attempted here: resolving those correctly needs knowledge this script doesn't
    # parse (Rust's own mod-declaration tree, which doesn't have to mirror the
    # directory layout at all; a Go module path from go.mod; a Java classpath/source-
    # root convention; PHP's composer.json PSR-4 autoload map; a C/C++ include path) --
    # guessing without that would trade one honestly-labeled heuristic (stem matching)
    # for one that merely looks more precise while being just as capable of being
    # wrong, which is the opposite of what this tier is for. Those languages keep using
    # the stem-matching soft boost and the same-file exact tier below instead.
    _JS_TS_EXTS = ('.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs')

    def _resolve_import_targets(self, caller_rel: str, import_name: str, all_file_rels: set) -> set:
        caller_ext = Path(caller_rel).suffix.lower()
        caller_dir = Path(caller_rel).parent
        results = set()

        if caller_ext in ('.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs'):
            if not import_name.startswith('.'):
                return results  # bare package name ("lodash", "react") -- not ours to resolve
            norm = os.path.normpath(str(caller_dir / import_name)).replace(os.sep, '/')
            if Path(norm).suffix:
                if norm in all_file_rels:
                    results.add(norm)
            else:
                for ext in self._JS_TS_EXTS:
                    if f"{norm}{ext}" in all_file_rels:
                        results.add(f"{norm}{ext}")
                for ext in self._JS_TS_EXTS:
                    if f"{norm}/index{ext}" in all_file_rels:
                        results.add(f"{norm}/index{ext}")

        elif caller_ext == '.py':
            mod_parts = [p for p in import_name.split('.') if p]
            if not mod_parts:
                return results
            base = '/'.join(mod_parts)
            for cand in (f"{base}.py", f"{base}/__init__.py"):
                if cand in all_file_rels:
                    results.add(cand)

        return results

    # -- cross-reference resolution ("calls" edges) --------------------------
    # Two sources of call sites feed one shared resolver (resolve_one below):
    #   1. AST call sites (self.ast_callsites) -- for languages whose tree-sitter walk
    #      emits real `call_expression`/`new_expression` nodes (JS/TS today). Structural,
    #      so `if (`/`while (`/`switch (`/`catch (`/a bare parenthesised group never
    #      register as calls, and `a.b.foo()` is captured with its receiver `a.b`.
    #      Each site is credited to the innermost function node covering its line.
    #   2. A regex `\bname(` scan of each function/entrypoint's own body span -- the
    #      fallback for Python, the regex languages, the not-yet-converted tree-sitter
    #      languages (Go/Rust/Java/C/C++/PHP), and any file tree-sitter failed to parse.
    # Both are still name-based at the resolution step, not full scope/type resolution:
    # two unrelated symbols sharing a name can cross-link. The tier system (same-class
    # via `this`, exact same-file, filesystem-verified import, then ambiguity-scored
    # INFERRED) plus the RESOLVED/INFERRED tag and per-edge confidence exist so Claude
    # weighs that uncertainty rather than treating an edge as fact -- see
    # references/query_protocol.md.
    def resolve_references(self):
        callable_types = {"function", "class", "symbol"}
        candidates = defaultdict(list)
        for name, ids in self.symbol_map.items():
            for nid in ids:
                n = self.get_node(nid)
                if n and n["type"] in callable_types:
                    candidates[name].append(nid)

        # Per-file set of "module segment" tokens named in that file's own import/
        # require statements (path components split on / . \, lowercased) -- e.g. a
        # file with `import { X } from "../services/userService"` gets {'..',
        # 'services', 'userservice'}. Used below as a soft confidence signal: when a
        # caller's own file actually imports something that looks like the candidate
        # callee's file, that's real local evidence pointing at that specific
        # candidate, even though matching a filename stem doesn't *prove* the call
        # came through that import (two files could coincidentally share a stem). It's
        # a boost, not a filter -- see the confidence tiers below and
        # references/query_protocol.md, which already frame confidence as a
        # probability signal to weigh, never a verified fact.
        file_import_segments = defaultdict(set)
        for node in self.nodes:
            if node["type"] == "import":
                for seg in re.split(r"[\\/.]", node["name"]):
                    seg = seg.strip().lower()
                    if seg:
                        file_import_segments[node["path"]].add(seg)

        # v4.2 Phase 4: real, filesystem-verified import resolution -- see
        # _resolve_import_targets above for exactly which import syntaxes this covers
        # (JS/TS relative imports, Python dotted module paths) and why the rest
        # (Go/Rust/Java/PHP/C/C++) are deliberately left to the softer stem-matching
        # signal above instead of a guess dressed up to look precise.
        all_file_rels = {n["path"] for n in self.nodes if n["type"] == "file"}
        file_resolved_imports = defaultdict(set)
        for node in self.nodes:
            if node["type"] != "import":
                continue
            resolved = self._resolve_import_targets(node["path"], node["name"], all_file_rels)
            if resolved:
                file_resolved_imports[node["path"]].update(resolved)

        # ---- one call site -> zero-or-more "calls" edges. Shared by the AST-callsite
        #      path and the regex body-scan fallback so the tier logic (same-class via
        #      `this`, same-file, filesystem-verified import, ambiguity-scored INFERRED)
        #      lives in exactly one place. `recv` is the receiver text for a
        #      `recv.name(...)` call (AST path only); None for a bare `name(...)` and
        #      for every call the regex scan finds, since it can't see receivers.
        def resolve_one(caller, name, recv):
            if name == caller["name"] or len(name) < 2:
                return
            cid, rel = caller["id"], caller["path"]
            others = [tid for tid in candidates.get(name, ()) if tid != cid]
            if not others:
                return

            # tier 0 (needs a receiver, so AST path only): `this.m()` / `self.m()`
            # inside a method resolves to a method of the caller's own class in the
            # same file when that's exactly one candidate -- the one spot where a
            # receiver token yields real scope information without a type system.
            caller_class = (caller.get("metadata") or {}).get("class")
            if recv in ("this", "self") and caller_class:
                same_class = [tid for tid in others
                              if ((self.get_node(tid) or {}).get("metadata") or {}).get("class") == caller_class
                              and (self.get_node(tid) or {}).get("path") == rel]
                if len(same_class) == 1:
                    self._add_unique_edge(cid, same_class[0], "calls", "RESOLVED", 0.97,
                                           {"resolved_by": "this_method"})
                    return

            # tier 1: exact same-file resolution. If exactly one name-matched candidate
            # is defined in this very file, a call to that name almost certainly means
            # the local one. A bare `name(...)` (no receiver) can't be a method call, so
            # class methods are dropped from the same-file set when a free function of
            # the name also lives in the file.
            same_file = [tid for tid in others if (self.get_node(tid) or {}).get("path") == rel]
            if recv is None and len(same_file) > 1:
                free = [tid for tid in same_file
                        if not ((self.get_node(tid) or {}).get("metadata") or {}).get("class")]
                if free:
                    same_file = free
            if len(same_file) == 1:
                self._add_unique_edge(cid, same_file[0], "calls", "RESOLVED", 0.97,
                                       {"resolved_by": "same_file"})
                return

            # tier 2: filesystem-verified import resolution. If the caller's file has an
            # import that genuinely resolves on disk to exactly one candidate's own
            # file, that's a resolved path, not a stem coincidence. Narrowing to >1
            # still beats leaving the full project-wide set for the tier below.
            resolved_files = file_resolved_imports.get(rel)
            if resolved_files:
                import_matches = [tid for tid in others if (self.get_node(tid) or {}).get("path") in resolved_files]
                if len(import_matches) == 1:
                    self._add_unique_edge(cid, import_matches[0], "calls", "RESOLVED", 0.93,
                                           {"resolved_by": "import"})
                    return
                if import_matches:
                    others = import_matches

            # tier 3: ambiguity-scored INFERRED. Confidence reflects how many unrelated
            # symbols share the name project-wide (a unique `chargeCard` is near-certain;
            # a `get`/`close`/`run` is barely a guess), plus the import-proximity stem
            # boost -- see references/query_protocol.md.
            k = len(others)
            base_conf = 0.85 if k == 1 else 0.55 if k <= 3 else 0.35 if k <= 8 else 0.2
            caller_imports = file_import_segments.get(rel)
            for tid in others:
                conf = base_conf
                if caller_imports:
                    cand = self.get_node(tid)
                    stem = Path(cand["path"]).stem.lower() if cand else ""
                    if stem and stem in caller_imports:
                        conf = min(0.95, conf + 0.25)
                self._add_unique_edge(cid, tid, "calls", "INFERRED", conf)

        # ---- AST-callsite path: files a tree-sitter engine handed us real call sites
        #      for (JS/TS today). Each call site is credited to the innermost function
        #      node whose line span covers it -- an anonymous callback has no node of
        #      its own, so a call inside one is correctly credited to the enclosing
        #      named function/method. A call at module scope goes to the file's
        #      entrypoint node when it has one, matching the regex path's whole-file
        #      entrypoint span.
        funcs_by_file = defaultdict(list)
        for node in self.nodes:
            if node["type"] in ("function", "entrypoint"):
                funcs_by_file[node["path"]].append(node)

        for rel, sites in self.ast_callsites.items():
            fnodes = funcs_by_file.get(rel, [])
            entry_node = next((n for n in fnodes if n["type"] == "entrypoint"), None)
            span_funcs = [n for n in fnodes
                          if n["type"] == "function" and n.get("line_start") and n.get("line_end")]
            for cs in sites:
                line = cs.get("line") or 0
                enclosing = None
                for n in span_funcs:
                    if n["line_start"] <= line <= n["line_end"] and (
                        enclosing is None
                        or (n["line_end"] - n["line_start"]) < (enclosing["line_end"] - enclosing["line_start"])
                    ):
                        enclosing = n
                caller = enclosing or entry_node
                if caller is not None:
                    resolve_one(caller, cs["name"], cs.get("recv"))

        # ---- regex body-scan fallback: every function/entrypoint in a file the AST
        #      path did not cover (Python, the regex languages, Go/Rust/Java/C/C++/PHP
        #      for now, and any file where tree-sitter parsing failed).
        file_lines = {}
        for node in self.nodes:
            if node["type"] not in ("function", "entrypoint"):
                continue
            rel = node["path"]
            if rel in self.ast_callsites:
                continue
            if rel not in file_lines:
                fp = self.root / rel
                try:
                    file_lines[rel] = fp.read_text(encoding="utf-8", errors="ignore").split('\n')
                except OSError:
                    file_lines[rel] = []
            lines = file_lines[rel]
            if not lines:
                continue

            start = max(1, node.get("line_start") or 1)
            end = min(len(lines), max(node.get("line_end") or len(lines), start))
            body_lines = lines[start - 1:end]

            marker_line = node.get("metadata", {}).get("marker_line") if node["type"] == "entrypoint" else None
            if marker_line and start <= marker_line <= end:
                # Blank out the entrypoint marker's own line, wherever it falls in the
                # scanned range, rather than shifting the range's start/end -- a
                # declaration-shaped marker (`public static void main(`, `func main(`,
                # `fn main(`) contains a literal self-mention of its own name that
                # would otherwise misread as a call to some unrelated same-named
                # function elsewhere in the project (bit go/rust once, then bit java
                # again the same way once "whole file" scanning started including the
                # marker line itself instead of starting after it -- see
                # detect_entrypoints). Blanking the single line, instead of skipping a
                # whole line at the start of the range, is what makes this correct for
                # both the narrow go/rust/java-with-node span *and* the whole-file
                # span, without the two cases needing different start-index logic.
                body_lines = list(body_lines)
                body_lines[marker_line - start] = ""
            body = "\n".join(body_lines)

            for name in set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", body)):
                resolve_one(node, name, None)

    # -- community detection ---------------------------------------------------
    def detect_communities(self):
        """Dispatches on self.community_algo (set from --community-algo, default
        'auto'): 'leiden' forces real graph clustering and raises if the optional
        packages aren't installed (so a caller who explicitly asked for Leiden finds
        out immediately, not via a silent fallback); 'directory' forces the original
        v3 path-grouping heuristic; 'auto' (the default) prefers Leiden when both
        `python-igraph` and `leidenalg` are importable, falling back to directory
        grouping otherwise -- same opt-in-but-transparent contract as tree-sitter/
        ladybug elsewhere in this script. Either path fills self.communities with the
        exact same shape ({"id", "name", "nodes", "size", "description"}), so nothing
        downstream (the report template, --community, graph_db sync) needs to know or
        care which one actually ran."""
        if self.community_algo == 'directory':
            return self._detect_communities_directory()
        if self.community_algo == 'leiden':
            if not LEIDEN_AVAILABLE:
                raise RuntimeError(
                    "--community-algo leiden was requested but `python-igraph`/`leidenalg` "
                    "aren't installed -- pip install python-igraph leidenalg, or drop back "
                    "to --community-algo auto (or directory) to use the built-in heuristic."
                )
            return self._detect_communities_leiden()
        # 'auto'
        if LEIDEN_AVAILABLE:
            return self._detect_communities_leiden()
        return self._detect_communities_directory()

    def _detect_communities_directory(self):
        comms = defaultdict(list)
        for n in self.nodes:
            d = str(Path(n["path"]).parent)
            comms[d].append(n["id"])

        self.communities = []
        for i, (dname, nids) in enumerate(sorted(comms.items(), key=lambda x: -len(x[1]))):
            for nid in nids:
                idx = self.node_map.get(nid)
                if idx is not None:
                    self.nodes[idx]["community"] = i
            type_counts = defaultdict(int)
            for nid in nids:
                idx = self.node_map.get(nid)
                if idx is not None:
                    type_counts[self.nodes[idx]["type"]] += 1
            desc_bits = ", ".join(f"{v} {k}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1]) if k != "file")
            self.communities.append({
                "id": i, "name": dname or "root",
                "nodes": nids, "size": len(nids),
                "description": f"{desc_bits} across files in `{dname or '.'}`" if desc_bits else "No symbols extracted here.",
            })

    # -- community detection (Leiden, v4.4 Phase 5, optional) -------------------
    # Real graph clustering instead of "group by which folder the file happens to be
    # in": two directories a directory-heuristic would never connect (a `services/`
    # module and the `handlers/` files that actually call it every time) can end up in
    # the same Leiden community when the calls/inherits edges say they belong together,
    # and a single directory that actually holds two unrelated feature areas can get
    # split -- both things the path-string grouping above structurally cannot do,
    # since it never looks at an edge at all. Only "calls"/"inherits" edges are used as
    # clustering input; "contains" (file -> its own symbols) is excluded on purpose --
    # feeding it in would just recreate the directory grouping through a different
    # mechanism (every symbol pulled toward its own file) and defeat the entire point
    # of switching algorithms.
    def _detect_communities_leiden(self):
        real_edges = [e for e in self.edges if e["type"] in ("calls", "inherits")]
        if len(real_edges) < 5:
            # Too little real-relationship signal for clustering to mean anything --
            # a handful of edges on a small/script-like project would just produce
            # noise dressed up as a sophisticated result. The directory heuristic is
            # at least honestly what it says it is in that case.
            vlog("leiden: fewer than 5 calls/inherits edges, falling back to directory grouping")
            return self._detect_communities_directory()

        node_ids = [n["id"] for n in self.nodes]
        index = {nid: i for i, nid in enumerate(node_ids)}
        edge_list = []
        weights = []
        for e in real_edges:
            if e["source"] not in index or e["target"] not in index:
                continue
            edge_list.append((index[e["source"]], index[e["target"]]))
            weights.append(max(0.05, e.get("confidence", 1.0)))  # never fully zero-weight an edge
        if not edge_list:
            return self._detect_communities_directory()

        try:
            g = _igraph.Graph()
            g.add_vertices(len(node_ids))
            g.add_edges(edge_list)
            partition = _leidenalg.find_partition(
                g, _leidenalg.RBConfigurationVertexPartition,
                weights=weights, seed=42, resolution_parameter=1.0,
            )
            membership = partition.membership
        except Exception as e:
            # Never let an optional-dependency internal error take the whole build
            # down -- same "degrade, don't crash" contract as a tree-sitter parse
            # failure falling back to regex.
            vlog(f"leiden clustering failed ({e}), falling back to directory grouping")
            return self._detect_communities_directory()

        # Every vertex was added (even ones touched by no calls/inherits edge at all --
        # most imports, most classes in a small project), so igraph still assigns each
        # of those its own singleton partition. Reporting dozens of one-node
        # "communities" would drown out the real clusters in --community output, so
        # every true singleton is folded into one shared "ungrouped" bucket instead of
        # being listed as its own community.
        raw = defaultdict(list)
        for nid, vi in index.items():
            raw[membership[vi]].append(nid)
        comms = {}
        ungrouped = []
        for cid, nids in raw.items():
            if len(nids) == 1:
                ungrouped.extend(nids)
            else:
                comms[cid] = nids
        if ungrouped:
            comms['ungrouped'] = ungrouped

        self.communities = []
        for i, (cid, nids) in enumerate(sorted(comms.items(), key=lambda x: -len(x[1]))):
            for nid in nids:
                idx = self.node_map.get(nid)
                if idx is not None:
                    self.nodes[idx]["community"] = i
            if cid == 'ungrouped':
                self.communities.append({
                    "id": i, "name": "ungrouped (no calls/inherits connections)",
                    "nodes": nids, "size": len(nids),
                    "description": "Symbols with no calls/inherits edge to anything else in "
                                    "the project -- isolated by the graph itself, not grouped "
                                    "by directory.",
                })
                continue
            type_counts = defaultdict(int)
            dir_counts = defaultdict(int)
            for nid in nids:
                idx = self.node_map.get(nid)
                if idx is not None:
                    type_counts[self.nodes[idx]["type"]] += 1
                    dir_counts[str(Path(self.nodes[idx]["path"]).parent)] += 1
            desc_bits = ", ".join(f"{v} {k}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1]) if k != "file")
            top_dir = max(dir_counts.items(), key=lambda x: x[1])[0] if dir_counts else "root"
            spans = len(dir_counts)
            name = f"{top_dir or 'root'} (+{spans - 1} more dir{'s' if spans > 2 else ''})" if spans > 1 else (top_dir or "root")
            span_note = f", spanning {spans} directories" if spans > 1 else ""
            self.communities.append({
                "id": i, "name": name,
                "nodes": nids, "size": len(nids),
                "description": (f"{desc_bits}, connected by calls/inherits{span_note}" if desc_bits
                                 else "No symbols extracted here."),
            })

    # -- metrics ---------------------------------------------------------
    def compute_metrics(self):
        deg = defaultdict(int)
        for e in self.edges:
            deg[e["source"]] += 1
            deg[e["target"]] += 1
        for n in self.nodes:
            n["degree"] = deg.get(n["id"], 0)

        # "file" nodes are excluded from god_nodes: every symbol in a file has a
        # "contains" edge back to it, so file nodes trivially accumulate the highest
        # degree in any codebase and would otherwise bury the actually interesting
        # highly-connected functions/classes under a wall of file entries.
        self.god_nodes = [
            n["id"] for n in sorted(self.nodes, key=lambda x: x["degree"], reverse=True)
            if n["degree"] > 0 and n["type"] != "file"
        ][:15]

    # -- build orchestration ------------------------------------------------
    def _load_disk_cache(self):
        cache_path = self.root / GRAPH_DIR / CACHE_FILE
        if not cache_path.exists():
            return {}
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_disk_cache(self):
        out_dir = self.root / GRAPH_DIR
        out_dir.mkdir(exist_ok=True)
        (out_dir / CACHE_FILE).write_text(json.dumps(self.file_cache), encoding="utf-8")

    def build(self, incremental=False, force=False):
        """incremental=False: re-parse every discovered file (full rebuild).
        incremental=True: reuse cached extraction for files whose mtime hasn't
        changed since the last run (on-disk cache), only re-parsing what changed,
        and dropping cache entries for files that were deleted.
        Returns True on success, False if the shrink-guard below refused to proceed --
        callers that run once (main()) should treat False as a hard failure; callers
        that run repeatedly (watch_mode/poll_mode) should just skip that rebuild and
        keep watching, since the guard will naturally re-fire on the next detected
        change without crashing the whole watcher."""
        t0 = time.time()
        log("[discovery] walking project tree...")
        files = self.discover()
        log(f"   {len(files)} files found")

        # Shrink-guard: refuse to overwrite a previously-good graph with one built from
        # a suspiciously collapsed file scan, rather than silently committing it. This
        # is exactly the failure mode a real bug once caused during this project's own
        # development -- a broken .gitignore matcher made discover() report "0 files
        # found" on a real project -- which would otherwise have quietly overwritten a
        # correct graph.json with an empty one. A `--update` naturally drops files one
        # at a time as they're actually deleted; this only fires when discover() itself
        # collapses, which a legitimate incremental prune does not do at this scale.
        prior_cache = self._load_disk_cache()
        if not force and prior_cache and len(prior_cache) >= 5 and len(files) < max(1, len(prior_cache) * 0.1):
            log(f"[!] refusing to continue: discover() found only {len(files)} file(s), "
                f"down from {len(prior_cache)} previously tracked in .file_cache.json. "
                f"This looks more like a bug (wrong path, a .gitignore/.codegraphignore "
                f"pattern matching everything, a permissions issue) than a real change -- "
                f"re-run with --verbose to see what discover() is skipping and why. If "
                f"this drop is actually expected (a real, deliberate prune of the "
                f"project), re-run with --force-rebuild to proceed anyway.")
            return False

        disk_cache = prior_cache if incremental else {}
        current_rels = set()
        reused, reparsed = 0, 0

        log("[extract] parsing files...")
        new_cache = {}
        for fp in files:
            rel = str(fp.relative_to(self.root).as_posix())
            current_rels.add(rel)
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                mtime = None

            cached = disk_cache.get(rel)
            # Force a one-time re-parse of an otherwise-fresh JS/TS file whose cache
            # entry predates AST call-site capture (no "callsites" key), but only when
            # the tree-sitter engine that would produce them is actually importable --
            # otherwise this would re-parse the file on every incremental build forever.
            _ext = fp.suffix.lower()
            _ts_key = 'tsx' if _ext == '.tsx' else EXT_MAP.get(_ext)
            stale_schema = (cached is not None
                            and EXT_MAP.get(_ext) in CALLSITE_TS_LANGS
                            and _ts_key in TREE_SITTER_LANGS
                            and "callsites" not in cached)
            if incremental and cached and not stale_schema and mtime is not None and abs(cached.get("mtime", -1) - mtime) < 1e-6:
                new_cache[rel] = cached
                reused += 1
                continue

            entry = self.extract_file(fp)
            if entry is not None:
                new_cache[rel] = entry
                reparsed += 1

        removed = len(disk_cache) - len(set(disk_cache) & current_rels)
        self.file_cache = new_cache
        if incremental:
            log(f"   {reparsed} file(s) (re)parsed, {reused} served from cache, {removed} removed")

        log("[link] merging cache, resolving references, communities, metrics...")
        self._merge_cache_into_graph()
        self.resolve_references()
        self.detect_communities()
        self.compute_metrics()

        self._save_disk_cache()
        self.save()
        log(f"[done] {time.time() - t0:.2f}s")
        return True

    # -- save ------------------------------------------------------------
    def to_graph_dict(self):
        return {
            "version": VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "project_root": str(self.root),
            "stats": dict(self.stats),
            "total_nodes": len(self.nodes),
            "total_edges": len(self.edges),
            "total_communities": len(self.communities),
            "entrypoints": self.entrypoints,
            "god_nodes": self.god_nodes,
            "nodes": self.nodes,
            "edges": self.edges,
            "communities": self.communities,
        }

    def save(self):
        out = self.root / GRAPH_DIR
        out.mkdir(exist_ok=True)
        graph = self.to_graph_dict()

        # v4.4 Phase 5: rotate a single-slot "previous build" snapshot before
        # overwriting graph.json, so `--diff` (no label) always has something to
        # compare the just-built graph against -- "what changed since the last build",
        # for free, with no unbounded history to manage. This is deliberately only one
        # slot deep, not a log: a real history is what --snapshot/--diff <name> is for.
        existing = out / GRAPH_FILE
        if existing.exists():
            try:
                shutil.copyfile(existing, out / PREV_GRAPH_FILE)
            except OSError as e:
                vlog(f"could not rotate {PREV_GRAPH_FILE}: {e}")

        with open(out / GRAPH_FILE, "w", encoding="utf-8") as f:
            json.dump(graph, f, indent=2)

        write_report(self, graph, out / REPORT_FILE)
        write_html(self, graph, out / HTML_FILE)

        log(f"[saved] {out}/")
        log(f"   nodes: {len(self.nodes)} | edges: {len(self.edges)} | communities: {len(self.communities)}")
        log(f"   LLM tokens spent on this build: 0 (deterministic local script -- ast/regex, "
            f"no model call). Claude only reads {REPORT_FILE} or the --explain/--callers/"
            f"--find-path/--trace-entrypoints/--impact command output that follow.")

    def load_existing(self):
        """Load .codegraph/graph.json into self.* without touching any source file or
        re-running extraction. Used by --report and by the query commands below (which
        exist precisely so a targeted question never requires loading the whole file
        into an LLM context -- see the module docstring and 'Querying the graph without
        loading the whole file' further down)."""
        gj = self.root / GRAPH_DIR / GRAPH_FILE
        if not gj.exists():
            return False
        graph = json.loads(gj.read_text(encoding="utf-8"))
        self.nodes = graph["nodes"]
        self.node_map = {n["id"]: i for i, n in enumerate(self.nodes)}
        self.edges = graph["edges"]
        self.communities = graph["communities"]
        self.god_nodes = graph["god_nodes"]
        self.entrypoints = graph["entrypoints"]
        return True

    def report_only(self):
        """--report: rebuild GRAPH_REPORT.md and graph.html from the existing
        graph.json without touching a single source file."""
        if not self.load_existing():
            log("[!] no .codegraph/graph.json yet -- run a full build first.")
            return
        graph = self.to_graph_dict()
        write_report(self, graph, self.root / GRAPH_DIR / REPORT_FILE)
        write_html(self, graph, self.root / GRAPH_DIR / HTML_FILE)
        log(f"[saved] regenerated {REPORT_FILE} and {HTML_FILE} from existing graph.json")

    def export_obsidian(self):
        """--export-obsidian: write the current graph.json out as an Obsidian vault (one
        Markdown note per node, wikilinked to every connected node) without touching a
        single source file -- same read-only-on-graph.json contract as --report. Opt-in
        and separate from the normal build path: most projects using this skill are
        never opened in Obsidian, so writing hundreds/thousands of note files on every
        incremental rebuild would be wasted work for everyone who doesn't ask for it."""
        if not self.load_existing():
            log("[!] no .codegraph/graph.json yet -- run a full build first.")
            return
        graph = self.to_graph_dict()
        vault_dir = self.root / GRAPH_DIR / OBSIDIAN_DIR
        count = write_obsidian(self, graph, vault_dir)
        log(f"[saved] {count} note(s) written to {vault_dir}/")
        log(f"   open this folder as an Obsidian vault (File > Open folder as vault), or "
            f"copy/symlink it into an existing vault, for a native graph view of the codebase.")

    # -- Ladybug/Cypher (v4.0): parallel, opt-in, read-only-on-graph.json -------------
    # This does NOT replace graph.json or the --explain/--callers/--find-path/
    # --trace-entrypoints/--impact commands, which stay the primary, zero-setup
    # interface (no dependency to install, purpose-built output). --sync-graphdb/
    # --cypher exist for questions those fixed commands don't cover -- multi-hop
    # pattern matches, aggregations, ad-hoc filters -- without writing a new cmd_*
    # method in this script for every such need. Schema is deliberately generic (one
    # Symbol node table, one Edge rel table, each with a type-discriminator property)
    # rather than one table per node/edge type: the graph's type vocabulary already
    # lives in graph.json and can grow (custom_patterns.json, more languages) without
    # a schema migration here.
    def sync_graphdb(self):
        """--sync-graphdb: export the existing graph.json into a local Ladybug (formerly
        Kuzu) embedded graph database at .codegraph/graph_db/, queryable with real
        Cypher via --cypher. Regenerated fresh each time (the directory is fully
        rebuilt, not merged) so a renamed/deleted node never leaves a stale row behind
        -- same reasoning as --export-obsidian's note cleanup, simpler here since
        nothing else legitimately lives in this directory the way a user might drop
        their own notes into an Obsidian vault."""
        if not LADYBUG_AVAILABLE:
            log("[!] ladybug not installed (pip install ladybug) -- --sync-graphdb/--cypher "
                "are unavailable. Everything else (build, --report, --explain/--callers/"
                "--find-path/--trace-entrypoints/--impact) is unaffected.")
            return
        if not self.load_existing():
            log("[!] no .codegraph/graph.json yet -- run a full build first.")
            return
        graph_json_path = self.root / GRAPH_DIR / GRAPH_FILE
        try:
            graph_json_mtime = graph_json_path.stat().st_mtime
        except OSError:
            graph_json_mtime = None
        graph = self.to_graph_dict()
        db_dir = self.root / GRAPH_DIR / GRAPHDB_DIR
        if db_dir.exists():
            # Ladybug (this version, 0.20.2) stores the database as a single file, not
            # a directory -- shutil.rmtree() on that raises NotADirectoryError. An
            # older/different version could still use a directory, so handle both
            # rather than assuming one shape. Found by actually re-running
            # --sync-graphdb against an existing graph_db/ (v4.0's own testing had
            # only ever exercised the "doesn't exist yet" path before -- fixed in v4.1).
            if db_dir.is_dir():
                shutil.rmtree(db_dir)
            else:
                db_dir.unlink()

        tmp_dir = self.root / GRAPH_DIR / ".graphdb_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        nodes_csv = tmp_dir / "nodes.csv"
        edges_csv = tmp_dir / "edges.csv"
        try:
            with open(nodes_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for n in graph["nodes"]:
                    w.writerow([
                        n["id"], n["name"], n["type"], n["path"],
                        n.get("line_start") if n.get("line_start") is not None else -1,
                        n.get("line_end") if n.get("line_end") is not None else -1,
                        n.get("community") if n.get("community") is not None else -1,
                        n.get("degree", 0),
                        json.dumps(n.get("metadata", {})),
                    ])
            with open(edges_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for e in graph["edges"]:
                    w.writerow([e["source"], e["target"], e["type"], e["tag"],
                                e.get("confidence", 1.0), json.dumps(e.get("metadata", {}))])

            db = _ladybug.Database(str(db_dir))
            conn = _ladybug.Connection(db)
            conn.execute(
                "CREATE NODE TABLE Symbol(id STRING, name STRING, ntype STRING, path STRING, "
                "line_start INT64, line_end INT64, community INT64, degree INT64, meta STRING, "
                "PRIMARY KEY(id))"
            )
            conn.execute(
                "CREATE REL TABLE Edge(FROM Symbol TO Symbol, etype STRING, tag STRING, "
                "confidence DOUBLE, meta STRING)"
            )
            conn.execute(f"COPY Symbol FROM '{nodes_csv.as_posix()}' (HEADER=false)")
            conn.execute(f"COPY Edge FROM '{edges_csv.as_posix()}' (HEADER=false)")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        # v4.1: record which graph.json build this sync came from (by file mtime --
        # cheap to check and, unlike generated_at, never requires parsing the
        # potentially-tens-of-MB graph.json just to answer "is graph_db/ stale". Empirically
        # verified that only a real build (save()) touches graph.json's mtime -- --report/
        # --export-obsidian/--sync-graphdb itself never rewrite it -- so this can't false-
        # positive off one of those). Read by cmd_cypher() to warn (not block -- this is a
        # read-only escape hatch, not another shrink-guard) when graph_db/ predates the
        # current graph.json.
        meta_path = self.root / GRAPH_DIR / GRAPHDB_SYNC_META_FILE
        try:
            meta_path.write_text(json.dumps({
                "graph_json_mtime": graph_json_mtime,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "nodes": len(graph["nodes"]),
                "edges": len(graph["edges"]),
            }), encoding="utf-8")
        except OSError as e:
            vlog(f"could not write {GRAPHDB_SYNC_META_FILE}: {e} (sync succeeded; only the "
                 f"staleness check on the next --cypher will be unavailable)")

        log(f"[saved] {len(graph['nodes'])} node(s), {len(graph['edges'])} edge(s) synced to {db_dir}/")
        log(f"   query with: python codegraph_builder.py {self.root} --cypher \"MATCH (n:Symbol) RETURN n.name LIMIT 5\"")

    def _graphdb_stale_warning(self):
        """v4.1: None if .codegraph/graph_db/ looks in sync with the current graph.json,
        else a human-readable warning string. Compares the graph.json mtime recorded at
        the last --sync-graphdb (in GRAPHDB_SYNC_META_FILE) against the current
        graph.json's mtime on disk -- cheap (no need to parse a potentially tens-of-MB
        file just to answer this) and reliable (only a real build ever touches that
        mtime; --report/--export-obsidian/--sync-graphdb itself don't). Returns None,
        not a guess, when there's nothing to compare (graph_db/ predates this tracking,
        or was never synced) -- silence here means "unknown", not "fresh". This is
        informational only: cmd_cypher() below prints it but never blocks on it, since
        --cypher is a read-only escape hatch, not another shrink-guard -- the user
        decides whether to trust a possibly-stale answer or re-run --sync-graphdb."""
        meta_path = self.root / GRAPH_DIR / GRAPHDB_SYNC_META_FILE
        graph_json_path = self.root / GRAPH_DIR / GRAPH_FILE
        if not meta_path.exists() or not graph_json_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            current_mtime = graph_json_path.stat().st_mtime
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        synced_mtime = meta.get("graph_json_mtime")
        if synced_mtime is None or current_mtime <= synced_mtime + 1e-6:
            return None
        return (f"graph_db/ may be stale -- it was synced from an older graph.json "
                f"(last --sync-graphdb: {meta.get('synced_at', 'unknown time')}, "
                f"{meta.get('nodes', '?')} nodes/{meta.get('edges', '?')} edges at that "
                f"time; graph.json has been rebuilt since). Run --sync-graphdb to refresh "
                f"before trusting these results for anything changed since then.")

    def cmd_cypher(self, query, as_json=False):
        """--cypher: run an arbitrary read query against .codegraph/graph_db/ (built by
        --sync-graphdb) and print the result. Schema: Symbol(id, name, ntype, path,
        line_start, line_end, community, degree, meta) and Edge(FROM Symbol TO Symbol,
        etype, tag, confidence, meta) -- ntype/etype hold this graph's own type
        vocabulary ('function'/'class'/...,'contains'/'calls'/'inherits'), meta is a
        JSON string (this schema is generic on purpose -- see the block comment above
        sync_graphdb()). This is a general escape hatch, not a replacement for --explain/
        --callers/--find-path/--trace-entrypoints/--impact: prefer those for the
        questions they already cover (they need no dependency and their output is
        purpose-formatted), and reach for --cypher for what they don't -- multi-hop
        pattern matches, aggregations, ad-hoc filters across many nodes at once."""
        if not LADYBUG_AVAILABLE:
            print("ladybug not installed (pip install ladybug) -- --cypher is unavailable.")
            return
        db_dir = self.root / GRAPH_DIR / GRAPHDB_DIR
        if not db_dir.exists():
            print(f"no {GRAPHDB_DIR}/ yet -- run --sync-graphdb first (it builds this from "
                  f"the existing graph.json, no source files touched).")
            return

        stale_warning = self._graphdb_stale_warning()
        if stale_warning and not as_json:
            print(f"[!] {stale_warning}")

        try:
            db = _ladybug.Database(str(db_dir))
            conn = _ladybug.Connection(db)
            result = conn.execute(query)
        except Exception as e:
            msg = str(e)
            if as_json:
                out = {"error": msg}
                if stale_warning:
                    out["warning"] = stale_warning
                print(json.dumps(out))
            else:
                print(f"Cypher error: {msg}")
            return

        cols = result.get_column_names()
        rows = []
        while result.has_next():
            rows.append(result.get_next())

        if as_json:
            out = {"columns": cols, "rows": rows}
            if stale_warning:
                out["warning"] = stale_warning
            print(json.dumps(out, indent=2, default=str))
            return
        if not rows:
            print("(no rows)")
            return
        print("  ".join(cols))
        for row in rows:
            print("  ".join(str(v) for v in row))

    # -- querying the graph without loading the whole file into an LLM context -------
    # graph.json is not small: on a 206-file / ~10.5k-node slice of the Python stdlib
    # used to test this script, it came out to 31.7MB. Telling Claude to "read
    # graph.json" for every question does not actually deliver the "zero token waste"
    # this skill promises once a project is more than toy-sized -- only
    # GRAPH_REPORT.md (a few KB) reliably does. These commands do the lookup/traversal
    # here, in the script, and print only the relevant slice, so a targeted question
    # ("what calls X", "how does A connect to B") costs a few hundred tokens against a
    # graph.json of any size, the same way building the graph never cost tokens.
    def _find_nodes(self, needle):
        exact_id = self.get_node(needle)
        if exact_id:
            return [exact_id]
        exact_name = [n for n in self.nodes if n["name"] == needle]
        if exact_name:
            return exact_name
        ci = [n for n in self.nodes if n["name"].lower() == needle.lower()]
        if ci:
            return ci
        needle_l = needle.lower()
        return [n for n in self.nodes if needle_l in n["name"].lower()]

    def _edges_touching(self, node_id):
        out = [e for e in self.edges if e["source"] == node_id]
        inc = [e for e in self.edges if e["target"] == node_id]
        return inc, out

    def _community_name(self, cid):
        for c in self.communities:
            if c["id"] == cid:
                return c["name"]
        return "?"

    def _resolve_ambiguous(self, needle, matches, limit=20):
        print(f"{len(matches)} nodes match '{needle}':")
        for n in matches[:limit]:
            print(f"  {n['id']}  ({n['type']}, {n['path']}:{n.get('line_start') or '?'})")
        if len(matches) > limit:
            print(f"  ... and {len(matches) - limit} more (narrow the query)")
        print("Re-run with the exact id shown above to disambiguate.")

    def cmd_explain(self, needle, as_json=False):
        matches = self._find_nodes(needle)
        if not matches:
            print(f"No node matches '{needle}'.")
            return
        if len(matches) > 1:
            if as_json:
                print(json.dumps({"ambiguous": [n["id"] for n in matches]}, indent=2))
            else:
                self._resolve_ambiguous(needle, matches)
            return

        n = matches[0]
        inc, out = self._edges_touching(n["id"])
        idx = {nd["id"]: nd for nd in self.nodes}

        def edge_view(e, other_key):
            other = idx.get(e[other_key])
            return {
                "node": other["name"] if other else e[other_key],
                "id": e[other_key],
                "type": e["type"], "tag": e["tag"], "confidence": e["confidence"],
            }

        result = {
            "id": n["id"], "name": n["name"], "type": n["type"], "path": n["path"],
            "line_start": n.get("line_start"), "line_end": n.get("line_end"),
            "metadata": n.get("metadata", {}),
            "community": self._community_name(n.get("community")),
            "degree": n.get("degree", 0),
            "uses": [edge_view(e, "target") for e in out],
            "used_by": [edge_view(e, "source") for e in inc],
        }
        if as_json:
            print(json.dumps(result, indent=2))
            return

        print(f"{n['name']}  ({n['type']})  {n['path']}:{n.get('line_start') or '?'}"
              + (f"-{n['line_end']}" if n.get('line_end') else ""))
        for k, v in n.get("metadata", {}).items():
            print(f"  {k}: {v}")
        print(f"  community: {result['community']}  degree: {result['degree']}")
        if result["uses"]:
            print("  uses:")
            for u in sorted(result["uses"], key=lambda x: -x["confidence"]):
                print(f"    -> {u['node']}  [{u['type']}/{u['tag']}"
                      + (f", conf={u['confidence']}" if u["type"] == "calls" else "") + "]")
        if result["used_by"]:
            print("  used by:")
            for u in sorted(result["used_by"], key=lambda x: -x["confidence"]):
                print(f"    <- {u['node']}  [{u['type']}/{u['tag']}"
                      + (f", conf={u['confidence']}" if u["type"] == "calls" else "") + "]")

    def cmd_callers(self, needle, as_json=False):
        matches = self._find_nodes(needle)
        if not matches:
            print(f"No node matches '{needle}'.")
            return
        if len(matches) > 1:
            if as_json:
                print(json.dumps({"ambiguous": [n["id"] for n in matches]}, indent=2))
            else:
                self._resolve_ambiguous(needle, matches)
            return
        n = matches[0]
        idx = {nd["id"]: nd for nd in self.nodes}
        inc = [e for e in self.edges if e["target"] == n["id"] and e["type"] in ("calls", "inherits")]
        inc.sort(key=lambda e: -e["confidence"])
        if as_json:
            print(json.dumps([{"node": idx[e["source"]]["name"], "id": e["source"],
                                "type": e["type"], "confidence": e["confidence"]} for e in inc], indent=2))
            return
        if not inc:
            print(f"Nothing in the graph points to '{n['name']}' via calls/inherits.")
            return
        print(f"{len(inc)} caller(s)/subclass(es) of {n['name']} ({n['path']}):")
        for e in inc:
            src = idx.get(e["source"])
            print(f"  {src['name'] if src else e['source']}  ({src['path'] if src else '?'})"
                  f"  [{e['type']}" + (f", conf={e['confidence']}" if e["type"] == "calls" else "") + "]")

    def cmd_path(self, a, b, as_json=False):
        na, nb = self._find_nodes(a), self._find_nodes(b)
        if not na or not nb:
            print(f"Could not resolve {'both symbols' if not na and not nb else ('%r' % a if not na else '%r' % b)}.")
            return
        if len(na) > 1:
            self._resolve_ambiguous(a, na)
            return
        if len(nb) > 1:
            self._resolve_ambiguous(b, nb)
            return
        start, goal = na[0]["id"], nb[0]["id"]

        adj = defaultdict(list)  # undirected: connectivity matters more than direction here
        for e in self.edges:
            adj[e["source"]].append((e["target"], e))
            adj[e["target"]].append((e["source"], e))

        # Weighted shortest path (Dijkstra), not plain BFS: "contains" edges connect
        # everything in the same file, so an unweighted search happily reports "they're
        # both in file X" as the answer to any question about two symbols that happen
        # to live in the same file -- true, but rarely what's being asked. Giving
        # `contains` a higher cost than calls/inherits means a real code relationship is
        # preferred whenever one exists at the same or shorter hop count.
        weight = {"contains": 2.0, "calls": 1.0, "inherits": 1.0}
        dist = {start: 0.0}
        prev = {start: None}
        heap = [(0.0, start)]
        visited = set()
        while heap:
            d, cur = heapq.heappop(heap)
            if cur in visited:
                continue
            visited.add(cur)
            if cur == goal:
                break
            for nxt, e in adj[cur]:
                nd = d + weight.get(e["type"], 1.5)
                if nxt not in dist or nd < dist[nxt]:
                    dist[nxt] = nd
                    prev[nxt] = (cur, e)
                    heapq.heappush(heap, (nd, nxt))

        if goal not in prev:
            msg = f"No connecting path found between {na[0]['name']} and {nb[0]['name']} in the graph."
            print(json.dumps({"path": None}) if as_json else msg)
            return

        path_ids = [goal]
        hops = []
        cur = goal
        while prev[cur] is not None:
            cur, e = prev[cur]
            path_ids.append(cur)
            hops.append(e)
        path_ids.reverse()
        hops.reverse()

        idx = {nd["id"]: nd for nd in self.nodes}
        if as_json:
            print(json.dumps({
                "path": [idx[i]["name"] for i in path_ids],
                "hops": [{"type": h["type"], "tag": h["tag"], "confidence": h["confidence"]} for h in hops],
            }, indent=2))
            return

        print(f"{na[0]['name']} -> {nb[0]['name']}  ({len(hops)} hop(s)):")
        for i, h in enumerate(hops):
            print(f"  {idx[path_ids[i]]['name']} --[{h['type']}/{h['tag']}"
                  + (f", conf={h['confidence']}" if h["type"] == "calls" else "")
                  + f"]--> {idx[path_ids[i+1]]['name']}")

    def cmd_trace_entrypoints(self, needle, as_json=False, max_results=5, max_depth=12):
        """Reverse-search from `needle` through incoming calls/inherits edges until
        hitting node(s) of type 'entrypoint'. Exists so 'find entry points to X' -- a
        query_protocol.md pattern that previously meant Claude calling --callers
        repeatedly by hand on each result -- costs one script invocation instead of
        several round trips, the same token-economy argument as the other query
        commands. `contains` edges are deliberately excluded from this search (unlike
        --find-path): "a file-mate of X" is not "something that leads to X being
        called", so mixing it in here would surface irrelevant, misleading chains."""
        matches = self._find_nodes(needle)
        if not matches:
            print(f"No node matches '{needle}'.")
            return
        if len(matches) > 1:
            if as_json:
                print(json.dumps({"ambiguous": [n["id"] for n in matches]}, indent=2))
            else:
                self._resolve_ambiguous(needle, matches)
            return
        n = matches[0]
        idx = {nd["id"]: nd for nd in self.nodes}

        if n["type"] == "entrypoint":
            msg = f"{n['name']} ({n['path']}) is itself an entrypoint."
            print(json.dumps({"symbol": n["name"], "is_entrypoint": True, "entrypoints": []}, indent=2)
                  if as_json else msg)
            return

        radj = defaultdict(list)
        for e in self.edges:
            if e["type"] in ("calls", "inherits"):
                radj[e["target"]].append((e["source"], e))

        start = n["id"]
        dist = {start: 0.0}
        prev = {start: None}
        heap = [(0.0, start)]
        visited = set()
        found = []  # [(entrypoint_id, dist), ...] in order discovered (shortest first)

        # Uniform-weight Dijkstra == BFS ordered by hop count; kept as a heap (rather
        # than a plain deque) for the same reason cmd_path uses one -- so a future
        # weight distinction between 'calls' and 'inherits' here doesn't need a
        # rewrite. visited/found caps bound the search on a dense or highly-recursive
        # graph so this stays a sub-second query regardless of project size, matching
        # every other command in this section.
        while heap and len(found) < max_results and len(visited) < 5000:
            d, cur = heapq.heappop(heap)
            if cur in visited:
                continue
            visited.add(cur)
            if d > max_depth:
                continue
            cur_node = idx.get(cur)
            if cur_node and cur_node["type"] == "entrypoint" and cur != start:
                found.append((cur, d))
                continue  # don't walk past an entrypoint -- it's the top of this chain
            for nxt, e in radj.get(cur, ()):
                if nxt in visited:
                    continue
                nd = d + 1.0
                if nxt not in dist or nd < dist[nxt]:
                    dist[nxt] = nd
                    prev[nxt] = (cur, e)
                    heapq.heappush(heap, (nd, nxt))

        if not found:
            msg = (f"No entrypoint found reaching '{n['name']}' within {max_depth} hops via "
                   f"calls/inherits edges. Could be dead/library code never reached from a "
                   f"detected entrypoint, or a call chain this script's regex 'calls' "
                   f"resolution doesn't catch (see references/extraction_patterns.md).")
            print(json.dumps({"symbol": n["name"], "entrypoints": []}, indent=2) if as_json else msg)
            return

        results = []
        for ep_id, d in found:
            path_ids, hops, cur = [ep_id], [], ep_id
            while prev[cur] is not None:
                cur, e = prev[cur]
                path_ids.append(cur)
                hops.append(e)
            path_ids.reverse()
            hops.reverse()
            results.append({
                "entrypoint": idx[ep_id]["name"], "entrypoint_id": ep_id,
                "path": [idx[i]["name"] for i in path_ids],
                "hops": [{"type": h["type"], "tag": h["tag"], "confidence": h["confidence"]} for h in hops],
            })

        if as_json:
            print(json.dumps({"symbol": n["name"], "entrypoints": results}, indent=2))
            return

        print(f"{len(results)} entrypoint(s) reach {n['name']} ({n['path']}):")
        for r in results:
            print(f"  {' -> '.join(r['path'])}")
            for i, h in enumerate(r["hops"]):
                print(f"    {r['path'][i]} --[{h['type']}/{h['tag']}"
                      + (f", conf={h['confidence']}" if h["type"] == "calls" else "")
                      + f"]--> {r['path'][i + 1]}")

    def cmd_impact(self, needle, as_json=False, max_depth=15, max_print=60):
        """Transitive closure of everything that depends on `needle`, walking calls/
        inherits edges backward (same direction as --callers, but the full closure
        rather than one hop) -- "if I change or break this symbol, what else might be
        affected". Unlike --trace-entrypoints, this does NOT stop at the first
        entrypoint reached: it keeps walking outward and reports the complete impacted
        set, while separately flagging which impacted nodes are themselves entrypoints
        (a quick way to see which user-facing paths sit in the blast radius). `contains`
        edges are excluded for the same reason as --trace-entrypoints: a file-mate of X
        is not something that breaks if X breaks."""
        matches = self._find_nodes(needle)
        if not matches:
            print(f"No node matches '{needle}'.")
            return
        if len(matches) > 1:
            if as_json:
                print(json.dumps({"ambiguous": [n["id"] for n in matches]}, indent=2))
            else:
                self._resolve_ambiguous(needle, matches)
            return
        n = matches[0]
        idx = {nd["id"]: nd for nd in self.nodes}

        radj = defaultdict(list)
        for e in self.edges:
            if e["type"] in ("calls", "inherits"):
                radj[e["target"]].append((e["source"], e))

        start = n["id"]
        dist = {start: 0}
        prev = {start: None}
        order = [start]
        visited = {start}
        queue = deque([start])  # plain BFS: hop count is what "blast radius depth" means
        while queue and len(visited) < 5000:
            cur = queue.popleft()
            d = dist[cur]
            if d >= max_depth:
                continue
            for nxt, e in radj.get(cur, ()):
                if nxt in visited:
                    continue
                visited.add(nxt)
                dist[nxt] = d + 1
                prev[nxt] = (cur, e)
                order.append(nxt)
                queue.append(nxt)

        impacted = order[1:]  # exclude the symbol itself
        if not impacted:
            msg = (f"Nothing in the graph transitively calls/inherits from '{n['name']}' -- "
                   f"changing it has no detected blast radius (could also be a call chain "
                   f"this script's heuristic 'calls' resolution doesn't catch -- see "
                   f"references/extraction_patterns.md).")
            print(json.dumps({"symbol": n["name"], "impacted_count": 0, "entrypoints_affected": [],
                               "impacted": []}, indent=2) if as_json else msg)
            return

        entrypoints_affected = sorted(
            {idx[i]["name"] for i in impacted if idx.get(i, {}).get("type") == "entrypoint"}
        )

        results = []
        for i in impacted:
            nd = idx.get(i)
            if not nd:
                continue
            _, e = prev[i]
            results.append({
                "id": i, "name": nd["name"], "type": nd["type"], "path": nd["path"],
                "depth": dist[i],
                "via": {"type": e["type"], "tag": e["tag"], "confidence": e["confidence"]},
            })
        results.sort(key=lambda r: (r["depth"], -r["via"]["confidence"], r["name"]))

        if as_json:
            print(json.dumps({
                "symbol": n["name"], "impacted_count": len(results),
                "entrypoints_affected": entrypoints_affected, "impacted": results,
            }, indent=2))
            return

        print(f"{len(results)} node(s) transitively depend on {n['name']} ({n['path']}) "
              f"via calls/inherits (blast radius, up to {max_depth} hops):")
        print(f"  entrypoints affected: {', '.join(entrypoints_affected) if entrypoints_affected else 'none detected'}")
        shown = results[:max_print]
        for r in shown:
            print(f"  [depth {r['depth']}] {r['name']}  ({r['path']})  "
                  f"[{r['via']['type']}"
                  + (f", conf={r['via']['confidence']}" if r['via']['type'] == "calls" else "") + "]")
        if len(results) > max_print:
            print(f"  ... and {len(results) - max_print} more (use --json for the full list)")

    # -- graph versioning / diff (v4.4 Phase 5) --------------------------------
    def _load_graph_file(self, path: Path):
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _safe_snapshot_name(name: str) -> str:
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', name.strip())
        return safe or "snapshot"

    def cmd_snapshot(self, name: str):
        """--snapshot NAME: copy the current graph.json to
        .codegraph/snapshots/<name>.json, so a later --diff NAME can compare against
        this exact point regardless of how many builds happen in between. Unlike the
        automatic single-slot .graph_prev.json (rotated on every save(), used by
        --diff with no name), a named snapshot is never overwritten except by another
        explicit --snapshot with the same name -- this is the "before I start this
        refactor" checkpoint, not "since the last build"."""
        current_path = self.root / GRAPH_DIR / GRAPH_FILE
        if not current_path.exists():
            log("[!] no .codegraph/graph.json yet -- run a build first.")
            sys.exit(1)
        safe = self._safe_snapshot_name(name)
        snap_dir = self.root / GRAPH_DIR / SNAPSHOTS_DIR
        snap_dir.mkdir(parents=True, exist_ok=True)
        dest = snap_dir / f"{safe}.json"
        shutil.copyfile(current_path, dest)
        log(f"[snapshot] saved current graph.json as '{safe}' -> {dest.relative_to(self.root)}")

    def cmd_diff(self, label, as_json=False):
        """--diff [NAME]: compare the current graph.json against either the automatic
        pre-build snapshot (.graph_prev.json, rotated by every save() -- "what changed
        since the last build/update", no name given) or a named snapshot taken earlier
        with --snapshot NAME. Reports added/removed/changed nodes (by node id) and
        added/removed/changed edges (by (source, target, type), since edge "id" fields
        are just a per-build list index, never stable across rebuilds -- see
        _add_unique_edge/save()).

        Known, deliberate limitation, not silently glossed over: node ids embed the
        symbol's own line number (see _nid()/extract_*), so a symbol that only moved --
        an unrelated blank line added above it, a reordered import shifting everything
        below -- shows up as one "removed" id and one "added" id at a different line,
        not as a single "changed" entry, even though nothing about the symbol itself
        changed. `file`/`entrypoint` nodes don't carry a line number in their id and
        are unaffected by this. A real move-aware diff would need matching nodes by
        (type, name, path) instead of by id when an exact id match fails -- a
        reasonable future improvement, not attempted here to avoid guessing at matches
        that could be wrong (two same-named overloads shifting past each other, for
        instance) and quietly misreporting a real add/remove as a move."""
        current_path = self.root / GRAPH_DIR / GRAPH_FILE
        current = self._load_graph_file(current_path)
        if current is None:
            log("[!] no .codegraph/graph.json yet -- run a build first.")
            sys.exit(1)

        if label is None:
            other_path = self.root / GRAPH_DIR / PREV_GRAPH_FILE
            other_label = "the state before the most recent build/update"
        else:
            safe = self._safe_snapshot_name(label)
            other_path = self.root / GRAPH_DIR / SNAPSHOTS_DIR / f"{safe}.json"
            other_label = f"snapshot '{safe}'"
        other = self._load_graph_file(other_path)
        if other is None:
            if label is None:
                log("[!] no previous-build state found (.codegraph/.graph_prev.json) -- "
                    "this looks like the first build since v4.3, or since the project "
                    "was created. Nothing to diff against yet; run another build/--update "
                    "and try again.")
            else:
                log(f"[!] no snapshot named '{self._safe_snapshot_name(label)}' found -- "
                    f"take one first with --snapshot {label}")
            sys.exit(1)

        old_nodes = {n["id"]: n for n in other.get("nodes", [])}
        new_nodes = {n["id"]: n for n in current.get("nodes", [])}
        added_node_ids = sorted(new_nodes.keys() - old_nodes.keys())
        removed_node_ids = sorted(old_nodes.keys() - new_nodes.keys())
        changed_node_ids = []
        for nid in new_nodes.keys() & old_nodes.keys():
            a, b = old_nodes[nid], new_nodes[nid]
            if (a.get("line_start"), a.get("line_end"), a.get("metadata")) != \
               (b.get("line_start"), b.get("line_end"), b.get("metadata")):
                changed_node_ids.append(nid)
        changed_node_ids.sort()

        def edge_key(e):
            return (e["source"], e["target"], e["type"])

        old_edges = {edge_key(e): e for e in other.get("edges", [])}
        new_edges = {edge_key(e): e for e in current.get("edges", [])}
        added_edge_keys = sorted(new_edges.keys() - old_edges.keys())
        removed_edge_keys = sorted(old_edges.keys() - new_edges.keys())
        changed_edge_keys = []
        for k in new_edges.keys() & old_edges.keys():
            a, b = old_edges[k], new_edges[k]
            if (a.get("tag"), a.get("confidence")) != (b.get("tag"), b.get("confidence")):
                changed_edge_keys.append(k)
        changed_edge_keys.sort()

        result = {
            "compared_to": other_label,
            "nodes": {
                "added": added_node_ids,
                "removed": removed_node_ids,
                "changed": changed_node_ids,
            },
            "edges": {
                "added": [{"source": k[0], "target": k[1], "type": k[2]} for k in added_edge_keys],
                "removed": [{"source": k[0], "target": k[1], "type": k[2]} for k in removed_edge_keys],
                "changed": [{"source": k[0], "target": k[1], "type": k[2],
                             "before": {"tag": old_edges[k]["tag"], "confidence": old_edges[k]["confidence"]},
                             "after": {"tag": new_edges[k]["tag"], "confidence": new_edges[k]["confidence"]}}
                            for k in changed_edge_keys],
            },
        }

        if as_json:
            print(json.dumps(result, indent=2))
            return

        print(f"[diff] comparing current graph.json to {other_label}")
        print(f"  nodes: +{len(added_node_ids)} -{len(removed_node_ids)} ~{len(changed_node_ids)}")
        print(f"  edges: +{len(added_edge_keys)} -{len(removed_edge_keys)} ~{len(changed_edge_keys)}")

        def _print_list(title, items, fmt, cap=50):
            if not items:
                return
            print(f"\n  {title}:")
            for item in items[:cap]:
                print(f"    {fmt(item)}")
            if len(items) > cap:
                print(f"    ... and {len(items) - cap} more (use --json for the full list)")

        _print_list("Added nodes", added_node_ids, lambda nid: f"+ {nid}")
        _print_list("Removed nodes", removed_node_ids, lambda nid: f"- {nid}")
        _print_list("Changed nodes (line/signature/metadata)", changed_node_ids, lambda nid: f"~ {nid}")
        _print_list("Added edges", result["edges"]["added"],
                    lambda e: f"+ {e['source']} --[{e['type']}]--> {e['target']}")
        _print_list("Removed edges", result["edges"]["removed"],
                    lambda e: f"- {e['source']} --[{e['type']}]--> {e['target']}")
        _print_list("Changed edges (tag/confidence)", result["edges"]["changed"],
                    lambda e: (f"~ {e['source']} --[{e['type']}]--> {e['target']}  "
                               f"{e['before']['tag']}/{e['before']['confidence']} -> "
                               f"{e['after']['tag']}/{e['after']['confidence']}"))


# == Report rendering (mustache-subset templating) ============================
_TAG_RE = re.compile(r"\{\{([#^/]?)\s*([A-Za-z0-9_]+)\s*\}\}")


def _parse_template(template):
    tokens = []
    pos = 0
    for m in _TAG_RE.finditer(template):
        if m.start() > pos:
            tokens.append(('text', template[pos:m.start()]))
        sigil, name = m.group(1), m.group(2)
        tokens.append(({'#': 'open', '^': 'openneg', '/': 'close'}.get(sigil, 'var'), name))
        pos = m.end()
    if pos < len(template):
        tokens.append(('text', template[pos:]))

    root = []
    stack = [root]
    for ttype, val in tokens:
        if ttype in ('open', 'openneg'):
            node = {'type': ttype, 'name': val, 'children': []}
            stack[-1].append(node)
            stack.append(node['children'])
        elif ttype == 'close':
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append((ttype, val))
    return root


def _lookup(name, ctx_stack):
    for ctx in reversed(ctx_stack):
        if isinstance(ctx, dict) and name in ctx:
            return ctx[name]
    return None


def _render_nodes(nodes, ctx_stack, out):
    for node in nodes:
        if isinstance(node, tuple):
            ttype, val = node
            if ttype == 'text':
                out.append(val)
            else:  # var
                v = _lookup(val, ctx_stack)
                out.append('' if v is None else str(v))
            continue
        val = _lookup(node['name'], ctx_stack)
        is_list = isinstance(val, list)
        truthy = (len(val) > 0) if is_list else bool(val)
        if node['type'] == 'open':
            if is_list:
                for item in val:
                    _render_nodes(node['children'], ctx_stack + [item], out)
            elif truthy:
                _render_nodes(node['children'], ctx_stack, out)
        elif node['type'] == 'openneg':
            if not truthy:
                _render_nodes(node['children'], ctx_stack, out)


def render_template(template, context):
    tree = _parse_template(template)
    out = []
    _render_nodes(tree, [context], out)
    return ''.join(out)


_FALLBACK_TEMPLATE = """# CodeGraph Report: {{PROJECT_NAME}}

> Generated on {{DATE}} | {{TOTAL_NODES}} nodes | {{TOTAL_EDGES}} edges | {{TOTAL_COMMUNITIES}} communities

## Overview

Root: `{{PROJECT_ROOT}}`
Languages: {{LANGUAGES}}

### God nodes
{{#GOD_NODES}}
- `{{NAME}}` ({{TYPE}}) -- degree {{DEGREE}}, `{{PATH}}:{{LINE}}`
{{/GOD_NODES}}

### Communities
{{#COMMUNITIES}}
- `{{NAME}}` -- {{NODE_COUNT}} nodes -- {{DESCRIPTION}}
{{/COMMUNITIES}}

### Entry points
{{#ENTRY_POINTS}}
- `{{NAME}}` at `{{PATH}}`
{{/ENTRY_POINTS}}
"""


def _detect_architecture_pattern(builder):
    top_dirs = set()
    for c in builder.communities:
        first = c["name"].split('/')[0] if c["name"] != "root" else "root"
        top_dirs.add(first.lower())
    mvc = {"controllers", "models", "views"}
    layered = {"services", "repositories", "handlers"} & top_dirs
    if mvc & top_dirs == mvc:
        return "MVC-style (controllers / models / views detected)"
    if len(layered) >= 2:
        return "Layered / service-repository (heuristic guess)"
    if len(top_dirs) > 6:
        return "Multiple top-level modules -- possibly a monorepo (heuristic guess)"
    return "No strong pattern detected (heuristic guess, verify manually)"


def _build_report_context(builder, graph):
    node_by_id = {n["id"]: n for n in graph["nodes"]}
    comm_by_id = {c["id"]: c for c in graph["communities"]}

    languages = sorted({n.get("metadata", {}).get("language", "unknown")
                         for n in graph["nodes"] if n["type"] == "file"})

    god_nodes = []
    for rank, nid in enumerate(graph["god_nodes"][:15], 1):
        n = node_by_id.get(nid)
        if not n:
            continue
        out_edges = [e for e in graph["edges"] if e["source"] == nid]
        in_edges = [e for e in graph["edges"] if e["target"] == nid]
        connections = []
        for e in out_edges[:5]:
            t = node_by_id.get(e["target"])
            connections.append({"TARGET": t["name"] if t else e["target"], "EDGE_TYPE": e["type"], "TAG": e["tag"]})
        comm = comm_by_id.get(n.get("community"))
        god_nodes.append({
            "RANK": rank, "NAME": n["name"], "TYPE": n["type"], "PATH": n["path"],
            "LINE": n.get("line_start") or "?", "DEGREE": n["degree"],
            "IN_EDGES": len(in_edges), "OUT_EDGES": len(out_edges),
            "COMMUNITY_NAME": comm["name"] if comm else "?",
            "CONNECTIONS": connections,
        })

    communities = []
    for c in sorted(graph["communities"], key=lambda x: -x["size"])[:10]:
        entry_names = [node_by_id[nid]["name"] for nid in graph["entrypoints"]
                       if node_by_id.get(nid, {}).get("community") == c["id"]]
        key_files = sorted({node_by_id[nid]["path"] for nid in c["nodes"] if nid in node_by_id})[:5]
        communities.append({
            "ID": c["id"], "NAME": c["name"], "NODE_COUNT": c["size"],
            "ENTRY_POINTS": ", ".join(entry_names) if entry_names else "none",
            "DESCRIPTION": c.get("description", ""),
            "KEY_FILES": [{"PATH": p} for p in key_files],
        })

    entry_points = [{"NAME": node_by_id[nid]["name"], "PATH": node_by_id[nid]["path"]}
                     for nid in graph["entrypoints"] if nid in node_by_id]

    # "surprising": cross-community edges between communities whose names share no
    # path segment -- a cheap heuristic, not a real analysis. Capped at 5.
    surprising = []
    seen_pairs = set()
    for e in graph["edges"]:
        s, t = node_by_id.get(e["source"]), node_by_id.get(e["target"])
        if not s or not t or s.get("community") == t.get("community"):
            continue
        cs, ct = comm_by_id.get(s["community"]), comm_by_id.get(t["community"])
        if not cs or not ct:
            continue
        a, b = cs["name"].split('/')[0], ct["name"].split('/')[0]
        if a == b:
            continue
        pair = tuple(sorted([s["id"], t["id"]]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        surprising.append({
            "SOURCE": s["name"], "TARGET": t["name"], "EDGE_TYPE": e["type"], "TAG": e["tag"],
            "REASON": f"Crosses from `{cs['name']}` to `{ct['name']}` with no shared path prefix.",
        })
        if len(surprising) >= 5:
            break

    questions = []
    if god_nodes:
        questions.append(f"What does `{god_nodes[0]['NAME']}` do, and what depends on it?")
    if len(communities) >= 2:
        questions.append(f"How is `{communities[0]['NAME']}` connected to `{communities[1]['NAME']}`?")
    if entry_points:
        questions.append(f"What does the codebase do starting from `{entry_points[0]['NAME']}`?")
    questions.append("Which modules have no incoming edges at all (dead code candidates)?")

    return {
        "PROJECT_NAME": builder.root.name,
        "DATE": graph["generated_at"],
        "TOTAL_NODES": graph["total_nodes"],
        "TOTAL_EDGES": graph["total_edges"],
        "TOTAL_COMMUNITIES": graph["total_communities"],
        "PROJECT_ROOT": graph["project_root"],
        "LANGUAGES": ", ".join(languages) if languages else "none detected",
        "ARCHITECTURE_PATTERN": _detect_architecture_pattern(builder),
        "ENTRYPOINTS": len(graph["entrypoints"]),
        "GOD_COUNT": len(graph["god_nodes"]),
        "STATS": [{"KEY": k, "VALUE": v} for k, v in sorted(graph["stats"].items())],
        "GOD_NODES": god_nodes,
        "COMMUNITIES": communities,
        "ENTRY_POINTS": entry_points,
        "SURPRISING": surprising,
        "QUESTIONS": [{"QUESTION": q} for q in questions],
    }


def write_report(builder, graph, path: Path):
    ctx = _build_report_context(builder, graph)
    template_path = Path(__file__).resolve().parent.parent / "templates" / "graph_report.md"
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError:
        template = _FALLBACK_TEMPLATE
    path.write_text(render_template(template, ctx), encoding="utf-8")


HTML_DEFAULT_RENDER_LIMIT = 700  # see the "scale" comment in write_html()


def write_html(builder, graph, path: Path):
    # graph.json itself can be large (tens of MB on a real project -- see the comment
    # above GraphBuilder's query commands), and embedding it whole here means this file
    # is at least as big. That's an acceptable one-time cost for a human opening a page
    # in their own browser (unlike asking an LLM to read the JSON), but a force-directed
    # layout of tens of thousands of nodes is not: the initial render defaults to the
    # top HTML_DEFAULT_RENDER_LIMIT nodes by degree, with a banner + control to raise
    # that. All node/edge data is still embedded and searchable/explainable, only the
    # initial simulation is capped.
    render_limit = min(HTML_DEFAULT_RENDER_LIMIT, len(graph["nodes"])) or 1
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>CodeGraph: {builder.root.name}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; overflow: hidden; }}
  body {{ display: flex; font: 13px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
          background: #0d1117; color: #c9d1d9; }}
  #graph {{ flex: 1; min-width: 0; }}
  aside {{ width: 380px; max-width: 42vw; background: #11161d; border-left: 1px solid #30363d;
           display: flex; flex-direction: column; }}
  #topbar {{ padding: 12px 14px; border-bottom: 1px solid #30363d; }}
  #topbar h1 {{ font-size: 15px; margin: 0 0 6px; color: #e6edf3; }}
  #topbar .stats {{ color: #8b949e; font-size: 12px; margin-bottom: 8px; }}
  #search {{ width: 100%; padding: 6px 8px; background: #0d1117; border: 1px solid #30363d;
             border-radius: 6px; color: #c9d1d9; font-size: 12px; }}
  #banner {{ font-size: 11px; color: #d29922; background: rgba(210,153,34,0.08);
             border: 1px solid rgba(210,153,34,0.3); border-radius: 6px; padding: 6px 8px;
             margin-top: 8px; display: none; }}
  #banner input {{ width: 60px; }}
  #scroll {{ flex: 1; overflow-y: auto; padding: 14px; }}
  #scroll h2 {{ font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
                color: #8b949e; margin: 14px 0 6px; }}
  #scroll h3 {{ font-size: 14px; margin: 0 0 4px; color: #e6edf3; word-break: break-word; }}
  .path {{ color: #8b949e; font-size: 11px; margin-bottom: 8px; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 99px; font-size: 10px;
            border: 1px solid #30363d; margin: 0 4px 4px 0; color: #8b949e; }}
  .kv {{ font-size: 11px; color: #8b949e; margin: 2px 0; word-break: break-word; }}
  .kv b {{ color: #c9d1d9; }}
  ul.edges {{ list-style: none; margin: 0; padding: 0; }}
  ul.edges li {{ padding: 4px 0; border-bottom: 1px solid #1c2128; font-size: 12px;
                 cursor: pointer; }}
  ul.edges li:hover {{ color: #58a6ff; }}
  .tag {{ color: #8b949e; font-size: 10px; }}
  #legend {{ padding: 10px 14px; border-top: 1px solid #30363d; display: flex;
             flex-wrap: wrap; gap: 6px; }}
  .chip {{ display: flex; align-items: center; gap: 5px; font-size: 11px; color: #8b949e;
           cursor: pointer; padding: 3px 8px; border: 1px solid #30363d; border-radius: 99px;
           user-select: none; }}
  .chip .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
  .chip.off {{ opacity: .35; text-decoration: line-through; }}
  .hint {{ color: #8b949e; font-size: 11px; margin-top: 10px; }}
  .node {{ cursor: pointer; }}
  .link {{ stroke: #58a6ff; stroke-opacity: 0.35; }}
  .faded {{ opacity: 0.08; }}
  @media (max-width: 800px) {{ body {{ flex-direction: column-reverse; }}
                                aside {{ width: 100%; max-width: 100%; max-height: 50vh; }} }}
</style>
</head>
<body>
<svg id="graph"></svg>
<aside>
  <div id="topbar">
    <h1>{builder.root.name}</h1>
    <div class="stats">{graph['total_nodes']} nodes - {graph['total_edges']} edges - {graph['total_communities']} communities</div>
    <input id="search" type="text" placeholder="Search by name...">
    <div id="banner"></div>
  </div>
  <div id="scroll"></div>
  <div id="legend"></div>
</aside>
<script>
// Dependency-free renderer -- no D3, no CDN, no build step. A small velocity-Verlet
// force simulation (O(n^2) repulsion is fine for the degree-capped node set), SVG
// drawn by hand, pan/zoom/drag/search/legend all vanilla. graph.html now opens
// with zero network access, matching this skill's local-first contract.
const DATA = {json.dumps(graph)};
const RENDER_LIMIT_DEFAULT = {render_limit};
const TYPE_COLORS = {{file:"#8b949e", function:"#58a6ff", class:"#d2a8ff", type:"#ff7b72",
                       entrypoint:"#3fb950", import:"#79c0ff", variable:"#ffa657", symbol:"#f2cc60"}};

const byId = new Map(DATA.nodes.map(n => [n.id, n]));
const commName = new Map(DATA.communities.map(c => [c.id, c.name]));
const hiddenTypes = new Set();
let renderLimit = RENDER_LIMIT_DEFAULT;
let searchTerm = "";
let selectedId = null;

const SVGNS = "http://www.w3.org/2000/svg";
const svg = document.getElementById("graph");
const gRoot = document.createElementNS(SVGNS, "g");
svg.appendChild(gRoot);
const gLinks = document.createElementNS(SVGNS, "g"); gRoot.appendChild(gLinks);
const gNodes = document.createElementNS(SVGNS, "g"); gRoot.appendChild(gNodes);

let view = {{ k: 1, x: 0, y: 0 }};
function applyView() {{ gRoot.setAttribute("transform", `translate(${{view.x}} ${{view.y}}) scale(${{view.k}})`); }}

let simNodes = [], simLinks = [], nodeEls = new Map(), linkEls = [], raf = null, alpha = 0;

function viewport() {{
  const w = window.innerWidth - document.querySelector("aside").offsetWidth;
  const h = window.innerHeight;
  svg.setAttribute("width", Math.max(w, 100));
  svg.setAttribute("height", h);
  return [Math.max(w, 100), h];
}}

function buildRender() {{
  const [W, H] = viewport();
  const ranked = [...DATA.nodes].sort((a, b) => (b.degree||0) - (a.degree||0));
  const capped = ranked.slice(0, renderLimit);
  const visible = new Set(capped.map(n => n.id));

  const banner = document.getElementById("banner");
  if (DATA.nodes.length > renderLimit) {{
    banner.style.display = "block";
    banner.innerHTML = `Showing top ${{renderLimit}} of ${{DATA.nodes.length}} nodes (by degree). `
      + `<input id="limitInput" type="number" min="10" value="${{renderLimit}}"> <button id="limitApply">Apply</button>`;
    document.getElementById("limitApply").onclick = () => {{
      const v = parseInt(document.getElementById("limitInput").value, 10);
      if (v > 0) {{ renderLimit = v; buildRender(); }}
    }};
  }} else {{
    banner.style.display = "none";
  }}

  // Deterministic seed positions on a spiral, so the same graph always starts
  // the same way (and re-running buildRender doesn't jump the layout around).
  simNodes = capped.map((n, i) => {{
    const ang = i * 2.399963;               // golden angle
    const rad = 12 * Math.sqrt(i);
    return {{ id: n.id, data: n, x: W/2 + rad*Math.cos(ang), y: H/2 + rad*Math.sin(ang),
             vx: 0, vy: 0, fx: null, fy: null, r: 3 + Math.sqrt(n.degree || 1) }};
  }});
  const nodeById = new Map(simNodes.map(n => [n.id, n]));
  simLinks = DATA.edges
    .filter(e => visible.has(e.source) && visible.has(e.target))
    .map(e => ({{ s: nodeById.get(e.source), t: nodeById.get(e.target), type: e.type }}))
    .filter(l => l.s && l.t);

  gLinks.textContent = ""; gNodes.textContent = ""; nodeEls.clear(); linkEls = [];
  for (const l of simLinks) {{
    const ln = document.createElementNS(SVGNS, "line");
    ln.setAttribute("class", "link");
    ln.setAttribute("stroke-width", l.type === "contains" ? 1 : 1.4);
    gLinks.appendChild(ln); l.el = ln; linkEls.push(l);
  }}
  for (const n of simNodes) {{
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("class", "node");
    c.setAttribute("r", n.r);
    c.setAttribute("fill", TYPE_COLORS[n.data.type] || "#8b949e");
    const title = document.createElementNS(SVGNS, "title");
    title.textContent = `${{n.data.name}} (${{n.data.type}})\n${{n.data.path}}`;
    c.appendChild(title);
    c.addEventListener("pointerdown", (ev) => startNodeDrag(ev, n));
    c.addEventListener("click", (ev) => {{ ev.stopPropagation(); selectNode(n.id); }});
    gNodes.appendChild(c); nodeEls.set(n.id, c);
  }}

  applyFilters();
  alpha = 1;
  if (!raf) tick();
}}

function tick() {{
  const [W, H] = [parseFloat(svg.getAttribute("width")), parseFloat(svg.getAttribute("height"))];
  const CHARGE = -220, LINK_DIST = 70, LINK_K = 0.04, CENTER_K = 0.02, DAMP = 0.82;
  // repulsion (naive O(n^2); the node set is degree-capped so this stays smooth)
  for (let i = 0; i < simNodes.length; i++) {{
    const a = simNodes[i];
    for (let j = i + 1; j < simNodes.length; j++) {{
      const b = simNodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx*dx + dy*dy || 0.01;
      if (d2 > 90000) continue;             // ignore far pairs -- keeps it fast
      const f = CHARGE / d2;
      const d = Math.sqrt(d2);
      const fx = f * dx / d, fy = f * dy / d;
      a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
    }}
  }}
  // link springs
  for (const l of simLinks) {{
    let dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
    const d = Math.sqrt(dx*dx + dy*dy) || 0.01;
    const f = (d - LINK_DIST) * LINK_K;
    const fx = f * dx / d, fy = f * dy / d;
    l.s.vx += fx; l.s.vy += fy; l.t.vx -= fx; l.t.vy -= fy;
  }}
  // centering + integrate
  for (const n of simNodes) {{
    n.vx += (W/2 - n.x) * CENTER_K * alpha;
    n.vy += (H/2 - n.y) * CENTER_K * alpha;
    if (n.fx !== null) {{ n.x = n.fx; n.y = n.fy; n.vx = 0; n.vy = 0; }}
    else {{
      n.vx *= DAMP; n.vy *= DAMP;
      n.x += n.vx * alpha; n.y += n.vy * alpha;
    }}
  }}
  for (const l of linkEls) {{
    l.el.setAttribute("x1", l.s.x); l.el.setAttribute("y1", l.s.y);
    l.el.setAttribute("x2", l.t.x); l.el.setAttribute("y2", l.t.y);
  }}
  for (const n of simNodes) {{
    const el = nodeEls.get(n.id);
    el.setAttribute("cx", n.x); el.setAttribute("cy", n.y);
  }}
  alpha *= 0.985;
  if (alpha > 0.02) {{ raf = requestAnimationFrame(tick); }} else {{ raf = null; }}
}}
function reheat() {{ alpha = Math.max(alpha, 0.4); if (!raf) tick(); }}

// ---------- node drag ----------
let dragNode = null, dragMoved = false;
function startNodeDrag(ev, n) {{
  ev.stopPropagation();
  dragNode = n; dragMoved = false;
  n.fx = n.x; n.fy = n.y;
  svg.setPointerCapture(ev.pointerId);
  reheat();
}}
svg.addEventListener("pointermove", (ev) => {{
  if (dragNode) {{
    dragMoved = true;
    dragNode.fx = (ev.offsetX - view.x) / view.k;
    dragNode.fy = (ev.offsetY - view.y) / view.k;
    reheat();
  }} else if (panning) {{
    view.x = panStart.vx + (ev.clientX - panStart.x);
    view.y = panStart.vy + (ev.clientY - panStart.y);
    applyView();
  }}
}});
svg.addEventListener("pointerup", (ev) => {{
  if (dragNode) {{ dragNode.fx = null; dragNode.fy = null; dragNode = null; }}
  panning = false;
  try {{ svg.releasePointerCapture(ev.pointerId); }} catch (e) {{}}
}});

// ---------- background pan + wheel zoom ----------
let panning = false, panStart = null;
svg.addEventListener("pointerdown", (ev) => {{
  if (dragNode) return;
  panning = true;
  panStart = {{ x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y }};
  svg.setPointerCapture(ev.pointerId);
}});
svg.addEventListener("wheel", (ev) => {{
  ev.preventDefault();
  const factor = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
  const nk = Math.min(4, Math.max(0.15, view.k * factor));
  const mx = ev.offsetX, my = ev.offsetY;
  view.x = mx - (mx - view.x) * (nk / view.k);
  view.y = my - (my - view.y) * (nk / view.k);
  view.k = nk;
  applyView();
}}, {{ passive: false }});
svg.addEventListener("click", () => {{ if (!dragMoved) showHome(); }});

// ---------- filters (type toggle + search fade + neighbour focus) ----------
function applyFilters() {{
  const focus = selectedId
    ? new Set([selectedId,
        ...DATA.edges.filter(e => e.source === selectedId).map(e => e.target),
        ...DATA.edges.filter(e => e.target === selectedId).map(e => e.source)])
    : null;
  for (const n of simNodes) {{
    const el = nodeEls.get(n.id);
    const hiddenByType = hiddenTypes.has(n.data.type);
    const fadedBySearch = searchTerm && !n.data.name.toLowerCase().includes(searchTerm);
    const fadedByFocus = focus && !focus.has(n.id);
    el.style.display = hiddenByType ? "none" : "";
    el.classList.toggle("faded", Boolean(fadedBySearch || fadedByFocus));
  }}
  for (const l of linkEls) {{
    const hidden = hiddenTypes.has(l.s.data.type) || hiddenTypes.has(l.t.data.type);
    l.el.style.display = hidden ? "none" : "";
  }}
}}

// ---------- side panel ----------
const scroll = document.getElementById("scroll");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]));

function showHome() {{
  selectedId = null;
  scroll.innerHTML = `<p class="hint">Click a node to inspect it. Drag a node to pin it,
    drag the background to pan, scroll to zoom. Use the legend below to hide node types,
    or search above to highlight by name.</p>`;
  applyFilters();
}}

function edgesTouching(id) {{
  return {{
    out: DATA.edges.filter(e => e.source === id),
    inc: DATA.edges.filter(e => e.target === id),
  }};
}}
function edgeLine(e, otherId, dir) {{
  const other = byId.get(otherId);
  const label = other ? esc(other.name) : otherId;
  const conf = e.type === "calls" ? ` conf=${{e.confidence}}` : "";
  return `<li data-id="${{esc(otherId)}}">${{dir}} ${{label}}
    <span class="tag">[${{e.type}}/${{e.tag}}${{conf}}]</span></li>`;
}}

function selectNode(id) {{
  const n = byId.get(id);
  if (!n) return;
  selectedId = id;
  const {{ inc, out }} = edgesTouching(id);
  const meta = n.metadata || {{}};
  const metaRows = Object.keys(meta).filter(k => meta[k] !== null && meta[k] !== "" &&
      !(Array.isArray(meta[k]) && meta[k].length === 0))
    .map(k => `<div class="kv"><b>${{esc(k)}}</b>: ${{esc(Array.isArray(meta[k]) ? meta[k].join(", ") : meta[k])}}</div>`)
    .join("");
  scroll.innerHTML = `
    <h3>${{esc(n.name)}}</h3>
    <div class="path">${{esc(n.path)}}${{n.line_start ? ":" + n.line_start : ""}}${{n.line_end && n.line_end !== n.line_start ? "-" + n.line_end : ""}}</div>
    <span class="badge" style="border-color:${{TYPE_COLORS[n.type]||'#30363d'}};color:${{TYPE_COLORS[n.type]||'#8b949e'}}">${{esc(n.type)}}</span>
    <span class="badge">community: ${{esc(commName.get(n.community) ?? "?")}}</span>
    <span class="badge">degree ${{n.degree||0}}</span>
    ${{metaRows ? `<h2>Metadata</h2>${{metaRows}}` : ""}}
    ${{out.length ? `<h2>Uses (${{out.length}})</h2><ul class="edges">${{out.map(e => edgeLine(e, e.target, "&#8594;")).join("")}}</ul>` : ""}}
    ${{inc.length ? `<h2>Used by (${{inc.length}})</h2><ul class="edges">${{inc.map(e => edgeLine(e, e.source, "&#8592;")).join("")}}</ul>` : ""}}
  `;
  scroll.querySelectorAll("li[data-id]").forEach(el => {{ el.onclick = () => selectNode(el.dataset.id); }});
  scroll.scrollTop = 0;
  applyFilters();
}}

document.getElementById("search").addEventListener("input", e => {{
  searchTerm = e.target.value.trim().toLowerCase();
  applyFilters();
}});

// ---------- legend ----------
const legend = document.getElementById("legend");
const typesPresent = [...new Set(DATA.nodes.map(n => n.type))];
typesPresent.forEach(t => {{
  const chip = document.createElement("div");
  chip.className = "chip";
  chip.innerHTML = `<span class="dot" style="background:${{TYPE_COLORS[t]||'#8b949e'}}"></span>${{esc(t)}}`;
  chip.onclick = () => {{
    if (hiddenTypes.has(t)) hiddenTypes.delete(t); else hiddenTypes.add(t);
    chip.classList.toggle("off");
    applyFilters();
  }};
  legend.appendChild(chip);
}});
const fitChip = document.createElement("div");
fitChip.className = "chip"; fitChip.textContent = "recenter";
fitChip.onclick = () => {{ view = {{ k: 1, x: 0, y: 0 }}; applyView(); reheat(); }};
legend.appendChild(fitChip);

window.addEventListener("resize", () => {{ viewport(); reheat(); }});

buildRender();
showHome();
</script>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")


# == Obsidian vault export ======================================================
_OBSIDIAN_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_OBSIDIAN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _obsidian_filenames(nodes):
    """First pass of the export: node id -> a filesystem- and Obsidian-safe, human
    readable .md filename, collision-disambiguated. Guards against Windows-specific
    restrictions (forbidden characters, trailing dots/spaces, reserved device names like
    CON/NUL) rather than just POSIX ones, since this vault is explicitly meant to be
    opened by the user directly, not just read by this script."""
    used = set()
    names = {}
    for n in nodes:
        base = _OBSIDIAN_UNSAFE.sub("_", n["name"]).strip(" .")
        if not base:
            base = "_"
        if base.upper() in _OBSIDIAN_RESERVED:
            base = f"_{base}"
        base = base[:120]  # keep well under Windows' 260-char path cap once nested in a vault
        candidate, i = base, 2
        while candidate.lower() in used:
            candidate = f"{base}_{i}"
            i += 1
        used.add(candidate.lower())
        names[n["id"]] = candidate
    return names


def write_obsidian(builder, graph, vault_dir: Path):
    """Export one Markdown note per graph node into vault_dir, with [[wikilinks]] to
    every connected node -- so pointing Obsidian at vault_dir (or copying/symlinking it
    into an existing vault) gives a native, zero-plugin graph view of the codebase,
    rather than needing a hand-built Canvas file. Two passes: _obsidian_filenames()
    resolves every node id to its filename first, so every wikilink target is known
    before any note body is written (a node's own file may reference a node discovered
    later in iteration order)."""
    vault_dir.mkdir(parents=True, exist_ok=True)
    nodes = graph["nodes"]
    idx = {n["id"]: n for n in nodes}
    filenames = _obsidian_filenames(nodes)

    # Regenerated fresh each export, same as GRAPH_REPORT.md/graph.html -- so a node
    # that was renamed or deleted since the last export doesn't leave a stale, orphaned
    # note behind. Only *.md files directly in vault_dir are touched (never recursively,
    # and never anything else) so Obsidian's own ".obsidian/" config folder -- created
    # the first time the user opens this directory as a vault -- is left alone.
    keep = {f"{fn}.md" for fn in filenames.values()}
    for existing in vault_dir.glob("*.md"):
        if existing.name not in keep:
            try:
                existing.unlink()
            except OSError:
                pass

    out_by_source = defaultdict(list)
    in_by_target = defaultdict(list)
    for e in graph["edges"]:
        out_by_source[e["source"]].append(e)
        in_by_target[e["target"]].append(e)

    community_name = {c["id"]: c["name"] for c in graph.get("communities", [])}

    def edge_note(e, other_id):
        other = idx.get(other_id)
        if not other:
            return None
        link = filenames.get(other_id, other["name"])
        conf = f", conf={e['confidence']}" if e["type"] == "calls" else ""
        return f"- [[{link}|{other['name']}]] — `{e['type']}/{e['tag']}{conf}`"

    written = 0
    for n in nodes:
        lines = [
            "---",
            f"type: {n['type']}",
            f"path: \"{n['path']}\"",
        ]
        if n.get("line_start"):
            lines.append(f"line: {n['line_start']}")
        comm = community_name.get(n.get("community"))
        if comm:
            lines.append(f"community: \"{comm}\"")
        lines.append(f"degree: {n.get('degree', 0)}")
        lines += [
            "---",
            "",
            f"# {n['name']}",
            "",
            f"**Type**: {n['type']}  ",
            f"**Location**: `{n['path']}`" + (f":{n['line_start']}" if n.get('line_start') else ""),
            "",
        ]

        outs = sorted(out_by_source.get(n["id"], []), key=lambda e: -e["confidence"])
        notes = [edge_note(e, e["target"]) for e in outs]
        notes = [x for x in notes if x]
        if notes:
            lines += ["## Uses", ""] + notes + [""]

        ins = sorted(in_by_target.get(n["id"], []), key=lambda e: -e["confidence"])
        notes = [edge_note(e, e["source"]) for e in ins]
        notes = [x for x in notes if x]
        if notes:
            lines += ["## Used by", ""] + notes + [""]

        (vault_dir / f"{filenames[n['id']]}.md").write_text("\n".join(lines), encoding="utf-8")
        written += 1

    return written


# == Watch / poll modes =========================================================
def watch_mode(builder: GraphBuilder):
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log("[!] watchdog not installed (pip install watchdog) -- using polling fallback.")
        return poll_mode(builder)

    class Handler(FileSystemEventHandler):
        def __init__(self, b):
            self.builder = b
            self.last_build = 0.0

        def on_any_event(self, event):
            if event.is_directory:
                return
            if not any(event.src_path.endswith(ext) for ext in EXT_MAP):
                return
            now = time.time()
            if now - self.last_build > 2:  # debounce
                log(f"[watch] change detected: {event.src_path}")
                self.builder.build(incremental=True)
                self.last_build = now

    observer = Observer()
    observer.schedule(Handler(builder), str(builder.root), recursive=True)
    observer.start()
    log(f"[watch] watching {builder.root} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def poll_mode(builder: GraphBuilder):
    log(f"[watch] polling {builder.root} every 5s (Ctrl+C to stop)")
    last_mtimes = {}
    while True:
        try:
            changed = False
            for p in builder.root.rglob('*'):
                if not p.is_file() or builder.should_skip(p):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                key = str(p)
                if key in last_mtimes and last_mtimes[key] != mtime:
                    changed = True
                last_mtimes[key] = mtime
            if changed:
                log("[watch] changes detected, updating...")
                builder.build(incremental=True)
            time.sleep(5)
        except KeyboardInterrupt:
            log("[watch] stopped.")
            break


# == CLI =========================================================================
def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="CodeGraph Builder -- token-free graph construction")
    parser.add_argument("path", nargs="?", default=".", help="Project root path")
    parser.add_argument("--watch", "-w", action="store_true", help="Continuous incremental watch mode")
    parser.add_argument("--update", "-u", action="store_true",
                         help="Incremental build: only re-parse files whose mtime changed")
    parser.add_argument("--report", "-r", action="store_true",
                         help="Regenerate GRAPH_REPORT.md/graph.html from the existing graph.json only")
    parser.add_argument("--export-obsidian", action="store_true",
                         help="Write .codegraph/obsidian_vault/ (one wikilinked Markdown "
                              "note per node) from the existing graph.json only -- open "
                              "that folder as an Obsidian vault for a native graph view")
    parser.add_argument("--sync-graphdb", action="store_true",
                         help="Export the existing graph.json into a local Ladybug (formerly "
                              "Kuzu) embedded graph database at .codegraph/graph_db/, queryable "
                              "with real Cypher via --cypher. Requires `pip install ladybug`; "
                              "parallel to graph.json, does not replace it")
    parser.add_argument("--cypher", metavar="QUERY",
                         help="Run a Cypher query against .codegraph/graph_db/ (built by "
                              "--sync-graphdb) and print the result. Schema: Symbol(id, name, "
                              "ntype, path, line_start, line_end, community, degree, meta) and "
                              "Edge(FROM Symbol TO Symbol, etype, tag, confidence, meta)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose diagnostics")
    parser.add_argument("--force-rebuild", action="store_true",
                         help="Bypass the shrink-guard that refuses to build when discover() "
                              "finds far fewer files than were previously tracked (a safety net "
                              "against a wrong path, a swallow-everything ignore pattern, or a "
                              "permissions issue silently collapsing the graph). Use this only "
                              "when the drop is real and expected, e.g. after deliberately "
                              "deleting most of the project.")
    parser.add_argument("--community-algo", choices=["auto", "directory", "leiden"], default="auto",
                         help="Community-detection algorithm used during a build. 'auto' "
                              "(default) uses real Leiden graph clustering over calls/inherits "
                              "edges when `python-igraph`+`leidenalg` are installed, else falls "
                              "back to the original directory-grouping heuristic. 'directory' "
                              "forces the heuristic regardless of what's installed. 'leiden' "
                              "forces Leiden and errors immediately if the packages are missing, "
                              "instead of silently falling back.")

    # Query commands: read-only, act on the existing graph.json, never rebuild anything.
    # These exist so a targeted question costs a few hundred tokens instead of however
    # many "read graph.json" costs once a project is big enough that the file itself
    # is megabytes -- see the comment above GraphBuilder.load_existing().
    parser.add_argument("--explain", metavar="SYMBOL",
                         help="Query: print one symbol's metadata + incoming/outgoing edges")
    parser.add_argument("--callers", metavar="SYMBOL",
                         help="Query: print everything that calls/subclasses a symbol")
    parser.add_argument("--find-path", nargs=2, metavar=("A", "B"),
                         help="Query: shortest connecting path between two symbols")
    parser.add_argument("--trace-entrypoints", metavar="SYMBOL",
                         help="Query: walk calls/inherits edges backward to the nearest "
                              "entrypoint(s) reaching SYMBOL, in one call instead of "
                              "repeated --callers round trips")
    parser.add_argument("--impact", metavar="SYMBOL",
                         help="Query: full transitive closure of everything that calls/"
                              "inherits from SYMBOL, directly or indirectly (blast radius "
                              "of a change), plus which of those are entrypoints")
    parser.add_argument("--snapshot", metavar="NAME",
                         help="Save the current .codegraph/graph.json as a named snapshot "
                              "(.codegraph/snapshots/NAME.json) to diff against later with "
                              "--diff NAME, regardless of how many builds happen in between")
    parser.add_argument("--diff", nargs="?", const="__PREV__", default=None, metavar="NAME",
                         help="Compare the current graph.json to an earlier one and report "
                              "added/removed/changed nodes and edges. With no NAME: compares "
                              "against the state immediately before the most recent build/"
                              "--update (rotated automatically on every save). With NAME: "
                              "compares against a snapshot taken earlier with --snapshot NAME")
    parser.add_argument("--json", action="store_true",
                         help="With a query flag: emit JSON instead of formatted text")
    args = parser.parse_args()

    VERBOSE = args.verbose
    root = Path(args.path).resolve()
    if not root.exists():
        log(f"[!] path does not exist: {root}")
        sys.exit(1)

    builder = GraphBuilder(root)
    builder.community_algo = args.community_algo

    if args.cypher:
        # Independent of the query-command block below: --cypher talks to
        # .codegraph/graph_db/ directly, not graph.json, so it doesn't need (and
        # shouldn't require) load_existing() to succeed first.
        builder.cmd_cypher(args.cypher, as_json=args.json)
    elif args.explain or args.callers or args.find_path or args.trace_entrypoints or args.impact:
        if not builder.load_existing():
            log("[!] no .codegraph/graph.json yet -- run a build first (no flags, or --update).")
            sys.exit(1)
        if args.explain:
            builder.cmd_explain(args.explain, as_json=args.json)
        if args.callers:
            builder.cmd_callers(args.callers, as_json=args.json)
        if args.find_path:
            builder.cmd_path(args.find_path[0], args.find_path[1], as_json=args.json)
        if args.trace_entrypoints:
            builder.cmd_trace_entrypoints(args.trace_entrypoints, as_json=args.json)
        if args.impact:
            builder.cmd_impact(args.impact, as_json=args.json)
    elif args.snapshot:
        builder.cmd_snapshot(args.snapshot)
    elif args.diff is not None:
        builder.cmd_diff(None if args.diff == "__PREV__" else args.diff, as_json=args.json)
    elif args.report:
        builder.report_only()
    elif args.export_obsidian:
        builder.export_obsidian()
    elif args.sync_graphdb:
        builder.sync_graphdb()
    elif args.watch:
        if not builder.build(incremental=True, force=args.force_rebuild):
            sys.exit(1)
        watch_mode(builder)
    else:
        if not builder.build(incremental=args.update, force=args.force_rebuild):
            sys.exit(1)


if __name__ == "__main__":
    main()
