#!/usr/bin/env python3
"""dogfood_prepublish_scan.py — #396: run the Bubble Shield ENGINE over the publish
payload as a pre-publish PII scan, flagging PLAUSIBLE-REAL PII the grep denylist
can't (unknown names/IBANs/addresses left in code/comments/tests).

Complements (does NOT replace) the grep + #317 hashed denylist — defense in depth.
A REVIEW gate: it flags for human y/n, it does not auto-block.

HIGH-PRECISION GATING (avoid drowning in synthetic-fixture / code noise):
  - IBAN: only CHECKSUM-VALID (mod-97) spans (a real IBAN, not a random FR-number).
  - EMAIL: only spans whose domain is NOT an obvious test/example domain.
  - SECU / SIREN / SIRET: only checksum-valid.
  - Everything else (NOM, ADRESSE, generic numbers): NOT flagged — too noisy in a
    codebase full of synthetic fixtures + variable names. The denylist covers known
    names; this scan's job is the UNKNOWN structured-PII a worker pasted by accident.
  - Known-synthetic allowlist (example.com/test/synthetic fixtures) is skipped.

Usage:  python3 tools/pii-guard/dogfood_prepublish_scan.py [--root <tree>]
Exit 0 = nothing flagged; exit 3 = findings to review (never a hard block — the
caller decides). Output NEVER prints a raw value — only redacted refs.
"""
from __future__ import annotations
import argparse
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
REPO = _HERE.parents[2]
sys.path.insert(0, str(REPO / "plugin" / "bubble-shield" / "vendor"))

# obvious non-real domains → skip EMAIL findings on these
_TEST_DOMAINS = re.compile(
    r"@([\w-]+\.)*(example\.(com|org|fr|test)|test|localhost|invalid|"
    r"courriel\.example|exemple\.(fr|com)|domain\.tld|email\.com)$", re.I)
# text-ish files worth scanning (skip binaries / the mcpb / images / the denylist)
_TEXT_EXT = {".py", ".md", ".json", ".txt", ".js", ".ts", ".html", ".sh",
             ".yaml", ".yml", ".toml", ".cfg"}
# tests/ + bench/ + fixtures are 100% SYNTHETIC by design (Jean Dupont, valid-but-
# fake IBANs constructed by the corpus generators) — scanning them is pure noise.
# The real risk is a worker pasting a REAL client value into SHIPPED code/comments,
# so we scan the shipped tree and skip the synthetic zones.
_SKIP_PARTS = {".git", "node_modules", "__pycache__", "vendor", "mcpb",
               "tests", "bench", "fixtures", "test", "docs"}


def _tracked_text_files(root: Path) -> "list[Path]":
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True, timeout=30).stdout
        files = [root / line for line in out.splitlines() if line.strip()]
    except Exception:
        files = list(root.rglob("*"))
    res = []
    for p in files:
        if not p.is_file() or p.suffix.lower() not in _TEXT_EXT:
            continue
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        if p.name == "denylist.sha256":
            continue
        res.append(p)
    return res


def _redact(entity_type: str, value: str) -> str:
    v = (value or "").strip()
    return f"{entity_type} len{len(v)} {v[:1]}•"


def scan(root: Path) -> "list[dict]":
    from bubble_shield.recognizers import detect, _iban_valid  # engine's own core
    # optional checksum validators (present in the engine)
    try:
        from bubble_shield.recognizers import _siren_valid  # noqa
    except Exception:
        _siren_valid = None  # type: ignore

    findings = []
    for p in _tracked_text_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in detect(text):
            t, v = m.entity_type, m.value
            # HIGH-PRECISION gate — only flag structured PII we can validate as real.
            if t == "IBAN":
                if not _iban_valid(v):
                    continue
            elif t == "EMAIL":
                if _TEST_DOMAINS.search(v):
                    continue
            elif t in ("SIREN", "SIRET", "SECU"):
                # keep only if the recognizer scored it as validated (score 1.0)
                if getattr(m, "score", 0) < 1.0:
                    continue
            else:
                continue  # names/addresses/etc → too noisy in a codebase; skip
            findings.append({"file": str(p.relative_to(root)),
                             "ref": _redact(t, v)})
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(REPO))
    args = ap.parse_args()
    root = Path(args.root).resolve()
    findings = scan(root)
    if not findings:
        print("✅ dogfood scan: no plausible-real structured PII in the publish "
              "payload (engine regex core + checksum gates).")
        return 0
    print(f"⚠️  dogfood scan flagged {len(findings)} plausible-real PII span(s) "
          "for REVIEW (redacted refs — verify each is synthetic before publishing):",
          file=sys.stderr)
    for f in findings:
        print(f"  {f['file']}: {f['ref']}", file=sys.stderr)
    print("\nThis is a REVIEW gate, not an auto-block. If all are synthetic/"
          "intended, proceed with the publish.", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
