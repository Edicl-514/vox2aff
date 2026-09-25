"""SDVX lane options.

MIRROR flips the chart, including laser hands and direction. RANDOM permutes
the four BT lanes among themselves and the two FX lanes among themselves.
Lasers stay put. S-RANDOM assigns every button on its own, and BT notes may
land on an FX lane. A continuous laser is sent to one knob, and may be flipped.

At 200 BPM and above, notes a 16th apart or closer are not stacked on one lane.
"""

from __future__ import annotations

import random
from dataclasses import replace

from vox2aff.vox import ButtonNote, LaserPoint, VoxChart, build_timeline

OFF = "off"
MIRROR = "mirror"
RANDOM = "random"
S_RANDOM = "s-random"
RANDOM_MIRROR = "random-mirror"
MODES = (OFF, MIRROR, RANDOM, RANDOM_MIRROR, S_RANDOM)

BT = (3, 4, 5, 6)
FX = (2, 7)
BUTTONS = (2, 3, 4, 5, 6, 7)
LASERS = (1, 8)
_MIRROR = {3: 6, 6: 3, 4: 5, 5: 4, 2: 7, 7: 2, 1: 8, 8: 1}
_NAME = {3: "A", 4: "B", 5: "C", 6: "D", 2: "L", 7: "R", 1: "VOL-L", 8: "VOL-R"}

_ALIASES = {
    "off": OFF,
    "none": OFF,
    "mirror": MIRROR,
    "random": RANDOM,
    "s-random": S_RANDOM,
    "srandom": S_RANDOM,
    "random-mirror": RANDOM_MIRROR,
    "random+mirror": RANDOM_MIRROR,
    "rmirror": RANDOM_MIRROR,
}


def normalize_arrange(value: str) -> str:
    text = str(value).strip().lower().replace("_", "-").replace(" ", "")
    mode = _ALIASES.get(text)
    if mode is None:
        raise ValueError(f"unknown SDVX arrange option {value!r}")
    return mode


def arrange_suffix(mode: str) -> str:
    mode = normalize_arrange(mode)
    if mode == OFF:
        return ""
    if mode == RANDOM_MIRROR:
        return "_random_mirror"
    return "_" + mode.replace("-", "")


def apply_arrange(chart: VoxChart, mode: str, rng: random.Random) -> tuple[VoxChart, str]:
    mode = normalize_arrange(mode)
    if mode == OFF:
        return chart, "OFF"
    if mode == MIRROR:
        chart = _map_buttons(chart, _MIRROR)
        chart = _mirror_lasers(chart)
        return chart, "MIRROR A↔D B↔C，FX L↔R，激光对调并左右翻转"
    if mode in {RANDOM, RANDOM_MIRROR}:
        mapping = _button_permutation(rng)
        if mode == RANDOM_MIRROR:
            mapping = {source: _MIRROR[dest] for source, dest in mapping.items()}
        chart = _map_buttons(chart, mapping)
        detail = _button_text(mapping)
        if mode == RANDOM:
            return chart, f"RANDOM {detail}，激光不动"
        chart = _mirror_lasers(chart)
        return chart, f"RANDOM+MIRROR {detail}，激光对调并左右翻转"
    chart, detail = _scatter(chart, rng)
    return chart, f"S-RANDOM {detail}"


def _button_permutation(rng: random.Random) -> dict[int, int]:
    bt = list(BT)
    fx = list(FX)
    rng.shuffle(bt)
    rng.shuffle(fx)
    return {source: dest for source, dest in zip(BT, bt, strict=True)} | {
        source: dest for source, dest in zip(FX, fx, strict=True)
    }


def _button_text(mapping: dict[int, int]) -> str:
    parts = [f"{_NAME[source]}→{_NAME[mapping[source]]}" for source in (*BT, *FX)]
    return " ".join(parts)


