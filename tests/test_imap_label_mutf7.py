#!/usr/bin/env python3
"""Regression tests for IMAP modified-UTF-7 encoding of Gmail X-GM-LABELS args.

LIVE BUG (v1.21.3, Cowork test): bubble_shield_mail_apply failed 0/20 with
UnicodeEncodeError. Root cause: _imap_label_arg built the X-GM-LABELS STORE argument
as a *str* containing raw emoji/accented label names (e.g. "🔴 Clients", "Système").
imaplib encodes command args as ASCII → UnicodeEncodeError on 🔴 (U+1F534) / accents.

FIX (verified LIVE against real Gmail by the requester): the non-ASCII label text is
encoded to modified UTF-7 (RFC 3501 §5.1.3) and the whole parenthesised arg is passed
as BYTES → Gmail stores "🔴 Clients" as `&2D3dNA- Clients` and it round-trips.

These tests assert the encoder, the byte-level STORE arg, and — via a mocked IMAP —
that the arg reaching M.uid("STORE", …) is BYTES with NO raw non-ASCII. No real Gmail.
"""
import base64
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mail as mail  # noqa: E402

CREDS = {"host": "imap.example.test", "user": "u@example.test",
         "password": "app-pw", "mailbox": "INBOX"}


def _mutf7_decode(b: bytes) -> str:
    """Independent modified-UTF-7 decoder (RFC 3501 §5.1.3) — the inverse of the
    encoder under test. Used to prove add/remove round-trip; kept separate from the
    implementation so the test doesn't just re-run the same code it validates."""
    s = b.decode("ascii")
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "&":
            j = s.index("-", i)
            chunk = s[i + 1:j]
            if chunk == "":
                out.append("&")          # '&-' → literal '&'
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# _mutf7_encode — the encoder itself
# ---------------------------------------------------------------------------
def test_mutf7_encode_emoji_label_exact_live_value():
    """The exact byte string the requester verified Gmail stores for "🔴 Clients"."""
    assert mail._mutf7_encode("🔴 Clients") == b"&2D3dNA- Clients"


def test_mutf7_encode_accented_is_pure_ascii_and_roundtrips():
    """Accented label ("Système") → pure-ASCII bytes that decode back to the original."""
    enc = mail._mutf7_encode("Système")
    assert isinstance(enc, bytes)
    assert all(b < 128 for b in enc), "encoded output must be pure-ASCII bytes"
    assert enc == b"Syst&AOg-me"
    assert _mutf7_decode(enc) == "Système"


def test_mutf7_encode_plain_ascii_passes_through():
    assert mail._mutf7_encode("plain") == b"plain"


def test_mutf7_encode_ampersand_is_escaped():
    """'&' is the mutf7 shift char — a literal '&' MUST be emitted as '&-'."""
    assert mail._mutf7_encode("a&b") == b"a&-b"
    assert mail._mutf7_encode("&") == b"&-"


def test_mutf7_encode_is_deterministic_for_add_remove_symmetry():
    """CRITICAL: add then remove of the SAME label must produce IDENTICAL bytes, or
    Gmail won't match the label on removal. The encoder is a pure function → identical."""
    for lab in ("🔴 Clients", "Système", "✍️ Brouillon prêt", "↪️ Transition-AC"):
        assert mail._mutf7_encode(lab) == mail._mutf7_encode(lab)


@pytest.mark.parametrize("lab", sorted(mail._SYSTEM_LABELS))
def test_mutf7_encode_every_taxonomy_label_is_ascii_and_roundtrips(lab):
    """Every fixed emoji-taxonomy label (🔴🟢⭐↪️📰🏗️📄✍️ …) encodes to pure-ASCII
    bytes and decodes back to itself — no label produces something unexpected on the
    wire, and none breaks the add/remove round-trip (variation-selector emoji included)."""
    enc = mail._mutf7_encode(lab)
    assert all(b < 128 for b in enc), f"{lab!r} produced non-ASCII bytes: {enc!r}"
    assert _mutf7_decode(enc) == lab, f"{lab!r} did not round-trip"


