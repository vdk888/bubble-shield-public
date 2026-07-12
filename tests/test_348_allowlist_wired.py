import importlib, os


def test_daemon_match_filter_drops_org_names(tmp_path, monkeypatch):
    monkeypatch.setenv("BUBBLE_SHIELD_HOME", str(tmp_path))
    # Build the engine the daemon builds, anonymise text containing an org GLiNER would flag.
    from bubble_shield import AnonymizationEngine
    from bubble_shield.recognizers import Match
    from bubble_shield import policy as P
    from bubble_shield.allowlist import load_deployment_allowlist
    # The composed filter the daemon SHOULD use:
    pol = P.default_policy()
    base = P.make_match_filter(pol)
    al = load_deployment_allowlist()
    def composed(matches):
        return al.filter(base(matches))
    # CORUM is in PUBLIC_THIRD_PARTIES → must be dropped (kept in clear).
    m_corum = Match(value="CORUM", entity_type="NOM", start=0, end=5, score=0.9)
    m_real  = Match(value="Testclient Surname", entity_type="NOM", start=10, end=28, score=0.9)
    out = composed([m_corum, m_real])
    out_vals = [m.value for m in out]
    assert "CORUM" not in out_vals          # org suppressed
    assert "Testclient Surname" in out_vals  # real name kept for masking
