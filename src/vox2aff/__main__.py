"""Command line: convert one VOX file, or a song folder, or the whole sample tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from vox2aff.arrange import MODES, apply_arrange, normalize_arrange
from vox2aff.catalog import (
    LEVEL_TO_INDEX,
    SUFFIX_INDEX,
    ChartCredit,
    chart_credit,
    difficulty_jackets,
    project_folder_name,
    restore_db_text,
)
from vox2aff.convert import (
    DEFAULT_LASER_STRENGTH,
    DEFAULT_STRAIGHT_LASER,
    STRAIGHT_LASER_MODES,
    convert_chart,
    laser_epsilon,
    straight_laser_mode,
)
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
    parser.add_argument(
        "--laser-strength",
        type=int,
        default=DEFAULT_LASER_STRENGTH,
        metavar="0-100",
        help="simplify laser polylines to cut arc density (0 keeps every point, default 20)",
    )
    parser.add_argument(
        "--straight-laser",
        choices=STRAIGHT_LASER_MODES,
        default=DEFAULT_STRAIGHT_LASER,
        help="straight lasers: keep, noinput (shown, no judgment, default), or black (guide line)",
    )
    parser.add_argument(
        "--jacket-diff",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write per-difficulty jackets (0.jpg–4.jpg). On by default; base.jpg is always the highest difficulty jacket",
    )
    parser.add_argument(
        "--6k",
        action="store_true",
        dest="six_key",
        help="map BT 1–4 to Arcaea lanes 1/3/4/6 and FX to lanes 2/5, and widen lasers. Song folders get a _6k suffix",
    )
    parser.add_argument(
        "--arrange",
        choices=MODES,
        default="off",
        help="lane option: mirror, random, random-mirror, or s-random. Off keeps the chart",
    )
    parser.add_argument("--seed", type=int, default=None, help="random seed for random and s-random")
    args = parser.parse_args(argv)
    arrange = normalize_arrange(args.arrange)
    epsilon = laser_epsilon(args.laser_strength)
    straight = straight_laser_mode(args.straight_laser)
    source: Path = args.source
    if source.is_file():
        rng = random.Random(args.seed)
        vox, detail = apply_arrange(parse_vox(read_vox_text(source)), arrange, rng)
        print(detail)
        chart = convert_chart(vox, epsilon, straight, args.six_key)
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
        _convert_song(
            folder,
            args.output / project_folder_name(folder.name, args.six_key, arrange),
            catalog,
            media=not args.no_media,
            laser_epsilon=epsilon,
            straight_laser=straight,
            jacket_diff=args.jacket_diff,
            six_key=args.six_key,
            arrange=arrange,
            seed=args.seed,
        )
    return 0


def _song_folders(source: Path) -> list[Path]:
    if any(source.glob("*.vox")):
        return [source]
    return sorted(path for path in source.iterdir() if path.is_dir() and any(path.glob("*.vox")))


def _convert_song(
    folder: Path,
    dest: Path,
    catalog: dict[str, tuple[str, str, dict[int, ChartCredit]]],
    media: bool,
    laser_epsilon: float = 0.0,
    straight_laser: str = DEFAULT_STRAIGHT_LASER,
    jacket_diff: bool = True,
    six_key: bool = False,
    arrange: str = "off",
    seed: int | None = None,
) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    info = catalog.get(folder.name)
    credits = info[2] if info else {}
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
        rng = random.Random(None if seed is None else f"{seed}:{difficulty}")
        vox, detail = apply_arrange(vox, arrange, rng)
        print(detail)
        aff = convert_chart(vox, laser_epsilon, straight_laser, six_key)
        (dest / f"{difficulty}.aff").write_text(aff.dumps(), encoding="utf-8")
        bpm = vox.bpms[0].bpm
        if base_bpm == 0.0:
            base_bpm = bpm
        credit = credits.get(difficulty, ChartCredit())
        difficulties[difficulty] = _difficulty_meta(credit, bpm)
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
        _copy_media(folder, dest, jacket_diff=jacket_diff)


def _difficulty_meta(credit: ChartCredit, bpm: float) -> dict:
    """One Arcade Chan difficulty. Empty designers stay null, matching a hand-edited project."""
    return {
        "Rating": credit.rating,
        "JacketDesign": credit.jacket_designer or None,
        "ChartDesign": credit.chart_designer or None,
        "RatingConstant": credit.constant,
        "AudioOverride": 0,
        "TitleOverride": "",
        "ArtistOverride": "",
        "BaseBpm": bpm,
        "BaseBpmOverride": -1.0,
        "BackgroundOverride": "",
    }


def _copy_media(folder: Path, dest: Path, jacket_diff: bool = True) -> None:
    _copy_jackets(folder, dest, jacket_diff=jacket_diff)
    main: Path | None = None
    for path in sorted(folder.glob("*.s3v")):
        parts = path.stem.split("_")
        if "pre" in parts:
            continue
        difficulty = DIFFICULTY.get(parts[-1])
        if difficulty is not None:
            _encode_audio(path, dest / f"{difficulty}.ogg")
            continue
        if parts[-1] == "fx" or main is not None:
            continue
        main = path
    if main is not None:
        _encode_audio(main, dest / "base.ogg")


def _copy_jackets(folder: Path, dest: Path, jacket_diff: bool = True) -> None:
    """Write base.jpg from the hardest jacket, and optional per-difficulty files.

    Arcade Plus reads 0.jpg–4.jpg before base.jpg. Those extra files are omitted
    unless jacket_diff is set, because Arcade Chan mis-loads projects that have them.
    base.jpg stays the highest difficulty jacket either way.
    """
    jackets = difficulty_jackets(folder)
    if not jackets:
        return
    base_index = max(jackets)
    base = jackets[base_index]
    shutil.copyfile(base, dest / "base.png")
    if _encode_jpeg(base, dest / "base.jpg"):
        print(f"jacket {base.name} -> {dest.name}/base.jpg")
    written: set[int] = set()
    if jacket_diff:
        base_digest = _digest(base)
        for index, path in sorted(jackets.items()):
            if index == base_index or _digest(path) == base_digest:
                continue
            target = dest / f"{index}.jpg"
            if _encode_jpeg(path, target):
                written.add(index)
                print(f"jacket {path.name} -> {dest.name}/{target.name}")
    for index in range(DIFFICULTY_COUNT):
        if index in written:
            continue
        leftover = dest / f"{index}.jpg"
        if leftover.is_file():
            leftover.unlink()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _encode_jpeg(source: Path, dest: Path) -> bool:
    _run_ffmpeg(["-y", "-i", str(source), str(dest)])
    return dest.is_file() and dest.stat().st_size > 0


def _encode_audio(source: Path, dest: Path) -> None:
    _run_ffmpeg(["-y", "-i", str(source), "-vn", "-c:a", "libvorbis", "-q:a", "6", str(dest)])


def _run_ffmpeg(args: list[str]) -> None:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found; media was not converted", file=sys.stderr)
        return
    result = subprocess.run(["ffmpeg", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-500:], file=sys.stderr)


def _load_catalog(path: Path) -> dict[str, tuple[str, str, dict[int, ChartCredit]]]:
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
    catalog: dict[str, tuple[str, str, dict[int, ChartCredit]]] = {}
    for music in root.findall("music"):
        info = music.find("info")
        if info is None:
            continue
        ascii_name = (info.findtext("ascii") or "").strip()
        music_id = music.get("id") or ""
        if music_id.isdigit() and ascii_name:
            key = f"{int(music_id):04d}_{ascii_name}"
        else:
            key = ascii_name or music_id
        credits: dict[int, ChartCredit] = {}
        difficulty = music.find("difficulty")
        if difficulty is not None:
            for name, index in LEVEL_TO_DIFF.items():
                node = difficulty.find(name)
                if node is None:
                    continue
                level = node.findtext("difnum")
                shown = ""
                if level and level.strip().lstrip("-").isdigit():
                    value = int(level) / 10
                    shown = str(int(value)) if value.is_integer() else f"{value:.1f}"
                credits[index] = chart_credit(
                    shown,
                    restore_db_text((node.findtext("illustrator") or "").strip()),
                    restore_db_text((node.findtext("effected_by") or "").strip()),
                )
        catalog[key] = (
            restore_db_text(info.findtext("title_name") or key),
            restore_db_text(info.findtext("artist_name") or ""),
            credits,
        )
    return catalog


if __name__ == "__main__":
    raise SystemExit(main())
