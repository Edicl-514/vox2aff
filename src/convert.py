"""Brute-force mapping from a Sound Voltex chart onto an Arcaea chart.

Ground notes are the four BT buttons, one Arcaea lane each. FX chips become sky taps and FX holds become traces at half height, both
on Arcade Plus arc color 2 (green), so they sit below the blue and red lasers.
Laser nodes are straight segments; a slam becomes a short arc and the
following piece starts when the slam ends.
"""

from __future__ import annotations

from vox2aff.aff import AffChart, Arc, ArcTap, Hold, Tap, Timing
from vox2aff.vox import LaserPoint, Timeline, VoxChart, build_timeline

BT_LANE = {3: 1, 4: 2, 5: 3, 6: 4}
FX_TRACK = {2: 0, 7: 1}
LASER_TRACK = {1: 0, 8: 1}
FX_X = {0: 0.16, 1: 0.84}
FX_COLOR = 2
FX_Y = 0.5


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _positive_end(start: int, end: int) -> int:
    return end if end > start else start + 1


def convert_chart(chart: VoxChart) -> AffChart:
    timeline = build_timeline(chart)
    aff = AffChart()
    _write_timing(aff, timeline, chart)
    _write_buttons(aff, chart, timeline)
    _write_lasers(aff, chart, timeline)
    _write_fx(aff, chart, timeline)
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
        aff.timings.append(Timing(timeline.ms(tick), bpm, beats))


def _write_buttons(aff: AffChart, chart: VoxChart, timeline: Timeline) -> None:
    for note in chart.buttons:
        lane = BT_LANE.get(note.track)
        if lane is None:
            continue
        start = timeline.ms(timeline.tick(note.measure, note.beat, note.cell))
        if note.length <= 0:
            aff.taps.append(Tap(start, lane))
            continue
        end_tick = timeline.tick(note.measure, note.beat, note.cell) + note.length
        end = timeline.ms(end_tick)
        aff.holds.append(Hold(start, _positive_end(start, end), lane))


def _write_lasers(aff: AffChart, chart: VoxChart, timeline: Timeline) -> list[Arc]:
    by_track: dict[int, list[LaserPoint]] = {1: [], 8: []}
    for point in chart.lasers:
        if point.track in by_track:
            by_track[point.track].append(point)
    written: list[Arc] = []
    for track, points in by_track.items():
        color = LASER_TRACK[track]
        for segment in _segments(points):
            written.extend(_segment_to_arcs(segment, color, timeline, aff))
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


def _segment_to_arcs(
    segment: list[LaserPoint], color: int, timeline: Timeline, aff: AffChart
) -> list[Arc]:
    timed = [
        (timeline.tick(point.measure, point.beat, point.cell), _clamp01(point.position))
        for point in segment
    ]
    arcs: list[Arc] = []
    # After a slam, the next piece starts when the slam ends, so the two
    # never occupy the same time.
    carry: int | None = None
    for index, (tick, position) in enumerate(timed[:-1]):
        next_tick, next_position = timed[index + 1]
        start_tick = tick if carry is None else max(tick, carry)
        slam = next_tick <= tick
        if slam:
            later = next(
                (item[0] for item in timed[index + 1 :] if item[0] > tick),
                tick + timeline.resolution,
            )
            span = max(1, min(timeline.resolution // 4, max(1, (later - start_tick) // 2)))
            end_tick = start_tick + span
            carry = end_tick
        else:
            end_tick = next_tick
            carry = None
        if end_tick <= start_tick:
            continue
        start = timeline.ms(start_tick)
        end = _positive_end(start, timeline.ms(end_tick))
        arc = Arc(start, end, position, next_position, color=color)
        aff.arcs.append(arc)
        arcs.append(arc)
    return arcs


def _write_fx(aff: AffChart, chart: VoxChart, timeline: Timeline) -> None:
    chips: list[tuple[int, float]] = []
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
        aff.arcs.append(Arc(start, end, x, x, y_start=FX_Y, y_end=FX_Y, color=FX_COLOR, is_void=False))
    for start, x in chips:
        host = Arc(start, start + 1, x, x, color=FX_COLOR, is_void=True)
        host.arc_taps.append(ArcTap(start))
        aff.arcs.append(host)
