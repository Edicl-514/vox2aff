"""Parser for SOUND VOLTEX OUTPUT TEXT FILE (VOX format version 13)."""

from __future__ import annotations

from dataclasses import dataclass, field


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
            if version != 13:
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
            bpms.append(BpmEvent(measure, beat, cell, float(parts[1])))
        elif section == "#END POSITION":
            end = _split_time(line.split()[0])
        elif section in {f"#TRACK{i}" for i in range(1, 9)}:
            track = int(section.removeprefix("#TRACK"))
            parts = line.split()
            measure, beat, cell = _split_time(parts[0])
            if "." in parts[1]:
                lasers.append(
                    LaserPoint(
                        track,
                        measure,
                        beat,
                        cell,
                        float(parts[1]),
                        int(parts[2]),
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
    measure_start: dict[int, int]
    meter_at: list[tuple[int, int]]
    bpm_at: list[tuple[int, float]]

    def tick(self, measure: int, beat: int, cell: int) -> int:
        if measure not in self.measure_start:
            raise ValueError(f"measure {measure} is outside the chart")
        return self.measure_start[measure] + (beat - 1) * self.resolution + cell

    def ms(self, tick: int) -> int:
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

    def bpm_on(self, tick: int) -> float:
        bpm = self.bpm_at[0][1]
        for at, new_bpm in self.bpm_at:
            if at > tick:
                break
            bpm = new_bpm
        return bpm

    def meter_on(self, tick: int) -> int:
        meter = self.meter_at[0][1]
        for at, numerator in self.meter_at:
            if at > tick:
                break
            meter = numerator
        return meter


def build_timeline(chart: VoxChart) -> Timeline:
    last_measure = max(
        [sig.measure for sig in chart.time_sigs]
        + [event.measure for event in chart.bpms]
        + [note.measure for note in chart.buttons]
        + [point.measure for point in chart.lasers]
        + [chart.end_measure]
    )
    meter_changes = {sig.measure: sig.numerator for sig in chart.time_sigs}
    numerator = 4
    measure_start: dict[int, int] = {}
    meter_at: list[tuple[int, int]] = []
    cursor = 0
    for measure in range(1, last_measure + 2):
        if measure in meter_changes:
            numerator = meter_changes[measure]
            meter_at.append((cursor, numerator))
        measure_start[measure] = cursor
        cursor += numerator * chart.resolution

    timeline = Timeline(chart.resolution, measure_start, meter_at, [])
    bpm_at = [
        (timeline.tick(event.measure, event.beat, event.cell), event.bpm)
        for event in chart.bpms
    ]
    bpm_at.sort()
    timeline.bpm_at = bpm_at
    return timeline
