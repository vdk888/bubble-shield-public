"""
test_daemon_onnx_detection.py — regression tests for the "healthy but blind"
daemon bug (fix/daemon-onnx-detection).

Root cause: _prepare_env() pointed BUBBLE_SHIELD_GLINER_MODEL at the local ONNX
model directory.  GLiNER.from_pretrained(<onnx-only-dir>) raises FileNotFoundError
because that dir has no pytorch weights; _load_model() silently caches None;
gliner_matches() always returns [].  The daemon reported /health ok=true, _warm=true
but every /detect returned {"matches": []}.  The fail-closed gate only checked
reachability — not detection capability — so the "healthy but blind" daemon was
treated as UP, names leaked, only the regex layer ran.

Two fixes in this PR:
  Fix 1  — _gliner_model_id() picks a PyTorch-loadable id (not the ONNX-only dir).
  Fix 2  — /health exposes self_test:"pass"|"fail"; _daemon_up() gates on it;
            a blind daemon is treated as DOWN (fail-closed) by the MCP gate.

All PII in this file is SYNTHETIC.  No real client names committed anywhere.
"""
from __future__ import annotations

import json
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — allow importing from the plugin scripts dir and vendor dir
# ---------------------------------------------------------------------------
_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "scripts"
_VENDOR = Path(__file__).resolve().parent.parent / "plugin" / "bubble-shield" / "vendor"
sys.path.insert(0, str(_SCRIPTS))
sys.path.insert(0, str(_VENDOR))


# ===========================================================================
# FIX 1 — _gliner_model_id: ONNX-only dir → fallback to PyTorch model id
# ===========================================================================

