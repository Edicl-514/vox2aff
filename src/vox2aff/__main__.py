"""Command line: convert one VOX file, or a song folder, or the whole sample tree."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from vox2aff.catalog import LEVEL_TO_INDEX, SUFFIX_INDEX
from vox2aff.convert import convert_chart
from vox2aff.vox import parse_vox, read_vox_text

# Slots match Arcade Plus: 0.aff through 4.aff.
DIFFICULTY = SUFFIX_INDEX
DIFFICULTY_COUNT = 5
LEVEL_TO_DIFF = LEVEL_TO_INDEX


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert SDVX VOX charts to Arcaea AFF")
    parser.add_argument("source", type=Path, help="a .vox file, a song folder, or a music root")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="AFF file, or a folder of Arcade Plus projects",
    )
    parser.add_argument(
        "--music-db",
        type=Path,
        default=None,
        help="music_db.xml used to fill song title and artist",
    )
    parser.add_argument(
        "--no-media",
        action="store_true",
        help="write charts only, skip jacket and audio",
    )
    args = parser.parse_args(argv)
    source: Path = args.source
    if source.is_file():
        chart = convert_chart(parse_vox(read_vox_text(source)))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(chart.dumps(), encoding="utf-8")
        print(args.output)
        return 0

    songs = _song_folders(source)
    if not songs:
        print(f"no .vox files under {source}", file=sys.stderr)
        return 1
    catalog = _load_catalog(args.music_db) if args.music_db else {}
    args.output.mkdir(parents=True, exist_ok=True)
    for folder in songs:
        _convert_song(folder, args.output / folder.name, catalog, media=not args.no_media)
    return 0


def _song_folders(source: Path) -> list[Path]:
    if any(source.glob("*.vox")):
        return [source]
    return sorted(path for path in source.iterdir() if path.is_dir() and any(path.glob("*.vox")))


def _convert_song(folder: Path, dest: Path, catalog: dict[str, tuple[str, str, dict[int, str]]], media: bool) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    info = catalog.get(folder.name)
    titles = info[2] if info else {}
    difficulties: list[dict | None] = [None] * DIFFICULTY_COUNT
    base_bpm = 0.0
    last_diff = 0
    for vox_path in sorted(folder.glob("*.vox")):
        suffix = vox_path.stem.rsplit("_", 1)[-1]
        difficulty = DIFFICULTY.get(suffix)
        if difficulty is None:
            print(f"skip {vox_path.name}: unknown difficulty suffix", file=sys.stderr)
            continue
        text = read_vox_text(vox_path)
        vox = parse_vox(text)
        aff = convert_chart(vox)
        (dest / f"{difficulty}.aff").write_text(aff.dumps(), encoding="utf-8")
        bpm = vox.bpms[0].bpm
        if base_bpm == 0.0:
            base_bpm = bpm
        rating = titles.get(difficulty, "")
        difficulties[difficulty] = {"Rating": rating, "BaseBpm": bpm}
        last_diff = difficulty
        print(f"{vox_path.name} -> {dest.name}/{difficulty}.aff")
    title, artist = (info[0], info[1]) if info else (folder.name, "")
    meta = {
        "Title": title,
        "Artist": artist,
        "BaseBpm": base_bpm,
        "Difficulties": difficulties,
        "LastWorkingDifficulty": last_diff,
        "LastWorkingTiming": 0,
    }
    arcade = dest / "Arcade"
    arcade.mkdir(exist_ok=True)
    (arcade / "Project.arcade").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if media:
        _copy_media(folder, dest)


def _copy_media(folder: Path, dest: Path) -> None:
    jackets = sorted(folder.glob("jk_*_[0-9].png"))
    if jackets:
        shutil.copyfile(jackets[0], dest / "base.png")
        _run_ffmpeg(["-y", "-i", str(jackets[0]), str(dest / "base.jpg")])
    audio = next(folder.glob("*.s3v"), None)
    preview = {path.name for path in folder.glob("*_pre.s3v")}
    full = [path for path in folder.glob("*.s3v") if path.name not in preview]
    if full:
        audio = full[0]
    if audio is not None:
        _run_ffmpeg(["-y", "-i", str(audio), "-vn", "-c:a", "libvorbis", "-q:a", "6", str(dest / "base.ogg")])


def _run_ffmpeg(args: list[str]) -> None:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found; media was not converted", file=sys.stderr)
        return
    result = subprocess.run(["ffmpeg", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-500:], file=sys.stderr)


def _load_catalog(path: Path) -> dict[str, tuple[str, str, dict[int, str]]]:
    raw = path.read_bytes()
    for encoding in ("cp932", "shift_jis", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("cp932", errors="replace")
    root = ET.fromstring(text)
    catalog: dict[str, tuple[str, str, dict[int, str]]] = {}
    for music in root.findall("music"):
        info = music.find("info")
        if info is None:
            continue
        ascii_name = (info.findtext("ascii") or "").strip()
        music_id = music.get("id") or ""
        key = f"{music_id}_{ascii_name}" if music_id and ascii_name else ascii_name
        ratings: dict[int, str] = {}
        difficulty = music.find("difficulty")
        if difficulty is not None:
            for name, index in LEVEL_TO_DIFF.items():
                node = difficulty.find(name)
                if node is None:
                    continue
                level = node.findtext("difnum")
                if level:
                    value = int(level) / 10
                    ratings[index] = str(int(value)) if value.is_integer() else f"{value:.1f}"
        catalog[key] = (
            info.findtext("title_name") or key,
            info.findtext("artist_name") or "",
            ratings,
        )
    return catalog


if __name__ == "__main__":
    raise SystemExit(main())
