#!/usr/bin/env python3
"""Tests for the mail MUTATION path — apply_labels / create_draft (bubble_shield_mail)
and the bubble_shield_mail_apply MCP tool.

The whole point of this path is that an unattended Cowork scheduled task can APPLY
triage decisions (labels / archive / reply-draft) host-side, bypassing Cowork's
greyed-out Gmail-mutation guard. Because that removes the human gate, the safety
guarantees must be STRUCTURAL. These tests prove them by construction:

  * NEVER send   — smtplib is never imported / referenced in the source.
  * NEVER delete — no \\Deleted STORE, no expunge, no Trash/Spam anywhere in the source.
  * Per-run cap  — a decisions list longer than MAX_MUTATIONS_PER_RUN is REJECTED.
  * Draft mechanics — create_draft/build_reply_draft produce valid RFC822 (parse back).
  * Gmail-IMAP label gotchas — the STORE arg is parenthesised, and a spaced user label
    is NEVER combined with \\Inbox in one STORE (separate store commands).

All IMAP is MOCKED — no real Gmail is ever contacted. A fake IMAP4_SSL records every
login / select / uid-STORE / append call so the emitted command strings can be asserted.
"""
import ast
import io
import sys
import tokenize
from email import message_from_bytes
from email.policy import default as _default_policy
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mail as mail  # noqa: E402

MAIL_PATH = _SCRIPTS / "bubble_shield_mail.py"
MAIL_SRC = MAIL_PATH.read_text(encoding="utf-8")


def _code_only_src(src: str) -> str:
    """Return the source with all comments AND string literals/docstrings stripped.

    The security documentation in this module deliberately MENTIONS smtplib / Trash /
    Spam / expunge (to say it never uses them). A prose grep would false-positive on
    that. Tokenizing and dropping COMMENT + STRING tokens leaves only executable
    code, so the tripwires assert on what the module actually DOES, not what it says."""
    out = []
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok in toks:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out).lower()


MAIL_CODE = _code_only_src(MAIL_SRC)

CREDS = {"host": "imap.example.test", "user": "u@example.test",
         "password": "app-pw", "mailbox": "INBOX"}


# ---------------------------------------------------------------------------
# Fake IMAP4_SSL — records every call; never touches the network.
# ---------------------------------------------------------------------------
class FakeIMAP:
    instances = []

    def __init__(self, host):
        self.host = host
        self.logged_in = False
        self.selected = None
        self.readonly = None
        self.uid_calls = []      # list of (command, *args)
        self.append_calls = []   # list of (mailbox, flags, date, message_bytes)
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.logged_in = True
        self.user = user
        return ("OK", [b"logged in"])

    def select(self, mailbox="INBOX", readonly=True):
        self.selected = mailbox
        self.readonly = readonly
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.uid_calls.append((command, *args))
        return ("OK", [b"1 (OK)"])

    def append(self, mailbox, flags, date, message):
        self.append_calls.append((mailbox, flags, date, message))
        return ("OK", [b"[APPENDUID 1 1]"])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"bye"])


@pytest.fixture()
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


# ---------------------------------------------------------------------------
# STRUCTURAL: never send / never delete (source-level tripwires)
# ---------------------------------------------------------------------------
def test_source_never_imports_smtplib():
    """smtplib must NEVER be imported by the mutation module — no SMTP == cannot send.

    Checked against the parsed AST (imports only) AND code-only tokens, so the module's
    own documentation that MENTIONS smtplib in prose does not false-positive."""
    tree = ast.parse(MAIL_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] != "smtplib" for a in node.names), \
                "smtplib imported — send is possible!"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "smtplib", \
                "smtplib imported — send is possible!"
    assert "smtplib" not in MAIL_CODE, "smtplib referenced in executable code!"


def test_source_has_no_delete_or_expunge():
    """No \\Deleted STORE, no expunge, no Trash/Spam in CODE — archive is the only removal.

    Scans code-only tokens (comments + docstrings stripped) so the safety docstring
    that says 'we never touch Trash/Spam/expunge' does not trip the tripwire."""
    assert ".expunge(" not in MAIL_CODE and "expunge (" not in MAIL_CODE, "expunge present — deletion is possible!"
    assert "trash" not in MAIL_CODE, "Trash mailbox referenced in code!"
    assert "spam" not in MAIL_CODE, "Spam mailbox referenced in code!"
    # \Deleted must never be constructed as a label to add (only the guard REJECTS it,
    # and that guard string is a docstring/string literal, stripped from MAIL_CODE).
    assert "deleted" not in MAIL_CODE, "\\Deleted referenced in executable code!"