# ---------------------------------------------------------------------------
# _imap_label_arg — the full parenthesised BYTES argument
# ---------------------------------------------------------------------------
def test_imap_label_arg_quotes_and_encodes_user_label():
    assert mail._imap_label_arg(["🔴 Clients"]) == b'("&2D3dNA- Clients")'


def test_imap_label_arg_system_flag_stays_ascii_unquoted():
    assert mail._imap_label_arg(["\\Inbox"]) == b'(\\Inbox)'


def test_imap_label_arg_pure_ascii_user_label_passes_through_unchanged():
    """A pure-ASCII user label is quoted (unchanged behavior) and its text passes
    through mutf7 untouched — so the wire bytes are byte-for-byte what they were before
    this fix for ASCII labels (the fix only affects labels that CARRY non-ASCII)."""
    assert mail._imap_label_arg(["Systeme"]) == b'("Systeme")'


def test_imap_label_arg_mixed_quotes_user_leaves_flag_atom():
    """A mixed list quotes+encodes the user label and leaves the \\Inbox flag as a bare
    atom — both inside one parenthesised BYTES arg."""
    arg = mail._imap_label_arg(["🔴 Clients", "\\Inbox"])
    assert isinstance(arg, bytes)
    assert arg == b'("&2D3dNA- Clients" \\Inbox)'


def test_imap_label_arg_preserves_quote_escaping():
    """A literal double-quote inside a label stays escaped as \\" (before mutf7)."""
    arg = mail._imap_label_arg(['a"b'])
    assert arg == b'("a\\"b")'
    assert all(byte < 128 for byte in arg)


def test_imap_label_arg_never_emits_raw_non_ascii():
    """No matter the label, the wire bytes are pure-ASCII (the UnicodeEncodeError guard)."""
    arg = mail._imap_label_arg(sorted(mail._SYSTEM_LABELS) + ["Système", "🗑 x"])
    assert isinstance(arg, bytes)
    assert all(b < 128 for b in arg)


