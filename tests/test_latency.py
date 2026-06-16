"""Phase 1 latency harness tests — a kept perf check (plan §Phase 1).

Two layers:
  * Deterministic unit tests on `summarize()`'s GO/NO-GO verdict bands — fully
    CI-stable (no timing, no sockets).
  * A live smoke run of the real closed-loop measurement with a *generous*
    regression guard on the median. The guard is loose enough not to flake on a
    loaded CI runner, but a real regression (e.g. TCP_NODELAY removed -> ~40 ms
    Nagle stalls) trips it. The actual GO/NO-GO numbers come from
    `python -m ringlink_server latency`, not from a hard CI threshold.
"""

from ringlink_server.latency import run, summarize


def test_summarize_green():
    r = summarize([1.0] * 100, warmup=0)
    assert r.verdict == "GREEN"
    assert r.n == 100


def test_summarize_acceptable():
    # p95 in the 5..10 ms HID-dominated band.
    r = summarize([8.0] * 100, warmup=0)
    assert r.verdict == "ACCEPTABLE"


def test_summarize_caution():
    # p95 in the 10..12 ms band: approaching the HID floor -> re-baseline.
    r = summarize([11.0] * 100, warmup=0)
    assert r.verdict == "CAUTION"


def test_summarize_red_high_p95():
    r = summarize([15.0] * 100, warmup=0)
    assert r.verdict == "RED"


def test_summarize_red_high_variance():
    # Low median, fat tail -> the variance guard must fire even though p95 is low.
    r = summarize([1.0] * 95 + [60.0] * 5, warmup=0)
    assert r.verdict == "RED"
    assert "variance" in r.note


def test_measure_latency_smoke():
    # Real server <-> client over loopback. Small n keeps CI fast.
    r = run(n=300, warmup=50)
    assert r.n == 300
    assert r.verdict in {"GREEN", "ACCEPTABLE", "CAUTION", "RED"}
    assert r.p50_ms >= 0.0
    # Regression guard: loopback median must stay well under the HID floor. A
    # Nagle regression would push this toward ~40 ms and trip the test.
    assert r.p50_ms < 20.0
