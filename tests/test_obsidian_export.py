"""--export-obsidian (v3.4): one Markdown note per node, wikilinked, regenerated fresh
each run -- stale notes for renamed/deleted nodes must be cleaned up, but a real
.obsidian/ config directory (created by Obsidian itself, not by this script) must never
be touched."""
from conftest import write_files


def test_export_creates_one_note_per_node(tmp_path, run_cli, graph_json):
    write_files(tmp_path, {"src/a.py": "def foo(): pass\ndef bar(): pass\n"})
    run_cli(tmp_path)
    g = graph_json(tmp_path)
    proc = run_cli(tmp_path, "--export-obsidian")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    vault = tmp_path / ".codegraph" / "obsidian_vault"
    md_files = list(vault.glob("*.md"))
    assert len(md_files) == len(g["nodes"])


def test_export_without_graph_json_tells_user_to_build_first(tmp_path, run_cli):
    proc = run_cli(tmp_path, "--export-obsidian")
    assert "run a full build first" in proc.stdout


def test_reexport_cleans_up_stale_notes_but_keeps_dot_obsidian(tmp_path, run_cli):
    write_files(tmp_path, {"src/a.py": "def foo(): pass\n"})
    run_cli(tmp_path)
    run_cli(tmp_path, "--export-obsidian")

    vault = tmp_path / ".codegraph" / "obsidian_vault"
    # Simulate Obsidian itself having opened the vault and created its config dir.
    dot_obsidian = vault / ".obsidian"
    dot_obsidian.mkdir()
    (dot_obsidian / "workspace.json").write_text("{}", encoding="utf-8")

    # Rename the symbol -- the old note should disappear on re-export, the new one appear.
    (tmp_path / "src" / "a.py").write_text("def renamed(): pass\n", encoding="utf-8")
    run_cli(tmp_path, "--force-rebuild")
    run_cli(tmp_path, "--export-obsidian")

    md_files = {p.stem for p in vault.glob("*.md")}
    assert not any("foo" in name for name in md_files)
    assert dot_obsidian.exists()
    assert (dot_obsidian / "workspace.json").exists()