def test_apply_labels_refuses_deleted_flag(fake_imap):
    """Defence-in-depth: apply_labels rejects a \\Deleted label outright."""
    with pytest.raises(mail.MailConfigError):
        mail.apply_labels("123", add_labels=["\\Deleted"], creds=CREDS)


# ---------------------------------------------------------------------------
# Per-run cap
# ---------------------------------------------------------------------------
def test_max_mutations_constant_is_60():
    assert mail.MAX_MUTATIONS_PER_RUN == 60


def test_apply_tool_rejects_over_cap(monkeypatch):
    """_apply_mail refuses a decisions list longer than the cap — fail-closed, whole call."""
    import bubble_shield_mcp as mcp
    # Should refuse BEFORE touching IMAP / creds, so no credential fixture is needed.
    over = [{"uid": str(i), "add_labels": ["x"]} for i in range(mail.MAX_MUTATIONS_PER_RUN + 1)]
    with pytest.raises(Exception) as ei:
        mcp._apply_mail(over)
    assert "limite" in str(ei.value).lower() or "trop" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# create_draft / build_reply_draft → valid RFC822
# ---------------------------------------------------------------------------
def test_build_reply_draft_valid_rfc822():
    raw = mail.build_reply_draft(
        to_addr="alice@example.test",
        subject="Re: dossier",
        body_text="Bonjour Alice,\n\nMerci.",
        in_reply_to="<orig@example.test>",
        references="<orig@example.test>")
    assert isinstance(raw, (bytes, bytearray))
    msg = message_from_bytes(bytes(raw), policy=_default_policy)
    assert msg["To"] == "alice@example.test"
    assert msg["Subject"] == "Re: dossier"
    assert msg["In-Reply-To"] == "<orig@example.test>"
    assert msg["References"] == "<orig@example.test>"
    assert "Merci." in msg.get_content()


def test_create_draft_appends_to_gmail_drafts(fake_imap):
    raw = mail.build_reply_draft("a@b.test", "Re: x", "body", in_reply_to="<i@d>")
    mail.create_draft(raw, creds=CREDS)
    assert len(fake_imap.instances) == 1
    inst = fake_imap.instances[0]
    assert inst.logged_in and inst.logged_out
    assert len(inst.append_calls) == 1
    mailbox, flags, _date, message = inst.append_calls[0]
    assert mailbox == "[Gmail]/Drafts"
    assert flags == "\\Draft"
    # the appended bytes parse back to the same RFC822
    parsed = message_from_bytes(bytes(message))
    assert parsed["Subject"] == "Re: x"


def test_create_draft_rejects_non_bytes(fake_imap):
    with pytest.raises(mail.MailConfigError):
        mail.create_draft("not bytes", creds=CREDS)


# ---------------------------------------------------------------------------
# Gmail-IMAP label gotchas — parens + separate STORE for \Inbox vs spaced label
# ---------------------------------------------------------------------------
def test_label_arg_is_parenthesised_and_quotes_spaced_labels():
    # _imap_label_arg now returns BYTES with the user-label TEXT modified-UTF-7 encoded
    # (RFC 3501 §5.1.3) — a raw str with emoji would ascii-encode → UnicodeEncodeError.
    arg = mail._imap_label_arg(["🔴 Clients", "\\Inbox"])
    assert isinstance(arg, bytes), "STORE arg must be BYTES (mutf7-encoded, imaplib-safe)"
    assert arg.startswith(b"(") and arg.endswith(b")"), "label list MUST be wrapped in parens"
    # spaced user label quoted; its text is mutf7-encoded (🔴 → &2D3dNA-)
    assert b'"&2D3dNA- Clients"' in arg, "spaced user label must be quoted + mutf7-encoded"
    assert b"\\Inbox" in arg and b'"\\Inbox"' not in arg, "system flag \\Inbox must NOT be quoted"
    # no raw non-ASCII may survive into the wire bytes (the UnicodeEncodeError root cause)
    assert all(b < 128 for b in arg), "STORE arg must be pure-ASCII bytes (no raw non-ASCII)"


