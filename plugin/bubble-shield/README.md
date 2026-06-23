# Bubble Shield Guard — fail-closed PII guard for Cowork / Claude Code

A Claude Code **plugin** that stops Claude from reading raw client data. While
enabled, any attempt to `Read`/`Grep`/`Glob`/`Edit`/`Write`/`Bash` a file inside
a **protected client folder** is **denied** — Claude is told to read it through
the MCP tool `bubble_shield_read` instead, which returns the contents
**already anonymised** (`⟦…⟧` tokens). Claude works on the tokens; the final
document is de-anonymised locally via `bubble_shield_write`. The token↔value
vault never leaves the machine.

This is **Jalon 2** of Bubble Shield: the engine (Jalon 1) anonymises; this plugin
*enforces* that nothing identifying reaches the model in clear.

> **Surface:** hooks run wherever the Claude Code engine runs — **Cowork**, the
> Claude Code CLI, and the IDE extension. They do **not** run on the plain
> claude.ai web chat (no hook engine there); for that surface use the Bubble Shield
> webapp (Mode B). The target CGP firm uses **Cowork**, which is fully covered.

## Why it's safe by design

- **Fail-closed.** A `PreToolUse` `deny` blocks the tool **even under
  `bypassPermissions` / `--dangerously-skip-permissions`** (per Claude Code
  docs). If the config is missing/malformed or the event is unparseable, the
  guard **denies** rather than waving data through.
- **100% local.** Pure-stdlib Python, no network, no telemetry. It only gates;
  anonymisation is the Bubble Shield engine, also local.
- **Configurable.** Protected folders, allow-listed sub-paths, exempt
  extensions, and Bash scanning are all set per deployment.

## Install (one command, from the Bubble marketplace)

```
/plugin marketplace add vdk888/bubble-shield
/plugin install bubble-shield@bubble-shield
```

Or test locally without installing:

```bash
claude --plugin-dir /path/to/bubble_shield/plugin/bubble-shield
```

After install, run `/reload-plugins` (or restart the session) to load the hook.

## Configure

**Cowork (recommended): drop a marker file in the client folder.** Put a
`.bubble-shield.json` (copy `config/marker.example.json`, or just `{}`) **inside**
any folder you want protected. The guard walks up from each accessed file to find
the nearest marker, so that folder + everything under it is guarded. This is the
only method that works in Cowork, which is sandboxed and can't write to
`~/.config` — but can write into a folder you've connected. Same idea as
`.gitignore`. Delete the marker to stop protecting the folder.

> **One marker at the root protects the whole tree.** Because the guard walks
> *up* to the nearest marker, a single `.bubble-shield.json` at the root of a
> client's synced folder (e.g. `~/Dropbox/`) guards **every file at every depth
> below it** — nested sub-folders inherit automatically. The recommended
> onboarding step is exactly this: drop one marker at the client's folder root,
> with **no** `allow_paths` / `allow_extensions` exemptions, for full coverage.
> ⚠️ A file placed **outside** any marked folder is **not** protected (read raw),
> and any `allow_paths` / `allow_extensions` you add are deliberate holes — keep
> them empty unless you mean it.

```jsonc
// <client-folder>/.bubble-shield.json
{ "allow_paths": ["clean"], "allow_extensions": [".anon.txt"], "block_bash": true }
```

**CLI fallback (optional):** a global config with a `protected_folders` list,
found in this order (first hit wins). It composes with markers.

1. `$BUBBLE_SHIELD_GUARD_CONFIG` (explicit path)
2. `<project>/.bubble-shield.json`
3. `~/.config/bubble_shield/bubble-shield.json`
4. `~/.bubble-shield.json`
5. `<plugin>/config/bubble-shield.json` (packaged default)

```json
{
  "protected_folders": ["~/Dossiers-clients", "~/Downloads/souscriptions"],
  "allow_paths": ["~/Dossiers-clients/dossier-x/clean"],
  "allow_extensions": [".anon.txt"],
  "block_bash": true,
  "message_fr": "🔒 Bubble Shield — accès bloqué…"
}
```

| Key | Meaning |
|---|---|
| `protected_folders` | Folders whose contents are blocked (recursive). The "coffre". |
| `allow_paths` | Specific paths inside a protected folder that are allowed (e.g. a `clean/` output dir). |
| `allow_extensions` | Extensions exempt inside protected folders (e.g. `.anon.txt` for cloaked output). |
| `block_bash` | Also deny Bash commands that mention a protected path (stops `cat …` bypassing the Read guard). |
| `message_fr` | The message Claude (and the user) sees when blocked. |

## How it works

1. `hooks/hooks.json` registers a `PreToolUse` hook on the file/shell tools.
2. `scripts/guard.py` reads the event JSON, resolves the target path(s), and
   compares against markers / `protected_folders` (symlink-resolved, `~`-expanded).
3. Inside a protected folder (and not exempted) → `permissionDecision: "deny"`
   with a French message telling Claude **not** to use `Read`/`Bash`, and to call
   the MCP tool `bubble_shield_read(path="…")` instead.
4. `bubble_shield_read` returns the file **already anonymised** (`⟦…⟧` tokens) as
   its **own** tool output — Claude works on the tokens, then produces the final
   document via `bubble_shield_write`, which de-anonymises locally from the vault.

> **Why deny-and-reroute, instead of scrubbing the result after the read?**
> Claude Code's `PostToolUse` hooks **cannot** rewrite the output of built-in
> tools like `Read`/`Bash` — the harness ignores `updatedToolOutput` for them
> (proven on v2.1.186; [anthropics/claude-code#32105](https://github.com/anthropics/claude-code/issues/32105),
> still open). A post-hook scrub would print a reassuring notice while raw PII
> still reached the model. So Bubble Shield does **not** rely on it: the guarantee
> is the `PreToolUse` **deny** (honored for built-in tools) plus a first-party MCP
> read whose **own** output — the only channel the harness reliably substitutes —
> is what Claude sees. The bundled `bubble-shield-anonymize` skill remains an
> alternative manual path (anonymise a folder into `clean/`, work on the copy).

### The chat box (tripwire)

The folder guard protects files **on disk**. A document **pasted or uploaded
directly into the chat** is injected into the model's context *before any tool
call*, so the `PreToolUse` guard never sees it — a platform limit, not a Bubble Shield
choice. A second hook (`scripts/tripwire.py`, on `UserPromptSubmit`) covers
that path: it scans the prompt text for raw PII (IBAN, email, n° sécu, phone)
or attachment phrasing and **nudges Claude to redirect you to the protected
folder** (or hard-blocks, if `tripwire_block: true`).

> **Work from the folder, not the chat.** Put client documents in your
> protected folder (e.g. your Dropbox client sub-folder) and ask about them
> there — that's where Bubble Shield anonymises for real. The tripwire is only a
> guard-rail for raw data that slips into the conversation.

## Test

```bash
python3 scripts/test_guard.py        # 14 black-box cases (deny/allow/fail-closed)
python3 scripts/test_tripwire.py     # 18 black-box cases (nudge/block/no-op/fail-open)
```

Verified end-to-end in a live Claude session: a raw PII file is blocked; an
`.anon.txt` copy is readable; the tripwire nudges on pasted IBAN/email.

## RGPD

Pseudonymisation **réversible** locale — mesure de sécurité (art. 25 & 32). Ne
remplace pas le DPA, l'AIPD, ni la relecture humaine. Voir `COMPLIANCE_RGPD.md`.
