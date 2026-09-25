"""Convert IIDX `.1` charts into Arcade Plus folders."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from iidx2aff.arrange import MODES, apply_arrange, arrange_suffix, normalize_arrange
from iidx2aff.audio import mix_song, sample_count
from iidx2aff.convert import convert_chart
from iidx2aff.iidx import SP_HARD_TO_EASY, SP_NAMES, read_charts

DIFFICULTY_COUNT = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert IIDX .1 charts to Arcaea AFF")
    parser.add_argument("source", type=Path, help="a .1 file or a folder of song directories")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--info", type=Path, default=None, help="folder with video_music_list.xml and music_title_yomi.xml")
    parser.add_argument("--movies", type=Path, default=None)
    parser.add_argument("--thumbs", type=Path, default=None)
    parser.add_argument("--no-media", action="store_true")
    parser.add_argument(
        "--arrange",
        choices=MODES,
        default="off",
        help="lane option: mirror, random, r-random, or s-random. Scratch stays put",
    )
    parser.add_argument("--seed", type=int, default=None, help="random seed for random, r-random, and s-random")
    args = parser.parse_args(argv)
    arrange = normalize_arrange(args.arrange)

    songs = _chart_files(args.source)
    if not songs:
        print(f"no .1 charts under {args.source}", file=sys.stderr)
        return 1
    catalog = _load_catalog(args.info) if args.info else {}
    args.output.mkdir(parents=True, exist_ok=True)
    for path in songs:
        song_id = path.parent.name if path.parent.name.isdigit() else path.stem
        data = path.read_bytes()
        charts = read_charts(data)
        if not charts:
            print(f"skip {path}: not a chart file", file=sys.stderr)
            continue
        title, artist = catalog.get(song_id, (song_id, ""))
        dest = args.output / f"{song_id}{arrange_suffix(arrange)}"
        sounds = _write_song(path, charts, dest, title, artist, arrange=arrange, seed=args.seed)
        if not args.no_media:
            _copy_media(song_id, path.parent, dest, args.thumbs, sounds)
    return 0


def _write_song(
    path: Path,
    charts: list,
    dest: Path,
    title: str,
    artist: str,
    ratings: dict[int, str] | None = None,
    slots: list[int] | None = None,
    side: str = "1p",
    arrange: str = "off",
    seed: int | None = None,
) -> list:
    by_slot = {index: chart for index, chart in charts}
    ordered = _export_slots(by_slot, slots)
    written: list[tuple[int, float, str]] = []
    used: list[int] = []
    for difficulty, slot in enumerate(ordered):
        rng = random.Random(None if seed is None else f"{seed}:{slot}")
        chart, detail = apply_arrange(by_slot[slot], arrange, rng)
        print(detail)
        aff = convert_chart(chart, side)
        if not aff.taps and not aff.holds and not aff.arcs:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"{difficulty}.aff").write_text(aff.dumps(), encoding="utf-8")
        rating = "" if ratings is None else ratings.get(slot, "")
        written.append((difficulty, aff.timings[0].bpm if aff.timings else 0.0, rating))
        used.append(slot)
        print(f"{path.parent.name} {SP_NAMES[slot]} -> {dest.name}/{difficulty}.aff")
    if not written:
        return []
    difficulties: list[dict | None] = [None] * DIFFICULTY_COUNT
    for difficulty, bpm, rating in written:
        difficulties[difficulty] = {"Rating": rating, "BaseBpm": bpm}
    meta = {
        "Title": title,
        "Artist": artist,
        "BaseBpm": written[0][1],
        "Difficulties": difficulties,
        "LastWorkingDifficulty": written[-1][0],
        "LastWorkingTiming": 0,
    }
    arcade = dest / "Arcade"
    arcade.mkdir(exist_ok=True)
    (arcade / "Project.arcade").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    hardest = next(slot for slot in SP_HARD_TO_EASY if slot in used)
    return by_slot[hardest].sounds


def _export_slots(by_slot: dict[int, object], slots: list[int] | None) -> list[int]:
    """SP charts in easy-to-hard order. The command line still exports the two hardest."""
    easy_to_hard = tuple(reversed(SP_HARD_TO_EASY))
    if slots is None:
        hardest = [slot for slot in SP_HARD_TO_EASY if slot in by_slot][:2]
        return [slot for slot in easy_to_hard if slot in hardest]
    chosen = {slot for slot in slots if slot in SP_NAMES}
    return [slot for slot in easy_to_hard if slot in chosen and slot in by_slot][:DIFFICULTY_COUNT]


def _sound_pack(source: Path, sounds: list) -> Path | None:
    """Keysound archive for this chart. Preview ``*_pre.2dx`` files are not songs."""
    s3p = sorted(source.glob("*.s3p"))
    if s3p:
        return s3p[0]
    needed = max((sound.sample for sound in sounds if sound.sample > 0), default=0)
    packs: list[tuple[int, str, Path]] = []
    for path in sorted(source.glob("*.2dx")):
        if path.stem.endswith("_pre"):
            continue
        count = sample_count(path.read_bytes()[:4096])
        if count <= 0:
            count = sample_count(path.read_bytes())
        if count <= 0:
            continue
        packs.append((count, path.name, path))
    enough = [item for item in packs if item[0] >= needed]
    pool = enough or packs
    if not pool:
        return None
    pool.sort(key=lambda item: (item[0], item[1]))
    return pool[0][2]


def _copy_media(song_id: str, source: Path, dest: Path, thumbs: Path | None, sounds: list) -> None:
    if not dest.exists():
        return
    pack = _sound_pack(source, sounds)
    mixed = pack is not None and mix_song(pack, sounds, dest / "base.ogg")
    if mixed:
        print(f"{song_id} keysounds -> {dest.name}/base.ogg")
    if not mixed:
        audio = _extract_preview(source, dest / f"{song_id}.preview.wav")
        if audio is not None:
            _run_ffmpeg(["-y", "-i", str(audio), "-vn", "-c:a", "libvorbis", "-q:a", "6", str(dest / "base.ogg")])
            audio.unlink(missing_ok=True)
    if thumbs is not None:
        thumb = thumbs / f"{song_id}_thum.png"
        if thumb.exists():
            _run_ffmpeg(["-y", "-i", str(thumb), str(dest / "base.jpg")])


def _extract_preview(folder: Path, dest: Path) -> Path | None:
    previews = sorted(folder.glob("*_pre.2dx"))
    if not previews:
        return None
    data = previews[0].read_bytes()
    start = data.find(b"RIFF")
    if start < 0 or start + 12 > len(data):
        return None
    size = int.from_bytes(data[start + 4 : start + 8], "little") + 8
    dest.write_bytes(data[start : start + size])
    return dest


def _run_ffmpeg(args: list[str]) -> None:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found; media was not converted", file=sys.stderr)
        return
    result = subprocess.run(["ffmpeg", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-400:], file=sys.stderr)


def _chart_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    return sorted(source.rglob("*.1"))


def _load_catalog(info: Path) -> dict[str, tuple[str, str]]:
    catalog: dict[str, tuple[str, str]] = {}
    yomi = info / "music_title_yomi.xml"
    if yomi.exists():
        root = ET.fromstring(_read_xml(yomi))
        for data in root.findall("data"):
            index = data.findtext("index")
            name = (data.findtext("yomi") or "").strip()
            if index and name:
                catalog[index] = (name, "")
    videos = info / "video_music_list.xml"
    if videos.exists():
        root = ET.fromstring(_read_xml(videos))
        for music in root.findall("music"):
            song_id = music.get("id")
            node = music.find("info")
            if not song_id or node is None:
                continue
            title = (node.findtext("title_name") or "").strip()
            artist = (node.findtext("artist_name") or "").strip()
            if title:
                catalog[song_id] = (title, artist)
    return catalog


def _read_xml(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
