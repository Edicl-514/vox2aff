"""Mix an IIDX chart's BGM bed and keysounds into one track.

Sample numbers in the chart are 1-based indexes into the song's ``.s3p`` or
``.2dx`` pack. Notes play the sample last assigned to their column, and a
hold also plays the sample assigned during the hold when it ends. Background
events play on their own, including the long bed.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

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
    pcm = _limit(_mix(sounds, clips, frames))
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0", "-c:a", "libvorbis", "-q:a", "6", str(dest)],
        input=pcm,
        capture_output=True,
    )
    return result.returncode == 0 and dest.exists()


def sample_count(data: bytes) -> int:
    if data[:4] == b"S3P0" and len(data) >= 8:
        return struct.unpack_from("<I", data, 4)[0]
    return _twodx_count(data)


def _entries(data: bytes) -> list[tuple[int, int]]:
    if data[:4] == b"S3P0":
        return _s3p_entries(data)
    if _twodx_count(data):
        return _twodx_entries(data)
    return []


def _s3p_entries(data: bytes) -> list[tuple[int, int]]:
    if len(data) < 8:
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


def _twodx_count(data: bytes) -> int:
    """Number of clips, or 0 when the bytes are not a 2dx keysound pack."""
    if len(data) < 76:
        return 0
    data_offset, count = struct.unpack_from("<II", data, 16)
    table_end = 72 + count * 4
    if count <= 0 or count > 10000 or data_offset != table_end or data_offset + 4 > len(data):
        return 0
    if data[data_offset : data_offset + 4] != b"2DX9":
        return 0
    return count


def _twodx_entries(data: bytes) -> list[tuple[int, int]]:
    count = _twodx_count(data)
    entries: list[tuple[int, int]] = []
    for index in range(count):
        offset = struct.unpack_from("<I", data, 72 + index * 4)[0]
        if offset + 12 > len(data) or data[offset : offset + 4] != b"2DX9":
            entries.append((0, 0))
            continue
        header_size, payload = struct.unpack_from("<II", data, offset + 4)
        start = offset + header_size
        if header_size < 12 or start + payload > len(data):
            entries.append((0, 0))
            continue
        entries.append((start, payload))
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


def _mix(sounds: list[Sound], clips: dict[int, bytes], frames: int) -> np.ndarray:
    """Sum every clip into one int64 stereo buffer.

    Gain is applied once per sample and pan, with truncation toward zero, the
    same rounding the old per-sample loop used. The accumulator is wider than
    int32 so dense overlaps stay exact until the limiter.
    """
    mix = np.zeros(frames * 2, dtype=np.int64)
    scaled: dict[tuple[int, int], np.ndarray] = {}
    for sound in sounds:
        pcm = clips.get(sound.sample)
        if pcm is None:
            continue
        key = (sound.sample, sound.pan)
        clip = scaled.get(key)
        if clip is None:
            clip = _scale(pcm, _gains(sound.pan))
            scaled[key] = clip
        start = int(sound.tick * RATE / 1000)
        count = min(len(clip) // 2, frames - start)
        if count <= 0:
            continue
        end = count * 2
        at = start * 2
        mix[at : at + end] += clip[:end]
    return mix


def _scale(pcm: bytes, gains: tuple[float, float]) -> np.ndarray:
    aligned = pcm[: len(pcm) // 4 * 4]
    stereo = np.frombuffer(aligned, dtype=np.int16).astype(np.float64).reshape(-1, 2)
    scaled = np.empty(stereo.shape, dtype=np.int32)
    gain_l, gain_r = gains
    scaled[:, 0] = stereo[:, 0] * gain_l
    scaled[:, 1] = stereo[:, 1] * gain_r
    return scaled.reshape(-1)


def _limit(mix: np.ndarray) -> bytes:
    peak = int(np.max(np.abs(mix))) if mix.size else 0
    if peak > 32767:
        mix = np.trunc(mix * (32767.0 / peak))
    out = np.clip(mix, -32767, 32767).astype(np.int16)
    return out.tobytes()
