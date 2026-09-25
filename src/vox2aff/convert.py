"""Brute-force mapping from a Sound Voltex chart onto an Arcaea chart.

Ground notes are the four BT buttons, one Arcaea lane each. FX chips become sky taps and FX holds become traces at half height, both
on Arcade Plus arc color 2 (green), so they sit below the blue and red lasers.
Laser nodes are straight segments. Before that, a density setting simplifies
each laser polyline by horizontal error; a slam stays pinned. Strength 0 keeps
every point. A slam becomes a short arc and the following piece starts when
the slam ends.
"""

from __future__ import annotations

from vox2aff.aff import AffChart, Arc, ArcTap, Hold, Scenecontrol, Tap, Timing
from vox2aff.vox import LaserPoint, Timeline, VoxChart, build_timeline

BT_LANE = {3: 1, 4: 2, 5: 3, 6: 4}
# 6K lanes in the file are 0–5 from left to right. BT 1–4 land on the 1st, 3rd,
# 4th and 6th lanes; the two FX buttons take the 2nd and 5th.
BT_LANE_6K = {3: 0, 4: 2, 5: 3, 6: 5}
FX_LANE_6K = {2: 1, 7: 4}
FX_TRACK = {2: 0, 7: 1}
LASER_TRACK = {1: 0, 8: 1}
FX_X = {0: 0.16, 1: 0.84}
FX_COLOR = 2
FX_Y = 0.5
# Slider 100 allows this much horizontal error, in Arcaea x units (about 0–1).
LASER_EPSILON_MAX = 0.10
DEFAULT_LASER_STRENGTH = 20
STRAIGHT_LASER_MODES = ("keep", "noinput", "black")
DEFAULT_STRAIGHT_LASER = "noinput"


def straight_laser_mode(value: str) -> str:
    text = str(value).strip().lower()
    if text in STRAIGHT_LASER_MODES:
        return text
    return DEFAULT_STRAIGHT_LASER


def laser_epsilon(strength: int) -> float:
    strength = min(100, max(0, int(strength)))
    if strength <= 0:
        return 0.0
    return LASER_EPSILON_MAX * strength / 100


def _clamp_x(value: float) -> float:
    return min(1.5, max(-0.5, value))


def _laser_x(position: float, wide: bool, six_key: bool = False) -> float:
    # A 2x laser stores the knob in 0–1, but the beam is twice as wide,
    # so the same reading covers -0.5 to 1.5.
    if wide:
        position = position * 2 - 0.5
    if not six_key:
        return _clamp_x(position)
    # Six lanes are 1.5× the four-lane width, one lane past each side.
    position = 0.5 + (position - 0.5) * 1.5
    return min(2.0, max(-1.0, position))


def _positive_end(start: int, end: int) -> int:
    return end if end > start else start + 1


def convert_chart(
    chart: VoxChart,
    laser_epsilon: float = 0.0,
    straight_laser: str = DEFAULT_STRAIGHT_LASER,
    six_key: bool = False,
) -> AffChart:
    timeline = build_timeline(chart)
    aff = AffChart()
    if six_key:
        # Timing is before 0 so Arcade Plus already treats the chart as widened
        # at time 0. The enable flag must be an int; a float is ignored.
        aff.scenecontrols.extend(
            (
                Scenecontrol(-1, "enwidenlanes", (0.0, 1)),
                Scenecontrol(-1, "enwidencamera", (0.0, 1)),
            )
        )
    _write_timing(aff, timeline, chart)
    _write_buttons(aff, chart, timeline, six_key)
    _write_lasers(aff, chart, timeline, laser_epsilon, straight_laser_mode(straight_laser), six_key)
    _write_fx(aff, chart, timeline, six_key)
    return aff


def _write_timing(aff: AffChart, timeline: Timeline, chart: VoxChart) -> None:
    points: dict[int, tuple[float, float]] = {}
    for tick, bpm in timeline.bpm_at:
        points[tick] = (bpm, float(timeline.meter_on(tick)))
    for tick, meter in timeline.meter_at:
        bpm, _meter = points.get(tick, (timeline.bpm_on(tick), float(meter)))
        points[tick] = (bpm, float(meter))
    if 0 not in points and timeline.bpm_at:
        points[0] = (timeline.bpm_at[0][1], float(timeline.meter_on(0)))
    for tick in sorted(points):
        bpm, beats = points[tick]
        # A trailing '-' on the VOX beat number freezes the highway until the
        # next tempo event. Note times stay on the audio clock; only the scroll stops.
        if tick in timeline.stopped_ticks:
            bpm = 0.0
        aff.timings.append(Timing(timeline.ms(tick), bpm, beats))


