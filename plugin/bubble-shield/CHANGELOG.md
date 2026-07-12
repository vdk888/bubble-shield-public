# Changelog — bubble-shield

## 1.23.6 — 2026-07-12 — FIX: onboarding demo + honest bubble_shield_read description

- **fix(read tool):** `bubble_shield_read`'s description now states the real
  shadow-index contract — an already-indexed file returns masked tokens, but a
  brand-new / not-yet-indexed file returns the RAW extracted text once (queued
  for the sweep). It tells the assistant not to assume a first read of a fresh
  document is masked, and to pass it through `bubble_shield_anonymize_text`
  (fail-closed) when a guarantee is needed. The old description claimed reads are
  always anonymised — false on a cache miss.
- **fix(onboarding):** the guided demo used `bubble_shield_read` on a
  just-marked folder and assumed tokens — but that first read is a cache miss and
  returns raw, so the demo could show the client's real name. Temps A now reads
  then passes through `bubble_shield_anonymize_text` (guaranteed masked). The
  welcome message and the everyday "read in tokens" step were corrected to match.

## 1.23.5 — 2026-07-12 — UX: dashboard is the landing page + nav + collapse

- **ux(landing):** the app now opens on the risk-control dashboard (coverage,
  stats, settings) instead of the anonymise tool. The anonymise/try-it tool
  moved to `/anonymize` and is in the nav.
- **ux(nav):** reordered — Contrôle & réglages · Anonymiser · File de révision ·
  Liste connue · Coffres · Comment ça marche (help moved to the end).
- **ux(dashboard):** "Derniers passages" is now collapsed by default (a
  click-to-expand section) with a one-line explanation of what the log shows —
  it no longer takes space unexplained.

## 1.23.4 — 2026-07-12 — FEATURE: shadow-index coverage on the dashboard

- **feat(dashboard):** an "Indexation du dossier protégé" panel shows how far the
  background sweep has indexed each protected folder — a progress bar, `N/M
  fichiers indexés`, and the count still pending. This surfaces the read-safety
  state: an already-indexed file reads fast and masked, while a brand-new,
  not-yet-indexed file may be served raw on its first read until the sweep
  catches up. Wires the existing `coverage.py` (`{total, indexed, pct,
  pending_files}`) into the UI; read-only, degrades to an empty state when no
  folder is marked.

## 1.23.3 — 2026-07-11 — FEATURE: in-app uninstall button + complete daemon cleanup

- **feat(uninstall):** the dashboard now has a "Désinstaller Bubble Shield" danger
  zone — a client can fully remove the host footprint (guard hooks, host scripts,
  daemons, caches) from the app UI, no terminal. A `/plugin uninstall` only drops
  the marketplace entry and leaves the guard hooks firing on every tool call; this
  button runs the real uninstaller. Confirm-word poka-yoke (`DESINSTALLER`) plus an
  opt-in "also erase my vaults" checkbox (with a clear de-cloak-loss warning); the
  result page shows the cleanup log.
- **fix(uninstall):** the uninstaller now removes **all three** daemon LaunchAgents
  (nerd, gemmad, sweep). It previously removed only the nerd daemon, so `gemmad`
  (the Gemma de-pollution judge) and `sweep` (the shadow-index indexer) were left
  behind and kept respawning after uninstall.

## 1.23.2 — 2026-07-11 — FIX: app self-update survives a rewritten remote + README/RELEASING docs

- **fix(installer):** `install-app.sh` now updates via `git fetch` + `git reset
  --hard origin/<default-branch>` instead of `git pull --ff-only`. A fast-forward
  pull cannot reconcile a remote whose history was rewritten (a maintainer
  force-push) — it aborted with "Not possible to fast-forward" and left the app
  permanently unable to update. Hard-reset to the remote is always the correct
  update for the app checkout (all user data lives in `~/.bubble_shield`, never
  in the app dir) and survives any remote rewrite. Resolves the default branch
  dynamically.
- **docs(README):** corrected the detection-layers table — the neural layers are
  **GLiNER (ONNX)** and **Gemma (MLX)** on-device daemons (the accuracy pack,
  opt-in, fail-open to the regex core), not the older Presidio/spaCy + Ollama
  paths (which remain as dormant fallback code, not the shipped stack).
- **docs(RELEASING):** never force-push the public repo — a rewrite breaks every
  existing client's update (app + plugin marketplace cache); a hard scrub must
  re-create the public repo fresh, not rewrite-and-force.

## 1.23.1 — 2026-07-11 — DOCS: skills + README match the v1.23.0 read model

Documentation-only release aligning the operator-facing docs with how v1.23.0
actually reads. No engine change.

- **Skills corrected.** `bubble-shield-anonymize` and `bubble-shield-onboarding`
  previously told the assistant that `bubble_shield_read` "always returns
  anonymised content / fails closed, never raw." Since v1.23.0 the read serves a
  pre-computed masked **shadow** (zero models at read time): a hit returns the
  masked copy, but a **miss** (a brand-new / never-indexed file) serves the raw
  extracted text once and queues the file for the background sweep. The skills
  now describe this honestly — including "do not assume a first read of a
  brand-new document is masked; run the sweep or the batch flow first" — so the
  assistant operates the tool with a correct mental model.
- **README.** Added a "Shadow-index runtime" section explaining the background
  sweep → shadow store → hash-serve read (and the accepted first-read-miss gap),
  and removed the stale mail-triage section for a feature that is no longer part
  of the product.

## 1.23.0 — 2026-07-11 — FEATURE: shadow-index runtime + de-pollution redesign

Reads no longer run heavy PII models on the hot path. A background sweep
pre-indexes the protected folder into a content-hash-keyed shadow store; at read
time `bubble_shield_read` serves the pre-computed masked shadow (zero models,
fast), accepting that a brand-new/unindexed doc is served on a cache miss (the
sweep masks it on the next pass). This is the speed redesign clients asked for.

- **Gazetteer de-pollution — rebuilt judge.** The FP-cleaning pass that prunes
  the always-mask gazetteer now uses an on-device identifying-value-vs-generic
  judge instead of the old binary NOM/MOT single-token prompt (which could not
  reason about multi-word phrases). It is scoped by an **entity-type allowlist**
  — only names, job titles, and addresses are ever adjudicated; structured
  identifiers (IBAN, SIRET, social-security, email, phone, tax IDs, dates,
  company names) are **never judged and never un-masked** (a structural
  guarantee over checksum-verified data). The judge keeps real names (including
  bare single-token surnames that are also common words, e.g. *Petit*),
  addresses, and companies masked, while un-masking job titles, form labels, and
  boilerplate. Fail-toward-masking throughout: only a clean "generic" verdict
  un-masks; any PII verdict, model error, timeout, or ambiguity keeps the entry
  masked. Measured end-to-end on a real 742-entry gazetteer: ~78 false positives
  cleaned per pass, **0** structured identifiers / **0** real names / **1** of
  110 addresses un-masked (a truncated address caught in the review queue);
  overall masking recall unchanged (98.9% overall, 97.1% on names).
- **Daemon no longer wedges under a large background pass.** De-pollution requests
  to the single on-device worker are chunked, so a big sweep can't monopolise the
  worker and stall every other request (previously a ~370-entry pass ground the
  worker for minutes while everything behind it timed out). Fail-toward-masking is
  preserved per chunk: a failed chunk contributes no verdicts (its entries stay
  masked).
- **On-device models are lazy — no ~6.7 GB at login.** Both daemons (Gemma judge,
  GLiNER NER) now start with `--no-warm`: they open their port without loading a
  model and load it on the first inference request (a sweep or read), then free it
  ~10 min after last use. A machine that never uses Shield holds ~0 GB of model
  memory instead of ~6.7 GB resident from every boot.

## 1.22.3 — SECURITY (P0 #589)

