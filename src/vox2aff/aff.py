"""Arcaea chart events and AFF text writer.

The written file matches the subset Arcade Plus parses: metadata, timing,
taps, holds, and arcs with optional arctaps.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Timing:
    time: int
    bpm: float
    beats: float


@dataclass
class Tap:
    time: int
    lane: int


@dataclass
class Hold:
    time: int
    end: int
    lane: int


@dataclass
class ArcTap:
    time: int


@dataclass
class Arc:
    time: int
    end: int
    x_start: float
    x_end: float
    y_start: float = 1.0
    y_end: float = 1.0
    color: int = 0
    curve: str = "s"
    effect: str = "none"
    is_void: bool = False
    noinput: bool = False
    arc_taps: list[ArcTap] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.time, self.end)


@dataclass
class Scenecontrol:
    time: int
    kind: str
    values: tuple[float, ...] = ()


@dataclass
class AffChart:
    audio_offset: int = 0
    timings: list[Timing] = field(default_factory=list)
    taps: list[Tap] = field(default_factory=list)
    holds: list[Hold] = field(default_factory=list)
    arcs: list[Arc] = field(default_factory=list)
    scenecontrols: list[Scenecontrol] = field(default_factory=list)

    def dumps(self) -> str:
        lines = [f"AudioOffset:{self.audio_offset}", "-"]
        events: list[tuple[int, int, str]] = []
        for timing in self.timings:
            events.append(
                (
                    timing.time,
                    0,
                    f"timing({timing.time},{timing.bpm:.2f},{timing.beats:.2f});",
                )
            )
        for control in self.scenecontrols:
            values = ",".join(_scene_value(value) for value in control.values)
            body = f"scenecontrol({control.time},{control.kind}"
            if values:
                body += f",{values}"
            events.append((control.time, 4, body + ");"))
        for tap in self.taps:
            events.append((tap.time, 1, f"({tap.time},{tap.lane});"))
        for hold in self.holds:
            events.append(
                (hold.time, 2, f"hold({hold.time},{hold.end},{hold.lane});")
            )
        noinput = [arc for arc in self.arcs if arc.noinput]
        for arc in self.arcs:
            if arc.noinput:
                continue
            events.append((arc.time, 3, _arc_text(arc)))
        events.sort()
        lines.extend(text for _time, _order, text in events)
        if noinput:
            lines.append("timinggroup(noinput){")
            for timing in self.timings:
                lines.append(f"timing({timing.time},{timing.bpm:.2f},{timing.beats:.2f});")
            for arc in sorted(noinput, key=lambda item: (item.time, item.end)):
                lines.append(_arc_text(arc))
            lines.append("};")
        return "\n".join(lines) + "\n"


def _scene_value(value: float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return f"{value:.2f}"


def _arc_text(arc: Arc) -> str:
    body = (
        f"arc({arc.time},{arc.end},{arc.x_start:.2f},{arc.x_end:.2f},"
        f"{arc.curve},{arc.y_start:.2f},{arc.y_end:.2f},{arc.color},"
        f"{arc.effect},{'true' if arc.is_void else 'false'})"
    )
    if arc.arc_taps:
        taps = ",".join(f"arctap({tap.time})" for tap in arc.arc_taps)
        body += f"[{taps}]"
    return body + ";"
