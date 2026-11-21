"""Trend samples over a time range, thinned for a chart, with the gaps of an
interval trend. Parent
side only: it reads what TrendSampler wrote."""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func

from . import timeutil
from .models import TrendSample

# Neighbouring interval samples further apart than this many intervals are
# a gap (a stopped device). The current interval is used for all history.
GAP_FACTOR = 2.5


@dataclass(frozen=True)
class Sample:
    t: dt.datetime  # naive UTC, the stored form
    value: float


def is_step(object_type: str, schedule_value_type: Optional[str]) -> bool:
    if object_type.startswith(("binary-", "multi-state-")) or object_type == "calendar":
        return True
    return object_type == "schedule" and schedule_value_type in ("binary", "multi-state")


def _bucket(t: dt.datetime, start: dt.datetime, width: float, buckets: int) -> int:
    return min(max(int((t - start).total_seconds() / width), 0), buckets - 1)


def _grouped(samples: List[Sample], start, end, buckets: int) -> Dict[int, List[Tuple[int, Sample]]]:
    width = max((end - start).total_seconds() / buckets, 1e-9)
    grouped: Dict[int, List[Tuple[int, Sample]]] = {}
    for i, sample in enumerate(samples):
        grouped.setdefault(_bucket(sample.t, start, width, buckets), []).append((i, sample))
    return grouped


def thin_analog_positions(samples: List[Sample], start, end, max_points: int) -> List[int]:
    """Each bucket's minimum and maximum: spikes survive, an average hides them.
    Positions, so two equal samples at one timestamp stay distinct rows and
    none is returned twice."""
    if len(samples) <= max_points:
        return list(range(len(samples)))
    kept = set()
    for group in _grouped(samples, start, end, max(max_points // 2, 1)).values():
        kept.add(min(group, key=lambda item: item[1].value)[0])
        kept.add(max(group, key=lambda item: item[1].value)[0])
    return sorted(kept)


def thin_step_positions(samples: List[Sample], start, end, max_points: int) -> List[int]:
    """A step line needs only the changes; if those are still too many, each
    bucket's first and last (fast changes inside a bucket are hidden)."""
    if len(samples) <= max_points:
        return list(range(len(samples)))
    changes = [i for i, p in enumerate(samples) if i == 0 or p.value != samples[i - 1].value]
    if len(changes) <= max_points:
        return changes
    kept = set()
    change_samples = [samples[i] for i in changes]
    for group in _grouped(change_samples, start, end, max(max_points // 2, 1)).values():
        kept.update((changes[group[0][0]], changes[group[-1][0]]))
    return sorted(kept)


def thin_analog(samples: List[Sample], start, end, max_points: int) -> List[Sample]:
    return [samples[i] for i in thin_analog_positions(samples, start, end, max_points)]


def thin_step(samples: List[Sample], start, end, max_points: int) -> List[Sample]:
    return [samples[i] for i in thin_step_positions(samples, start, end, max_points)]


def find_gaps(
    samples: List[Sample], start, end, interval_seconds: float, earliest: Optional[dt.datetime]
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Stretches with no samples while the trend should have recorded some.
    Nothing before the earliest kept sample counts: retention or the sample
    cap removed that history, which the chart says separately."""
    if earliest is None or earliest > end:
        return []
    threshold = dt.timedelta(seconds=GAP_FACTOR * interval_seconds)
    if not samples:
        return [(start, end)] if earliest < start and end - start > threshold else []
    gaps = []
    if start > earliest and samples[0].t - start > threshold:
        gaps.append((start, samples[0].t))
    for before, after in itertools.pairwise(samples):
        if after.t - before.t > threshold:
            gaps.append((before.t, after.t))
    if end - samples[-1].t > threshold:
        gaps.append((samples[-1].t, end))
    return gaps


def _point(sample: Optional[Sample]) -> Optional[dict]:
    return None if sample is None else {"timestamp": timeutil.iso_utc(sample.t), "value": sample.value}


def fetch_range(
    session_factory,
    *,
    object_id: int,
    mode: Optional[str],
    interval_seconds: Optional[int],
    step: bool,
    start: dt.datetime,
    end: dt.datetime,
    max_points: int,
) -> dict:
    """`start`/`end` naive UTC. `mode` None means the object isn't trended."""
    out = {
        "mode": mode,
        "interval_seconds": interval_seconds if mode == "interval" else None,
        "step": step,
        "samples": [],
        "carry_in": None,
        "gaps": [],
        "thinned": False,
        "count_in_range": 0,
        "earliest_sample": None,
    }
    if mode is None:
        return out
    # A range ending in the future would otherwise show a gap from the last
    # sample up to a time that hasn't happened.
    end = min(end, timeutil.db_utc_now())
    values = (TrendSample.object_id == object_id, TrendSample.kind == "value")
    with session_factory() as session:
        rows = (
            session.query(TrendSample.timestamp, TrendSample.value)
            .filter(*values, TrendSample.timestamp >= start, TrendSample.timestamp <= end)
            .order_by(TrendSample.timestamp, TrendSample.id)
            .all()
        )
        before = (
            session.query(TrendSample.timestamp, TrendSample.value)
            .filter(*values, TrendSample.timestamp < start)
            .order_by(TrendSample.timestamp.desc(), TrendSample.id.desc())
            .first()
        )
        earliest = session.query(func.min(TrendSample.timestamp)).filter(*values).scalar()

    samples = [Sample(t, value) for t, value in rows]
    gaps = find_gaps(samples, start, end, interval_seconds, earliest) if mode == "interval" else []
    # Room for the anchors below, so the answer stays within max_points.
    budget = max(max_points - 2 - 2 * len(gaps), 2)
    positions = set((thin_step_positions if step else thin_analog_positions)(samples, start, end, budget))
    if samples:
        # Always the latest sample (the chart labels it, and a line ending at
        # the last change would look like a stopped recorder) and the samples
        # either side of each gap, so the line holds right up to it.
        positions.update((0, len(samples) - 1))
        by_time = {s.t: i for i, s in reversed(list(enumerate(samples)))}
        last_at = {s.t: i for i, s in enumerate(samples)}
        for gap_start, gap_end in gaps:
            if gap_start in last_at:
                positions.add(last_at[gap_start])
            if gap_end in by_time:
                positions.add(by_time[gap_end])
    thinned = [samples[i] for i in sorted(positions)]
    out.update(
        samples=[_point(p) for p in thinned],
        carry_in=_point(Sample(*before)) if before else None,
        gaps=[{"start": timeutil.iso_utc(a), "end": timeutil.iso_utc(b)} for a, b in gaps],
        thinned=len(thinned) < len(samples),
        count_in_range=len(samples),
        earliest_sample=timeutil.iso_utc(earliest) if earliest is not None else None,
    )
    return out
