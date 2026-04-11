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

## Quick start

```sh
# 1. Point the toolkit at your projects folder
#    Edit projtool.config.json and set "roots": ["/path/to/your/projects"]

# 2. Inventory everything (read-only)
python projtool.py scan

# 3. Render reports
python projtool.py report
#    -> out/inventory.json
#    -> out/inventory.md
#    -> out/dashboard.html    <- open this in a browser

# 4. (Optional) check health — dry-run by default
python projtool.py health            # prints the plan
python projtool.py health --run      # actually runs install/build/test/lint

# 5. (Optional) check outdated dependencies
python projtool.py deps

# 6. (Optional) flag dead projects for archival
python projtool.py tidy              # dry-run
python projtool.py tidy --apply      # moves flagged projects to _archive/
```

On Windows use `python projtool.py ...` or the `bin\projtool.cmd` wrapper.
On Linux/macOS you can also use `bin/projtool ...`.

## Subcommands

| Command | What it does |
|---|---|
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
