# Releasing a new bubble-shield version

Clients' `claude plugin update` compares the **declared version**, not the git
commit. If the version doesn't change, the update is a silent no-op even though
the code changed (Claude Code issue #35752). So **every shipped change must bump
the version.**

## Self-contained / vendored deps

The plugin is **self-contained** — it bundles `vendor/bubble_shield` (the engine) and
`vendor/pypdf` so it runs with no `pip install`. **If you changed the engine**
(anything under the repo-root `bubble_shield/`), re-vendor before releasing:

```bash
# from the bubble_shield repo root
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='deployment_allowlist.json' \
  bubble_shield/ plugin/bubble-shield/vendor/bubble_shield/
```

Never vendor `deployment_allowlist.json` (firm identity) — only the `.example`.
`.docx` uses stdlib (no python-docx); pypdf is pure-python and already vendored.

## The MCP server ships as an MCPB (NOT a stdio `.mcp.json`)

Plugins may **not** declare a bare local/stdio MCP server — the app rejects it
("Plugins may only declare remote (http/sse/ws) or MCPB servers"). So the stdio
server is packaged as an **MCPB** at `mcpb/bubble-shield.mcpb` and declared in
`plugin.json` via `"mcpServers": "./mcpb/bubble-shield.mcpb"` (the path MUST start
with `./` and end in `.mcpb` — that is the schema). The `.mcpb` is a **built
artifact** committed to the repo; the host extracts it and runs
`mcpb/server/entry.py`.

**The MCPB bundles a COPY of `scripts/` + `vendor/`** under `mcpb/server/`. If you
change anything under `scripts/` or `vendor/` (including a re-vendor of the engine
above), you MUST re-sync those copies and re-pack, or the shipped server runs stale
code:

```bash
# from plugin/bubble-shield/
# NOTE: rsync --delete will NOT remove a destination file that matches an
# --exclude pattern, so if a test/pyc file ever lands in mcpb/server/, delete it
# explicitly (rm) — the exclude only stops re-copying, not re-removing. The test
# globs cover test_*.py, _test_*.py and *_test.py (e.g. _test_mock_daemon.py).
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='test_*.py' --exclude='_test_*.py' --exclude='*_test.py' \
  scripts/ mcpb/server/scripts/
rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='deployment_allowlist.json' \
  vendor/ mcpb/server/vendor/
# belt-and-suspenders: purge any pycache/test that an exclude couldn't delete
find mcpb/server -name __pycache__ -type d -prune -exec rm -rf {} +
find mcpb/server \( -name '*.pyc' -o -name 'test_*.py' -o -name '_test_*.py' -o -name '*_test.py' \) -delete
# keep mcpb/manifest.json "version" in sync with plugin.json, then pack:
npx --yes @anthropic-ai/mcpb validate mcpb/manifest.json
npx --yes @anthropic-ai/mcpb pack mcpb/ mcpb/bubble-shield.mcpb
```

Only the pure-python engine + `pypdf` go in the bundle. Do NOT bundle the ML pack
(GLiNER/onnxruntime/numpy) — it stays a lazy on-demand `bubble_shield_setup_ml`
download into a separate runtime venv.

## Checklist

1. **Bump the version in BOTH manifests** (keep them in sync):
   - `plugin/bubble-shield/.claude-plugin/plugin.json` → `version`
   - `.claude-plugin/marketplace.json` → `metadata.version` AND `plugins[0].version`
   (semver: patch = fix, minor = feature, major = breaking)
2. Add a `CHANGELOG.md` entry for the new version.
3. Run the tests: `python3 scripts/test_guard.py && python3 scripts/test_guard_marker.py && python3 scripts/test_tripwire.py` and `claude plugin validate .` from the repo root.
3b. **Regenerate the dated #572 recall/precision report** (the RGPD art. 32(1)(d)
    effectiveness-evidence artifact — see `bench/docs/572-recall-benchmark-corpus-design.md`).
    This is an **on-demand run, not a scheduled/CI job** — the daemon-up half needs the
    2.7GB ML pack, which is not assumed to be available in CI; a human runs it manually
    at each release:
    ```bash
    # from the repo root, with the ML-pack venv available
    ~/.bubble_shield/ml-env/bin/python \
      plugin/bubble-shield/scripts/bubble_shield_nerd.py --port 8723 &
    # wait for /health -> self_test: pass (~20-45s cold)

    ~/.bubble_shield/ml-env/bin/python bench/run_daemon_up_bench.py \
      bench/fixtures/corpus_572.json \
      --json bench/out/daemon-up-recall-572-$(date +%F).json
    ~/.bubble_shield/ml-env/bin/python bench/run_daemon_up_bench.py \
      bench/fixtures/corpus_572.json --regex-only \
      --json bench/out/regex-only-recall-572-$(date +%F).json

    python3 bench/gen_report_572.py \
      --daemon-up bench/out/daemon-up-recall-572-$(date +%F).json \
      --regex-only bench/out/regex-only-recall-572-$(date +%F).json \
      --corpus bench/fixtures/corpus_572.json
    ```
    Commit the new dated `bench/out/REPORT-572-<date>.{json,md}` alongside the release.
    If the daemon-up run could not be completed (ML pack unavailable), the report
    script says so explicitly on page 1 — do NOT hand out a report silently missing
    the daemon-up column; re-run once the ML pack is available instead.
    (Why not a scheduled job: the fail-closed + `residual_when_safe` flags already
    catch leaks live on real client docs, red-team catches leak classes, and the KPI
    dashboard (#567) surfaces the headline number to a human before any hand-off — a
    cron adds the heavy ML dependency + a silent-break risk for a regression class
    already covered by faster live signals. This benchmark's unique job is producing
    the dated, versioned regulator artifact, not continuous monitoring.)
4. Commit + push to `vdk888/bubble-shield` (private dev repo).
4b. **Publish to the PUBLIC distribution repo `vdk888/bubble-shield-public`** (this
    is where clients install from — no GitHub account needed because it's public).
    Sync the self-contained plugin + root marketplace.json into the public repo
    (exclude tests/__pycache__/.git and the real `deployment_allowlist.json` —
    only the `.example` ships), run the 5-point PII/secret scan, commit + push.
    Clients install with: `vdk888/bubble-shield-public`.
5. **Verify a real client can get it:**
   - CLI: `/plugin marketplace update bubble-shield` then
     `claude plugin update bubble-shield@bubble-shield` → should report the NEW version (not "already at latest").
   - Cowork: Customize → Plugins → the marketplace → **Update** (or enable
     **Sync automatically** so it re-syncs on each merge). Then `/reload-plugins`.

## Why uninstall+reinstall is NOT the answer

It works in a pinch but clients won't do it. The version bump is the supported
path that makes `update` actually pull the new code.
