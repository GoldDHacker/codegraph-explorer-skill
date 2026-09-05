"""`--doctor` and the tree-sitter grammar loader.

The loader must never let a bad grammar package take the whole script down, and it must
make an installed-but-incompatible grammar *visible* (it used to fall back to regex
silently, which reads as "less precise than SKILL.md claims" with no explanation).
"""
import subprocess
import sys

from conftest import SCRIPT, write_files


def test_import_never_crashes_and_exposes_the_load_maps(cb):
    # importing the module is enough of a check that the grammar loop swallowed
    # whatever the environment threw at it; these two names are the contract --doctor
    # and the build-time warning rely on.
    assert isinstance(cb.TREE_SITTER_LANGS, dict)
    assert isinstance(cb.TREE_SITTER_LOAD_ERRORS, dict)
    # a language can be loaded xor broken, never both
    assert not (set(cb.TREE_SITTER_LANGS) & set(cb.TREE_SITTER_LOAD_ERRORS))


def test_doctor_runs_touches_nothing_and_reports_status(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--doctor"],
        capture_output=True, text=True, cwd=tmp_path, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "tree-sitter" in proc.stdout.lower()
    # --doctor must not build anything
    assert not (tmp_path / ".codegraph").exists()


def test_doctor_flags_incompatible_grammars_with_a_fix(cb):
    lines = "\n".join(cb.tree_sitter_status())
    if cb.TREE_SITTER_LOAD_ERRORS:
        assert "NOT loadable" in lines
        assert "pip install" in lines
        for lang in cb.TREE_SITTER_LOAD_ERRORS:
            assert lang in lines
    else:
        # nothing broken in this environment -- the status should still name the engine
        assert "tree-sitter core" in lines


def test_build_warns_when_a_grammar_is_installed_but_broken(tmp_path, run_cli, cb):
    if not cb.TREE_SITTER_LOAD_ERRORS:
        import pytest
        pytest.skip("no ABI-incompatible grammar in this environment to trigger the warning")
    write_files(tmp_path, {"a.py": "def a(): pass\n"})
    proc = run_cli(tmp_path)
    assert "not loadable" in proc.stdout
    assert "--doctor" in proc.stdout
