"""Task 12 — validate the launchd sweep .plist TEMPLATE (not a live launchd load).

The template ships __ROOT__/__INTERVAL__ placeholders that install-app.sh
substitutes at install time. We substitute here the same way and assert the
result is a well-formed plist whose StartInterval is a bare INTEGER (a plist
StartInterval MUST be <integer>1200</integer>, not "1200") and whose
ProgramArguments invoke bubble_shield_sweep.py --root <root>.
"""
import plistlib
from pathlib import Path

# Repo root = two levels up from this test file (tests/ -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[1]
TPL = REPO_ROOT / "plugin" / "bubble-shield" / "launcher" / "com.bubbleinvest.bubble-shield-sweep.plist.tpl"


def test_plist_template_is_valid_and_has_interval():
    text = TPL.read_text().replace("__ROOT__", "/tmp/protected").replace("__INTERVAL__", "1200")
    pl = plistlib.loads(text.encode())
    # Must parse as an INT, not a string — plist StartInterval must be <integer>.
    assert pl["StartInterval"] == 1200
    assert isinstance(pl["StartInterval"], int)
    assert any("bubble_shield_sweep.py" in a for a in pl["ProgramArguments"])
    assert "/tmp/protected" in pl["ProgramArguments"]
    assert pl["Label"] == "com.bubbleinvest.bubble-shield-sweep"
    assert pl["RunAtLoad"] is True
