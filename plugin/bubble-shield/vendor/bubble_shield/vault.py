"""
vault.py — the bidirectional mapping between real PII and opaque tokens.

One vault per (client / mission). It maps each distinct real value to a
stable, opaque token and back. The vault is the ONLY place the real values
live once a document is anonymised — so it must never leave the machine and
never be included in what is sent to an LLM.

Token design (Joris's warning on the Notion page: a deterministic Presidio
placeholder like <PERSON_1> can break de-anonymisation if the LLM reformats
it). We use an opaque, alphanumeric, bracketed sentinel:

    ⟦NOM_0001⟧   ⟦IBAN_0003⟧   ⟦MONTANT_0007⟧

- The ⟦ ⟧ math brackets are extremely unlikely to appear in real text and
  survive copy/paste, so restoration is an exact, unambiguous replace.
- The TYPE prefix keeps anonymised text readable for the human + the LLM
  ("le client ⟦NOM_0001⟧ détient ⟦MONTANT_0002⟧").
- The zero-padded counter is unique per distinct value; the SAME real value
  always gets the SAME token within a vault (consistency across a document
  and across requests in the same mission).
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Opaque sentinel. Group 1 = type, group 2 = counter (+ optional variant letter).
# The optional [a-z] suffix lets name VARIANTS of one person share a number
# ("Marie Dubois" → NOM_0001, "Dubois" → NOM_0001a) for a consistent, readable
# anonymisation, while each token still restores to its own surface form.
TOKEN_RE = re.compile(r"⟦([A-Z_]+)_(\d{4,}[a-z]?)⟧")


def make_token(entity_type: str, counter: int, variant: str = "") -> str:
    return f"⟦{entity_type}_{counter:04d}{variant}⟧"


@dataclass
class Vault:
    """A reversible PII ↔ token store for one client/mission.

    `mission` names the vault; `to_token` maps real→token, `to_value` maps
    token→real. Counters are per entity type so tokens read naturally.
    """
    mission: str = "default"
    to_token: Dict[str, str] = field(default_factory=dict)   # real value → token
    to_value: Dict[str, str] = field(default_factory=dict)   # token → real value
    _counters: Dict[str, int] = field(default_factory=dict)  # entity type → last n
    # Per-person variant tracking for NOM dedup: person-number → next variant
    # letter, and a map of normalised name-token → the person-number it belongs to.
    _person_of_token: Dict[str, int] = field(default_factory=dict)  # name-word → person n
    _variant_seq: Dict[int, int] = field(default_factory=dict)      # person n → next letter idx
    # RGPD art. 5-1-e (storage limitation): record when the vault was created so
    # we can auto-expire/purge stale vaults. UTC ISO string for unambiguous,
    # timezone-aware comparison regardless of where the file is read.
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def token_for(self, value: str, entity_type: str) -> str:
        """Return the stable token for a real value, minting one if new.

        Same value → same token (idempotent). For NOM, a VARIANT of an
        already-known person (a name that shares a distinctive word, e.g.
        "Dubois" after "Marie Dubois") reuses that person's NUMBER with a variant
        letter — consistent identity for the reader, exact restoration per token.
        Other types get sequential counters as before.
        """
        if value in self.to_token and self.to_token[value][1:].startswith(entity_type):
            return self.to_token[value]
        if entity_type == "NOM":
            return self._token_for_name(value)
        n = self._counters.get(entity_type, 0) + 1
        self._counters[entity_type] = n
        token = make_token(entity_type, n)
        self.to_token[value] = token
        self.to_value[token] = value
        return token

    # ── name-variant de-duplication (PII-Shield idea) ──────────────────────
    @staticmethod
    def _name_words(value: str):
        """Distinctive name words (≥3 chars, drop titles/particles) for matching."""
        import re as _re
        stop = {"m", "mr", "mme", "mlle", "monsieur", "madame", "mademoiselle",
                "dr", "me", "de", "la", "le", "du", "des", "et", "von", "van"}
        return [w for w in _re.split(r"[\s\-]+", value.lower())
                if len(w) >= 3 and w not in stop]

    def _token_for_name(self, value: str) -> str:
        words = self._name_words(value)
        # Does any distinctive word already belong to a known person?
        person = None
        for w in words:
            if w in self._person_of_token:
                person = self._person_of_token[w]
                break
        if person is None:
            # New person → new NOM number.
            person = self._counters.get("NOM", 0) + 1
            self._counters["NOM"] = person
            variant = ""
        else:
            # Known person → reuse the number with the next variant letter.
            idx = self._variant_seq.get(person, 0)
            self._variant_seq[person] = idx + 1
            variant = chr(ord("a") + idx) if idx >= 0 else ""
            # first reuse gets 'a', etc. (the canonical full form keeps no suffix)
        # Register this value's words against the person.
        for w in words:
            self._person_of_token.setdefault(w, person)
        token = make_token("NOM", person, variant)
        self.to_token[value] = token
        self.to_value[token] = value
        return token

    def value_for(self, token: str) -> Optional[str]:
        return self.to_value.get(token)

    def restore(self, text: str) -> str:
        """Replace every known token in `text` with its real value.

        Unknown tokens (not in this vault) are left untouched — restoring a
        foreign mission's token would be a leak/bug, so we never guess.
        """
        def _sub(m: re.Match) -> str:
            tok = m.group(0)
            return self.to_value.get(tok, tok)
        return TOKEN_RE.sub(_sub, text)

    @property
    def size(self) -> int:
        return len(self.to_token)

    # ---- RGPD erasure / rectification (art. 17 & 16) ---------------------
    # The vault is the single source of truth for a person's identity, so the
    # data-subject's rights to erasure and rectification are exercised HERE.
    # We always mutate BOTH directions of the mapping to avoid a dangling
    # half-entry that could resurface real PII via restore().

    def forget(self, value: str) -> bool:
        """Erase one real value and its token entirely (art. 17).

        Removes both directions of the mapping. Returns True if the value was
        present. The token's number is NOT reused — minting stays monotonic so
        already-anonymised documents can't accidentally collide with a future
        person, but a forgotten token simply restores to itself (no leak).
        """
        token = self.to_token.pop(value, None)
        if token is None:
            return False
        self.to_value.pop(token, None)
        return True

    def forget_subject(self, name_substring: str) -> int:
        """Erase EVERY entry whose real value contains `name_substring`.

        Case-insensitive substring match — this is the "right to be forgotten"
        for a whole data subject (e.g. forget_subject("Dupont") sweeps the full
        name, the abbreviated variant and the email in one call). Returns the
        number of entries removed.
        """
        needle = name_substring.lower()
        victims = [val for val in self.to_token if needle in val.lower()]
        for val in victims:
            self.forget(val)
        return len(victims)

    def rectify(self, old_value: str, new_value: str) -> bool:
        """Correct the real value behind a token, keeping the token (art. 16).

        The token already lives in anonymised documents, so rectification must
        preserve it — we only swap the cleartext it restores to. Returns True
        if `old_value` existed.
        """
        token = self.to_token.pop(old_value, None)
        if token is None:
            return False
        self.to_token[new_value] = token
        self.to_value[token] = new_value
        return True

    # ---- RGPD storage limitation (art. 5-1-e) ---------------------------

    def is_expired(self, ttl_days: float) -> bool:
        """True if the vault is older than `ttl_days` (storage limitation).

        Compares now (UTC) against `created_at`. Tolerant of a missing/garbled
        timestamp: an unparseable date is treated as NOT expired so we never
        delete a vault we can't reason about.
        """
        try:
            born = datetime.fromisoformat(self.created_at)
        except (TypeError, ValueError):
            return False
        if born.tzinfo is None:
            born = born.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - born).total_seconds() / 86400.0
        return age_days > ttl_days

    # ---- persistence (local file; treat as sensitive — chmod 600) --------

    def to_dict(self) -> dict:
        return {
            "mission": self.mission,
            "to_token": self.to_token,
            "to_value": self.to_value,
            "_counters": self._counters,
            # name-variant dedup state (so a reloaded vault keeps person numbers)
            "_person_of_token": self._person_of_token,
            "_variant_seq": {str(k): v for k, v in self._variant_seq.items()},
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Vault":
        v = cls(mission=d.get("mission", "default"))
        v.to_token = dict(d.get("to_token", {}))
        v.to_value = dict(d.get("to_value", {}))
        v._counters = {k: int(n) for k, n in d.get("_counters", {}).items()}
        v._person_of_token = {k: int(n) for k, n in d.get("_person_of_token", {}).items()}
        v._variant_seq = {int(k): int(n) for k, n in d.get("_variant_seq", {}).items()}
        # Keep the original creation time if the saved file has one; older vaults
        # without the field fall back to the constructor's "now" (treated as fresh).
        if d.get("created_at"):
            v.created_at = d["created_at"]
        return v

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write (#345): write to a temp file then os.replace() into place,
        # so a crash mid-write can never truncate the existing vault — the one
        # store holding the reversible token<->cleartext-PII map. Mirrors
        # known_pii_store._save_raw.
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
        try:
            p.chmod(0o600)  # vault holds the real PII — keep it private
        except OSError:
            pass
        # RGPD art. 32 — this is a PLAINTEXT-at-rest write. The vault concentrates
        # all the mission's real PII, so a cleartext file is the single
        # highest-value target on the machine. chmod 600 is NOT encryption. Warn
        # loudly (stderr only — never touches the JSON stdout that hooks parse) so
        # the gap can't stay silent, and point at the one-command remediation.
        # Suppressible via BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN=1 once acknowledged.
        _warn_plaintext_at_rest(p)

    @classmethod
    def load(cls, path: str | Path) -> "Vault":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- RGPD encryption at rest (art. 32) ------------------------------
    # The plain save() above writes cleartext PII to disk (chmod 600 only).
    # For a vault that leaves the local machine — backups, shared drives — that
    # is a high-value target. save_encrypted() derives a key from a passphrase
    # and authenticated-encrypts the JSON, so the file contains NO plaintext PII
    # and tampering / wrong-passphrase fail loudly.
    #
    # PURE-STDLIB by design (no `cryptography`, no `pip install`): the plugin
    # ships self-contained and runs offline on any Mac's built-in python3, where
    # compiled wheels can't be assumed. Construction (all standard, all stdlib):
    #   - KDF:    PBKDF2-HMAC-SHA256, 200k iterations, random 16-byte salt.
    #             (hashlib.scrypt needs an OpenSSL-built python; pbkdf2_hmac is
    #              always present.) Derives 64 bytes → 32B enc key + 32B mac key.
    #   - Cipher: HMAC-SHA256 keystream in counter mode (CTR), XOR with plaintext.
    #   - Auth:   encrypt-then-MAC — HMAC-SHA256 over salt|nonce|ciphertext;
    #             verified with hmac.compare_digest before any decryption is
    #             returned, so a wrong passphrase or tampered file raises.
    #
    # On-disk envelope (one line of JSON; itself reveals nothing):
    #   {"bubble_shield_enc":2,"kdf":"pbkdf2-sha256","iter":200000,"salt":..,"nonce":..,
    #    "ct":<b64>,"mac":<b64>}
    # Legacy "bubble_shield_enc":1 (scrypt+Fernet) files still load IF `cryptography`
    # is present — kept only for backward compatibility with old vaults.
    _ENC_MAGIC = "bubble_shield_enc"
    _PBKDF2_ITER = 200_000      # ~70ms; fine for an interactive unlock

    @staticmethod
    def _derive_keys(passphrase: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
        """PBKDF2-HMAC-SHA256 → 64 bytes, split into (enc_key, mac_key)."""
        import hashlib
        dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=64)
        return dk[:32], dk[32:]

    @staticmethod
    def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
        """HMAC-SHA256 counter-mode keystream (deterministic, stdlib)."""
        import hmac as _hmac, hashlib
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = _hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def save_encrypted(self, path: str | Path, passphrase: str) -> None:
        """Encrypt the whole vault to disk (RGPD art. 32). Pure stdlib — no deps.

        Random per-file salt + nonce; PBKDF2 key derivation; HMAC-CTR cipher;
        encrypt-then-MAC. The file holds no plaintext PII. chmod 600 too.
        """
        import hmac as _hmac, hashlib
        salt = os.urandom(16)
        nonce = os.urandom(16)
        enc_key, mac_key = self._derive_keys(passphrase, salt, self._PBKDF2_ITER)
        plaintext = json.dumps(self.to_dict(), ensure_ascii=False).encode("utf-8")
        ct = bytes(a ^ b for a, b in zip(plaintext, self._keystream(enc_key, nonce, len(plaintext))))
        mac = _hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
        envelope = {
            self._ENC_MAGIC: 2,
            "kdf": "pbkdf2-sha256",
            "iter": self._PBKDF2_ITER,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ct": base64.b64encode(ct).decode("ascii"),
            "mac": base64.b64encode(mac).decode("ascii"),
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(envelope), encoding="utf-8")
        try:
            p.chmod(0o600)  # still sensitive — the passphrase is the only other gate
        except OSError:
            pass

    @classmethod
    def load_encrypted(cls, path: str | Path, passphrase: str) -> "Vault":
        """Decrypt and rebuild a vault saved with save_encrypted().

        Verifies the MAC first (constant-time); a wrong passphrase or tampered
        file raises ValueError rather than returning a half-decrypted / garbage
        vault, so the caller can never act on corrupt PII. Also reads legacy v1
        (scrypt+Fernet) files when `cryptography` is available.
        """
        import hmac as _hmac, hashlib
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        version = envelope.get(cls._ENC_MAGIC)

        if version == 2:  # pure-stdlib format
            salt = base64.b64decode(envelope["salt"])
            nonce = base64.b64decode(envelope["nonce"])
            ct = base64.b64decode(envelope["ct"])
            mac = base64.b64decode(envelope["mac"])
            iterations = int(envelope.get("iter", cls._PBKDF2_ITER))
            enc_key, mac_key = cls._derive_keys(passphrase, salt, iterations)
            expected = _hmac.new(mac_key, salt + nonce + ct, hashlib.sha256).digest()
            if not _hmac.compare_digest(mac, expected):
                raise ValueError(
                    "Bubble Shield: mauvaise phrase secrète ou fichier coffre altéré "
                    "(échec de vérification d'intégrité)."
                )
            plaintext = bytes(a ^ b for a, b in zip(ct, cls._keystream(enc_key, nonce, len(ct))))
            return cls.from_dict(json.loads(plaintext.decode("utf-8")))

        if version == 1:  # legacy scrypt + Fernet (back-compat only)
            try:
                from cryptography.fernet import Fernet
                from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
            except ImportError as e:
                raise ImportError(
                    "Ce coffre est au format chiffré hérité (v1) ; pour le lire, "
                    "installez 'cryptography' (pip install cryptography), ou "
                    "régénérez-le — les nouveaux coffres n'en ont pas besoin."
                ) from e
            salt = base64.b64decode(envelope["salt"])
            kdf = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1)
            key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
            plaintext = Fernet(key).decrypt(envelope["token"].encode("ascii"))
            return cls.from_dict(json.loads(plaintext.decode("utf-8")))

        raise ValueError("Format de coffre chiffré non reconnu.")


# ---- RGPD storage limitation: directory-level purge (art. 5-1-e) --------

# The minimal set of keys we require before we are willing to treat a *.json
# file as one of our vaults and (potentially) delete it. Guards against nuking
# an unrelated config/notes file that merely happens to live in the directory.
_VAULT_REQUIRED_KEYS = {"mission", "to_token", "to_value"}


def purge_expired(directory: str | Path, ttl_days: float) -> List[Path]:
    """Delete expired vault files in `directory`, returning the purged paths.

    Scans every *.json file, but ONLY deletes ones that (a) parse as JSON,
    (b) look like a vault (have the required keys) and (c) are expired per
    `is_expired(ttl_days)`. Anything else — non-JSON, unrelated config, a
    fresh vault — is left strictly untouched. This conservative filter is the
    whole safety contract: purge must never destroy data it doesn't own.
    """
    purged: List[Path] = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict) or not _VAULT_REQUIRED_KEYS.issubset(d):
            continue
        if Vault.from_dict(d).is_expired(ttl_days):
            p.unlink()
            purged.append(p)
    return purged


# ---- RGPD encryption at rest: plaintext detection + one-command remediation --
# (art. 32) — the encryption primitives (save_encrypted / load_encrypted) have
# existed for a while but were OPT-IN, so the default save() wrote cleartext PII.
# Closing that gap fully (encrypt-by-default with machine-local key management
# threaded through the webapp + MCP server + PostToolUse hook) is a larger change
# with real corruption risk; until then, the MINIMUM viable art. 32 posture is:
# (1) never let a plaintext write stay silent, (2) ship a one-command migration.

def is_encrypted_vault_file(path: str | Path) -> bool:
    """True if `path` is a Bubble Shield ENCRYPTED vault envelope (v1 or v2).

    Cheap + safe: parses the JSON and checks for the magic key. A plaintext
    vault, a non-vault JSON, or an unreadable file all return False.
    """
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    return isinstance(d, dict) and Vault._ENC_MAGIC in d


def _warn_plaintext_at_rest(path: Path) -> None:
    """Emit a loud, one-line-ish stderr warning that a vault sits in plaintext.

    stderr ONLY — hooks/MCP servers parse stdout JSON, so this never corrupts a
    tool response. Suppress with BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN=1 once the
    operator has acknowledged the trade-off (or migrated to encrypted-at-rest).
    """
    if os.environ.get("BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN") == "1":
        return
    import sys
    try:
        sys.stderr.write(
            f"⚠️  Bubble Shield — coffre EN CLAIR sur le disque : {path}\n"
            "    Ce fichier concentre toute la PII de la mission (RGPD art. 32).\n"
            "    Chiffrez-le :  python3 -m bubble_shield.vault encrypt "
            f"'{path.parent}'\n"
            "    (silence : BUBBLE_SHIELD_SILENCE_PLAINTEXT_WARN=1)\n"
        )
    except Exception:
        pass


def find_plaintext_vaults(directory: str | Path) -> List[Path]:
    """Return the plaintext (unencrypted) vault files under `directory`.

    A file counts as a plaintext vault if it parses as JSON, carries the vault
    required-keys, and is NOT an encrypted envelope. Encrypted vaults, non-vault
    JSON and unrelated files are skipped — same conservative filter as purge.
    """
    out: List[Path] = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, dict):
            continue
        if Vault._ENC_MAGIC in d:
            continue  # already encrypted
        if not _VAULT_REQUIRED_KEYS.issubset(d):
            continue  # not one of our vaults
        out.append(p)
    return out


def encrypt_vault_file(path: str | Path, passphrase: str) -> bool:
    """Encrypt ONE plaintext vault file in place (art. 32). Returns True if it
    was plaintext and got encrypted; False if it was already encrypted / not a
    vault. Round-trips through Vault so the on-disk map is preserved exactly.

    Safe by construction: we load → re-save encrypted to a temp file → verify the
    temp file decrypts back to an identical mapping → only THEN os.replace() over
    the original. A failure at any step leaves the original untouched.
    """
    p = Path(path)
    if is_encrypted_vault_file(p):
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not (isinstance(d, dict) and _VAULT_REQUIRED_KEYS.issubset(d)):
        return False
    v = Vault.from_dict(d)
    tmp = p.with_suffix(p.suffix + ".enc.tmp")
    try:
        v.save_encrypted(tmp, passphrase)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    # verify round-trip before we overwrite the only copy of the PII map
    check = Vault.load_encrypted(tmp, passphrase)
    if check.to_value != v.to_value or check.to_token != v.to_token:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Vérification round-trip échouée pour {p} — original conservé.")
    os.replace(tmp, p)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return True


def encrypt_all_vaults(directory: str | Path, passphrase: str) -> List[Path]:
    """Encrypt every plaintext vault under `directory`. Returns the paths that
    were converted. Idempotent: already-encrypted vaults are skipped."""
    done: List[Path] = []
    for p in find_plaintext_vaults(directory):
        if encrypt_vault_file(p, passphrase):
            done.append(p)
    return done


def _cli(argv: Optional[List[str]] = None) -> int:
    """One-command remediation: `python3 -m bubble_shield.vault encrypt <dir>`.

    Reads the passphrase from $BUBBLE_SHIELD_VAULT_PASSPHRASE or prompts (never
    echoes it, never puts it in argv). `status <dir>` reports the plaintext/
    encrypted split without changing anything.
    """
    import argparse
    import getpass
    import sys

    ap = argparse.ArgumentParser(
        prog="bubble_shield.vault",
        description="Bubble Shield vault — encryption-at-rest maintenance (RGPD art. 32).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_enc = sub.add_parser("encrypt", help="encrypt every plaintext vault in a folder")
    ap_enc.add_argument("directory", help="folder holding *.json vault files")
    ap_st = sub.add_parser("status", help="report which vaults are plaintext vs encrypted")
    ap_st.add_argument("directory", help="folder holding *.json vault files")
    args = ap.parse_args(argv)

    directory = Path(args.directory)
    if not directory.is_dir():
        sys.stderr.write(f"Dossier introuvable : {directory}\n")
        return 2

    if args.cmd == "status":
        plain = find_plaintext_vaults(directory)
        allj = sorted(directory.glob("*.json"))
        enc = [p for p in allj if is_encrypted_vault_file(p)]
        print(f"{len(enc)} coffre(s) chiffré(s), {len(plain)} en clair, dans {directory}")
        for p in plain:
            print(f"  ⚠️  EN CLAIR : {p.name}")
        return 0

    # encrypt
    plain = find_plaintext_vaults(directory)
    if not plain:
        print(f"Aucun coffre en clair dans {directory} — rien à faire.")
        return 0
    passphrase = os.environ.get("BUBBLE_SHIELD_VAULT_PASSPHRASE")
    if not passphrase:
        passphrase = getpass.getpass("Phrase secrète pour chiffrer les coffres : ")
    if not passphrase:
        sys.stderr.write("Phrase secrète vide — abandon (rien n'a été modifié).\n")
        return 2
    done = encrypt_all_vaults(directory, passphrase)
    print(f"✅ {len(done)} coffre(s) chiffré(s) sur {len(plain)}.")
    for p in done:
        print(f"   🔒 {p.name}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
