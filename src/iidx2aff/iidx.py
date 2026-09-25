"""Parser for beatmania IIDX `.1` chart files.

A file starts with pairs of (offset, length). The first integer is the offset
of the first chart, so the header holds that many bytes of pairs. Modern
files have fourteen slots. Single player is:

0 hyper, 1 normal, 2 another, 3 beginner, 4 leggendaria.

Double player uses the same order from slot 6. Events are eight bytes:
tick, type, parameter, value. A tick is a millisecond on current charts.
Player 1 columns are keys 1-7 then the scratch.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Hardest first. Beginner is slot 3, not slot 0.
SP_HARD_TO_EASY = (4, 2, 0, 1, 3)
SP_NAMES = {4: "leggendaria", 2: "another", 0: "hyper", 1: "normal", 3: "beginner"}


@dataclass
class Note:
    tick: int
    column: int
    length: int


@dataclass
class Sound:
    tick: int
    sample: int
    pan: int


@dataclass
class Tempo:
    tick: int
    bpm: float


@dataclass
class Meter:
    tick: int
    beats: float


@dataclass
class IidxChart:
    notes: list[Note]
    tempos: list[Tempo]
    meters: list[Meter]
    sounds: list[Sound]


def read_charts(data: bytes) -> list[tuple[int, IidxChart]]:
    if len(data) < 8:
        return []
    header = struct.unpack_from("<I", data, 0)[0]
    if header == 0 or header % 8 != 0 or header > 256 or header > len(data):
        return []
    charts: list[tuple[int, IidxChart]] = []
    for index in range(header // 8):
        offset, length = struct.unpack_from("<II", data, index * 8)
        if length == 0 or offset + length > len(data):
            continue
        chart = _read_chart(data, offset, length)
        if chart.notes:
            charts.append((index, chart))
    return charts


def _read_chart(data: bytes, offset: int, length: int) -> IidxChart:
    notes: list[Note] = []
    tempos: list[Tempo] = []
    meters: list[Meter] = []
    sounds: list[Sound] = []
    columns = [0] * 8
    end = offset + length
    cursor = offset
    while cursor + 8 <= end:
        tick, kind, param, value = struct.unpack_from("<IBBH", data, cursor)
        cursor += 8
        if kind == 0x06 or tick == 0x7FFFFFFF:
            break
        if kind == 0x02 and param <= 7:
            columns[param] = value
        elif kind == 0x00 and param <= 7:
            notes.append(Note(tick, param, value))
            if columns[param]:
                sounds.append(Sound(tick, columns[param], 0))
        elif kind == 0x07 and value:
            sounds.append(Sound(tick, value, param))
        elif kind == 0x04 and param:
            tempos.append(Tempo(tick, value / param))
        elif kind == 0x05 and param:
            meters.append(Meter(tick, value / param * 4))
    return IidxChart(notes, tempos, meters, sounds)
