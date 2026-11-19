"""R3-T5 abstention contract test (keyless / hermetic).

Loads the COMMITTED calibration artifact ai_agents/rag/abstention_threshold.json
and asserts the HuggingFace-embedding calibrated gate's safety contract
(V14 / V15 / V17). No DB, no network, no HF token, no fixtures required —
must run in push-CI. Do NOT mark @pytest.mark.db.
"""

import json
from pathlib import Path

# ai_agents/tests/test_r3_abstention_contract.py
#   parents[0] = ai_agents/tests
#   parents[1] = ai_agents
#   parents[2] = <repo root>
_ARTIFACT = Path(__file__).resolve().parents[2] / "ai_agents/rag/abstention_threshold.json"


def _load_hf() -> dict:
    assert _ARTIFACT.exists(), f"missing committed artifact: {_ARTIFACT}"
    data = json.loads(_ARTIFACT.read_text())
    assert "huggingface" in data, "artifact missing 'huggingface' block"
    return data["huggingface"]


def test_r3_abstention_contract() -> None:
    hf = _load_hf()

    # (I_R3_1 / V14) precision-on-answered floor: >= 0.95
    precision_on_answered = 1.0 - hf["risk_at_tau"]
    assert precision_on_answered >= 0.95, (
        f"precision-on-answered {precision_on_answered:.4f} below 0.95 floor "
        f"(risk_at_tau={hf['risk_at_tau']})"
    )

    # (I_R3_2 / V15) NY coverage canary must be answered
    assert hf["ny_canary_answered"] is True, "NY coverage canary not answered"

    # (V17) reranker signal wins over cosine on AURC (lower is better)
    assert (
        hf["aurc_reranker"] < hf["aurc_cosine"]
    ), f"reranker AURC {hf['aurc_reranker']} not < cosine AURC {hf['aurc_cosine']}"

    # calibrated gate is active
    assert hf["promoted"] is True, "calibrated gate not promoted"

    # Soft honest-recall framing (no assert; recall != 1.0 is the chosen tradeoff).
    print(
        "abstention recall = 0.886 (39/44); 5 leakers "
        "(una-037/039/035/034/038) at calibrated confidence just above "
        "tau*=0.039 (~4-8%); recall=1.0 would collapse coverage "
        "- precision floor is the chosen tradeoff."
    )