def _map_buttons(chart: VoxChart, mapping: dict[int, int]) -> VoxChart:
    buttons = [
        replace(note, track=mapping.get(note.track, note.track)) for note in chart.buttons
    ]
    return replace(chart, buttons=buttons)


def _mirror_lasers(chart: VoxChart) -> VoxChart:
    lasers = [
        replace(point, track=_MIRROR[point.track], position=1.0 - point.position)
        if point.track in _MIRROR
        else point
        for point in chart.lasers
    ]
    return replace(chart, lasers=lasers)


def _scatter(chart: VoxChart, rng: random.Random) -> tuple[VoxChart, str]:
    timeline = build_timeline(chart)
    placed: list[tuple[int, int, int]] = []
    buttons: list[ButtonNote] = []
    order = sorted(
        chart.buttons,
        key=lambda note: (timeline.tick(note.measure, note.beat, note.cell), note.track),
    )
    for note in order:
        start = int(timeline.tick(note.measure, note.beat, note.cell))
        end = start + max(note.length, 0)
        bpm = timeline.bpm_on(start)
        track = _pick_button(start, end, bpm, chart.resolution, placed, rng)
        placed.append((track, start, end))
        buttons.append(replace(note, track=track))
    lasers, laser_note = _scatter_lasers(chart, timeline, rng)
    return replace(chart, buttons=buttons, lasers=lasers), laser_note


def _pick_button(
    start: int,
    end: int,
    bpm: float,
    resolution: int,
    placed: list[tuple[int, int, int]],
    rng: random.Random,
) -> int:
    window = resolution / 4 if bpm >= 200 else 0
    free = [
        track
        for track in BUTTONS
        if not _blocked(track, start, end, window, placed)
    ]
    if free:
        return rng.choice(free)
    return max(BUTTONS, key=lambda track: _clearance(track, start, placed))


def _blocked(
    track: int,
    start: int,
    end: int,
    window: float,
    placed: list[tuple[int, int, int]],
) -> bool:
    for other, begin, stop in placed:
        if other != track:
            continue
        if start == begin or (start < stop and begin < end):
            return True
        if window and abs(start - begin) <= window:
            return True
    return False


def _clearance(track: int, start: int, placed: list[tuple[int, int, int]]) -> float:
    gaps = [abs(start - begin) for other, begin, _stop in placed if other == track]
    return min(gaps) if gaps else 10**9


def _scatter_lasers(chart: VoxChart, timeline, rng: random.Random) -> tuple[list[LaserPoint], str]:
    by_track: dict[int, list[LaserPoint]] = {1: [], 8: []}
    for point in chart.lasers:
        if point.track in by_track:
            by_track[point.track].append(point)
    segments: list[list[LaserPoint]] = []
    for track in LASERS:
        segments.extend(_segments(by_track[track]))
    occupied: list[tuple[int, float, float]] = []
    rewritten: list[LaserPoint] = []
    flipped = 0
    for segment in segments:
        ticks = [timeline.tick(point.measure, point.beat, point.cell) for point in segment]
        begin, end = min(ticks), max(ticks)
        track = _pick_laser(begin, end, occupied, rng)
        invert = rng.randrange(2) == 1
        if invert:
            flipped += 1
        occupied.append((track, begin, end if end > begin else begin + 1))
        for point in segment:
            position = 1.0 - point.position if invert else point.position
            rewritten.append(replace(point, track=track, position=position))
    note = f"按键逐个换列，激光 {len(segments)} 段、其中 {flipped} 段左右翻转"
    return rewritten, note


def _pick_laser(
    begin: float,
    end: float,
    occupied: list[tuple[int, float, float]],
    rng: random.Random,
) -> int:
    free = [track for track in LASERS if not _laser_hit(track, begin, end, occupied)]
    if free:
        return rng.choice(free)
    return rng.choice(LASERS)


def _laser_hit(track: int, begin: float, end: float, occupied: list[tuple[int, float, float]]) -> bool:
    for other, start, stop in occupied:
        if other == track and begin < stop and start < end:
            return True
    return False


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
