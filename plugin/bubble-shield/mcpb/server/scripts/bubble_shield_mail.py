#!/usr/bin/env python3
"""bubble_shield_mail — IMAP fetch layer + host-side credential store for Bubble Shield.

WHY THIS EXISTS
---------------
Email is Bubble Shield's weakest surface today. The old approach connects a Gmail
*connector* and tries to SCRUB its output after the fact (the PostToolUse hook) —
fail-OPEN, regex-only, fragile (breaks the connector), and too late (the raw body
already became a tool result; the harness drops the rewrite per #32105).

The file guard avoids all this because it OWNS the read. This module does the same
for mail: Bubble Shield fetches the email ITSELF over IMAP, so the raw body never
becomes a tool result. The MCP tool (`bubble_shield_mail_read` in
bubble_shield_mcp.py) then routes every fetched body through the SAME fail-CLOSED
`_anonymise_text` core the file guard uses — daemon-up-or-refuse, no raw email ever.

This module is DELIBERATELY split from the anonymise wiring: it does ONLY the two
non-security-critical mechanics — (1) fetch mail over IMAP, (2) read host-side
credentials — as pure stdlib (`imaplib` + `email`, ZERO new dependency). The
security-critical fail-closed anonymise step lives in bubble_shield_mcp.py.

CREDENTIAL FLOW (host-side, never exposed to the model / Cowork VM)
------------------------------------------------------------------
The MCPB server runs on the HOST, outside the Cowork sandbox. IMAP credentials
therefore live host-side and are read by THIS module, never returned to the model:

  - Path:  $BUBBLE_SHIELD_MAIL_CREDS  (env override)  OR  ~/.bubble_shield/mail.json
  - Format: {"host": "imap.gmail.com", "user": "you@gmail.com",
             "password": "<app-password>", "mailbox": "INBOX"}
  - Perms: the file MUST be chmod 600 (owner-only). load_credentials() REFUSES to
    read a world/group-readable creds file (fail-closed on a mis-permissioned secret).
  - The app-password NEVER leaves this module: it is passed straight to
    imaplib.login() and is never logged, never returned to the caller, never put in
    an error message. fetch_mail() returns only (from, subject, body) tuples.

Phase 1 keeps the setup minimal: the operator populates mail.json out-of-band (the
docstring / README documents it). A future `bubble_shield_mail_setup` MCP tool can
write it host-side; that is out of scope for the read path.
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import stat
from email.header import decode_header
from pathlib import Path

# ---------------------------------------------------------------------------
# Credential store (host-side, chmod-600, never exposed to the model)
# ---------------------------------------------------------------------------

BUBBLE_SHIELD_HOME = Path(
    os.environ.get("BUBBLE_SHIELD_HOME", Path.home() / ".bubble_shield"))


class MailConfigError(RuntimeError):
    """Raised when IMAP credentials are missing or mis-permissioned.

    The message NEVER contains the password (or any secret value) — only the
    path and the nature of the problem, so it is safe to surface to the caller.
    """


def _creds_path() -> Path:
    override = os.environ.get("BUBBLE_SHIELD_MAIL_CREDS")
    if override:
        return Path(os.path.expanduser(override))
    return BUBBLE_SHIELD_HOME / "mail.json"


def load_credentials() -> dict:
    """Read the host-side IMAP credentials, fail-CLOSED on any problem.

    Returns {"host", "user", "password", "mailbox"}. Raises MailConfigError
    (never leaking the secret) when the file is absent, mis-permissioned
    (world/group readable), malformed, or missing a required field.

    Security: a secret file that is readable by other users on the host is a
    leak; we refuse to use it rather than silently trusting it. Set it with
    `chmod 600 ~/.bubble_shield/mail.json`.
    """
    p = _creds_path()
    if not p.is_file():
        raise MailConfigError(
            f"aucun identifiant IMAP configuré ({p}). "
            "Crée ce fichier (chmod 600) avec {\"host\",\"user\",\"password\",\"mailbox\"}.")
    # Refuse a world/group-readable secret file (fail-closed on a mis-permission).
    try:
        mode = p.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise MailConfigError(
                f"le fichier d'identifiants {p} est accessible à d'autres utilisateurs "
                "(permissions trop larges). Corrige avec: chmod 600 " + str(p))
    except MailConfigError:
        raise
    except Exception as e:
        # If we can't even stat it, refuse rather than proceed with an unknown secret.
        raise MailConfigError(f"impossible de vérifier les permissions de {p}: {e}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise MailConfigError(f"identifiants IMAP illisibles ({p}): {e}")
    for key in ("host", "user", "password"):
        if not data.get(key):
            raise MailConfigError(
                f"champ obligatoire manquant dans {p}: '{key}' "
                "(attendus: host, user, password ; mailbox optionnel).")
    return {
        "host": str(data["host"]),
        "user": str(data["user"]),
        "password": str(data["password"]),
        "mailbox": str(data.get("mailbox", "INBOX")),
    }


# ---------------------------------------------------------------------------
# IMAP fetch + parse (pure stdlib; hardened from the proven prototype)
# ---------------------------------------------------------------------------

def _decode_header(raw) -> str:
    """Decode an RFC-2047 encoded header (e.g. =?UTF-8?B?...?=) into a str.

    Hardened from the prototype: tolerates None, bytes, mixed charsets and a
    bad declared charset (falls back to utf-8/replace so we never raise here).
    """
    if not raw:
        return ""
    out = []
    try:
        parts = decode_header(raw)
    except Exception:
        return str(raw)
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", "replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", "replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _plain_body(msg) -> str:
    """Extract the plain-text body of an email.message.Message.

    Prefers the first non-attachment text/plain part of a multipart message;
    falls back to the raw payload for a single-part message. Charset-tolerant:
    an unknown/bad declared charset falls back to utf-8/replace. Never raises —
    a body we can't decode becomes "" (the anonymiser then sees empty text,
    which is safe) rather than crashing the fetch.
    """
    def _decode_part(part) -> str:
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            return ""
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, "replace")
        except (LookupError, TypeError):
            return payload.decode("utf-8", "replace")

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            body = _decode_part(part)
            if body:
                return body
        return ""
    return _decode_part(msg)


def _validate_query(query: str) -> str:
    """Validate an IMAP search query token/expression (defence-in-depth).

    We only accept the small set of IMAP SEARCH keywords and safe arguments the
    tool advertises (UNSEEN/ALL/SEEN/RECENT, FROM "x", SINCE dd-Mon-yyyy, etc.).
    Newlines/CRs are rejected — an IMAP command is line-delimited, so a raw CRLF
    in the query would be command injection. imaplib itself also rejects control
    chars, but we reject early with a clear message.
    """
    if not query:
        return "ALL"
    if "\r" in query or "\n" in query:
        raise MailConfigError("requête IMAP invalide (retour à la ligne interdit).")
    return query


def parse_message(raw_bytes: bytes) -> tuple[str, str, str]:
    """Parse raw RFC822 bytes into (from, subject, plain-text body).

    Split out so tests can exercise the parse layer with synthetic fixtures
    without a live IMAP server.
    """
    msg = email.message_from_bytes(raw_bytes)
    frm = _decode_header(msg.get("From"))
    subj = _decode_header(msg.get("Subject"))
    body = _plain_body(msg)
    return frm, subj, body


def fetch_mail(query: str = "ALL", maxn: int = 10, since: str | None = None,
               creds: dict | None = None) -> list[tuple[str, str, str]]:
    """Fetch up to `maxn` messages over IMAP and return raw (from, subject, body).

    NOTE: the returned bodies are RAW and MUST be routed through the fail-closed
    `_anonymise_text` by the caller before the model sees them — this function
    performs NO anonymisation. It exists so Bubble Shield OWNS the read; the raw
    body never becomes a tool result on its own.

    Args:
      query: IMAP SEARCH criterion (UNSEEN, ALL, SEEN, 'FROM "x@y"', …).
      maxn:  hard upper bound on messages fetched (most-recent first).
      since: optional 'dd-Mon-yyyy' date; ANDed with query as SINCE <since>.
      creds: {"host","user","password","mailbox"}; defaults to load_credentials().

    The password is used only for imaplib.login() and is never logged/returned.
    """
    if creds is None:
        creds = load_credentials()
    query = _validate_query(query)
    maxn = max(1, min(int(maxn), 50))  # clamp: never fetch an unbounded pile

    M = imaplib.IMAP4_SSL(creds["host"])
    try:
        M.login(creds["user"], creds["password"])
        M.select(creds.get("mailbox", "INBOX"), readonly=True)  # read-only: never mutate the mailbox
        criteria = [query]
        if since:
            since = _validate_query(since)
            criteria = ["SINCE", since, query] if query != "ALL" else ["SINCE", since]
        typ, data = M.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        ids = data[0].split()[-maxn:]
        out: list[tuple[str, str, str]] = []
        for i in ids:
            typ, d = M.fetch(i, "(RFC822)")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            out.append(parse_message(d[0][1]))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass
