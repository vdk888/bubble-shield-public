#!/usr/bin/env python3
"""test_mail_read_uid.py — fetch_mail must EMIT a stable UID, surfaced by mail_read,
in the SAME UID space bubble_shield_mail_apply's ``M.uid("STORE", uid, …)`` mutates.

THE BUG THIS LOCKS DOWN
-----------------------
bubble_shield_mail_apply targets each message by UID (its schema requires a `uid`
"from bubble_shield_mail_read"). But mail_read (``_anonymise_mail``) used to return
only From/Subject/body — NO UID. An agent literally could not call apply correctly:
it had no UID to pass, and inventing one would label/archive the WRONG message. The
apply path was unreachable.

THE FIX (asserted here)
-----------------------
  1. fetch_mail returns (uid, from, subject, body) and uses UID SEARCH + UID FETCH —
     NOT bare SEARCH/FETCH (sequence numbers SHIFT and would target the wrong message
     when later fed to a UID STORE). We assert both the emitted UID and the command.
  2. ``_anonymise_mail`` PREPENDS a ``UID: <uid>`` line to each block. The From/
     Subject/body are still anonymised (tokens present); the UID line is NOT tokenised
     (a mailbox-local integer is not PII).
  3. Round-trip contract: the UID string mail_read emits is the plain decoded string
     apply's ``M.uid("STORE", uid, …)`` expects — the same value apply_labels sends.

All IMAP is MOCKED and the anonymiser is a deterministic test-double — no real Gmail,
no live NER daemon (so this test never depends on the vendor engine / the flaky
test_334 live-daemon path).
"""
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bubble_shield_mail as mail  # noqa: E402

CREDS = {"host": "imap.example.test", "user": "u@example.test",
         "password": "app-pw", "mailbox": "INBOX"}

# Synthetic mailbox: UID -> raw RFC822 bytes. UIDs are DELIBERATELY not 1..N and not
# contiguous, so a test that accidentally used sequence numbers (1,2,3) would fail.
_RAW = {
    b"4242": (
        b"From: Jean DUPONT <j.dupont@wanadoo.fr>\r\n"
        b"Subject: Souscription PER\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Bonjour Monsieur Jean DUPONT, IBAN FR76 3000 6000 0112 3456 7890 189.\r\n"
    ),
    b"9001": (
        b"From: Marie MARTIN <m.martin@orange.fr>\r\n"
        b"Subject: RDV succession\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Merci Marie MARTIN, a bientot.\r\n"
    ),
}


class FakeIMAP:
    """Records uid() commands and answers UID SEARCH/FETCH from the _RAW map."""
    instances = []

    def __init__(self, host):
        self.host = host
        self.uid_calls = []      # list of (command, *args)
        self.plain_search = 0    # bare M.search calls (must stay 0)
        self.plain_fetch = 0     # bare M.fetch calls (must stay 0)
        self.selected = None
        self.readonly = None
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        return ("OK", [b"ok"])

    def select(self, mailbox="INBOX", readonly=True):
        self.selected = mailbox
        self.readonly = readonly
        return ("OK", [b"2"])

    def uid(self, command, *args):
        self.uid_calls.append((command, *args))
        if command == "SEARCH":
            # args = (None, *criteria) — return the two known UIDs, space-separated.
            return ("OK", [b" ".join(_RAW.keys())])
        if command == "FETCH":
            u = args[0]
            u = u if isinstance(u, bytes) else str(u).encode()
            raw = _RAW.get(u)
            if raw is None:
                return ("NO", [None])
            # imaplib FETCH shape: [(b"1 (RFC822 {N}", raw_bytes), b")"]
            return ("OK", [(b"x (RFC822 {})", raw), b")"])
        return ("OK", [b"ok"])

    # bare (sequence-number) search/fetch MUST NOT be used — flag if they are.
    def search(self, *a, **k):
        self.plain_search += 1
        return ("OK", [b"1 2"])

    def fetch(self, *a, **k):
        self.plain_fetch += 1
        return ("OK", [(b"1 (RFC822 {N}", next(iter(_RAW.values()))), b")"])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"bye"])


@pytest.fixture()
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


# ---------------------------------------------------------------------------
# fetch_mail: emits the exact UIDs, paired with the right message, UID-based.
# ---------------------------------------------------------------------------
def test_fetch_mail_returns_uid_paired_with_message(fake_imap):
    out = mail.fetch_mail(query="ALL", maxn=10, creds=CREDS)
    # 4-tuple shape (uid, from, subject, body)
    assert all(len(rec) == 4 for rec in out)
    by_uid = {uid: (frm, subj, body) for (uid, frm, subj, body) in out}
    assert set(by_uid) == {"4242", "9001"}, "must emit the EXACT UIDs from UID SEARCH"
    # UID paired with the RIGHT message (not off-by-one / not swapped)
    assert "DUPONT" in by_uid["4242"][0] and "Souscription PER" == by_uid["4242"][1]
    assert "IBAN FR76" in by_uid["4242"][2]
    assert "MARTIN" in by_uid["9001"][0] and by_uid["9001"][1] == "RDV succession"
    # UID is a plain str (decoded from bytes), never bytes
    assert all(isinstance(uid, str) for (uid, *_rest) in out)


