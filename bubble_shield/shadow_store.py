from __future__ import annotations
import base64, hashlib, hmac, json, os, sqlite3, time
from pathlib import Path
from typing import Optional

# Reuse the vault's EXACT crypto primitives — do NOT roll our own cipher / KDF /
# MAC. The shadow store holds clean (unmasked) PII text, so at rest it must be
# encrypted with the same PBKDF2 + HMAC-CTR encrypt-then-MAC construction the
# vault already uses for its own PII-at-rest (RGPD art. 32). See vault.py.
from bubble_shield.vault import Vault

# Env var carrying the machine-local passphrase for the shadow store at rest.
# If it is UNSET, the store stays plaintext (back-compat with pre-encryption
# callers / tests). If it is SET, the on-disk artifact is a vault-format
# encrypted envelope and no plaintext DB survives a write.
_PASSPHRASE_ENV = "BUBBLE_SHIELD_STORE_PASSPHRASE"


def _shield_home() -> Path:
    return Path(os.environ.get("BUBBLE_SHIELD_HOME", str(Path.home() / ".bubble_shield")))

def store_path() -> Path:
    """Plaintext SQLite working-copy path. When encryption is active this file
    is transient: it exists only while a connection is open, and is removed
    (sealed back into shield.db.enc) after every write."""
    return _shield_home() / "shield.db"

def enc_path() -> Path:
    """At-rest encrypted artifact (vault-format envelope of the SQLite bytes)."""
    return _shield_home() / "shield.db.enc"

def _passphrase() -> Optional[str]:
    pw = os.environ.get(_PASSPHRASE_ENV)
    return pw if pw else None

# One-time-per-process latch so plaintext-fallback warns loudly exactly once,
# not on every connect() (get_shadow/list_indexed/put_shadow all connect()).
_warned_plaintext_fallback = False