def test_apply_labels_uses_uid_store_with_parens(fake_imap):
    mail.apply_labels("42", add_labels=["🔴 Clients"], remove_labels=["\\Inbox"], creds=CREDS)
    inst = fake_imap.instances[0]
    # mutation opens the mailbox read-WRITE
    assert inst.readonly is False
    # UID STORE (never sequence numbers)
    assert all(c[0] == "STORE" for c in inst.uid_calls)
    # SEPARATE store commands: one +X-GM-LABELS (add), one -X-GM-LABELS (remove)
    ops = [c[2] for c in inst.uid_calls]  # ("STORE", uid, op, labelarg)
    assert "+X-GM-LABELS" in ops
    assert "-X-GM-LABELS" in ops
    assert len(inst.uid_calls) == 2, "add and remove must be SEPARATE STORE commands"
    # every STORE arg is parenthesised BYTES with no raw non-ASCII (regression guard for
    # the live UnicodeEncodeError: a str arg carrying 🔴 would blow up in imaplib).
    for cmd, uid, op, labelarg in inst.uid_calls:
        assert uid == "42"
        assert isinstance(labelarg, bytes), "STORE arg passed to M.uid must be BYTES"
        assert labelarg.startswith(b"(") and labelarg.endswith(b")")
        assert all(b < 128 for b in labelarg), "STORE arg must contain NO raw non-ASCII"
    # the spaced label (mutf7 &2D3dNA- Clients) and \Inbox are NEVER in the same STORE
    for cmd, uid, op, labelarg in inst.uid_calls:
        assert not (b"&2D3dNA- Clients" in labelarg and b"\\Inbox" in labelarg), \
            "spaced label must NOT be combined with \\Inbox in one STORE"


def test_apply_labels_archive_removes_inbox(fake_imap):
    mail.apply_labels("7", add_labels=[], remove_labels=["\\Inbox"], creds=CREDS)
    inst = fake_imap.instances[0]
    assert len(inst.uid_calls) == 1
    cmd, uid, op, labelarg = inst.uid_calls[0]
    assert op == "-X-GM-LABELS"
    # \Inbox is ASCII so it passes through mutf7 untouched — archive path is unchanged.
    assert b"\\Inbox" in labelarg


# ---------------------------------------------------------------------------
# Journal — records uid + action + labels, NO body / PII
# ---------------------------------------------------------------------------
def test_journal_records_action_no_body(fake_imap, tmp_path, monkeypatch):
    import json
    jpath = tmp_path / "mail_journal.jsonl"
    monkeypatch.setattr(mail, "_journal_path", lambda: jpath)
    mail.apply_labels("99", add_labels=["🔴 Clients"], remove_labels=["\\Inbox"], creds=CREDS)
    lines = jpath.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # one add, one remove
    recs = [json.loads(l) for l in lines]
    for r in recs:
        assert set(r.keys()) == {"ts", "uid", "action", "labels"}
        assert r["uid"] == "99"
    actions = {r["action"] for r in recs}
    assert actions == {"add_labels", "remove_labels"}


# ---------------------------------------------------------------------------
# Fix B1a — journal never records a raw (non-system) label NAME (could be PII)
# ---------------------------------------------------------------------------
def test_journal_masks_custom_label_names(fake_imap, tmp_path, monkeypatch):
    """A Gmail label may itself be a client's real name. The journal must NEVER
    write a non-system label name in clear — it becomes 'custom-label'."""
    import json
    jpath = tmp_path / "mail_journal.jsonl"
    monkeypatch.setattr(mail, "_journal_path", lambda: jpath)
    # "DURAND Théophile" is a (fake) client name a user could have made a label of.
    mail.apply_labels("5", add_labels=["DURAND Théophile", "🔴 Clients"],
                      remove_labels=["\\Inbox"], creds=CREDS)
    raw = jpath.read_text(encoding="utf-8")
    assert "DURAND" not in raw, "raw client-name label leaked into the journal!"
    assert "Théophile" not in raw
    recs = [json.loads(l) for l in raw.strip().splitlines()]
    add_rec = next(r for r in recs if r["action"] == "add_labels")
    # system label kept verbatim, custom label masked
    assert "🔴 Clients" in add_rec["labels"]
    assert "custom-label" in add_rec["labels"]
    assert "DURAND Théophile" not in add_rec["labels"]
    # \Inbox (system flag atom) is safe and kept as-is
    rm_rec = next(r for r in recs if r["action"] == "remove_labels")
    assert rm_rec["labels"] == ["\\Inbox"]