class TestGlinerModelId(unittest.TestCase):
    """_gliner_model_id() must never return an ONNX-only local path."""

    def _call(self, man: dict) -> str:
        """Import fresh each time (env-independent)."""
        import importlib
        import bubble_shield_nerd as nerd
        importlib.reload(nerd)
        return nerd._gliner_model_id(man)

    def test_onnx_only_dir_falls_back_to_default(self, tmp_path=None):
        """A dir with only onnx/ contents → fallback to urchade (the known-working id)."""
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            # Create ONNX-only model dir (no .safetensors or pytorch_model.bin)
            onnx_dir = Path(d) / "onnx-community__gliner_multi_pii-v1"
            (onnx_dir / "onnx").mkdir(parents=True)
            (onnx_dir / "onnx" / "model_quantized.onnx").write_bytes(b"\x00")
            man = {
                "model_dir": str(onnx_dir),
                "model_id": "onnx-community/gliner_multi_pii-v1",
                "onnx_file": "onnx/model_quantized.onnx",
            }
            import bubble_shield_nerd as nerd
            result = nerd._gliner_model_id(man)
            # Must NOT be the local ONNX-only dir (which has no PyTorch weights)
            self.assertNotEqual(result, str(onnx_dir),
                                "ONNX-only dir must not be returned as model id")
            # Should be an HF repo id — i.e. NOT a local absolute path
            self.assertFalse(Path(result).is_absolute(),
                             f"Fallback should be an HF id, not an absolute path. Got: {result}")

    def test_onnx_only_dir_uses_pytorch_model_id_from_manifest(self):
        """pytorch_model_id field in manifest takes priority over hard default."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            onnx_dir = Path(d) / "model_onnx"
            onnx_dir.mkdir()
            man = {
                "model_dir": str(onnx_dir),
                "model_id": "onnx-community/gliner_multi_pii-v1",
                "pytorch_model_id": "urchade/gliner_multi_pii-v1",
                "onnx_file": "onnx/model.onnx",
            }
            import bubble_shield_nerd as nerd
            result = nerd._gliner_model_id(man)
            self.assertEqual(result, "urchade/gliner_multi_pii-v1")

    def test_pytorch_dir_is_used_as_is(self):
        """A dir with pytorch_model.bin returns the local path (preferred)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            model_dir = Path(d) / "my_model"
            model_dir.mkdir()
            (model_dir / "pytorch_model.bin").write_bytes(b"\x00")  # fake weight file
            man = {"model_dir": str(model_dir), "model_id": "urchade/gliner_multi_pii-v1"}
            import bubble_shield_nerd as nerd
            result = nerd._gliner_model_id(man)
            self.assertEqual(result, str(model_dir))

    def test_safetensors_dir_is_used_as_is(self):
        """A dir with model.safetensors also returns the local path."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            model_dir = Path(d) / "safe_model"
            model_dir.mkdir()
            (model_dir / "model.safetensors").write_bytes(b"\x00")
            man = {"model_dir": str(model_dir), "model_id": "urchade/gliner_multi_pii-v1"}
            import bubble_shield_nerd as nerd
            result = nerd._gliner_model_id(man)
            self.assertEqual(result, str(model_dir))

    def test_empty_manifest_falls_back_to_urchade(self):
        """Manifest without model_dir or pytorch_model_id → hard default."""
        import bubble_shield_nerd as nerd
        result = nerd._gliner_model_id({})
        self.assertEqual(result, "urchade/gliner_multi_pii-v1")


# ===========================================================================
# FIX 1 — _run_selftest: pass/fail based on whether gliner_matches returns NOM
# ===========================================================================

class TestRunSelftest(unittest.TestCase):
    """_run_selftest() must return 'pass' when NOM detected, 'fail' when not."""

    def setUp(self):
        import bubble_shield_nerd as nerd
        self.nerd = nerd

    def _make_gliner_ext_stub(self, *, returns_nom: bool):
        """Build a minimal gliner_ext stub."""
        stub = types.SimpleNamespace()
        if returns_nom:
            from bubble_shield.recognizers import Match
            stub.gliner_matches = lambda text: [
                Match(start=0, end=20, entity_type="NOM",
                      value="Jean DUPONT", score=0.8, priority=5)
            ]
        else:
            stub.gliner_matches = lambda text: []
        return stub

    def test_returns_pass_when_nom_detected(self):
        stub = self._make_gliner_ext_stub(returns_nom=True)
        result = self.nerd._run_selftest(stub)
        self.assertEqual(result, "pass")

    def test_returns_fail_when_empty_matches(self):
        stub = self._make_gliner_ext_stub(returns_nom=False)
        result = self.nerd._run_selftest(stub)
        self.assertEqual(result, "fail")

    def test_returns_fail_on_exception(self):
        stub = types.SimpleNamespace()
        def boom(text):
            raise RuntimeError("model exploded")
        stub.gliner_matches = boom
        result = self.nerd._run_selftest(stub)
        self.assertEqual(result, "fail")

    def test_only_nom_type_counts_as_pass(self):
        """A non-NOM match (e.g. ADRESSE) must NOT be counted as passing self-test."""
        from bubble_shield.recognizers import Match
        stub = types.SimpleNamespace()
        stub.gliner_matches = lambda text: [
            Match(start=0, end=5, entity_type="ADRESSE",
                  value="Paris", score=0.9, priority=5)
        ]
        result = self.nerd._run_selftest(stub)
        self.assertEqual(result, "fail",
                         "Self-test must fail when only non-NOM entities returned")


# ===========================================================================
# FIX 2 — /health exposes self_test field; /selftest endpoint
# ===========================================================================

class _HealthDaemonHandler(BaseHTTPRequestHandler):
    """Minimal handler that mimics the fixed daemon /health + /selftest responses."""
    selftest_val = "pass"   # class-level, override per test

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({
                "ok": True, "warm": True,
                "model": "urchade/gliner_multi_pii-v1",
                "mode": "gliner",
                "self_test": self.__class__.selftest_val,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/selftest":
            ok = self.__class__.selftest_val == "pass"
            body = json.dumps({
                "ok": ok,
                "self_test": self.__class__.selftest_val,
                "probe": "Monsieur Jean DUPONT",
            }).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def _start_health_daemon(selftest_val: str):
    """Start a daemon that returns the given self_test value in /health."""
    class _H(_HealthDaemonHandler):
        pass
    _H.selftest_val = selftest_val
    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return port, srv


class TestHealthSelfTestField(unittest.TestCase):
    """Fixed /health must include self_test field."""

    def test_health_includes_selftest_pass(self):
        port, srv = _start_health_daemon("pass")
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2)
            data = json.loads(resp.read())
            self.assertEqual(data.get("self_test"), "pass")
            self.assertTrue(data.get("ok"))
        finally:
            srv.shutdown()

    def test_health_includes_selftest_fail(self):
        port, srv = _start_health_daemon("fail")
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2)
            data = json.loads(resp.read())
            self.assertEqual(data.get("self_test"), "fail")
        finally:
            srv.shutdown()

    def test_selftest_endpoint_pass(self):
        port, srv = _start_health_daemon("pass")
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/selftest", timeout=2)
            data = json.loads(resp.read())
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("self_test"), "pass")
            self.assertIn("probe", data)
        finally:
            srv.shutdown()

    def test_selftest_endpoint_fail_returns_503(self):
        port, srv = _start_health_daemon("fail")
        try:
            import urllib.error
            import urllib.request
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/selftest", timeout=2)
                self.fail("Expected HTTPError 503")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 503)
                data = json.loads(e.read())
                self.assertFalse(data.get("ok"))
                self.assertEqual(data.get("self_test"), "fail")
        finally:
            srv.shutdown()


# ===========================================================================
# FIX 2 — _daemon_up() gates on self_test field in posttool_anonymize
# ===========================================================================

class TestDaemonUpSelfTest(unittest.TestCase):
    """posttool_anonymize._daemon_up() must return False for self_test=fail."""

    def _daemon_up_with_port(self, port: int) -> bool:
        import importlib
        import posttool_anonymize as pa
        # Override the port without reloading (module-level constant)
        original = pa.NERD_URL
        pa.NERD_URL = f"http://127.0.0.1:{port}"
        try:
            return pa._daemon_up()
        finally:
            pa.NERD_URL = original

    def test_daemon_up_false_when_selftest_fail(self):
        """A daemon answering /health with self_test=fail must be treated as DOWN."""
        port, srv = _start_health_daemon("fail")
        try:
            result = self._daemon_up_with_port(port)
            self.assertFalse(result,
                "_daemon_up() must return False for self_test=fail (blind daemon)")
        finally:
            srv.shutdown()

    def test_daemon_up_true_when_selftest_pass(self):
        """A daemon answering /health with self_test=pass must be treated as UP."""
        port, srv = _start_health_daemon("pass")
        try:
            result = self._daemon_up_with_port(port)
            self.assertTrue(result)
        finally:
            srv.shutdown()

    def test_daemon_up_false_when_unreachable(self):
        """Port 1 is always unreachable → must return False."""
        import posttool_anonymize as pa
        original = pa.NERD_URL
        pa.NERD_URL = "http://127.0.0.1:1"
        try:
            self.assertFalse(pa._daemon_up())
        finally:
            pa.NERD_URL = original

    def test_daemon_up_true_for_old_daemon_plain_ok_response(self):
        """Old daemon builds return plain text 'ok' (not JSON).
        Backward compat: trust the 200 (no self_test field → UP)."""
        class _OldDaemonHandler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == "/health":
                    body = b"ok"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), _OldDaemonHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = self._daemon_up_with_port(port)
            self.assertTrue(result,
                "Old daemon (plain 'ok' response) should be treated as UP")
        finally:
            srv.shutdown()

    def test_daemon_up_true_when_selftest_none(self):
        """New daemon with self_test=null (not yet warmed) should be treated as UP
        (no-warm path or test-before-warmup case — fail-open on null)."""
        class _NullSelfTestHandler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == "/health":
                    body = json.dumps({"ok": True, "warm": False,
                                       "self_test": None}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), _NullSelfTestHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            result = self._daemon_up_with_port(port)
            self.assertTrue(result, "null self_test should be treated as UP (fail-open)")
        finally:
            srv.shutdown()


# ===========================================================================
# LAZY SELF-TEST — --no-warm daemon populates self_test on first /detect call
# ===========================================================================

class TestLazySelfTestOnFirstDetect(unittest.TestCase):
    """After the first /detect, a --no-warm daemon must have a real self_test.

    The "healthy but blind" class under --no-warm:
      - daemon starts with --no-warm → _selftest_result stays None
      - /health reports self_test:null → _daemon_up() treats it as UP (fail-open)
      - if the model is broken, every /detect returns [] while looking healthy

    Fix: on the first /detect the daemon runs _run_selftest() lazily and caches
    the result.  After that call:
      - broken model  → self_test="fail"  → _daemon_up() returns False (fail-closed)
      - healthy model → self_test="pass"  → _daemon_up() returns True  (UP)
    """

    def _make_nerd_module(self):
        """Return a freshly imported bubble_shield_nerd with its module globals
        reset so tests don't bleed into each other."""
        import importlib
        import bubble_shield_nerd as nerd
        importlib.reload(nerd)
        return nerd

    def test_no_warm_broken_model_flips_to_fail_after_detect(self):
        """--no-warm + broken model: after first /detect self_test must be 'fail'
        and _daemon_up() (gating on /health) must return False."""
        import types
        from http.server import BaseHTTPRequestHandler, HTTPServer

        nerd = self._make_nerd_module()

        # Broken gliner_ext: model never detects anything
        broken_ext = types.SimpleNamespace()
        broken_ext.gliner_matches = lambda text: []

        # Inject into Handler as --no-warm would do (no warm_up call)
        nerd.Handler.gliner_ext = broken_ext
        nerd.Handler.openai_pf_ext = types.SimpleNamespace(
            openai_pf_matches=lambda text, **kw: [])
        nerd.Handler.merge_mod = types.SimpleNamespace(
            merge_soft=lambda a, b: [])
        nerd._selftest_result = None   # explicit: simulate --no-warm start

        # Start the real nerd server (not a stub) on a random port
        srv = HTTPServer(("127.0.0.1", 0), nerd.Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        import urllib.request
        try:
            # Before first /detect: self_test is null → _daemon_up() treats as UP
            h_before = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2).read())
            self.assertIsNone(h_before.get("self_test"),
                              "Before first /detect, --no-warm daemon should report null")

            # Fire first /detect
            body = json.dumps({"text": "test"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/detect",
                data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Content-Length": str(len(body))})
            urllib.request.urlopen(req, timeout=2)

            # After first /detect: self_test must be "fail" (broken model)
            h_after = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2).read())
            self.assertEqual(h_after.get("self_test"), "fail",
                             "After first /detect with broken model, self_test must be 'fail'")

            # _daemon_up() must now return False (fail-closed)
            import posttool_anonymize as pa
            original = pa.NERD_URL
            pa.NERD_URL = f"http://127.0.0.1:{port}"
            try:
                self.assertFalse(pa._daemon_up(),
                                 "After self_test=fail is set, _daemon_up() must return False")
            finally:
                pa.NERD_URL = original
        finally:
            srv.shutdown()

    def test_no_warm_healthy_model_flips_to_pass_after_detect(self):
        """--no-warm + healthy model: after first /detect self_test must be 'pass'
        and _daemon_up() must return True."""
        import types
        from bubble_shield.recognizers import Match

        nerd = self._make_nerd_module()

        # Healthy gliner_ext: detects NOM correctly
        healthy_ext = types.SimpleNamespace()
        healthy_ext.gliner_matches = lambda text: [
            Match(start=0, end=20, entity_type="NOM",
                  value="Jean DUPONT", score=0.8, priority=5)
        ]
        nerd.Handler.gliner_ext = healthy_ext
        nerd.Handler.openai_pf_ext = types.SimpleNamespace(
            openai_pf_matches=lambda text, **kw: [])
        nerd.Handler.merge_mod = types.SimpleNamespace(
            merge_soft=lambda a, b: a)
        nerd._selftest_result = None   # simulate --no-warm

        srv = HTTPServer(("127.0.0.1", 0), nerd.Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        import urllib.request
        try:
            # Fire first /detect with any text
            body = json.dumps({"text": "Monsieur Jean DUPONT"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/detect",
                data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Content-Length": str(len(body))})
            urllib.request.urlopen(req, timeout=2)

            # After detect: self_test must be "pass"
            h_after = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2).read())
            self.assertEqual(h_after.get("self_test"), "pass",
                             "After first /detect with healthy model, self_test must be 'pass'")

            # _daemon_up() must return True
            import posttool_anonymize as pa
            original = pa.NERD_URL
            pa.NERD_URL = f"http://127.0.0.1:{port}"
            try:
                self.assertTrue(pa._daemon_up(),
                                "After self_test=pass is set, _daemon_up() must return True")
            finally:
                pa.NERD_URL = original
        finally:
            srv.shutdown()


# ===========================================================================
# FIX 2 — MCP gate: blind daemon (self_test=fail) → NERDownError, fail-closed
# ===========================================================================

class TestMCPGateBlindDaemon(unittest.TestCase):
    """_anonymise_text must raise NERDownError when the daemon is blind.

    This test uses an MCP subprocess (bubble_shield_mcp.py driven over stdio)
    with a mock daemon that returns self_test=fail in /health and [] in /detect.
    We assert that bubble_shield_anonymize_text returns isError:true with no
    anonymized body and no raw PII.
    """

    SYNTHETIC_TEXT = (
        "Nom: Jean DUPONT\n"
        "Né le: 01/01/1980 à Paris\n"
        "Email: jean.dupont@testpii.invalid\n"
        "IBAN: FR76 3000 6000 0112 3456 7890 189"
    )

    def _start_blind_daemon(self):
        """Daemon that answers /health ok=true but self_test=fail, and /detect returns []."""
        class _BlindHandler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                if self.path == "/health":
                    body = json.dumps({"ok": True, "warm": True,
                                       "model": "broken",
                                       "mode": "gliner",
                                       "self_test": "fail"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
            def do_POST(self):
                if self.path == "/detect":
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    body = json.dumps({"matches": []}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), _BlindHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return port, srv

    def _rpc(self, calls, *, nerd_port: int):
        import os, subprocess, tempfile
        SERVER = _SCRIPTS / "bubble_shield_mcp.py"
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_ROOT"] = str(_SCRIPTS.parent)
        env["BUBBLE_SHIELD_HOME"] = str(Path(tempfile.mkdtemp()) / "bshome")
        env["HOME"] = str(Path(tempfile.mkdtemp()) / "fakehome")
        env["BUBBLE_SHIELD_NERD_PORT"] = str(nerd_port)
        lines = "\n".join(json.dumps(c) for c in calls) + "\n"
        r = subprocess.run([sys.executable, str(SERVER)], input=lines,
                           capture_output=True, text=True, env=env, timeout=30)
        out = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                if "id" in o:
                    out[o["id"]] = o
            except Exception:
                pass
        return out

    def test_blind_daemon_self_test_fail_triggers_ner_down_error(self):
        """bubble_shield_anonymize_text with a self_test=fail daemon must return
        isError:true — fail-closed, not leak."""
        port, srv = self._start_blind_daemon()
        try:
            INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            ANON = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "bubble_shield_anonymize_text",
                               "arguments": {"text": self.SYNTHETIC_TEXT}}}
            r = self._rpc([INIT, ANON], nerd_port=port)
            res = r.get(2, {})
            # Must be isError — the blind daemon should not be trusted
            self.assertTrue(res.get("result", {}).get("isError"),
                            "Blind daemon (self_test=fail) must trigger fail-closed "
                            f"(isError:true). Got: {res}")
            # Raw PII must NOT appear in the error body
            text = res.get("result", {}).get("content", [{}])[0].get("text", "")
            self.assertNotIn("FR76 3000", text,
                             "Raw IBAN must not appear in error response")
            self.assertNotIn("jean.dupont@testpii.invalid", text,
                             "Raw email must not appear in error response")
            self.assertNotIn("Jean DUPONT", text,
                             "Synthetic name must not appear in error response")
        finally:
            srv.shutdown()

    def test_blind_daemon_read_of_unindexed_file_serves_raw_daemon_independent(self):
        """B1 read contract: bubble_shield_read of an UNINDEXED file is
        daemon-independent — it hashes the file, misses the shadow cache, and
        serves the RAW extracted text (accepted, client-agreed B1 gap for
        speed). It runs ZERO models at read time, so it emits no ⟦…⟧ tokens and
        does NOT fail-closed regardless of daemon health.

        This inverts the pre-redesign assertion. Previously this test required
        bubble_shield_read to return isError:true (fail-closed) when the NER
        daemon was blind. The shadow-index redesign (Task 5) RETIRED that
        contract: the read path no longer touches the daemon, so a blind daemon
        is irrelevant to a read. A read of an unindexed doc serves raw by
        design. Mirrors the B1 re-encoding in
        scripts/test_bubble_shield_mcp.py sections 3 & 6.

        NOTE: the sibling test above — anonymize_text with a blind daemon must
        still return isError:true — is UNCHANGED. That path still runs models
        and still fails-closed; only the read path became daemon-independent.
        """
        import tempfile
        port, srv = self._start_blind_daemon()
        try:
            tf = Path(tempfile.mkdtemp()) / "test_synthetic.txt"
            tf.write_text(self.SYNTHETIC_TEXT, encoding="utf-8")
            INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            READ = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "bubble_shield_read",
                               "arguments": {"path": str(tf)}}}
            r = self._rpc([INIT, READ], nerd_port=port)
            res = r.get(2, {})
            # B1: read is daemon-independent → no fail-closed even on blind daemon.
            self.assertFalse(res.get("result", {}).get("isError"),
                             "B1 read is daemon-independent: an unindexed read must "
                             f"NOT fail-closed on a blind daemon. Got: {res}")
            body = res.get("result", {}).get("content", [{}])[0].get("text", "")
            # MISS → raw extracted text served verbatim (no masking at read time).
            self.assertIn("Jean DUPONT", body,
                          "B1 read miss must serve the raw extracted text verbatim")
            self.assertIn("FR76 3000", body,
                          "B1 read miss must serve the raw extracted text verbatim")
            # No models ran at read time → no ⟦…⟧ tokenisation in the body.
            self.assertNotIn("⟦", body,
                             "B1 read runs no models: body must contain no ⟦tokens⟧")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
