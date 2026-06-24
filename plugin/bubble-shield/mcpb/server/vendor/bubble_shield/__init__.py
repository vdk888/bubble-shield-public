"""
bubble_shield — local, reversible PII anonymisation for LLM workflows.

Anonymise sensitive text on the way IN to an LLM, de-anonymise the answer on
the way OUT, with the real values never leaving the machine. A reliable
anonymiser + a demo webapp; the LLM wiring (Claude Code hooks / a proxy) is a
separate, downstream concern.

Doctrine:
  - reversible: anonymise on the way in, de-anonymise on the way out;
  - the mapping vault is per client/mission, stays on the machine, and is
    NEVER part of what would be sent to an LLM;
  - fail-closed: if residual PII is detected after anonymisation, the doc is
    flagged unsafe to send;
  - recall is the real risk (a missed PII leaks), so the bench optimises for
    it and calibrates the fail-closed threshold;
  - layered detection: a zero-dependency regex/checksum core, plus OPTIONAL
    local NER (Presidio) and a local LLM (Ollama) for prose — see PRIOR_ART.md.
"""
from bubble_shield.engine import AnonymizationEngine, AnonymizationResult
from bubble_shield.vault import Vault

__all__ = ["AnonymizationEngine", "AnonymizationResult", "Vault"]
__version__ = "1.17.0"
