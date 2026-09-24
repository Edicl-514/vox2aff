"""Parser for SOUND VOLTEX OUTPUT TEXT FILE (VOX format versions 10 through 13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TimeSig:
    measure: int
    numerator: int
    denominator: int


@dataclass
class BpmEvent:
    measure: int
    beat: int
    cell: int
    bpm: float
    stopped: bool = False


@dataclass
class ButtonNote:
    track: int
    measure: int
    beat: int
    cell: int
    length: int
    kind: int


@dataclass
class LaserPoint:
    track: int
    measure: int
    beat: int
    cell: int
    position: float
    point: int
    wide: bool = False


@dataclass
class VoxChart:
    resolution: int
    time_sigs: list[TimeSig]
    bpms: list[BpmEvent]
    buttons: list[ButtonNote]
    lasers: list[LaserPoint]
    end_measure: int = 1
    end_beat: int = 1
    end_cell: int = 0


def _split_time(token: str) -> tuple[int, int, int]:
    measure, beat, cell = token.split(",")
    return int(measure), int(beat), int(cell)


# Versions 10 and earlier store a laser knob as an integer from 0 to 127.
# Versions 12 and 13 store the same knob as a 0–1 float, with the same
# point codes: 1 starts a segment, 0 continues it, 2 ends it.
LASER_POSITION_MAX = 127
SUPPORTED_VERSIONS = range(10, 14)


def _laser_position(token: str) -> float:
    if "." in token:
        return float(token)
    return int(token) / LASER_POSITION_MAX


def read_vox_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp932", errors="replace")


def parse_vox(text: str) -> VoxChart:
    resolution = 48
    time_sigs: list[TimeSig] = []
    bpms: list[BpmEvent] = []
    buttons: list[ButtonNote] = []
    lasers: list[LaserPoint] = []
    end = (1, 1, 0)
    section: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line == "#END":
            section = None
            continue
        if line.startswith("#"):
            section = line
            continue
        if section is None:
            continue

        if section == "#FORMAT VERSION":
            version = int(line)
            if version not in SUPPORTED_VERSIONS:
                raise ValueError(f"unsupported VOX format version {version}")
        elif section == "#BEAT RESOLUTION":
            resolution = int(line)
        elif section == "#BEAT INFO":
            when, numerator, denominator = line.split()
            measure, _beat, _cell = _split_time(when)
            time_sigs.append(TimeSig(measure, int(numerator), int(denominator)))
        elif section == "#BPM INFO":
            parts = line.split()
            measure, beat, cell = _split_time(parts[0])
            meter = parts[2] if len(parts) > 2 else ""
            bpms.append(BpmEvent(measure, beat, cell, float(parts[1]), meter.endswith("-")))
        elif section == "#END POSITION":
            end = _split_time(line.split()[0])
        elif section in {f"#TRACK{i}" for i in range(1, 9)}:
            track = int(section.removeprefix("#TRACK"))
            parts = line.split()
            measure, beat, cell = _split_time(parts[0])
            if track in {1, 8}:
                lasers.append(
                    LaserPoint(
                        track,
                        measure,
                        beat,
                        cell,
                        _laser_position(parts[1]),
                        int(parts[2]),
                        wide=len(parts) > 5 and parts[5] == "2",
                    )
                )
            else:
                buttons.append(
                    ButtonNote(
                        track,
                        measure,
                        beat,
                        cell,
                        int(parts[1]),
                        int(parts[2]),
                    )
                )

    if not time_sigs:
        time_sigs.append(TimeSig(1, 4, 4))
    if not bpms:
        raise ValueError("VOX chart has no BPM info")
    return VoxChart(resolution, time_sigs, bpms, buttons, lasers, *end)


@dataclass
class Timeline:
    resolution: int
    measure_start: dict[int, float]
    beat_len: dict[int, float]
    meter_at: list[tuple[float, float]]
    bpm_at: list[tuple[float, float]]
    stopped_ticks: set[float] = field(default_factory=set)

    def tick(self, measure: int, beat: int, cell: int) -> float:
        if measure not in self.measure_start:
            raise ValueError(f"measure {measure} is outside the chart")
        return self.measure_start[measure] + (beat - 1) * self.beat_len[measure] + cell

    def ms(self, tick: float) -> int:
        cursor = 0
        bpm = self.bpm_at[0][1]
        elapsed = 0.0
        for at, new_bpm in self.bpm_at:
            if at >= tick:
                break
            if at > cursor:
                elapsed += (at - cursor) * 60_000.0 / (bpm * self.resolution)
                cursor = at
            bpm = new_bpm
        if tick > cursor:
            elapsed += (tick - cursor) * 60_000.0 / (bpm * self.resolution)
        return int(round(elapsed))

    def bpm_on(self, tick: float) -> float:
        bpm = self.bpm_at[0][1]
        for at, new_bpm in self.bpm_at:
            if at > tick:
                break
            bpm = new_bpm
        return bpm

    def meter_on(self, tick: float) -> float:
        meter = self.meter_at[0][1]
        for at, beats in self.meter_at:
            if at > tick:
                break
            meter = beats
        return meter


def build_timeline(chart: VoxChart) -> Timeline:
    last_measure = max(
        [sig.measure for sig in chart.time_sigs]
        + [event.measure for event in chart.bpms]
        + [note.measure for note in chart.buttons]
        + [point.measure for point in chart.lasers]
        + [chart.end_measure]
    )
    sigs = {sig.measure: sig for sig in chart.time_sigs}
    numerator = 4
    denominator = 4
    measure_start: dict[int, float] = {}
    beat_len: dict[int, float] = {}
    meter_at: list[tuple[float, float]] = []
    cursor = 0.0
    if 1 not in sigs:
        meter_at.append((0.0, 4.0))
    for measure in range(1, last_measure + 2):
        if measure in sigs:
            numerator = sigs[measure].numerator
            denominator = sigs[measure].denominator
            beat_ticks = chart.resolution * 4 / denominator
            meter_at.append((cursor, numerator * 4 / denominator))
        else:
            beat_ticks = chart.resolution * 4 / denominator
        measure_start[measure] = cursor
        beat_len[measure] = beat_ticks
        cursor += numerator * beat_ticks

    timeline = Timeline(chart.resolution, measure_start, beat_len, meter_at, [])
    bpm_at: list[tuple[float, float]] = []
    stopped: set[float] = set()
    for event in chart.bpms:
        tick = timeline.tick(event.measure, event.beat, event.cell)
        bpm_at.append((tick, event.bpm))
        if event.stopped:
            stopped.add(tick)
    bpm_at.sort()
    timeline.bpm_at = bpm_at
    timeline.stopped_ticks = stopped
    return timeline
