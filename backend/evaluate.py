"""Evaluation harness: replay data/lab/ through the same pipeline as the
kiosk, compare with expected.json, and write metrics.

    python -m backend.evaluate

Definitions (README, "Metrics"):
  FAR      = spoof clips decided ALLOW / all spoof clips
  FRR      = live clips expected ALLOW but decided DENY / live clips expected ALLOW
  accuracy = clips whose decision equals the expected decision / all clips
"""
import cv2
import numpy as np

from . import config, decision
from .store import now_iso


def default_expected(clip_type, enrolled):
    if clip_type == "live":
        return decision.ALLOW if enrolled else decision.DENY
    if clip_type == "poor_quality":
        return decision.REQUEST_VERIFICATION
    return decision.DENY


def load_clip(store, name):
    """3 to 5 frames, evenly spaced over the clip's stored images."""
    paths = store.clip_frame_paths(name)
    if len(paths) > 5:
        paths = [paths[i] for i in np.linspace(0, len(paths) - 1, 5).round().astype(int)]
    frames = [cv2.imread(str(p)) for p in paths]
    return [f for f in frames if f is not None]


def _pct(values, q):
    return round(float(np.percentile(values, q)), 1) if values else None


def _ratio(num, den):
    return round(num / den, 4) if den else None


def summarise(rows):
    """Metrics for one group of result rows."""
    n = len(rows)
    spoof = [r for r in rows if r["type"] in config.SPOOF_TYPES]
    genuine = [r for r in rows if r["type"] == "live" and r["expected"] == decision.ALLOW]
    lat = [r["latency_ms"] for r in rows]
    counts = {d: sum(r["actual"] == d for r in rows)
              for d in (decision.ALLOW, decision.REQUEST_VERIFICATION, decision.DENY)}
    return {
        "n": n,
        "correct": sum(r["correct"] for r in rows),
        "accuracy": _ratio(sum(r["correct"] for r in rows), n),
        "n_spoof": len(spoof),
        "false_accepts": sum(r["actual"] == decision.ALLOW for r in spoof),
        "far": _ratio(sum(r["actual"] == decision.ALLOW for r in spoof), len(spoof)),
        "n_genuine": len(genuine),
        "false_rejects": sum(r["actual"] == decision.DENY for r in genuine),
        "frr": _ratio(sum(r["actual"] == decision.DENY for r in genuine), len(genuine)),
        "genuine_stepups": sum(r["actual"] == decision.REQUEST_VERIFICATION for r in genuine),
        "decisions": counts,
        "p50_ms": _pct(lat, 50),
        "p95_ms": _pct(lat, 95),
    }


def run_harness(pipeline):
    store = pipeline.store
    expected = store.expected()
    rows, skipped = [], []
    warmed = False
    for clip in expected["clips"]:
        frames = load_clip(store, clip["name"])
        if len(frames) < 3:
            skipped.append({"name": clip["name"], "why": f"{len(frames)} readable frames, need 3"})
            continue
        if not warmed:  # keep one-off warm-up cost out of the latency numbers
            pipeline._analyse(frames)
            warmed = True
        rec = pipeline.verify(frames, clip_label=clip["name"], source="harness")
        rows.append({
            "name": clip["name"],
            "type": clip["type"],
            "person": clip.get("person"),
            "enrolled": clip.get("enrolled"),
            "lighting": clip.get("lighting"),
            "expected": clip["expected"],
            "actual": rec["decision"],
            "reason": rec["reason"],
            "correct": rec["decision"] == clip["expected"],
            "liveness": rec["liveness"],
            "similarity": rec["similarity"],
            "matched_name": rec["matched_name"],
            "gate_reason": rec["gate_reason"],
            "latency_ms": rec["latency_ms"],
        })

    types = [t for t in config.CLIP_TYPES if any(r["type"] == t for r in rows)]
    metrics = {
        "ran_at": now_iso(),
        "liveness_mode": pipeline.liveness.mode,
        "identity_available": pipeline.faces.identity_available,
        "thresholds": decision.thresholds(),
        "conditions": {
            "people": sorted({r["person"] for r in rows if r["person"]}),
            "lighting": sorted({r["lighting"] for r in rows if r["lighting"]}),
            "clip_types": types,
            "spoof_types_missing": [t for t in config.SPOOF_TYPES if t not in types],
            "notes": expected["conditions"].get("notes", ""),
        },
        "overall": summarise(rows),
        "by_type": {t: summarise([r for r in rows if r["type"] == t]) for t in types},
        "clips": rows,
        "skipped": skipped,
    }
    store.write_metrics(metrics)
    return metrics


def format_table(metrics):
    def cell(v, pct=False):
        if v is None:
            return "n/a"
        return f"{v * 100:.1f}%" if pct else str(v)

    lines = ["type          n  accuracy     FAR     FRR  step-up  p50 ms  p95 ms"]
    groups = list(metrics["by_type"].items()) + [("ALL", metrics["overall"])]
    for name, m in groups:
        lines.append(f"{name:<12}{m['n']:>3}  {cell(m['accuracy'], True):>8}  "
                     f"{cell(m['far'], True):>6}  {cell(m['frr'], True):>6}  "
                     f"{m['decisions']['REQUEST_VERIFICATION']:>7}  "
                     f"{cell(m['p50_ms']):>6}  {cell(m['p95_ms']):>6}")
    c = metrics["conditions"]
    lines.append(f"people: {', '.join(c['people']) or 'none'} | lighting: "
                 f"{', '.join(c['lighting']) or 'none'} | liveness mode: {metrics['liveness_mode']}")
    if c["spoof_types_missing"]:
        lines.append("spoof types NOT tested: " + ", ".join(c["spoof_types_missing"]))
    return "\n".join(lines)


if __name__ == "__main__":
    from .pipeline import Pipeline
    result = run_harness(Pipeline())
    if not result["clips"]:
        print("No clips to evaluate. Record some on the /lab page first.")
    else:
        print(format_table(result))
