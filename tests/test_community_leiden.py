"""Regression tests for the v4.4 Phase 5 Leiden community-detection option
(`--community-algo`). Skipped automatically wherever `python-igraph`/`leidenalg` aren't
installed -- same optional-dependency contract as the tree-sitter/ladybug tests
elsewhere in this suite.

These tests deliberately pin --community-algo explicitly (rather than relying on
'auto', which depends on what happens to be installed in whatever environment runs the
suite) so behavior here is the same whether or not this dev environment happens to have
the packages -- 'auto' itself is covered once, by test_auto_prefers_leiden_when_available."""
import json

import pytest

from conftest import write_files


def _import_cb(cb):
    return cb.LEIDEN_AVAILABLE


class TestLeidenClustering:
    def _require(self, cb):
        if not cb.LEIDEN_AVAILABLE:
            pytest.skip("python-igraph/leidenalg not installed")

    def _cross_directory_fixture(self, tmp_path):
        # handlers/*.py each call into services/*.py, which (for the user path) calls
        # into utils/db.py -- a real call chain that crosses three directories, the
        # exact shape a directory-only heuristic can never group together, but a real
        # clustering algorithm over calls edges should.
        write_files(tmp_path, {
            "handlers/user_handler.py": (
                "from services.user_service import get_user, create_user\n\n"
                "def handle_get(req):\n    return get_user(req)\n\n"
                "def handle_create(req):\n    return create_user(req)\n"
            ),
            "services/user_service.py": (
                "from utils.db import query\n\n"
                "def get_user(req):\n    return query('select')\n\n"
                "def create_user(req):\n    return query('insert')\n"
            ),
            "utils/db.py": "def query(sql):\n    return sql\n",
            "handlers/order_handler.py": (
                "from services.order_service import get_order\n\n"
                "def handle_order(req):\n    return get_order(req)\n"
            ),
            "services/order_service.py": "def get_order(req):\n    return 'order'\n",
        })

    def test_leiden_groups_across_directories(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        self._cross_directory_fixture(tmp_path)
        proc = run_cli(tmp_path, "--community-algo", "leiden")
        assert proc.returncode == 0, proc.stderr
        g = graph_json(tmp_path)
        id_to_name = {n["id"]: n["name"] for n in g["nodes"]}

        def community_of(name):
            for c in g["communities"]:
                names = {id_to_name[nid] for nid in c["nodes"]}
                if name in names:
                    return c
            return None

        # get_user and handle_get are in different directories (services/ vs
        # handlers/) but directly connected by a calls edge -- Leiden must put them in
        # the same community, which the directory heuristic structurally cannot do.
        c1 = community_of("get_user")
        c2 = community_of("handle_get")
        assert c1 is not None and c2 is not None
        assert c1["id"] == c2["id"]

    def test_directory_algo_forced_regardless_of_leiden_availability(self, tmp_path, run_cli, graph_json, cb):
        """--community-algo directory must produce the exact pre-v4.3 grouping (one
        community per directory) even when leidenalg IS installed -- confirms the
        override actually overrides, rather than 'auto' logic leaking through."""
        self._cross_directory_fixture(tmp_path)
        proc = run_cli(tmp_path, "--community-algo", "directory")
        assert proc.returncode == 0, proc.stderr
        g = graph_json(tmp_path)
        names = sorted(c["name"] for c in g["communities"])
        assert names == sorted(["handlers", "services", "utils"])

    def test_leiden_flag_without_package_errors_clearly(self, tmp_path, run_cli, cb):
        """The inverse case: --community-algo leiden must fail loudly, not silently
        fall back, when the packages genuinely aren't installed. Can only be exercised
        in an environment that doesn't have them -- skipped otherwise, since we can't
        uninstall a real dependency out from under the rest of the suite."""
        if cb.LEIDEN_AVAILABLE:
            pytest.skip("leidenalg/python-igraph are installed in this environment -- "
                        "can't exercise the not-installed error path here")
        write_files(tmp_path, {"a.py": "def f():\n    return 1\n"})
        proc = run_cli(tmp_path, "--community-algo", "leiden")
        assert proc.returncode != 0
        assert "leiden" in (proc.stdout + proc.stderr).lower()

    def test_auto_prefers_leiden_when_available(self, tmp_path, run_cli, graph_json, cb):
        self._require(cb)
        self._cross_directory_fixture(tmp_path)
        proc = run_cli(tmp_path)  # no --community-algo flag -> default 'auto'
        assert proc.returncode == 0, proc.stderr
        g = graph_json(tmp_path)
        names = sorted(c["name"] for c in g["communities"])
        # If auto fell back to directory grouping, this would be exactly
        # ["handlers", "services", "utils"] -- assert it is NOT that, i.e. Leiden
        # actually ran.
        assert names != sorted(["handlers", "services", "utils"])

    def test_sparse_graph_falls_back_to_directory(self, tmp_path, run_cli, graph_json, cb):
        """Fewer than 5 calls/inherits edges total -- even with --community-algo
        leiden explicitly requested, the script should fall back to directory grouping
        rather than cluster noise (see the < 5 guard in _detect_communities_leiden)."""
        self._require(cb)
        write_files(tmp_path, {
            "a.py": "def f():\n    return g()\n\ndef g():\n    return 1\n",
        })
        proc = run_cli(tmp_path, "--community-algo", "leiden")
        assert proc.returncode == 0, proc.stderr
        g = graph_json(tmp_path)
        assert len(g["communities"]) == 1
        assert g["communities"][0]["name"] == "."  # top-level dir, pre-v4.3 directory-heuristic naming
