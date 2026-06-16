# Bubble Shield Guard

**A privacy guard for Claude Cowork / Claude Code.** It stops the AI from reading
your raw client files until the identifying data has been replaced with anonymous
labels — locally, reversibly, and with no data leaving your machine.

Built for financial advisors (CGP/CIF) and anyone who works with client
documents in an AI assistant. 100 % local, no network, no account, no telemetry.

## Install in Claude Cowork (Desktop)

1. Open **Cowork** → **Customize** → **Plugins**.
2. Click **“+”** → **Add from a repository**.
3. Paste:

   ```
   vdk888/bubble-shield-public
   ```

4. Install **bubble-shield** and toggle it on.
5. Run **`/reload-plugins`** (or restart Cowork).

That’s it — no GitHub account needed (this is a public repository), and it runs
**fully offline** afterwards.

### Claude Code (CLI) alternative

```
/plugin marketplace add vdk888/bubble-shield-public
/plugin install bubble-shield@bubble-shield
```

## How it works

- **The guard** (a `PreToolUse` hook) blocks Claude from reading any file inside a
  folder you’ve marked as protected — drop a `.bubble-shield.json` marker into a
  client folder and everything in it becomes the *coffre*.
- **The anonymiser** (the bundled `/bubble-shield:bubble-shield-anonymize` skill) turns a
  dossier into anonymised copies the AI can safely work on, then de-anonymises
  the answer locally.
- **The tripwire** (a `UserPromptSubmit` hook) nudges you if raw client data is
  pasted directly into the chat.

Everything is **self-contained** — the engine and a pure-python PDF reader are
bundled, so there is nothing to `pip install`. PDFs, Word (`.docx`), and plain
text all work out of the box, offline.

New here? Just ask Claude *“how does Bubble Shield work / help me set it up”* — the
bundled onboarding skill walks you through it in plain language.

## Privacy & RGPD

Pseudonymisation is **local and reversible** — a security measure under RGPD
art. 25 & 32. The token↔value vault never leaves your machine. This tool does not
replace your DPA / AIPD or human review; see the plugin’s `README` and
`RELEASING` for details.

## License

MIT — see the plugin folder.
