# Changelog — caveau-guard

All notable changes to the plugin. Bump the version in BOTH
`plugin/caveau-guard/.claude-plugin/plugin.json` and the repo-root
`.claude-plugin/marketplace.json` (two places) on every release, or clients'
`claude plugin update` will report "already at latest" and skip the new code.

## 1.5.0 — 2026-06-14

- **Cowork-only gate on the self-installer — no more host-Mac spill.** The
  SessionStart `install_user_hooks.py` previously wrote the PreToolUse guard +
  UserPromptSubmit tripwire into `$HOME/.claude/settings.json` unconditionally.
  On a real Mac that shared file is read by every CLI session, cron, and the
  Desktop app — so the guard spilled off-Cowork and could block unrelated
  Bash/Read calls (it broke a scheduled task on 2026-06-07). It also survived a
  plugin uninstall, so it kept firing after removal.
- The installer now runs ONLY inside the Cowork sandbox VM, detected by
  `HOME` starting with `/sessions/` (the confirmed Cowork VM home), or
  `CLAUDE_CODE_IS_COWORK=1`, or `CLAUDE_CODE_ENTRYPOINT=local-agent`. On the host
  Mac none of these hold → the installer no-ops and writes NOTHING (not even the
  stable script dir). Verified: live Cowork probe (HOME=/sessions/<name>) +
  anthropics/claude-code#40495. Fail-safe direction: if Cowork can't be
  positively confirmed, it does not install.
- Guard enforcement itself is unchanged and still fires in Cowork (live-verified
  2026-06-14: a marked Dropbox folder blocked a raw Read with the 🔒 message).

## 1.4.0 — 2026-06-03

- **Visual tool for Cowork — the before/after as an artifact.** The local webapp
  (FastAPI on 127.0.0.1) can't run in Cowork's sandbox (its localhost is the VM,
  not the user's Mac; FastAPI deps aren't vendorable). Added
  `scripts/make_artifact.py`: runs the same engine and emits one self-contained
  HTML file (inline CSS, identical view + styling to the webapp) with the
  highlighted before/after, the verdict, and the masquer/conserver toggle table.
  The anonymise + onboarding skills now present this artifact as the visual tool.
  Pure-stdlib + vendored engine — runs in Cowork, zero install.
- Onboarding no longer sends Cowork users to the dead-end webapp; the standalone
  webapp is reframed as a power-user/own-machine tool.

## 1.3.0 — 2026-06-03

- **Cowork enforcement fix.** In Cowork the agent runs in a VM spawned with
  `--setting-sources=user`, so plugin-bundled hooks (`hooks/hooks.json`) are
  silently ignored — only the VM's user `settings.json` is honoured (Anthropic
  issue #16288). Added a **SessionStart** hook (`scripts/install_user_hooks.py`)
  which DOES fire from a plugin and writes the guard (PreToolUse) + tripwire
  (UserPromptSubmit) into the VM's `~/.claude/settings.json` at session start.
  Idempotent; preserves other hooks; harmless no-op on the CLI.
- The guard now also matches Cowork's shell tool `mcp__workspace__bash` (not just
  `Bash`), and reads the command from `command`/`script`/`code`.

## 1.2.1 — 2026-06-02

- **Encrypted vaults now work with zero install too.** Re-implemented vault
  encryption in pure Python stdlib (PBKDF2-HMAC-SHA256 key derivation + an
  HMAC-SHA256 counter-mode cipher + encrypt-then-MAC authentication), dropping
  the `cryptography` dependency. So saving/loading a passphrase-protected vault
  runs on any Mac's built-in python3 — no `pip install`, fully offline. Wrong
  passphrase or a tampered file fails loudly (constant-time MAC check). Legacy
  v1 (scrypt+Fernet) vault files still load when `cryptography` is present.

## 1.2.0 — 2026-06-02

- **Fully self-contained — works as a complete product with zero install.** The
  plugin now bundles its dependencies under `vendor/` (the Caveau engine + a
  pure-python `pypdf`), so the anonymiser runs from a GitHub install or a Cowork
  zip with **no `pip install`, no engine on the user's machine, no network**.
  Same approach as Bubble Sentinel.
- `.docx` is now read with the Python standard library (zipfile + ElementTree) —
  no `python-docx`/`lxml` needed. PDF via the vendored pypdf. Plain text native.
- Scripts + the `caveau-anonymize` skill load the bundled engine via
  `${CLAUDE_PLUGIN_ROOT}/vendor`. The firm-identity config is never vendored.

## 1.1.0 — 2026-06-02

- **In-folder marker protection (Cowork-native).** Drop a `.caveau-guard.json`
  inside a folder to protect it + its subtree; the guard walks up from each
  accessed file to the nearest marker. Works inside Cowork's sandbox (which can't
  write to `~/.config`). Opt-in: only marked folders are guarded.
- **Chat-box tripwire** (`UserPromptSubmit` hook): nudges/blocks when raw PII is
  pasted or a document is uploaded directly in the conversation.
- **Client identity removed from source.** The firm allowlist now loads from a
  gitignored deployment config; source ships only generic public third parties.
- Onboarding skill + docs rewritten to the Cowork flow (request the client
  folder, write the marker into it — no Terminal, no `~/.config`).

## 1.0.0 — 2026-06-01

- Initial release: fail-closed `PreToolUse` guard blocking reads of
  `protected_folders` (global config) + bundled `caveau-anonymize` and
  `caveau-onboarding` skills.
