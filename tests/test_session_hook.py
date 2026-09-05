"""``codegraph_session_hook.py`` -- the SessionStart auto-build wrapper.

The hook's job is to be invisible: build or refresh ``.codegraph/graph.json`` in a
detached background process, print at most one line, and never fail the session. These
tests drive it as a real subprocess (the way a hook runner does) and point it at a
*fake* builder so nothing slow or environment-dependent runs.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "codegraph_session_hook.py"

FAKE_BUILDER = '''\
import json, os, sys, time
root = sys.argv[-1]
d = os.path.join(root, ".codegraph")
os.makedirs(d, exist_ok=True)
open(os.path.join(d, ".fake_ran"), "a").write(("update" if "--update" in sys.argv else "build") + "\\n")
json.dump({"nodes": [1, 2, 3], "edges": [1, 2]}, open(os.path.join(d, "graph.json"), "w"))
'''


@pytest.fixture
def fake_builder(tmp_path_factory):
    # Kept outside every project `tmp_path` so it never makes a dir "look like code".
    p = tmp_path_factory.mktemp("fakebuilder") / "fake_codegraph_builder.py"
    p.write_text(FAKE_BUILDER, encoding="utf-8")
    return p


def run_hook(root, *, env_extra=None, argv_extra=(), timeout=30):
    import os
    env = dict(os.environ)
    env.pop("CODEGRAPH_AUTOBUILD", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK), *argv_extra, str(root)],
        capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL,
    )


def wait_for(path, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if Path(path).exists():
            return True
        time.sleep(0.1)
    return False


def wait_for_gone(path, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if not Path(path).exists():
            return True
        time.sleep(0.1)
    return False


def test_opt_out_is_silent(tmp_path, fake_builder):
    (tmp_path / "a.py").write_text("def a(): pass\n")
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_AUTOBUILD": "0", "CODEGRAPH_BUILDER": str(fake_builder)})
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (tmp_path / ".codegraph").exists()


def test_non_code_directory_is_silent(tmp_path, fake_builder):
    (tmp_path / "notes.txt").write_text("hello\n")
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_BUILDER": str(fake_builder)})
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_a_bad_builder_env_falls_back_to_the_sibling_script(tmp_path):
    # $CODEGRAPH_BUILDER is a hint, not a hard requirement: a stale/wrong value must
    # not disable the hook -- it falls back to scripts/codegraph_builder.py.
    (tmp_path / ".codegraph").mkdir()
    (tmp_path / ".codegraph" / "graph.json").write_text(json.dumps({"nodes": [1], "edges": []}))
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_BUILDER": str(tmp_path / "nope.py")})
    assert proc.returncode == 0
    assert "graph ready" in proc.stdout


def test_missing_graph_spawns_a_background_build(tmp_path, fake_builder):
    (tmp_path / "pkg" / "mod.py").parent.mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("class C: pass\n")
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_BUILDER": str(fake_builder)})
    assert proc.returncode == 0
    assert "background" in proc.stdout
    assert wait_for(tmp_path / ".codegraph" / "graph.json")
    assert (tmp_path / ".codegraph" / ".fake_ran").read_text().strip() == "build"
    # the lock the hook took is released by the worker once the build finishes
    assert wait_for_gone(tmp_path / ".codegraph" / ".autobuild.lock")


def test_fresh_graph_prints_counts_and_spawns_nothing(tmp_path, fake_builder):
    d = tmp_path / ".codegraph"
    d.mkdir()
    (d / "graph.json").write_text(json.dumps({"nodes": list(range(10)), "edges": list(range(7))}))
    (tmp_path / "a.py").write_text("def a(): pass\n")
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_BUILDER": str(fake_builder)})
    assert proc.returncode == 0
    assert "graph ready (10 nodes, 7 edges)" in proc.stdout
    assert "background" not in proc.stdout
    assert not (d / ".fake_ran").exists()


def test_stale_graph_refreshes_in_background(tmp_path, fake_builder):
    d = tmp_path / ".codegraph"
    d.mkdir()
    gj = d / "graph.json"
    gj.write_text(json.dumps({"nodes": [1], "edges": []}))
    old = time.time() - 10_000
    import os
    os.utime(gj, (old, old))
    (tmp_path / "a.py").write_text("def a(): pass\n")
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_BUILDER": str(fake_builder)})
    assert proc.returncode == 0
    assert "refreshing in background" in proc.stdout
    assert wait_for(d / ".fake_ran")
    assert "update" in (d / ".fake_ran").read_text()


def test_a_live_lock_blocks_a_second_spawn(tmp_path, fake_builder):
    d = tmp_path / ".codegraph"
    d.mkdir()
    (d / ".autobuild.lock").write_text("999999 " + str(int(time.time())))
    (tmp_path / "a.py").write_text("def a(): pass\n")
    proc = run_hook(tmp_path, env_extra={"CODEGRAPH_BUILDER": str(fake_builder)})
    assert proc.returncode == 0
    assert "already running" in proc.stdout
    assert not (d / ".fake_ran").exists()


def test_worker_runs_the_builder_and_drops_the_lock(tmp_path, fake_builder):
    d = tmp_path / ".codegraph"
    d.mkdir()
    (d / ".autobuild.lock").write_text("1 1")
    proc = subprocess.run(
        [sys.executable, str(HOOK), "--_worker", str(tmp_path), "build"],
        capture_output=True, text=True, timeout=30,
        env={**_clean_env(), "CODEGRAPH_BUILDER": str(fake_builder)},
    )
    assert proc.returncode == 0
    assert (d / "graph.json").exists()
    assert not (d / ".autobuild.lock").exists()


def _clean_env():
    import os
    e = dict(os.environ)
    e.pop("CODEGRAPH_AUTOBUILD", None)
    return e
