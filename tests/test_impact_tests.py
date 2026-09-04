"""--impact splits its blast radius into prod code vs tests, and reports the test
files that transitively exercise the changed symbol ("what to re-run"). Test files are
recognised by directory segment (tests/, __tests__/, spec/, ...) or filename shape
(test_*.py, *_test.go, *.test.ts, *Test.java, ...) -- see is_test_path().
"""
import json

from conftest import write_files


def _project(root):
    write_files(root, {
        "src/core.py": (
            "def parse(s):\n"
            "    return s.strip()\n\n"
            "def load(path):\n"
            "    return parse(open(path).read())\n"
        ),
        "tests/test_core.py": (
            "from src.core import parse, load\n\n"
            "def test_parse():\n"
            "    assert parse(' x ') == 'x'\n\n"
            "def test_load():\n"
            "    assert load('f') is not None\n"
        ),
    })


def test_file_nodes_get_a_role(tmp_path, run_cli, graph_json):
    _project(tmp_path)
    run_cli(tmp_path)
    roles = {n["path"]: n["metadata"].get("role")
             for n in graph_json(tmp_path)["nodes"] if n["type"] == "file"}
    assert roles["src/core.py"] == "prod"
    assert roles["tests/test_core.py"] == "test"


def test_impact_reports_tests_to_run(tmp_path, run_cli):
    _project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--impact", "parse", "--json")
    data = json.loads(proc.stdout)

    assert data["tests_to_run"] == ["tests/test_core.py"]
    assert data["impacted_prod_count"] == 1                       # load()
    assert data["impacted_count"] == 3                            # load + 2 tests

    prod = [r for r in data["impacted"] if r["role"] != "test"]
    assert [r["name"] for r in prod] == ["load"]
    # test_load reaches parse only transitively (test_load -> load -> parse)
    assert any(r["name"] == "test_load" and r["depth"] == 2 for r in data["impacted"])


def test_impact_text_output_has_the_split(tmp_path, run_cli):
    _project(tmp_path)
    run_cli(tmp_path)
    proc = run_cli(tmp_path, "--impact", "parse")
    assert "in prod code" in proc.stdout
    assert "test files to re-run" in proc.stdout
    assert "tests/test_core.py" in proc.stdout


def test_impact_no_tests_when_none_touch_the_symbol(tmp_path, run_cli):
    write_files(tmp_path, {
        "src/a.py": "def helper():\n    return 1\n\ndef useit():\n    return helper()\n",
        "tests/test_a.py": "def test_nothing():\n    assert True\n",
    })
    run_cli(tmp_path)
    data = json.loads(run_cli(tmp_path, "--impact", "helper", "--json").stdout)
    assert data["tests_to_run"] == []
    assert data["impacted_prod_count"] == 1
