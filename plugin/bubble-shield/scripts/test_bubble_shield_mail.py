#!/usr/bin/env python3
"""Tests for bubble_shield_mail_read — the fail-CLOSED anonymised mail read.

Run: python3 test_bubble_shield_mail.py

The security-critical property under test: mail_read fetches e-mail ITSELF
(Bubble Shield owns the read) and routes every body through the SAME fail-closed
_anonymise_text core the file guard uses. So:

  - daemon DOWN → the tool REFUSES (raises NERDownError → isError), NEVER returns
    a raw e-mail body. This is the whole point — proven here with synthetic
    Jean DUPONT PII.
  - daemon UP   → the body comes back cloaked (⟦NOM…⟧ etc), and the client's name
    in the From-header shares the SAME token root as the same name in the body
    (cross-field consistency).

The IMAP mechanics (imaplib) are NOT hit here: fetch_mail is monkeypatched with a
synthetic message so the test needs no live mailbox and no real PII. The parse
layer (parse_message) and the cred store (load_credentials) are tested directly.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# vendor engine is needed by bubble_shield_mcp (_anonymise_text)
sys.path.insert(0, str((HERE.parent / "vendor")))

passed = failed = 0


# ---------------------------------------------------------------------------
# Mock NER daemon — flags bare all-caps two-word sequences as PERSON (mirrors
# the mock in test_bubble_shield_mcp.py), so 'JEAN DUPONT' is caught daemon-up.
# ---------------------------------------------------------------------------
class _MockNERHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/detect":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            text = body.get("text", "")
            matches = []
            for m in re.finditer(r"\b([A-Z]{2,}\s+[A-Z]{2,})\b", text):
                matches.append({"start": m.start(), "end": m.end(),
                                "entity_type": "PERSON", "value": m.group(), "score": 0.91})
            # also flag mixed-case 'Jean DUPONT' (name in From/body of the fixture)
            for m in re.finditer(r"\b([A-Z][a-z]+\s+[A-Z]{2,})\b", text):
                matches.append({"start": m.start(), "end": m.end(),
                                "entity_type": "PERSON", "value": m.group(), "score": 0.91})
            resp = json.dumps({"matches": matches}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404); self.end_headers()


def start_mock_daemon():
    srv = HTTPServer(("127.0.0.1", 0), _MockNERHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port, srv


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


# Synthetic email — the SCOPE's proven Jean DUPONT fixture. No real PII.
FAKE_MSG = (
    "Jean DUPONT <j.dupont@wanadoo.fr>",
    "Souscription PER",
    "Bonjour, je confirme l'ouverture du PER pour Monsieur Jean DUPONT, "
    "IBAN FR76 3000 6000 0112 3456 7890 189, ne le 14/07/1968 a Lyon. Cordialement.",
)


def _fresh_home():
    return str(Path(tempfile.mkdtemp()) / "home")


def _import_mcp_with_port(port, home):
    """Import a FRESH copy of bubble_shield_mcp with a given NERD port + HOME.

    posttool_anonymize reads NERD_PORT at import time, so we must set the env
    and drop any cached module before importing to bind the right port.
    """
    os.environ["BUBBLE_SHIELD_NERD_PORT"] = str(port)
    os.environ["BUBBLE_SHIELD_HOME"] = home
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(HERE.parent)
    os.environ["BUBBLE_SHIELD_SESSION"] = "mail-test"
    for mod in ("bubble_shield_mcp", "posttool_anonymize", "bubble_shield_mail"):
        sys.modules.pop(mod, None)
    import importlib
    mcp = importlib.import_module("bubble_shield_mcp")
    mail = importlib.import_module("bubble_shield_mail")
    return mcp, mail


# ---------------------------------------------------------------------------
# 1. parse_message — synthetic RFC822 bytes → (from, subject, body)
# ---------------------------------------------------------------------------
print("\n--- 1. parse_message (synthetic RFC822) ---")
import bubble_shield_mail as _bm  # noqa: E402
raw = (
    b"From: Jean DUPONT <j.dupont@wanadoo.fr>\r\n"
    b"Subject: Souscription PER\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Bonjour Monsieur Jean DUPONT, IBAN FR76 3000 6000 0112 3456 7890 189.\r\n"
)
frm, subj, body = _bm.parse_message(raw)
check("parse_message extracts From", "DUPONT" in frm and "wanadoo" in frm)
check("parse_message extracts Subject", subj == "Souscription PER")
check("parse_message extracts body", "IBAN FR76" in body)

# encoded-word header
raw2 = (
    b"From: =?UTF-8?B?SmVhbiBEVVBPTlQ=?= <j.dupont@wanadoo.fr>\r\n"
    b"Subject: =?UTF-8?Q?Souscription?=\r\n\r\nCorps.\r\n"
)
frm2, subj2, _ = _bm.parse_message(raw2)
check("parse_message decodes RFC-2047 From", "Jean DUPONT" in frm2)
check("parse_message decodes RFC-2047 Subject", "Souscription" in subj2)

# ---------------------------------------------------------------------------
# 2. load_credentials — fail-closed on missing / mis-permissioned
# ---------------------------------------------------------------------------
print("\n--- 2. load_credentials fail-closed ---")
d = Path(tempfile.mkdtemp())
os.environ["BUBBLE_SHIELD_MAIL_CREDS"] = str(d / "mail.json")
import importlib  # noqa: E402
importlib.reload(_bm)
try:
    _bm.load_credentials()
    check("missing creds → raises", False)
except _bm.MailConfigError:
    check("missing creds → raises MailConfigError", True)

creds_file = d / "mail.json"
creds_file.write_text('{"host":"imap.x","user":"u@x","password":"SECRETPW"}')
os.chmod(creds_file, 0o644)  # world-readable → must be refused
try:
    _bm.load_credentials()
    check("world-readable creds → refused", False)
except _bm.MailConfigError as e:
    check("world-readable creds → refused", True)
    check("permission error message hides the secret", "SECRETPW" not in str(e))

os.chmod(creds_file, 0o600)
c = _bm.load_credentials()
check("chmod-600 creds → loads", c["host"] == "imap.x" and c["mailbox"] == "INBOX")
del os.environ["BUBBLE_SHIELD_MAIL_CREDS"]

# ---------------------------------------------------------------------------
# 3. DAEMON DOWN → mail_read REFUSES (isError), NEVER raw e-mail  *** CRITICAL ***
# ---------------------------------------------------------------------------
print("\n--- 3. Daemon DOWN → fail-CLOSED (the whole point) ---")
mcp_down, mail_down = _import_mcp_with_port(1, _fresh_home())  # port 1 = unreachable
mail_down.fetch_mail = lambda **kw: [FAKE_MSG]                 # inject synthetic mail
mail_down.load_credentials = lambda: {"host": "x", "user": "u", "password": "p", "mailbox": "INBOX"}
mcp_down.sys.modules["bubble_shield_mail"] = mail_down
raised = False
leaked = None
try:
    mcp_down._anonymise_mail(query="ALL", maxn=3)
except mcp_down.NERDownError:
    raised = True
except Exception as e:  # any other error still must not carry raw PII
    leaked = str(e)
check("daemon-down mail_read → raises NERDownError (fail-closed)", raised)
if leaked is not None:
    check("daemon-down non-NER error → no raw name leaked", "DUPONT" not in leaked)
    check("daemon-down non-NER error → no raw IBAN leaked", "FR76" not in leaked)

# ---------------------------------------------------------------------------
# 4. DAEMON UP → cloaked body + shared token root (From ↔ body consistency)
# ---------------------------------------------------------------------------
print("\n--- 4. Daemon UP → cloaked + cross-field token consistency ---")
port, srv = start_mock_daemon()
time.sleep(0.05)
mcp_up, mail_up = _import_mcp_with_port(port, _fresh_home())
mail_up.fetch_mail = lambda **kw: [FAKE_MSG]
mail_up.load_credentials = lambda: {"host": "x", "user": "u", "password": "p", "mailbox": "INBOX"}
mcp_up.sys.modules["bubble_shield_mail"] = mail_up
out = mcp_up._anonymise_mail(query="ALL", maxn=3)

check("daemon-up mail_read → emits tokens", "⟦" in out)
check("daemon-up mail_read → no raw client name", "DUPONT" not in out)
check("daemon-up mail_read → no raw IBAN", "FR76 3000" not in out)
check("daemon-up mail_read → no raw email address", "j.dupont@wanadoo.fr" not in out)

# From-name and body-name must share the SAME NOM token ROOT (cross-field).
# The vault emits case/form variants as suffixes on a shared numeric root, e.g.
# the From shows ⟦NOM_0001a⟧ and the body ⟦NOM_0001⟧ — same client, same root
# 0001 (this is exactly the SCOPE's proven example). So consistency = ONE root.
nom_tokens = set(re.findall(r"⟦(NOM_[0-9A-Za-z]+)⟧", out))
check("daemon-up mail_read → a NOM token was produced", len(nom_tokens) >= 1)
# Root = the numeric part after NOM_ (a trailing case-variant letter is stripped).
nom_roots = {re.match(r"NOM_(\d+)", t).group(1) for t in nom_tokens if re.match(r"NOM_(\d+)", t)}
check("daemon-up mail_read → From & body share ONE name-token root (consistency)",
      len(nom_roots) == 1)

# ---------------------------------------------------------------------------
# 5. Empty result → friendly message (no crash)
# ---------------------------------------------------------------------------
print("\n--- 5. Empty mailbox → friendly message ---")
mail_up.fetch_mail = lambda **kw: []
empty = mcp_up._anonymise_mail(query="UNSEEN", maxn=3)
check("empty mailbox → no tokens, no error", "⟦" not in empty and "Aucun message" in empty)

srv.shutdown()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
