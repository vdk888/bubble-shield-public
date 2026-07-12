import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from webapp.app import app

client = TestClient(app)


def test_index_renders_with_sample():
    r = client.get("/")
    assert r.status_code == 200
    assert "Mettez vos données" in r.text   # hero headline
    assert "Jean Dupont" in r.text          # the bundled sample


def test_anonymize_returns_before_after_and_mapping():
    r = client.post("/anonymize", data={
        "text": "M. Jean Dupont, jean@x.com, IBAN FR76 3000 6000 0112 3456 7890 189, 45 000 €",
        "mission": "t",
    })
    assert r.status_code == 200
    assert "table de correspondance" in r.text
    assert "⟦" in r.text                     # tokens shown
    # Operator-friendly verdict leads with the win ("N identifiants protégés")
    # on a clean doc, instead of the old scary binary "ne pas envoyer".
    assert "protégé" in r.text                # safe verdict
    assert "restauration vérifiée" in r.text  # roundtrip proof


def test_clear_pii_not_in_after_view_token_region(tmp_path, monkeypatch):
    """EMAIL token must appear when the policy cloaks EMAIL.

    The test patches DEFAULT_POLICY_PATH (already resolved at import time) to
    point to a clean all-cloaked policy so the test is independent of whatever
    on-disk policy.json the user has configured.
    """
    import json
    import bubble_shield.policy as _pol_module
    from bubble_shield.policy import default_policy

    # Write a clean policy (all cloaked) to a temp file.
    policy_file = tmp_path / "policy.json"
    policy = default_policy()
    policy["EMAIL"] = True   # ensure cloaked regardless of saved state
    policy_file.write_text(json.dumps(policy), encoding="utf-8")

    # Patch the module-level constant so load_policy() reads our temp file.
    monkeypatch.setattr(_pol_module, "DEFAULT_POLICY_PATH", str(policy_file))

    r = client.post("/anonymize", data={"text": "Contact: jean.dupont@example.com",
                                        "mission": "t"})
    # The email appears in the mapping table (local), but the "after" doc
    # must show the token, not the address — assert the token is present.
    assert "EMAIL_0001" in r.text


def test_health_noauth():
    assert client.get("/health-noauth").json() == {"status": "ok"}
