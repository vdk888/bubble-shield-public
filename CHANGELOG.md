# Changelog

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
