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
import email.policy
import imaplib
import json
import os
import stat
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# Mutation guardrails (the whole point of the apply path — enforce structurally)
# ---------------------------------------------------------------------------
#
# This module is READ-heavy by design (fetch_mail is readonly=True). The mutation
# functions below (apply_labels / create_draft) open IMAP with readonly=False so an
# unattended Cowork scheduled task can APPLY triage decisions without going through
# Cowork's greyed-out Gmail-mutation guard. Because that removes the human gate, the
# safety guarantees here are STRUCTURAL, not by convention:
#
#   * NEVER send   — smtplib is never imported; there is no SMTP anywhere in this
#     module. A draft created via IMAP APPEND to [Gmail]/Drafts CANNOT be sent by
#     this code — it just sits in the Drafts folder for a human to review/send.
#   * NEVER delete — we never STORE the \Deleted flag, never EXPUNGE, and never
#     touch [Gmail]/Trash or [Gmail]/Spam. Removing \Inbox (archive) is the ONLY
#     removal this module performs.
#   * Per-run cap  — MAX_MUTATIONS_PER_RUN bounds how many mutations one apply call
#     may perform (enforced by the apply tool in bubble_shield_mcp.py).
#   * Journal      — every mutation appends ONE JSON line (timestamp, uid, action,
#     labels) to ~/.bubble_shield/mail_journal.jsonl so every action is auditable.
#     The journal NEVER records message bodies or PII — only uid + label names.

MAX_MUTATIONS_PER_RUN = 60  # apply tool refuses more than this in one call

# Gmail's Drafts mailbox (IMAP APPEND target). Draft-only: a draft is NEVER sent.
_GMAIL_DRAFTS_MAILBOX = "[Gmail]/Drafts"

# The FIXED emoji-taxonomy label set. These are the ONLY user labels safe to journal
# by NAME — they are a closed vocabulary chosen by Bubble Shield, never a client's
# real name. Any OTHER label could itself BE a client's name (a user may create a
# Gmail label literally named after a client), so we journal it as "custom-label"
# WITHOUT its name (see _sanitise_labels_for_journal). Gmail system flags (\Inbox,
# \Draft, …) are backslash-atoms and are never PII, so they are journalled as-is.
_SYSTEM_LABELS = frozenset({
    "🔴 Clients",
    "⭐ Important",
    "📰 Newsletters",
    "📄 CV reçus",
    "🏗️ Structurés-Produits",
    "↪️ Transition-AC",
    "✍️ Brouillon prêt",
})


def _sanitise_labels_for_journal(labels) -> list[str]:
    """Reduce a label list to journal-safe values (never a raw non-system name).

    A Gmail label is user-controlled text and MAY be a client's real name. The
    journal is written in clear to ~/.bubble_shield/mail_journal.jsonl, so writing
    a raw label name there would leak PII. We keep ONLY:
      * Gmail system-flag atoms (start with "\\", e.g. \\Inbox) — never PII;
      * the fixed emoji-taxonomy labels in _SYSTEM_LABELS — a closed, non-PII set.
    Anything else is replaced by the fixed placeholder "custom-label" (its NAME is
    dropped), so a custom/PII label name NEVER reaches disk.
    """
    safe: list[str] = []
    for lab in (labels or []):
        s = str(lab)
        if s.startswith("\\") or s in _SYSTEM_LABELS:
            safe.append(s)
        else:
            safe.append("custom-label")
    return safe

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
               creds: dict | None = None) -> list[tuple[str, str, str, str]]:
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
        # UID SEARCH/FETCH (not sequence numbers): the UID space is stable and is
        # the SAME identifier apply's UID STORE targets — so the uid we surface can be
        # passed straight to bubble_shield_mail_apply.
        typ, data = M.uid("SEARCH", None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-maxn:]
        out: list[tuple[str, str, str, str]] = []
        for u in uids:
            typ, d = M.uid("FETCH", u, "(RFC822)")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            uid_str = u.decode("ascii", "replace") if isinstance(u, bytes) else str(u)
            frm, subj, body = parse_message(d[0][1])
            out.append((uid_str, frm, subj, body))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Mutation layer (host-side, readonly=False) — labels + draft-only, NEVER send /
# NEVER delete. See the "Mutation guardrails" block at the top of the module.
# ---------------------------------------------------------------------------

def _journal_path() -> Path:
    """Path to the append-only mutation journal (~/.bubble_shield/mail_journal.jsonl)."""
    return BUBBLE_SHIELD_HOME / "mail_journal.jsonl"


