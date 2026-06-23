# Changelog — bubble-shield

All notable changes to the plugin. Bump the version in BOTH
`plugin/bubble-shield/.claude-plugin/plugin.json` and the repo-root
`.claude-plugin/marketplace.json` (two places) on every release, or clients'
`claude plugin update` will report "already at latest" and skip the new code.

## 1.14.1 — 2026-06-23 (fix #257-b: DOB leak via Né(e) le + placeholder guard)

- **BLOCKER (PR #3 reviewer): birthdate `03/05/1980` leaked in clear** in the
  form pattern `Né(e) le : DD/MM/YYYY`. Root cause: the core `DATE_NAISSANCE`
  recognizer regex `n[ée]e?\s+le` did NOT match `Né(e) le` because the literal
  parenthetical `(e)` breaks the `e?` alternation. The 1.14.0 release added
  Nom/Lieu/Pièce recognizers but left the DOB recognizer un-fixed, and
  `test_257_form_layout.py` had no assertion for birthdate masking — so the bug
  was missed by the green test run.
- **Fix — `recognizers.py` (Option A):** extended the `DATE_NAISSANCE` recognizer
  alternation from `n[ée]e?\s+le` to `n[eé](?:e|\(e\))?\s+le` so that `Né(e) le`,
  `né(e) le`, `née le`, `né le`, and `nee le` all match. The fix is in the core
  regex (daemon-independent) and fires in both daemon DOWN and UP paths. No
  existing DATE_NAISSANCE tests regress. (file: `bubble_shield/recognizers.py`,
  recognizer at line ~170 in the RECOGNIZERS list.)
- **Advisory fix — placeholder guard in `structured_ext.py`:** form label
  recognizers now skip placeholder/empty-marker values (`(vide)`, `N/A`, `néant`,
  `non renseigné`, etc.) that appear in unfilled template fields. Previously
  `Prénom : (vide)` produced a false NOM match that could corrupt the vault and
  mask template boilerplate. Guard is applied in `form_nom_matches` and
  `form_lieu_naissance_matches`. New `_PLACEHOLDER_VALUES` frozenset + `_is_placeholder()`
  helper added.
- **`test_257_form_layout.py` — new assertions (was missing, now explicit):**
  - Part A (daemon DOWN): `"03/05/1980" not in output` — the blocker assertion.
  - Part B (daemon UP): same assertion guarded behind daemon availability check.
  - Part D (unit): `DATE_NAISSANCE` recognizer tested for `Né(e) le` form directly;
    placeholder guard tested for `(vide)`, `N/A`, `néant` → no NOM emitted.
  - Total: 49 assertions (was 42).
- **CHANGELOG correction:** 1.14.0 entry falsely claimed DOB was among the fields
  fixed. Corrected — 1.14.0 fixed Nom/Prénom/Lieu/Passeport/CNI wiring; DOB via
  `Né(e) le` was missed and is fixed here in 1.14.1.
- All 3 copies of `structured_ext.py` and `recognizers.py` re-synced (root →
  `plugin/bubble-shield/vendor/` → `plugin/bubble-shield/mcpb/server/vendor/`).
  `scripts/` ↔ `mcpb/server/scripts/` identical. MCPB re-packed.
- All suites green: `test_257_form_layout` 49/49, `test_bubble_shield_mcp` 18/18,
  `test_guard` 21/21, `test_guard_marker` 11/11, `test_tripwire` 18/18,
  `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.

## 1.14.0 — 2026-06-23 (fix #257: FR état-civil FORM layout detection — wiring + Nom/Lieu/Pièce recognizers)

- **Fixed: GLiNER misses PII in FORM-LABEL layouts (board #257 — TOTAL LEAK).**
  In real client DCCs structured as `Nom : DUBOIS / Prénom : Marc / Né(e) le :
  03/05/1980 à : Lyon / Passeport n° 12AB34567`, the GLiNER NER daemon was blind
  to name, prénom, birthplace, and passport because the label-value layout has no
  prose context for NER to anchor on. Even with the daemon armed, those fields leaked.
  **Note: DOB via `Né(e) le` was NOT fixed in this release — see 1.14.1.**
- **Root cause — wiring bug confirmed:** `_engine()` in `bubble_shield_mcp.py`
  and the engine construction in `posttool_anonymize.py::main()` both built the
  engine with only `[daemon_detector]` as `extra_detectors`. `structured_ext`'s
  deterministic form-layout recognizers were NEVER wired in, so they never ran in
  the `bubble_shield_read` / `anonymize_text` / posttool path.
- **Fix 1 — wiring:** `structured_ext.make_structured_detector()` is now the
  FIRST entry in `extra_detectors` in both `bubble_shield_mcp.py::_engine()` and
  `posttool_anonymize.py` main engine build. It runs before the daemon and is
  daemon-independent (fail-open if import fails).
- **Fix 2 — new recognizers in `structured_ext.py`:**
  - `form_nom_matches`: matches `Nom : <VALUE>`, `Prénom : <VALUE>`,
    `Nom de naissance : <VALUE>`, `Nom d'usage : <VALUE>` → **NOM**.
  - `form_lieu_naissance_matches`: matches `Lieu de naissance : <VALUE>` and
    `Né(e) le : <DATE> à : <CITY>` (the "à :" city fragment) → **LIEU_NAISSANCE**.
  - `form_piece_identite_matches`: matches `Passeport n° <ID>`, `CNI n° <ID>`,
    `Pièce d'identité : <ID>`, `Titre de séjour n° <ID>` → **PIECE_IDENTITE**.
    Pattern: ID numbers are bounded to 30 chars starting with uppercase letter or digit.
  - All three are ADD-only, fail-open, recall-first.
- **Regression test `scripts/test_257_form_layout.py`:** 42 assertions covering
  daemon DOWN and daemon UP paths, `bubble_shield_read` file path, explicit lieu
  label, non-PII control text (no over-masking), and direct unit tests of each new
  recognizer. Uses `XANTHIPPE ZORVEC` — a name not in any FR first-name gazetteer —
  to prove the structured recognizer does the work, not the gazetteer.
  **Missing assertion: DOB masking — see 1.14.1.**
- Vendor re-synced. MCPB re-packed; `scripts/` ↔ `mcpb/server/scripts/` identical.
- All existing suites green: `test_bubble_shield_mcp` 18/18, `test_guard` 21/21,
  `test_guard_marker` 11/11, `test_tripwire` 18/18, `test_posttool_anonymize` 19/19,
  `test_option_b_e2e` 9/9, `test_256_daemon_path_fail_loud` 16/16.

## 1.13.1 — 2026-06-23 (dev: bump to align with #256 public release)

- Dev plugin.json version bump to 1.13.1 to align with public repo (no code changes).

## 1.13.0 — 2026-06-23 (MCPB-packaged MCP server — installable as a plugin)

- **Fixed plugin install failure.** The Claude app rejected the plugin with
  "MCP server 'bubble_shield' is a local/stdio server. Plugins may only declare
  remote (http/sse/ws) or MCPB servers." (same rejection caused the opaque GitHub
  `failed_content`). Plugins may not declare a bare stdio MCP server.
