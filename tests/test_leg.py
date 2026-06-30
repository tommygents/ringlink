"""L3 leg-cooking tests, verified against the real Phase-3 left-pad trace.

The headline is `test_gait_squat_invariant_holds`: replaying the captured squat
sequence, `gait` is NEVER `run`/`sprint` while `squatting` — the design's G1
invariant and a Phase-4 success criterion, proven hardware-free.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ringlink_server.leg import LegCooker, LegTracker

TRACE = Path(__file__).parent.parent / "traces" / "left-pad.jsonl"


def _segments() -> dict[str, list[dict]]:
    lines = [json.loads(l) for l in TRACE.read_text(encoding="utf-8").splitlines() if l.strip()]
    byseg: dict[str, list[dict]] = defaultdict(list)
    for f in lines[1:]:
        byseg[f["seg"]].append(f)
    return byseg


SEG = _segments()
ORDER = ["rest", "squat", "run", "sprint"]


def _fresh_cooker() -> LegCooker:
    ck = LegCooker()
    ck.snap_rest([f["accel"] for f in SEG["rest"]])
    return ck


def _replay(ck: LegCooker):
    """Replay all segments in capture order; yield (segment, state, events)."""
    for name in ORDER:
        for f in SEG[name]:
            state, events = ck.update(f["accel"], f["gyro"])
            yield name, state, events


def test_gait_squat_invariant_holds():
    ck = _fresh_cooker()
    for _name, state, _ev in _replay(ck):
        if state is None:
            continue
        if state["squatting"]:
            assert state["gait"] == "rest", "gait must be 'rest' while squatting (G1)"
        # 'squat' is never a gait value.
        assert state["gait"] in ("rest", "run", "sprint")


def test_squat_reps_counted_and_monotonic():
    ck = _fresh_cooker()
    last = 0
    rep_events = 0
    for _name, state, events in _replay(ck):
        if state is None:
            continue
        assert state["squat_reps"] >= last  # never decreases (survives recalibration)
        last = state["squat_reps"]
        rep_events += sum(1 for e in events if e["type"] == "squat_rep")
    assert last >= 3                      # several full squats were performed
    assert rep_events == last             # one squat_rep event per counted rep


def test_gait_classifies_dominantly_per_segment():
    ck = _fresh_cooker()
    gait_by_seg: dict[str, Counter] = defaultdict(Counter)
    for name, state, _ev in _replay(ck):
        if state is not None:
            gait_by_seg[name][state["gait"]] += 1
    assert gait_by_seg["rest"].most_common(1)[0][0] == "rest"
    assert gait_by_seg["run"].most_common(1)[0][0] == "run"      # not 'sprint' (retuned)
    assert gait_by_seg["sprint"].most_common(1)[0][0] == "sprint"


def test_squatting_latches_only_during_squat_segment():
    ck = _fresh_cooker()
    squat_frames = Counter()
    for name, state, _ev in _replay(ck):
        if state is not None and state["squatting"]:
            squat_frames[name] += 1
    assert squat_frames["squat"] > 0
    assert squat_frames["rest"] == 0


def test_gait_change_events_fire_on_transitions():
    ck = _fresh_cooker()
    changes = 0
    prev = "rest"
    for _name, state, events in _replay(ck):
        if state is None:
            continue
        for e in events:
            if e["type"] == "gait_change":
                assert e["from"] == prev and e["to"] == state["gait"]
                prev = e["to"]
                changes += 1
    assert changes > 0  # rest->run->sprint etc. produced transitions


def test_nullable_holds_last():
    ck = _fresh_cooker()
    state, events = ck.update(None, (0, 0, 0))
    assert state is None and events == []


def test_threshold_retune_changes_classification():
    # The lifted defaults (700/2200) misclassified this jog as sprint; the widened
    # defaults fix it. Prove the knob is real: a low sprint_on re-breaks it.
    hot = LegCooker(LegTracker(run_on=300, sprint_on=900))
    hot.snap_rest([f["accel"] for f in SEG["rest"]])
    gaits = Counter()
    for name in ORDER:
        for f in SEG[name]:
            state, _ = hot.update(f["accel"], f["gyro"])
            if state is not None and name == "run":
                gaits[state["gait"]] += 1
    assert gaits["sprint"] > gaits["run"]  # too-low thresholds -> jog reads as sprint