def _write_buttons(aff: AffChart, chart: VoxChart, timeline: Timeline, six_key: bool = False) -> None:
    lanes = BT_LANE_6K if six_key else BT_LANE
    for note in chart.buttons:
        lane = lanes.get(note.track)
        if lane is None:
            continue
        start = timeline.ms(timeline.tick(note.measure, note.beat, note.cell))
        if note.length <= 0:
            aff.taps.append(Tap(start, lane))
            continue
        end_tick = timeline.tick(note.measure, note.beat, note.cell) + note.length
        end = timeline.ms(end_tick)
        aff.holds.append(Hold(start, _positive_end(start, end), lane))


def _write_lasers(
    aff: AffChart,
    chart: VoxChart,
    timeline: Timeline,
    laser_epsilon: float = 0.0,
    straight_laser: str = DEFAULT_STRAIGHT_LASER,
    six_key: bool = False,
) -> list[Arc]:
    by_track: dict[int, list[LaserPoint]] = {1: [], 8: []}
    for point in chart.lasers:
        if point.track in by_track:
            by_track[point.track].append(point)
    written: list[Arc] = []
    for track, points in by_track.items():
        color = LASER_TRACK[track]
        segments = _segments(points)
        for index, segment in enumerate(segments):
            next_start = None
            if index + 1 < len(segments):
                nxt = segments[index + 1][0]
                next_start = timeline.tick(nxt.measure, nxt.beat, nxt.cell)
            written.extend(
                _segment_to_arcs(
                    segment, color, timeline, aff, next_start, laser_epsilon, straight_laser, six_key
                )
            )
    return written


def _segments(points: list[LaserPoint]) -> list[list[LaserPoint]]:
    segments: list[list[LaserPoint]] = []
    current: list[LaserPoint] = []
    for point in points:
        if point.point == 1 and current:
            current = []
        if point.point == 1 or current:
            current.append(point)
        if point.point == 2 and current:
            segments.append(current)
            current = []
    return segments


