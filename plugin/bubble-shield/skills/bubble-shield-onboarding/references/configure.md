# Bubble Shield Guard — configuration reference

Read this when the user asks about a specific setting. Translate only the
relevant part into plain French; don't dump the whole file on them.

## Two ways to protect a folder

### A. In-folder marker — `.bubble-shield.json` (PRIMARY, Cowork-native)

Drop a `.bubble-shield.json` file **inside** the client folder. That folder and
everything under it become protected. The guard walks up from each accessed file
to find the nearest marker. This is the method to use in **Cowork**, because
Cowork is sandboxed and cannot write to `~/.config` (it refuses system/dotfile
dirs), but it CAN write into a folder the user connected.

The marker may be empty (`{}`) or carry per-folder overrides:
```json
{
  "allow_paths": ["clean"],
  "allow_extensions": [".anon.txt"],
  "block_bash": true,
  "tripwire_enabled": true,
  "message_fr": "🔒 ..."
}
```
`allow_paths` may be relative to the marker's folder. The marker file is never
itself blocked. To stop protecting a folder, delete its marker.

### B. Global config (CLI fallback, optional)

A single JSON file with a `protected_folders` LIST, found in this order (first
hit wins). Useful only when running Claude Code in a terminal (where `~/.config`
is writable). It COMPOSES with markers.

1. `$BUBBLE_SHIELD_GUARD_CONFIG` — an explicit path (env var)
2. `<project>/.bubble-shield.json`
3. `~/.config/bubble_shield/bubble-shield.json`
4. `~/.bubble-shield.json`
5. `<plugin>/config/bubble-shield.json` — packaged default (overwritten on update)

Start from `${CLAUDE_PLUGIN_ROOT}/config/bubble-shield.example.json`. Note: a
global config needs an explicit `protected_folders` list; a marker doesn't (its
own folder is the protected root).

## Fields

| Key | What it does | Plain-French framing |
|---|---|---|
| `protected_folders` | Folders whose contents are blocked, recursively. The "coffre". | "Les dossiers où vivent vos fichiers clients." |
| `allow_paths` | Specific paths *inside* a protected folder that are allowed anyway — e.g. a `clean/` output dir. | "Le sous-dossier des copies anonymisées, qu'on peut lire sans risque." |
| `allow_extensions` | Extensions exempt inside protected folders, e.g. `.anon.txt`. | "Les fichiers déjà anonymisés." |
| `block_bash` | Also deny shell commands that mention a protected path (stops `cat fichier` bypassing the read-guard). Keep `true`. | "Empêche aussi de contourner le verrou par une commande." |
| `message_fr` | The message shown when a read is blocked. | (cosmetic) |
| `tripwire_enabled` | Second guard for the **chat box**: nudges Claude when raw PII is pasted, or a document is uploaded/attached, directly in the conversation. Keep `true`. | "Un garde-fou si on colle des données client directement dans le chat au lieu de les mettre dans le dossier." |
| `tripwire_block` | If `true`, the tripwire HARD-blocks such a prompt instead of just nudging. More strict, more disruptive. Default `false`. | "Mode strict : bloque carrément le message au lieu de juste prévenir." |

## The chat box is NOT protected the same way — work from the folder

The folder guard (`protected_folders`) is the real vault: any file Claude
reads/edits inside it gets anonymised first. **But a document you paste or
drag-drop/upload directly into the chat is already in the AI's context before
any guard can run** — that's a platform limit, not a Bubble Shield choice. The
tripwire can only *notice* this and tell Claude to redirect you.

→ **The rule to teach the user:** *put client documents in the protected
folder (e.g. your Dropbox client sub-folder) and ask Claude about them there —
don't paste raw client data into the chat.* That's the path where Bubble Shield
anonymises for real.

## Dropbox (common setup)

The protected folder is usually a **sub-folder inside Dropbox** (e.g.
`~/Dropbox/Clients`), not a separate folder. Point `protected_folders` straight
at it:

```json
{ "protected_folders": ["~/Dropbox/Clients"] }
```

Every file under it (recursively) is guarded. **Caveat:** if a Dropbox file is
"online-only" (cloud icon, not downloaded), it must sync to disk first before
Claude can read/anonymise it — right-click → "Make available offline" if needed.

## Example for a single advisor

```json
{
  "protected_folders": ["~/Dossiers-clients", "~/Downloads/souscriptions"],
  "allow_paths": [],
  "allow_extensions": [".anon.txt"],
  "block_bash": true,
  "message_fr": "🔒 Bubble Shield — ce fichier client est protégé. Lance d'abord l'anonymisation, puis travaille sur la copie."
}
```

## Fail-safe behaviour (reassure the user)

- **No config / not configured** → the guard is *inert* (it doesn't block
  everything and brick the session). Setting `protected_folders` is what turns
  protection on.
- **Malformed config** → the guard fails **closed** (blocks) and says the config
  is unreadable. A guard you can't parse must not silently wave data through.

## After any change

Run `/reload-plugins` (or restart the session). The guard reads the config on
every tool call, but the plugin itself loads at session start.
