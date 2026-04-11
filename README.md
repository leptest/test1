# projtool

A tiny stdlib-only Python CLI for wrangling a folder full of personal
projects. Designed for the case where you have dozens of repos in varying
states of decay and want to inventory, categorize, health-check, and tidy
them up.

## Requirements

- Python 3.8+
- `git` on your PATH (optional, for git metadata)
- Whatever toolchains your projects use (`npm`, `cargo`, `go`, ...) — only
  needed when running `health` / `deps`

No `pip install`. No venv. No dependencies.

## Quick start — interactive TUI

Just run it with no arguments:

```sh
python projtool.py
```

That drops you into a full-screen interactive TUI (Unicode boxes, color,
tables). On the first run it detects that you haven't configured any roots
yet and walks you through adding one, then brings you to a menu where you
can scan, browse, run health checks, check deps, tidy, tag, and open the
HTML dashboard. Everything destructive still stays dry-run until you
explicitly opt in.

```
╭─ status ──────────────────────────────────────────────────────╮
│ roots: /home/you/projects                                     │
│ inventory: 64 projects  (game 12 · website 18 · service 8 …)  │
│ stacks: node 38 · python 9 · cpp 3 · rust 2 · static-web 6    │
│ health: 31 ok · 12 fail · 21 skipped  (16/64 projects checked)│
╰───────────────────────────────────────────────────────────────╯

  1) Configure project roots
  2) Scan projects (read-only)
  3) Browse inventory (table view)
  4) Render inventory.md + dashboard.html
  5) Run health checks (install/build/test/lint)
  6) Check outdated dependencies
  7) Flag archival candidates
  8) Tag a project
  9) Open HTML dashboard in browser
  q) Quit
```

The browse view paginates 20 projects at a time, supports
category filtering (`f`), free-text search (`/`), sort by last commit,
and per-row detail cards.

## Quick start — direct subcommands

Everything the TUI does is also available non-interactively:

```sh
python projtool.py scan              # read-only inventory
python projtool.py report            # inventory.md + dashboard.html
python projtool.py health [--run]    # dry-run or actually execute
python projtool.py deps              # outdated deps
python projtool.py tidy [--apply]    # archival candidates
python projtool.py tag <name> <tags...>
python projtool.py tui               # same as no args
```

On Windows use `python projtool.py ...` or the `bin\projtool.cmd` wrapper.
On Linux/macOS you can also use `bin/projtool ...`.

On Windows 10+, the TUI enables ANSI escape processing automatically
(via `os.system("")`). If you're on an ancient console, colors may show
as raw escape codes — use the classic subcommands instead.

## Subcommands

| Command | What it does |
|---|---|
| `tui` | Interactive full-screen menu (default when run with no args). Walks you through setup on first run and exposes every other command. |
| `scan` | Walks each configured root, detects stack/category/git info, writes `out/inventory.json`. Pure read-only. |
| `report` | Renders `inventory.json` into `inventory.md` and a single-file `dashboard.html` (sortable, filterable, no external assets). |
| `health [name] [--run]` | For each project, runs install/build/test/lint for its detected stack. Prints the plan without `--run`; actually executes with `--run`. Captures output to `out/health/<name>.log`. |
| `deps [name] [--apply]` | Summarises outdated dependencies via `npm outdated --json` and `pip list --outdated --format=json`. `--apply` is guarded and requires a clean git tree. |
| `tidy [--apply]` | Flags projects that are all of: stale (> `archive_after_days`), broken/untested, tiny or missing a README. `--apply` moves them to a sibling `_archive/` folder. |
| `tag <name> <tags...>` | Adds manual tags to a project in `projtool.config.json` → `overrides`. These persist across `scan` runs. |

## Config

`projtool.config.json` is the only file you should need to edit:

```json
{
  "roots": ["/home/you/projects"],
  "exclude_dirs": ["node_modules", ".git", "dist", "..."],
  "archive_after_days": 730,
  "overrides": {
    "bean-rpg":      { "tags": ["game", "js"], "status": "active" },
    "pokemon-stats": { "tags": ["scraper"], "category": "data" }
  }
}
```

Per-project `overrides` let you hand-curate tags, category, status, or notes
and keep them stable across re-scans.

## Safety

- `scan` and `report` never write outside `out/` and `projtool.config.json`.
- `health` prints its plan without `--run` and never touches files outside
  the project's own install cache.
- `deps --apply` and `tidy --apply` refuse to operate on dirty git trees.
- `tidy --apply` moves, never deletes.

## Output layout

```
out/
├── inventory.json      # full machine-readable catalog
├── inventory.md        # human-readable markdown table, grouped by category
├── dashboard.html      # single-file browser UI (sort, filter, search)
└── health/
    └── <project>.log   # captured install/build/test/lint output
```

## Detection reference

**Stacks**: node (`package.json`), python (`pyproject.toml` /
`requirements.txt` / `setup.py`), rust (`Cargo.toml`), go (`go.mod`), cpp
(`CMakeLists.txt` or ≥5 `.cpp`/`.c` files), dotnet (`*.sln` / `*.csproj`),
minecraft-datapack (`pack.mcmeta`), static-web (`index.html` only),
meta (folder starts with `_`), unknown (anything else).

**Categories** are inferred from folder name patterns (`site-*` → website,
`*-game` → game, `*-scraper` → data, `*-api` → service, `*-cli` → tool,
`advent-of-code-*` → experiment, `eslint-config-*` → library, …).
Override any project in `projtool.config.json`.
