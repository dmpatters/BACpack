"""trend_range: samples over a time range, thinned for a chart, with the gaps
of an interval trend."""
from __future__ import annotations

import datetime as dt

from bacpack.models import BacnetObject, Device, TrendSample
from bacpack.trend_range import Sample, fetch_range, find_gaps, is_step, thin_analog, thin_step

T0 = dt.datetime(2026, 1, 15, 9, 0, 0)


def at(seconds):
    return T0 + dt.timedelta(seconds=seconds)


def s(seconds, value):
    return Sample(at(seconds), value)


def test_is_step():
    assert is_step("binary-value", None) and is_step("multi-state-input", None) and is_step("calendar", None)
    assert is_step("schedule", "binary") and is_step("schedule", "multi-state")
    assert not is_step("schedule", "analog") and not is_step("analog-value", None)


def test_thin_analog_keeps_each_buckets_min_and_max_in_time_order():
    samples = [s(i, 50.0) for i in range(100)]
    samples[37] = s(37, 99.0)  # a spike an average would hide
    samples[38] = s(38, 1.0)

    out = thin_analog(samples, at(0), at(100), max_points=10)

    assert len(out) <= 10
    assert s(37, 99.0) in out and s(38, 1.0) in out
    assert [p.t for p in out] == sorted(p.t for p in out)


def test_thin_analog_with_identical_timestamps_returns_no_duplicate_rows():
    samples = [s(0, 1.0), s(0, 1.0), s(0, 2.0)] + [s(i, 5.0) for i in range(1, 50)]

    out = thin_analog(samples, at(0), at(50), max_points=10)

    assert len(out) == len({id(p) for p in out})
    assert len(out) <= 10


def test_thin_analog_leaves_a_short_range_alone():
    samples = [s(i, float(i)) for i in range(5)]
    assert thin_analog(samples, at(0), at(5), max_points=10) == samples


def test_thin_step_drops_repeats_first():
    samples = [s(i, 1.0 if i < 50 else 2.0) for i in range(100)]

    out = thin_step(samples, at(0), at(100), max_points=10)

    assert out == [s(0, 1.0), s(50, 2.0)]


def test_thin_step_falls_back_to_first_and_last_per_bucket():
    samples = [s(i, float(i % 2)) for i in range(100)]  # toggles every sample

    out = thin_step(samples, at(0), at(100), max_points=10)

    assert len(out) <= 10
    assert out[0] == s(0, 0.0) and out[-1] == s(99, 1.0)


def test_gaps_between_samples_and_up_to_the_end():
    samples = [s(0, 1), s(5, 1), s(10, 1), s(60, 1), s(65, 1)]

    gaps = find_gaps(samples, at(0), at(100), interval_seconds=5, earliest=at(-1000))

    assert gaps == [(at(10), at(60)), (at(65), at(100))]


def test_a_leading_gap_only_after_the_earliest_sample():
    samples = [s(50, 1), s(55, 1)]

    assert find_gaps(samples, at(0), at(56), 5, earliest=at(-10)) == [(at(0), at(50))]
    # History starts at the first sample: retention or the cap trimmed it, not a gap.
    assert find_gaps(samples, at(0), at(56), 5, earliest=at(50)) == []


def test_an_empty_range_inside_history_is_one_gap_and_without_history_none():
    assert find_gaps([], at(0), at(100), 5, earliest=at(-10)) == [(at(0), at(100))]
    assert find_gaps([], at(0), at(100), 5, earliest=None) == []
    assert find_gaps([], at(0), at(100), 5, earliest=at(200)) == []


def _object_with_samples(session_factory, values, **fields):
    with session_factory() as session:
        device = Device(id=1, name="D")
        obj = BacnetObject(device=device, object_type="analog-value", instance_number=1, name="AV", trended=True, **fields)
        session.add_all([device, obj])
        session.flush()
        for seq, (seconds, value) in enumerate(values, start=1):
            session.add(TrendSample(object_id=obj.id, sequence=seq, timestamp=at(seconds), value=value))
        session.commit()
        return obj.id