def test_journal_keeps_all_system_taxonomy_labels(fake_imap, tmp_path, monkeypatch):
    """Every fixed emoji-taxonomy label is non-PII and journalled by name."""
    import json
    jpath = tmp_path / "mail_journal.jsonl"
    monkeypatch.setattr(mail, "_journal_path", lambda: jpath)
    sys_labels = sorted(mail._SYSTEM_LABELS)
    mail.apply_labels("1", add_labels=sys_labels, creds=CREDS)
    rec = json.loads(jpath.read_text(encoding="utf-8").strip().splitlines()[0])
    assert sorted(rec["labels"]) == sys_labels
    assert "custom-label" not in rec["labels"]


# ---------------------------------------------------------------------------
# Fix B1b — journal file is created chmod 600 (owner-only), like mail.json
# ---------------------------------------------------------------------------
def test_journal_file_is_chmod_600(fake_imap, tmp_path, monkeypatch):
    import stat as _stat
    jpath = tmp_path / "mail_journal.jsonl"
    monkeypatch.setattr(mail, "_journal_path", lambda: jpath)
    mail.apply_labels("1", add_labels=["🔴 Clients"], creds=CREDS)
    mode = _stat.S_IMODE(jpath.stat().st_mode)
    assert mode == 0o600, f"journal must be chmod 600, got {oct(mode)}"


def test_journal_repairs_mispermissioned_file(fake_imap, tmp_path, monkeypatch):
    """A pre-existing world/group-readable journal is chmod'd back to 600 on write."""
    import stat as _stat
    jpath = tmp_path / "mail_journal.jsonl"
    jpath.write_text("", encoding="utf-8")
    jpath.chmod(0o644)  # deliberately too-wide
    monkeypatch.setattr(mail, "_journal_path", lambda: jpath)
    mail.apply_labels("1", add_labels=["🔴 Clients"], creds=CREDS)
    mode = _stat.S_IMODE(jpath.stat().st_mode)
    assert mode == 0o600, f"journal perms not repaired, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Fix B3 — RFC822 uses CRLF line endings + no empty threading header
# ---------------------------------------------------------------------------
def test_build_reply_draft_uses_crlf():
    raw = bytes(mail.build_reply_draft(
        to_addr="a@b.test", subject="Re: x", body_text="ligne1\nligne2"))
    # header/body separator and line endings must be CRLF for strict servers
    assert b"\r\n" in raw, "RFC822 must use CRLF line endings"
    # no bare LF that isn't part of a CRLF pair
    assert b"\n" in raw
    bare_lf = raw.replace(b"\r\n", b"")
    assert b"\n" not in bare_lf, "found a bare LF not paired as CRLF"


def test_build_reply_draft_omits_empty_threading_headers():
    """In-Reply-To / References set ONLY when provided — never a malformed empty header."""
    raw = bytes(mail.build_reply_draft(
        to_addr="a@b.test", subject="Re: x", body_text="body"))  # no in_reply_to
    parsed = message_from_bytes(raw)
    assert parsed["In-Reply-To"] is None
    assert parsed["References"] is None


