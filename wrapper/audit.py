"""Audit log: one JSON line per decision. Done for you; extend the fields if your policy needs more."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# gate.py judge/try set GATE_LOG_DIR so evaluation runs never pollute your demo audit sample.
LOG = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent) / "audit_log.jsonl"
_lock = threading.Lock()


def record(persona, text, context, decision):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": persona,
        "input": text,
        "context": context,
        "decision": decision.decision,
        "reason": decision.reason,
        "evidence": decision.evidence,
        "ezio": decision.ezio,
    }
    # Tamper-evident chain: each record carries the hash of the previous one.
    import hashlib
    with _lock:
        prev = "0" * 64
        if LOG.exists():
            lines = LOG.read_text().splitlines()
            if lines:
                prev = json.loads(lines[-1]).get("hash", prev)
        entry["prev_hash"] = prev
        entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
        with LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def _append(entry):
    import hashlib
    with _lock:
        prev = "0" * 64
        if LOG.exists():
            lines = LOG.read_text().splitlines()
            if lines:
                prev = json.loads(lines[-1]).get("hash", prev)
        entry["prev_hash"] = prev
        entry["hash"] = hashlib.sha256((prev + json.dumps(entry, sort_keys=True)).encode()).hexdigest()
        with LOG.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def record_review(text, verdict, reason):
    """A human reviewer's call on a held draft. Recorded only; the gate never sends on review."""
    _append({"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": "manager_review",
             "input": text, "review_verdict": verdict, "review_reason": reason})


def trail(limit=20):
    """Latest records and whether the hash chain verifies end to end."""
    import hashlib
    if not LOG.exists():
        return {"verified": True, "records": [], "count": 0}
    lines = LOG.read_text().splitlines()
    prev, verified, broken_at = "0" * 64, True, None
    for i, line in enumerate(lines):
        rec = json.loads(line)
        if "hash" not in rec:
            continue  # records written before chaining was added
        body = {k: v for k, v in rec.items() if k != "hash"}
        expected = hashlib.sha256((rec.get("prev_hash", "") + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        if rec.get("prev_hash") != prev or rec["hash"] != expected:
            verified, broken_at = False, i
            break
        prev = rec["hash"]
    slim = []
    for line in lines[-limit:]:
        rec = json.loads(line)
        slim.append({k: rec.get(k) for k in ("timestamp", "event", "input", "decision", "reason", "review_verdict", "review_reason", "hash")})
    return {"verified": verified, "broken_at": broken_at, "records": slim[::-1], "count": len(lines)}