def test_fetch_range_shape_carry_in_and_earliest(session_factory):
    object_id = _object_with_samples(session_factory, [(-30, 1.0), (-5, 2.0), (0, 3.0), (5, 4.0)])

    out = fetch_range(
        session_factory, object_id=object_id, mode="interval", interval_seconds=5, step=False,
        start=at(-1), end=at(6), max_points=1000,
    )

    assert out["samples"] == [
        {"timestamp": "2026-01-15T09:00:00+00:00", "value": 3.0},
        {"timestamp": "2026-01-15T09:00:05+00:00", "value": 4.0},
    ]
    assert out["carry_in"] == {"timestamp": "2026-01-15T08:59:55+00:00", "value": 2.0}
    assert out["earliest_sample"] == "2026-01-15T08:59:30+00:00"
    assert out["count_in_range"] == 2 and out["thinned"] is False
    assert out["mode"] == "interval" and out["interval_seconds"] == 5 and out["step"] is False
    assert out["gaps"] == []


def test_fetch_range_cov_reports_no_gaps(session_factory):
    object_id = _object_with_samples(session_factory, [(0, 1.0), (500, 2.0)])

    out = fetch_range(
        session_factory, object_id=object_id, mode="cov", interval_seconds=None, step=False,
        start=at(0), end=at(1000), max_points=1000,
    )

    assert out["gaps"] == [] and out["interval_seconds"] is None


def test_fetch_range_not_trended_is_empty(session_factory):
    object_id = _object_with_samples(session_factory, [(0, 1.0)])

    out = fetch_range(
        session_factory, object_id=object_id, mode=None, interval_seconds=None, step=False,
        start=at(-10), end=at(10), max_points=1000,
    )

    assert out == {
        "mode": None, "interval_seconds": None, "step": False, "samples": [], "carry_in": None,
        "gaps": [], "thinned": False, "count_in_range": 0, "earliest_sample": None,
    }


def test_fetch_range_thins_and_says_so(session_factory):
    object_id = _object_with_samples(session_factory, [(i, float(i % 7)) for i in range(200)])

    out = fetch_range(
        session_factory, object_id=object_id, mode="interval", interval_seconds=1, step=False,
        start=at(0), end=at(200), max_points=20,
    )

    assert out["thinned"] is True and out["count_in_range"] == 200 and len(out["samples"]) <= 20


def test_fetch_range_skips_time_change_records(session_factory):
    object_id = _object_with_samples(session_factory, [(0, 1.0)])
    with session_factory() as session:
        session.add(TrendSample(object_id=object_id, sequence=2, timestamp=at(1), value=3600.0, kind="time-change"))
        session.commit()

    out = fetch_range(
        session_factory, object_id=object_id, mode="interval", interval_seconds=60, step=False,
        start=at(-10), end=at(10), max_points=1000,
    )

    assert [p["value"] for p in out["samples"]] == [1.0]


def test_fetch_range_keeps_the_latest_sample_of_a_thinned_step_trend(session_factory):
    # Unchanged since sample 10: change points alone would end the line there.
    values = [(i, 0.0 if i < 10 else 1.0) for i in range(100)]
    object_id = _object_with_samples(session_factory, values)

    out = fetch_range(
        session_factory, object_id=object_id, mode="interval", interval_seconds=1, step=True,
        start=at(0), end=at(100), max_points=10,
    )

    assert out["samples"][-1] == {"timestamp": "2026-01-15T09:01:39+00:00", "value": 1.0}


def test_fetch_range_keeps_the_samples_either_side_of_a_gap(session_factory):
    values = [(i, 1.0) for i in range(50)] + [(i, 1.0) for i in range(80, 130)]
    object_id = _object_with_samples(session_factory, values)

    out = fetch_range(
        session_factory, object_id=object_id, mode="interval", interval_seconds=1, step=True,
        start=at(0), end=at(130), max_points=10,
    )

    stamps = [p["timestamp"] for p in out["samples"]]
    assert out["gaps"] == [{"start": "2026-01-15T09:00:49+00:00", "end": "2026-01-15T09:01:20+00:00"}]
    assert "2026-01-15T09:00:49+00:00" in stamps and "2026-01-15T09:01:20+00:00" in stamps


def test_fetch_range_keeps_the_latest_sample_of_a_thinned_analog_trend(session_factory):
    # The last sample is neither its bucket's minimum nor its maximum.
    values = [(i, float(i % 3)) for i in range(200)] + [(200, 1.0)]
    object_id = _object_with_samples(session_factory, values)

    out = fetch_range(
        session_factory, object_id=object_id, mode="interval", interval_seconds=1, step=False,
        start=at(0), end=at(201), max_points=20,
    )

    assert out["samples"][-1] == {"timestamp": "2026-01-15T09:03:20+00:00", "value": 1.0}