- **Converted the stdio server to MCPB.** The local stdio server is now packaged
  as `mcpb/bubble-shield.mcpb` (MCPB manifest v0.4, `server.type=python`) and the
  plugin declares it via `plugin.json` → `"mcpServers": "./mcpb/bubble-shield.mcpb"`.
  The old stdio `plugin/bubble-shield/.mcp.json` was removed (the loader merges it
  too, so leaving it would re-trigger the rejection). The host extracts the bundle
  and launches the wrapper `server/entry.py`, which puts the bundled pure-python
  engine + scripts on `sys.path` and runs the unchanged `bubble_shield_mcp.py`.
- **Bundle is pure-python.** Only the vendored `bubble_shield` engine and `pypdf`
  ship in the MCPB (1.7 MB unpacked). The ML accuracy pack (GLiNER/onnxruntime/
  numpy) stays a lazy, on-demand `bubble_shield_setup_ml` download into a separate
  runtime venv — NOT bundled.
- All 8 tools, fail-closed behaviour, and the vault round-trip verified through the
  packed-and-extracted bundle on synthetic PII. Existing suites stay green.
- **Re-pack on every release** (see RELEASING.md): the `.mcpb` is a built artifact;
  if you change `scripts/` or `vendor/`, re-sync into `mcpb/server/` and re-pack.

## 1.12.0 — 2026-06-23 (verified protection model + client docs)

- **Corrected the documented protection model.** Proven on Claude Code v2.1.186:
  PostToolUse `updatedToolOutput` is ignored by the harness for built-in tools
  (Read *and* Bash), every value shape — not just a Cowork limit
  ([anthropics/claude-code#32105](https://github.com/anthropics/claude-code/issues/32105),
  open). The plugin README now states the guarantee plainly: protection is the
  `PreToolUse` deny/steer + the first-party `bubble_shield_read` MCP tool (whose
  own output the harness *does* honor), NOT a post-hook scrub. The "anonymisé"
  notice is context, never proof of substitution.
- **Verified the Option B path end-to-end.** `bubble_shield_read` returns `⟦…⟧`
  tokens, fails closed on error, the guard denies a built-in Read of a marked
  folder and steers to the MCP tool, vault round-trips. Added
  `scripts/test_option_b_e2e.py` (9 cases). No regressions across the suites.
- **Documented root-marker coverage.** One `.bubble-shield.json` at a folder root
  protects the whole tree (sub-folders inherit); files outside marked folders are
  not covered — now explicit in the README.
- **Client onboarding docs** (non-technical, FR): added a Cowork user tutorial and
  corrected the governance email (both surfaces install the same plugin; the
  difference is config scope, not install-vs-no-install).

## 1.11.1 — 2026-06-14 (finding + honest docs)

- **PROVEN: Cowork does not run PostToolUse on third-party MCP connectors.** A
  live probe (logged every PostToolUse invocation) across 19 real Gmail calls in
  Cowork recorded ZERO firings on the mail connector (only Bash fired). So mail
  containment — correct + fail-safe in unit tests and in plain Claude Code CLI —
  NEVER ENGAGES in Cowork. It is effectively CLI-only.
- Consequence for mail in Cowork (now documented honestly, no overclaim): raw
  mail reaches context; the PreToolUse mail-guard STEER is the only mechanism that
  fires, and it is best-effort (observed: agent complied once, summarised raw mail
  twice). The RELIABLE protection for sensitive mail is to route it through a
  protected folder and read via bubble_shield_read (PreToolUse-enforced). Onboarding
  skill rewritten to say exactly this.
- mail_containment left ON BY DEFAULT (harmless fail-safe; protects in CLI). Probe
  removed. No behaviour change beyond docs + the recorded finding.

## 1.11.0 — 2026-06-14

- **Mail containment is now ON BY DEFAULT.** `mail_containment` defaults true and
  runs INDEPENDENTLY of `posttool_enabled`. Mail PII reaches context the instant
  the connector returns and is high-risk, so protection-on is the honest default
  for a privacy product. The fail-safe (pass-through on any shape mismatch, never
  crashes the connector) is what makes default-on safe despite the
  undocumented-shape dependency. Set `mail_containment:false` to fall back to the
  PreToolUse steer only. Tests: posttool 19/19, guard 21/21.

## 1.10.0 — 2026-06-14

- **Mail containment (opt-in `mail_containment`) — true protection with a safety
  net.** The 1.9.1 allow+steer improved output behaviour but live-tested as
  unreliable: the agent self-censored yet did NOT call bubble_shield_anonymize_text, so
  raw mail still reached context. This adds real containment: a PostToolUse
  handler intercepts a mail connector's result and anonymises its string values
  IN PLACE, PRESERVING the exact data shape (no flat-string clobber → no
  H.reduce), so raw PII never enters context. SAFETY NET: it only rewrites a
  shape it can cleanly reproduce; on ANY mismatch/error it emits nothing
  (pass-through → the 1.9.1 steer still applies), so it can never crash the
  connector — turning the brittle (undocumented Anthropic-owned shapes)
  dependency from silent-brick into silent-degrade. Off by default; enable per
  client. PostToolUse matcher widened to mcp__.* — SAFE because non-mail
  structured results bail via the simple-text safe-gate (verified: notion-style
  result untouched). Tests: posttool 19/19, guard 21/21. NOTE: still depends on
  undocumented connector/hook shapes; the fail-safe is why it's shippable. Needs
  live Cowork confirmation that the preserved shape is accepted (no H.reduce).

## 1.9.1 — 2026-06-14 (fix)

- **Fix: mail-guard no longer creates a catch-22.** 1.9.0 DENIED raw mail reads
  and told the agent to "fetch then anonymise" — but a PreToolUse deny means the
  fetch never runs, so the agent retried the same blocked call forever and mail
  became UNUSABLE (live-caught: the agent correctly refused to route around it).
  The fetch IS the only way to get the mail text to anonymise, so blocking it is
  self-contradictory. Now the guard ALLOWS the mail read but returns
  `additionalContext` (PreToolUse supports allow + context) forcefully instructing
  the model to run the fetched text through `bubble_shield_anonymize_text` before using
  it. Mail flows; strong steer; still honest-scope (a steer, not hard
  containment — raw mail transits the result once). guard 21/21.

## 1.9.0 — 2026-06-14

- **Mail-guard — enforced anonymisation for e-mail (PreToolUse).** Live test
  showed that, given a neutral "read my emails", the agent read raw mail and
  leaked a real e-mail address — it did NOT anonymise on its own. Judgment-based
  protection is unreliable for a privacy product. Now the guard BLOCKS raw mail-
  connector reads (Gmail `search_threads`/`get_thread`/`list_messages`… — detected
  by mail-specific actions since the connector id is an opaque UUID) with a
  forceful instruction to pipe the fetched text through `bubble_shield_anonymize_text`
  first. The guard's PreToolUse matcher widened to `mcp__.*` (safe — the guard
  only emits allow/deny, never updatedToolOutput, so it can't hit the #H.reduce
  rewrite-shape bug). Non-mail mcp tools (incl. our own bubble_shield_read, workspace
  bash, notion) are not caught. Opt-out: `mail_guard:false`; extend detection via
  `mail_tool_patterns`. Tests: guard 21/21 (+7 mail-guard). HONEST SCOPE: this is
  a strong STEER, not the hard containment the folder guard gives — raw mail still
  transits the tool result once before the agent anonymises it (Bubble Shield has no mail
  creds, can't fetch+anonymise mail server-side).

## 1.8.3 — 2026-06-14 (fix)

- **Fix: PostToolUse no longer rewrites arbitrary MCP connector output (the real
  Gmail `H.reduce` cause).** 1.8.2 skipped structured results by shape, but the
  hook still MATCHED `mcp__.*`, and rewriting any MCP connector result breaks the
  Cowork harness — it measures the result as a content-block array, so a rewrite
  throws `H.reduce is not a function` (live-diagnosed: Gmail failed every call in
  Cowork-with-Bubble Shield, worked in a plain chat). The PostToolUse matcher is now
  `Read|Bash|mcp__workspace__bash` only — built-in tools + Cowork's own shell,
  whose text rewrite is proven safe. Arbitrary `mcp__.*` connectors (Gmail etc.)
  are no longer touched; PII in their results goes through the explicit
  `bubble_shield_anonymize_text` / `bubble_shield_read` tools. The 1.8.2 shape-gate stays as a
  second guard. Regression green.

## 1.8.2 — 2026-06-14 (fix)

- **Fix: PostToolUse hook no longer breaks MCP connectors (Gmail).** The hook
  matches `mcp__.*` and rewrote any result containing PII via a flat
  `updatedToolOutput {type:text,text}`. For connectors returning STRUCTURED
  results (e.g. Gmail `{threads:[...]}`) that replaced the array/object shape
  with a string → the connector's own handler threw `H.reduce is not a function`
  on every call (reproduced + root-caused from a live Cowork session; Gmail
  worked in a plain chat with no Bubble Shield). Now the hook only rewrites SIMPLE text
  results (bare string / `{type,text}` / pure text-block list) and leaves any
  structured result UNTOUCHED. PII inside structured MCP results is handled by
  the explicit `bubble_shield_anonymize_text` / `bubble_shield_read` tools, not the ambient
  rewrite. Regression guards added (structured + mixed). posttool 13/13.

## 1.8.1 — 2026-06-14

- **`bubble_shield_enable_global` MCP tool — truly-global "anonymise everywhere", one
  click, no Terminal.** Sets `posttool_enabled` in the host config
  (~/.config/bubble_shield/bubble-shield.json) from inside Cowork — the host-side MCP
  server reaches the path the agent's VM shell can't. on/off/status; MERGES into
  the existing config (preserves protected_folders etc., never clobbers). Closes
  the last "needs you / needs Terminal" gap for machine-wide coverage. Tests:
  bubble_shield_mcp 18/18 (incl. merge-preserves-keys).

## 1.8.0 — 2026-06-14

- **Three new MCP tools — "PII from anywhere" + write-back, no Terminal.**
  - `bubble_shield_anonymize_text(text)` — anonymise any text that isn't a file (an
    e-mail body, a message, an API result). The mail path: fetch → anonymise the
    body → reason over tokens. Closes the gap that `bubble_shield_read` (files only)
    left.
  - `bubble_shield_write(path, content)` — the write-back direction: the agent drafts a
    document using ⟦…⟧ tokens, calls this with the output path; Bubble Shield restores
    the real values LOCALLY and writes the file, returning only a success line —
    NEVER the de-anonymised content. So a finished client document with real PII
    is produced without the agent ever seeing the real values. Fail-closed
    (errors if no vault; never writes raw on failure).
  - `bubble_shield_setup_ml(action=start|status)` — installs/checks the ML accuracy pack
    from INSIDE Cowork with no Terminal: the host-side MCP server spawns the
    bootstrap detached and reports progress, since the agent's own shell is
    VM-only. start is idempotent (no reinstall if present).
