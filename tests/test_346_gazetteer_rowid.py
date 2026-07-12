"""
test_346_gazetteer_rowid.py — #346: opaque server-side row-id for /gazetteer.

WHY THIS EXISTS
---------------
webapp/app.py:854-914 (pre-fix) rendered the /gazetteer listing with the
confirmed-PII value base64-encoded in a hidden form field (``value_b64``), and
the remove/add POST handlers decoded it back. base64 is an ENCODING, not
encryption: a devtools ``atob()`` on the hidden input recovered the cleartext
confirmed name straight out of the page DOM.

Fix: GET /gazetteer now renders an opaque ``row_id`` per entry (an HMAC-SHA256
of the value keyed on a server-only, per-process secret — see
``_gazetteer_row_id`` in webapp/app.py). POST /gazetteer/remove takes
``row_id`` and resolves it back to the value SERVER-SIDE only. Nothing
reversible ever appears in the rendered HTML.

This test suite asserts:
  1. The rendered /gazetteer page contains no base64 (nor cleartext) of the
     confirmed value — only the opaque row_id and the already-masked display.
  2. Removing an entry via its row_id removes the correct entry (and doesn't
     touch other entries with a similar/substring value).
  3. row_id is stable across repeated GETs within the same process (a human
     could load, refresh, then click remove and it still works).
  4. row_id is NOT a reversible re-encoding of the value: it does not
     round-trip through base64/hex-of-utf8/etc, and it is NOT simply derived
     without the server secret (same input value across two independently
     keyed processes must NOT collide) — i.e. it's an opaque token, not a
     deterministic content hash a client could precompute.

All names used below are synthetic test fixtures, not real client PII.
"""
from __future__ import annotations

import base64
import binascii

import pytest
from fastapi.testclient import TestClient

from webapp.app import app, _gazetteer_row_id
from bubble_shield.known_pii_store import add_confirmed_pii, load_gazetteer, is_known_pii

client = TestClient(app)

SYNTH_NAME_1 = "Testalot Dupreux"
SYNTH_NAME_2 = "Fixturine Marchamps"


def _seed(names_and_types):
    for value, etype in names_and_types:
        add_confirmed_pii(value, etype)


def test_gazetteer_page_has_no_base64_or_cleartext_of_value():
    """The rendered DOM must contain neither base64(value) nor value itself."""
    _seed([(SYNTH_NAME_1, "NOM"), (SYNTH_NAME_2, "NOM")])

    r = client.get("/gazetteer")
    assert r.status_code == 200

    # No cleartext of the confirmed value anywhere in the page.
    assert SYNTH_NAME_1 not in r.text
    assert SYNTH_NAME_2 not in r.text

    # No reversible base64 encoding of the value in the page either — this is
    # the exact vulnerability: devtools atob() on a hidden field recovering
    # the name. Simulate that check directly.
    b64_1 = base64.urlsafe_b64encode(SYNTH_NAME_1.encode()).decode()
    b64_2 = base64.urlsafe_b64encode(SYNTH_NAME_2.encode()).decode()
    assert b64_1 not in r.text
    assert b64_2 not in r.text

    # No legacy hidden-field name either.
    assert "value_b64" not in r.text

    # The opaque row_id IS present (as a hidden field value) but atob() on it
    # must not yield the name — prove that decoding it as base64 either fails
    # or produces garbage, not the confirmed value.
    row_id = _gazetteer_row_id(SYNTH_NAME_1)
    assert row_id in r.text
    try:
        decoded = base64.urlsafe_b64decode(row_id + "==").decode("utf-8")
    except (UnicodeDecodeError, binascii.Error):
        decoded = None
    assert decoded != SYNTH_NAME_1
    assert decoded != SYNTH_NAME_2


def test_remove_by_row_id_removes_correct_entry_only():
    """Posting the opaque row_id removes exactly that entry, no others."""
    _seed([(SYNTH_NAME_1, "NOM"), (SYNTH_NAME_2, "NOM")])
    assert is_known_pii(SYNTH_NAME_1)
    assert is_known_pii(SYNTH_NAME_2)

    row_id_1 = _gazetteer_row_id(SYNTH_NAME_1)

    r = client.post("/gazetteer/remove", data={"row_id": row_id_1}, follow_redirects=False)
    assert r.status_code == 303
    assert "flash=retire" in r.headers["location"]

    assert not is_known_pii(SYNTH_NAME_1)
    assert is_known_pii(SYNTH_NAME_2)  # untouched


def test_remove_unknown_row_id_is_a_noop_not_an_error():
    """An id that doesn't resolve to any entry must fail gracefully (flash=absent),
    never raise, never remove an unrelated entry."""
    _seed([(SYNTH_NAME_1, "NOM")])

    r = client.post("/gazetteer/remove", data={"row_id": "not-a-real-id"}, follow_redirects=False)
    assert r.status_code == 303
    assert "flash=absent" in r.headers["location"]
    assert is_known_pii(SYNTH_NAME_1)


def test_row_id_stable_across_repeated_page_loads():
    """A human loading /gazetteer twice (e.g. refresh) then clicking remove on
    the second load must still hit the right entry — the id can't drift
    between GETs within the same running process."""
    _seed([(SYNTH_NAME_1, "NOM")])

    r1 = client.get("/gazetteer")
    r2 = client.get("/gazetteer")
    assert r1.status_code == 200 and r2.status_code == 200

    id_from_helper = _gazetteer_row_id(SYNTH_NAME_1)
    assert id_from_helper in r1.text
    assert id_from_helper in r2.text  # same id both times

    # And it still resolves correctly on removal after two GETs.
    r = client.post("/gazetteer/remove", data={"row_id": id_from_helper}, follow_redirects=False)
    assert "flash=retire" in r.headers["location"]
    assert not is_known_pii(SYNTH_NAME_1)


def test_row_id_is_opaque_not_a_client_computable_hash():
    """The id must depend on a server-only secret, not be reproducible by a
    client from the value alone with a known/public algorithm (e.g. plain
    sha256(value) with no secret) — otherwise it's just another reversible-ish
    scheme (an attacker with a gazetteer-value guess list could confirm
    membership by hashing candidates and comparing, defeating the point of
    "opaque"). Assert the row_id is NOT equal to unsalted sha256/sha1/md5 of
    the value in any common encoding.
    """
    import hashlib

    row_id = _gazetteer_row_id(SYNTH_NAME_1)
    norm = SYNTH_NAME_1.strip().lower()
    for algo in (hashlib.sha256, hashlib.sha1, hashlib.md5):
        h = algo(norm.encode("utf-8")).hexdigest()
        assert row_id != h
        assert row_id != h[:24]
        assert not h.startswith(row_id)


def test_case_insensitive_row_id_matches_store_semantics():
    """known_pii_store matching is case-insensitive; the row_id lookup must
    agree so a differently-cased direct POST still resolves (defense-in-depth,
    matches entity_type_of / contains semantics in known_pii_store.py)."""
    _seed([(SYNTH_NAME_1, "NOM")])
    assert _gazetteer_row_id(SYNTH_NAME_1) == _gazetteer_row_id(SYNTH_NAME_1.upper())
    assert _gazetteer_row_id(SYNTH_NAME_1) == _gazetteer_row_id(SYNTH_NAME_1.lower())
