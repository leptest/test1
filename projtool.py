#!/usr/bin/env python3
"""
projtool - a personal multi-project management CLI.

Stdlib-only. Cross-platform (Windows + Linux + macOS).

Subcommands:
  scan     Walk roots, build out/inventory.json.
  report   Render inventory.json -> inventory.md + dashboard.html.
  health   Run install/build/test/lint per project (dry-run without --run).
  deps     Check outdated dependencies (dry-run without --apply).
  tidy     Flag archival candidates (dry-run without --apply).
  tag      Add manual tags to a project in the config overrides.

Usage:
  python projtool.py scan
  python projtool.py report
  python projtool.py health [name] [--run]
  python projtool.py deps [name] [--apply]
  python projtool.py tidy [--apply]
  python projtool.py tag <name> <tag> [<tag> ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# --------------------------------------------------------------------------- #
#  Paths / constants
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "projtool.config.json"
OUT_DIR = HERE / "out"
INVENTORY_PATH = OUT_DIR / "inventory.json"
MD_PATH = OUT_DIR / "inventory.md"
HTML_PATH = OUT_DIR / "dashboard.html"
HEALTH_DIR = OUT_DIR / "health"

DEFAULT_EXCLUDES = [
    "node_modules", ".git", "dist", "build", ".next", ".nuxt",
    "target", "venv", ".venv", "__pycache__", ".cache", ".parcel-cache",
    "out", "coverage", ".turbo",
]

HEALTH_TIMEOUT_SECONDS = 300

# --------------------------------------------------------------------------- #
#  Config
# --------------------------------------------------------------------------- #

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {
            "roots": [],
            "exclude_dirs": DEFAULT_EXCLUDES,
            "archive_after_days": 730,
            "overrides": {},
        }
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("roots", [])
    cfg.setdefault("exclude_dirs", DEFAULT_EXCLUDES)
    cfg.setdefault("archive_after_days", 730)
    cfg.setdefault("overrides", {})
    return cfg


def save_config(cfg: dict) -> None:
    write_text(CONFIG_PATH, json.dumps(cfg, indent=2) + "\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def load_inventory() -> list[dict]:
    if not INVENTORY_PATH.exists():
        return []
    with INVENTORY_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_inventory(projects: list[dict]) -> None:
    write_text(INVENTORY_PATH, json.dumps(projects, indent=2) + "\n")


# --------------------------------------------------------------------------- #
#  Stack + category detection
# --------------------------------------------------------------------------- #

NODE_FRAMEWORK_KEYS = [
    "react", "next", "vite", "vue", "svelte", "nuxt",
    "express", "koa", "fastify", "nestjs", "@nestjs/core",
    "ethers", "hardhat", "web3", "electron", "gatsby",
    "remix", "astro", "solid-js",
]


def _read_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _count_files(project: Path, suffixes: tuple[str, ...], limit: int = 10) -> int:
    count = 0
    for p in project.rglob("*"):
        if any(part in DEFAULT_EXCLUDES for part in p.parts):
            continue
        if p.is_file() and p.suffix.lower() in suffixes:
            count += 1
            if count >= limit:
                break
    return count


def detect_stack(project: Path) -> tuple[list[str], list[str]]:
    """Return (stacks, frameworks). A project can have multiple stacks."""
    stacks: list[str] = []
    frameworks: list[str] = []

    name = project.name
    if name.startswith("_"):
        stacks.append("meta")
        return stacks, frameworks

    pkg_json = project / "package.json"
    if pkg_json.exists():
        stacks.append("node")
        data = _read_json(pkg_json) or {}
        deps = {}
        deps.update(data.get("dependencies") or {})
        deps.update(data.get("devDependencies") or {})
        lower = {k.lower() for k in deps.keys()}
        for fw in NODE_FRAMEWORK_KEYS:
            if fw in lower or any(k.startswith(fw + "/") or k == fw for k in lower):
                frameworks.append(fw)

    if (project / "pyproject.toml").exists() or \
       (project / "requirements.txt").exists() or \
       (project / "setup.py").exists() or \
       _count_files(project, (".py",), limit=4) >= 3:
        stacks.append("python")

    if (project / "Cargo.toml").exists():
        stacks.append("rust")

    if (project / "go.mod").exists():
        stacks.append("go")

    if (project / "CMakeLists.txt").exists() or \
       _count_files(project, (".cpp", ".cc", ".hpp", ".c"), limit=6) >= 5:
        stacks.append("cpp")

    if list(project.glob("*.sln")) or list(project.glob("*.csproj")):
        stacks.append("dotnet")

    if (project / "pack.mcmeta").exists():
        stacks.append("minecraft-datapack")

    if not stacks and (project / "index.html").exists():
        stacks.append("static-web")

    if not stacks:
        stacks.append("unknown")

    return stacks, frameworks


CATEGORY_RULES = [
    ("website",    ["site-", "-www", "www-"]),
    ("game",       ["-game", "game-", "rpg-", "loot-", "aoe2-", "garden-"]),
    ("data",       ["-scraper", "-stats", "analytics", "market-research"]),
    ("service",    ["-api", "unify-", "announce-", "bullpost"]),
    ("tool",       ["-cli", "image-min", "bundler", "eslint-config-", "eslinttest-"]),
    ("experiment", ["advent-of-code-", "-hackathon", "genetic-", "-test", "niko-"]),
    ("library",    ["-library", "-component-library"]),
]


def detect_category(name: str, stacks: list[str]) -> str:
    if "meta" in stacks:
        return "meta"
    lname = name.lower()
    for cat, needles in CATEGORY_RULES:
        for needle in needles:
            if needle in lname:
                return cat
    # Fallback from stack
    if "minecraft-datapack" in stacks:
        return "game"
    if "static-web" in stacks:
        return "website"
    return "other"


# --------------------------------------------------------------------------- #
#  Git info
# --------------------------------------------------------------------------- #

def _kill_tree(pid: int) -> None:
    # On Windows, npm.CMD/yarn.CMD spawn node children that survive Popen.kill().
    # Walk the parent-PID tree via taskkill; on POSIX use the session group.
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> tuple[int, str]:
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            shell=False, **popen_kwargs,
        )
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except Exception as e:  # pragma: no cover
        return 1, repr(e)

    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            out = ""
        return 124, (out or "") + "\n[timeout]"


def git_info(project: Path) -> dict:
    if not (project / ".git").exists():
        return {"tracked": False}
    git = shutil.which("git")
    if not git:
        return {"tracked": True, "error": "git not installed"}

    info: dict = {"tracked": True}

    rc, out = _run([git, "log", "-1", "--format=%cI"], cwd=project)
    if rc == 0 and out.strip():
        info["last_commit"] = out.strip().split("T")[0]
        info["last_commit_iso"] = out.strip()
    else:
        info["last_commit"] = None

    rc, out = _run([git, "rev-parse", "--abbrev-ref", "HEAD"], cwd=project)
    info["branch"] = out.strip() if rc == 0 else None

    rc, out = _run([git, "status", "--porcelain"], cwd=project)
    info["dirty"] = bool(out.strip()) if rc == 0 else None

    rc, out = _run([git, "rev-list", "--count", "HEAD"], cwd=project)
    info["commit_count"] = int(out.strip()) if rc == 0 and out.strip().isdigit() else None

    return info


# --------------------------------------------------------------------------- #
#  Size + README
# --------------------------------------------------------------------------- #

def dir_size_mb(project: Path, excludes: list[str]) -> float:
    total = 0
    excl = set(excludes)
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in excl]
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)


def read_readme(project: Path) -> tuple[bool, str]:
    for name in ("README.md", "readme.md", "README.MD", "README", "README.txt"):
        p = project / name
        if p.exists():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return True, ""
            for line in text.splitlines():
                s = line.strip().lstrip("#").strip()
                if s:
                    return True, s[:200]
            return True, ""
    return False, ""


# --------------------------------------------------------------------------- #
#  Scan
# --------------------------------------------------------------------------- #

def scan(cfg: dict) -> list[dict]:
    projects: list[dict] = []
    roots = [Path(r).expanduser() for r in cfg["roots"]]
    if not roots:
        print("!! No roots configured. Edit projtool.config.json -> roots: [...] ")
        return projects

    excludes = cfg.get("exclude_dirs", DEFAULT_EXCLUDES)
    overrides = cfg.get("overrides", {})

    for root in roots:
        if not root.exists():
            print(f"!! root does not exist: {root}")
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if child.name in excludes:
                continue
            rec = scan_one(child, excludes)
            ov = overrides.get(child.name, {})
            if "tags" in ov:
                rec["tags"] = sorted(set(rec.get("tags", []) + ov["tags"]))
            if "status" in ov:
                rec["status"] = ov["status"]
            if "category" in ov:
                rec["category"] = ov["category"]
            if "notes" in ov:
                rec["notes"] = ov["notes"]
            projects.append(rec)

    return projects


def scan_one(project: Path, excludes: list[str]) -> dict:
    stacks, frameworks = detect_stack(project)
    category = detect_category(project.name, stacks)
    has_readme, desc = read_readme(project)
    return {
        "name": project.name,
        "path": str(project),
        "stacks": stacks,
        "frameworks": frameworks,
        "category": category,
        "tags": [],
        "size_mb": dir_size_mb(project, excludes),
        "git": git_info(project),
        "readme": desc,
        "has_readme": has_readme,
        "health": None,
        "deps_outdated": None,
    }


# --------------------------------------------------------------------------- #
#  Health
# --------------------------------------------------------------------------- #

def health_plan(rec: dict) -> list[tuple[str, list[str]]]:
    """Return [(step_name, argv)] to run for this project."""
    stacks = rec["stacks"]
    project = Path(rec["path"])
    plan: list[tuple[str, list[str]]] = []

    if "node" in stacks:
        pkg = _read_json(project / "package.json") or {}
        scripts = pkg.get("scripts") or {}
        npm = shutil.which("npm")
        if npm:
            if (project / "package-lock.json").exists():
                plan.append(("install", [npm, "ci", "--ignore-scripts"]))
            else:
                plan.append(("install", [npm, "install", "--ignore-scripts"]))
            if "build" in scripts:
                plan.append(("build", [npm, "run", "build", "--if-present"]))
            if "test" in scripts:
                plan.append(("test", [npm, "test", "--", "--passWithNoTests"]))
            if "lint" in scripts:
                plan.append(("lint", [npm, "run", "lint", "--if-present"]))

    if "python" in stacks:
        py = shutil.which("python3") or shutil.which("python")
        if py and (project / "requirements.txt").exists():
            plan.append(("install", [py, "-m", "pip", "install", "-r", "requirements.txt", "--dry-run"]))
        if shutil.which("pytest"):
            plan.append(("test", ["pytest", "--collect-only", "-q"]))

    if "rust" in stacks:
        cargo = shutil.which("cargo")
        if cargo:
            plan.append(("install", [cargo, "fetch"]))
            plan.append(("build", [cargo, "build"]))
            plan.append(("test", [cargo, "test", "--no-run"]))

    if "go" in stacks:
        go = shutil.which("go")
        if go:
            plan.append(("install", [go, "mod", "download"]))
            plan.append(("build", [go, "build", "./..."]))
            plan.append(("test", [go, "test", "-count=0", "./..."]))

    return plan


def run_health(rec: dict, do_run: bool) -> dict:
    project = Path(rec["path"])
    plan = health_plan(rec)
    result = {"install": "skipped", "build": "skipped", "test": "skipped", "lint": "skipped",
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}

    if not plan:
        return result

    if not do_run:
        print(f"  [dry-run] {rec['name']}:")
        for step, argv in plan:
            print(f"    {step}: {' '.join(argv)}")
        return result

    HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    log_path = HEALTH_DIR / f"{rec['name']}.log"
    lines: list[str] = [f"# {rec['name']}  ({dt.datetime.now().isoformat()})\n"]

    for step, argv in plan:
        lines.append(f"\n$ {' '.join(argv)}\n")
        print(f"  {rec['name']:40s} {step:8s} running...", end="", flush=True)
        rc, out = _run(argv, cwd=project, timeout=HEALTH_TIMEOUT_SECONDS)
        tail = "\n".join(out.splitlines()[-200:])
        lines.append(tail + "\n")
        lines.append(f"[exit {rc}]\n")
        if rc == 0:
            result[step] = "ok"
        elif rc == 124:
            result[step] = "timeout"
        else:
            result[step] = "fail"
        print(f"\r  {rec['name']:40s} {step:8s} {result[step]:12s}")

    write_text(log_path, "".join(lines))
    return result


def cmd_health(args: argparse.Namespace) -> None:
    projects = load_inventory()
    if not projects:
        print("!! no inventory; run `scan` first")
        return
    target = args.name
    for rec in projects:
        if target and rec["name"] != target:
            continue
        rec["health"] = run_health(rec, do_run=args.run)
    save_inventory(projects)


# --------------------------------------------------------------------------- #
#  Deps
# --------------------------------------------------------------------------- #

def check_outdated(rec: dict) -> dict | None:
    project = Path(rec["path"])
    stacks = rec["stacks"]
    out: dict = {}

    if "node" in stacks:
        npm = shutil.which("npm")
        if npm:
            rc, txt = _run([npm, "outdated", "--json"], cwd=project, timeout=120)
            try:
                data = json.loads(txt) if txt.strip() else {}
            except json.JSONDecodeError:
                data = {}
            out["npm"] = {k: {"current": v.get("current"), "latest": v.get("latest")}
                          for k, v in data.items()}

    if "python" in stacks:
        py = shutil.which("python3") or shutil.which("python")
        if py:
            rc, txt = _run([py, "-m", "pip", "list", "--outdated", "--format=json"],
                           cwd=project, timeout=120)
            try:
                data = json.loads(txt) if txt.strip().startswith("[") else []
            except json.JSONDecodeError:
                data = []
            out["pip"] = {d["name"]: {"current": d.get("version"), "latest": d.get("latest_version")}
                          for d in data}

    return out or None


def cmd_deps(args: argparse.Namespace) -> None:
    projects = load_inventory()
    if not projects:
        print("!! no inventory; run `scan` first")
        return
    for rec in projects:
        if args.name and rec["name"] != args.name:
            continue
        print(f"== {rec['name']}")
        if args.apply:
            if rec.get("git", {}).get("dirty"):
                print("  refusing --apply: dirty git tree")
                continue
            print("  --apply not implemented for safety; run package manager manually")
            continue
        data = check_outdated(rec)
        rec["deps_outdated"] = data
        if not data:
            print("  no data")
            continue
        for mgr, pkgs in data.items():
            if not pkgs:
                print(f"  {mgr}: up to date")
                continue
            print(f"  {mgr}: {len(pkgs)} outdated")
            for name, v in list(pkgs.items())[:10]:
                print(f"    {name:30s} {v.get('current')} -> {v.get('latest')}")
    save_inventory(projects)


# --------------------------------------------------------------------------- #
#  Tidy
# --------------------------------------------------------------------------- #

def cmd_tidy(args: argparse.Namespace) -> None:
    cfg = load_config()
    projects = load_inventory()
    if not projects:
        print("!! no inventory; run `scan` first")
        return
    threshold_days = cfg.get("archive_after_days", 730)
    now = dt.datetime.now(dt.timezone.utc)
    flagged: list[dict] = []

    for rec in projects:
        if rec.get("category") == "meta":
            continue
        git = rec.get("git") or {}
        last = git.get("last_commit_iso")
        stale = False
        if last:
            try:
                d = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                stale = (now - d).days >= threshold_days
            except ValueError:
                pass
        else:
            stale = True

        health = rec.get("health") or {}
        broken = all(v in (None, "skipped", "fail") for k, v in health.items() if k != "checked_at") if health else True
        tiny_or_no_readme = rec.get("size_mb", 0) < 1 or not rec.get("has_readme")

        if stale and broken and tiny_or_no_readme:
            flagged.append(rec)

    print(f"== {len(flagged)} archival candidates")
    for rec in flagged:
        print(f"  {rec['name']:40s} last={rec.get('git', {}).get('last_commit')} size={rec.get('size_mb')}MB")

    if not args.apply:
        print("(dry-run; pass --apply to move them)")
        return

    for rec in flagged:
        src = Path(rec["path"])
        dest = src.parent / "_archive" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            print(f"  skip (exists): {dest}")
            continue
        print(f"  mv {src} -> {dest}")
        shutil.move(str(src), str(dest))
        rec["path"] = str(dest)
        rec["tags"] = sorted(set(rec.get("tags", []) + ["archived"]))
    save_inventory(projects)


# --------------------------------------------------------------------------- #
#  Tag
# --------------------------------------------------------------------------- #

def cmd_tag(args: argparse.Namespace) -> None:
    cfg = load_config()
    overrides = cfg.setdefault("overrides", {})
    entry = overrides.setdefault(args.name, {})
    existing = set(entry.get("tags", []))
    existing.update(args.tags)
    entry["tags"] = sorted(existing)
    save_config(cfg)
    print(f"{args.name}: tags = {entry['tags']}")


# --------------------------------------------------------------------------- #
#  Report: Markdown
# --------------------------------------------------------------------------- #

def _health_glyph(health: dict | None, key: str) -> str:
    if not health:
        return "–"
    v = health.get(key)
    return {"ok": "✓", "fail": "✗", "skipped": "–", None: "–"}.get(v, "–")


def render_markdown(projects: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# Project inventory\n")
    lines.append(f"_Generated {dt.datetime.now().isoformat(timespec='seconds')}_  \n")
    lines.append(f"_Total projects: **{len(projects)}**_\n\n")

    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for rec in projects:
        by_cat.setdefault(rec.get("category", "other"), []).append(rec)

    for cat in sorted(by_cat.keys()):
        group = sorted(by_cat[cat], key=lambda r: (r.get("git") or {}).get("last_commit") or "", reverse=True)
        lines.append(f"## {cat}  ({len(group)})\n\n")
        lines.append("| Name | Stacks | Last commit | Size (MB) | I | B | T | L | Tags |\n")
        lines.append("|---|---|---|---|---|---|---|---|---|\n")
        for rec in group:
            name = rec["name"]
            stacks = ", ".join(rec.get("stacks", []))
            last = (rec.get("git") or {}).get("last_commit") or "–"
            size = rec.get("size_mb", 0)
            h = rec.get("health")
            gi = _health_glyph(h, "install")
            gb = _health_glyph(h, "build")
            gt = _health_glyph(h, "test")
            gl = _health_glyph(h, "lint")
            tags = ", ".join(rec.get("tags") or [])
            lines.append(f"| {name} | {stacks} | {last} | {size} | {gi} | {gb} | {gt} | {gl} | {tags} |\n")
        lines.append("\n")

    return "".join(lines)


# --------------------------------------------------------------------------- #
#  Report: HTML
# --------------------------------------------------------------------------- #

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Project dashboard</title>
<style>
 body { font: 14px/1.4 -apple-system, Segoe UI, sans-serif; margin: 24px; color: #222; background: #fafafa; }
 h1 { margin: 0 0 4px; }
 .meta { color: #666; margin-bottom: 16px; }
 .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }
 .controls input { padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; min-width: 220px; }
 .chip { padding: 4px 10px; border: 1px solid #bbb; border-radius: 999px; background: #fff; cursor: pointer; font-size: 12px; }
 .chip.active { background: #222; color: #fff; border-color: #222; }
 table { border-collapse: collapse; width: 100%; background: #fff; }
 th, td { padding: 6px 10px; border-bottom: 1px solid #eee; text-align: left; vertical-align: top; }
 th { background: #f0f0f0; cursor: pointer; position: sticky; top: 0; }
 tr.row:hover { background: #fafcff; }
 .ok { color: #1a7f37; }
 .fail { color: #cf222e; }
 .skip { color: #999; }
 .stacks span { display: inline-block; padding: 1px 6px; background: #eef; border-radius: 3px; margin-right: 3px; font-size: 11px; }
 tr.detail td { background: #fcfcfc; border-bottom: 2px solid #ddd; }
 .detail-body { padding: 6px 0; color: #444; }
 .detail-body pre { background: #f4f4f4; padding: 8px; overflow-x: auto; }
</style>
</head>
<body>
<h1>Project dashboard</h1>
<div class="meta">Generated __GENERATED__ · <b>__COUNT__</b> projects</div>

<div class="controls">
 <input id="q" placeholder="search name, stack, tag, readme…">
 <span id="chips"></span>
</div>

<table>
 <thead><tr>
  <th data-key="name">name</th>
  <th data-key="category">category</th>
  <th data-key="stacks">stacks</th>
  <th data-key="last">last commit</th>
  <th data-key="size_mb">MB</th>
  <th>health</th>
  <th>tags</th>
 </tr></thead>
 <tbody id="tbody"></tbody>
</table>

<script>
const DATA = __DATA__;

function glyph(h, k) {
  if (!h) return '<span class="skip">–</span>';
  const v = h[k];
  if (v === 'ok') return '<span class="ok">✓</span>';
  if (v === 'fail') return '<span class="fail">✗</span>';
  return '<span class="skip">–</span>';
}

function row(rec) {
  const last = (rec.git && rec.git.last_commit) || '–';
  const stacks = (rec.stacks || []).map(s => '<span>' + s + '</span>').join('');
  const tags = (rec.tags || []).join(', ');
  const hb = rec.health ? [glyph(rec.health,'install'), glyph(rec.health,'build'),
                           glyph(rec.health,'test'), glyph(rec.health,'lint')].join(' ')
                        : '<span class="skip">–</span>';
  return `<tr class="row" data-name="${rec.name}">
    <td><b>${rec.name}</b></td>
    <td>${rec.category || ''}</td>
    <td class="stacks">${stacks}</td>
    <td>${last}</td>
    <td>${rec.size_mb || 0}</td>
    <td>${hb}</td>
    <td>${tags}</td>
  </tr>
  <tr class="detail" data-for="${rec.name}" style="display:none"><td colspan="7">
    <div class="detail-body">
      <div><b>path:</b> ${rec.path}</div>
      <div><b>readme:</b> ${(rec.readme || '').replace(/</g,'&lt;')}</div>
      <div><b>frameworks:</b> ${(rec.frameworks || []).join(', ') || '–'}</div>
      <div><b>git:</b> branch=${(rec.git||{}).branch||'–'} dirty=${(rec.git||{}).dirty} commits=${(rec.git||{}).commit_count||'–'}</div>
    </div>
  </td></tr>`;
}

let activeCat = null;
let sortKey = 'last';
let sortDir = -1;

function render() {
  const q = document.getElementById('q').value.toLowerCase();
  const rows = DATA.filter(r => {
    if (activeCat && r.category !== activeCat) return false;
    if (!q) return true;
    const hay = [r.name, r.category, (r.stacks||[]).join(' '),
                 (r.tags||[]).join(' '), (r.readme||'')].join(' ').toLowerCase();
    return hay.includes(q);
  });
  rows.sort((a,b) => {
    const av = sortKey === 'last' ? ((a.git||{}).last_commit || '')
             : sortKey === 'stacks' ? (a.stacks||[]).join(',')
             : (a[sortKey] ?? '');
    const bv = sortKey === 'last' ? ((b.git||{}).last_commit || '')
             : sortKey === 'stacks' ? (b.stacks||[]).join(',')
             : (b[sortKey] ?? '');
    if (av < bv) return -1 * sortDir;
    if (av > bv) return 1 * sortDir;
    return 0;
  });
  document.getElementById('tbody').innerHTML = rows.map(row).join('');
}

function renderChips() {
  const cats = Array.from(new Set(DATA.map(r => r.category || 'other'))).sort();
  const el = document.getElementById('chips');
  el.innerHTML = cats.map(c => `<span class="chip" data-cat="${c}">${c}</span>`).join('');
  el.querySelectorAll('.chip').forEach(ch => ch.onclick = () => {
    if (activeCat === ch.dataset.cat) { activeCat = null; }
    else { activeCat = ch.dataset.cat; }
    el.querySelectorAll('.chip').forEach(x => x.classList.toggle('active', x.dataset.cat === activeCat));
    render();
  });
}

document.getElementById('q').oninput = render;
document.querySelectorAll('th[data-key]').forEach(th => {
  th.onclick = () => {
    const k = th.dataset.key;
    if (sortKey === k) sortDir *= -1; else { sortKey = k; sortDir = 1; }
    render();
  };
});
document.addEventListener('click', e => {
  const tr = e.target.closest('tr.row');
  if (!tr) return;
  const detail = document.querySelector(`tr.detail[data-for="${tr.dataset.name}"]`);
  if (detail) detail.style.display = detail.style.display === 'none' ? '' : 'none';
});

renderChips();
render();
</script>
</body>
</html>
"""


