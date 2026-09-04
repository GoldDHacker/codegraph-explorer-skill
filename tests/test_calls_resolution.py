"""Regression tests for the v4.2 Phase 4 calls-resolution upgrade: exact same-file
resolution (any language) and filesystem-verified import resolution (JS/TS relative
imports, Python dotted module paths) -- both produce a `calls` edge tagged "RESOLVED"
with a high, but not maximal, confidence, instead of the old project-wide
candidate-count heuristic tagged "INFERRED". The old heuristic must still be exactly
what a genuinely unresolvable ambiguous call falls back to -- these tests cover both
sides so neither the new tiers nor the old fallback silently regress."""
from conftest import write_files


def _calls_edges(graph):
    id_to_node = {n["id"]: n for n in graph["nodes"]}
    out = []
    for e in graph["edges"]:
        if e["type"] != "calls":
            continue
        out.append((id_to_node[e["source"]], id_to_node[e["target"]], e))
    return out


def _find(graph, src_name, tgt_name):
    matches = [(s, t, e) for s, t, e in _calls_edges(graph) if s["name"] == src_name and t["name"] == tgt_name]
    assert matches, f"no calls edge {src_name} -> {tgt_name} found among {[(s['name'], t['name']) for s, t, e in _calls_edges(graph)]}"
    return matches[0]


class TestSameFileResolution:
    def test_ambiguous_name_resolved_to_local_definition(self, tmp_path, run_cli, graph_json):
        """Two files each define `helper`; only one is a candidate in the SAME file as
        the caller -- that must resolve exactly, tagged RESOLVED, not fall into the old
        n=2 'plausible, verify' heuristic tier."""
        write_files(tmp_path, {
            "a.py": "def helper():\n    return 1\n\ndef run():\n    return helper()\n",
            "b.py": "def helper():\n    return 2\n",
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        src, tgt, e = _find(g, "run", "helper")
        assert tgt["path"] == "a.py"
        assert e["tag"] == "RESOLVED"
        assert e["metadata"]["resolved_by"] == "same_file"
        assert e["confidence"] >= 0.9
        # the OTHER helper (b.py) must not also get an edge from run() -- same-file
        # resolution should narrow to exactly the local one, not fan out to both.
        others = [(s, t, ed) for s, t, ed in _calls_edges(g) if s["name"] == "run" and t["name"] == "helper"]
        assert len(others) == 1


class TestPythonImportResolution:
    def test_dotted_from_import_resolves_uniquely(self, tmp_path, run_cli, graph_json):
        write_files(tmp_path, {
            "main.py": "from services.userservice import getuser\n\ndef run():\n    return getuser()\n",
            "services/userservice.py": "def getuser():\n    return 'user'\n",
            "services/otherservice.py": "def getuser():\n    return 'other'\n",
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        src, tgt, e = _find(g, "run", "getuser")
        assert tgt["path"] == "services/userservice.py"
        assert e["tag"] == "RESOLVED"
        assert e["metadata"]["resolved_by"] == "import"

    def test_plain_import_module_dotted_path_resolves(self, tmp_path, run_cli, graph_json):
        write_files(tmp_path, {
            "main.py": "import services.userservice\n\ndef run():\n    return doit()\n",
            "services/userservice.py": "def doit():\n    return 1\n",
            "other/userservice2.py": "def doit():\n    return 2\n",
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        src, tgt, e = _find(g, "run", "doit")
        assert tgt["path"] == "services/userservice.py"
        assert e["tag"] == "RESOLVED"


class TestJsTsImportResolution:
    def test_relative_import_resolves_uniquely(self, tmp_path, run_cli, graph_json):
        write_files(tmp_path, {
            "src/main.ts": (
                "import { getUser } from \"./services/userService\";\n\n"
                "function run() {\n  return getUser();\n}\n"
            ),
            "src/services/userService.ts": "export function getUser() {\n  return 'user';\n}\n",
            "src/services/otherService.ts": "export function getUser() {\n  return 'other';\n}\n",
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        src, tgt, e = _find(g, "run", "getUser")
        assert tgt["path"] == "src/services/userService.ts"
        assert e["tag"] == "RESOLVED"
        assert e["metadata"]["resolved_by"] == "import"

    def test_relative_import_resolves_across_extension(self, tmp_path, run_cli, graph_json):
        """The import string has no extension (`./services/userService`) -- resolution
        must try the real extension (.ts here) rather than only literal-matching."""
        write_files(tmp_path, {
            "src/main.js": (
                "const { getUser } = require(\"./services/userService\");\n\n"
                "function run() {\n  return getUser();\n}\n"
            ),
            "src/services/userService.ts": "export function getUser() {\n  return 'user';\n}\n",
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        src, tgt, e = _find(g, "run", "getUser")
        assert tgt["path"] == "src/services/userService.ts"
        assert e["tag"] == "RESOLVED"

    def test_bare_package_import_not_resolved(self, tmp_path, run_cli, graph_json):
        """A bare package specifier ("lodash", not "./lodash") must never be treated as
        a resolvable filesystem path -- this project has no node_modules to resolve it
        against, and guessing would be exactly the kind of false precision this tier is
        designed to avoid."""
        write_files(tmp_path, {
            "src/main.js": (
                "const debounce = require(\"lodash\");\n\n"
                "function run() {\n  return helper();\n}\n\n"
                "function helper() {\n  return 1;\n}\n"
            ),
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        # helper is same-file unique -> RESOLVED via same_file, not import; this just
        # confirms the bare "lodash" import didn't crash resolution or produce a bogus
        # "import"-tagged resolution for an unrelated call.
        src, tgt, e = _find(g, "run", "helper")
        assert e["metadata"]["resolved_by"] == "same_file"


class TestFallbackUnchanged:
    def test_unresolvable_ambiguous_call_still_uses_old_heuristic(self, tmp_path, run_cli, graph_json):
        """No same-file candidate, no resolvable import pointing at either candidate --
        must fall back to the pre-v4.2 project-wide candidate-count tier, tagged
        INFERRED, not RESOLVED."""
        write_files(tmp_path, {
            "main.py": "def run():\n    return doit()\n",
            "a/mod.py": "def doit():\n    return 1\n",
            "b/mod.py": "def doit():\n    return 2\n",
        })
        run_cli(tmp_path)
        g = graph_json(tmp_path)
        matches = [(s, t, e) for s, t, e in _calls_edges(g) if s["name"] == "run" and t["name"] == "doit"]
        assert len(matches) == 2
        for s, t, e in matches:
            assert e["tag"] == "INFERRED"
            assert e["confidence"] == 0.55  # n=2 tier, unchanged from pre-v4.2
