"""Read an SDVX data folder into a song list.

Song folders live in ``data/music/{id}_{ascii}``. Titles, artists, and
difficulty metadata come from ``data/others/music_db.xml``.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

LEVEL_NAMES = ("novice", "advanced", "exhaust", "infinite", "maximum")
CHART_SUFFIX = {0: "1n", 1: "2a", 2: "3e", 3: "4i", 4: "5m"}
SUFFIX_INDEX = {suffix: index for index, suffix in CHART_SUFFIX.items()}

VERSIONS = {
    1: "BOOTH",
    2: "INFINITE INFECTION",
    3: "GRAVITY WARS",
    4: "HEAVENLY HAVEN",
    5: "VIVID WAVE",
    6: "EXCEED GEAR",
    7: "∇ NABLA",
}

# music_db genre is a bit field. Names follow the in-game categories.
GENRE_BITS = (
    (0x01, "EXIT TUNES"),
    (0x02, "FLOOR"),
    (0x04, "东方 ARRANGE"),
    (0x08, "VOCALOID"),
    (0x10, "BEMANI"),
    (0x20, "SDVX ORIGINAL"),
    (0x40, "POPS & ANIME"),
    (0x80, "ひなビタ♪"),
)

INFINITE_NAMES = {2: "INF", 3: "GRV", 4: "HVN", 5: "VVD", 6: "XCD", 7: "NBL"}
DIFFICULTY_NAMES = ("NOV", "ADV", "EXH", "INF", "MXM")


@dataclass
class Difficulty:
    index: int
    level: str
    illustrator: str
    effector: str


@dataclass
class Song:
    music_id: str
    folder_id: str
    title: str
    artist: str
    ascii_name: str
    bpm_min: int
    bpm_max: int
    version: int
    genre: int
    release_date: str
    inf_ver: int
    difficulties: dict[int, Difficulty] = field(default_factory=dict)
    folder: Path | None = None
    jackets: dict[int, Path] = field(default_factory=dict)
    charts: set[int] = field(default_factory=set)

    @property
    def number(self) -> int:
        return int(self.music_id)

    @property
    def output_name(self) -> str:
        return f"{self.folder_id}_{self.ascii_name}"

    @property
    def label(self) -> str:
        return f"{self.folder_id} - {self.title} - {self.artist}"

    def difficulty_name(self, index: int) -> str:
        if index == 3:
            return INFINITE_NAMES.get(self.inf_ver, "INF")
        return DIFFICULTY_NAMES[index]

    def rating_map(self) -> dict[int, str]:
        return {index: item.level for index, item in self.difficulties.items() if item.level}


def version_text(version: int) -> str:
    return VERSIONS.get(version, f"Version {version}")


def genre_text(genre: int) -> str:
    if genre == 0:
        return "OTHER"
    names = [name for bit, name in GENRE_BITS if genre & bit]
    unknown = genre
    for bit, _name in GENRE_BITS:
        unknown &= ~bit
    if unknown:
        names.append(f"0x{unknown:X}")
    return " / ".join(names) if names else str(genre)


def format_bpm(value: int) -> str:
    if value <= 0:
        return ""
    number = value / 100
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def format_date(raw: str) -> str:
    if len(raw) >= 8 and raw[:8].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def find_music_db(data_dir: Path) -> Path | None:
    for candidate in (
        data_dir / "others" / "music_db.xml",
        data_dir / "music" / "music_db.xml",
        data_dir / "music_db.xml",
    ):
        if candidate.is_file():
            return candidate
    return None


def load_songs(data_dir: Path) -> list[Song]:
    database = find_music_db(data_dir)
    if database is None:
        raise FileNotFoundError(f"在 {data_dir} 下没有找到 music_db.xml")
    music_root = data_dir / "music"
    if not music_root.is_dir():
        raise FileNotFoundError(f"在 {data_dir} 下没有找到 music 文件夹")
    folders = {
        path.name: path
        for path in music_root.iterdir()
        if path.is_dir()
    }
    songs = [_song_from_element(music, folders) for music in _music_elements(database)]
    songs.sort(key=lambda song: song.number)
    return songs


def _music_elements(path: Path) -> list[ET.Element]:
    raw = path.read_bytes()
    text = raw.decode("cp932")
    root = ET.fromstring(text)
    return root.findall("music")


def _song_from_element(music: ET.Element, folders: dict[str, Path]) -> Song:
    info = music.find("info")
    music_id = music.get("id") or "0"
    ascii_name = ""
    title = music_id
    artist = ""
    bpm_min = bpm_max = 0
    version = genre = inf_ver = 0
    release_date = ""
    if info is not None:
        ascii_name = (info.findtext("ascii") or "").strip()
        title = info.findtext("title_name") or music_id
        artist = info.findtext("artist_name") or ""
        bpm_min = _int_text(info.findtext("bpm_min"))
        bpm_max = _int_text(info.findtext("bpm_max"))
        version = _int_text(info.findtext("version"))
        genre = _int_text(info.findtext("genre"))
        inf_ver = _int_text(info.findtext("inf_ver"))
        release_date = (info.findtext("distribution_date") or "").strip()

    folder, folder_id = _locate_folder(folders, music_id, ascii_name)
    song = Song(
        music_id=music_id,
        folder_id=folder_id,
        title=title.strip(),
        artist=artist.strip(),
        ascii_name=ascii_name,
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        version=version,
        genre=genre,
        release_date=release_date,
        inf_ver=inf_ver,
        difficulties=_difficulties(music.find("difficulty")),
        folder=folder,
        jackets=_jackets(folder, folder_id),
        charts=_charts(folder),
    )
    return song


def _locate_folder(folders: dict[str, Path], music_id: str, ascii_name: str) -> tuple[Path | None, str]:
    number = int(music_id) if music_id.isdigit() else 0
    padded = f"{number:04d}"
    if ascii_name:
        for key in (f"{padded}_{ascii_name}", f"{music_id}_{ascii_name}", f"{number}_{ascii_name}"):
            folder = folders.get(key)
            if folder is not None:
                return folder, key.split("_", 1)[0]
    return None, padded


def _difficulties(node: ET.Element | None) -> dict[int, Difficulty]:
    if node is None:
        return {}
    found: dict[int, Difficulty] = {}
    for index, name in enumerate(LEVEL_NAMES):
        level_node = node.find(name)
        if level_node is None:
            continue
        found[index] = Difficulty(
            index=index,
            level=_format_level(level_node.findtext("difnum")),
            illustrator=(level_node.findtext("illustrator") or "").strip(),
            effector=(level_node.findtext("effected_by") or "").strip(),
        )
    return found


def _jackets(folder: Path | None, folder_id: str) -> dict[int, Path]:
    if folder is None:
        return {}
    found: dict[int, Path] = {}
    for index in range(5):
        path = folder / f"jk_{folder_id}_{index + 1}.png"
        if path.is_file():
            found[index] = path
    return found


def _charts(folder: Path | None) -> set[int]:
    if folder is None:
        return set()
    found: set[int] = set()
    for path in folder.glob("*.vox"):
        suffix = path.stem.rsplit("_", 1)[-1]
        index = SUFFIX_INDEX.get(suffix)
        if index is not None:
            found.add(index)
    return found


def _format_level(raw: str | None) -> str:
    if not raw or not raw.strip().lstrip("-").isdigit():
        return ""
    value = int(raw) / 10
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}"


def _int_text(raw: str | None) -> int:
    if not raw:
        return 0
    text = raw.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return 0