def render_html(projects: list[dict]) -> str:
    data = json.dumps(projects)
    return (HTML_TEMPLATE
            .replace("__GENERATED__", html.escape(dt.datetime.now().isoformat(timespec="seconds")))
            .replace("__COUNT__", str(len(projects)))
            .replace("__DATA__", data))


# --------------------------------------------------------------------------- #
#  CLI handlers
# --------------------------------------------------------------------------- #

def cmd_scan(args: argparse.Namespace) -> None:
    cfg = load_config()
    projects = scan(cfg)
    save_inventory(projects)
    print(f"scanned {len(projects)} projects -> {INVENTORY_PATH}")


def cmd_report(args: argparse.Namespace) -> None:
    projects = load_inventory()
    if not projects:
        print("!! no inventory; run `scan` first")
        return
    write_text(MD_PATH, render_markdown(projects))
    write_text(HTML_PATH, render_html(projects))
    print(f"wrote {MD_PATH}")
    print(f"wrote {HTML_PATH}")


# --------------------------------------------------------------------------- #
#  TUI
# --------------------------------------------------------------------------- #

ANSI = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
    "grey": "\033[90m", "bgblue": "\033[44m",
}


def ansi_enable_windows() -> None:
    """On Windows 10+, flip the console into VT processing mode."""
    if os.name == "nt":
        # Triggers ENABLE_VIRTUAL_TERMINAL_PROCESSING via cmd.exe
        os.system("")