- Onboarding skill updated: the four tools, the no-Terminal ML setup flow, and
  the read-in → tokens → write-out workflow.
- Tests: bubble_shield_mcp 12/12 (incl. write hides real PII from the response while the
  file gets it; fail-closed without a vault); guard 14/14, marker 11/11,
  tripwire 18/18, posttool 11/11, extract OK. plugin validate ✔.

## 1.7.0 — 2026-06-14

- **`bubble_shield_read` MCP tool — the Cowork workaround for ambient anonymisation.**
  Live testing showed Cowork RUNS our PostToolUse hook but ignores
  `updatedToolOutput` for built-in tools (Read/Bash) — output rewrite only takes
  effect for MCP tools (anthropics/claude-code#32105). So the v1.6 ambient hook
  can't cloak a built-in Read in Cowork.
  - New pure-stdlib stdio MCP server (`.mcp.json` → `scripts/bubble_shield_mcp.py`)
    exposing `bubble_shield_read(path)`: the agent reads client files THROUGH it and the
    tool's OWN returned content is already anonymised (⟦…⟧), so no rewrite is
    needed — output is controlled at the source, which Cowork honours for MCP.
  - Reuses the engine + extractor + policy panel + warm NER daemon + session
    vault (reversible, consistent tokens with the folder path). FAIL-CLOSED:
    returns an error, never raw text (opposite of the ambient hook's fail-open).
  - The folder guard still blocks the bare Read of protected files, steering the
    agent to `bubble_shield_read`.
  - Verified locally (CLI --plugin-dir): agent discovers + calls the tool and
    receives cloaked name/IBAN/email. Cowork-surfacing is the next live test.

## 1.6.0 — 2026-06-14

- **ML accuracy pack — "protect PII anywhere" (opt-in, off by default).** A new
  PostToolUse hook anonymises sensitive data in ANY tool result before Claude
  sees it (a fetched email, a script's stdout, an opened Excel) — not just inside
  marked folders. On-device GLiNER NER via a warm localhost daemon (~40ms warm),
  ONNX runtime (~71MB, no PyTorch), nothing leaves the machine.
  - `bubble_shield_setup_ml.py` — one-time day-one bootstrap: persistent venv + model
    download + a login LaunchAgent so the daemon is always warm.
  - `bubble_shield_nerd.py` — warm NER daemon (127.0.0.1 only, idle-shutdown).
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
  plugin now bundles its dependencies under `vendor/` (the Bubble Shield engine + a
  pure-python `pypdf`), so the anonymiser runs from a GitHub install or a Cowork
  zip with **no `pip install`, no engine on the user's machine, no network**.
  Same approach as Bubble Sentinel.
- `.docx` is now read with the Python standard library (zipfile + ElementTree) —
  no `python-docx`/`lxml` needed. PDF via the vendored pypdf. Plain text native.
- Scripts + the `bubble-shield-anonymize` skill load the bundled engine via
  `${CLAUDE_PLUGIN_ROOT}/vendor`. The firm-identity config is never vendored.

## 1.1.0 — 2026-06-02

- **In-folder marker protection (Cowork-native).** Drop a `.bubble-shield.json`
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
  `protected_folders` (global config) + bundled `bubble-shield-anonymize` and
  `bubble-shield-onboarding` skills.