def _simplify_laser(timed: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Drop laser samples whose horizontal error stays within ``epsilon``.

    Points that share a timestamp are slams and stay in place. The error is the
    distance in x from the chord through the surrounding kept points, so a
    straight run collapses to one arc and a real bend remains.
    """
    if epsilon <= 0 or len(timed) < 3:
        return timed
    pins = _laser_pins(timed)
    kept: list[tuple[float, float]] = []
    for left, right in zip(pins, pins[1:]):
        piece = _rdp(timed[left : right + 1], epsilon)
        if kept:
            piece = piece[1:]
        kept.extend(piece)
    return kept


def _laser_pins(timed: list[tuple[float, float]]) -> list[int]:
    last = len(timed) - 1
    pins = {0, last}
    for index in range(1, len(timed)):
        if timed[index][0] <= timed[index - 1][0]:
            pins.add(index - 1)
            pins.add(index)
    return sorted(pins)


def _rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    count = len(points)
    if count < 3:
        return list(points)
    keep = [False] * count
    keep[0] = keep[-1] = True
    stack = [(0, count - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        t0, x0 = points[start]
        t1, x1 = points[end]
        span = t1 - t0
        max_dev = 0.0
        split = start
        for index in range(start + 1, end):
            tick, position = points[index]
            if span == 0:
                deviation = abs(position - x0)
            else:
                interpolated = x0 + (x1 - x0) * ((tick - t0) / span)
                deviation = abs(position - interpolated)
            if deviation > max_dev:
                max_dev = deviation
                split = index
        if max_dev > epsilon and split != start:
            keep[split] = True
            stack.append((start, split))
            stack.append((split, end))
    return [point for point, flagged in zip(points, keep) if flagged]


def _segment_to_arcs(
    segment: list[LaserPoint],
    color: int,
    timeline: Timeline,
    aff: AffChart,
    next_start: float | None = None,
    laser_epsilon: float = 0.0,
    straight_laser: str = DEFAULT_STRAIGHT_LASER,
    six_key: bool = False,
) -> list[Arc]:
    wide = segment[0].wide
    timed = _simplify_laser(
        [
            (
                timeline.tick(point.measure, point.beat, point.cell),
                _laser_x(point.position, wide, six_key),
            )
            for point in segment
        ],
        laser_epsilon,
    )
    arcs: list[Arc] = []
    # After a slam, the next piece starts when the slam ends, so the two
    # never occupy the same time.
    carry: float | None = None
    for index, (tick, position) in enumerate(timed[:-1]):
        next_tick, next_position = timed[index + 1]
        start_tick = tick if carry is None else max(tick, carry)
        slam = next_tick <= tick
        if slam:
            later = next(
                (item[0] for item in timed[index + 1 :] if item[0] > tick),
                None,
            )
            if later is None:
                if next_start is not None and next_start > start_tick:
                    later = next_start
                else:
                    later = tick + timeline.resolution
            span = max(1, min(timeline.resolution // 4, max(1, (later - start_tick) // 2)))
            end_tick = start_tick + span
            if next_start is not None and end_tick > next_start > start_tick:
                end_tick = next_start
            carry = end_tick
        else:
            end_tick = next_tick
            carry = None
        if end_tick <= start_tick:
            continue
        start = timeline.ms(start_tick)
        end = _positive_end(start, timeline.ms(end_tick))
        arc = Arc(start, end, position, next_position, color=color)
        if _is_straight_laser(position, next_position):
            if straight_laser == "noinput":
                arc.noinput = True
            elif straight_laser == "black":
                arc.is_void = True
        _split_straight_joint(arcs, arc)
        aff.arcs.append(arc)
        arcs.append(arc)
    return arcs


def _split_straight_joint(previous: list[Arc], arc: Arc) -> None:
    """Keep a straight laser from joining the curve before or after it.

    Arcade draws and judges arcs that meet at the same time as one laser, so a
    black line glued to a curved arc is shown and played as part of that arc.
    """
    if not previous:
        return
    prior = previous[-1]
    if prior.is_void == arc.is_void and prior.noinput == arc.noinput:
        return
    if arc.time > prior.end:
        return
    if arc.end - arc.time > prior.end - prior.time:
        arc.time = prior.end + 1
        if arc.time >= arc.end:
            arc.time = arc.end - 1
            prior.end = max(prior.time + 1, arc.time - 1)
    else:
        prior.end = arc.time - 1
        if prior.end <= prior.time:
            prior.end = prior.time + 1
            arc.time = min(arc.end - 1, prior.end + 1)


def _is_straight_laser(x_start: float, x_end: float) -> bool:
    # The file writes x to two decimals. A 6K stretch turns one SDVX knob step
    # into 0.01, which still does not need to be held.
    return abs(round(x_start, 2) - round(x_end, 2)) <= 0.025


def _write_fx(aff: AffChart, chart: VoxChart, timeline: Timeline, six_key: bool = False) -> None:
    if six_key:
        _write_fx_lanes(aff, chart, timeline)
        return
    chips: list[tuple[int, float]] = []
    holds: list[tuple[int, int, float]] = []
    for note in chart.buttons:
        side = FX_TRACK.get(note.track)
        if side is None:
            continue
        start_tick = timeline.tick(note.measure, note.beat, note.cell)
        start = timeline.ms(start_tick)
        x = FX_X[side]
        if note.length <= 0:
            chips.append((start, x))
            continue
        end = _positive_end(start, timeline.ms(start_tick + note.length))
        holds.append((start, end, x))
    for start, end, x in _merge_touching(holds):
        aff.arcs.append(Arc(start, end, x, x, y_start=FX_Y, y_end=FX_Y, color=FX_COLOR, is_void=False))
    for start, x in chips:
        host = Arc(start, start + 1, x, x, color=FX_COLOR, is_void=True)
        host.arc_taps.append(ArcTap(start))
        aff.arcs.append(host)


def _write_fx_lanes(aff: AffChart, chart: VoxChart, timeline: Timeline) -> None:
    taps: list[tuple[int, int]] = []
    holds: list[tuple[int, int, int]] = []
    for note in chart.buttons:
        lane = FX_LANE_6K.get(note.track)
        if lane is None:
            continue
        start = timeline.ms(timeline.tick(note.measure, note.beat, note.cell))
        if note.length <= 0:
            taps.append((start, lane))
            continue
        end_tick = timeline.tick(note.measure, note.beat, note.cell) + note.length
        end = timeline.ms(end_tick)
        holds.append((start, _positive_end(start, end), lane))
    for start, lane in taps:
        aff.taps.append(Tap(start, lane))
    for start, end, lane in _merge_touching(holds):
        aff.holds.append(Hold(start, end, lane))


def _merge_touching(spans: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Join FX holds on the same lane that meet, so Arcade does not draw a new head.

    Other lanes can sit between two pieces of the same hold in time order, so the
    match has to be the previous span of this lane, not the previous span overall.
    """
    merged: list[tuple[int, int, float]] = []
    latest: dict[float, int] = {}
    for start, end, key in sorted(spans):
        index = latest.get(key)
        if index is not None and start <= merged[index][1]:
            prev_start, prev_end, _key = merged[index]
            merged[index] = (prev_start, max(prev_end, end), key)
            continue
        latest[key] = len(merged)
        merged.append((start, end, key))
    return merged