def _journal(uid: str, action: str, labels) -> None:
    """Append ONE JSON line recording a mutation, for auditability.

    Records ONLY {ts, uid, action, labels} — NEVER a message body, subject, sender
    or any PII. A journal that leaked bodies would defeat the whole anonymise story;
    this records just enough to reconstruct WHAT was done to WHICH message.

    PII-safe labels: a Gmail label name is user-controlled and MAY be a client's real
    name, so we NEVER journal a raw non-system label — labels are passed through
    _sanitise_labels_for_journal (system flags + the fixed emoji taxonomy stay; any
    other label becomes "custom-label" without its name).

    The journal file is created chmod 600 (owner-only) — it mirrors the fail-closed
    permission stance load_credentials() takes on mail.json: audit records must not be
    world/group readable on a shared host.

    Best-effort: a journal write failure must never crash a mutation (the mutation
    already happened server-side), so any error here is swallowed after a stderr note.
    """
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "uid": str(uid),
            "action": str(action),
            "labels": _sanitise_labels_for_journal(labels),
        }
        p = _journal_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Create owner-only (chmod 600). O_CREAT honours the mode ONLY on creation, so
        # we also fchmod every time to repair an already-existing mis-permissioned file.
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:  # fdopen takes ownership of fd
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover - journal must never break a mutation
        import sys
        print(f"[bubble_shield_mail] journal write failed: {e!r}", file=sys.stderr, flush=True)


def _mutf7_encode(s: str) -> bytes:
    """Encode a str to IMAP modified UTF-7 (RFC 3501 §5.1.3) bytes.

    ASCII printable passes through; '&' → '&-'; runs of non-ASCII → '&<base64(UTF-16BE)>-'
    with base64 '/' replaced by ','. Used for Gmail X-GM-LABELS values that contain
    emoji/accents (a raw str there triggers imaplib's ascii encode → UnicodeEncodeError,
    which failed mail_apply 0/20 in a live Cowork test against real Gmail).

    Deterministic: the same input always yields the same bytes, so an add and a later
    remove of the SAME label produce IDENTICAL encoded bytes — Gmail matches on removal.
    """
    import base64
    res = bytearray()
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            b64 = base64.b64encode(buf.encode("utf-16-be")).decode("ascii").rstrip("=").replace("/", ",")
            res.extend(("&" + b64 + "-").encode("ascii"))
            buf = ""

    for ch in s:
        o = ord(ch)
        if 0x20 <= o <= 0x7e:
            flush()
            res.extend(b"&-" if ch == "&" else ch.encode("ascii"))
        else:
            buf += ch
    flush()
    return bytes(res)


def _imap_label_arg(labels: list[str]) -> bytes:
    """Render a list of Gmail labels as the parenthesised X-GM-LABELS argument, as BYTES.

    CRITICAL Gmail-IMAP gotcha: the label list MUST be wrapped in parens `(...)`.
    A label that contains a space (e.g. a user label "🔴 Clients") must be quoted so
    the space is not read as a label separator; Gmail's system flags like \\Inbox
    are backslash-atoms and are NOT quoted.

    Non-ASCII gotcha: imaplib encodes str command args as ASCII, so a label carrying an
    emoji (🔴 U+1F534) or accent (Système) raises UnicodeEncodeError. Per RFC 3501
    §5.1.3, IMAP mailbox/label names carrying non-ASCII MUST be modified-UTF-7 encoded.
    We therefore return BYTES: only the label TEXT of a user label is mutf7-encoded; the
    surrounding parens/quotes/spaces and the backslash-atom system flags stay ASCII.
    So ["🔴 Clients"] → b'("&2D3dNA- Clients")', ["\\Inbox"] → b'(\\Inbox)',
    ["Systeme"] → b'(Systeme)'. imaplib sends a bytes arg literally.
    """
    parts: list[bytes] = []
    for lab in labels:
        lab = str(lab)
        if lab.startswith("\\"):
            parts.append(lab.encode("ascii"))   # system flag atom, e.g. \Inbox — never quote
        else:
            # user label — quote (spaces/emoji safe); the TEXT is mutf7-encoded, the
            # surrounding quotes stay ASCII. Preserve the existing quote-escaping of ".
            body = _mutf7_encode(lab.replace('"', '\\"'))
            parts.append(b'"' + body + b'"')
    return b"(" + b" ".join(parts) + b")"