# ---------------------------------------------------------------------------
# Mocked IMAP — the STORE arg reaching M.uid must be BYTES, no raw non-ASCII
# ---------------------------------------------------------------------------
class _FakeIMAP:
    instances = []

    def __init__(self, host):
        self.host = host
        self.uid_calls = []
        _FakeIMAP.instances.append(self)

    def login(self, user, password):
        return ("OK", [b"ok"])

    def select(self, mailbox="INBOX", readonly=True):
        return ("OK", [b"1"])

    def uid(self, command, *args):
        self.uid_calls.append((command, *args))
        return ("OK", [b"1 (OK)"])

    # append/list added for the create_draft (French-Drafts) tests below; harmless here.
    list_response = ("OK", [b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Brouillons"'])

    def list(self, directory='""', pattern="*"):
        return _FakeIMAP.list_response

    def append(self, mailbox, flags, date_time, message):
        self.append_calls = getattr(self, "append_calls", [])
        self.append_calls.append((mailbox, flags, message))
        return ("OK", [b"[APPENDUID 1 1]"])

    def logout(self):
        return ("BYE", [b"bye"])


@pytest.fixture()
def fake_imap(monkeypatch):
    _FakeIMAP.instances = []
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", _FakeIMAP)
    return _FakeIMAP


def test_apply_labels_passes_bytes_arg_no_raw_non_ascii(fake_imap, tmp_path, monkeypatch):
    """Regression guard for the LIVE UnicodeEncodeError: the STORE arg handed to M.uid
    must be BYTES and contain no raw non-ASCII, for an emoji add AND an \\Inbox remove."""
    monkeypatch.setattr(mail, "_journal_path", lambda: tmp_path / "j.jsonl")
    mail.apply_labels("42", add_labels=["🔴 Clients"], remove_labels=["\\Inbox"], creds=CREDS)
    inst = fake_imap.instances[0]
    assert len(inst.uid_calls) == 2, "add and remove are SEPARATE STORE commands"
    for cmd, uid, op, labelarg in inst.uid_calls:
        assert cmd == "STORE"
        assert op in ("+X-GM-LABELS", "-X-GM-LABELS")
        assert isinstance(labelarg, bytes), "STORE arg must be BYTES, not a str"
        assert all(b < 128 for b in labelarg), "STORE arg must contain NO raw non-ASCII"
    # the add carries the mutf7-encoded emoji label; the remove carries \Inbox verbatim.
    add = next(c for c in inst.uid_calls if c[2] == "+X-GM-LABELS")
    rm = next(c for c in inst.uid_calls if c[2] == "-X-GM-LABELS")
    assert add[3] == b'("&2D3dNA- Clients")'
    assert rm[3] == b'(\\Inbox)'


def test_apply_labels_important_emoji_label_encodes_and_applies(fake_imap, tmp_path, monkeypatch):
    """Regression guard for the 'Important'-category messages (live UIDs 3796/3797/3801):
    an emoji label like '⭐ Important' must now encode to pure-ASCII mutf7 BYTES and reach
    M.uid("STORE", …) — proving bug #1's fix covers that category too (not a distinct bug).
    """
    monkeypatch.setattr(mail, "_journal_path", lambda: tmp_path / "j.jsonl")
    mail.apply_labels("3796", add_labels=["⭐ Important"], creds=CREDS)
    inst = fake_imap.instances[0]
    assert len(inst.uid_calls) == 1
    cmd, uid, op, labelarg = inst.uid_calls[0]
    assert (cmd, uid, op) == ("STORE", "3796", "+X-GM-LABELS")
    assert isinstance(labelarg, bytes) and all(b < 128 for b in labelarg)
    assert labelarg == b'("&K1A- Important")'  # ⭐ U+2B50 → mutf7 &K1A-
    assert _mutf7_decode(labelarg[2:-2]) == "⭐ Important"  # strip ("  ")


# ---------------------------------------------------------------------------
# _find_drafts_mailbox — discover Drafts by the \Drafts special-use flag (bug #2)
# ---------------------------------------------------------------------------
class _ListOnlyIMAP:
    """Minimal IMAP double whose .list() returns a caller-supplied response."""
    def __init__(self, list_response):
        self._resp = list_response

    def list(self, directory='""', pattern="*"):
        return self._resp


_FRENCH_LIST = ("OK", [b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Brouillons"'])
_ENGLISH_LIST = ("OK", [b'(\\Drafts \\HasNoChildren) "/" "[Gmail]/Drafts"'])
_NO_DRAFTS_LIST = ("OK", [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\Sent \\HasNoChildren) "/" "[Gmail]/Messages envoy&AOk-s"',
])


def test_find_drafts_mailbox_french():
    """French account: \\Drafts flag on '[Gmail]/Brouillons' → discover that NAME."""
    assert mail._find_drafts_mailbox(_ListOnlyIMAP(_FRENCH_LIST)) == "[Gmail]/Brouillons"


def test_find_drafts_mailbox_english():
    assert mail._find_drafts_mailbox(_ListOnlyIMAP(_ENGLISH_LIST)) == "[Gmail]/Drafts"


def test_find_drafts_mailbox_falls_back_to_none_when_no_flag():
    """No folder carries the \\Drafts flag → None (create_draft then uses fallbacks)."""
    assert mail._find_drafts_mailbox(_ListOnlyIMAP(_NO_DRAFTS_LIST)) is None


def test_find_drafts_mailbox_flag_match_is_case_insensitive():
    resp = ("OK", [b'(\\drafts \\HasNoChildren) "/" "[Gmail]/Brouillons"'])
    assert mail._find_drafts_mailbox(_ListOnlyIMAP(resp)) == "[Gmail]/Brouillons"


def test_find_drafts_mailbox_returns_none_on_list_error():
    class _Boom:
        def list(self, *a, **k):
            return ("NO", [b"error"])
    assert mail._find_drafts_mailbox(_Boom()) is None


# ---------------------------------------------------------------------------
# create_draft — must APPEND to the DISCOVERED folder, not the hardcoded English one
# ---------------------------------------------------------------------------
class _DraftIMAP:
    """IMAP double for create_draft: records login/list/append, French Drafts by default."""
    def __init__(self, host, list_response=_FRENCH_LIST, fail_mailboxes=()):
        self.host = host
        self.list_response = list_response
        self.fail_mailboxes = set(fail_mailboxes)
        self.append_calls = []

    def login(self, user, password):
        return ("OK", [b"ok"])

    def list(self, directory='""', pattern="*"):
        return self.list_response

    def append(self, mailbox, flags, date_time, message):
        self.append_calls.append((mailbox, flags, message))
        if mailbox in self.fail_mailboxes:
            return ("NO", [b"[TRYCREATE] Folder doesn't exist"])
        return ("OK", [b"[APPENDUID 1 1]"])

    def logout(self):
        return ("BYE", [b"bye"])


def test_create_draft_appends_to_discovered_french_folder(monkeypatch, tmp_path):
    """create_draft must APPEND to the discovered '[Gmail]/Brouillons', NOT the hardcoded
    '[Gmail]/Drafts' — the exact live TRYCREATE failure on a French Gmail."""
    made = {}
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL",
                        lambda host: made.setdefault("m", _DraftIMAP(host)))
    monkeypatch.setattr(mail, "_journal_path", lambda: tmp_path / "j.jsonl")
    mail.create_draft(b"From: a@b\r\nSubject: x\r\n\r\nbody\r\n", creds=CREDS)
    m = made["m"]
    assert len(m.append_calls) == 1, "must not retry once the discovered folder succeeds"
    mailbox, flags, _msg = m.append_calls[0]
    assert mailbox == "[Gmail]/Brouillons"
    assert mailbox != mail._GMAIL_DRAFTS_MAILBOX
    assert flags == "\\Draft"  # still draft-only


def test_create_draft_falls_back_to_english_when_no_flag(monkeypatch, tmp_path):
    """No \\Drafts flag discovered → fall back to the '[Gmail]/Drafts' constant."""
    made = {}
    monkeypatch.setattr(
        mail.imaplib, "IMAP4_SSL",
        lambda host: made.setdefault("m", _DraftIMAP(host, list_response=_NO_DRAFTS_LIST)))
    monkeypatch.setattr(mail, "_journal_path", lambda: tmp_path / "j.jsonl")
    mail.create_draft(b"From: a@b\r\nSubject: x\r\n\r\nbody\r\n", creds=CREDS)
    m = made["m"]
    assert m.append_calls[0][0] == mail._GMAIL_DRAFTS_MAILBOX  # "[Gmail]/Drafts"


def test_create_draft_raises_when_all_folders_fail(monkeypatch, tmp_path):
    """If EVERY candidate APPEND fails, raise MailConfigError — never silently succeed."""
    made = {}
    monkeypatch.setattr(
        mail.imaplib, "IMAP4_SSL",
        lambda host: made.setdefault("m", _DraftIMAP(
            host, list_response=_NO_DRAFTS_LIST,
            fail_mailboxes=("[Gmail]/Drafts", "Drafts"))))
    monkeypatch.setattr(mail, "_journal_path", lambda: tmp_path / "j.jsonl")
    with pytest.raises(mail.MailConfigError):
        mail.create_draft(b"From: a@b\r\nSubject: x\r\n\r\nbody\r\n", creds=CREDS)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
