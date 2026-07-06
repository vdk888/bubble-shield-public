# Changelog

## 1.21.4

### Fixed
- **Mail triage can now apply emoji labels and prepare drafts on any-locale Gmail.**
  Two bugs surfaced by a live triage test, both fixed and verified against a real
  account:
  - **Emoji/accented label names no longer crash the apply.** Applying a label like
    `🔴 Clients` or `Système` failed with `UnicodeEncodeError` because IMAP label
    names carrying non-ASCII must be modified-UTF-7 encoded; the label argument is
    now encoded correctly (`🔴 Clients` → `&2D3dNA- Clients`).
  - **Draft creation works on non-English Gmail.** The drafts folder was hardcoded to
    the English `[Gmail]/Drafts`; a French account uses `[Gmail]/Brouillons`, so
    drafts failed with "folder doesn't exist". Bubble Shield now discovers the drafts
    folder via its IMAP `\Drafts` special-use flag (locale-proof), with a fallback.

## 1.21.3

### Fixed
- **A failed mail-triage action now says WHY (the exception type), so it's
  diagnosable.** `bubble_shield_mail_apply` used to report only `1 échec (UID: n)`
  with no reason — the cause was redacted to avoid leaking PII, but that made a real
  failure impossible to diagnose from the assistant side. It now surfaces the
  exception **type name** (a class identifier like `MailConfigError`, `IMAP4.error`,
  `PermissionError` — never PII, never the message/args) in the returned summary and
  stderr, e.g. `1 échec (UID:type — 3785:MailConfigError)`. The restored draft body
  and any exception message are still never surfaced.

## 1.21.2

### Fixed
- **Mail triage can now actually apply its decisions.** `bubble_shield_mail_apply`
  targets each message by UID, but `bubble_shield_mail_read` did not return one — and
  read used unstable sequence numbers while apply used UIDs, a mismatched identifier
  space. So an assistant could classify mail but could not label/archive/draft without
  guessing an identifier (risking action on the wrong message), and correctly refused.
  Read now uses UID SEARCH/FETCH and starts each message block with a `UID:` line (a
  mailbox integer, not PII — never anonymised) that the assistant passes straight to
  apply. The read→apply hand-off is verified byte-identical. Also added
  `bubble_shield_mail.py` to the plugin↔bundle mirror-integrity tripwire.

## 1.21.1

