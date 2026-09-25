"""Read an IIDX data folder into a song list.

Titles, artists, genres, and difficulty levels come from ``music_data.bin``.
Charts live in ``sound/{id}/{id}.1``. Jackets are ``graphic/thumbnail/{id}_thum.png``.
BGA files live under ``movie/``; a data set with that folder removed has no BGA.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from iidx2aff.iidx import read_charts
from iidx2aff.ifs import load_chart, read_at

# game_version 0 is substream. Numbered styles match the IIDX version.
VERSIONS = {
    0: "substream",
    1: "1st style",
    2: "2nd style",
    3: "3rd style",
    4: "4th style",
    5: "5th style",
    6: "6th style",
    7: "7th style",
    8: "8th style",
    9: "9th style",
    10: "10th style",
    11: "IIDX RED",
    12: "HAPPY SKY",
    13: "DistorteD",
    14: "GOLD",
    15: "DJ TROOPERS",
    16: "EMPRESS",
    17: "SIRIUS",
    18: "Resort Anthem",
    19: "Lincle",
    20: "tricoro",
    21: "SPADA",
    22: "PENDUAL",
    23: "copula",
    24: "SINOBUZ",
    25: "CANNON BALLERS",
    26: "Rootage",
    27: "HEROIC VERSE",
    28: "BISTROVER",
    29: "CastHour",
    30: "RESIDENT",
    31: "EPOLIS",
    32: "Pinky Crush",
    33: "Sparkle Shower",
}

# Slot, play side, name. Beginner is not slot 0.
CHART_SLOTS = (
    (3, "SP", "BEGINNER"),
    (1, "SP", "NORMAL"),
    (0, "SP", "HYPER"),
    (2, "SP", "ANOTHER"),
    (4, "SP", "LEGGENDARIA"),
    (9, "DP", "BEGINNER"),
    (7, "DP", "NORMAL"),
    (6, "DP", "HYPER"),
    (8, "DP", "ANOTHER"),
    (10, "DP", "LEGGENDARIA"),
)
# music_data stores SP then DP, each beginner, normal, hyper, another, leggendaria.
_LEVEL_INDEX = {3: 0, 1: 1, 0: 2, 2: 3, 4: 4, 9: 5, 7: 6, 6: 7, 8: 8, 10: 9}

VIDEO_SUFFIXES = {".mp4", ".m4v", ".wmv", ".avi", ".mpg", ".mpeg", ".m2v", ".webm", ".mov", ".mkv"}


@dataclass
class ChartInfo:
    slot: int
    side: str
    name: str
    level: int
    present: bool


@dataclass
class Song:
    song_id: int
    title: str
    artist: str
    ascii_name: str
    genre: str
    subtitle: str
    version: int
    title_yomi: str = ""
    artist_yomi: str = ""
    levels: tuple[int, ...] = ()
    bga_name: str = ""
    bpm_min: float = 0.0
    bpm_max: float = 0.0
    peak_level: int = 0
    folder: Path | None = None
    chart_path: Path | None = None
    pack: Path | None = None
    chart_span: tuple[int, int] | None = None
    jacket: Path | None = None
    movie: Path | None = None
    charts: set[int] = field(default_factory=set)

    @property
    def folder_id(self) -> str:
        return f"{self.song_id:05d}"

    @property
    def output_name(self) -> str:
        ascii_name = _folder_piece(self.ascii_name.strip().replace(" ", "_"))
        if not ascii_name:
            return self.folder_id
        return f"{self.folder_id}_{ascii_name}"

    @property
    def label(self) -> str:
        return f"{self.folder_id} - {self.title} - {self.artist}"

    @property
    def number(self) -> int:
        return self.song_id

    @property
    def has_chart(self) -> bool:
        return self.chart_path is not None or self.chart_span is not None

    def chart_bytes(self) -> bytes:
        if self.chart_path is not None:
            return self.chart_path.read_bytes()
        if self.pack is None or self.chart_span is None:
            return b""
        offset, size = self.chart_span
        return read_at(self.pack, offset, size)

    def chart_rows(self) -> list[ChartInfo]:
        rows: list[ChartInfo] = []
        for slot, side, name in CHART_SLOTS:
            level = self.level_of(slot)
            present = slot in self.charts
            if level <= 0 and not present:
                continue
            rows.append(ChartInfo(slot, side, name, level, present))
        return rows

    def level_of(self, slot: int) -> int:
        index = _LEVEL_INDEX.get(slot)
        if index is None or index >= len(self.levels):
            return 0
        return self.levels[index]

    def rating_map(self) -> dict[int, str]:
        return {slot: str(level) for slot, level in ((row.slot, row.level) for row in self.chart_rows()) if level > 0}


# A name like ``n/a`` must stay one folder. These characters are illegal in a
# Windows path component, and ``/`` plus ``\`` are separators on every platform.
_INVALID_FOLDER = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _folder_piece(text: str) -> str:
    return _INVALID_FOLDER.sub("_", text).rstrip(" .")


def version_text(version: int) -> str:
    return VERSIONS.get(version, f"IIDX {version}")


def format_bpm(value: float) -> str:
    if value <= 0:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def load_songs(data_dir: Path) -> list[Song]:
    root = find_data_root(data_dir)
    databases = find_music_data(root)
    if not databases:
        raise FileNotFoundError(f"在 {data_dir} 下没有找到 music_data.bin")
    merged: dict[int, Song] = {}
    for path in databases:
        for song in _read_music_data(path):
            merged[song.song_id] = song
    songs = list(merged.values())
    _apply_yomi(root, songs)
    _apply_video_names(root, songs)
    movies = _index_movies(root)
    thumbs = root / "graphic" / "thumbnail"
    sound = root / "sound"
    for song in songs:
        song.jacket = _thumbnail(thumbs, song.song_id)
        song.movie = _movie_for(movies, song)
        folder, chart = _chart_file(sound, song.song_id)
        song.folder = folder
        song.chart_path = chart
        if chart is not None:
            _attach_chart(song, chart.read_bytes())
        else:
            pack = _pack_file(sound, song.song_id)
            loaded = load_chart(pack) if pack is not None else None
            if pack is not None and loaded is not None:
                offset, size, data = loaded
                song.pack = pack
                song.chart_span = (offset, size)
                _attach_chart(song, data)
    songs.sort(key=lambda song: song.song_id)
    return songs


def find_data_root(path: Path) -> Path:
    for candidate in (path, path.parent, path.parent.parent):
        if (candidate / "info").is_dir() or (candidate / "music_data.bin").is_file():
            if (candidate / "sound").is_dir() or (candidate / "info").is_dir():
                return candidate
    return path


def find_music_data(root: Path) -> list[Path]:
    found: list[Path] = []
    direct = root / "music_data.bin"
    if direct.is_file():
        found.append(direct)
    info = root / "info"
    if info.is_dir():
        nested = info / "music_data.bin"
        if nested.is_file():
            found.append(nested)
        found.extend(sorted(info.glob("*/music_data.bin")))
    # Higher data versions win when the same song id appears twice.
    return sorted(found, key=lambda path: (_data_version(path), str(path)))


def _data_version(path: Path) -> int:
    header = path.read_bytes()[:8]
    if len(header) < 8 or header[:4] != b"IIDX":
        return -1
    return struct.unpack_from("<I", header, 4)[0]


class _Cursor:
    def __init__(self, data: bytes, pos: int) -> None:
        self.data = data
        self.pos = pos

    def unpack(self, fmt: str) -> tuple:
        values = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return values

    def string(self, length: int, encoding: str) -> str:
        raw = self.data[self.pos : self.pos + length]
        self.pos += length
        return raw.decode(encoding, errors="ignore").split("\0", 1)[0].strip()

    def skip(self, length: int) -> None:
        self.pos += length


def _read_music_data(path: Path) -> list[Song]:
    data = path.read_bytes()
    if data[:4] != b"IIDX":
        raise ValueError(f"{path} 不是 music_data.bin")
    version = struct.unpack_from("<I", data, 4)[0]
    if version == 80 or not 20 <= version <= 34:
        raise ValueError(f"不支持的 music_data 版本 {version}（{path.name}）")
    if version >= 32:
        available, _unk, total = struct.unpack_from("<HHI", data, 8)
        index_width = 4
    else:
        available, total, _unk = struct.unpack_from("<HIH", data, 8)
        index_width = 2
    cursor = _Cursor(data, 16 + total * index_width)
    songs: list[Song] = []
    for _index in range(available):
        song = _read_entry(cursor, version)
        if song.song_id > 0 and song.title:
            songs.append(song)
    return songs


def _read_entry(cursor: _Cursor, version: int) -> Song:
    if version >= 32:
        title = cursor.string(0x100, "utf-16-le")
        ascii_name = cursor.string(0x40, "cp932")
        genre = cursor.string(0x80, "utf-16-le")
        artist = cursor.string(0x100, "utf-16-le")
        subtitle = cursor.string(0x100, "utf-16-le")
    else:
        title = cursor.string(0x40, "cp932")
        ascii_name = cursor.string(0x40, "cp932")
        genre = cursor.string(0x40, "cp932")
        artist = cursor.string(0x40, "cp932")
        subtitle = ""
    cursor.skip(20)
    if version >= 32:
        cursor.skip(4)
    cursor.skip(4)  # font index
    game_version = cursor.unpack("<H")[0]
    cursor.skip(14 if version >= 32 else 6)
    if version >= 27:
        levels = cursor.unpack("<10B")
    else:
        normal, hyper, another, _dpn, _dph, _dpa, beginner, _dpb = cursor.unpack("<8B")
        levels = (beginner, normal, hyper, another, 0, _dpb, _dpn, _dph, _dpa, 0)
    cursor.skip(0x286 if version >= 27 else 0xA0)
    song_id, _volume = cursor.unpack("<II")
    cursor.skip(10 if version >= 27 else 8)
    cursor.skip(2)  # bga delay
    if version <= 26:
        cursor.skip(2)
    bga_name = cursor.string(0x20, "ascii")
    cursor.skip(4)  # afp flag
    cursor.skip((10 if version >= 22 else 9) * 0x20)
    if version >= 26:
        cursor.skip(4)
    if subtitle == "0":
        subtitle = ""
    # Song list sorts on this. SP is the first five entries; DP follows and is ignored.
    sp_levels = levels[:5]
    peak = max(sp_levels) if sp_levels else 0
    return Song(
        song_id=song_id,
        title=title,
        artist=artist,
        ascii_name=ascii_name,
        genre=genre,
        subtitle=subtitle,
        version=game_version,
        levels=levels,
        bga_name=bga_name,
        peak_level=peak,
    )


def _decode_xml(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _apply_yomi(root: Path, songs: list[Song]) -> None:
    titles = _yomi_map(root, "music_title_yomi.xml")
    artists = _yomi_map(root, "music_artist_yomi.xml")
    for song in songs:
        song.title_yomi = titles.get(song.song_id, "")
        song.artist_yomi = artists.get(song.song_id, "")


def _yomi_map(root: Path, name: str) -> dict[int, str]:
    found: dict[int, str] = {}
    info = root / "info"
    paths = sorted(info.glob(f"*/{name}")) if info.is_dir() else []
    direct = info / name
    if direct.is_file():
        paths.append(direct)
    for path in paths:
        root_node = ET.fromstring(_decode_xml(path))
        for data in root_node.findall("data"):
            index = (data.findtext("index") or "").strip()
            yomi = (data.findtext("yomi") or "").strip()
            if index.isdigit() and yomi:
                found[int(index)] = yomi
    return found


def _apply_video_names(root: Path, songs: list[Song]) -> None:
    """Restore letters music_data.bin stores as question marks."""
    names = _video_names(root)
    if not names:
        return
    for song in songs:
        title, artist = names.get(song.song_id, ("", ""))
        if title and "?" in song.title:
            song.title = title
        if artist and "?" in song.artist:
            song.artist = artist


def _video_names(root: Path) -> dict[int, tuple[str, str]]:
    found: dict[int, tuple[str, str]] = {}
    info = root / "info"
    paths = sorted(info.glob("*/video_music_list.xml")) if info.is_dir() else []
    direct = info / "video_music_list.xml"
    if direct.is_file():
        paths.append(direct)
    for path in paths:
        root_node = ET.fromstring(_decode_xml(path))
        for music in root_node.findall("music"):
            song_id = music.get("id")
            node = music.find("info")
            if not song_id or not song_id.isdigit() or node is None:
                continue
            title = (node.findtext("title_name") or "").strip()
            artist = (node.findtext("artist_name") or "").strip()
            if title:
                found[int(song_id)] = (title, artist)
    return found


def _thumbnail(folder: Path, song_id: int) -> Path | None:
    if not folder.is_dir():
        return None
    for name in (f"{song_id:05d}_thum.png", f"{song_id}_thum.png"):
        path = folder / name
        if path.is_file():
            return path
    return None


def _index_movies(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for folder_name in ("movie", "movies"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            for key in (path.stem, path.parent.name):
                found.setdefault(key, path)
    return found


def _movie_for(movies: dict[str, Path], song: Song) -> Path | None:
    for key in (song.bga_name, song.folder_id, str(song.song_id)):
        if key and key in movies:
            return movies[key]
    return None


def _chart_file(sound: Path, song_id: int) -> tuple[Path | None, Path | None]:
    if not sound.is_dir():
        return None, None
    for name in (f"{song_id:05d}", str(song_id)):
        folder = sound / name
        chart = folder / f"{name}.1"
        if chart.is_file():
            return folder, chart
        if folder.is_dir():
            found = sorted(folder.glob("*.1"))
            if found:
                return folder, found[0]
    return None, None


def _pack_file(sound: Path, song_id: int) -> Path | None:
    if not sound.is_dir():
        return None
    for name in (f"{song_id:05d}.ifs", f"{song_id}.ifs"):
        path = sound / name
        if path.is_file():
            return path
    return None


def _attach_chart(song: Song, data: bytes) -> None:
    parsed = read_charts(data)
    song.charts = {slot for slot, _chart in parsed}
    bpms = [tempo.bpm for _slot, item in parsed for tempo in item.tempos if tempo.bpm > 0]
    if not bpms:
        return
    song.bpm_min = min(bpms)
    song.bpm_max = max(bpms)
