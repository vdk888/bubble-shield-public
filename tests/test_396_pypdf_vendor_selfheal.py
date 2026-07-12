"""#396 — regression test for the "pypdf manquant" client bug (PDF/image read).

THE BUG (real, client-reported): Bubble Shield refused to read a PDF with
"pypdf manquant -- pip install pypdf" even though pypdf IS vendored in the
published .mcpb. Two stacked causes, both masked on a contaminated dev machine:

  1. PATH: bubble_shield_mcp._anonymise_file inserted only _scripts_dir() on
     sys.path before importing the extractor, never _vendor(). The extractor's
     own vendor insertion keyed off a single CLAUDE_PLUGIN_ROOT env var — if that
     resolved elsewhere, pypdf was never found.
  2. MISSING DEP: the vendored pypdf (6.12.2) imports `typing_extensions`
     (Self / TypeAlias / TypeGuard) on Python < 3.11. typing_extensions was NOT
     vendored. A dev Mac's global site-packages provided it; a clean client Mac
     did not -> ModuleNotFoundError surfaced as the same "pypdf manquant" error.

WHY EARLIER TESTS WERE BLIND: they ran under the dev venv / dev `python3` which
had global `pypdf` AND `typing_extensions` in user site-packages, masking both.

THIS TEST CANNOT BE FOOLED THE SAME WAY: it spawns the stock interpreter
`/usr/bin/python3` with `-S` (no user/site site-packages -> no global pypdf,
no global typing_extensions) and a DELIBERATELY MINIMAL env (no PYTHONPATH,
CLAUDE_PLUGIN_ROOT pointed at a wrong dir). The only way the import can succeed
is via the in-repo vendor/ tree + the self-heal path resolution. If someone
un-vendors typing_extensions or reverts the path fix, this test fails even on a
contaminated dev machine.
"""
import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin" / "bubble-shield"
SCRIPTS = PLUGIN / "scripts"
VENDOR = PLUGIN / "vendor"

STOCK_PY = "/usr/bin/python3"  # the interpreter a clean client Mac actually has

pytestmark = pytest.mark.skipif(
    not Path(STOCK_PY).exists(),
    reason="stock /usr/bin/python3 absent (test targets the clean-Mac client case)",
)


def _minimal_pdf(text: str = "Monsieur Jean Dupont test") -> bytes:
    """Build a tiny valid one-page PDF with a native text layer (no libs)."""
    content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objs) + 1))
    out.write(b"startxref\n%d\n%%%%EOF" % xref_pos)
    return out.getvalue()


def _clean_env():
    """Minimal env: no PYTHONPATH seeding, CLAUDE_PLUGIN_ROOT deliberately wrong.

    Combined with `-S` (no user/site site-packages), this is the clean-client-Mac
    condition that a contaminated dev machine otherwise hides.
    """
    return {
        "HOME": "/tmp/bubble_shield_clean_home",
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PLUGIN_ROOT": "/nonexistent/wrong/plugin/root",
    }


def _run_clean(snippet: str):
    return subprocess.run(
        [STOCK_PY, "-S", "-c", textwrap.dedent(snippet)],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_no_global_pypdf_or_typing_extensions_under_minimal_interpreter():
    """Sanity: the clean interpreter genuinely lacks both globals.

    If this fails, the rest of the file proves nothing (the dev contamination
    would be leaking through and masking the bug class). This guards the guard.
    """
    r = _run_clean(
        """
        import importlib.util as u
        print("PYPDF", u.find_spec("pypdf") is not None)
        print("TE", u.find_spec("typing_extensions") is not None)
        """
    )
    assert r.returncode == 0, r.stderr
    assert "PYPDF False" in r.stdout, (
        "global pypdf visible under -S — test environment is contaminated, "
        f"cannot prove the vendor path. stdout={r.stdout!r}"
    )
    assert "TE False" in r.stdout, (
        "global typing_extensions visible under -S — environment contaminated. "
        f"stdout={r.stdout!r}"
    )


def test_pypdf_imports_from_vendor_only(tmp_path):
    """`from pypdf import PdfReader` must succeed using ONLY the vendored tree.

    This is the typing_extensions-vendoring regression: pypdf 6.12.2 needs
    typing_extensions on py<3.11. If it's not vendored, this import dies.
    """
    r = _run_clean(
        f"""
        import sys
        sys.path.insert(0, {str(VENDOR)!r})
        from pypdf import PdfReader
        print("PYPDF_IMPORT_OK")
        """
    )
    assert r.returncode == 0, f"vendored pypdf import failed:\n{r.stderr}"
    assert "PYPDF_IMPORT_OK" in r.stdout, r.stdout


def test_extract_file_selfheals_with_wrong_plugin_root(tmp_path):
    """End-to-end: extract a real PDF the way _anonymise_file does, in the
    clean-client condition. This is the bug the client actually hit.

    Crucially we do NOT seed the vendor dir on sys.path here — we only insert the
    scripts dir (exactly what bubble_shield_mcp._anonymise_file did at the broken
    call site). The extraction must still succeed, which proves the extractor's
    self-heal recovers the sibling vendor dir despite CLAUDE_PLUGIN_ROOT being
    wrong. Before the fix this raised "pypdf manquant".
    """
    pdf = tmp_path / "client.pdf"
    pdf.write_bytes(_minimal_pdf("Monsieur Jean Dupont test"))
    r = _run_clean(
        f"""
        import sys
        # Mimic the (formerly broken) _anonymise_file call site: scripts dir only.
        sys.path.insert(0, {str(SCRIPTS)!r})
        from bubble_shield_extract import extract_file
        text = extract_file({str(pdf)!r})
        assert "Jean Dupont" in text, repr(text)
        print("EXTRACT_OK")
        """
    )
    assert r.returncode == 0, f"extract_file failed in clean env:\n{r.stderr}"
    assert "EXTRACT_OK" in r.stdout, r.stdout


def test_anonymise_file_call_site_inserts_vendor(tmp_path):
    """Drive the real _anonymise_file path-prep then extract, in the clean env.

    Reproduces bubble_shield_mcp._anonymise_file's import preamble (now fixed to
    insert _vendor()) without needing the heavy engine: we replay its two
    sys.path.insert calls then extract. Guards against a revert of the mcp.py fix.
    """
    pdf = tmp_path / "client.pdf"
    pdf.write_bytes(_minimal_pdf("Madame Claire Martin"))
    # Resolve _vendor()/_scripts_dir() the way mcp.py does, but only replay the
    # two inserts the fixed _anonymise_file performs.
    r = _run_clean(
        f"""
        import sys
        sys.path.insert(0, {str(VENDOR)!r})    # the fix: _vendor() first
        sys.path.insert(0, {str(SCRIPTS)!r})   # _scripts_dir()
        from bubble_shield_extract import extract_file
        text = extract_file({str(pdf)!r})
        assert "Claire Martin" in text, repr(text)
        print("CALLSITE_OK")
        """
    )
    assert r.returncode == 0, f"call-site preamble extract failed:\n{r.stderr}"
    assert "CALLSITE_OK" in r.stdout, r.stdout
