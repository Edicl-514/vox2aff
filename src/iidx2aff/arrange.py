"""IIDX lane options. Scratch stays on its own column.

MIRROR flips keys 1–7. RANDOM shuffles those seven columns. R-RANDOM is one
of the twelve rotations, never the original chart or a plain mirror. S-RANDOM
sends each note to its own key. Two notes at the same time, or a note landing
inside a hold, stay on different keys.
"""

from __future__ import annotations

import random
from dataclasses import replace

from iidx2aff.iidx import IidxChart, Note

OFF = "off"
MIRROR = "mirror"
RANDOM = "random"
R_RANDOM = "r-random"
S_RANDOM = "s-random"
MODES = (OFF, MIRROR, RANDOM, R_RANDOM, S_RANDOM)
SCRATCH = 7
_KEYS = (0, 1, 2, 3, 4, 5, 6)

_ALIASES = {
    "off": OFF,
    "none": OFF,
    "mirror": MIRROR,
    "random": RANDOM,
    "r-random": R_RANDOM,
    "rrandom": R_RANDOM,
    "s-random": S_RANDOM,
    "srandom": S_RANDOM,
}


def normalize_arrange(value: str) -> str:
    text = str(value).strip().lower().replace("_", "-").replace(" ", "")
    mode = _ALIASES.get(text)
    if mode is None:
        raise ValueError(f"unknown IIDX arrange option {value!r}")
    return mode


def arrange_suffix(mode: str) -> str:
    mode = normalize_arrange(mode)
    if mode == OFF:
        return ""
    return "_" + mode.replace("-", "")


def apply_arrange(chart: IidxChart, mode: str, rng: random.Random) -> tuple[IidxChart, str]:
    mode = normalize_arrange(mode)
    if mode == OFF:
        return chart, "OFF"
    if mode == MIRROR:
        mapping = {column: 6 - column for column in _KEYS}
        return _remap(chart, mapping), f"MIRROR {describe(mapping)}"
    if mode == RANDOM:
        order = list(_KEYS)
        rng.shuffle(order)
        mapping = {column: order[column] for column in _KEYS}
        return _remap(chart, mapping), f"RANDOM {describe(mapping)}"
    if mode == R_RANDOM:
        mapping = _rotation(rng.randrange(2) == 1, rng.randrange(1, 7))
        return _remap(chart, mapping), f"R-RANDOM {describe(mapping)}"
    notes = _scatter(chart.notes, rng)
    return replace(chart, notes=notes), "S-RANDOM 每个键音单独换列，盘子不动"


def describe(mapping: dict[int, int]) -> str:
    """Lane string in the usual S1234567 form. Scratch is the leading S."""
    shown = [0] * 7
    for source, dest in mapping.items():
        shown[dest] = source + 1
    return "S" + "".join(str(key) for key in shown)


def _rotation(mirrored: bool, shift: int) -> dict[int, int]:
    base = [7, 6, 5, 4, 3, 2, 1] if mirrored else [1, 2, 3, 4, 5, 6, 7]
    order = base[shift:] + base[:shift]
    return {order[index] - 1: index for index in range(7)}


def _remap(chart: IidxChart, mapping: dict[int, int]) -> IidxChart:
    notes = [
        replace(note, column=mapping[note.column]) if note.column in mapping else note
        for note in chart.notes
    ]
    return replace(chart, notes=notes)


def _scatter(notes: list[Note], rng: random.Random) -> list[Note]:
    placed: list[tuple[int, int, int]] = []
    rewritten: list[Note] = []
    for note in sorted(notes, key=lambda item: (item.tick, item.column)):
        if note.column == SCRATCH:
            rewritten.append(note)
            continue
        end = note.tick + note.length if note.length else note.tick
        column = _pick_key(note.tick, end, placed, rng)
        placed.append((column, note.tick, end))
        rewritten.append(replace(note, column=column))
    return rewritten


def _pick_key(tick: int, end: int, placed: list[tuple[int, int, int]], rng: random.Random) -> int:
    free = [column for column in _KEYS if not _blocked(column, tick, end, placed)]
    if free:
        return rng.choice(free)
    return max(_KEYS, key=lambda column: _clearance(column, tick, placed))


def _blocked(column: int, tick: int, end: int, placed: list[tuple[int, int, int]]) -> bool:
    for other, start, stop in placed:
        if other != column:
            continue
        if tick == start or (tick < stop and start < end):
            return True
    return False


def _clearance(column: int, tick: int, placed: list[tuple[int, int, int]]) -> int:
    gaps = [abs(tick - start) for other, start, _stop in placed if other == column]
    return min(gaps) if gaps else 10**9
