#!/usr/bin/env python3
"""bubble_shield_doctor.py — STATIC dependency-inventory verifier (Layer 1).

Why this exists
---------------
The Bubble Shield one-liner installer (install-app.sh) is NOT zero-prerequisite:
it silently assumes a preexisting toolchain (git, curl, tar, …), fetches pinned
artifacts from the network (python-build-standalone, a Gemma model, the Claude
installer), and installs pip deps OFFLINE from `vendor/wheels/`. Reading the
scripts tells us what they *intend* to install — not what they silently assume.

Last client install, the client Mac was missing Python entirely and the founder
had to debug live. This tool makes every hidden install assumption EXPLICIT and
DIFFABLE, so a newly-assumed binary, a moved fetch URL, an unpinned/unvendored
dep, or a diverged model-id is caught at RELEASE time (CI) instead of on the
client's Mac.

Scope (Layer 1 ONLY): pure STATIC parse. NO installer execution, NO network, NO
downloads, NO TCC / Full-Disk-Access probing, NO clean-room / VM. It statically
parses install-app.sh + the ML/OCR setup scripts + constraints.txt + the vendored
wheel filenames and emits a 4-category dependency manifest, then diffs that
manifest against a committed baseline (DEPENDENCY-MANIFEST.json).

Hard runtime constraint: pure stdlib, MUST run under stock /usr/bin/python3 (3.9).
It must NOT need the 3.12 the installer provisions and must NOT import any
third-party package.

The 4 categories
----------------
1. must_preexist_binaries — binaries the client Mac must already have (Xcode CLT
   / macOS base) or the one-liner dies. Each with file:line provenance.
2. fetched_artifacts — artifacts fetched-not-bundled at install time, each with a
   source URL/id and any SHA / tag pin, so a moved/dead URL is caught.
3. pip_deps — pip deps installed into each venv (app / ml / gemma / openai / ocr).
4. path_and_python — `export PATH=` mutations in install-app.sh + every python3.N
   version pin referenced across the scripts.

Highest-value assertion (baked into --check and the wheel-coverage report):
every `constraints.txt` pin has a matching wheel in `vendor/wheels/*.whl`
(name + version; cp39 ABI for the compiled ones). A missing one = the offline
app-venv install fails on a client with no PyPI — the exact "debug on the spot"
class this tool is meant to prevent.

CLI
---
  --json     machine-readable manifest (the 4 categories + wheel coverage).
  --report   human-readable, grouped, with file:line provenance (default).
  --check    regenerate the manifest, DIFF vs the committed baseline
             (DEPENDENCY-MANIFEST.json), exit 1 with a clear message on ANY delta
             (new assumed binary / new fetched artifact / vendored-wheel MISSING
             for a constraints pin / MODEL_ID divergence). Exit 0 if identical.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Locate the repo root. This file lives at
#   <root>/plugin/bubble-shield/scripts/bubble_shield_doctor.py
# so the repo root is three parents up. The baseline manifest is committed next
# to the plugin at <root>/plugin/bubble-shield/DEPENDENCY-MANIFEST.json.
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3]
PLUGIN_DIR = _HERE.parents[1]                      # <root>/plugin/bubble-shield
SCRIPTS_DIR = _HERE.parent                         # <root>/plugin/bubble-shield/scripts
BASELINE_PATH = PLUGIN_DIR / "DEPENDENCY-MANIFEST.json"

INSTALL_SH = REPO_ROOT / "install-app.sh"
CONSTRAINTS_TXT = REPO_ROOT / "constraints.txt"
WHEELS_DIR = REPO_ROOT / "vendor" / "wheels"
SETUP_ML = SCRIPTS_DIR / "bubble_shield_setup_ml.py"
SETUP_OCR = SCRIPTS_DIR / "bubble_shield_setup_ocr.py"
GEMMA_CLASSIFIER = SCRIPTS_DIR / "gemma_classifier.py"
GEMMAD = SCRIPTS_DIR / "bubble_shield_gemmad.py"

# Binaries we consider "MUST pre-exist" when invoked bare in install-app.sh.
# These are all Xcode-Command-Line-Tools / macOS-base tools; if any is missing
# the one-liner dies before it can provision anything. (bash is the interpreter
# itself so it is implicitly required; we track the explicit tool invocations.)
KNOWN_BINARIES = {
    "git", "curl", "tar", "mktemp", "shasum", "uname",
    "xcode-select", "launchctl",
}

# The Gemma model id is duplicated across three scripts. If they ever diverge,
# that's a real bug (the daemon would load a model the setup didn't download, or
# the classifier would judge with a different model than the daemon). We collect
# all three and FLAG a divergence.
GEMMA_MODEL_SCRIPTS = {
    "gemma_classifier.py": GEMMA_CLASSIFIER,
    "bubble_shield_gemmad.py": GEMMAD,
    "bubble_shield_setup_ml.py": SETUP_ML,
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _rel(p: Path) -> str:
    """Repo-relative POSIX path for stable, machine-independent provenance."""
    try:
        return p.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _read_lines(p: Path) -> "list[str]":
    if not p.is_file():
        return []
    return p.read_text(encoding="utf-8", errors="replace").splitlines()


def _norm_dist(name: str) -> str:
    """Normalise a distribution name for comparison.

    PyPI treats '-', '_', and '.' as equivalent and is case-insensitive
    (PEP 503). Wheel filenames additionally replace '-' with '_'. So
    'annotated-doc' (constraint) must match 'annotated_doc' (wheel) —
    normalise both to a canonical lowercase-with-hyphens form.
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower())


