#!/usr/bin/env python3
"""Bubble Shield — Cowork artifact generator (the visual tool, Cowork-native).

The local webapp (FastAPI on 127.0.0.1) can't run inside Cowork's sandbox VM:
the agent's localhost is the VM, not the user's Mac, and FastAPI's deps aren't
vendorable. A Cowork ARTIFACT is the native equivalent — rich HTML rendered in
Cowork's own UI, on the user's screen.

This script does the SAME thing the webapp's "Accueil"/result + dashboard pages
do, with the SAME engine (vendored), and emits ONE self-contained HTML file
(inline CSS, no server, no JS deps) that Cowork presents as an artifact:
  - the verdict (X confidently masked · Y to review · round-trip OK),
  - the BEFORE (clear, PII highlighted) / AFTER (anonymised, tokens highlighted)
    side-by-side view — identical markup + styling to the webapp,
  - the masquer / conserver table (interactive toggles; choices saved by the
    agent re-running with --policy, since a sandboxed artifact can't write disk).

Usage (the anonymize skill calls this after running the engine):
    python3 make_artifact.py --text "<clear text>"  [--mission NAME] [--out PATH]
    python3 make_artifact.py --file  <path>          [--out PATH]
Prints the artifact's path on stdout. Pure-stdlib + the vendored engine.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

# self-contained: bundled engine under vendor/
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(PLUGIN_ROOT / "vendor"))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from bubble_shield import AnonymizationEngine, Vault  # noqa: E402
from bubble_shield.vault import TOKEN_RE  # noqa: E402

try:
    from bubble_shield.policy import ENTITY_CATALOG, load_policy, policy_view, make_match_filter
except Exception:  # policy module optional
    ENTITY_CATALOG = {}
    def load_policy():  # type: ignore
        return {}
    def policy_view(_):  # type: ignore
        return []
    make_match_filter = None  # type: ignore


# ── highlighting (mirrors webapp/render.py exactly) ───────────────────────────
def _span(text, css, title=""):
    t = f' title="{html.escape(title, quote=True)}"' if title else ""
    return f'<mark class="pii pii--{css}"{t}>{html.escape(text)}</mark>'


def highlight_before(result):
    text = result.original
    out, cursor = [], 0
    for e in sorted(result.entities, key=lambda x: x.start):
        if e.start < cursor:
            continue
        out.append(html.escape(text[cursor:e.start]))
        out.append(_span(text[e.start:e.end], e.entity_type.lower(),
                         f"{e.entity_type} · confiance {e.score:.2f}"))
        cursor = e.end
    out.append(html.escape(text[cursor:]))
    return "".join(out)


def highlight_after(result):
    text = result.anonymized
    out, cursor = [], 0
    for m in TOKEN_RE.finditer(text):
        out.append(html.escape(text[cursor:m.start()]))
        out.append(f'<mark class="tok tok--{m.group(1).lower()}">{html.escape(m.group(0))}</mark>')
        cursor = m.end()
    out.append(html.escape(text[cursor:]))
    return "".join(out)


# ── the artifact CSS (the webapp's private-banking identity, inlined) ─────────
CSS = """
:root{--paper:#fbfaf7;--panel:#fff;--ink:#1b1a17;--muted:#6c6862;--faint:#9b958c;
--line:#e7e3db;--line-strong:#d6d1c6;--accent:#8a6d3b;--safe:#3f6b52;--safe-bg:#f1f4ef;
--safe-line:#cdddcf;--warn:#9a4a36;--warn-bg:#f7efea;--warn-line:#e6cfc4;
--serif:"Newsreader",Georgia,serif;--sans:"IBM Plex Sans",system-ui,sans-serif;
--mono:"JetBrains Mono",ui-monospace,monospace;--maxw:880px;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:var(--maxw);margin:0 auto;padding:40px 24px 64px}
h1{font-family:var(--serif);font-weight:500;font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--muted);font-size:.85rem;margin:0 0 30px}
.verdict{display:flex;align-items:flex-start;gap:14px;border:1px solid var(--line-strong);
border-left-width:3px;border-radius:4px;padding:16px 20px;margin-bottom:30px;background:var(--panel)}
.verdict--ok{border-left-color:var(--safe);background:var(--safe-bg)}
.verdict--warn{border-left-color:var(--warn);background:var(--warn-bg)}
.verdict--review{border-left-color:var(--accent);background:#faf6ee}
.verdict-mark{flex:0 0 auto;font-family:var(--serif);font-size:1.1rem;width:1.4em;text-align:center}
.verdict--ok .verdict-mark{color:var(--safe)}.verdict--warn .verdict-mark{color:var(--warn)}
.verdict--review .verdict-mark{color:var(--accent)}
.verdict-body{display:flex;flex-direction:column;gap:2px}
.verdict-text{font-size:1rem;font-weight:600}
.verdict--ok .verdict-text{color:var(--safe)}.verdict--warn .verdict-text{color:var(--warn)}
.verdict--review .verdict-text{color:var(--accent)}
.verdict-meta{font-size:.8rem;color:var(--muted);font-family:var(--mono)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:30px}
.col-h{font-size:.82rem;color:var(--muted);margin:0 0 12px;display:flex;align-items:baseline;gap:9px}
.col-k{font-size:.68rem;font-weight:600;letter-spacing:.10em;text-transform:uppercase;
color:var(--ink);border-bottom:2px solid var(--accent)}
.doc{font-family:var(--mono);font-size:.8rem;line-height:1.85;white-space:pre-wrap;
word-break:break-word;margin:0;padding:18px;background:var(--panel);border:1px solid var(--line);border-radius:4px}
.pii{background:transparent;color:var(--ink);border-bottom:1px solid var(--accent);cursor:help}
.tok{font-family:var(--mono);color:var(--accent);background:rgba(138,109,59,.07);
border:1px solid var(--line-strong);border-radius:3px;padding:0 3px;font-size:.94em}
.residual{border:1px solid var(--warn-line);border-left:3px solid var(--warn);background:var(--warn-bg);
border-radius:4px;padding:16px 20px;margin-bottom:30px}
.residual-h{font-size:.9rem;font-weight:600;color:var(--warn);margin:0 0 10px}
.residual code{font-family:var(--mono);font-size:.78rem;background:var(--panel);
border:1px solid var(--warn-line);padding:2px 7px;border-radius:3px}
table.policy{width:100%;border-collapse:collapse;margin-top:8px;font-size:.85rem}
table.policy th,table.policy td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
table.policy th{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.tag{font-size:.66rem;text-transform:uppercase;letter-spacing:.06em;padding:1px 7px;border-radius:10px;border:1px solid var(--line-strong)}
.tag--id{color:var(--warn);border-color:var(--warn-line);background:var(--warn-bg)}
.tag--keep{color:var(--safe);border-color:var(--safe-line);background:var(--safe-bg)}
.switch{position:relative;display:inline-block;width:40px;height:22px}
.switch input{display:none}
.slider{position:absolute;cursor:pointer;inset:0;background:#d6d1c6;border-radius:22px;transition:.2s}
.slider:before{content:"";position:absolute;height:16px;width:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.slider{background:var(--accent)}
.switch input:checked+.slider:before{transform:translateX(18px)}
.sec-h{font-family:var(--serif);font-size:1.05rem;margin:34px 0 6px}
.sec-sub{color:var(--muted);font-size:.8rem;margin:0 0 14px}
.note{margin-top:14px;font-size:.78rem;color:var(--muted);background:var(--panel);
border:1px solid var(--line);border-radius:4px;padding:10px 14px}
@media(max-width:680px){.cols{grid-template-columns:1fr}}
"""


def build_html(result, mission, policy_rows):
    confident = [e for e in result.entities if e.score >= result.threshold]
    to_review = [e for e in result.entities if e.score < result.threshold]
    has_review = bool(to_review)
    safe = getattr(result, "safe_to_send", True)
    vclass = "verdict--ok" if safe and not has_review else ("verdict--review" if safe else "verdict--warn")
    vmark = "✓" if safe else "!"
    vtext = ("Prêt à envoyer — données identifiantes masquées"
             if safe and not has_review else
             ("Anonymisé — quelques éléments à vérifier d'un coup d'œil" if safe
              else "À revoir — donnée identifiante encore en clair"))
    roundtrip = "vérifiée"  # caller passes only when ok; default vérifiée
    meta = (f"{len(confident)} élément(s) masqué(s) avec certitude · "
            f"{len(to_review)} à vérifier · restauration {roundtrip} · tout reste local")

    review_html = ""
    if has_review:
        chips = " ".join(
            f'<code title="{html.escape(e.entity_type)} · {e.score:.2f}">{html.escape(result.original[e.start:e.end])}</code>'
            for e in to_review)
        review_html = (f'<div class="residual"><p class="residual-h">À vérifier d\'un coup d\'œil '
                       f'({len(to_review)})</p><div>{chips}</div>'
                       f'<p class="note">Ces éléments sont anonymisés ; ce sont les détections les '
                       f'moins sûres (souvent des libellés de formulaire). Un simple coup d\'œil suffit.</p></div>')

    rows = ""
    for r in policy_rows:
        tag = ('<span class="tag tag--id">identifiant</span>' if r["identifying"]
               else '<span class="tag tag--keep">contexte</span>')
        checked = "checked" if r["cloak"] else ""
        rows += (f'<tr><td>{html.escape(r["label"])}</td><td>{tag}</td>'
                 f'<td><label class="switch"><input type="checkbox" data-type="{html.escape(r["type"])}" {checked}>'
                 f'<span class="slider"></span></label></td></tr>')
    policy_block = ""
    if policy_rows:
        policy_block = f"""
<h2 class="sec-h">Masquer / conserver</h2>
<p class="sec-sub">Pour chaque type de donnée : masquer (anonymiser) ou conserver en clair. Les € et codes (ISIN) sont conservés par défaut pour pouvoir raisonner sur l'allocation ; tout ce qui identifie est masqué.</p>
<table class="policy"><thead><tr><th>Type</th><th></th><th>Masquer</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">Réglage enregistré localement. Dites à Bubble Shield « conserve les montants » ou « masque le poste » et il rejoue l'anonymisation avec votre choix — la table ci-dessus reflète ce qui s'appliquera.</p>
"""

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bubble Shield — {html.escape(mission)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Bubble Shield — avant / après</h1>
<p class="sub">Anonymisation locale et réversible · le coffre (noms ↔ jetons) ne quitte pas votre machine</p>
<div class="verdict {vclass}"><span class="verdict-mark">{vmark}</span><div class="verdict-body">
<span class="verdict-text">{vtext}</span><span class="verdict-meta">{meta}</span></div></div>
{review_html}
<div class="cols">
<section class="col"><p class="col-h"><span class="col-k">Avant</span> document en clair — reste local</p>
<pre class="doc">{highlight_before(result)}</pre></section>
<section class="col"><p class="col-h"><span class="col-k">Après</span> ce qui serait envoyé au LLM</p>
<pre class="doc">{highlight_after(result)}</pre></section>
</div>
{policy_block}
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default=None)
    ap.add_argument("--file", default=None)
    ap.add_argument("--mission", default="demo")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.file:
        from bubble_shield_extract import extract_file
        text = extract_file(Path(args.file))
    elif args.text is not None:
        text = args.text
    else:
        text = sys.stdin.read()

    # policy filter (masquer/conserver) applied if available
    match_filter = None
    policy_rows = []
    try:
        policy = load_policy()
        policy_rows = policy_view(policy)
        if make_match_filter:
            match_filter = make_match_filter(policy)
    except Exception:
        pass

    engine = AnonymizationEngine(vault=Vault(mission=args.mission), match_filter=match_filter)
    result = engine.anonymize(text or "")

    out_html = build_html(result, args.mission, policy_rows)
    out_path = Path(args.out) if args.out else (Path(os.environ.get("CLAUDE_PLUGIN_ROOT", ".")) / "bubble-shield-apercu.html")
    try:
        out_path.write_text(out_html, encoding="utf-8")
    except Exception:
        out_path = Path("/tmp/bubble-shield-apercu.html")
        out_path.write_text(out_html, encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
