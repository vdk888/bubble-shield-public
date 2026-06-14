# Changelog — caveau-guard

All notable changes to the plugin. Bump the version in BOTH
`plugin/caveau-guard/.claude-plugin/plugin.json` and the repo-root
`.claude-plugin/marketplace.json` (two places) on every release, or clients'
`claude plugin update` will report "already at latest" and skip the new code.

## 1.8.0 — 2026-06-14

- **Three new MCP tools — "PII from anywhere" + write-back, no Terminal.**
  - `caveau_anonymize_text(text)` — anonymise any text that isn't a file (an
    e-mail body, a message, an API result). The mail path: fetch → anonymise the
    body → reason over tokens. Closes the gap that `caveau_read` (files only)
    left.
  - `caveau_write(path, content)` — the write-back direction: the agent drafts a
    document using ⟦…⟧ tokens, calls this with the output path; Caveau restores
    the real values LOCALLY and writes the file, returning only a success line —
    NEVER the de-anonymised content. So a finished client document with real PII
    is produced without the agent ever seeing the real values. Fail-closed
    (errors if no vault; never writes raw on failure).
  - `caveau_setup_ml(action=start|status)` — installs/checks the ML accuracy pack
    from INSIDE Cowork with no Terminal: the host-side MCP server spawns the
    bootstrap detached and reports progress, since the agent's own shell is
    VM-only. start is idempotent (no reinstall if present).
- Onboarding skill updated: the four tools, the no-Terminal ML setup flow, and
  the read-in → tokens → write-out workflow.
- Tests: caveau_mcp 12/12 (incl. write hides real PII from the response while the
  file gets it; fail-closed without a vault); guard 14/14, marker 11/11,
  tripwire 18/18, posttool 11/11, extract OK. plugin validate ✔.

## 1.7.0 — 2026-06-14

- **`caveau_read` MCP tool — the Cowork workaround for ambient anonymisation.**
  Live testing showed Cowork RUNS our PostToolUse hook but ignores
  `updatedToolOutput` for built-in tools (Read/Bash) — output rewrite only takes
  effect for MCP tools (anthropics/claude-code#32105). So the v1.6 ambient hook
  can't cloak a built-in Read in Cowork.
  - New pure-stdlib stdio MCP server (`.mcp.json` → `scripts/caveau_mcp.py`)
    exposing `caveau_read(path)`: the agent reads client files THROUGH it and the
    tool's OWN returned content is already anonymised (⟦…⟧), so no rewrite is
    needed — output is controlled at the source, which Cowork honours for MCP.
  - Reuses the engine + extractor + policy panel + warm NER daemon + session
    vault (reversible, consistent tokens with the folder path). FAIL-CLOSED:
    returns an error, never raw text (opposite of the ambient hook's fail-open).
  - The folder guard still blocks the bare Read of protected files, steering the
    agent to `caveau_read`.
  - Verified locally (CLI --plugin-dir): agent discovers + calls the tool and
    receives cloaked name/IBAN/email. Cowork-surfacing is the next live test.

## 1.6.0 — 2026-06-14

- **ML accuracy pack — "protect PII anywhere" (opt-in, off by default).** A new
  PostToolUse hook anonymises sensitive data in ANY tool result before Claude
  sees it (a fetched email, a script's stdout, an opened Excel) — not just inside
  marked folders. On-device GLiNER NER via a warm localhost daemon (~40ms warm),
  ONNX runtime (~71MB, no PyTorch), nothing leaves the machine.
  - `caveau_setup_ml.py` — one-time day-one bootstrap: persistent venv + model
    download + a login LaunchAgent so the daemon is always warm.
  - `caveau_nerd.py` — warm NER daemon (127.0.0.1 only, idle-shutdown).
  - `posttool_anonymize.py` — the hook: regex core + daemon NER, opt-in
    (`posttool_enabled`), FAIL-OPEN, PII-presence gate, honours the policy panel
    + session vault (reversible, consistent tokens with the folder path).
  - Self-installer + hooks.json register PostToolUse; cowork-only gate unchanged.
  - onboarding skill: plain-FR "protect everywhere" branch + accuracy-pack.md.
- Limit (by design): a doc pasted/dragged straight into the chat is in context
  before any hook — cannot be auto-anonymised. Onboarding warns about this; it is
  the user's responsibility. The fail-closed guarantee stays the folder guard.

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
