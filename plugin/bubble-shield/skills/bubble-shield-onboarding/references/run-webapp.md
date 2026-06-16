# Bubble Shield — running the local web app

Read this when the user wants the visual tool (before/after view, the
risk-control dashboard, the masquer/conserver settings).

## What it is (say this)

A small app that runs **only on the advisor's machine**. It binds to
`127.0.0.1` (localhost), makes **no outbound network calls**, and the clear text
never leaves the process. Closing the terminal stops it. Nothing is published
online.

## Launch

The webapp is part of the Bubble Shield project (the engine repo), not bundled inside
the installed plugin — it needs the `bubble_shield` Python package and a couple of libs.
If the user has the Bubble Shield project folder:

```bash
cd <bubble-shield-project>            # the folder containing webapp/ and bubble_shield/
python3 -m venv .venv && . .venv/bin/activate   # first time only
pip install -r requirements.txt                  # first time only
python3 -m uvicorn webapp.app:app --host 127.0.0.1 --port 8765
```

Then open **http://127.0.0.1:8765** in a browser.

Offer to run the launch command for the user and hand them the link, rather than
making them type it.

## The tabs

- **Accueil** — paste text, or drop a `.pdf` / `.docx` / `.txt`, and see exactly
  what gets anonymised, side by side (before / after). Good for building trust.
  Use the built-in fictional sample for demos — never paste real client data.
- **Comment ça marche** — the plain-language explanation page.
- **Contrôle & réglages** (`/dashboard`) — the risk-control dashboard:
  - headline stats: how many anonymisations, how many flagged "à relire", errors,
    % "sûr à envoyer", total items hidden;
  - the types of data encountered;
  - the last runs with their verdict;
  - the **masquer / conserver** table (the per-type cloak/keep policy) — editable,
    saved to disk, applied to the next anonymisation.

## Troubleshooting

- **Port already in use** → pick another port (`--port 8766`) and use that URL.
- **`ModuleNotFoundError`** → the venv isn't active or deps aren't installed;
  re-run the venv + `pip install` step.
- **Page won't load** → confirm the `uvicorn` process is still running in the
  terminal; it stops when the terminal closes.
- **The dashboard is empty** → no anonymisations have been logged yet; run one
  from Accueil (or via the assistant) and refresh.

## Privacy notes to reassure the user

- The audit log behind the dashboard stores **counts and types only — never the
  real values** (it's a processing record, RGPD art. 30, not a copy of the data).
- The masquer/conserver settings file holds only entity-type names + on/off — no
  client data.