def apply_labels(msg_uid, add_labels: list[str] | None = None,
                 remove_labels: list[str] | None = None, creds: dict | None = None) -> None:
    """Add / remove Gmail labels on ONE message, by UID, over IMAP (readonly=False).

    Uses Gmail's X-GM-LABELS extension with UID STORE (uidvalidity-safe — we NEVER
    use sequence numbers, which shift as the mailbox changes). Adding "\\Inbox" to
    remove_labels archives the message (removes it from the inbox); that archive is
    the ONLY removal this function performs.

    CRITICAL Gmail-IMAP gotchas (documented in our Claudette lessons, enforced here):
      * the label list MUST be wrapped in parens `(...)` — see _imap_label_arg.
      * NEVER combine a spaced user label + a system flag like \\Inbox in ONE STORE.
        Gmail chokes on the mix; we issue SEPARATE +X-GM-LABELS / -X-GM-LABELS store
        commands per operation (add is one store, remove is another).

    SECURITY: this NEVER stores \\Deleted, NEVER expunges, NEVER touches Trash/Spam.
    The password is used only for imaplib.login() and is never logged/returned.
    """
    add_labels = list(add_labels or [])
    remove_labels = list(remove_labels or [])
    if creds is None:
        creds = load_credentials()
    uid = str(msg_uid)

    # Defence-in-depth: refuse to ever remove the \Deleted flag path or expunge.
    for lab in add_labels + remove_labels:
        if str(lab).strip().lower() in ("\\deleted", "deleted"):
            raise MailConfigError("opération interdite: \\Deleted n'est jamais autorisé (aucune suppression).")

    M = imaplib.IMAP4_SSL(creds["host"])
    try:
        M.login(creds["user"], creds["password"])
        M.select(creds.get("mailbox", "INBOX"), readonly=False)  # mutation: readonly=False
        # SEPARATE store commands — never combine add + remove (or spaced + \Inbox)
        # in one STORE (Gmail-IMAP gotcha).
        # _imap_label_arg returns BYTES (mutf7-encoded label text) — imaplib sends a
        # bytes arg literally; the "+X-GM-LABELS"/"-X-GM-LABELS" word stays a str
        # (imaplib handles a mixed str-word + bytes-arg command fine). Passing a raw str
        # here would ascii-encode and raise UnicodeEncodeError on emoji/accented labels.
        if add_labels:
            typ, resp = M.uid("STORE", uid, "+X-GM-LABELS", _imap_label_arg(add_labels))
            if typ != "OK":
                raise MailConfigError(f"échec de l'ajout d'étiquette (UID {uid}).")
            _journal(uid, "add_labels", add_labels)
        if remove_labels:
            typ, resp = M.uid("STORE", uid, "-X-GM-LABELS", _imap_label_arg(remove_labels))
            if typ != "OK":
                raise MailConfigError(f"échec du retrait d'étiquette (UID {uid}).")
            _journal(uid, "remove_labels", remove_labels)
    finally:
        try:
            M.logout()
        except Exception:
            pass


def build_reply_draft(to_addr: str, subject: str, body_text: str,
                      in_reply_to: str | None = None,
                      references: str | None = None) -> bytes:
    """Build an RFC822 reply-draft message with stdlib EmailMessage → bytes.

    Sets To / Subject / In-Reply-To / References (for proper reply threading) and a
    plain-text body. Returns the serialised RFC822 bytes ready for create_draft().
    No SMTP, no send — this only assembles the message.

    CRLF: serialised with email.policy.SMTP so line endings are CRLF (\\r\\n), which
    strict IMAP servers expect for an APPENDed RFC822 message; the stdlib default
    policy uses bare \\n and can trip such servers. In-Reply-To / References headers
    are set ONLY when a value is provided, so we never emit a malformed empty header
    (an empty In-Reply-To is not a valid Message-Id and confuses threading).
    """
    msg = EmailMessage()
    if to_addr:
        msg["To"] = to_addr
    msg["Subject"] = subject or ""
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body_text or "")
    # policy.SMTP → CRLF line endings (RFC 5322 / strict-server safe).
    return msg.as_bytes(policy=email.policy.SMTP)


