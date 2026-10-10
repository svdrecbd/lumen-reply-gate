"""Your wrapper. Start with check_source_of_truth() and policy.json.

Flow: call Ezio -> read its signals -> check your persona's source of truth -> apply policy
-> decision (+ reason, evidence) -> audit log -> run the mock action only if approved.

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan."
Score yourself: python3 evaluate.py customer_support
"""
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import mock_actions
from ezio_client import EzioError, analyze

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in ("tone", "bias", "agreement")]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=resp.get("claims", []),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise EzioError("malformed mock response", resp) from e


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


def check_source_of_truth(text, context, sot, signals=None):
    """Independent signals: the persona's own data, plus Ezio's per-claim fact-check where it can see."""
    findings = []
    if "price_sheet" in sot and "policies" in sot:
        from checks import check_customer_support
        findings += [Finding(level, message) for level, message in check_customer_support(text, context, sot)]
    if signals is not None:
        contradicted = [c for c in signals.claims if str(c.get("verdict", c.get("status", ""))).lower() in ("contradicted", "refuted", "false")]
        if contradicted or signals.claims_contradicted > 0:
            detail = "; ".join(str(c.get("text") or c.get("claim") or "")[:120] for c in contradicted) or f"{signals.claims_contradicted} claim(s)"
            findings.append(Finding(BLOCK, f"Ezio fact-check contradicts: {detail}"))
    return findings


def apply_policy(signals, findings, policy):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append(reason)
        if RANK[level] > RANK[decision]:
            decision = level

    for f in findings:
        raise_to(f.level, f.message)
    for dim, rule in policy["dimension_minimums"].items():
        score = getattr(signals, dim)
        if score < rule["hold_below"]:
            raise_to(HOLD, f"{dim} score {score} is below {rule['hold_below']}")
    if signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        raise_to(policy["when_accuracy_not_scored"], "accuracy was not scored (no claims checked)")
    if signals.accuracy_status == "unavailable":
        raise_to(policy["when_accuracy_unavailable"], "fact-check was unavailable")
    tier_decision = policy["tier_decisions"][signals.tier]
    if tier_decision != APPROVE:
        raise_to(tier_decision, f"Ezio tier is {signals.tier} ({signals.score})")
    if not reasons:
        reasons.append(f"No issues found (Ezio {signals.tier} {signals.score})")
    return reasons, decision


def signal_evidence(signals):
    return [f"Ezio tier {signals.tier} (score {signals.score}); accuracy {signals.accuracy_status}"
            f" ({signals.claims_verified}/{signals.claims_total} claims verified, {signals.claims_contradicted} contradicted)",
            f"Ezio dimensions: tone {signals.tone}, bias {signals.bias}, agreement {signals.agreement}"]


CHECKS_RUN = ("Checked against price_sheet.csv and policies.json: prices, discounts and eligibility, stacking, "
              "refund windows, response times, trials, unauthorized promises, promotional language")


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context or {}
    sot = load_source_of_truth(persona)
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        # Ezio is down or malformed: our own data checks still run; fail closed otherwise.
        findings = check_source_of_truth(text, context, sot)
        blocking = [f for f in findings if f.level == BLOCK]
        decision = BLOCK if blocking else POLICY["on_ezio_failure"]
        reason = ("; ".join(f.message for f in blocking) + " (Ezio unavailable)") if blocking else \
                 f"Ezio unavailable ({e}); held for a human because public claims could not be fact-checked"
        evidence = [f.message for f in findings] + [f"Ezio error: {e}", CHECKS_RUN]
        result = Decision(decision, reason, evidence, {"error": str(e), "response": e.response})
    else:
        findings = check_source_of_truth(text, context, sot, signals)
        reasons, decision = apply_policy(signals, findings, POLICY)
        critical = [f for f in response.get("findings", []) if isinstance(f, dict) and f.get("severity") == "CRITICAL"]
        if critical and decision == APPROVE:
            # Ezio's own critical finding is a reason for a human look, even under a GREEN tier.
            decision = HOLD
            reasons = [f"Ezio raised a CRITICAL finding: {str(critical[0].get('message', '')).strip()[:160] or 'see Ezio findings'}"]
        evidence = [f.message for f in findings] + signal_evidence(signals) + [CHECKS_RUN]
        result = Decision(decision, "; ".join(reasons), evidence, response)
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