def c(text: str, *codes: str) -> str:
    return "".join(ANSI.get(k, "") for k in codes) + text + ANSI["reset"]


def term_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(100, 30))
    return max(60, size.columns), max(15, size.lines)


def clear() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def strip_ansi(text: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", text)


def visible_len(text: str) -> int:
    return len(strip_ansi(text))


def pad(text: str, width: int, align: str = "left") -> str:
    pad_n = max(0, width - visible_len(text))
    if align == "right":
        return " " * pad_n + text
    if align == "center":
        left = pad_n // 2
        return " " * left + text + " " * (pad_n - left)
    return text + " " * pad_n


def truncate(text: str, width: int) -> str:
    if visible_len(text) <= width:
        return text
    # Stripping ANSI for truncation; TUI table cells don't carry ANSI through here.
    plain = strip_ansi(text)
    if width <= 1:
        return plain[:width]
    return plain[: width - 1] + "…"


def box(title: str, lines: list[str], width: int) -> list[str]:
    top = "╭─ " + title + " " + "─" * max(0, width - 4 - len(title)) + "╮"
    bot = "╰" + "─" * (width - 2) + "╯"
    out = [c(top, "cyan")]
    for line in lines:
        content = truncate(line, width - 4)
        out.append(c("│ ", "cyan") + pad(content, width - 4) + c(" │", "cyan"))
    out.append(c(bot, "cyan"))
    return out


def table(headers: list[str], rows: list[list[str]], widths: list[int]) -> list[str]:
    """Render a simple table with Unicode separators."""
    def sep(left: str, mid: str, right: str, fill: str) -> str:
        parts = [fill * (w + 2) for w in widths]
        return left + mid.join(parts) + right

    out: list[str] = []
    out.append(c(sep("┌", "┬", "┐", "─"), "grey"))
    header_row = c("│", "grey") + c("│", "grey").join(
        " " + c(pad(truncate(h, w), w), "bold") + " " for h, w in zip(headers, widths)
    ) + c("│", "grey")
    out.append(header_row)
    out.append(c(sep("├", "┼", "┤", "─"), "grey"))
    for row in rows:
        cells = []
        for cell, w in zip(row, widths):
            cells.append(" " + pad(truncate(cell, w), w) + " ")
        out.append(c("│", "grey") + c("│", "grey").join(cells) + c("│", "grey"))
    out.append(c(sep("└", "┴", "┘", "─"), "grey"))
    return out


def pause(msg: str = "press enter to continue") -> None:
    try:
        input(c(f"\n{msg} ", "dim"))
    except EOFError:
        pass


def prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(c(f"{msg}{suffix}: ", "yellow")).strip()
    except EOFError:
        return default
    return val or default


def confirm(msg: str, default: bool = False) -> bool:
    yn = "Y/n" if default else "y/N"
    val = prompt(f"{msg} ({yn})").lower()
    if not val:
        return default
    return val.startswith("y")


# --- TUI screens --------------------------------------------------------------

def tui_header(width: int) -> None:
    title = "  projtool — personal multi-project dashboard  "
    sys.stdout.write(c(pad(title, width, "center"), "bold", "bgblue") + "\n")


def tui_status_lines(cfg: dict, projects: list[dict]) -> list[str]:
    lines: list[str] = []
    roots = cfg.get("roots") or []
    if roots:
        lines.append(c("roots: ", "dim") + c(", ".join(roots), "green"))
    else:
        lines.append(c("roots: ", "dim") + c("(none configured — press 1)", "red"))

    if projects:
        cats: dict[str, int] = {}
        for r in projects:
            cats[r.get("category", "other")] = cats.get(r.get("category", "other"), 0) + 1
        cats_str = " · ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1]))
        lines.append(c("inventory: ", "dim") + c(f"{len(projects)} projects", "green") +
                     c(f"   ({cats_str})", "grey"))

        # Stacks breakdown
        stacks: dict[str, int] = {}
        for r in projects:
            for s in r.get("stacks") or []:
                stacks[s] = stacks.get(s, 0) + 1
        if stacks:
            top = sorted(stacks.items(), key=lambda kv: -kv[1])[:6]
            lines.append(c("stacks: ", "dim") + " · ".join(f"{k} {v}" for k, v in top))

        # Health summary (across all projects)
        hstats = {"ok": 0, "fail": 0, "skipped": 0, "none": 0}
        for r in projects:
            h = r.get("health")
            if not h:
                hstats["none"] += 1
                continue
            for k in ("install", "build", "test", "lint"):
                v = h.get(k)
                if v in hstats:
                    hstats[v] += 1
        checked = sum(1 for r in projects if r.get("health"))
        lines.append(c("health: ", "dim") +
                     c(f"{hstats['ok']} ok", "green") + " · " +
                     c(f"{hstats['fail']} fail", "red") + " · " +
                     c(f"{hstats['skipped']} skipped", "grey") +
                     c(f"   ({checked}/{len(projects)} projects checked)", "grey"))
    else:
        lines.append(c("inventory: ", "dim") + c("(empty — press 2 to scan)", "red"))
    return lines


MENU_ITEMS = [
    ("1", "configure", "Configure project roots"),
    ("2", "scan",      "Scan projects (read-only)"),
    ("3", "browse",    "Browse inventory (table view)"),
    ("4", "report",    "Render inventory.md + dashboard.html"),
    ("5", "health",    "Run health checks (install/build/test/lint)"),
    ("6", "deps",      "Check outdated dependencies"),
    ("7", "tidy",      "Flag archival candidates"),
    ("8", "tag",       "Tag a project"),
    ("9", "open",      "Open HTML dashboard in browser"),
    ("q", "quit",      "Quit"),
]


def tui_draw_menu() -> None:
    print(c("\n  what do you want to do?\n", "bold"))
    for key, _name, desc in MENU_ITEMS:
        print(f"   {c(key, 'yellow', 'bold')})  {desc}")
    print()


def tui_main() -> int:
    ansi_enable_windows()
    first_run = True
    while True:
        cfg = load_config()
        projects = load_inventory()
        width, _height = term_size()

        clear()
        tui_header(width)
        print()
        for line in box("status", tui_status_lines(cfg, projects), width - 2):
            print(line)
        tui_draw_menu()

        # First-run nudge: if no roots, jump straight to configure.
        if first_run and not cfg.get("roots"):
            first_run = False
            print(c("  (no roots configured yet — let's set one up)\n", "yellow"))
            tui_configure()
            continue
        first_run = False

        choice = prompt("choose", "q").lower()
        action = None
        for key, name, _ in MENU_ITEMS:
            if choice == key or choice == name:
                action = name
                break
        if action == "quit":
            print(c("\nbye!\n", "dim"))
            return 0
        if action == "configure":
            tui_configure()
        elif action == "scan":
            tui_scan()
        elif action == "browse":
            tui_browse()
        elif action == "report":
            tui_report()
        elif action == "health":
            tui_health()
        elif action == "deps":
            tui_deps()
        elif action == "tidy":
            tui_tidy()
        elif action == "tag":
            tui_tag()
        elif action == "open":
            tui_open()
        else:
            print(c(f"  unknown choice: {choice!r}", "red"))
            pause()


def tui_configure() -> None:
    cfg = load_config()
    while True:
        clear()
        width, _ = term_size()
        tui_header(width)
        print()
        lines = []
        roots = cfg.get("roots") or []
        if roots:
            for i, r in enumerate(roots, 1):
                exists = Path(r).expanduser().exists()
                marker = c("✓", "green") if exists else c("✗ missing", "red")
                lines.append(f"{i}. {r}  {marker}")
        else:
            lines.append(c("(no roots yet)", "dim"))
        lines.append("")
        lines.append(f"archive_after_days: {cfg.get('archive_after_days', 730)}")
        lines.append(f"excludes: {', '.join(cfg.get('exclude_dirs', [])[:6])}…")
        for line in box("configuration", lines, width - 2):
            print(line)
        print()
        print("   " + c("a", "yellow") + ") add a root folder")
        print("   " + c("r", "yellow") + ") remove a root folder")
        print("   " + c("d", "yellow") + ") change archive_after_days")
        print("   " + c("b", "yellow") + ") back to main menu")
        choice = prompt("choose", "b").lower()
        if choice == "b":
            return
        if choice == "a":
            raw = prompt("folder path")
            if not raw:
                continue
            path = Path(raw).expanduser().resolve()
            if not path.exists():
                if not confirm(f"{path} does not exist; add anyway?", False):
                    continue
            cfg.setdefault("roots", []).append(str(path))
            save_config(cfg)
            print(c(f"  added {path}", "green"))
            pause()
        elif choice == "r":
            if not cfg.get("roots"):
                pause("nothing to remove — enter to go back")
                continue
            idx_s = prompt("number to remove")
            try:
                idx = int(idx_s) - 1
                removed = cfg["roots"].pop(idx)
                save_config(cfg)
                print(c(f"  removed {removed}", "yellow"))
            except (ValueError, IndexError):
                print(c("  invalid index", "red"))
            pause()
        elif choice == "d":
            val = prompt("archive_after_days", str(cfg.get("archive_after_days", 730)))
            try:
                cfg["archive_after_days"] = int(val)
                save_config(cfg)
            except ValueError:
                print(c("  not a number", "red"))
                pause()


def tui_scan() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    cfg = load_config()
    if not cfg.get("roots"):
        print(c("  no roots configured — use (1) configure first", "red"))
        pause()
        return
    print(c("  scanning… (this is read-only)\n", "dim"))
    projects = scan(cfg)
    save_inventory(projects)
    print(c(f"  ✓ scanned {len(projects)} projects", "green"))
    print(c(f"    wrote {INVENTORY_PATH}", "grey"))
    pause()


def tui_report() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    projects = load_inventory()
    if not projects:
        print(c("  no inventory — run (2) scan first", "red"))
        pause()
        return
    write_text(MD_PATH, render_markdown(projects))
    write_text(HTML_PATH, render_html(projects))
    print(c(f"  ✓ wrote {MD_PATH}", "green"))
    print(c(f"  ✓ wrote {HTML_PATH}", "green"))
    pause()


def _glyph_colored(health: dict | None, key: str) -> str:
    if not health:
        return c("–", "grey")
    v = health.get(key)
    if v == "ok":
        return c("✓", "green")
    if v == "fail":
        return c("✗", "red")
    return c("–", "grey")


def tui_browse() -> None:
    projects = load_inventory()
    if not projects:
        clear()
        print(c("  no inventory — run (2) scan first", "red"))
        pause()
        return

    filter_cat: str | None = None
    query: str = ""
    page = 0
    PAGE_SIZE = 20

    while True:
        clear()
        width, height = term_size()
        tui_header(width)

        # Filter + search
        filtered = projects
        if filter_cat:
            filtered = [r for r in filtered if r.get("category") == filter_cat]
        if query:
            q = query.lower()
            def match(r: dict) -> bool:
                hay = " ".join([
                    r.get("name", ""), r.get("category", ""),
                    " ".join(r.get("stacks") or []),
                    " ".join(r.get("tags") or []),
                    r.get("readme", "") or "",
                ]).lower()
                return q in hay
            filtered = [r for r in filtered if match(r)]

        filtered = sorted(
            filtered,
            key=lambda r: ((r.get("git") or {}).get("last_commit") or ""),
            reverse=True,
        )

        # Pagination
        total = len(filtered)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * PAGE_SIZE
        window = filtered[start : start + PAGE_SIZE]

        # Column widths: name, cat, stacks, last, MB, IBTL, tags
        col_name = 28
        col_cat = 10
        col_stacks = 18
        col_last = 10
        col_size = 6
        col_health = 9
        col_tags = max(10, width - (col_name + col_cat + col_stacks + col_last + col_size + col_health + 16))

        headers = ["name", "cat", "stacks", "last", "MB", "I B T L", "tags"]
        widths = [col_name, col_cat, col_stacks, col_last, col_size, col_health, col_tags]

        rows: list[list[str]] = []
        for r in window:
            last = (r.get("git") or {}).get("last_commit") or "–"
            h = r.get("health")
            hb = " ".join([
                _glyph_colored(h, "install"),
                _glyph_colored(h, "build"),
                _glyph_colored(h, "test"),
                _glyph_colored(h, "lint"),
            ])
            rows.append([
                r.get("name", ""),
                r.get("category", "") or "",
                ", ".join(r.get("stacks") or []),
                last,
                f"{r.get('size_mb', 0)}",
                hb,
                ", ".join(r.get("tags") or []),
            ])

        status_line = (
            c(f"showing {start + 1}–{start + len(window)} of {total}", "dim") +
            (c(f"  · category={filter_cat}", "cyan") if filter_cat else "") +
            (c(f"  · q={query!r}", "cyan") if query else "") +
            c(f"   page {page + 1}/{pages}", "grey")
        )
        print()
        print("  " + status_line)
        print()
        for line in table(headers, rows, widths):
            print("  " + line)

        print()
        print("  " + c("n", "yellow") + ") next  " +
              c("p", "yellow") + ") prev  " +
              c("f", "yellow") + ") filter category  " +
              c("/", "yellow") + ") search  " +
              c("c", "yellow") + ") clear filters")
        print("  " + c("#", "yellow") + ") view details by row number  " +
              c("b", "yellow") + ") back")
        choice = prompt("choose", "b").lower()

        if choice == "b" or choice == "":
            return
        if choice == "n":
            page += 1
        elif choice == "p":
            page -= 1
        elif choice == "c":
            filter_cat = None
            query = ""
            page = 0
        elif choice == "f":
            cats = sorted(set(r.get("category") or "other" for r in projects))
            print(c("  categories: " + ", ".join(cats), "cyan"))
            pick = prompt("category (blank to clear)")
            filter_cat = pick or None
            page = 0
        elif choice == "/":
            query = prompt("search")
            page = 0
        elif choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(window):
                tui_detail(window[idx])
            else:
                print(c("  out of range", "red"))
                pause()


def tui_detail(rec: dict) -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    git = rec.get("git") or {}
    h = rec.get("health") or {}
    lines = [
        c(rec["name"], "bold"),
        c(rec["path"], "grey"),
        "",
        f"category:   {rec.get('category', '') }",
        f"stacks:     {', '.join(rec.get('stacks') or [])}",
        f"frameworks: {', '.join(rec.get('frameworks') or []) or '–'}",
        f"tags:       {', '.join(rec.get('tags') or []) or '–'}",
        f"size:       {rec.get('size_mb', 0)} MB",
        "",
        f"git:        branch={git.get('branch') or '–'}  "
        f"last={git.get('last_commit') or '–'}  "
        f"dirty={git.get('dirty')}  commits={git.get('commit_count') or '–'}",
    ]
    if h:
        lines.append("")
        lines.append(
            "health:     " +
            f"install={h.get('install')}  "
            f"build={h.get('build')}  "
            f"test={h.get('test')}  "
            f"lint={h.get('lint')}"
        )
    if rec.get("readme"):
        lines.append("")
        lines.append("readme:")
        lines.append("  " + (rec.get("readme") or ""))
    log_path = HEALTH_DIR / f"{rec['name']}.log"
    if log_path.exists():
        lines.append("")
        lines.append(c(f"health log: {log_path}", "grey"))
    for line in box(rec["name"], lines, width - 2):
        print(line)
    pause()


def tui_health() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    projects = load_inventory()
    if not projects:
        print(c("  no inventory — run (2) scan first", "red"))
        pause()
        return

    print(c("  health checks run install/build/test/lint per project.", "dim"))
    print(c("  by default it only prints the plan (dry-run).", "dim"))
    print()
    name = prompt("project name (blank for all)")
    do_run = confirm("actually execute the commands? (otherwise dry-run)", False)
    print()
    count = 0
    for rec in projects:
        if name and rec["name"] != name:
            continue
        print(c(f"== {rec['name']}", "bold"))
        rec["health"] = run_health(rec, do_run=do_run)
        count += 1
    save_inventory(projects)
    print()
    print(c(f"  ✓ processed {count} project(s)", "green"))
    pause()


def tui_deps() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    projects = load_inventory()
    if not projects:
        print(c("  no inventory — run (2) scan first", "red"))
        pause()
        return
    name = prompt("project name (blank for all)")
    for rec in projects:
        if name and rec["name"] != name:
            continue
        data = check_outdated(rec)
        rec["deps_outdated"] = data
        print(c(f"== {rec['name']}", "bold"))
        if not data:
            print(c("  no data", "grey"))
            continue
        for mgr, pkgs in data.items():
            if not pkgs:
                print(f"  {mgr}: " + c("up to date", "green"))
                continue
            print(f"  {mgr}: " + c(f"{len(pkgs)} outdated", "yellow"))
            for pkg, v in list(pkgs.items())[:10]:
                print(f"    {pkg:30s} {v.get('current')} -> {v.get('latest')}")
    save_inventory(projects)
    pause()


def tui_tidy() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    cfg = load_config()
    projects = load_inventory()
    if not projects:
        print(c("  no inventory — run (2) scan first", "red"))
        pause()
        return

    threshold_days = cfg.get("archive_after_days", 730)
    now = dt.datetime.now(dt.timezone.utc)
    flagged: list[dict] = []
    for rec in projects:
        if rec.get("category") == "meta":
            continue
        git = rec.get("git") or {}
        last = git.get("last_commit_iso")
        stale = False
        if last:
            try:
                d = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                stale = (now - d).days >= threshold_days
            except ValueError:
                pass
        else:
            stale = True
        health = rec.get("health") or {}
        broken = all(
            v in (None, "skipped", "fail")
            for k, v in health.items() if k != "checked_at"
        ) if health else True
        tiny_or_no_readme = rec.get("size_mb", 0) < 1 or not rec.get("has_readme")
        if stale and broken and tiny_or_no_readme:
            flagged.append(rec)

    if not flagged:
        print(c("  nothing flagged for archival", "green"))
        pause()
        return

    rows = [
        [r["name"], (r.get("git") or {}).get("last_commit") or "–", f"{r.get('size_mb', 0)}"]
        for r in flagged
    ]
    for line in table(["name", "last commit", "MB"], rows, [40, 14, 8]):
        print("  " + line)

    print()
    if not confirm(f"move these {len(flagged)} projects into a sibling _archive/ folder?", False):
        print(c("  (dry-run only — nothing moved)", "dim"))
        pause()
        return
    for rec in flagged:
        src = Path(rec["path"])
        dest = src.parent / "_archive" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            print(c(f"  skip (exists): {dest}", "yellow"))
            continue
        print(c(f"  mv {src} -> {dest}", "yellow"))
        shutil.move(str(src), str(dest))
        rec["path"] = str(dest)
        rec["tags"] = sorted(set((rec.get("tags") or []) + ["archived"]))
    save_inventory(projects)
    pause()


def tui_tag() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    name = prompt("project name")
    if not name:
        return
    tags_raw = prompt("tags (space-separated)")
    tags = tags_raw.split()
    if not tags:
        print(c("  no tags given", "red"))
        pause()
        return
    cfg = load_config()
    entry = cfg.setdefault("overrides", {}).setdefault(name, {})
    existing = set(entry.get("tags", []))
    existing.update(tags)
    entry["tags"] = sorted(existing)
    save_config(cfg)
    print(c(f"  ✓ {name}: tags = {entry['tags']}", "green"))
    pause()


def tui_open() -> None:
    clear()
    width, _ = term_size()
    tui_header(width)
    print()
    if not HTML_PATH.exists():
        print(c("  no dashboard.html yet — run (4) report first", "red"))
        pause()
        return
    url = HTML_PATH.resolve().as_uri()
    print(c(f"  dashboard:  {HTML_PATH}", "green"))
    print(c(f"  url:        {url}", "green"))
    try:
        import webbrowser
        if confirm("open in default browser now?", True):
            webbrowser.open(url)
    except Exception as e:  # pragma: no cover
        print(c(f"  could not open browser: {e}", "red"))
    pause()


def cmd_tui(args: argparse.Namespace | None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tui_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="projtool", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tui", help="interactive TUI (default when no args given)").set_defaults(func=cmd_tui)
    sub.add_parser("scan", help="walk roots, build out/inventory.json").set_defaults(func=cmd_scan)
    sub.add_parser("report", help="render inventory.md + dashboard.html").set_defaults(func=cmd_report)

    h = sub.add_parser("health", help="run install/build/test/lint (dry-run without --run)")
    h.add_argument("name", nargs="?", help="project name; omit for all")
    h.add_argument("--run", action="store_true", help="actually execute commands")
    h.set_defaults(func=cmd_health)

    d = sub.add_parser("deps", help="check outdated deps (dry-run)")
    d.add_argument("name", nargs="?")
    d.add_argument("--apply", action="store_true")
    d.set_defaults(func=cmd_deps)

    t = sub.add_parser("tidy", help="flag archival candidates")
    t.add_argument("--apply", action="store_true")
    t.set_defaults(func=cmd_tidy)

    g = sub.add_parser("tag", help="add tags to a project (stored in config overrides)")
    g.add_argument("name")
    g.add_argument("tags", nargs="+")
    g.set_defaults(func=cmd_tag)

    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # No arguments -> launch the interactive TUI by default.
    if not argv:
        return cmd_tui(None) or 0
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