def _warn_plaintext_fallback() -> None:
    """Loud, one-time stderr warning when BUBBLE_SHIELD_STORE_PASSPHRASE is
    unset and the shadow store is therefore operating in PLAINTEXT-at-rest
    fallback mode. Mirrors vault._warn_plaintext_at_rest — stderr ONLY (never
    stdout, which hooks/MCP parse as JSON), non-fatal (the hard refuse-to-run
    guard for this is Task 9's daemon, out of scope here), suppressible once
    acknowledged via the same env var the vault uses.
    """
    global _warned_plaintext_fallback
    if _warned_plaintext_fallback:
        return
    _warned_plaintext_fallback = True
    if os.environ.get("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN") == "1":
        return
    import sys
    try:
        sys.stderr.write(
            f"⚠️  Bubble Shield — shadow store EN CLAIR sur le disque : {store_path()}\n"
            "    Cette table concentre les noms clients en clair de toute la "
            "mission (RGPD art. 32).\n"
            f"    Définissez {_PASSPHRASE_ENV} pour chiffrer ce magasin au repos.\n"
            "    (silence : BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN=1)\n"
        )
    except Exception:
        pass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadows (
  content_hash TEXT PRIMARY KEY,
  src_path     TEXT,
  clean_text   TEXT NOT NULL,
  size         INTEGER,
  mtime        REAL,
  indexed_at   REAL
);
CREATE TABLE IF NOT EXISTS gazetteer (
  value       TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  added_at    REAL,
  UNIQUE(value, entity_type)
);
CREATE TABLE IF NOT EXISTS pending (
  src_path   TEXT PRIMARY KEY,
  marked_at  REAL,
  fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shadow_values (
  content_hash TEXT NOT NULL,
  value_hash   TEXT NOT NULL,
  UNIQUE(content_hash, value_hash)
);
CREATE INDEX IF NOT EXISTS idx_shadow_values_vh ON shadow_values(value_hash);
"""

# #646 — after this many consecutive sweep failures on the SAME source, a doc is
# QUARANTINED (surfaced as needs-attention) instead of retried every sweep forever
# (which burns the single serial Gemma worker on a doc that can't complete — e.g. an
# un-extractable INPI/watermarked doc, or a giant form). Env-tunable.
import os as _os_q
QUARANTINE_AFTER_FAILS = int(_os_q.environ.get("BUBBLE_SHIELD_QUARANTINE_AFTER_FAILS", "5"))


def _ensure_fail_count_column(conn) -> None:
    """Backward-compat: an existing pending table (pre-#646) lacks fail_count. Add it
    additively (defaulted) so old DBs upgrade in place with no data migration. Idempotent."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending)")}
        if "fail_count" not in cols:
            conn.execute("ALTER TABLE pending ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # best-effort; a missing column just means quarantine never triggers (safe)


def _ensure_stale_column(conn) -> None:
    """Backward-compat (#554 retro re-index): an existing shadows table lacks the
    `stale` flag. Add it additively (default 0 = fresh) so old DBs upgrade in place.
    Idempotent."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(shadows)")}
        if "stale" not in cols:
            conn.execute("ALTER TABLE shadows ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # best-effort; missing column just means retro re-index never triggers (safe)

def _harden_permissions(p: Path) -> None:
    """Lock the store file to owner-only (0600) and its parent dir to 0700 —
    same discipline as the vault / known_pii_store.py: `shadows` holds clean
    (unmasked) PII text, so a world-readable DB file would be a real leak."""
    try:
        p.parent.chmod(0o700)
    except OSError:
        pass
    try:
        p.chmod(0o600)
    except OSError:
        pass

# ---- encryption at rest: reuse the vault's PBKDF2 + HMAC-CTR primitives -----
# We mirror vault.save_encrypted / load_encrypted EXACTLY (same envelope keys,
# same magic, same v2 format), but over the raw SQLite FILE BYTES instead of the
# vault's JSON. No new cipher, nonce scheme, KDF or MAC is introduced here.

def _encrypt_bytes(plaintext: bytes, passphrase: str) -> bytes:
    """Vault-format v2 encrypt-then-MAC of arbitrary bytes → envelope JSON bytes.

    Random per-write salt + nonce, PBKDF2-SHA256 key derivation (Vault._derive_keys),
    HMAC-SHA256 counter-mode keystream (Vault._keystream), HMAC over salt|nonce|ct.
    Identical construction to Vault.save_encrypted."""
    salt = os.urandom(16)
    nonce = os.urandom(16)
    enc_key, mac_key = Vault._derive_keys(passphrase, salt, Vault._PBKDF2_ITER)
    ks = Vault._keystream(enc_key, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    mac = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
    envelope = {
        Vault._ENC_MAGIC: 2,
        "kdf": "pbkdf2-sha256",
        "iter": Vault._PBKDF2_ITER,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }
    return json.dumps(envelope).encode("utf-8")

def _decrypt_bytes(envelope_bytes: bytes, passphrase: str) -> bytes:
    """Verify the MAC (constant-time) then decrypt a vault-format v2 envelope.

    A wrong passphrase or a tampered file RAISES ValueError (fail-loud) rather
    than returning garbage or empty bytes — an empty shadow store reads as 'no
    known names' downstream, which would re-leak PII. Mirrors Vault.load_encrypted."""
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    version = envelope.get(Vault._ENC_MAGIC)
    if version != 2:
        raise ValueError("Bubble Shield: format de magasin chiffré non reconnu.")
    salt = base64.b64decode(envelope["salt"])
    nonce = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    mac = base64.b64decode(envelope["mac"])
    iterations = int(envelope.get("iter", Vault._PBKDF2_ITER))
    enc_key, mac_key = Vault._derive_keys(passphrase, salt, iterations)
    expected = hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError(
            "Bubble Shield: mauvaise phrase secrète ou magasin altéré "
            "(échec de vérification d'intégrité)."
        )
    ks = Vault._keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))

