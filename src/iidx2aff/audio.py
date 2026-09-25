"""Mix an IIDX chart's BGM bed and keysounds into one track.

Sample numbers in the chart are 1-based indexes into the song's ``.s3p`` pack.
Each entry is an S3V-wrapped WMA clip. Notes play the sample last assigned to
their column. Background events play on their own, including the long bed.
"""

from __future__ import annotations

import array
import math
import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from iidx2aff.iidx import Sound

RATE = 44100


def mix_song(pack: Path, sounds: list[Sound], dest: Path) -> bool:
    if shutil.which("ffmpeg") is None or not sounds:
        return False
    data = pack.read_bytes()
    entries = _entries(data)
    if not entries:
        return False
    needed = sorted({sound.sample for sound in sounds if sound.sample > 0})
    clips = _decode_all(data, entries, needed)
    if not clips:
        return False
    length = max(sound.tick for sound in sounds)
    for sound in sounds:
        clip = clips.get(sound.sample)
        if clip is not None:
            length = max(length, sound.tick + _duration_ms(clip))
    frames = int(length * RATE / 1000) + RATE
    mix = array.array("i", bytes(frames * 2 * 4))
    for sound in sounds:
        clip = clips.get(sound.sample)
        if clip is None:
            continue
        _add(mix, frames, clip, int(sound.tick * RATE / 1000), _gains(sound.pan))
    pcm = _limit(mix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0", "-c:a", "libvorbis", "-q:a", "6", str(dest)],
        input=pcm,
        capture_output=True,
    )
    return result.returncode == 0 and dest.exists()


def _entries(data: bytes) -> list[tuple[int, int]]:
    if data[:4] != b"S3P0" or len(data) < 8:
        return []
    count = struct.unpack_from("<I", data, 4)[0]
    if count <= 0 or 8 + count * 8 > len(data):
        return []
    entries: list[tuple[int, int]] = []
    for index in range(count):
        offset, size = struct.unpack_from("<II", data, 8 + index * 8)
        if size < 64 or offset + size > len(data):
            entries.append((0, 0))
            continue
        entries.append((offset, size))
    return entries


def _decode_all(data: bytes, entries: list[tuple[int, int]], sample_ids: list[int]) -> dict[int, bytes]:

    def decode(sample_id: int) -> tuple[int, bytes | None]:
        index = sample_id - 1
        if index < 0 or index >= len(entries):
            return sample_id, None
        offset, size = entries[index]
        if size <= 0:
            return sample_id, None
        blob = data[offset : offset + size]
        if blob[:4] == b"S3V0":
            blob = blob[32:]
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-ac", "2", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
            input=blob,
            capture_output=True,
        )
        if result.returncode != 0 or len(result.stdout) < 4:
            return sample_id, None
        return sample_id, result.stdout

    clips: dict[int, bytes] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for sample_id, pcm in pool.map(decode, sample_ids):
            if pcm:
                clips[sample_id] = pcm
    return clips


def _duration_ms(pcm: bytes) -> int:
    return int(len(pcm) / 4 / RATE * 1000)


def _gains(pan: int) -> tuple[float, float]:
    if pan <= 0 or pan == 8:
        position = 0.0
    else:
        position = max(-1.0, min(1.0, (pan - 8) / 7))
    angle = (position + 1) * math.pi / 4
    return math.cos(angle), math.sin(angle)


def _add(mix: array.array, frames: int, pcm: bytes, start: int, gains: tuple[float, float]) -> None:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 4 * 4])
    gain_l, gain_r = gains
    count = min(len(samples) // 2, frames - start)
    if count <= 0:
        return
    left = start * 2
    for index in range(count):
        mix[left] += int(samples[index * 2] * gain_l)
        mix[left + 1] += int(samples[index * 2 + 1] * gain_r)
        left += 2


def _limit(mix: array.array) -> bytes:
    peak = max((abs(sample) for sample in mix), default=0)
    scale = 32767 / peak if peak > 32767 else 1.0
    out = array.array("h", (max(-32767, min(32767, int(sample * scale))) for sample in mix))
    return out.tobytes()