### Fixed
- **NER daemon no longer drops after a plugin update (#561, the real fix).** The
  LaunchAgent that keeps the detection daemon warm was pointed at the daemon script
  inside the ephemeral per-session plugin cache. Cowork garbage-collects that cache on
  every plugin update, so after each update launchd tried to start a deleted file, crash-
  looped, and the daemon went "down" — making reads fail-closed until a session lazily
  respawned it. The daemon is now installed to a STABLE location (`~/.bubble_shield/
  daemon/`) and the LaunchAgent points there, so a plugin update can no longer orphan it.
  A re-run of the ML setup refreshes the stable copy to the new code. Only the daemon +
  its setup script are copied (guard/hooks/tests are explicitly excluded).

## 1.21.0

### Added
- **Mail triage — trie une boîte Gmail entière sans jamais voir de PII.** Nouveau skill
  `bubble-shield-mail-triage` + nouvel outil `bubble_shield_mail_apply`. L'assistant lit
  chaque mail anonymisé (`bubble_shield_mail_read`), le classe dans une taxonomie 5 niveaux
  (Clients / Important / Newsletters / Structurés / CV / Transition), puis pose les libellés
  Gmail, archive, et prépare des brouillons de réponse — via un chemin d'écriture IMAP
  host-side, donc utilisable dans une tâche planifiée sans validation manuelle. La liste des
  clients est lue anonymisée depuis le dossier protégé et matchée jeton-à-jeton, donc elle
  reste à jour à chaque export sans changer le code.

### Security
- **Chemin d'écriture mail à garanties structurelles.** `bubble_shield_mail_apply` ne peut
  PAS envoyer (aucun SMTP — uniquement APPEND vers les brouillons), ne peut PAS supprimer
  (aucun `\Deleted`/expunge/Trash/Spam — archiver = retirer `\Inbox`), est plafonné à 60
  mutations par passage, et journalise chaque action (chmod 600, sans noms de libellés custom
  potentiellement PII). Les brouillons sont restaurés en mémoire via le vault : le vrai nom va
  dans le brouillon Gmail, jamais dans le contexte du modèle ni sur disque. Un brouillon dont
  un jeton reste non résolu est SAUTÉ plutôt qu'envoyé avec des marqueurs visibles.
- **stderr redigé** pour les deux outils qui restaurent du PII (`write` + `mail_apply`) : le
  type d'exception est loggé, jamais le message brut, pour qu'une erreur de librairie ne puisse
  pas déposer du PII restauré dans un log host persistant.

## 1.20.6

### Fixed
- **NER daemon stays warm across a working session.** The idle-shutdown timeout
  was 15 minutes, but the idle exit is clean (exit 0) and the LaunchAgent only
  auto-restarts on a failure exit — so the daemon dropped mid-session and reads
  refused (fail-closed) until a ~20-37s cold re-spawn. The idle timeout is now 4
  hours (set `BUBBLE_SHIELD_NERD_IDLE=0` for an always-warm install), so a normal
  working session no longer hits intermittent NER-down refusals.

## 1.20.5

### Fixed
- **`bubble_shield_list` no longer self-blocks.** The sanctioned folder-listing
  tool (added in 1.20.2) was not in the guard's own-tool allow-list, so the guard
  denied it on every protected folder — making file discovery impossible and
  forcing the operator to paste paths by hand. It is now exempted like
  `bubble_shield_read`/`write` (it returns masked filenames, fail-closed if NER is
  down). The exemption stays narrow — non-sanctioned MCP file tools remain blocked.

## 1.20.4

Security hardening of the Cowork sandbox-mount guard, surfaced by a live red-team.

### Security
- **`cd`-compound mount-alias bypass closed (#553).** A shell command that does its
  own `cd` into a sandbox mount and then reads via a relative path
  (`cd .../mnt/outputs && cat "../Dropbox/clients/x"`) bypassed the guard, because
  the guard saw the session-root cwd, not the post-`cd` directory. The guard now
  resolves the effective cwd from `cd` chains before classifying.
- **Whole cwd-hiding class fail-closed (#553-B/C/D).** Constructs that hide the
  effective directory from the guard — subshells `(...)`, `bash -c`, `pushd`,
  `eval`, `cd $VAR`/`cd $(...)`, opaque `eval "$(...)"`, and mount paths assembled
  from simple shell variables — now fail closed when combined with a read that
  could reach a protected mount. Infra mounts (`outputs`/`uploads`/`.claude`/
  `.remote-plugins`) and non-mount work are unaffected.

### Fixed
- **No more spurious "internal error" blocks.** A tool event with an unexpected
  shape (command passed as a list, cwd as a number) made the guard fail closed
  with a scary "internal error" on a perfectly legitimate command. The guard now
  normalizes these shapes and reaches a correct decision instead of crashing.

## 1.20.3

Security + feature release. Fixes surfaced by a live red-team pass against the
guard, plus a new client-facing gazetteer tool.

### Security
- **Relative `..`-traversal exfil closed (#19).** A path that reached a protected
  file by walking up out of a sandbox mount alias (e.g. `…/mnt/<X>/../../secret`)
  slipped past the marker walk-up. The guard now resolves and re-checks traversal
  tokens fail-closed so the escape hatch is gone.
- **Bare-name symlink bypass closed (#20).** A symlink named as a bare word (no
  path separators) pointing at a protected file let a read dodge the guard. The
  guard now extracts and resolves bare-word tokens before deciding, so a symlink
  target inside a protected folder is blocked like the real path.
- **Write-back can no longer read PII back into context (#40).** `bubble_shield_write`
  now refuses any target that is not itself guarded or explicitly allow-listed, so
  a restored in-clear file can't be written to an unguarded path and then re-read
  by the agent.
- **Guard block-decision is now single-sourced (#40 structural).** The write gate
  and the read guard both call one shared `decide_block_for_path` decision, so the
  two can no longer drift apart as the code evolves — a whole class of "the write
  path allowed what the read path blocked" bugs is designed out.

### Added
- **Cowork human-viewer tools exempted.** The viewer tools that render a file
  straight to the human (`present_files`, `create_artifact`, `update_artifact`)
  are exempted from the protected-path block, so the agent can show a restored
  file to the operator without the raw body ever entering the model's context.
- **`bubble_shield_add_known_pii` tool.** Lets a client flag a word the anonymiser
  missed and add it to the always-mask gazetteer, so that term is masked on every
  subsequent document without a code change.

## 1.20.1

- **P0 SECURITY — Cowork sandbox-mount-alias Bash exfil closed.** In a Cowork
  session the guard runs host-side and matches real Mac paths, but the sandbox
  mounts each connected folder under a dynamic alias `/sessions/<name>/mnt/<X>/…`.
  A shell command using that alias path bypassed the marker walk-up and could read
  a protected file in clear. The guard now DENIES any `/sessions/*/mnt/<X>` token
  fail-closed (except the known infra mounts `outputs`/`uploads`/`.claude`/
  `.remote-plugins`), regardless of config/marker state — because the host cannot
  see a marker on the sandbox filesystem.
- **Manifest completeness** — the MCPB manifest now advertises `bubble_shield_mail_read`
  and `bubble_shield_status` (both were served but not listed).
- **Onboarding demo** — the first-run demo now runs a real, client-chosen task on a
  real file, showing the anonymized result in-session and the in-clear result written
  locally to disk (never re-entering the session).

## 1.18.20

> Assumes PR #24 (1.18.19, `verdict_state` honesty fix) merges first. If #24
> does not land before this, the version needs reconciling (this would become
> 1.18.19).

### Fixed
- **Recall LEAK 1 — hyphen/dot-grouped IBAN was not detected.** The IBAN
  recognizer's separator class only allowed a space between groups, so real forms
  written `FR76-1027-…` or `FR76.1027.…` never matched the regex and leaked in the
  daemon-DOWN (regex-only) config while still being certified "safe to send".
  Extended the separator class to accept space, `-` or `.` between groups, and the
  mod-97 validator now strips the same separators before checksum validation. All
  three groupings (spaced / unspaced / hyphen / dot) now match and mod-97-validate
  identically. No change to spaced/unspaced behaviour (regression-tested).
- **Recall LEAK 2 — bare Title-case "Prénom Nom" mid-sentence leaked.** In running
  prose with no structured label (`Titulaire:` / `Nom:`), a bare Title-case name
  such as "Frédérique Marchand" leaked because GLiNER scores that span *below* the
  0.30 accept threshold (measured 0.21) and the forename gazetteer — the anchor
  that catches untitled `Prénom Nom` — was missing the forename. Root cause was
  investigated by running the real GLiNER detector on the exact span and inspecting
  raw scores (0.05–0.21 mid-sentence vs 0.74 in isolation), confirming the signal
  is genuinely below threshold and that lowering the global threshold would tank
  precision. Fix: expand the forename gazetteer (`gazetteer.py`) with the missing
  common French forenames. This anchors strictly on the forename list — the
  untitled-NOM recognizer only fires when the FIRST token is a known forename — so
  precision on ordinary capitalized French terms ("Plan Épargne Retraite",
  "Assurance Vie", "Crédit Agricole") is unchanged (measured 0 false positives on a
  20-sample financial-prose corpus). ALL-CAPS and labeled forms were already caught;
  this closes the bare Title-case gap without touching the detection thresholds.

## 1.18.17

### Fixed
- **Installer online fallback pulled unpinned dependencies.** `install-app.sh`
  has two dependency-install paths: the default offline path (vendored wheels in
  `vendor/wheels/`, versions pinned by the exact wheel files) and an online
  fallback taken on the rare Mac with no ABI-matching interpreter. The online
  fallback ran `pip install fastapi uvicorn pywebview jinja2 pypdf
  python-multipart` with no version constraints, so it fetched the LATEST
  releases from PyPI — e.g. pywebview 6.x against launcher code written and
  tested for pywebview 3.4. That is the same pywebview 3.x/4.x API-drift crash
  class (`window.events` AttributeError) fixed in 1.18.16; the unpinned fallback
  simply relocated it. Added `constraints.txt` at the repo root pinning the exact
  versions vendored in `vendor/wheels/`, and applied `-c constraints.txt` to
  BOTH the offline and online pip installs so they resolve to identical versions.
  pywebview now stays 3.4 on every install path.