def _decrypt_to_working_copy(passphrase: str) -> None:
    """Decrypt shield.db.enc → chmod-600 plaintext working copy shield.db."""
    p = store_path()
    envelope_bytes = enc_path().read_bytes()
    db_bytes = _decrypt_bytes(envelope_bytes, passphrase)  # raises on wrong pw / tamper
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(db_bytes)
    _harden_permissions(p)

def _drop_working_copy() -> None:
    """Best-effort removal of the plaintext SQLite working copy (shield.db).

    Shared cleanup used after reads (get_shadow, list_indexed) AND as the
    guaranteed fallback in _seal()'s finally / put_shadow's finally, so no
    unmasked PII working copy can strand on disk regardless of which path
    (success or exception) got us here. No-op in plaintext-store mode (no
    passphrase) — that mode's shield.db is the permanent store, not transient.
    """
    if _passphrase() is None:
        return
    try:
        store_path().unlink()
    except OSError:
        pass

def _seal() -> None:
    """Re-encrypt the plaintext working copy back into shield.db.enc and remove
    the plaintext. No-op when no passphrase is set (plaintext-store mode).

    The plaintext unlink runs in a `finally` so it happens even if encryption
    or the envelope write raises (Finding 2) — we cannot produce shield.db.enc
    in that case (fail loud, the exception propagates), but the plaintext
    temp must never be left stranded on disk either way.
    """
    passphrase = _passphrase()
    if passphrase is None:
        return
    p = store_path()
    if not p.exists():
        return
    try:
        db_bytes = p.read_bytes()
        envelope_bytes = _encrypt_bytes(db_bytes, passphrase)
        ep = enc_path()
        ep.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: temp then os.replace so a crash can't truncate the only
        # at-rest copy of the store.
        tmp = ep.with_suffix(ep.suffix + ".tmp")
        tmp.write_bytes(envelope_bytes)
        os.replace(tmp, ep)
        try:
            ep.parent.chmod(0o700)
        except OSError:
            pass
        try:
            ep.chmod(0o600)  # still sensitive — the passphrase is the only other gate
        except OSError:
            pass
    finally:
        # Remove the plaintext working copy so no unmasked PII survives at
        # rest, whether sealing succeeded or _encrypt_bytes/tmp.write_bytes
        # raised partway through.
        _drop_working_copy()

def connect() -> sqlite3.Connection:
    """Open a SQLite connection to the shadow store.

    Encryption mode (passphrase set): if shield.db.enc exists, decrypt it to a
    chmod-600 working copy shield.db first; otherwise start fresh. Callers that
    WRITE must call _seal() (put_shadow does) to re-encrypt and drop the plaintext.
    Plaintext mode (no passphrase): behaves exactly as before."""
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    passphrase = _passphrase()
    if passphrase is not None and enc_path().exists():
        # Decrypt-on-open. Raises loudly on wrong passphrase / tamper.
        _decrypt_to_working_copy(passphrase)
    elif passphrase is None:
        # No passphrase configured: this store operates in plaintext-at-rest
        # fallback mode (Finding 3). Warn loudly (once per process) so the
        # gap can't stay silent — mirrors vault._warn_plaintext_at_rest. This
        # is a WARN, not a refuse: the hard guard belongs to the Task 9
        # single-writer daemon, out of scope here.
        _warn_plaintext_fallback()
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    _ensure_fail_count_column(conn)
    _ensure_stale_column(conn)
    conn.commit()
    _harden_permissions(p)
    return conn


def value_hash(value: str) -> str:
    """#554 retro re-index — canonical hash of a masked value (strip+lower,
    matching safe_words' case-insensitive semantics). Only these hashes are
    stored in shadow_values — never the values themselves."""
    return hashlib.sha256(str(value).strip().lower().encode("utf-8")).hexdigest()


