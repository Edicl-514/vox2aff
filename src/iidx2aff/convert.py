"""Map one IIDX single-player chart onto Arcaea.

Keys 1, 3, 5 and 7 are the four ground lanes. Keys 2, 4 and 6 are sky notes
on the lane boundaries. The scratch is a green arc on the outer edge of lane 1
for 1P, or lane 4 for 2P. Chips stay at the top, holds sit at half height.
Tick values are already milliseconds.
"""

from __future__ import annotations

from vox2aff.aff import AffChart, Arc, ArcTap, Hold, Tap, Timing

from iidx2aff.iidx import IidxChart, Meter, Tempo

WHITE_LANE = {0: 1, 2: 2, 4: 3, 6: 4}
# Arcade places 4K lanes on the inner four of six lane slots. Those centers are
# arc x -0.25, 0.25, 0.75 and 1.25, so the gaps between them are 0, 0.5 and 1.
BLACK_X = {1: 0.00, 3: 0.50, 5: 1.00}
SCRATCH = 7
# Left edge of lane 1, and the right edge of lane 4.
SCRATCH_X = {"1p": -0.50, "2p": 1.50}
SCRATCH_COLOR = 2
HOLD_Y = 0.5


def convert_chart(chart: IidxChart, side: str = "1p") -> AffChart:
    aff = AffChart()
    _write_timing(aff, chart)
    for note in chart.notes:
        start = _ms(note.tick)
        end = _ms(note.tick + note.length) if note.length else None
        if end is not None and end <= start:
            end = start + 1
        _write_note(aff, note.column, start, end, _scratch_x(side))
    return aff


def _write_timing(aff: AffChart, chart: IidxChart) -> None:
    bpm_points = chart.tempos
    meter_points = chart.meters
    default_bpm = bpm_points[0].bpm if bpm_points else 120.0
    points: dict[int, tuple[float, float]] = {}
    for tempo in bpm_points:
        points[tempo.tick] = (tempo.bpm, _beats_at(meter_points, tempo.tick))
    for meter in meter_points:
        points[meter.tick] = (_bpm_at(bpm_points, meter.tick, default_bpm), meter.beats)
    points.setdefault(0, (default_bpm, _beats_at(meter_points, 0)))
    for tick in sorted(points):
        bpm, beats = points[tick]
        aff.timings.append(Timing(_ms(tick), bpm, beats))


def _scratch_x(side: str) -> float:
    return SCRATCH_X.get(side, SCRATCH_X["1p"])


def _write_note(aff: AffChart, column: int, start: int, end: int | None, scratch_x: float) -> None:
    lane = WHITE_LANE.get(column)
    if lane is not None:
        if end is None:
            aff.taps.append(Tap(start, lane))
        else:
            aff.holds.append(Hold(start, end, lane))
        return
    x = BLACK_X.get(column)
    if x is not None:
        _sky(aff, start, end, x, color=0, hold_y=1.0)
        return
    if column == SCRATCH:
        _sky(aff, start, end, scratch_x, color=SCRATCH_COLOR, hold_y=HOLD_Y)


def _sky(aff: AffChart, start: int, end: int | None, x: float, color: int, hold_y: float) -> None:
    if end is None:
        arc = Arc(start, start + 1, x, x, color=color, is_void=True)
        arc.arc_taps.append(ArcTap(start))
        aff.arcs.append(arc)
        return
    aff.arcs.append(Arc(start, end, x, x, y_start=hold_y, y_end=hold_y, color=color))


def _ms(tick: int) -> int:
    return int(round(tick))


def _beats_at(meters: list[Meter], tick: int) -> float:
    beats = 4.0
    for meter in meters:
        if meter.tick > tick:
            break
        beats = meter.beats
    return beats


def _bpm_at(tempos: list[Tempo], tick: int, default: float) -> float:
    bpm = default
    for tempo in tempos:
        if tempo.tick > tick:
            break
        bpm = tempo.bpm
    return bpm