- **fix(security) (#589, P0):** `bubble_shield_read` / `bubble_shield_anonymize_text` no longer return the RAW document when the engine finds ZERO detections on a SUBSTANTIAL document (`verdict_state == "zero_detection"`). Previously `_anonymise_text` failed closed only when the NER daemon was DOWN (`NERDownError`); when the daemon was UP and healthy but the engine simply found nothing to mask, it still returned `res.anonymized` — which on a zero-detection result IS THE RAW INPUT TEXT — decorated with a soft "please review" note. A note is not containment: the raw PII is already in the model's context. This leaked a real client's raw PDF (43KB, 4 raw phone numbers, zero tokens) in a live session. Now raises `ZeroDetectionError`, converted by the tools/call handler to `isError:true` with a FIXED French refusal message and NO body. Gated precisely on engine.py's `substantial_text` boundary (>=8 words AND >=40 chars) so a genuinely tiny/empty input (`nothing_to_do`) is never refused. The daemon-down fail-closed path, the leak/low_confidence review-note cases, and normal masking are unchanged. **BEHAVIOR CHANGE:** a genuinely-clean substantial document now also refuses pending human review — the intentional safe direction.

## 1.22.2

- fix(installer): `install-app.sh` no longer blindly reuses an existing `.venv` on update. It now compares the existing venv's Python ABI (major.minor) against the freshly-selected interpreter and rebuilds the venv (`rm -rf .venv`, French log message) on mismatch — fixes a live bug where a client whose `.venv` was built against a wrong-ABI Python (e.g. Homebrew python3.12 shadowing stock python3.9 on PATH, the #396b case) hit `ResolutionImpossible / Cannot install pywebview==3.4` on every subsequent update, because the cp39-only offline wheels can't install into a 3.12 venv holding old unpinned deps. A matching-ABI venv is still reused untouched (fast path); only the app's own `.venv` is ever deleted, never the app dir or user data.
- fix(dashboard) (#587): the risk-control dashboard no longer counts `vault_reveal` (document RESTORE) events as anonymisation runs. `summarize()` now scopes `runs`/`unsafe_runs`/the "anonymisations" headline to `event == "anonymize"` only; restores are surfaced separately and honestly as `reveal_runs` ("restaurations"), never merged into the risk numbers. Fixes a live false "35 à relire of 38" (should have read "0 à relire of 3") caused by fail-closed `_is_unsafe()` flagging every reveal (no `safe_to_send` key) as residual-PII risk. Relabeled the "à relire" stat and the detail-table verdict badge to distinguish restores from genuine risk flags.

## 1.22.1

- fix(list): `bubble_shield_list` returns folder/file NAMES in clear (they are navigation labels the user owns and already sees on their machine) — masking them broke the user's ability to tell the agent which client folder to open. File CONTENTS stay fully masked (`bubble_shield_read` unchanged). The listing no longer depends on the NER daemon.
- docs: two-phase architecture roadmap in PRODUCT-REFERENCE §7.1 (tool-layer masking now / egress-proxy after Cowork→Claude Code migration).


All notable changes to the plugin. Bump the version in BOTH
`plugin/bubble-shield/.claude-plugin/plugin.json` and the repo-root
`.claude-plugin/marketplace.json` (two places) on every release, or clients'
`claude plugin update` will report "already at latest" and skip the new code.

## 1.22.0 — 2026-07-07 — FEATURE: gazetteer de-pollution (#568, A+D→Gemma cascade)

The always-mask gazetteer (`known_pii_store.py`) self-pollutes: every value the
engine ever over-masks as a name gets seeded in permanently, hiding legitimate
content forever after. This release adds a repeatable de-pollution pipeline
instead of a one-time cleanup, since the seeding never stops:

- **New `depollute.py` pipeline.** Triages every gazetteer entry through a
  cheap word-frequency + structural filter (A+D) first, then escalates only
  the ambiguous middle to a local Gemma classifier (`gemma_classifier.py`,
  MLX `gemma-3n-E4B-it-4bit`) for a NOM/MOT verdict. Fail-safe: any parse
  ambiguity defaults to NOM (keeps masking) rather than risk a leak.
- **New `bubble_shield_gemmad.py` daemon.** Keeps the Gemma model warm behind
  a local `/classify` HTTP endpoint so de-pollution calls don't pay
  cold-start cost per token; the pipeline fails closed to the daemon-down
  path if the daemon isn't reachable.
- **"Clean now" button + de-pollution audit view** in the review-queue UI —
  runs the pipeline on demand and shows what was removed/kept and why.
- **Async on-seed trigger.** De-pollution now also runs (non-blocking) right
  after a new value is seeded into the gazetteer, so junk is caught close to
  when it's introduced instead of only during an on-demand sweep.
- **Fix (root cause, also closes a latent audit-log gap):** `add_candidate`
  now honors the caller's `gaz_path` instead of silently defaulting to the
  main gazetteer — this was dropping audit-log entries for callers operating
  on a non-default gazetteer (e.g. the de-pollution un-mask path itself).
- **Fix:** the gazetteer conflict flag now fires correctly on a default-path
  reseed, and `_parse_verdict` defaults to NOM on any ambiguity (fail-safe).

Design doc: `docs/superpowers/specs/2026-07-07-gazetteer-depollution-design.md`.

**MCPB mirror sync.** `depollute.py`, `gemma_classifier.py`, and
`bubble_shield_gemmad.py` are new vendored/scripted files added by this
release — they, plus the `known_pii_store.py` / `review_queue.py` fixes
above, are now copied into `mcpb/server/{vendor/bubble_shield,scripts}/` so
the shipped `.mcpb` bundle runs the same de-pollution code and the same
`gaz_path` audit-log fix as the plugin copy (was previously deferred/drifted
during the #568 build; `tests/test_mirror_copies_identical.py` now passes).

## 1.20.2 — 2026-07-05 — FEATURE: folder-listing discovery (Glob allow + bubble_shield_list)

The agent can now discover *which* file to read inside a protected folder without
being able to read any file's contents. Two coordinated changes:

- **Glob is now allowed on protected folders (names only).** Previously Glob, Grep and
  Read were blocked as one group on protected paths; Glob is now split out into its own
  branch and permitted, because it returns only filenames — never file content. Grep,
  Read and Bash stay blocked on protected paths (they can surface content), so no PII in
  a file's *body* can leak through this path.
- **New `bubble_shield_list(folder)` MCP tool.** Lists the filenames and subfolders in a
  protected folder (non-recursive), with any PII in the *names themselves* masked through
  the anonymiser before returning. It never returns file content, and it is fail-closed:
  if the NER pipeline is down, the tool refuses rather than returning unmasked names.

Together these let an agent answer "what's in this client's folder?" and pick the right
document to run through `bubble_shield_read`, without ever seeing raw client data.

## 1.20.1 — 2026-07-05 — P0 SECURITY: close Cowork sandbox-mount-alias Bash exfil

A red-team pass found a real PII-exfil path on Cowork: a Bash command referencing a
sandbox mount alias under `/sessions/*/mnt/<...>` could reach protected client data
without going through the anonymiser, because the guard's mount-namespace check was
not unconditional. **Fixed, fail-closed:** the guard now DENIES any
`/sessions/*/mnt/<non-infra>` token — only the known infra mount tokens are allowed,
everything else is blocked by default (Fix C is now unconditional; new
`_COWORK_INFRA_MNT` allowlist gate). A leaked reference in the original report was a
genuine client name; this release is the reason it can no longer escape.

**Also in this release (the re-pack is the whole point):**
- **Re-packed MCPB.** The shipped stdio server (`mcpb/bubble-shield.mcpb`) bundles a
  copy of `scripts/`; a prior release did not re-pack, so a client kept running stale
  guard code. This release re-syncs `scripts/` into `mcpb/server/` and re-packs the
  `.mcpb`, so the fix actually reaches the shipped server.
- **Manifest** now exposes the `mail_read` / `status` tools in the MCPB manifest.
- **Onboarding demo reworked:** the demo now runs a real client-chosen task and shows
  the anonymized-vs-clear output side-by-side, instead of a canned sample.

## 1.20.0 — 2026-07-02 — FEATURE: built-in FR tax/admin recognizers (avis d'impôt / KYC leak fix #319)

A fresh-install client is now protected on avis d'impôt / KYC documents WITHOUT
hand-configuring any custom field. Three FR identifiers that leaked out-of-the-box
(reproduced on v1.19.1 with synthetic values) are now masked:

- **Unlabeled numéro fiscal / référence d'avis** (grouping `NN NN NNNNNNN NN`,
  13 digits). The pre-existing `NUM_FISCAL` recognizer only fired behind a label
  ("numéro fiscal :"); the bare forms printed on an avis d'impôt leaked
  (`Référence de l'avis : 25 92 0364665 70`, or in plain prose). A new UNLABELED
  recognizer catches this shape. **Precision anchor:** the trailing 2-digit control
  key is a mod-97 checksum on the leading 11 digits (`key == 97 - body11 % 97`) —
  a span is masked ONLY when the checksum validates, so a stray 13-digit run in the
  same grouping (a bad-checksum lookalike, a date+amount collision) is NOT masked.
  Maps to the existing `NUM_FISCAL` type (identifying → CLOAK).
- **Télédéclarant alphanumeric block** (grouping `NNN NN NN NNNNNNNNNN N A`,
  18 digits + a trailing single letter, e.g. `922 65 91 2768797789 3 A`). No
  recognizer existed → leaked. New `NUM_TELEDECLARANT` type (identifying → CLOAK).
  **Precision anchor:** the exact digit-group counts + the terminal uppercase
  letter make this shape collision-free against dates, amounts, phones and refs.
- **Bare commune + postcode** (`MONTBOURG-LES-PINS 99000` standalone): already
  shipped in v1.19.x as `commune_postcode_matches` (structured_ext, fix #395) and
  verified still working here — the structured/regex path covers the standalone
  bare-commune leak (the GLiNER 'city' threshold is a separate card, out of scope).

New `Recognizer.drop_if_unvalidated` flag: a checksum-gated recognizer whose regex
shape is not rare enough to fail-closed on drops (does not emit) a validator-failing
match — the mechanism behind the fiscal-ref precision guarantee. IBAN/ISIN/SIRET
keep their fail-closed behaviour (flag defaults False).

SIREN leading-fragment leak (card gap 3): VERIFIED on v1.19.1 and found to be a
mischaracterization — the SIREN recognizer is correctly bounded (it consumes exactly
a checksum-valid 9-digit SIREN and never leaves a fragment of the SIREN itself in
clear). The leading `NN NN` groups in the reported example are a SEPARATE number
(fiscal-ref / télédéclarant fragment), not part of the SIREN. Forcing SIREN to
swallow leading groups would over-mask adjacent dates/amounts/page-numbers — a
precision regression. Left the working SIREN recognizer untouched per the card's
safe-subset guidance; gaps 1/2 cover the real leading-fragment identifiers.

## 1.19.0 — 2026-07-02 — FEATURE: `bubble_shield_mail_read` — own the mail read, fail-CLOSED (Phase 1)

Email is Bubble Shield's weakest surface. Today the ambient PostToolUse hook tries
to SCRUB a Gmail *connector*'s output after the fact — fail-OPEN, regex-only,
fragile (breaks the connector with `H.reduce`), and too late (the raw body already
became a tool result; the harness drops the rewrite per #32105). The FILE guard
avoids all this because it OWNS the read. This release gives mail the same
ownership.

- **New MCP tool `bubble_shield_mail_read(query, max, since)`.** Bubble Shield
  fetches the e-mail ITSELF over IMAP (pure stdlib `imaplib` + `email` — ZERO new
  dependency) and returns each message already anonymised. The connector leaves the
  trust path entirely: the raw e-mail NEVER becomes a tool result.
- **Fail-CLOSED, same guarantee as file read.** Every fetched body is routed
  through the SAME `_anonymise_text` core the file guard uses, which raises
  `NERDownError` when the GLiNER daemon is down → the tool returns `isError:true`,
  NEVER raw e-mail. (The proven prototype used the base engine, which fails OPEN —
  that was wrong; the real tool inherits daemon-up-or-refuse.)
- **Cross-source token consistency.** Uses the same per-mission vault as files, so a
  client masked in a PDF gets the SAME token in their e-mail; From-header and body
  names collapse to one token root (cross-field consistency).
- **Host-side credential store.** IMAP host/user/app-password live host-side
  (`~/.bubble_shield/mail.json`, or `$BUBBLE_SHIELD_MAIL_CREDS`), never exposed to
  the model/Cowork VM. `load_credentials()` REFUSES a world/group-readable creds
  file (chmod-600 enforced) and never logs/returns the password.
- **New files:** `scripts/bubble_shield_mail.py` (IMAP fetch + cred store),
  `scripts/test_bubble_shield_mail.py` (fail-closed + consistency tests on synthetic
  Jean DUPONT fixtures — no real mail, no real PII).
- Scope: READ-ONLY. `bubble_shield_mail_write` (send/reply) is Phase 2. The old
  PostToolUse mail-containment hook is NOT removed yet (Phase 3) — but
  **`bubble_shield_mail_read` is the sanctioned mail path going forward**; the
  connector-scrub approach is deprecated.

## 1.19.1 — 2026-07-02 — Three P1 fixes: allow_paths resolution, pseudonymisation vocabulary, vault encryption-at-rest

Product-review batch of three independent P1s. (Version reconciled to 1.19.1 on the merge ladder: 1.18.19 safe-to-send fix,
1.18.20 recall leaks, 1.19.0 mail-read, then this batch as 1.19.1.)

- **P1 — `allow_paths` relative resolution was broken (escape-hatch dead).** The
  `.bubble-shield.json` marker documents `allow_paths: ["clean"]` as *relative to
  the marker's own folder* (an anonymized-output sub-folder that should be
  readable). But `_norm()` resolved a relative entry against the guard **process
  CWD** (`os.getcwd()`), not the marker folder — so `_norm("clean")` became
  `<random-cwd>/clean` and never matched the client's real `<marked-folder>/clean/`.
  The documented escape-hatch silently never worked, pushing clients to disable the
  guard. **Fix:** `_norm(x, base=marker_root)` resolves relative `allow_paths`
  against the marker folder. `.resolve()` still follows symlinks, so a symlink
  inside `clean/` pointing back at a protected file resolves OUT and stays DENIED —
  the hatch opens the anonymized-output folder, not a hole. Covers both the
  file-tool and Bash paths (both route through `decide_block`). New regression tests
  in `test_guard_marker.py` (relative-allow ALLOWED, secret outside DENIED, symlink
  DENIED); they fail on pre-fix code and pass after.

- **P1 (compliance) — "anonymisation" → "pseudonymisation" in the legal claims.**
  `COMPLIANCE_RGPD.md` flags that calling the tool "anonymisation" in client-facing
  copy is a legal over-promise: it is reversible **pseudonymisation** (RGPD art. 4
  §5 / art. 32), not RGPD anonymisation (which would take data *out* of the
  regulation — the vault keeps it reversible, so it doesn't). **Fix:** corrected the
  formal capability/legal CLAIMS — the root `README.md` product one-liner and the
  webapp `about.html` hero lede — to "pseudonymisation réversible (et locale)".
  Deliberately left the informal VERB usage ("anonymiser ce dossier", button
  labels, mechanism descriptions) and the token type names (`⟦NOM⟧`) untouched, per
  the compliance doc's own guidance to fix the claim, not every occurrence.

- **P1 (art. 32) — vault encryption-at-rest was opt-in; plaintext by default.** The
  vault concentrates all of a mission's PII, so a cleartext file is the
  highest-value target on the machine. `save_encrypted()`/`load_encrypted()` existed
  (pure-stdlib PBKDF2 + HMAC-CTR, encrypt-then-MAC) but weren't the default.
  **Fix (minimum-viable, non-disruptive — chosen over encrypt-by-default to avoid
  corrupting existing client vaults; full default-encryption with machine-local key
  management is a flagged follow-up):** the default `save()` now **warns loudly on
  stderr** whenever it writes plaintext (never touches the JSON stdout that hooks
  parse; suppressible via `BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN=1`), plus a
  one-command in-place migration: `python3 -m bubble_shield.vault encrypt <dir>`
  (and `status <dir>` to audit). Migration verifies an exact decrypt round-trip
  before replacing the original, so no vault can be lost or corrupted; existing
  plaintext vaults still load unchanged via `Vault.load`. New tests cover the
  warning, plaintext detection, exact round-trip, idempotency and legacy-load.

- Re-vendored the changed engine (`vault.py`, 3 copies) + `guard.py` (2 copies) and
  re-packed the MCPB — all copies verified byte-identical.

## 1.18.19 — 2026-07-02 — PRODUCT INTEGRITY: no green "safe to send" on a zero-detection document

A product review found the single most damaging failure mode for a tool sold on
"your client data is protected": the verdict surfaces showed a **green "✓ sûr à
envoyer" / "✓ Aucune PII détectée"** on documents where the engine had detected
**nothing** — and in the regex-only / no-ML config, "found nothing" on a real
free-text document very often means a name or address was simply **MISSED**. In
the review battery, **26 of 27 leaking documents showed the green verdict.** The
verdict is computed by the same recognizers that missed the entity, so it
structurally cannot catch its own false negatives.

**This release reframes the verdict honestly. Detection/masking is UNCHANGED — a
document that was masking correctly still masks byte-for-byte identically. This is
purely the MESSAGE/flag.**

- **New engine state machine** (`AnonymizationResult.verdict_state`): one canonical
  source of truth for every surface — `leak | low_confidence | zero_detection |
  nothing_to_do | masked_ok`.
- **`zero_detection`** — a SUBSTANTIAL document (real prose: ≥ 8 words and ≥ 40
  chars) where ZERO entities were found. This is now a distinct **CAUTION** state:
  "⚠️ Aucune donnée identifiante détectée — cela ne garantit PAS l'absence de PII.
  Une relecture humaine est requise avant envoi." Visually distinct amber banner
  (`verdict--caution`), never green.
- **`safe_to_send` now returns `False`** for the zero-detection case (with a
  distinct reason via `verdict_state`), so every consumer that only reads the bool
  (dossier `all_safe`, dashboard flag, bench) stops certifying it safe. A `True`
  now means the honest best case — entities were found and all masked — still
  framed as "revue conseillée", never an absolute guarantee.
- **`nothing_to_do`** — a trivially short no-PII input (below the substantial bar)
  stays genuinely safe/green: "rien à anonymiser".
- **All surfaces updated**: engine `verdict_fr`, webapp `result.html` verdict banner
  + mapping-table hint, MCP note (`bubble_shield_mcp.py`), artifact
  (`make_artifact.py`), and the `bubble-shield-anonymize` skill guidance.
- **PostToolUse email hook honesty** (`posttool_anonymize.py`): the ambient
  regex-only, fail-open mail containment now states plainly it is **best-effort**,
  not the fail-closed guarantee the folder guard provides.
## 1.18.18 — 2026-07-02 — SECURITY (P0): close three raw-PII exfil bypasses of the PreToolUse guard

A security review found **three confirmed ways** to get raw client PII past the
guard, all reproduced with synthetic data (Jean Dupont). Each let a read of a file
inside a marked (`.bubble-shield.json`) client folder reach the model instead of
being rerouted through `bubble_shield_read`. All three fixed here.

- **P0-SEC-1 — guard fails OPEN on any uncaught exception.** `main()` wrapped only
  the event-JSON parse in try/except; everything after (`_load_config`, mail-guard,
  path extraction, `decide_block`, marker discovery) was unguarded. An unhandled
  error → Python exits **code 1 with no deny JSON** → per Claude Code hook semantics
  (only exit 2 or an explicit deny blocks) the tool **runs**. Reproduced with
  `tool_input` as a list (`AttributeError`) and `cwd` as an int (`TypeError`) — both
  exited 1 and would have allowed the tool. **Fix:** the entire decision body now
  runs inside a blanket `try/except Exception` that fails **closed** with a French
  deny (`🔒 Bubble Shield — erreur interne du guard, accès bloqué par sécurité.`).
  `SystemExit` from the legitimate `_deny`/`_allow` paths is re-raised so it isn't
  swallowed. The explicit early denies (malformed event) are kept — the wrapper is a
  backstop, not a replacement.

- **P0-SEC-2 — glob metacharacters in a parent segment bypass the Bash guard.** A
  bash command referencing a marked file via a glob in a segment at/above the marked
  folder (`cat /…/cl*/Dupont/avis.txt`) kept the literal `cl*` after `Path.resolve()`,
  so the marker walk-up never found the marker → **ALLOW**, while the shell expands
  the glob at runtime and reads the file. **Fix:** the command-path extractor now
  expands any token containing `* ? [ ] { }` (incl. brace and `**` recursive globs)
  against the real filesystem and runs each real match through the same
  `decide_block` walk-up. If nothing matches on disk, it fails **closed** by
  discovering any marked subtree under the longest glob-free prefix. Literal
  leaf-only globs (all parent segments literal) still deny — no regression.

- **P0-SEC-3 — generic `mcp__*` file tools were matched but never inspected.** The
  hook matcher (`…|mcp__.*`) runs the guard for every MCP tool, but `_candidate_paths`
  only extracted paths for the 6 native tools — so any other file MCP server (e.g.
  `mcp__filesystem__read_file(path=<marked>)`) yielded zero candidates and fell
  through to ALLOW. **Fix:** for any `mcp__*` tool (that isn't a mail/`*__bash`/our
  own sanctioned `bubble_shield_read`/`_write` tool), scan `tool_input` for
  path-shaped values across common keys (`path`, `file_path`, `uri`, `target`, …),
  list keys (`paths`, `files`, …), and a regex backstop over every string value, and
  gate each. Our own read/write tools are never treated as candidates (they are the
  safe, already-anonymised path).

**Tests:** `scripts/test_guard_bash_cwd_exfil.py` gains 24 assertions covering all
three fixes (glob variants `*/ ? [c] {…} **` + multi-segment + fail-closed
un-expandable, 5 generic-mcp cases incl. own-tool allow, 4 malformed-crash
fail-closed cases). Each new assertion was proven to FAIL on the pre-fix code and
pass after. Full suite (`test_guard.py`, `test_guard_marker.py`,
`test_guard_bash_cwd_exfil.py`) stays green; mcpb copy re-synced and re-packed.

## 1.18.17 — 2026-07-02 — FIX: pin the installer's online-fallback deps (no pywebview-6 crash)

`install-app.sh` has two dependency-install paths: an OFFLINE default (installs from the
pinned wheels in `vendor/wheels/`) and an ONLINE FALLBACK (used only when no interpreter
matching the vendored wheel ABI is found). The online fallback ran
`pip install fastapi uvicorn pywebview jinja2 pypdf python-multipart` **unpinned** — so it
would pull the **latest** from PyPI, e.g. **pywebview 6.x** against launcher code written and
tested for **pywebview 3.4**. That is exactly the pywebview 3.x/4.x major-API-drift crash
class fixed in 1.18.16 (the `window.events` AttributeError), relocated to the fallback branch.

- **Fix:** added `constraints.txt` at the repo root pinning every vendored-wheel version, and
  passed `-c constraints.txt` to **both** the offline and the online pip installs, so both
  resolve to the identical versions (pywebview stays 3.4). Verified: the pin forces 3.4 even
  when a 6.0 wheel is available; the offline path still installs clean from vendored wheels.

## 1.18.16 — 2026-07-02 — FIX: desktop app crashed on launch (pywebview 3.x/4.x events API mismatch)

**Found on-site at a real client's Mac** (their own Claude Code diagnosed it). The
install succeeded end-to-end — repo cloned, cp39 offline wheels installed against
stock 3.9.6, `Bubble Shield.app` created — but the app **crashed immediately on
open**, 100% reproducible on the default offline-install path:

```
File ".../launcher/__main__.py", line 172, in _run_webview
    window.events.loaded += _on_loaded
AttributeError: 'Window' object has no attribute 'events'
```

Root cause: `launcher/__main__.py` used the **pywebview 4.x** events API
(`window.events.loaded`), but `install-app.sh` vendors **pywebview 3.4**
(`vendor/wheels/pywebview-3.4-py3-none-any.whl`). In 3.x, events are attributes
directly on the window (`window.loaded`); the `window.events` namespace was only
introduced in 4.0. So every launch on the vendored wheel died before the GUI opened.

- **Fix (version-robust — works on both 3.x and 4.x, so a future wheel restaging in
  either direction can't re-break it; `launcher/__main__.py` only, version bumped
  together per project convention):** resolve the loaded event via whichever API is
  present — `getattr(getattr(window, "events", None), "loaded", None)` (4.x) then fall
  back to `getattr(window, "loaded", None)` (3.x), and only bind if found. (The
  `_on_loaded` hook is currently a no-op, so the binding is defensive/forward-looking,
  but keeping it robust is cheaper than re-litigating it next time the wheel moves.)
- **Verified against the real vendored 3.4 wheel** (installed from `vendor/wheels/`
  into a clean venv, not code review): the old line reproduces the exact reported
  `AttributeError`; the fixed logic resolves `window.loaded` (an `Event`) and `+=`
  binds cleanly.

## 1.18.15 — 2026-06-30 — FIX: desktop installer picks the interpreter matching the staged wheel ABI (not "newest")

**Second installer bug in the same release window** (the first was the 3.10-vs-3.9
gate, shipped in 1.18.13/14). Running the published `install-app.sh` one-liner
end-to-end (not just diffing it) surfaced it: the Python candidate search preferred
the **newest** `pythonN.M` on PATH. But `vendor/wheels/` only holds **cp39**-tagged
compiled wheels (pyobjc, pydantic-core, markupsafe). On a client Mac that happens to
have a newer Python ranking ahead of stock `/usr/bin/python3` (Homebrew/pyenv — or a
Homebrew `python3` shadowing stock entirely), the installer picked e.g. 3.12/3.14,
then the offline `pip install --no-index --find-links=vendor/wheels` failed with
`No matching distribution found for pyobjc-core` and no PyPI fallback. Stock-only
Macs were fine; **mixed Macs broke silently** — plausible even for a non-technical
CGP client whose Mac was once used to install anything.

- **Fix (note: this is `install-app.sh` only, not plugin/MCPB code — version bumped
  together per the project convention):**
  - The supported ABI(s) are now derived **dynamically from the actual
    `vendor/wheels/*.whl` filenames** (the source of truth — no hardcoded "39"; if
    the wheel set is ever re-staged for a different ABI this adapts automatically).
  - The interpreter is chosen because its ABI **matches the staged wheels**,
    regardless of PATH order — not because it's newest. The canonical stock path
    `/usr/bin/python3` is added to the candidate set so a shadowed-but-ABI-matching
    stock interpreter is still discovered (it is NOT hardcoded as the winner; it
    only wins on an ABI match).
  - Python selection now runs **after** the clone (the wheels it inspects only exist
    post-clone).
  - **Residual fallback (documented):** if NO interpreter matches the staged ABI
    (e.g. a Mac with only 3.11+ and no 3.9 anywhere), the installer falls back to the
    newest available `>=3.9` interpreter and installs **online from PyPI** (drops
    `--no-index`) with a clear French message explaining the network use — chosen over
    a hard error because failing to install at all is worse than one client
    occasionally needing the network.
- **Verified end-to-end (real installer runs, not code review):**
  - Stock-only PATH → still installs clean offline on 3.9.6 (no regression).
  - Homebrew python3.12 + python3.14 ahead of stock, stock shadowed → now selects
    stock 3.9.6, installs offline, builds the `.app` (was the broken case).
  - Idempotent re-run (pull path) → clean.
- **Regression test:** `tests/test_396_installer_abi_select.py` runs the real
  installer with an ABI-mismatched newer interpreter forced ahead on PATH and asserts
  the venv was built on an ABI-matching interpreter — **proven to FAIL on the pre-fix
  installer** (reproduces the exact `pyobjc-core` error) and pass after.

## 1.18.14 — 2026-06-30 — SECURITY (P0): close the `block_bash` cwd-anchoring exfil gap

**Highest-severity finding to date.** A real CGP client, security-testing Bubble
Shield on his own initiative, found that `mcp__workspace__bash` (Cowork's sandboxed
shell) could re-extract a real avis d'impôt (`file`, hex dump, then `tesseract`
OCR) from a `block_bash:true`-marked folder — despite the native Read tool being
correctly blocked. The bash command scan built its protected-path needles ONLY
from `_discover_marker_roots(cwd)`, which is **cwd-anchored**: when the bash tool's
`cwd` was an unrelated session/workspace root (the normal Cowork case), marker
discovery found nothing, the needle-set was empty, and a command containing the
literal absolute path to a marked file sailed through silently — `block_bash`
became a no-op independent of the command's content.

- **Fix:** the Bash scan now extracts path-shaped tokens (absolute `/…`, home
  `~/…`, and slash-bearing relative paths) directly from the command string and
  runs each through the SAME robust per-path marker walk-up the file-tool path uses
  (`decide_block` → `_find_marker_root`), which is **cwd-INDEPENDENT** for absolute
  paths. `tesseract /a/b/Dossier/avis.jpg stdout` with `cwd=/Users/joris` now
  resolves the marker on the file's own ancestry and DENIES. The legacy
  cwd-anchored needle scan is kept as defense-in-depth (it still catches relative
  commands where cwd is informative). `allow_paths` / extension-exemption for Bash
  inherit the same robust per-path resolution (no longer cwd-anchored either).
- **Per-marker `block_bash`:** a `block_bash:false` set in a folder marker is now
  honoured (previously only the GLOBAL config's `block_bash` was read, so the
  documented folder-level setting was silently ignored). The read/write guard
  still protects such a folder; only the deliberate bash opt-out is respected.
- **Residual policy (documented):** a BARE filename with no slash (`cat avis.jpg`)
  whose cwd is itself inside a marked folder is deliberately allowed — extracting
  a bare word as a path is indistinguishable from any other shell token without a
  real lexer, and fail-closing every bare word would brick routine shell use. The
  dangerous absolute-path-from-unrelated-cwd case is always denied.
- **Regression test:** `scripts/test_guard_bash_cwd_exfil.py` (22 cases) locks the
  fix in — proven to FAIL on the pre-fix code (9 failures incl. the exfil case)
  and pass after. Second time a bash-coverage gap bit a real client; this stays
  bulletproof.

## 1.18.13 — 2026-06-30 — #396 fix the "pypdf manquant" client bug (PDF/image read)

**A real CGP client could not read PDFs/images** — Bubble Shield raised
"pypdf manquant -- pip install pypdf" even though pypdf IS vendored in the
published `.mcpb`. Two stacked causes, both masked on a contaminated dev machine
(global `pypdf` + `typing_extensions` in user site-packages hid them):

- **Path resolution:** `bubble_shield_mcp.py::_anonymise_file` inserted only
  `_scripts_dir()` on `sys.path` before importing the extractor, never `_vendor()`
  — so the extractor's import of the vendored pypdf relied on a single
  `CLAUDE_PLUGIN_ROOT` env var resolving correctly. Now inserts `_vendor()` too,
  matching every other call site in the file.
- **Self-heal in the extractor:** `bubble_shield_extract.py`'s lazy
  `from pypdf import PdfReader` now retries once after putting the file's *actual*
  sibling vendor dirs on `sys.path`, instead of trusting one env var, before
  raising "pypdf manquant".
- **Missing vendored dependency:** the vendored pypdf (6.x) imports
  `typing_extensions` (Self/TypeAlias/TypeGuard) on Python < 3.11, but
  `typing_extensions` was NOT vendored — so on a clean client Mac (stock
  /usr/bin/python3 3.9.6, no global typing_extensions) the import failed with the
  same opaque "pypdf manquant" error. Now vendored as `vendor/typing_extensions.py`.
- **Regression test (`tests/test_396_pypdf_vendor_selfheal.py`):** runs the
  import/extraction in a subprocess via stock `/usr/bin/python3 -S` with a minimal
  env and a deliberately-wrong `CLAUDE_PLUGIN_ROOT`, so a dev machine's global
  packages can no longer mask this bug class on the next release.

(The desktop-app installer fix — Python 3.9 gate + offline wheel vendoring,
issue #396 Part 2 — ships in the app repo, not the plugin bundle.)

## 1.18.6 — 2026-06-29 — re-vendor #348 precision-filter + #345 atomic-vault

**Re-vendor of the #348 precision filter and the #345 atomic vault save into the
shipped bundle.** The engine fixes had landed in the repo-root `bubble_shield/` but
were not yet vendored into the plugin bundle, so the shipped server ran stale code.
This release re-vendors the engine and re-packs the MCPB.

- #348 — precision filter: gazetteer-wins precedence across all negative filters and
  word-boundary org matching, closing a substring false-positive leak. Ships the new
  `common_words.py` and `safe_words.py` stoplists, the word-boundary-aware
  `allowlist.py` (`_short_token_allowlists`), the augmented `policy.py`, and the
  daemon `bubble_shield_mcp.py` `_apply_negative_filters` / `_composed_match_filter`.
- #345 — atomic vault save: `vault.py` now writes to a `.tmp` sidecar then
  `os.replace()`s it into place, eliminating the truncated-vault window on crash/interrupt.

## 1.18.5 — 2026-06-27 — feat/desktop-tier2-integration (Tier-2 desktop app groundwork)

Desktop app Tier-2 groundwork: candidate-signal sidecar (sub-threshold spans recorded
host-side for human review, agent output unchanged) + local review-queue store. The
companion config/review desktop app is a separate host-native artifact.

- `candidate_sidecar.py`: after each anonymization, entities detected below the confidence
  threshold are written to `~/.bubble_shield/candidates/<mission>.candidates.json` (host-only,
  chmod 600, atomic write, fail-open — the agent-facing output is never touched).
- `review_queue.py`: local SQLite-backed store for the HITL review queue; confirm/dismiss
  actions feed the gazetteer and drain the queue (webapp/desktop-app component).
- Phase 2 native launcher: pywebview window wrapping the existing config/review webapp.
- Phase 3 review-queue UI: HITL inbox + audit log (Tier-2 UX, desktop-app component).
- Warning banner when policy keeps identifying types (NOM, etc.) visible (#334).
- Fix: test isolation — test_334 now loads the real posttool_anonymize (not a stub)
  when the module is importable, preventing NERD_URL attribute error in daemon tests.

## 1.18.4 — 2026-06-27 — feat/326-known-pii-gazetteer (local self-improving known-PII gazetteer)

Add a local self-improving known-PII gazetteer: once a name is confirmed as PII
(high-confidence detection or explicit confirmation), it is masked deterministically
in every subsequent document — including bare, low-context occurrences that the NER
model alone scores below threshold. Anti-poisoning gate (only high-confidence/confirmed
entries enter); the gazetteer is local, stored outside the repo, and never shared.
Checksum-validated structured PII still takes precedence on overlaps.

## 1.18.3 — 2026-06-26 — fix/318-overlap-span-drop (name-recall leak + truncation)

**Security: close trailing-forename leak on administrative forms; improve recall on bare names.**

Fix a name-recall leak on administrative forms: when the detector returned overlapping
person-name spans (a full SURNAME FORENAME FORENAME block scoring lower than a shorter
sub-span), the overlap resolver kept the shorter span and a trailing forename leaked in
clear. The resolver now extends a kept person-name span to cover the full overlapping
name. Also reduced the GLiNER chunk size to eliminate silent token-window truncation on
form-fill (dotted) sections, and lowered the name-detection threshold — both improve
recall on bare/low-context names without over-masking.

Fixes:
- `_extend_nom_containment()` — after the NOM overlap resolver selects the winning span,
  any overlapping NOM parent span that contains the winner is used to extend the winner's
  end offset to cover the full name block; trailing forenames no longer leak.
- `DEFAULT_CHUNK` reduced 1500 → 1000 chars — eliminates silent 384-token GLiNER
  truncation on dotted form-fill sections; 1000 chars produces ≤~200 word-tokens after
  dot-compression, safely within the model's 384-token context window.
- Detection threshold reduced 0.45 → 0.30 — improves recall on bare/low-context names
  (single forename, surname-only) without triggering measurable over-masking on the
  regression suite.
- Dot-compression — dotted sequences (…………) are collapsed before tokenisation, reducing
  token count for form-fill sections and ensuring the name context is not crowded out.

## 1.18.2 — 2026-06-26 — fix/daemon-onnx-detection (lazy self-test hardening)

**Security: close the "healthy but blind" daemon failure class for --no-warm starts.**

The NER daemon could report healthy (`/health ok=true`) while detecting nothing.
Two root causes:

1. **ONNX-only model directory**: when the local model directory contained only
   ONNX weights (`onnx/model_quantized.onnx`) with no PyTorch weights
   (`pytorch_model.bin` / `model.safetensors`), `GLiNER.from_pretrained()` raised
   `FileNotFoundError` which `_load_model()` swallowed, caching `None`. Every
   subsequent `gliner_matches()` call returned `[]`. The daemon answered `/health`
   with `ok=true, warm=true` while every `/detect` silently returned zero matches —
   names leaked while the tool reported the document safe.

2. **`--no-warm` blind-daemon hole**: even with the ONNX-fix in place, a daemon
   started with `--no-warm` skipped the detection self-test, so `_selftest_result`
   stayed `None`, `/health` reported `self_test: null`, and `_daemon_up()` treated
   `null` as UP (fail-open). A `--no-warm` daemon with a broken model could therefore
   answer `/detect` with `[]` while looking healthy.

Fixes:
- `_gliner_model_id()` — when the local model directory lacks PyTorch weights, fall
  back to the PyTorch HuggingFace repo id stored in the manifest (`pytorch_model_id`
  field, or hard default `urchade/gliner_multi_pii-v1`), which GLiNER can load
  correctly. The daemon now selects a working model when the local dir is ONNX-only.
- `warm_up()` — runs a detection self-test (a known synthetic name must produce a NOM
  match) and stores the result in `_selftest_result`; `/health` exposes
  `self_test: "pass"/"fail"`, and `_daemon_up()` gates on it — a daemon that answers
  `/health` but cannot actually detect is now treated as DOWN and reads fail closed
  instead of leaking.
- **Lazy self-test on first `/detect`** (this release) — if the daemon started with
  `--no-warm`, `_selftest_result` is `None` until the first real `/detect` call, at
  which point the self-test runs lazily and caches the result. After the first
  detection call, `/health` reports the real `pass`/`fail` state and `_daemon_up()`
  gates correctly. This closes the blind-daemon hole for `--no-warm` starts without
  changing the warm-path behavior. Production LaunchAgent does not pass `--no-warm`;
  this fix hardens the edge case.

## 1.18.1 — 2026-06-26 — fix/gliner-nom-span-dropped

**Privacy: close a name-recall leak where neural-NER (GLiNER) NOM spans were
dropped by the profile-sweep trust gate.**

Previously, when the soft-ML detector (GLiNER / OpenAI-PF, priority ≤ 5) returned
a NOM span for an all-caps `SURNAME FORENAME` block on an administrative form — a
pattern that carries no civility title and whose forename is absent from the
gazetteer — the span was silently discarded by `ClientProfile.learn()` because its
confidence score (0.45–0.70) fell below the 0.85 regex-NOM trust threshold.  As a
result, the person's name detected in one section of the document was never seeded
into the doc-level repetition sweep, so the same name block appearing verbatim in
a separate address block (or page header) survived in clear.

Fixes:
- `DetectedEntity` now carries a `priority` field so the source recognizer's tier
  is propagated all the way through to `profile_sweep.ClientProfile.learn()`.
- `ClientProfile.learn()` trusts soft-ML NOM spans (priority ≤ 5) regardless of
  score; it also normalises double-space and trailing-newline PDF extraction
  artifacts in span boundaries before storing the value and deriving sweep tokens.
- `engine._detect()` builds a mini `ClientProfile` from soft-ML NOM spans after
  `resolve_overlaps`, sweeps the full document text for uncovered occurrences, and
  folds them back into `raw` before the final re-resolve — ensuring the address-block
  copy of an all-caps administrative name is masked even when it fell outside the
  GLiNER chunk window.

## 1.18.0 — 2026-06-26 — fix/ner-fail-closed-gate
**Security: fail-closed when fine-grained NER (GLiNER) is unavailable.** Previously,
if the NER daemon was down, the anonymiser silently fell back to regex-only mode and
still returned a "safe-looking" result — but regex alone cannot catch context-free
name blocks (e.g. an all-caps `SURNAME FORENAME` line on an administrative form with
no title or label), so identifying names could survive in clear while the tool reported
the document safe. Now:
- `bubble_shield_read` / `bubble_shield_anonymize_text` **fail closed** when the NER
  daemon is down: they return an error (no anonymised body, no raw PII) instead of a
  regex-only result. A document can never be certified safe without live fine-grained
  detection.
- New **`bubble_shield_status`** tool reports NER state (`ner`, `model`,
  `ml_pack_installed`, `daemon_reachable`, `launchagent_loaded`) so the agent can
  verify detection is active before processing, and to diagnose daemon liveness vs
  reachability.
- **SessionStart re-arm**: each new session health-checks and re-spawns the NER daemon
  if down (fail-open on the spawn — never blocks session start; the per-call gate is
  the safety guarantee).

## 1.17.3 — 2026-06-24 — fix/genericize-demo-client

- **privacy**: replaced a real client name in the `bubble-shield-onboarding`
  skill with a synthetic placeholder ("Jean DUPONT" / "DUPONT") — no real PII in
  the public repo. Demo file example is now
  `DCC - Monsieur Jean DUPONT - 2026-02-19.pdf` with a parenthetical clarifying
  it is an example the operator swaps for their real client file. Skill text only;
  no logic, tool, or flow changes.

---

## 1.17.2 — 2026-06-24 — feat/283-onboarding-demo-flow

- **#283** (`feat/283-onboarding-demo-flow`): Added "Première prise en main"
  guided demo flow to `bubble-shield-onboarding` skill — 6-step elicitation
  sequence (GLiNER install → OCR install → folder marking → DCC demo, two beats:
  AI reads blind, then produces the real document via `bubble_shield_write`).
  Trigger words wired into skill description. Skill text only — no new tools,
  no code, no widget. Honesty invariants preserved (folder-first; no
  everywhere/mail overclaim).

---

## 1.17.1 — 2026-06-24 — fix/honesty-corrections

See git history for v1.17.1 details.

---

## 1.17.0 — 2026-06-24 — integrate/v1.17.0: #267+#269+#276+#280(rev3) composed

Consolidated integration of 4 reviewed+approved features:

- **#269** (`fix/269-tableformer-cache`): OCR setup pre-caches TableFormer model (`setup_ocr.py`)
  — prevents silent table-OCR miss on HF_HUB_OFFLINE runtime after fresh install.
- **#276** (`feat/276-ocr-route-garbled`): `_is_garbled_extraction` detects glued PDF text
  and reroutes to OCR (`bubble_shield_extract.py`) — eliminates the entire glue-artifact class.
- **#267** (`chore/267-harden-surname-guard-v2`): expanded `_COMMON_FRENCH_SURNAMES` to ~186
  entries + `bypass_common_surname_guard` parameter so RAISON_SOCIALE-anchored surnames
  mask everywhere (`structured_ext.py`).
- **#280 rev3** (`fix/280-filename-footer-leak`): `filename_footer_matches` returns
  `(matches, footer_nom_spans)` tuple; footer-sourced spans are excluded from the
  corroboration pool in `doc_level_person_repetition_matches` — closes the self-corroboration
  loop that caused brand/insurer names to over-mask body-wide (`structured_ext.py`,
  `bubble_shield_mcp.py`, `__init__.py`).

All 4 features verified green: 255+ pytest tests, 19/19 MCP, 19/19 posttool.
MCPB re-packed at 1.17.0 (9 tools, no .bak). All vendor copies in sync.

---

## 1.16.5 — 2026-06-24 (fix #280 rev 3: close self-corroboration loop in filename-footer masking)

### Rev 3 — self-corroboration bug fix

**Root cause (rev 2 confirmed bug):** Layer 1 `filename_footer_matches()` emits a footer
NOM for every filename token (incl. brand names like ZEPHYRA). Layer 2
`doc_level_person_repetition_matches()` was building its corroboration pool
`nom_detected_words` from the SAME `found` list — which already included those Layer-1
footer NOMs. So ANY filename token in the footer self-corroborated → seeded body-wide →
over-masked insurer/brand names.

**Proof:** "ZEPHYRA DUPONT - DER.pdf", body mentions ZEPHYRA (insurer Multisupport) →
ZEPHYRA masked body-wide (0 un-masked body occurrences). PREDICA passed rev2 ONLY because
it's on the stop-list — a false pass, not a real fix.

**Fix (rev 3):**
- `filename_footer_matches()` now returns `(matches, footer_nom_spans)` where
  `footer_nom_spans` is a `frozenset` of `(start, end)` tuples covering all emitted NOMs.
- `_detector` in `make_structured_detector` captures this frozenset and passes it to Layer 2.
- `doc_level_person_repetition_matches()` accepts `footer_nom_spans` parameter and EXCLUDES
  those spans when building `nom_detected_words`. Only independent body-recognizer NOMs
  (civility "M. DUPONT", form-label, signataire) count for corroboration.

**Result:**
- ZEPHYRA (insurer): footer NOM excluded from pool → no independent NOM → NOT corroborated
  → NOT seeded body-wide → body occurrences survive. Footer still masks via Layer 1 ✓.
- DUPONT (client, "M. DUPONT" body): civility NOM in pool (not footer) → corroborated →
  body-wide mask ✓.
- TESTONI + "M. TESTONI" signal: independent civility NOM → corroborated → body mask ✓.
- Footer-only TESTONI (no body signal): Layer 1 positional masks footer; body survives ✓.

**Files changed (rev 3):**
- `bubble_shield/structured_ext.py` + 2 vendor copies (3 in sync):
  - `filename_footer_matches` → returns `(matches, footer_nom_spans)`
  - `make_structured_detector._detector` → captures and passes `footer_nom_spans`
  - `doc_level_person_repetition_matches` → `footer_nom_spans` parameter, exclusion logic
- `tests/test_280_filename_footer.py` → D15 updated (unpack tuple), D16 replaced with
  4 regression tests: D16a ZEPHYRA, D16b KORRIGAN, D16c corroborated TESTONI, D16d footer-only.
- D5 round-trip: updated to use "M. TESTONI" for independent corroboration.
- MCPB re-packed at 1.16.5 (no version bump).

**Test suite (rev 3):** 255 tests, all green (4 new D16 regression tests added).
Scripts: posttool 19/19, MCP 19/19, test_259/264/266/267/273/275 all pass.

---

## 1.16.5 — 2026-06-24 (fix #280 rev 2: corroboration + positional — over-mask ship-blockers closed)

### High-severity fix: systematic client-name leak via footer boilerplate (#280)
### Rev 2 (same release, same version): over-mask ship-blockers closed

**Root cause (#280):** Every signed CGP PDF ends with:
  `"Page de signatures complémentaire au document DURAND Théophile - DER 012026..."`
The client's full name was leaking verbatim in this footer. No content recognizer caught it.

**Original fix (rev 1):** Seeded ALL non-stop-list filename tokens body-wide.
**Problem (reviewer-proven):** The stop-list is inherently incomplete. Major FR insurers
PREDICA, HELVETIA, PREVOIR, ARIAL, AG2R, ALLIANZ, MONCEAU, CNP, SEQUOIA were not in it.
"PREDICA DUPONT.pdf" → seeded PREDICA → masked "PREDICA" (insurer name) in body everywhere.
Similarly: BOURSE missing → over-mask; FINAL missing; LIASSE/FISCALE missing.

**Rev 2 fix — corroboration + positional approach (wins over extending the stop-list):**

Layer 1 — **`filename_footer_matches(text, candidates)`** — new positional function:
  Directly emits NOM matches where filename person-tokens appear in footer boilerplate
  (`"au document <name-fragment>"`). Covers the pure-footer case (name ONLY in footer,
  no body corroboration needed) without body-wide seeding. Safe even for PREDICA —
  the footer reference to the filename is correctly masked, but no body-wide seed.

Layer 2 — **Corroboration in `doc_level_person_repetition_matches`**:
  A filename candidate token is promoted to a body-wide seed ONLY when it is
  corroborated — i.e. the token already appears in a NOM match detected by another
  recognizer (civility, form-label, signataire, or the footer NOM from Layer 1).
  - "DUPONT" from civility "M. DUPONT" → NOM → corroborated → body-wide seed → masks ✓
  - "PREDICA" → no NOM detection anywhere → NOT corroborated → NOT seeded → body untouched ✓
  - Footer-only case: Layer 1 NOM provides corroboration for Layer 2 → body seed activates ✓

Belt-and-suspenders: stop-list extended with reviewer-missing tokens:
  PREDICA, HELVETIA, PREVOIR, ARIAL, AG2R, ALLIANZ, MONCEAU, CNP, SEQUOIA,
  BOURSE, FINAL, FINALE, LIASSE, FISCALE, and other major FR insurers.

**Recall preserved:** TESTONI (footer-only) → Layer 1 positional catches footer ✓.
TESTONI (body+footer, via "M. TESTONI") → Layer 1 covers footer, Layer 2 covers body ✓.

**1. `extract_person_tokens_from_filename(basename)`** — extended stop-list (rev 2).

**2. `filename_footer_matches(text, candidates)`** — new, emits NOM for footer-quoted tokens.

**3. `make_structured_detector(filename_basename="")`** — now calls `filename_footer_matches`
   before the repetition pass (Layer 1), so footer NOMs are in `found` for corroboration.

**4. `doc_level_person_repetition_matches(text, found, filename_seeds=None)`** — rev 2:
   corroboration filter before body-wide seeding.

**5. `bubble_shield/__init__.py`** — `__version__` aligned to 1.16.5 (was stuck at 0.2.0).

**6. Threading in `bubble_shield_mcp.py`** (carried from rev 1).

**Files changed:**
- `bubble_shield/structured_ext.py` + 2 vendor copies (3 in sync)
- `bubble_shield/__init__.py` + 2 vendor copies (version 0.2.0 → 1.16.5)
- `plugin/bubble-shield/scripts/bubble_shield_mcp.py` + mcpb/server copy (3 in sync)
- `plugin/bubble-shield/.claude-plugin/plugin.json` — version 1.16.5 (unchanged)
- `tests/test_280_filename_footer.py` — 6 new tests (D11–D16): PREDICA/HELVETIA body
  no-mask, BOURSE/LIASSE/FISCALE no-mask, positional footer, corroboration feedback.
- MCPB re-packed at 1.16.5.

**Test suite:** 246 existing + 6 new = 252 tests, all green.
Scripts: test_257/259/264/266/267/273/275/posttool/256/260 — all pass.

## 1.16.4 -- 2026-06-24 (chore #267 v2: recall regression fix — raison-sociale bypass)

### Recall regression fix (#267-v2, reviewer-reported)

**Root cause (regression introduced by #267 v1):** `_person_name_seeds()` applied the
`_COMMON_FRENCH_SURNAMES` guard to ALL lone-token seeds, including those derived from a
confirmed RAISON_SOCIALE match (e.g. "SELARL GARCIA TESTONI"). A newly-listed common
surname that IS the client's name would be excluded from lone-token seeds, causing
standalone body repetitions ("GARCIA agit seul", "GARCIA a décidé") to LEAK even
though the header ("SELARL GARCIA TESTONI") was correctly masked.

**Fix:** Added `bypass_common_surname_guard=False` parameter to `_person_name_seeds()`.
The RAISON_SOCIALE path in `doc_level_person_repetition_matches` now calls with
`bypass_common_surname_guard=True` — the company match already confirms the token IS
the client's name, so the "common word in prose" concern does not apply.

**Both properties preserved:**
1. Known-client surname (anchored to raison sociale) masks everywhere, including
   standalone body repetitions and right-glued PDF artifacts. (RECALL)
2. Unanchored common surname in a doc with NO matching company → NOT masked. (PRECISION)

**Scope of change:**
- `structured_ext.py`: `_person_name_seeds()` new `bypass_common_surname_guard` param
  + `doc_level_person_repetition_matches()` RAISON_SOCIALE call-site uses `bypass=True`.
- `test_267_surname_guard.py`: D23 corrected (lone GARCIA IS in seeds via bypass);
  D32-D36 added (standalone LECLERC masked, standalone PEREZ unanchored NOT masked,
  PEREZ-as-client masked everywhere).
- `test_275_right_glue.py`: D10 updated (LEBLANC as known-client name IS right-glue-
  masked — old "NOT masked" expectation was based on the pre-fix behavior).
- All three copies synced; MCPB re-packed at 1.16.4.

**Data-only change from #267 v1 still in place:**
- `_COMMON_FRENCH_SURNAMES` expanded from ~60 to ~186 entries (GARCIA, NGUYEN, PEREZ,
  LECLERC, CHEVALIER, FERNANDEZ, GONZALEZ, MARTINEZ, LAURENT, SIMON, MICHEL, ROUX,
  FONTAINE, etc.) — guard still applies on the unanchored/prose path.

## 1.17.0 — 2026-06-24 (feat #276: route GARBLED-extraction PDFs through OCR)

### Feature — GARBLED native PDF extraction routed through OCR pack (#276)

**Root cause:** pypdf extracts text from the liasse fiscale but GLUES words together
(no spaces between tokens): "gérantETESTONI", "FAKENAMESignature". Per-boundary fixes
(#273 left-glue, #275 right-glue) catch most of these, but a 4-char forename like
"FAKENAME" cannot be safely caught without lowering the length floor and over-masking
common names (JEAN, PAUL, etc.).

**DURABLE fix:** when native PDF extraction returns non-empty text that looks GARBLED
and the OCR pack is installed, re-extract via OCR (clean, properly-spaced,
layout-aware text) and anonymise THAT — eliminating the entire glue-artifact class
at once.  Fail-open: if the OCR pack is absent or fails, the native text is used
as before (with the #273/#275 per-boundary fixes still applied).

**Heuristic `_is_garbled_extraction(text) -> bool`** — all three signals must fire:
1. **Long-token rate ≥ 3 %**: tokens > 25 chars (excluding URLs/email addresses)
   as a fraction of all tokens.  Glued liasse text produces many super-long tokens
   ("SIGNATAIREFAKENAMEETESTONISignature"); normal French prose has almost none.
2. **Low space density**: fewer than 1 space per 10 characters.  Normal French prose
   is ~1 space per 5–6 chars; garbled liasse extractions are much denser.
3. **CamelCase-glue signature ≥ 3**: occurrences of `[a-z][A-Z]` or `[A-Z]{2,}[a-z]`
   transitions.  These are the exact signature of PDF-extraction word-fusion:
   the casing of one word collides with the next.  URLs/headers don't produce this.

**Conservative by design:** all three signals must fire together (AND logic).
Any single signal alone would be too noisy.  When unsure → return False (keep native
text).  False-positive (OCR a clean doc) is a perf/recall cost; false-negative
(miss garbled) just falls through to #273/#275 — so bias toward NOT-garbled.

**Wiring in `extract_pdf_text`:**
- After pypdf extraction yields non-empty text, call `_is_garbled_extraction(text)`.
- If True AND `_ocr_pdf_if_pack_present` returns text → return the OCR text.
- The `[OCR]` quality note is already prepended by `_ocr_pdf_if_pack_present`, so
  callers see the caveat automatically.
- If False OR OCR unavailable/fails → return native text unchanged (current behaviour
  + #273/#275 per-boundary fixes still apply).

**Location:** `bubble_shield_extract.py`
- `_is_garbled_extraction()` — new function added before `extract_pdf_text` (line ~137).
- `extract_pdf_text()` — garbled-route block added after the empty-text check.

### Test `scripts/test_276_garbled_route.py` — 22/22

- **Test 1:** garbled liasse-like text (glued tokens, long tokens, CamelCase
  transitions) → `_is_garbled_extraction` = True (2 fixtures: GARBLED_TEXT,
  DENSE_GARBLED with tokens > 25 chars).
- **Test 2:** clean texts → `_is_garbled_extraction` = False (7 cases: clean French
  prose, ALL-CAPS headings, long URL, long legitimate words, empty, single word,
  label-value pairs).
- **Test 3a:** garbled native text + OCR pack present (mocked) → OCR text used;
  `[OCR]` tag present; mock called exactly once.
- **Test 3b:** garbled native text + OCR pack absent (mock returns None) → native
  text returned, no crash (fail-open verified).
- **Test 4:** clean native text → OCR mock NOT called (clean docs never OCR'd).
- **Test 5:** edge cases (one-artifact-in-otherwise-clean doc, all-caps non-glued,
  mixed doc) — heuristic does not crash; all-caps-clean correctly returns False.

### Note on real-liasse confirmation
The real-liasse end-to-end test (re-running the actual liasse through OCR) requires
the OCR pack + model installed.  Tony to confirm post-merge that the liasse is now
extracted cleanly and "FAKENAME" is masked.  All tests in the PR use synthetic PII.

### All copies synced + MCPB re-packed at 1.17.0 (9 tools, no .bak)
- Root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/` (engine unchanged for this PR).
- `scripts/bubble_shield_extract.py` → `mcpb/server/scripts/bubble_shield_extract.py`.
- `scripts/test_276_garbled_route.py` → `mcpb/server/scripts/test_276_garbled_route.py`.
- MCPB re-packed (9 tools intact, no .bak).

### All suites green
`test_276_garbled_route` 22/22, `test_275_right_glue` 47/47,
`test_273_glued_token` 32/32, `test_266_person_name_corporate` 57/57,
`test_264_repeated_company` 31/31, `test_259_corporate_kyc` 20/20,
`test_257_form_layout` 42/42, `test_bubble_shield_mcp` 19/19,
`test_posttool_anonymize` 19/19, `test_256_daemon_path_fail_loud` 10/10,
`test_260_ocr` green, `test_option_b_e2e` pre-existing Python 3.9 compat skip.
pytest 229/229.

## 1.16.4 — 2026-06-24 (fix #269: pre-cache TableFormer at OCR setup)

### Bug fixed — TableFormer (table-structure model) not pre-cached at setup (risk:LOW, table OCR miss)

- **Root cause (#269):** The OCR setup warm-script used `do_table_structure=False`, so the
  TableFormer model (which lives in the same `docling-models` HF repo as the layout model
  but is fetched separately only when `do_table_structure=True`) was never downloaded during
  setup.  On a fresh install with `HF_HUB_OFFLINE=1` at runtime, any table-heavy scanned
  PDF silently produced zero table output (cache-miss → fail-closed, no crash, no privacy
  risk — just missing tables).  The review of #260 only passed because the test machine
  already had `docling-models` cached locally.

- **Fix:** `_WARM_MODEL_SCRIPT` now uses `do_table_structure=True` (aligned with the runtime
  setting), so both the layout model AND TableFormer are exercised and downloaded in the
  same `DocumentConverter()` instantiation during setup.  The `~750MB` estimate in the
  docstring is updated accordingly.

- **Sentinel hardening:** `ensure_models_cached()` (renamed from `ensure_layout_model_cached`,
  old name kept as alias) writes `layout_model_cached.flag` ONLY after the warm script exits
  with `returncode=0` and `stdout.startswith("OK")`.  A partial cache (e.g. TableFormer
  download error → non-zero returncode) raises `RuntimeError` and leaves the sentinel absent,
  so the next setup re-run picks up from scratch.

- **Privacy guarantee unchanged:** `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` still
  enforced in every runtime subprocess invocation (Test 0 re-confirmed).

- **Tests extended (`test_260_ocr.py`):**
  - Test 3 (#269): mocked subprocess returns `"OK models cached"` → sentinel written.
  - Test 4 (#269): mocked subprocess returns non-zero → RuntimeError raised, sentinel absent.
  - All 5 tests pass (Tests 0–4).

- **Note:** #267 also targets 1.16.4 — if both land, Tony resolves at merge; this PR uses
  1.16.4 as instructed.

## 1.16.3 — 2026-06-24 (fix #275: right-glued forename leak — mirror of #273)

### Ship-blockers fixed (v2 — over-mask guards, 2026-06-24)

Two over-masking regressions found in PR review. Both fixed in the same version
(no version bump — SAME 1.16.3, hotfixed before merge):

**Ship-blocker 1 — Forename over-mask** (ANDREA→ANDREAssistant, CLAIRE→CLAIREment):
- Root cause: common French forenames (len≥6) are frequent word-prefixes in French.
  The right-glue pass would fire for ANDREA in "ANDREAssistant" (next char 'A' = letter),
  masking the legitimate French word instead of just the client name.
- Fix: added `_COMMON_FRENCH_FORENAMES` exclusion set (~80 entries: CLAIRE, JULIEN,
  ANTOINE, ANDREA, ISABELLE, NATHALIE, SANDRINE, CAROLINE, CHRISTELLE, THEOPHILE,
  FREDERIC, NICOLAS, STEPHANE, CATHERINE, VERONIQUE, DOMINIQUE, EMMANUEL, GUILLAUME,
  etc.). Seeds in this set do NOT get the right-glue pass. Standard + left-glue (#273)
  passes still fire for these seeds (they're still masked when isolated or left-glued).

**Ship-blocker 2 — Seed-prefix-of-longer-word** (TESTONImania, TESTONIal):
- Root cause: the right-glue check fired for ANY following alphabetic char, including
  lowercase continuations that form a single inflected French word (TESTONImania,
  CLAIREment, ANTOINEtte, JULIENne).
- Fix: the right-glue pass now only fires when the FOLLOWING char is UPPERCASE
  (`[A-Z\xc0-\xd6\xd8-\xde]`). CamelCase = PDF extraction glue artifact
  ("FAKENAMESignature", "FAKENAMESignature" — capital S). Lowercase continuation =
  inflected French word — never mask.
- `structured_ext.py` line: `re.match(r"[A-Z\xc0-\xd6\xd8-\xde]", text[end])`.

**Both guards combined:** forename exclusion + uppercase-next-char.

**Verified still masks (real artifact):** "FAKENAMESignature" (F, not a forename),
"TESTONIDocument" (uppercase D), "FAKENAMESignature" (FAKENAME len=4 < 6, still masked by
standard pass anyway). "ANDREA DUPONT" pair still masked via standard pair-seed pass.

**Verified does NOT mask (ship-blockers):** "ANDREAssistant" (ANDREA in forenames),
"CLAIREment" (CLAIRE in forenames + lowercase), "JULIENne" (JULIEN in forenames),
"ANTOINEtte" (ANTOINE in forenames), "TESTONImania" (lowercase 'm' next).

### Bug fixed (v1) — FORENAME LEAK when right-glued to following word by PDF extraction artifact (risk:HIGH)

- **Root cause:** the mirror of #273. PDF text extraction omits the space between
  a known forename/surname ("FAKENAME") and the FOLLOWING word ("Signature"),
  producing "FAKENAMESignature" with no whitespace. The standard right word-boundary
  check `(?![A-Za-z])` fails because "S" IS a letter immediately after the seed, so
  the known forename stayed in clear — the surname was already masked as a separate
  token. Real output observed: `⟦NOM_0002⟧ FAKENAMESignature`.

### Fix — Option B (detection fix) — `structured_ext.py` `doc_level_person_repetition_matches`

- For RAISON_SOCIALE-derived lone-token seeds (len >= 6, not a common surname AND
  not a common French forename), compile a `right_glued_pattern` with a loose RIGHT
  boundary but a strict LEFT boundary `(?<![A-Za-z\xc0-\xff])`.
- After the standard word-boundary + left-glue (#273) scans, run the right-glue
  pattern and emit any occurrence whose FOLLOWING character IS an UPPERCASE letter —
  confirming a genuine CamelCase PDF-glue artifact (not a French word continuation).
- **Match value:** only the seed itself (e.g. "FAKENAME" — 8 chars), not the following
  word ("Signature"). The following word survives in the anonymised output.
- Precision guards:
  1. Seeds shorter than 6 chars never enter `raison_sociale_lone_seeds`.
  2. Seeds in `_COMMON_FRENCH_SURNAMES` excluded by `_person_name_seeds()`.
  3. Seeds in `_COMMON_FRENCH_FORENAMES` excluded from right-glue pass (new, v2).
  4. Following char must be UPPERCASE (new, v2).
  5. Strict LEFT boundary: no partial-tail matches.
- **Location:** `structured_ext.py` `doc_level_person_repetition_matches`
  (right_glued_pattern gate + scanning loop, after the #273 left-glue pass).

### Note on signataire label lines
When `FAKENAMESignature` appears as the value of a signataire/gérant label
("Signataire : FAKENAMESignature"), the `signataire_matches` recognizer correctly
masks the ENTIRE value as NOM (it is the complete name field). The right-glue fix
provides additional coverage for occurrences in free-text / unlabeled positions
(headers, footers, free-text paragraphs).

### Test `scripts/test_275_right_glue.py` — extended (47/47)
- D1-D15: original right-glue tests (all pass).
- D16-D21: forename exclusion set — CLAIRE/JULIEN/ANTOINE/ANDREA/ISABELLE in set;
  FAKENAME NOT in set.
- D22-D24: ANDREA forename does NOT over-mask ANDREAssistant / JULIENne.
- D25-D26: ship-blocker 2 — TESTONImania NOT masked (lowercase next);
  TESTONIDocument IS masked (uppercase next).
- 47/47 passed.

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to both
  `scripts/` and `mcpb/server/scripts/`. MCPB re-packed at 1.16.3 (no .bak,
  OCR 9 tools intact).
- All suites green: `test_275_right_glue` 47/47, `test_273_glued_token` 32/32,
  `test_266_person_name_corporate` 57/57, `test_264_repeated_company` 31/31,
  `test_259_corporate_kyc` 20/20, `test_257_form_layout` 42/42,
  `test_bubble_shield_mcp` 19/19, `test_posttool_anonymize` 19/19,
  `test_256_daemon_path_fail_loud` 10/10, `test_260_ocr` green,
  `test_option_b_e2e` pre-existing Python 3.9 compat skip (str|None syntax).
  pytest 229/229.

## 1.16.2 — 2026-06-24 (fix #273: glued-token surname leak in liasse fiscale)

### Bug fixed — SURNAME LEAK when glued to preceding token by PDF extraction artifact (risk:HIGH)

- **Root cause:** in some liasse fiscale PDFs, text extraction omits the space
  between a preceding token (e.g. a POSTE/role word such as "gérant") and the
  following surname, producing "gérantETESTONI" — with a trailing artifact char
  ("E") belonging to the preceding word. The anonymised output therefore showed
  `⟦POSTE_0003⟧ESURNAME` (the POSTE token immediately followed by the bare
  surname). The doc-level person-repetition pass (#266) uses a strict left word
  boundary `(?<![A-Za-z])SURNAME` that fails here because "E" IS a letter, so
  the glued surname was never detected and leaked in clear.
  The same SURNAME was correctly masked in a separate, clean occurrence
  (`⟦NOM_0001⟧`) — confirming this is purely a boundary/extraction miss, not
  a vault or recognizer error.
  DCC .docx variants are CLEAN; this is liasse/PDF-extraction-specific.

### Fix — Option A + Option B (both applied)

**Option B (detection fix) — `structured_ext.py` `doc_level_person_repetition_matches`**
- Track which seeds came from RAISON_SOCIALE extraction separately as
  `raison_sociale_lone_seeds` (lone tokens with no space, length ≥ 6).
- For these seeds only, compile a second `glued_pattern` with NO left-char
  restriction (only the strict right boundary `(?![A-Za-z])` remains).
- After the standard word-boundary scan, run the glued pattern and emit any
  occurrence whose PRECEDING character IS alphabetic or a digit — confirming
  it is a genuine glue artifact the standard scan missed.
- Precision guards: seeds shorter than 6 chars or in `_COMMON_FRENCH_SURNAMES`
  are excluded by `_person_name_seeds()` BEFORE this code runs, so "MARTIN",
  "BLANC", "PETIT", etc. are never loose-boundary-scanned. The right boundary
  still prevents partial matches inside longer words (e.g. "TESTONIAN").
- **Location:** `structured_ext.py` `doc_level_person_repetition_matches`
  (seed collection loop + per-seed scanning loop).

**Option A (output normalisation) — `engine.py` `anonymize()`**
- Added `_GLUED_TOKEN_RE = re.compile(r"(⟧)([A-Za-z\xc0-\xff])")` at module
  level.
- After all token substitutions, apply `_GLUED_TOKEN_RE.sub(r"\1 \2", out)` to
  insert a space between `⟧` and any immediately adjacent alphabetic character.
  This fixes the output representation (`⟦POSTE_0003⟧ESURNAME` →
  `⟦POSTE_0003⟧ ESURNAME`) even if Option B missed the detection.
  Display normalisation only — does not change detection coverage.
- **Location:** `engine.py` (module-level constant + `anonymize()` post-loop).

### Precision (same guards as #264/#266 — over-mask is a ship-blocker)
- Common-surname lone-token seeds (MARTIN/BLANC/PETIT) excluded by
  `_COMMON_FRENCH_SURNAMES` BEFORE the loose-boundary pass — never scanned
  loosely.
- Lone seeds shorter than 6 chars never enter `raison_sociale_lone_seeds`.
- Right boundary always strict: no partial word matches.
- "Forme juridique : SELARL", "La SELARL exerce", "blanc" in prose → NOT masked.
- All #266/#264/#259/#257/#256 precision controls green.

### New test `scripts/test_273_glued_token.py`
- Part A (daemon DOWN): glued TESTONI masked; FAKENAME masked; 'Forme juridique'
  preserved; precision block unchanged.
- Part A2 (precision): common-word names NOT masked.
- Part B (de-anon round-trip via Python API): anon has NOM tokens, TESTONI/FAKENAME
  gone; de-anon restores both.
- Part C (daemon UP if available): same checks.
- Part D unit tests D1–D12: glued detection, raison_sociale_lone_seeds set,
  common-word guards, full combined detector, Option A engine normalisation,
  de-anon round-trip, precision controls.
- 36/36 passed.

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to both
  `scripts/` and `mcpb/server/scripts/`. MCPB re-packed at 1.16.2 (no stray .bak).
- All suites green: `test_273_glued_token` 36/36, `test_266_person_name_corporate`
  62/62, `test_264_repeated_company` 37/37, `test_259_corporate_kyc` 24/24,
  `test_257_form_layout` 51/51, `test_bubble_shield_mcp` 19/19,
  `test_posttool_anonymize` 19/19, `test_256_daemon_path_fail_loud` 16/16,
  `test_260_ocr` 3/3. pytest 50/50.
  (`test_guard`/`test_guard_marker`/`test_tripwire`/`test_option_b` require
  Python ≥ 3.10; pre-existing on this machine's Python 3.9.)

## 1.16.1 — 2026-06-24 (fix #266: practitioner's personal name leaks in corporate/fiscal docs)

### Bug fixed — PERSONAL NAME leaks in signature blocks and label-less table cells (risk:HIGH)
- **Root cause:** #259/#264 mask the company name ("SELARL DU DOCTEUR FORENAME SURNAME"
  → one RAISON_SOCIALE token), but the practitioner's bare personal name standing ALONE
  (e.g. "TESTONI FAKENAME" in "Signataire : GÉRANT  TESTONI FAKENAME" or in a label-less
  table cell "/ / / TESTONI FAKENAME") was missed — GLiNER needs prose context; form
  recognizers need a "Nom :" label (absent here).

### Fix 1 — `extract_person_name_from_raison_sociale` (new, `structured_ext.py`)
- Strips the forme-juridique prefix and honorific words from a detected RAISON_SOCIALE
  ("SELARL DU DOCTEUR FAKENAME TESTONI" → residual ["FAKENAME", "TESTONI"]).
- Uses `_RAISON_SOCIALE_PREFIXES` frozenset (type words + honorifics + connectors).

### Fix 2 — `_person_name_seeds` (new, `structured_ext.py`)
- Given name tokens, returns seed strings for the doc-level repetition pass.
- Always includes the full PAIR ("FAKENAME TESTONI" + "TESTONI FAKENAME") — distinctive.
- Lone tokens only if NOT a common French surname (`_COMMON_FRENCH_SURNAMES`) and length ≥ 4.
- Precision guard: "MARTIN", "BLANC", "DUPONT", etc. are NOT lone seeds (common words).
  A practitioner named "MARTIN" will have the pair masked but not every "martin" in prose.

### Fix 3 — `signataire_matches` (new recognizer, `structured_ext.py`)
- Matches labeled signature/role blocks → NOM:
    "Signataire : TESTONI FAKENAME"
    "Gérant : FAKENAME TESTONI"
    "Nom (et qualité) du signataire/déclarant : TESTONI FAKENAME"
    "Représentant légal : FAKENAME TESTONI"
- Strips leading role/position words ("GÉRANT TESTONI FAKENAME" → value "TESTONI FAKENAME").
- Precision guards: placeholder values skipped; value capped at 80 chars.

### Fix 4 — `doc_level_person_repetition_matches` (new post-pass, `structured_ext.py`)
- Reuses the #264 doc-level repetition machinery for person names.
- After all primary detectors run, derives the person name from:
    1. RAISON_SOCIALE matches: strips company prefix → personal-name sub-span.
    2. High-confidence NOM matches with ≥2 CAPS tokens (signataire / form_nom / civility).
- Scans the FULL document for every verbatim occurrence of each seed and emits NOM.
- This catches all uncovered occurrences: label-less table cells, doubled signataire,
  "N° département  TESTONI FAKENAME", etc.
- Vault consistency: all repetition matches use the full PAIR as their value → one NOM token.
- Word-boundary-aware matching: no partial substring matches.
- ADD-only, fail-open.

### Precision guards (critical — same risk class as #264)
- `_COMMON_FRENCH_SURNAMES`: frozenset of ~60 high-frequency surnames / common words.
  These are NOT used as lone-token seeds. Pair seeds (always) + lone seeds (if distinctive only).
- `_RAISON_SOCIALE_PREFIXES` guard: forme-juridique words never used as person name seeds.
- Minimum lone-seed length: 4 characters.
- Precision: "La SELARL exerce" → NOT masked; "Forme juridique : SELARL" → NOT masked;
  common words "blanc", "martin" in prose → NOT masked.

### make_structured_detector updated
- Now calls `signataire_matches(text)` after the #264 repetition pass.
- Now calls `doc_level_person_repetition_matches(text, matches)` as the final pass.

### New test `scripts/test_266_person_name_corporate.py`
- Part A (daemon DOWN): all 5 person-name occurrences masked in CORP_BLOCK.
- Part A2 (precision): common-word names NOT masked in ordinary prose.
- Part B (daemon UP if available): same as A with NER running.
- Part C (de-anon round-trip): anon output has NOM tokens, FAKENAME/TESTONI gone, ≤2 NOM token types.
- Part D (unit tests D1–D25): extract, seeds, signataire, repetition pass, full detector, vault consistency.

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` →
  `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to `mcpb/server/scripts/`.
  MCPB re-packed.
- All suites green: `test_266_person_name_corporate` 57/57, `test_264_repeated_company` 31/31,
  `test_259_corporate_kyc` 20/20, `test_257_form_layout` 42/42, `test_bubble_shield_mcp` 18/18,
  `test_posttool_anonymize` 19/19, `test_256_daemon_path_fail_loud` 10/10.
  (test_guard/marker/tripwire/option_b require Python ≥10; pre-existing on this machine's 3.9.)

### Fidelity patch — doc-level person-name de-anon (Bug 1 + Bug 2 from reviewer, no leak)

**Bug 1 — round-trip name inversion** (`structured_ext.py`
`doc_level_person_repetition_matches`, ~L900):
- **Root cause:** stored `value=canonical_val` for every occurrence even when the doc
  had the inverted form ("TESTONI FAKENAME"). Vault restored all occurrences to the
  canonical form instead of the original surface text.
- **Fix:** `value=occ.group(0)` — actual matched text. vault._token_for_name groups by
  distinctive words: both forms share person-number 1 (\u27e6NOM_0001\u27e7 / \u27e6NOM_0001a\u27e7),
  each restores to its own surface form. Dead-code `nom_canonical` dict removed.
- **Test D26:** de-anon restores "TESTONI FAKENAME" unchanged (not inverted);
  all NOM tokens share one person number.

**Bug 2 — POSTE/_QUAL crosses newlines** (`recognizers.py` ~L141):
- **Root cause:** `_QUAL = r"(?:\s+...){0,3}"` used `\s+` which matches `\n`,
  swallowing the next line's "Signataire" label and breaking signataire_matches.
- **Fix:** `\s+` → `[^\S\n]+` in `_QUAL` and `_COMP`. Same-line whitespace only.
  POSTE still matches same-line qualifiers; next-line label is never swallowed.
- **Test D27:** POSTE match ends before newline; signataire name still masked;
  same-line qualifier regression-free.

## 1.16.0 — 2026-06-23 (feat #260: optional local OCR for scanned/image PDFs)

### Feature — OCR pack (optional, off by default, fail-open)
- **New: read scanned/image-only PDFs locally (board #260).** The plugin core
  continues to handle native/text PDFs with zero install. For scanned or
  image-only PDFs (no text layer), the optional OCR pack can now be installed
  once and used fully offline thereafter.
- **New MCP tool `bubble_shield_setup_ocr(action=start|status)`:** installs or
  checks the OCR pack from inside Cowork — no Terminal needed. `start` spawns
  the setup in the background; `status` reports progress. The setup creates a
  dedicated venv at `~/.bubble_shield/ocr-env/` with `docling` + `onnxruntime`
  (~150MB pip deps + ~506MB docling layout model from HuggingFace — one-time).
- **`bubble_shield_setup_ocr.py` — setup script (one-time, idempotent):**
  - Installs `docling` + `onnxruntime` into a persistent venv.
  - Downloads the docling layout model (docling-layout-heron, ~506MB) ONCE into
    the HuggingFace local cache (`ensure_layout_model_cached()`). A sentinel
    file (`layout_model_cached.flag`) marks the cache as warm.
  - Verifies the install by running a synthetic scanned-page OCR test WITH
    `HF_HUB_OFFLINE=1` enforced — proving the model loads from cache with ZERO
    network access.
  - Writes `~/.bubble_shield/ocr.json` with the venv python path.
- **Offline enforcement (PRIVACY GUARANTEE):**
  - `bubble_shield_extract._ocr_pack_python()` checks the sentinel before returning
    the venv path — if the model was not cached during setup, it returns `None`
    (fail-closed, never silently fetches at runtime).
  - `_ocr_pdf_if_pack_present()` sets `HF_HUB_OFFLINE=1` and
    `TRANSFORMERS_OFFLINE=1` in the subprocess env for every OCR invocation.
  - ZERO outbound connections after setup: the sentinel + offline flag together
    guarantee that no call to huggingface.co is ever made at OCR runtime.
  - Documentation updated: "models downloaded once at setup; OCR runs fully
    offline thereafter (HF_HUB_OFFLINE enforced)."
- **Fail-open on OCR error, fail-closed on pack absent (unchanged):** if the
  OCR pack is not installed, `extract_pdf_text` raises `ExtractionError` with
  an install hint — it never returns empty text. If the pack IS installed but
  OCR fails (e.g., illegible scan), the error falls through to `ExtractionError`
  (fail-open within the OCR path, fail-closed to the caller).
- **`[OCR]` quality note:** text extracted via OCR is prefixed with `[OCR]`;
  `bubble_shield_read` prepends a human-readable note recommending review of
  critical fields (names, dates, numbers) for OCR accuracy.
- **`test_260_ocr.py` — 3-test suite:**
  - Test 0: `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are verified in the
    subprocess env via mock — proves zero runtime network (no real OCR run
    needed for this assertion).
  - Test 1: pack absent → `ExtractionError` with install hint (fail-closed).
  - Test 2: pack present → OCR text + anonymise pipeline (skipped if not installed).
- **`test_bubble_shield_mcp.py` — updated:** expected tool count 8 → 9 (added
  `bubble_shield_setup_ocr`). New assertion: `setup_ocr status` returns a state.
  Suite: 19/19 passed.
- All 4 script copies synced (`scripts/` ↔ `mcpb/server/scripts/` identical).
  MCPB re-packed.
- All suites green: `test_bubble_shield_mcp` 19/19, `test_260_ocr` 3/3,
  `test_264_repeated_company` 37/37, `test_259_corporate_kyc` 24/24,
  `test_257_form_layout` 51/51, `test_guard` 21/21, `test_guard_marker` 11/11,
  `test_tripwire` 18/18, `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.
## 1.15.2 — 2026-06-23 (fix #264 ship-blockers: bare-type seed + two-token company)

### Bug A fix — degenerate seed corrupts the document (ship-blocker)
- **Root cause:** `doc_level_repetition_matches()` accepted any RAISON_SOCIALE seed
  whose canonical form was a bare forme-juridique type word (e.g. "SARL", "SELARL").
  Input `Raison sociale : SARL` → canonical "SARL" → the repetition pass matched
  every bare "SARL" in the document. Simultaneous over-mask ("Forme juridique : SARL"
  and "La SARL exerce…" → wrongly masked) AND under-mask ("SARL DUPONT" → only
  "SARL" masked, "DUPONT" leaked).
- **Fix:** Added `_FORME_JURIDIQUE_SET` (frozenset of all 14 type words).
  In `doc_level_repetition_matches`, skip any seed whose canonical form is a member
  of `_FORME_JURIDIQUE_SET`. Also skip the original (non-canonical) form if it is
  itself a bare type word. A bare-type seed contributes NO repetition matches.
- **Location:** `structured_ext.py` lines 419–425 (`_FORME_JURIDIQUE_SET`),
  `doc_level_repetition_matches()` (seed-collection loop, `_FORME_JURIDIQUE_SET` guards).

### Bug B fix — two vault tokens for the same company (ship-blocker)
- **Root cause:** `Dénomination de l'entreprise :` captured `SELARL SELARL DU DOCTEUR
  FAKENAME TESTONI` (doubled prefix, PDF extraction artifact) while `Raison sociale :`
  captured `SELARL DU DOCTEUR FAKENAME TESTONI`. Different `Match.value` strings →
  `vault.token_for()` (keyed by exact string) minted two different tokens
  (`⟦RAISON_SOCIALE_0001⟧` and `⟦RAISON_SOCIALE_0002⟧`) for ONE company. De-anon
  returned inconsistent surface forms.
- **Fix:** Apply `_canonical_company_name()` to `Match.value` before emitting in BOTH
  `form_raison_sociale_matches()` and `forme_juridique_anchored_matches()`. Both
  recognizers now emit `canonical_val` ("SELARL DU DOCTEUR FAKENAME TESTONI") as the
  vault key regardless of which surface form they observed. The vault sees one string →
  one token → consistent output.
- **Location:** `structured_ext.py` `form_raison_sociale_matches()` (emit block),
  `forme_juridique_anchored_matches()` (emit block).

### Test additions (D13–D20 in `test_264_repeated_company.py`)
- D13: bare SARL seed → 0 repetition matches (Bug A guard)
- D14: "Forme juridique : SARL" NOT masked as RAISON_SOCIALE (Bug A)
- D15: prose "La SARL exerce…" NOT masked as RAISON_SOCIALE (Bug A)
- D16: real multi-word canonical seed still produces repetition matches (Bug A, no regression)
- D17/D17b: doubled-prefix and clean form → same canonical value from form_raison_sociale_matches (Bug B)
- D18: vault emits different tokens for different raw strings (confirms fix must be in recognizer)
- D19: structured_ext emissions all share one value → single vault key (Bug B)
- D20: end-to-end simulation → both lines get the same ⟦RAISON_SOCIALE_0001⟧ token (Bug B)

### Other
- All 3 copies synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/` → `plugin/bubble-shield/mcpb/server/vendor/`). Test scripts synced to `mcpb/server/scripts/`. MCPB re-packed.
- All suites green: `test_264_repeated_company` 37/37, `test_259_corporate_kyc` 24/24, `test_257_form_layout` 51/51, `test_bubble_shield_mcp` 18/18, `test_guard` 21/21, `test_guard_marker` 11/11, `test_tripwire` 18/18, `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9, `test_256_daemon_path_fail_loud` 16/16.

## 1.15.1 — 2026-06-23 (fix #264: repeated company name leaks in liasse fiscale)

- **LEAK FIXED — corporate name repeated 14× in liasse fiscale.** Once #259 masked
  the labeled `Raison sociale :` line, the practitioner's company name (SELARL DU
  DOCTEUR …) still appeared unmasked as free-standing page/table headers throughout
  the liasse fiscale — the label-anchored recognizer naturally misses unlabeled
  repetitions. The company name leaked in clear in every table header, footer line,
  and section title.
- **Root cause:** PII detection is span-based — only the labeled occurrence was
  matched. Subsequent verbatim repetitions of the same string in unlabeled positions
  had no anchor to trigger a match.
- **Fix 1 — extend `_RAISON_SOCIALE_LABEL_RE`:** added the `de l'entreprise` label
  variant (`Dénomination de l'entreprise :`) which the #259 pattern missed. Liasse
  fiscale uses this exact label form.
- **Fix 2 — `forme_juridique_anchored_matches` (new, `structured_ext.py`):**
  Catches unlabeled company-name headers anchored by a forme-juridique type word
  (`SELARL|SARL|SAS|SCI|SCM|SCP|SASU|SNC|EURL|SCOP|…`), including the doubled-
  prefix extraction artifact `SELARL SELARL DU DOCTEUR …`. Emits RAISON_SOCIALE
  for the full span (type word + name) at score 0.82 (below labeled 0.90; overlap
  resolution keeps the higher-scoring labeled match when both fire on the same span).
  Precision guards: bare `SELARL` alone not matched; `Forme juridique : SELARL`
  label line not matched (line-prefix check); prose `La SELARL exerce…` not matched
  (ALL-CAPS name continuation required); `Type : SAS` not matched.
- **Fix 3 — `doc_level_repetition_matches` post-pass (new, `structured_ext.py`):**
  After all primary detectors run, collects every unique RAISON_SOCIALE value
  (canonicalised — doubled prefix stripped), then scans the full document for every
  verbatim occurrence and emits a RAISON_SOCIALE match for each span not already
  covered. This is the definitive belt-and-suspenders fix: even if the
  forme-juridique-anchored pattern can't fire (unusual layout), once the labeled
  occurrence is found the post-pass finds the other 13 repetitions. ADD-only,
  fail-open.
- **Canonical normalisation helper `_canonical_company_name`:** strips the doubled-
  prefix artifact (`SELARL SELARL DU DOCTEUR X` → `SELARL DU DOCTEUR X`) so the
  vault lookup is consistent and both forms resolve to the same canonical key for
  the repetition search.
- **New test `scripts/test_264_repeated_company.py`:** 4-part (daemon DOWN / UP /
  unlabeled-only / unit), 28 assertions. Full synthetic liasse block with all 5
  occurrence types (labeled variant, doubled-prefix, standalone header,
  prefixed header, date-prefixed header). Precision controls: `Forme juridique :
  SELARL` and `La SELARL exerce une activité` → NOT masked.
- All 3 copies re-synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/`
  → `plugin/bubble-shield/mcpb/server/vendor/`). `scripts/` ↔
  `mcpb/server/scripts/` identical. MCPB re-packed.
- All suites green: `test_264_repeated_company` 28/28, `test_259_corporate_kyc`
  24/24, `test_257_form_layout` 51/51, `test_bubble_shield_mcp` 18/18,
  `test_guard` 21/21, `test_guard_marker` 11/11, `test_tripwire` 18/18,
  `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.

## 1.15.0 — 2026-06-23 (fix #259: corporate KYC PII leaks — raison sociale + SIRET NIC suffix)

- **LEAK 1 FIXED — Raison sociale leaks in clear.** Form line `Dénomination ou
  raison sociale : SELARL DU DOCTEUR <PERSON NAME>` returned the company name
  unmasked. For SELARL/SCM/SCI/SCP practices the company name EMBEDS the
  practitioner's personal name. No recognizer existed for `dénomination / raison
  sociale :` label lines.
- **Fix — `structured_ext.py` — new `form_raison_sociale_matches` recognizer:**
  Matches `Dénomination (ou raison sociale)? :` and `Raison sociale :` label
  lines → entity **RAISON_SOCIALE**, masks the whole company name value. ADD-only,
  fail-open, daemon-independent (deterministic, same contract as #257 recognizers).
  Precision guards: requires explicit label+colon; skips placeholder values
  (`néant`, `N/A`, etc.); value must contain at least one letter; capped at 120
  chars. Does NOT mask `Forme juridique : SELARL` — only labeled dénomination/
  raison-sociale lines. Wired into `make_structured_detector()`.
- **LEAK 2 FIXED — SIRET NIC suffix leaks.** Output showed
  `N° SIRET : ⟦SIREN_0001⟧-NNNNN` — the 9-digit SIREN masked but the 5-digit
  NIC suffix (`00011`) stayed in clear, making the full 14-digit SIRET
  reconstructable. Root cause: the SIRET regex `\d{3}[ ]?\d{3}[ ]?\d{3}[ ]?\d{5}`
  only tolerated spaces between groups. Real DCC forms use a hyphen between the
  SIREN and NIC (e.g. `123 456 789-00011`), which the old pattern missed —
  causing the SIRET to match only 9 digits (SIREN), leaving the NIC exposed.
- **Fix — `recognizers.py` — SIRET pattern updated:** Changed `[ ]?` separators
  to `[ -]?` so the SIRET recognizer (priority 93) now catches all variants:
  spaced (`123 456 789 00011`), compact (`12345678900011`), and hyphen-separated
  (`123 456 789-00011` / `123456789-00011`). Fix #259.
- **Regression test `scripts/test_259_corporate_kyc.py`:** 4-part test covering
  daemon DOWN and daemon UP paths, with:
  - Raison sociale masking for SELARL with embedded non-gazetteer name
  - Full 14-digit SIRET masking (hyphen-separated form)
  - Control: `Forme juridique : SELARL` type-word NOT masked
  - Prose control: no false positives on unlabeled text
  Uses synthetic-only PII (`FAKENAME TESTONI`, `123 456 789 00011`).
- All 3 copies re-synced (root `bubble_shield/` → `plugin/bubble-shield/vendor/`
  → `plugin/bubble-shield/mcpb/server/vendor/`). `scripts/` ↔
  `mcpb/server/scripts/` identical. MCPB re-packed.
- All suites green: `test_259_corporate_kyc` 8/8, `test_257_form_layout` 49/49,
  `test_bubble_shield_mcp` 18/18, `test_guard` 21/21, `test_guard_marker` 11/11,
  `test_tripwire` 18/18, `test_posttool_anonymize` 19/19, `test_option_b_e2e` 9/9,
  `test_256_daemon_path_fail_loud` 16/16.

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