# ---------------------------------------------------------------------------
# Fix B2 — a draft whose restored body still carries an unresolved ⟦…⟧ token
#          is SKIPPED (never APPENDed) while labels/archive still apply.
# ---------------------------------------------------------------------------
def test_apply_mail_remove_labels_and_unarchive(monkeypatch):
    """Correction flow: remove_labels re-tags a mistagged mail and unarchive brings
    an archived mail back into the inbox — \\Inbox is ALWAYS a separate STORE from
    user labels (never mixed in one command)."""
    import bubble_shield_mcp as mcp
    calls = []

    def fake_apply_labels(uid, add_labels=None, remove_labels=None, creds=None):
        calls.append((uid, list(add_labels or []), list(remove_labels or [])))

    monkeypatch.setattr(mail, "apply_labels", fake_apply_labels)
    monkeypatch.setattr(mail, "load_credentials", lambda: CREDS)

    # change category: remove 🔴 Clients, add 📰 Newsletters (one user-label STORE pair)
    mcp._apply_mail([{"uid": "7", "remove_labels": ["🔴 Clients"],
                      "add_labels": ["📰 Newsletters"]}])
    assert calls == [("7", ["📰 Newsletters"], ["🔴 Clients"])]

    # unarchive: \Inbox added in its OWN call, never mixed with a user label
    calls.clear()
    mcp._apply_mail([{"uid": "7", "add_labels": ["⭐ Important"], "unarchive": True}])
    assert ("7", ["⭐ Important"], []) in calls          # user label alone
    assert ("7", ["\\Inbox"], []) in calls               # \Inbox alone
    # a caller who wrongly puts \Inbox in add_labels: it's stripped from the user set
    calls.clear()
    mcp._apply_mail([{"uid": "7", "add_labels": ["\\Inbox", "⭐ Important"]}])
    assert calls == [("7", ["⭐ Important"], [])]         # \Inbox dropped from user add


def test_apply_mail_skips_draft_with_unresolved_token(monkeypatch):
    """If _deanonymise_string leaves a literal ⟦…⟧ token, the draft is SKIPPED
    (no APPEND), the labels are still applied, and the counts report the skip."""
    import bubble_shield_mcp as mcp

    applied = {"labels": [], "drafts": []}

    def fake_apply_labels(uid, add_labels=None, remove_labels=None, creds=None):
        applied["labels"].append((uid, add_labels, remove_labels))

    def fake_create_draft(raw, creds=None):
        applied["drafts"].append(raw)  # must NOT be called for the unresolved draft

    # restore leaves the token as-is (simulating "no vault entry")
    monkeypatch.setattr(mcp, "_deanonymise_string", lambda s: s)
    monkeypatch.setattr(mail, "apply_labels", fake_apply_labels)
    monkeypatch.setattr(mail, "create_draft", fake_create_draft)
    monkeypatch.setattr(mail, "load_credentials", lambda: CREDS)

    decisions = [{
        "uid": "42",
        "add_labels": ["🔴 Clients"],
        "archive": True,
        "draft": {"to": "a@b.test", "subject": "Re: x",
                  "body_tokens": "Bonjour ⟦NOM_0001⟧"},  # 4+ digits → valid TOKEN_RE
    }]
    summary = mcp._apply_mail(decisions)
    # labels applied — user labels and the \Inbox archive are now SEPARATE STORE
    # calls (never mix a spaced/emoji label with \Inbox in one command).
    assert applied["labels"] == [
        ("42", ["🔴 Clients"], []),      # user-label add (remove_labels defaults to [])
        ("42", None, ["\\Inbox"]),       # archive = remove \Inbox, on its own
    ]
    # ...but the draft was NOT appended
    assert applied["drafts"] == [], "draft with unresolved token must NOT be appended"
    assert "ignoré" in summary and "0 brouillon(s) créé" in summary
    # the raw token must not appear in the returned summary either
    assert "⟦NOM" not in summary


def test_apply_mail_creates_draft_when_fully_resolved(monkeypatch):
    """Control: a draft with NO leftover token IS appended."""
    import bubble_shield_mcp as mcp
    applied = {"drafts": []}
    monkeypatch.setattr(mcp, "_deanonymise_string", lambda s: s.replace("⟦NOM_0001⟧", "Alice"))
    monkeypatch.setattr(mail, "apply_labels", lambda *a, **k: None)
    monkeypatch.setattr(mail, "create_draft", lambda raw, creds=None: applied["drafts"].append(raw))
    monkeypatch.setattr(mail, "load_credentials", lambda: CREDS)
    decisions = [{"uid": "7", "draft": {"to": "a@b.test", "subject": "Re: x",
                                        "body_tokens": "Bonjour ⟦NOM_0001⟧"}}]
    summary = mcp._apply_mail(decisions)
    assert len(applied["drafts"]) == 1
    assert "1 brouillon(s) créé" in summary
    # restored real name must NEVER appear in the returned summary
    assert "Alice" not in summary


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
