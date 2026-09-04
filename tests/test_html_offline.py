"""graph.html must be fully self-contained -- no D3, no CDN, no external <script src>.

This is a frozen regression guard: between v3.4 and v4.4 the renderer silently regressed
to loading D3 from https://d3js.org/d3.v7.min.js, which blanks the page the moment it is
opened without connectivity -- directly contradicting this skill's local-first contract
(and the SKILL.md line that still called the file "self-contained"). The dependency-free
velocity-Verlet renderer was restored after v4.4; this test keeps it that way.
"""
import re

from conftest import write_files


def _tiny_project(root):
    write_files(root, {
        "app.py": (
            "def helper():\n"
            "    return 1\n\n"
            "def main():\n"
            "    return helper()\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        ),
    })


def test_graph_html_has_no_external_script_or_cdn(tmp_path, run_cli):
    _tiny_project(tmp_path)
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    html = (tmp_path / ".codegraph" / "graph.html").read_text(encoding="utf-8")

    assert "d3js.org" not in html
    # no <script src="..."> and no <link href="..."> -- every asset must be inline
    assert not re.search(r"<script[^>]+\bsrc\s*=", html), "graph.html loads an external script"
    assert not re.search(r"<link[^>]+\bhref\s*=", html), "graph.html loads an external stylesheet"
    for host in ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "cdn.tailwindcss.com"):
        assert host not in html
    # the D3 globals the old renderer used must be gone
    for token in ("d3.forceSimulation", "d3.select(", "d3.zoom(", "d3.drag("):
        assert token not in html, f"graph.html still references {token}"


def test_graph_html_embeds_the_dependency_free_renderer(tmp_path, run_cli):
    _tiny_project(tmp_path)
    run_cli(tmp_path)
    html = (tmp_path / ".codegraph" / "graph.html").read_text(encoding="utf-8")

    # markers of the hand-rolled renderer that replaced D3
    assert "Dependency-free renderer" in html
    assert "requestAnimationFrame" in html
    assert "createElementNS" in html
