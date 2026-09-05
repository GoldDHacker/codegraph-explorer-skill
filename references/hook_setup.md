# Auto-build hook setup (v4.7)

Rule 1 of the [Auto-Build Protocol](auto_build_protocol.md) says the graph is built or
refreshed *before* Claude reasons about the code. That only works if Claude remembers to
do it. `scripts/codegraph_session_hook.py` makes it happen on its own: wire it once and
`.codegraph/graph.json` is simply *there* at the start of every session, built or
refreshed in a detached background process that never blocks the session.

The hook is stdlib-only Python 3.8+. It only ever shells out to `codegraph_builder.py`
(found next to it, on `$CODEGRAPH_BUILDER`, or on `PATH`) — the builder does the work.

## What it does, each time it runs

| State of `.codegraph/graph.json` | Action | Blocks the session? |
|---|---|---|
| absent (and the directory has code) | spawn a full build in the background | no — returns at once |
| older than `CODEGRAPH_AUTOBUILD_MAX_AGE` (default 1800 s) | spawn `--update` in the background | no |
| newer than that | print `graph ready (N nodes, M edges)` | no |
| a build is already running (live `.codegraph/.autobuild.lock`) | say so, do nothing | no |
| the directory has no code and no graph | stay silent | no |

It prints **at most one line** and **always exits 0** — a hook that fails a session start
is worse than no hook.

## Claude Code — `SessionStart` hook

Add this to `~/.claude/settings.json` (or a project `.claude/settings.json`). Point the
`command` at wherever the skill's `scripts/` folder lives on your machine:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/skills/codegraph/scripts/codegraph_session_hook.py\""
          }
        ]
      }
    ]
  }
}
```

- **If the skill is installed as a plugin**, the path is usually
  `$CLAUDE_PLUGIN_ROOT/scripts/codegraph_session_hook.py` — Claude Code expands
  `$CLAUDE_PLUGIN_ROOT` for plugin-provided hooks.
- **On Windows**, use `python` instead of `python3` and a full path:
  `python "%USERPROFILE%\.claude\skills\codegraph\scripts\codegraph_session_hook.py"`.
- Claude Code passes the session JSON (with `cwd`) on stdin; the hook reads it, so it
  builds the graph for whatever directory the session opened in.

`SessionStart` fires on a new session and on `--resume` / `--continue`, so a long-lived
project keeps a fresh graph without you thinking about it.

## Git hook (works with any editor or agent)

Drop a one-liner in `.git/hooks/post-checkout` and `.git/hooks/post-merge` (make them
executable):

```sh
#!/bin/sh
python3 /path/to/codegraph_session_hook.py "$(git rev-parse --show-toplevel)" >/dev/null 2>&1 || true
```

Git passes ref hashes as arguments; the hook ignores anything that isn't an existing
directory and falls back to the repo root.

## Manual

```bash
python3 scripts/codegraph_session_hook.py            # uses the current directory
python3 scripts/codegraph_session_hook.py /path/to/project
```

## Knobs (environment variables)

| Variable | Effect |
|---|---|
| `CODEGRAPH_AUTOBUILD=0` | disable the hook entirely (`false` / `no` / `off` also work) |
| `CODEGRAPH_AUTOBUILD_MAX_AGE=<seconds>` | how old a graph may be before a background `--update` (default `1800`) |
| `CODEGRAPH_BUILDER=<path>` | explicit path to `codegraph_builder.py` (otherwise: sibling file, then `PATH`) |
| `CLAUDE_PROJECT_DIR` | Claude Code sets this; the hook uses it when no path argument and no stdin `cwd` |

## Files it touches

Everything stays under `.codegraph/` (already in the skill's `.gitignore`):

- `.codegraph/.autobuild.lock` — held while a background build runs, released by the
  build worker when it finishes; ignored if older than one hour (a crashed build).
- `.codegraph/.autobuild.log` — stdout/stderr of the last background build, for
  debugging a build that didn't produce a graph.

## When *not* to use it

- A repo you only ever touch for a one-line typo fix — the first build has a cost.
- CI / throwaway containers — wire `--update` into your pipeline explicitly instead.
- If your `codegraph_builder.py` build takes minutes on a huge monorepo: the hook still
  won't block you (it's detached), but the first session's graph won't be ready
  immediately. Consider a nightly `--update` cron so the incremental cache is always warm.