def test_fetch_mail_uses_uid_search_and_fetch_not_sequence_numbers(fake_imap):
    """Regression guard: UID SEARCH + UID FETCH only — NEVER bare search/fetch."""
    mail.fetch_mail(query="UNSEEN", maxn=10, creds=CREDS)
    inst = fake_imap.instances[0]
    cmds = [c[0] for c in inst.uid_calls]
    assert "SEARCH" in cmds, "must use M.uid('SEARCH', …)"
    assert "FETCH" in cmds, "must use M.uid('FETCH', …)"
    # bare sequence-number search/fetch would be the wrong-message bug
    assert inst.plain_search == 0, "bare M.search() used — sequence numbers are unstable!"
    assert inst.plain_fetch == 0, "bare M.fetch() used — sequence numbers are unstable!"
    # read stays read-only
    assert inst.readonly is True


def test_fetch_mail_uid_matches_apply_store_uid(fake_imap):
    """Round-trip: the UID read emits is the SAME plain string apply's UID STORE sends.

    We drive the REAL apply_labels against the same FakeIMAP and assert the STORE uid
    equals, byte-for-byte, the string fetch_mail produced — proving one UID space."""
    read_uid = mail.fetch_mail(query="ALL", maxn=1, creds=CREDS)[0][0]  # most-recent
    assert isinstance(read_uid, str)

    FakeIMAP.instances = []  # fresh instance for the apply call
    mail.apply_labels(read_uid, add_labels=["🔴 Clients"], creds=CREDS)
    store = fake_imap.instances[0].uid_calls[0]  # ("STORE", uid, op, labelarg)
    assert store[0] == "STORE"
    assert store[1] == read_uid, "apply's STORE uid must be the exact string read emitted"


# ---------------------------------------------------------------------------
# _anonymise_mail: prepends a UID line; body anonymised; UID NOT tokenised.
# ---------------------------------------------------------------------------
def test_anonymise_mail_prepends_uid_and_keeps_body_anonymised(monkeypatch):
    """mail_read output has a 'UID: <uid>' line per message; From/Subject/body are
    still routed through the anonymiser (tokens present); the UID is NOT tokenised."""
    import bubble_shield_mcp as mcp

    # Deterministic anonymiser double: turn any all-caps surname into a ⟦NOM_…⟧ token.
    # Proves the From/Subject/body pass through the anonymiser WITHOUT needing the
    # live NER daemon (and without ever tokenising the UID line, which we prepend
    # AFTER anonymising).
    def fake_anon(text, filename_basename=""):
        return (text.replace("DUPONT", "⟦NOM_0001⟧")
                    .replace("MARTIN", "⟦NOM_0002⟧"))

    monkeypatch.setattr(mcp, "_anonymise_text", fake_anon)
    monkeypatch.setattr(mcp, "load_credentials", lambda: CREDS, raising=False)
    # fetch_mail returns (uid, from, subject, body) 4-tuples
    monkeypatch.setattr(
        mail, "fetch_mail",
        lambda **kw: [
            ("4242", "Jean DUPONT <j.dupont@wanadoo.fr>", "Souscription PER",
             "Bonjour Monsieur Jean DUPONT."),
            ("9001", "Marie MARTIN <m.martin@orange.fr>", "RDV succession",
             "Merci Marie MARTIN."),
        ],
    )
    # _anonymise_mail imports fetch_mail/load_credentials from bubble_shield_mail at
    # call time; patch that module too so both point at our doubles.
    monkeypatch.setattr(mail, "load_credentials", lambda: CREDS)

    out = mcp._anonymise_mail(query="ALL", maxn=10)

    # one UID line per message, with the REAL uid
    assert "UID: 4242" in out
    assert "UID: 9001" in out
    # body/From still anonymised — the surname is a token, not raw
    assert "⟦NOM_0001⟧" in out and "⟦NOM_0002⟧" in out
    assert "DUPONT" not in out and "MARTIN" not in out
    # the UID itself must NOT be tokenised (a bare integer is not PII)
    assert "⟦" not in "4242"
    assert "UID: ⟦" not in out, "the UID line must never be tokenised"

    # structural: the UID line comes BEFORE its anonymised block (agent reads uid first)
    assert out.index("UID: 4242") < out.index("⟦NOM_0001⟧")


def test_anonymise_mail_uid_line_form_matches_apply_uid(monkeypatch):
    """The UID string surfaced in the 'UID: <n>' line is the plain form apply expects
    (no quoting, no brackets) — an agent copies it verbatim into a decision's uid."""
    import re
    import bubble_shield_mcp as mcp

    monkeypatch.setattr(mcp, "_anonymise_text", lambda t, filename_basename="": t)
    monkeypatch.setattr(
        mail, "fetch_mail",
        lambda **kw: [("777", "a@b", "s", "body")],
    )
    monkeypatch.setattr(mail, "load_credentials", lambda: CREDS)

    out = mcp._anonymise_mail(query="ALL", maxn=1)
    m = re.search(r"UID: (\S+)", out)
    assert m and m.group(1) == "777"
    # apply_labels(str_uid) round-trips this exact form (validated in the mail tests).


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
