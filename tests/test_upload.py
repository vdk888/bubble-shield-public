"""
test_upload.py — the file-upload path (was previously untested).

Covers the three things that matter: a plain-text upload anonymises, a real
PDF is text-extracted and anonymised end-to-end through the webapp, and a PDF
with no extractable text (scanned/image) fails closed with a clear message
rather than feeding garbage into the anonymiser.
"""
import io

import pytest
from fastapi.testclient import TestClient

from webapp.app import app
from webapp.extract import ExtractionError, extract_text

client = TestClient(app)


def make_pdf(lines, *, with_text=True):
    """Build a minimal single-page PDF. `with_text=False` produces a valid PDF
    whose content stream draws nothing — i.e. no extractable text (mimics a
    scanned/image-only PDF)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    if with_text:
        content = "BT /F1 12 Tf 72 740 Td 16 TL\n"
        content += "".join(f"({esc(ln)}) Tj T*\n" for ln in lines)
        content += "ET"
    else:
        content = "q Q"                      # valid, but draws no text
    cb = content.encode("latin-1")
    objs = [
        b"<</Type /Catalog /Pages 2 0 R>>",
        b"<</Type /Pages /Kids [3 0 R] /Count 1>>",
        b"<</Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources <</Font <</F1 5 0 R>>>> /Contents 4 0 R>>",
        b"<</Length %d>>\nstream\n" % len(cb) + cb + b"\nendstream",
        b"<</Type /Font /Subtype /Type1 /BaseFont /Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<</Size %d /Root 1 0 R>>\nstartxref\n%d\n%%%%EOF"
            % (len(objs) + 1, xref_pos))
    return out


def test_extract_text_plain():
    assert extract_text("note.txt", "Jean Dupont".encode()) == "Jean Dupont"


def test_extract_text_pdf_unit():
    pdf = make_pdf(["Client : Monsieur Jean Dupont",
                    "IBAN FR76 3000 6000 0112 3456 7890 189"])
    out = extract_text("dossier.pdf", pdf)
    assert "Jean Dupont" in out and "FR76" in out


def test_scanned_pdf_fails_closed():
    pdf = make_pdf([], with_text=False)
    with pytest.raises(ExtractionError):
        extract_text("scan.pdf", pdf)


def test_upload_txt_anonymises(monkeypatch, tmp_path):
    # Redirect the policy path to a nonexistent file so load_policy() falls back
    # to default_policy() (all identifying types cloaked).  The live
    # ~/.bubble_shield/policy.json may have every type set to KEEP (false),
    # which would suppress all NOM matches and make the assertion below fail.
    import bubble_shield.policy as _bp
    monkeypatch.setattr(_bp, "DEFAULT_POLICY_PATH", str(tmp_path / "policy_isolation.json"))
    r = client.post(
        "/anonymize",
        data={"mission": "t"},
        files={"document": ("note.txt", io.BytesIO("Contact M. Jean Dupont".encode()), "text/plain")},
    )
    assert r.status_code == 200
    assert "⟦NOM_0001⟧" in r.text          # name from the uploaded file got tokenised
    assert "Jean Dupont" in r.text          # shown in the local-only mapping table


def test_upload_pdf_anonymises_end_to_end(monkeypatch, tmp_path):
    # Same policy isolation as test_upload_txt_anonymises — see comment there.
    import bubble_shield.policy as _bp
    monkeypatch.setattr(_bp, "DEFAULT_POLICY_PATH", str(tmp_path / "policy_isolation.json"))
    pdf = make_pdf(["FICHE D'ENTREE EN RELATION",
                    "Client : Monsieur Jean Dupont, ne le 14/03/1968.",
                    "IBAN FR76 3000 6000 0112 3456 7890 189"])
    r = client.post(
        "/anonymize",
        data={"mission": "t"},
        files={"document": ("dossier.pdf", io.BytesIO(pdf), "application/pdf")},
    )
    assert r.status_code == 200
    assert "table de correspondance" in r.text
    assert "⟦IBAN_" in r.text               # the IBAN from the PDF was detected & tokenised
    assert "FR76 3000 6000 0112 3456 7890 189" in r.text   # restored value in the vault table


def test_upload_scanned_pdf_shows_notice():
    pdf = make_pdf([], with_text=False)
    r = client.post(
        "/anonymize",
        data={"mission": "t"},
        files={"document": ("scan.pdf", io.BytesIO(pdf), "application/pdf")},
    )
    assert r.status_code == 200
    assert "scanné" in r.text               # the explanatory notice…
    assert 'class="verdict' not in r.text   # …on the home page, not a result page
