# Accuracy pack — "protect PII everywhere" (reference)

The optional on-device ML layer. Read this when setting it up, troubleshooting,
or answering a technical "how does it actually work" question. The client-facing
flow is in `SKILL.md` ("The accuracy pack" section) — this is the detail.

## What it is

Two cooperating pieces, both **on the user's Mac, no network at run time**:

- **A warm NER daemon** (`bubble_shield_nerd.py`) — holds a small on-device AI model
  (GLiNER, multilingual incl. FR) resident in memory and answers "what PII is in
  this text?" in ~40ms. Binds `127.0.0.1` only — never a public interface.
- **A PostToolUse hook** (`posttool_anonymize.py`) — after the assistant runs any
  tool that reads data (a file, a script, a fetched e-mail), it inspects the
  result and, if it contains sensitive data, rewrites it to the anonymised copy
  *before the assistant sees it*. Uses the fast rules first, then the daemon for
  the soft names/addresses the rules miss.

It composes with everything else: same `⟦…⟧` tokens, same vault (so a name
cloaked in an e-mail is the same token as in a folder dossier), same
masquer/conserver policy table.

## Setup (one time, with the operator)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/bubble_shield_setup_ml.py"
```

What it does, in order (idempotent — safe to re-run):
1. Creates a dedicated environment at `~/.bubble_shield/ml-env/` (persistent — survives
   reboots; not a temp dir).
2. Installs the runtime there (`onnxruntime` + `gliner`, ~71 MB — **no PyTorch**,
   so the footprint stays small).
3. Downloads the pre-exported quantised model to `~/.bubble_shield/models/` (a few
   hundred MB, once).
4. Writes `~/.bubble_shield/ml.json` (the daemon reads this for paths).
5. Verifies the model loads and detects, then installs a macOS **LaunchAgent**
   (`com.bubbleinvest.bubble-shield-nerd`) so the daemon starts at login and is kept
   alive — this is the "no intervention from then on" piece.

Flags: `--check-only` (verify an existing install, no download), `--no-launchd`
(skip the login auto-start — the hook will lazy-start the daemon on demand
instead), `--onnx onnx/model_q4.onnx` (smaller/faster model variant).

Network is needed only for this first run (pip + model download). After that it
is fully offline.

## Turn the feature on (opt-in)

Installing the pack does NOT activate it. Set, in the relevant config:

```json
{ "posttool_enabled": true }
```

- **Cowork:** add it to the folder marker `.bubble-shield.json` (or the global
  config if one is used).
- **CLI:** `~/.config/bubble_shield/bubble-shield.json`.

Optional scoping:
- `"posttool_tools": ["Read","mcp__gmail__","Bash"]` — only scrub these tools'
  output (default: the hook's matcher already targets ingestion tools).

## How it fails — the safety posture (important)

- **Fail-OPEN.** Any error in the hook or daemon → the assistant gets the
  ORIGINAL tool output. This is a broad safety net on already-allowed data; it
  must never wedge a session. The **fail-closed** hard guarantee remains the
  PreToolUse folder guard on marked folders.
- **PII-presence gate.** It only rewrites output that actually contains validated
  PII, so normal/benign tool output is never mangled (no over-redaction).
- **Off by default**, opt-in per `posttool_enabled`.

## The one path it CANNOT cover (warn the client)

If the user **pastes or drags a document directly into the conversation**, its
content is injected into the assistant's context *before any tool runs* — there
is no hook that fires on a chat-box upload (platform limit; confirmed in the
Claude Code hooks docs and issues #29434 / #39882). So the accuracy pack cannot
anonymise pasted/dragged content. **Rule to convey: always work from files in a
folder, never by copy-pasting client data into the chat.** This is the user's
responsibility and should be stated explicitly during onboarding.

## Troubleshooting

| Symptom | Check |
|---|---|
| Not catching anything | `bubble_shield_setup_ml.py --check-only` prints `OK`? `posttool_enabled: true` set? |
| First read after reboot missed it | the model warms on first use; the daemon (LaunchAgent) should be up — `curl -s http://127.0.0.1:8723/health` returns `{"warm": true}`. The hook also lazy-starts the daemon if it's down, so the *next* read is covered. |
| A name in dense text slipped through | recall isn't 100% — names near the confidence threshold can be missed. Lower `BUBBLE_SHIELD_GLINER_THRESHOLD` for more recall (more over-redaction). Calibrate on real dossiers. |
| Want it faster / smaller | use a smaller model variant: `--onnx onnx/model_q4.onnx`. |
| Daemon won't start | check `~/.bubble_shield/nerd.log`; ensure `~/.bubble_shield/ml-env/bin/python` exists (re-run setup). |

## Performance (measured)

- Model load: ~50 s cold (once, at login) → daemon keeps it warm.
- Detection: ~40 ms per tool result, warm.
- Footprint: ~71 MB runtime + a few hundred MB model, all under `~/.bubble_shield/`.
