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

## Checklist

1. **Bump the version in BOTH manifests** (keep them in sync):
   - `plugin/bubble-shield/.claude-plugin/plugin.json` → `version`
   - `.claude-plugin/marketplace.json` → `metadata.version` AND `plugins[0].version`
   (semver: patch = fix, minor = feature, major = breaking)
2. Add a `CHANGELOG.md` entry for the new version.
3. Run the tests: `python3 scripts/test_guard.py && python3 scripts/test_guard_marker.py && python3 scripts/test_tripwire.py` and `claude plugin validate .` from the repo root.
4. Commit + push to `vdk888/bubble-shield` (private dev repo).
4b. **Publish to the PUBLIC distribution repo `Bubble-invest/bubble-shield`** (this
    is where clients install from — no GitHub account needed because it's public).
    Sync the self-contained plugin + root marketplace.json into the public repo
    (exclude tests/__pycache__/.git and the real `deployment_allowlist.json` —
    only the `.example` ships), run the 5-point PII/secret scan, commit + push.
    Clients install with: `Bubble-invest/bubble-shield`.
5. **Verify a real client can get it:**
   - CLI: `/plugin marketplace update bubble-shield` then
     `claude plugin update bubble-shield@bubble-shield` → should report the NEW version (not "already at latest").
   - Cowork: Customize → Plugins → the marketplace → **Update** (or enable
     **Sync automatically** so it re-syncs on each merge). Then `/reload-plugins`.

## Why uninstall+reinstall is NOT the answer

It works in a pinch but clients won't do it. The version bump is the supported
path that makes `update` actually pull the new code.