# --------------------------------------------------------------------------- #
# Category 1 — MUST-PREEXIST binaries
# --------------------------------------------------------------------------- #
def scan_must_preexist_binaries() -> "list[dict]":
    """Statically scan install-app.sh for assumed-preexisting binaries.

    Two detection modes:
      * `command -v X` guards — an explicit "must exist" assertion.
      * bare invocations of a KNOWN_BINARIES tool at a command position.

    Every hit records the tool, file:line provenance, and how it was detected.
    We dedupe on (tool) but keep ALL provenance lines so a reviewer can see every
    place a tool is assumed. Comment-only lines (leading '#') are ignored so the
    long explanatory comments in the script don't create phantom assumptions.
    """
    lines = _read_lines(INSTALL_SH)
    rel = _rel(INSTALL_SH)

    # command -v <tool>
    cmd_v_re = re.compile(r"command\s+-v\s+([A-Za-z0-9._-]+)")
    # A bare tool invocation: the tool appears as the first token of a command.
    # We look for the tool as a "word" (start-of-line-ish or after a shell
    # separator) followed by whitespace/end. This is heuristic but bounded to
    # KNOWN_BINARIES so it can't over-match arbitrary identifiers.
    sep = r"(?:^|[;&|]|\|\||&&|\bthen\b|\bdo\b|\belse\b|\$\()\s*"
    bare_res = {
        tool: re.compile(sep + re.escape(tool) + r"(?=\s|$)")
        for tool in KNOWN_BINARIES
    }

    found: "dict[str, dict]" = {}

    def _record(tool: str, lineno: int, text: str, how: str) -> None:
        entry = found.setdefault(
            tool, {"binary": tool, "detected_via": set(), "provenance": []}
        )
        entry["detected_via"].add(how)
        entry["provenance"].append(
            {"file": rel, "line": lineno, "text": text.strip()}
        )

    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue  # pure comment line — not an actual invocation
        # Drop trailing comments so a '#' in a comment can't be scanned as code.
        code = raw.split(" #", 1)[0] if " #" in raw else raw

        for m in cmd_v_re.finditer(code):
            tool = m.group(1)
            if tool in KNOWN_BINARIES:
                _record(tool, i, raw, "command -v")

        for tool, rx in bare_res.items():
            # Skip if this line's only hit for the tool is the `command -v` form
            # (already recorded above) — but a bare invocation elsewhere on the
            # same line still counts.
            for _m in rx.finditer(code):
                # Avoid double-counting the `command -v git` occurrence as also a
                # bare `git` invocation (it's preceded by `command -v`).
                span_start = _m.start()
                preceding = code[max(0, span_start - 12):span_start]
                if re.search(r"command\s+-v\s*$", preceding):
                    continue
                _record(tool, i, raw, "bare invocation")

    # Deterministic order: alphabetical by binary.
    out = []
    for tool in sorted(found):
        e = found[tool]
        out.append(
            {
                "binary": e["binary"],
                "detected_via": sorted(e["detected_via"]),
                "provenance": e["provenance"],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Category 2 — FETCHED-not-bundled artifacts
# --------------------------------------------------------------------------- #
def scan_fetched_artifacts() -> "list[dict]":
    """Inventory every artifact fetched-not-bundled at install time.

    Each entry has a stable `id`, a human `kind`, the source URL/id, any pins
    (tag / version / SHA256), and file:line provenance. All values are parsed
    from the real tree (no hardcoded URLs) so a moved URL or changed pin surfaces
    as a manifest delta.
    """
    artifacts: "list[dict]" = []
    install_rel = _rel(INSTALL_SH)
    install_lines = _read_lines(INSTALL_SH)

    def _line_of(pattern: str, lines: "list[str]", rel: str) -> "dict | None":
        rx = re.compile(pattern)
        for i, ln in enumerate(lines, start=1):
            if rx.search(ln):
                return {"file": rel, "line": i, "text": ln.strip()}
        return None

    def _all_lines_of(pattern: str, lines: "list[str]", rel: str) -> "list[dict]":
        rx = re.compile(pattern)
        hits = []
        for i, ln in enumerate(lines, start=1):
            if rx.search(ln):
                hits.append({"file": rel, "line": i, "text": ln.strip()})
        return hits

    # --- python-build-standalone tarball ----------------------------------- #
    pbs_tag = None
    pbs_cpython = None
    pbs_shas = []
    for i, ln in enumerate(install_lines, start=1):
        m = re.search(r'^\s*PBS_TAG="([^"]+)"', ln)
        if m:
            pbs_tag = m.group(1)
        m = re.search(r'^\s*PBS_CPYTHON="([^"]+)"', ln)
        if m:
            pbs_cpython = m.group(1)
        # SHA256 pins live in the py312_asset() case arms as `sha="<64 hex>"`.
        m = re.search(r'sha="([0-9a-fA-F]{64})"', ln)
        if m:
            pbs_shas.append({"sha256": m.group(1), "line": i})
    pbs_url_line = _line_of(
        r"python-build-standalone/releases/download", install_lines, install_rel
    )
    artifacts.append(
        {
            "id": "python-build-standalone",
            "kind": "cpython-runtime-tarball",
            "source": "github.com/astral-sh/python-build-standalone",
            "pins": {
                "tag": pbs_tag,
                "cpython": pbs_cpython,
                "sha256": sorted(s["sha256"] for s in pbs_shas),
            },
            "provenance": [
                p
                for p in (
                    _line_of(r'PBS_TAG=', install_lines, install_rel),
                    _line_of(r'PBS_CPYTHON=', install_lines, install_rel),
                    pbs_url_line,
                )
                if p is not None
            ]
            + [
                {"file": install_rel, "line": s["line"], "text": "sha256 pin"}
                for s in pbs_shas
            ],
        }
    )

    # --- ORT nightly index feed -------------------------------------------- #
    ort_lines = _read_lines(SETUP_ML)
    ort_rel = _rel(SETUP_ML)
    ort_hits = _all_lines_of(
        r"aiinfra\.pkgs\.visualstudio\.com", ort_lines, ort_rel
    )
    artifacts.append(
        {
            "id": "ort-nightly",
            "kind": "pip-index-feed",
            "source": (
                "https://aiinfra.pkgs.visualstudio.com/PublicPackages/"
                "_packaging/ORT-Nightly/pypi/simple/"
            ),
            "pins": {"package": "ort-nightly", "min_version": ">=1.27"},
            "provenance": ort_hits,
        }
    )

    # --- Gemma model (HF) -------------------------------------------------- #
    gemma = scan_gemma_model_ids()
    artifacts.append(
        {
            "id": "gemma-model",
            "kind": "huggingface-model",
            "source": gemma["model_id"],  # canonical id (None if diverged)
            "pins": {
                "model_ids": gemma["model_ids"],   # per-script, for divergence
                "diverged": gemma["diverged"],
            },
            "provenance": gemma["provenance"],
        }
    )

    # --- Claude Code installer --------------------------------------------- #
    claude_hits = _all_lines_of(
        r"claude\.ai/install\.sh", install_lines, install_rel
    )
    artifacts.append(
        {
            "id": "claude-installer",
            "kind": "install-shell-script",
            "source": "https://claude.ai/install.sh",
            "pins": {},  # unpinned by design (support tool, non-fatal step)
            "provenance": claude_hits,
        }
    )

    return artifacts


def scan_gemma_model_ids() -> dict:
    """Collect the Gemma MODEL_ID from each of the three scripts and flag divergence.

    Returns:
      {
        "model_ids": {script_name: model_id_or_None, ...},
        "model_id": canonical id if all agree else None,
        "diverged": bool,
        "provenance": [ {file, line, text}, ... ],
      }
    """
    # Matches:  MODEL_ID = "…"   |   GEMMA_MODEL_ID = "…"
    rx = re.compile(r'(?:GEMMA_)?MODEL_ID\s*=\s*["\']([^"\']+)["\']')
    per_script: "dict[str, str | None]" = {}
    provenance: "list[dict]" = []
    for name, path in sorted(GEMMA_MODEL_SCRIPTS.items()):
        rel = _rel(path)
        model_id = None
        for i, ln in enumerate(_read_lines(path), start=1):
            m = rx.search(ln)
            if m:
                model_id = m.group(1)
                provenance.append(
                    {"file": rel, "line": i, "text": ln.strip()}
                )
                break  # first module-level assignment is the source of truth
        per_script[name] = model_id

    distinct = {v for v in per_script.values() if v is not None}
    diverged = len(distinct) > 1
    canonical = next(iter(distinct)) if len(distinct) == 1 else None
    return {
        "model_ids": per_script,
        "model_id": canonical,
        "diverged": diverged,
        "provenance": provenance,
    }


# --------------------------------------------------------------------------- #
# Category 3 — pip deps per venv
# --------------------------------------------------------------------------- #
def _parse_pip_list_var(path: Path, var_name: str) -> "list[str]":
    """Parse a `VAR = ["a", "b>=1,<2", ...]` list literal from a .py file.

    Static: reads the source, finds `VAR = [ ... ]` (single or multi-line) and
    extracts the quoted string items. No exec/import.
    """
    src = "\n".join(_read_lines(path))
    m = re.search(
        r"^\s*" + re.escape(var_name) + r"\s*=\s*\[(.*?)\]",
        src,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return []
    body = m.group(1)
    items = re.findall(r"""["']([^"']+)["']""", body)
    return items


def scan_pip_deps() -> dict:
    """Inventory pip deps per venv.

    * app venv — source is constraints.txt (exact pins), installed offline from
      vendor/wheels. We also capture the explicit top-level names pip is asked to
      install in install-app.sh (fastapi uvicorn …) for provenance.
    * ml / gemma / openai / ocr — the PIP_DEPS-style list vars in the setup scripts.
    """
    # App-venv top-level install targets (the args to `pip install … -c constraints`).
    install_lines = _read_lines(INSTALL_SH)
    install_rel = _rel(INSTALL_SH)
    app_targets: "list[str]" = []
    app_prov: "list[dict]" = []
    for i, ln in enumerate(install_lines, start=1):
        m = re.search(
            r"fastapi\s+uvicorn\s+pywebview\s+jinja2\s+pypdf\s+python-multipart",
            ln,
        )
        if m:
            app_targets = m.group(0).split()
            app_prov.append({"file": install_rel, "line": i, "text": ln.strip()})

    constraints = parse_constraints()

    ml_rel = _rel(SETUP_ML)
    ocr_rel = _rel(SETUP_OCR)

    def _prov_for_var(path: Path, rel: str, var: str) -> "list[dict]":
        rx = re.compile(r"^\s*" + re.escape(var) + r"\s*=")
        for i, ln in enumerate(_read_lines(path), start=1):
            if rx.search(ln):
                return [{"file": rel, "line": i, "text": ln.strip()}]
        return []

    return {
        "app_venv": {
            "venv": ".venv (app / launcher)",
            "python_abi": "cp39 (offline wheels)",
            "source_file": _rel(CONSTRAINTS_TXT),
            "install": "pip install --no-index --find-links=vendor/wheels/ -c constraints.txt",
            "top_level_targets": app_targets,
            "constraints": constraints,   # [{name, version, raw}, ...]
            "provenance": app_prov,
        },
        "ml_env": {
            "venv": "~/.bubble_shield/ml-env (GLiNER)",
            "python_abi": "python3.12",
            "deps": _parse_pip_list_var(SETUP_ML, "PIP_DEPS"),
            "provenance": _prov_for_var(SETUP_ML, ml_rel, "PIP_DEPS"),
        },
        "gemma_env": {
            "venv": "~/.bubble_shield/gemma-env (Gemma / mlx)",
            "python_abi": "python3.12",
            "deps": _parse_pip_list_var(SETUP_ML, "GEMMA_PIP_DEPS"),
            "provenance": _prov_for_var(SETUP_ML, ml_rel, "GEMMA_PIP_DEPS"),
        },
        "openai_extra": {
            "venv": "~/.bubble_shield/ml-env (OpenAI ORT path)",
            "python_abi": "python3.12",
            "deps": _parse_pip_list_var(SETUP_ML, "PIP_DEPS_OPENAI"),
            "provenance": _prov_for_var(SETUP_ML, ml_rel, "PIP_DEPS_OPENAI"),
        },
        "ocr_env": {
            "venv": "~/.bubble_shield/ocr-env (docling)",
            "python_abi": "python3.12",
            "deps": _parse_pip_list_var(SETUP_OCR, "PIP_DEPS"),
            "provenance": _prov_for_var(SETUP_OCR, ocr_rel, "PIP_DEPS"),
        },
    }


def parse_constraints() -> "list[dict]":
    """Parse constraints.txt into [{name, version, raw}] pins (skip comments/blanks)."""
    out = []
    for ln in _read_lines(CONSTRAINTS_TXT):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s#]+)", s)
        if m:
            out.append({"name": m.group(1), "version": m.group(2), "raw": s})
    return out


# --------------------------------------------------------------------------- #
# Category 4 — PATH mutations + python-version pins
# --------------------------------------------------------------------------- #
def scan_path_and_python() -> dict:
    """Collect `export PATH=` mutations in install-app.sh and every python3.N pin.

    python version pins are gathered across install-app.sh + the setup scripts so
    an ABI/version-assumption drift (e.g. someone bumps a setup to python3.13) is
    visible in the diff.
    """
    install_lines = _read_lines(INSTALL_SH)
    install_rel = _rel(INSTALL_SH)

    path_mutations = []
    for i, ln in enumerate(install_lines, start=1):
        if re.search(r'export\s+PATH=', ln):
            path_mutations.append(
                {"file": install_rel, "line": i, "text": ln.strip()}
            )

    # python3.N references across the relevant files.
    py_re = re.compile(r"python3\.(\d+)")
    version_files = [INSTALL_SH, SETUP_ML, SETUP_OCR]
    versions: "dict[str, list[dict]]" = {}
    for path in version_files:
        rel = _rel(path)
        for i, ln in enumerate(_read_lines(path), start=1):
            for m in py_re.finditer(ln):
                ver = "3." + m.group(1)
                versions.setdefault(ver, []).append(
                    {"file": rel, "line": i, "text": ln.strip()}
                )

    return {
        "path_mutations": path_mutations,
        "python_versions": {
            ver: versions[ver] for ver in sorted(versions)
        },
    }


# --------------------------------------------------------------------------- #
# Highest-value assertion — constraints-pin → vendored-wheel coverage
# --------------------------------------------------------------------------- #
def list_vendored_wheels() -> "list[dict]":
    """Parse vendor/wheels/*.whl filenames into {name, version, abi, filename}.

    Wheel filename format (PEP 427):
      {distribution}-{version}(-{build})?-{python}-{abi}-{platform}.whl
    distribution uses '_' for '-'. We keep the normalised name for matching and
    the ABI tag (e.g. cp39 / py3) so the report can show the ABI of compiled ones.
    """
    out = []
    if not WHEELS_DIR.is_dir():
        return out
    for whl in sorted(WHEELS_DIR.glob("*.whl")):
        parts = whl.name[: -len(".whl")].split("-")
        if len(parts) < 5:
            # Unexpected shape — record raw so it's visible, don't crash.
            out.append(
                {
                    "filename": whl.name,
                    "name": None,
                    "version": None,
                    "abi": None,
                }
            )
            continue
        # distribution-version-[build-]pytag-abitag-plat
        # version is parts[1]; abi tag is parts[-2]; python tag parts[-3].
        name = parts[0]
        version = parts[1]
        abi = parts[-2]
        out.append(
            {
                "filename": whl.name,
                "name": _norm_dist(name),
                "raw_name": name,
                "version": version,
                "abi": abi,
            }
        )
    return out


def check_wheel_coverage() -> dict:
    """For every constraints.txt pin, verify a matching vendored wheel exists.

    Match on normalised name AND exact version. Returns per-pin match status and
    an overall boolean. A missing wheel is the "offline install fails on a
    client with no PyPI" bug — the single most valuable thing this tool catches.
    """
    pins = parse_constraints()
    wheels = list_vendored_wheels()

    # Index wheels by (normalised-name, version).
    index: "dict[tuple, list[dict]]" = {}
    for w in wheels:
        if w.get("name") is None:
            continue
        index.setdefault((w["name"], w["version"]), []).append(w)

    results = []
    missing = []
    for pin in pins:
        key = (_norm_dist(pin["name"]), pin["version"])
        matches = index.get(key, [])
        ok = bool(matches)
        results.append(
            {
                "name": pin["name"],
                "version": pin["version"],
                "matched": ok,
                "wheel": matches[0]["filename"] if matches else None,
                "abi": matches[0]["abi"] if matches else None,
            }
        )
        if not ok:
            missing.append(f"{pin['name']}=={pin['version']}")

    return {
        "all_covered": not missing,
        "pin_count": len(pins),
        "wheel_count": len(wheels),
        "missing": missing,
        "pins": results,
    }


# --------------------------------------------------------------------------- #
# Manifest assembly
# --------------------------------------------------------------------------- #
MANIFEST_VERSION = 1


def build_manifest() -> dict:
    """Assemble the full 4-category manifest + the wheel-coverage assertion."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "generator": "bubble_shield_doctor.py",
        "categories": {
            "must_preexist_binaries": scan_must_preexist_binaries(),
            "fetched_artifacts": scan_fetched_artifacts(),
            "pip_deps": scan_pip_deps(),
            "path_and_python": scan_path_and_python(),
        },
        "wheel_coverage": check_wheel_coverage(),
    }


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render_report(manifest: dict) -> str:
    cats = manifest["categories"]
    lines: "list[str]" = []
    add = lines.append

    add("=" * 72)
    add("Bubble Shield — STATIC dependency inventory (Layer 1)")
    add("=" * 72)

    # 1. binaries
    bins = cats["must_preexist_binaries"]
    add("")
    add(f"[1] MUST-PREEXIST BINARIES  ({len(bins)})")
    add("    (client Mac must already have these — Xcode CLT / macOS base)")
    for b in bins:
        via = ", ".join(b["detected_via"])
        prov = b["provenance"][0]
        add(f"    - {b['binary']:<13} via {via:<28} {prov['file']}:{prov['line']}")

    # 2. fetched artifacts
    arts = cats["fetched_artifacts"]
    add("")
    add(f"[2] FETCHED-NOT-BUNDLED ARTIFACTS  ({len(arts)})")
    for a in arts:
        add(f"    - {a['id']}  ({a['kind']})")
        add(f"        source: {a['source']}")
        pins = a.get("pins") or {}
        for k, v in pins.items():
            add(f"        {k}: {v}")
        for p in a["provenance"][:3]:
            add(f"        @ {p['file']}:{p['line']}")

    # 3. pip deps
    pip = cats["pip_deps"]
    add("")
    add("[3] PIP DEPS PER VENV")
    app = pip["app_venv"]
    add(f"    - app_venv ({app['python_abi']}) — source {app['source_file']}")
    add(f"        {len(app['constraints'])} pinned constraints; install: {app['install']}")
    for key in ("ml_env", "gemma_env", "openai_extra", "ocr_env"):
        env = pip[key]
        deps = ", ".join(env["deps"]) if env["deps"] else "(none parsed)"
        prov = env["provenance"][0] if env["provenance"] else None
        loc = f"  @ {prov['file']}:{prov['line']}" if prov else ""
        add(f"    - {key} ({env['python_abi']}): {deps}{loc}")

    # 4. path + python
    pp = cats["path_and_python"]
    add("")
    add("[4] PATH MUTATIONS + PYTHON VERSION PINS")
    add(f"    export PATH= mutations: {len(pp['path_mutations'])}")
    for p in pp["path_mutations"]:
        add(f"        @ {p['file']}:{p['line']}  {p['text']}")
    add(f"    python versions referenced: {', '.join(pp['python_versions'].keys())}")
    for ver, hits in pp["python_versions"].items():
        add(f"        {ver}: {len(hits)} reference(s)")

    # Gemma divergence callout
    gemma = next((a for a in arts if a["id"] == "gemma-model"), None)
    if gemma:
        add("")
        if gemma["pins"]["diverged"]:
            add("    !! MODEL_ID DIVERGENCE DETECTED across scripts:")
            for s, mid in gemma["pins"]["model_ids"].items():
                add(f"       {s}: {mid}")
        else:
            add(f"    Gemma MODEL_ID consistent across 3 scripts: {gemma['source']}")

    # Wheel coverage — the headline assertion
    wc = manifest["wheel_coverage"]
    add("")
    add("=" * 72)
    add("WHEEL COVERAGE ASSERTION (offline app-venv install)")
    add("-" * 72)
    add(
        f"    constraints pins: {wc['pin_count']}   "
        f"vendored wheels: {wc['wheel_count']}"
    )
    if wc["all_covered"]:
        add("    RESULT: OK — every constraints.txt pin has a matching wheel.")
    else:
        add("    RESULT: FAIL — pins with NO vendored wheel (offline install WILL break):")
        for m in wc["missing"]:
            add(f"        MISSING: {m}")
    add("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# #385 — skill-description length gate (Cowork enforces 1024 chars; `claude plugin
# validate` does NOT, so a too-long description passes validation but is REJECTED at
# install on a client Mac). Check it at release time.
_SKILL_DESC_MAX = 1024


def scan_version_consistency() -> "list[str]":
    """#682 — the 3 version fields (marketplace.json metadata + plugins[0],
    plugin.json, mcpb/manifest.json) AND the version INSIDE the packed .mcpb must
    all agree. A silently-aborted version-bump (the v1.24.3 heredoc-guard-block
    case) left the fields stale → the client `plugin update` no-ops (#35752). This
    catches that at release time. Empty == all consistent."""
    import json as _json
    import zipfile as _zip
    problems: "list[str]" = []
    root = REPO_ROOT
    versions = {}
    try:
        mk = _json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
        versions["marketplace.metadata"] = mk.get("metadata", {}).get("version")
        plugins = mk.get("plugins", [])
        if plugins:
            versions["marketplace.plugins[0]"] = plugins[0].get("version")
    except Exception as e:
        problems.append(f"could not read marketplace.json version: {e}")
    try:
        pj = _json.loads((root / "plugin" / "bubble-shield" / ".claude-plugin"
                          / "plugin.json").read_text())
        versions["plugin.json"] = pj.get("version")
    except Exception as e:
        problems.append(f"could not read plugin.json version: {e}")
    try:
        mf = _json.loads((root / "plugin" / "bubble-shield" / "mcpb"
                          / "manifest.json").read_text())
        versions["mcpb.manifest"] = mf.get("version")
    except Exception as e:
        problems.append(f"could not read mcpb/manifest.json version: {e}")
    mcpb = root / "plugin" / "bubble-shield" / "mcpb" / "bubble-shield.mcpb"
    if mcpb.is_file():
        try:
            with _zip.ZipFile(mcpb) as z:
                versions["packed.mcpb"] = _json.loads(
                    z.read("manifest.json").decode("utf-8")).get("version")
        except Exception as e:
            problems.append(f"could not read version inside the packed .mcpb: {e}")
    distinct = {v for v in versions.values() if v is not None}
    if len(distinct) > 1:
        detail = ", ".join(f"{k}={v}" for k, v in versions.items())
        problems.append(f"VERSION MISMATCH across manifests/.mcpb ({detail}) — a "
                        "bump step was skipped; the client update would no-op")
    return problems


def scan_skill_description_lengths() -> "list[str]":
    """Return release-blocking messages for any SKILL.md whose YAML `description`
    exceeds the Cowork 1024-char limit. Empty == all within limit."""
    import re as _re
    problems: "list[str]" = []
    skills_dir = REPO_ROOT / "plugin" / "bubble-shield" / "skills"
    if not skills_dir.is_dir():
        return problems
    for sk in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            text = sk.read_text(encoding="utf-8")
        except Exception:
            continue
        # description: may be a single line or a YAML folded/quoted block; take the
        # value up to the next top-level key. Conservative: single-line form (what
        # our skills use). Fall back to the raw line length.
        m = _re.search(r"(?m)^description:[ \t]*(.+)$", text)
        if not m:
            continue
        desc = m.group(1).strip().strip('"\'')
        if len(desc) > _SKILL_DESC_MAX:
            problems.append(
                f"skill '{sk.parent.name}' description is {len(desc)} chars "
                f"(> {_SKILL_DESC_MAX} — Cowork will REJECT the install; trim it)")
    return problems


# Drift diff (--check)
# --------------------------------------------------------------------------- #
def _binary_names(manifest: dict) -> set:
    return {b["binary"] for b in manifest["categories"]["must_preexist_binaries"]}


def _artifact_ids(manifest: dict) -> set:
    return {a["id"] for a in manifest["categories"]["fetched_artifacts"]}


def compute_drift(baseline: dict, current: dict) -> "list[str]":
    """Return a list of human-readable drift messages (empty == no drift)."""
    problems: "list[str]" = []

    # NEW assumed binaries (added since baseline) — and removed ones too.
    base_bins = _binary_names(baseline)
    cur_bins = _binary_names(current)
    for b in sorted(cur_bins - base_bins):
        problems.append(f"NEW assumed binary: '{b}' (not in baseline)")
    for b in sorted(base_bins - cur_bins):
        problems.append(f"REMOVED assumed binary: '{b}' (was in baseline)")

    # NEW / removed fetched artifacts.
    base_arts = _artifact_ids(baseline)
    cur_arts = _artifact_ids(current)
    for a in sorted(cur_arts - base_arts):
        problems.append(f"NEW fetched artifact: '{a}' (not in baseline)")
    for a in sorted(base_arts - cur_arts):
        problems.append(f"REMOVED fetched artifact: '{a}' (was in baseline)")

    # Artifact pin drift (tag / sha / model id changed).
    base_art_map = {
        a["id"]: a for a in baseline["categories"]["fetched_artifacts"]
    }
    for a in current["categories"]["fetched_artifacts"]:
        b = base_art_map.get(a["id"])
        if b is None:
            continue
        if a.get("pins") != b.get("pins"):
            problems.append(
                f"PIN DRIFT for artifact '{a['id']}': "
                f"baseline pins {b.get('pins')} != current {a.get('pins')}"
            )
        if a.get("source") != b.get("source"):
            problems.append(
                f"SOURCE DRIFT for artifact '{a['id']}': "
                f"baseline '{b.get('source')}' != current '{a.get('source')}'"
            )

    # MODEL_ID divergence in the CURRENT tree is always a hard failure.
    gemma = next(
        (
            a
            for a in current["categories"]["fetched_artifacts"]
            if a["id"] == "gemma-model"
        ),
        None,
    )
    if gemma and gemma["pins"].get("diverged"):
        problems.append(
            "MODEL_ID divergence: Gemma model id differs across scripts "
            f"-> {gemma['pins']['model_ids']}"
        )

    # Wheel coverage: any missing wheel is a hard failure regardless of baseline.
    wc = current["wheel_coverage"]
    if not wc["all_covered"]:
        for m in wc["missing"]:
            problems.append(
                f"vendored-wheel MISSING for constraints pin: {m} "
                "(offline app-venv install will fail on a no-PyPI client)"
            )

    # Also flag if the SET of constraints pins changed vs baseline (new/removed
    # dep) — a new pin without a wheel is caught above, but a new pin WITH a wheel
    # is still a dependency-surface change worth surfacing at release time.
    base_pins = {
        (p["name"], p["version"])
        for p in baseline["wheel_coverage"]["pins"]
    }
    cur_pins = {(p["name"], p["version"]) for p in wc["pins"]}
    for name, ver in sorted(cur_pins - base_pins):
        problems.append(f"NEW constraints pin: {name}=={ver} (not in baseline)")
    for name, ver in sorted(base_pins - cur_pins):
        problems.append(f"REMOVED constraints pin: {name}=={ver} (was in baseline)")

    # pip-deps set drift per venv (ml/gemma/openai/ocr).
    for env_key in ("ml_env", "gemma_env", "openai_extra", "ocr_env"):
        b_deps = set(baseline["categories"]["pip_deps"].get(env_key, {}).get("deps", []))
        c_deps = set(current["categories"]["pip_deps"].get(env_key, {}).get("deps", []))
        for d in sorted(c_deps - b_deps):
            problems.append(f"NEW pip dep in {env_key}: '{d}' (not in baseline)")
        for d in sorted(b_deps - c_deps):
            problems.append(f"REMOVED pip dep in {env_key}: '{d}' (was in baseline)")

    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _dumps(obj: dict) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=_json_default)


def _json_default(o):
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="bubble_shield_doctor.py",
        description="Static dependency-inventory verifier for the Bubble Shield installer (Layer 1).",
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="emit machine-readable manifest")
    g.add_argument("--report", action="store_true", help="human-readable report (default)")
    g.add_argument(
        "--check",
        action="store_true",
        help="diff current tree vs committed baseline; exit 1 on any drift",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="(re)write the committed DEPENDENCY-MANIFEST.json from the current tree",
    )
    args = parser.parse_args(argv)

    manifest = build_manifest()

    if args.write_baseline:
        BASELINE_PATH.write_text(_dumps(manifest) + "\n", encoding="utf-8")
        print(f"Wrote baseline: {_rel(BASELINE_PATH)}")
        return 0

    if args.json:
        print(_dumps(manifest))
        return 0

    if args.check:
        if not BASELINE_PATH.is_file():
            print(
                f"ERROR: baseline manifest not found at {BASELINE_PATH}.\n"
                "Generate it once with: bubble_shield_doctor.py --write-baseline",
                file=sys.stderr,
            )
            return 1
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        problems = compute_drift(baseline, manifest)
        if problems:
            print("DEPENDENCY DRIFT DETECTED (release-blocking):", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            print(
                "\nIf this change is intentional, review it, then refresh the "
                "baseline with:\n  python3 "
                + _rel(BASELINE_PATH.parent / "scripts" / "bubble_shield_doctor.py")
                + " --write-baseline",
                file=sys.stderr,
            )
            return 1
        # #385 — skill-description length gate (Cowork rejects > 1024; plugin
        # validate does not catch it). Release-blocking.
        desc_problems = scan_skill_description_lengths()
        if desc_problems:
            print("SKILL DESCRIPTION TOO LONG (release-blocking):", file=sys.stderr)
            for p in desc_problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        # #682 — version-consistency gate (a skipped bump silently no-ops the
        # client update). Release-blocking.
        ver_problems = scan_version_consistency()
        if ver_problems:
            print("VERSION CONSISTENCY (release-blocking):", file=sys.stderr)
            for p in ver_problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("OK — dependency manifest matches the committed baseline (no drift); "
              "skill descriptions within 1024 chars; version consistent across "
              "manifests + .mcpb.")
        return 0

    # default: --report
    print(render_report(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
