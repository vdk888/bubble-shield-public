# Bubble Shield

**A privacy guard for Claude Cowork / Claude Code.** It stops the AI from reading
your raw client files until the identifying data has been replaced with anonymous
labels — locally, reversibly, and with no data leaving your machine.

Built for financial advisors (CGP/CIF) and anyone who works with client
documents in an AI assistant. 100 % local, no network, no account, no telemetry.

Bubble Shield is **two pieces**:

1. **The plugin** — the *protection*, running inside Cowork / Claude Code. It
   blocks Claude from reading raw client files and reroutes through a local
   anonymiser.
2. **The desktop app** — a small Mac app: the human control surface where you
   review uncertain detections, manage the vault, and the known-PII list.

---

## 1. Install the plugin in Claude Cowork (Desktop)

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
/plugin install bubble-shield@bubble-shield-public
```

---

## 2. Install the desktop app (review / vault / management)

The plugin does the *protection*; the desktop app is the *human control surface*.
Install it once, in Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/vdk888/bubble-shield-public/main/install-app.sh | bash
```

This drops a **Bubble Shield** app on your Desktop. First launch: **right-click →
Open**, once (it's unsigned). Re-run the command any time to update.

**The app's three screens:**

- **File de révision** — confirm or ignore the *low-confidence* detections Bubble
  Shield wasn't sure about (a bare first name, a partial address). This is the
  human safety net — open it periodically to clear the queue.
- **Coffre** — view/correct the token↔value mappings for a dossier (RGPD).
- **Liste connue** — manage the gazetteer of confirmed PII.

> New here? Just ask Claude *“how does Bubble Shield work / help me set it up”* — the
> bundled onboarding skill walks you through it in plain language.

---

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

See the [plugin README](plugin/bubble-shield/README.md) for the full
configuration reference (markers, `protected_folders`, exemptions, the tripwire).

## Privacy & RGPD

Pseudonymisation is **local and reversible** — a security measure under RGPD
art. 25 & 32. The token↔value vault never leaves your machine. This tool does not
replace your DPA / AIPD or human review; see the plugin’s `README` and
`RELEASING` for details.

## License

MIT — see the plugin folder.
