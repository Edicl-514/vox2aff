"""Read chart and audio entries out of a Konami IFS sound pack.

CastHour and earlier keep each song in ``sound/{id}.ifs``. The manifest is
binary XML. File names escape punctuation so ``01000.1`` is stored as
``_01000_E1``. Offsets are relative to the end of that manifest.
"""

from __future__ import annotations

import struct
from pathlib import Path

from kbinxml import KBinXML

_SIGNATURE = 0x6CAD8F89
_FIX = {
    "A": " ",
    "B": "$",
    "C": "+",
    "D": "-",
    "E": ".",
    "F": ":",
    "G": "@",
    "H": "~",
    "_": "_",
}
_SPECIAL = {"_info_", "_super_"}


def read_at(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(size)


def load_chart(path: Path) -> tuple[int, int, bytes] | None:
    """Offset, size, and bytes of the ``.1`` chart inside this IFS."""
    with path.open("rb") as handle:
        found = _entries(handle)
        for name, offset, size in found:
            if not name.endswith(".1"):
                continue
            handle.seek(offset)
            return offset, size, handle.read(size)
    return None


def extract(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with path.open("rb") as handle:
        for name, offset, size in entries(path):
            handle.seek(offset)
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read(size))


def entries(path: Path) -> list[tuple[str, int, int]]:
    with path.open("rb") as handle:
        return _entries(handle)


def _entries(handle) -> list[tuple[str, int, int]]:
    header = handle.read(20)
    if len(header) < 20:
        return []
    signature, flags, inverse, _time, _tree, manifest_end = struct.unpack(">IHHIII", header)
    if signature != _SIGNATURE or flags ^ inverse != 0xFFFF:
        return []
    header_size = 36 if flags & 2 else 20
    if manifest_end <= header_size:
        return []
    handle.seek(header_size)
    manifest = handle.read(manifest_end - header_size)
    try:
        root = KBinXML(manifest).xml_doc
    except Exception:
        return []
    found: list[tuple[str, int, int]] = []
    _walk(root, "", manifest_end, found)
    return found


def _walk(node, prefix: str, base: int, found: list[tuple[str, int, int]]) -> None:
    for child in node:
        name = _fix_name(child.tag)
        kind = child.get("__type")
        if kind in {"2s32", "3s32"} and child.text:
            parts = child.text.split()
            if len(parts) >= 2:
                found.append((prefix + name, base + int(parts[0]), int(parts[1])))
            continue
        if len(child):
            _walk(child, prefix + name + "/", base, found)


def _fix_name(name: str) -> str:
    if name in _SPECIAL:
        return name
    start = 0
    fixed = name
    while True:
        index = fixed.find("_", start)
        if index < 0:
            return fixed
        key = fixed[index + 1 : index + 2]
        if index == 0 and key.isdigit():
            replacement = key
        else:
            replacement = _FIX.get(key)
            if replacement is None:
                return name
        fixed = fixed[:index] + replacement + fixed[index + 2 :]
        start = index + 1