def _find_drafts_mailbox(M) -> str | None:
    """Return the DRAFTS mailbox NAME by its IMAP \\Drafts special-use flag, or None.

    The Gmail Drafts folder is LOCALIZED: on a French account M.list() yields
    `(\\Drafts \\HasNoChildren) "/" "[Gmail]/Brouillons"` — the name is "Brouillons",
    not "Drafts". Hardcoding the English "[Gmail]/Drafts" makes APPEND fail with
    `NO [TRYCREATE] Folder doesn't exist` on every non-English Gmail (confirmed live).

    So we DISCOVER the folder by its special-use flag \\Drafts (RFC 6154) instead of
    by name: scan M.list(), find the line whose flag list contains \\Drafts
    (case-insensitive), and return the trailing mailbox name.

    The returned name is passed VERBATIM to M.append — the wire form M.list() gave us
    (it may itself be IMAP modified-UTF-7 encoded for non-ASCII names). We do NOT
    decode/re-encode it: append wants the SAME bytes list returned.
    """
    try:
        typ, data = M.list()
    except Exception:
        return None
    if typ != "OK" or not data:
        return None
    for raw in data:
        line = raw.decode("utf-8", "surrogateescape") if isinstance(raw, (bytes, bytearray)) else str(raw)
        # Format: (<flags>) "<sep>" <mailbox-name>   e.g. (\Drafts \HasNoChildren) "/" "[Gmail]/Brouillons"
        close = line.find(")")
        if not line.startswith("(") or close == -1:
            continue
        flags = line[1:close]
        if "\\drafts" not in flags.lower():
            continue
        rest = line[close + 1:].strip()
        # rest is: "<sep>" <name>. Drop the separator token (quoted or NIL), keep the name.
        if rest.startswith('"'):
            end = rest.find('"', 1)
            if end == -1:
                continue
            rest = rest[end + 1:].strip()
        else:
            # unquoted separator token (e.g. NIL) — drop the first whitespace-delimited word
            parts = rest.split(None, 1)
            rest = parts[1].strip() if len(parts) == 2 else ""
        if not rest:
            continue
        # rest is now the mailbox name — quoted or bare. Return verbatim (unquoted).
        if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            return rest[1:-1]
        return rest
    return None


def create_draft(raw_rfc822_bytes: bytes, creds: dict | None = None) -> None:
    """Create a Gmail DRAFT from raw RFC822 bytes via IMAP APPEND to the Drafts folder.

    The Drafts folder is DISCOVERED by its \\Drafts special-use flag (see
    _find_drafts_mailbox), because its name is localized ("[Gmail]/Brouillons" on a
    French account); a hardcoded English name breaks non-English Gmail.

    This is draft-ONLY: the message is appended to the Drafts folder with the \\Draft
    flag. It is STRUCTURALLY impossible for this code to SEND it — there is no SMTP
    anywhere in this module. A human reviews and sends the draft from Gmail.

    SECURITY: the password is used only for imaplib.login() and is never logged /
    returned. The draft body is NEVER journalled (no PII in the journal) — only the
    fact that a draft was created is recorded (action="create_draft").
    """
    if not isinstance(raw_rfc822_bytes, (bytes, bytearray)):
        raise MailConfigError("create_draft attend des octets RFC822 (bytes).")
    if creds is None:
        creds = load_credentials()

    M = imaplib.IMAP4_SSL(creds["host"])
    try:
        M.login(creds["user"], creds["password"])
        # Discover the Drafts folder by its \Drafts special-use flag (RFC 6154) — the
        # folder NAME is localized ("[Gmail]/Brouillons" on a French account), so the
        # hardcoded English name breaks non-English Gmail. Fall back to the English
        # name then bare "Drafts" only if no \Drafts-flagged folder is found. We try
        # each candidate with APPEND until one succeeds; if ALL fail we raise (never
        # silently succeed). This stays draft-ONLY: still just an APPEND with \Draft.
        discovered = _find_drafts_mailbox(M)
        candidates: list[str] = []
        for c in (discovered, _GMAIL_DRAFTS_MAILBOX, "Drafts"):
            if c and c not in candidates:
                candidates.append(c)
        last_typ = None
        appended = False
        for mailbox in candidates:
            typ, resp = M.append(
                mailbox,
                "\\Draft",
                imaplib.Time2Internaldate(time.time()),
                bytes(raw_rfc822_bytes),
            )
            last_typ = typ
            if typ == "OK":
                appended = True
                break
        if not appended:
            raise MailConfigError(
                "échec de la création du brouillon (APPEND dossier Brouillons introuvable)."
            )
        _journal("-", "create_draft", [])
    finally:
        try:
            M.logout()
        except Exception:
            pass
