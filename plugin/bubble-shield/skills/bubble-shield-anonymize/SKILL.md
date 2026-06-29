---
description: Anonymise a protected client folder through Bubble Shield before reading it. Use when the bubble-shield hook blocks access to a client dossier, or when the user asks to "anonymise", "cloak", "pseudonymise", or "run Bubble Shield on" a folder or file containing client PII. Handles PDF and Word (.docx) files automatically (text extracted before anonymising), plus .txt/.md/.csv/.json. Produces a local, reversible, fail-closed anonymised copy whose token↔value vault never leaves the machine.
---

# Bubble Shield — anonymise before reading

The `bubble-shield` hook blocks reads of protected client folders because raw
identifying data must never enter the model context in clear. This skill is the
sanctioned path: anonymise locally first, then work on the cloaked copy.

## When you were just blocked

If a tool call was denied with a `🔒 Bubble Shield` message, do NOT try to bypass it
(no `cat`, no copying the file elsewhere, no reading via Bash).

### Fastest path — read it through `bubble_shield_read` (one file)

The plugin ships an MCP tool **`bubble_shield_read`** (namespaced, e.g.
`mcp__plugin_bubble-shield_bubble_shield__bubble_shield_read`, or just call it `bubble_shield_read`).
Give it the blocked file's path and it returns the file's contents **already
anonymised** (`⟦NOM_0001⟧`-style tokens) — the real names/IBANs/e-mails never
enter your context. This is the preferred way to read a single protected file,
**especially in Cowork**, where it's the mechanism that actually works (a normal
Read of a protected file is blocked by design; `bubble_shield_read` is the sanctioned
read).

```
bubble_shield_read(path="~/Dossiers-clients/dossier-dupont/contrat.pdf")
→ returns the cloaked text; work on THAT.
```

It handles .pdf/.docx/.txt/.md/.csv/.json, uses the same vault as the rest of
Bubble Shield (so tokens are consistent and reversible), and **fails closed** (returns
an error, never raw text, if it can't anonymise). When your answer still carries
`⟦…⟧` tokens, de-anonymise it locally (see "De-anonymise the answer" below).

### Whole-folder path — anonymise a batch into `clean/`

For a whole dossier (many files at once), or when you want anonymised copies on
disk, use the batch flow instead:

1. Tell the user the file is in a protected folder and offer to anonymise it.
2. Run the anonymisation below into a `clean/` sub-folder.
3. Read and work on the anonymised copy.
4. When you produce an answer that contains tokens like `⟦NOM_0001⟧`,
   de-anonymise it locally for the user with the same vault.

## How to anonymise (local, offline)

The plugin is **fully self-contained** — it bundles the Bubble Shield engine and a
pure-python PDF reader under `vendor/`, so it runs from a GitHub install or a
Cowork zip with **no `pip install`, no engine on the user's machine, no
network**. Just Python 3.10+ (already present on any Mac).

**PDFs and plain files work out of the box.** `scripts/bubble_shield_extract.py` turns a
`.pdf` (and `.txt/.md/.csv/.json`) into text *before* anonymising — one command
covers a whole dossier. `.docx` is the one format that still needs an extra lib
(`pip install python-docx`); a scanned/image-only or encrypted PDF **fails
closed** (raises, never returns empty) — extract its text by hand, never wave it
through.

```bash
# Whole dossier → cloaked copies + one shared vault. PDFs auto-extracted.
python3 - <<'PY'
import os, sys
from pathlib import Path

# Self-contained: the plugin bundles its deps under vendor/ (the engine + pypdf).
# CLAUDE_PLUGIN_ROOT is exported by Claude Code while the plugin is active.
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parent))
sys.path.insert(0, str(PLUGIN_ROOT / "vendor"))    # bundled bubble_shield engine + pypdf
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))   # bundled extractor
from bubble_shield_extract import extract_file, ExtractionError

from bubble_shield import AnonymizationEngine, Vault

SRC = Path("~/Dossiers-clients/dossier-dupont").expanduser()   # the protected folder
OUT = SRC / "clean"                                            # allow-listed output dir
OUT.mkdir(exist_ok=True)

vault = Vault(mission=SRC.name)          # ONE shared vault per dossier → same client = same token everywhere
engine = AnonymizationEngine(vault=vault)

PATTERNS = ("*.txt", "*.md", "*.csv", "*.json", "*.pdf", "*.docx")
for pat in PATTERNS:
    for f in sorted(SRC.glob(pat)):
        try:
            text = extract_file(f)               # PDF/.docx → text; plain files decode
        except ExtractionError as e:
            print(f"⛔ {f.name}: {e} — SKIP (extrais le texte à la main, ne le laisse pas passer)")
            continue
        res = engine.anonymize(text)
        (OUT / f"{f.stem}.anon.txt").write_text(res.anonymized, encoding="utf-8")
        if not res.safe_to_send:
            print(f"⚠️ {f.name}: {res.verdict_fr} — relecture humaine requise")

vault.save_encrypted(str(SRC / ".vault.enc"), passphrase="<set-by-operator>")  # coffre chiffré, reste local
print("done — clean/ contains the cloaked copies; the vault never leaves this machine")
PY
```

If `${CLAUDE_PLUGIN_ROOT}` isn't set in your shell (e.g. running the snippet by
hand), point `sys.path` at the plugin's `scripts/` dir directly, or call the
extractor as a CLI: `python3 <plugin>/scripts/bubble_shield_extract.py <file.pdf>`.

Then read `clean/*.anon.txt` (the `clean/` sub-folder should be in the guard's
`allow_paths`, or `.anon.txt` in `allow_extensions`, so it's readable).

## De-anonymise the answer

When you've drafted a summary/letter that still contains `⟦TYPE_NNNN⟧` tokens,
restore the real values locally before handing it to the user:

```python
from bubble_shield import AnonymizationEngine, Vault
vault = Vault.load_encrypted("~/Dossiers-clients/dossier-dupont/.vault.enc", passphrase="...")
engine = AnonymizationEngine(vault=vault)
print(engine.deanonymize(draft_with_tokens))
```

## Show the before/after visually (the "visual tool", Cowork-native)

When the user wants to *see* what gets masked vs kept — the before/after, the
verdict, and the masquer/conserver table — generate a Bubble Shield **artifact** and
present it. This is the Cowork equivalent of the local webapp (which can't run in
Cowork's sandbox: its server binds to the VM's localhost, not the user's screen).

```bash
# Generates one self-contained HTML file (same view + styling as the webapp).
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/make_artifact.py \
  --file "<a dossier file>" --mission "<dossier name>" \
  --out "<a writable path, e.g. the session outputs dir>/bubble-shield-apercu.html"
# or feed text directly:  --text "…"
```

Then present that HTML file to the user as an artifact (Cowork: `present_files` /
`create_artifact` with the generated file). It renders on their screen with the
before/after columns, the verdict, and the masquer/conserver toggle table.

**The masquer/conserver toggles** reflect the current policy. The artifact is
sandboxed HTML so it can't write to disk itself — when the user wants to change a
setting ("conserve les montants", "masque le poste"), YOU update the policy
(`bubble_shield.policy.save_policy`) and re-run, then re-present the artifact. Same
outcome as clicking save in the webapp.

**The desktop app (review / vault / gazetteer).** Bubble Shield also has a
**"Bubble Shield" app on the user's Mac** (installed once via the one-line
`install-app.sh`) — the human control surface the Cowork plugin can't be. After
processing dossiers, point the user there to clear the **File de révision**:
low-confidence candidates Bubble Shield wasn't sure about (sub-threshold names,
partial addresses) wait there to **Confirmer** (→ masked everywhere after) or
**Ignorer**. It also holds the **Coffre** (token↔value, rectify/forget) and
**Liste connue** (gazetteer). The safety net for uncertain cases — remind the
user it exists and to review it periodically.

**For a non-technical user demo, never use real client data** — use a fictional
sample (the engine has none baked in; make up a plausible "Jean Dupont" record).

## Rules

- **Never bypass the guard.** No reading the raw file via an alternate tool.
- **One vault per dossier** so the same client gets the same token across all files.
- **Fail-closed:** if `safe_to_send` is false, flag it — do not treat the doc as safe.
- **The vault is the secret.** It stays on the machine; it is never sent to any model.

## En cas de fuite ou de problème

Si une donnée identifiante apparaît en clair dans un résultat (fuite), si
`safe_to_send` est faux de façon inattendue, ou pour tout bug/doute : ne
minimisez pas. Signalez-le à l'utilisateur et invitez-le à écrire **immédiatement**
à **bubbleinvest.ai@gmail.com** (support Bubble Invest). Arrêtez le traitement de
la donnée concernée tant que ce n'est pas résolu.