def put_shadow(content_hash: str, clean_text: str, *, src_path: str = "",
               size: int = 0, mtime: float = 0.0, value_hashes=None) -> None:
    conn = connect()
    try:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO shadows "
                "(content_hash, src_path, clean_text, size, mtime, indexed_at, stale) "
                "VALUES (?,?,?,?,?,?,0)",
                (content_hash, src_path, clean_text, size, mtime, time.time()))
            # #554 — refresh the masked-value hash map for this shadow (a re-index
            # replaces the old set; stale flag reset by the INSERT above).
            conn.execute("DELETE FROM shadow_values WHERE content_hash=?",
                         (content_hash,))
            for vh in (value_hashes or []):
                conn.execute(
                    "INSERT OR IGNORE INTO shadow_values (content_hash, value_hash) "
                    "VALUES (?,?)", (content_hash, vh))
            conn.commit()
        finally:
            conn.close()
    finally:
        # GUARANTEED path (runs even if execute/commit above raised): re-seal
        # the working copy back into shield.db.enc. _seal() itself drops the
        # plaintext working copy in its own finally (Finding 2), so a mid-write
        # exception here can never leave decrypted PII on disk (Finding 1).
        _seal()  # re-encrypt working copy → shield.db.enc, drop the plaintext

def get_shadow(content_hash: str) -> Optional[str]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT clean_text FROM shadows WHERE content_hash=?",
            (content_hash,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
        # A read decrypted a plaintext working copy; drop it so no unmasked PII
        # lingers at rest between operations.
        _drop_working_copy()

def content_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def list_indexed() -> set:
    """Hashes the sweep may SKIP. #554: stale shadows are excluded — the sweep
    re-indexes their files on its next pass. Reads (get_shadow) intentionally
    keep serving stale shadows meanwhile: over-masked-but-served is the safe
    direction (deleting would turn the next read into a raw-serving MISS)."""
    conn = connect()
    try:
        return {r[0] for r in conn.execute(
            "SELECT content_hash FROM shadows WHERE stale=0")}
    finally:
        conn.close()
        _drop_working_copy()


def mark_stale_by_value_hash(vh: str) -> int:
    """#554 retro re-index — flag every shadow whose masked-value set contains
    `vh` as stale (its junk word was just judged safe). Returns the number of
    shadows flagged. The shadow row is NEVER deleted (see list_indexed)."""
    conn = connect()
    try:
        try:
            cur = conn.execute(
                "UPDATE shadows SET stale=1 WHERE stale=0 AND content_hash IN "
                "(SELECT content_hash FROM shadow_values WHERE value_hash=?)",
                (vh,))
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    finally:
        _seal()


# Mask-token shape ⟦TYPE_id⟧ — mirrors vault.TOKEN_RE. Used to derive per-type
# entity counts from stored cloaked text for the dashboard stats.
import re as _re
_STATS_TOKEN_RE = _re.compile(r"⟦([A-Z_]+)_(\d{4,}[a-z]?)⟧")


def stats() -> dict:
    """Live dashboard stats derived from the SHADOW STORE — the current truth of
    what is indexed — rather than the append-only audit log (which only recorded
    the old interactive path and missed all background-sweep indexing, so the
    cards froze).

    Returns:
      {"indexed_files": int,              # rows in the shadow store
       "entity_totals": {TYPE: count},    # DISTINCT ⟦TYPE_id⟧ tokens summed across
                                          #   all files (same client repeated in one
                                          #   file = one token; counted per file so
                                          #   the total reflects masked entities)
       "total_entities": int}            # sum of entity_totals

    NO PII: reads only the cloaked_text's token TYPE + numeric id, never a value.
    Best-effort: any error yields zeros (the panel degrades, never crashes)."""
    from collections import Counter
    totals: "Counter[str]" = Counter()
    n_files = 0
    conn = connect()
    try:
        for (clean_text,) in conn.execute("SELECT clean_text FROM shadows"):
            n_files += 1
            # distinct (type,id) within THIS file, then add to the running totals
            per_file = {}
            for m in _STATS_TOKEN_RE.finditer(clean_text or ""):
                per_file.setdefault(m.group(1), set()).add(m.group(2))
            for etype, ids in per_file.items():
                totals[etype] += len(ids)
    except Exception:
        return {"indexed_files": 0, "entity_totals": {}, "total_entities": 0}
    finally:
        conn.close()
        _drop_working_copy()
    return {
        "indexed_files": n_files,
        "entity_totals": dict(totals.most_common()),
        "total_entities": sum(totals.values()),
    }

def mark_pending(src_path: str, *, failed: bool = False) -> None:
    """Queue a source file for the shadow-index sweep (Task 5's read-miss path
    calls this). A WRITE, so it mirrors put_shadow EXACTLY: connect → write →
    close, with the outer finally re-sealing the working copy back into
    shield.db.enc (Task 4 encryption at rest).

    #646: `failed=True` (a sweep tried to certify this doc and COULD NOT) INCREMENTS
    fail_count so a deterministically-uncertifiable doc (un-extractable INPI, giant
    form) is quarantined after QUARANTINE_AFTER_FAILS instead of retried forever. A
    plain miss-queue (`failed=False`, the read-miss path) does NOT increment — it's
    not a failure, just a not-yet-indexed file."""
    conn = connect()
    try:
        try:
            _ensure_fail_count_column(conn)
            if failed:
                # increment on a real certify-failure; preserve count across re-marks
                conn.execute(
                    "INSERT INTO pending (src_path, marked_at, fail_count) VALUES (?,?,1) "
                    "ON CONFLICT(src_path) DO UPDATE SET marked_at=excluded.marked_at, "
                    "fail_count=fail_count+1",
                    (src_path, time.time()))
            else:
                conn.execute(
                    "INSERT INTO pending (src_path, marked_at, fail_count) VALUES (?,?,0) "
                    "ON CONFLICT(src_path) DO UPDATE SET marked_at=excluded.marked_at",
                    (src_path, time.time()))
            conn.commit()
        finally:
            conn.close()
    finally:
        _seal()  # re-encrypt working copy → shield.db.enc, drop the plaintext

def pending_files() -> list:
    """Source paths queued for the sweep, EXCLUDING quarantined ones (#646) — a
    quarantined doc has failed QUARANTINE_AFTER_FAILS+ times and must NOT be retried
    (it burns the serial worker). A READ: drop the plaintext working copy afterwards."""
    conn = connect()
    try:
        _ensure_fail_count_column(conn)
        return [r[0] for r in conn.execute(
            "SELECT src_path FROM pending WHERE fail_count < ?",
            (QUARANTINE_AFTER_FAILS,))]
    finally:
        conn.close()
        _drop_working_copy()

def quarantined_files() -> list:
    """#646 — source paths that have failed to certify QUARANTINE_AFTER_FAILS+ times.
    These are surfaced to the operator (needs-attention) and NOT re-swept. A READ."""
    conn = connect()
    try:
        _ensure_fail_count_column(conn)
        return [r[0] for r in conn.execute(
            "SELECT src_path FROM pending WHERE fail_count >= ?",
            (QUARANTINE_AFTER_FAILS,))]
    finally:
        conn.close()
        _drop_working_copy()

def clear_pending(src_path: str) -> None:
    """Remove a source file from the sweep queue (Task 7's index_one calls this
    after indexing). A WRITE, so it mirrors put_shadow/mark_pending: the outer
    finally re-seals the encrypted store and drops the plaintext working copy."""
    conn = connect()
    try:
        try:
            conn.execute("DELETE FROM pending WHERE src_path=?", (src_path,))
            conn.commit()
        finally:
            conn.close()
    finally:
        _seal()  # re-encrypt working copy → shield.db.enc, drop the plaintext
