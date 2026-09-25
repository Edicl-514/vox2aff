"""Desktop shell for picking an SDVX song and converting its charts."""

from __future__ import annotations

import contextlib
import io
import random
import sys
import threading
import traceback
from pathlib import Path

# ``src/`` is the import root, so ``import vox2aff`` works when this file is launched directly.
_src = Path(__file__).resolve().parents[1]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vox2aff.__main__ import _convert_song
from vox2aff.arrange import MODES
from vox2aff.convert import DEFAULT_LASER_STRENGTH, DEFAULT_STRAIGHT_LASER, laser_epsilon
from vox2aff.catalog import (
    GENRE_BITS,
    VERSIONS,
    Song,
    format_bpm,
    format_date,
    genre_text,
    load_songs,
    project_folder_name,
    version_text,
)

STYLESHEET = """
QWidget {
    background: #1a1d23;
    color: #e8eaed;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QLineEdit, QComboBox, QPlainTextEdit, QListView, QTableWidget {
    background: #23272f;
    border: 1px solid #343a44;
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: #2f6fed;
}
QComboBox::drop-down { border: none; width: 22px; }
QPushButton {
    background: #2e3440;
    border: 1px solid #3d4450;
    border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover { background: #3a4150; }
QPushButton:disabled { color: #8b919a; }
QPushButton#convert {
    background: #2f6fed;
    border: none;
    font-weight: 600;
    padding: 8px 18px;
}
QPushButton#convert:hover { background: #4a82f0; }
QPushButton#convert:disabled { background: #3a4150; color: #8b919a; }
QListView::item { padding: 6px 8px; border-radius: 4px; }
QListView::item:selected { background: #2f6fed; color: white; }
QHeaderView::section {
    background: #23272f;
    color: #c4c8d0;
    border: none;
    padding: 6px;
}
QTableWidget { gridline-color: #343a44; }
QLabel#title { font-size: 18px; font-weight: 600; }
QLabel#artist { color: #b7bdc7; font-size: 14px; }
QLabel#hint { color: #9aa1ab; }
QFrame#cover {
    background: #23272f;
    border: 1px solid #343a44;
    border-radius: 8px;
}
QSplitter::handle { background: #2a2f38; }
QStatusBar { color: #b7bdc7; }
QSlider::groove:horizontal {
    height: 4px;
    background: #343a44;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    margin: -6px 0;
    background: #2f6fed;
    border-radius: 7px;
}
"""


class SongListModel(QAbstractListModel):
    def __init__(self, songs: list[Song] | None = None) -> None:
        super().__init__()
        self.songs = songs or []

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self.songs)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.songs):
            return None
        song = self.songs[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return song.label
        if role == Qt.ItemDataRole.UserRole:
            return song
        return None

    def song_at(self, row: int) -> Song:
        return self.songs[row]

    def set_songs(self, songs: list[Song]) -> None:
        self.beginResetModel()
        self.songs = songs
        self.endResetModel()


class SongProxy(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self.search = ""
        self.version: int | None = None
        self.genre_bit: int | None = None
        self.local_only = False
        self.sort_key = "id"
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, SongListModel):
            return False
        song = model.song_at(source_row)
        if self.version is not None and song.version != self.version:
            return False
        if self.genre_bit is not None:
            if self.genre_bit == 0:
                if song.genre != 0:
                    return False
            elif song.genre & self.genre_bit == 0:
                return False
        if self.local_only and not song.charts:
            return False
        if self.search:
            blob = f"{song.label}\n{song.ascii_name}".casefold()
            if self.search.casefold() not in blob:
                return False
        return True

    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, SongListModel):
            return False
        return _sort_key(model.song_at(left.row()), self.sort_key) < _sort_key(
            model.song_at(right.row()), self.sort_key
        )


def _sort_key(song: Song, key: str) -> tuple:
    if key == "title":
        return (song.title.casefold(), song.number)
    if key == "artist":
        return (song.artist.casefold(), song.number)
    if key == "bpm":
        return (song.bpm_max, song.number)
    if key == "version":
        return (song.version, song.number)
    if key == "date":
        dated = len(song.release_date) >= 8 and song.release_date[:8].isdigit()
        return (not dated, song.release_date[:8] if dated else "", song.number)
    if key == "level":
        return (song.peak_level < 0, song.peak_level, song.number)
    return (song.number,)


def _stored_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes"}
    return False


class MainWindow(QMainWindow):
    songs_ready = Signal(int, object, object)
    songs_failed = Signal(int, bool, str)
    convert_finished = Signal(str)
    convert_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("vox2aff")
        self.resize(1180, 760)
        self.settings = QSettings("vox2aff", "vox2aff")
        self.data_dir: Path | None = None
        self.output_dir: Path | None = None
        self.songs: list[Song] = []
        self._load_generation = 0
        self._converting = False
        self._converting_name = ""
        self.songs_ready.connect(self._on_songs_ready, Qt.ConnectionType.QueuedConnection)
        self.songs_failed.connect(self._on_songs_failed, Qt.ConnectionType.QueuedConnection)
        self.convert_finished.connect(self._on_converted, Qt.ConnectionType.QueuedConnection)
        self.convert_failed.connect(self._on_convert_failed, Qt.ConnectionType.QueuedConnection)

        self.model = SongListModel()
        self.proxy = SongProxy()
        self.proxy.setSourceModel(self.model)

        self._build()
        self._restore_paths()

    def _build(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(10)

        layout.addLayout(self._path_row("SDVX data 文件夹", "data"))
        layout.addLayout(self._path_row("铺面输出文件夹", "output"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_list())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([460, 680])
        layout.addWidget(splitter, 1)

        status = QStatusBar()
        self.setStatusBar(status)
        self.status_label = QLabel("请选择 SDVX 的 data 文件夹")
        status.addWidget(self.status_label, 1)

    def _path_row(self, title: str, kind: str) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(title)
        label.setMinimumWidth(120)
        edit = QLineEdit()
        edit.setReadOnly(True)
        edit.setPlaceholderText("尚未选择")
        button = QPushButton("浏览")
        button.clicked.connect(lambda: self._browse(kind))
        row.addWidget(label)
        row.addWidget(edit, 1)
        row.addWidget(button)
        if kind == "data":
            self.data_edit = edit
        else:
            self.output_edit = edit
        return row

    def _build_list(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索 id、曲名、艺术家")
        self.search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_edit)

        filters = QHBoxLayout()
        self.sort_box = QComboBox()
        self.sort_box.addItem("按 ID", "id")
        self.sort_box.addItem("按曲名", "title")
        self.sort_box.addItem("按艺术家", "artist")
        self.sort_box.addItem("按 BPM", "bpm")
        self.sort_box.addItem("按版本", "version")
        self.sort_box.addItem("按配信日", "date")
        self.sort_box.addItem("按难度", "level")
        self.sort_box.currentIndexChanged.connect(self._apply_filter)
        self.version_box = QComboBox()
        self.version_box.addItem("全部版本", None)
        for number, name in VERSIONS.items():
            self.version_box.addItem(name, number)
        self.version_box.currentIndexChanged.connect(self._apply_filter)
        self.genre_box = QComboBox()
        self.genre_box.addItem("全部流派", None)
        self.genre_box.addItem("OTHER", 0)
        for bit, name in GENRE_BITS:
            self.genre_box.addItem(name, bit)
        self.genre_box.currentIndexChanged.connect(self._apply_filter)
        filters.addWidget(self.sort_box, 1)
        filters.addWidget(self.version_box, 1)
        filters.addWidget(self.genre_box, 1)
        layout.addLayout(filters)

        self.local_only = QCheckBox("只显示有谱面文件的歌曲")
        self.local_only.toggled.connect(self._apply_filter)
        layout.addWidget(self.local_only)

        self.count_label = QLabel("0 首")
        self.count_label.setObjectName("hint")
        layout.addWidget(self.count_label)

        self.song_list = QListView()
        self.song_list.setModel(self.proxy)
        self.song_list.setUniformItemSizes(True)
        self.song_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.song_list.selectionModel().currentChanged.connect(self._on_song_changed)
        layout.addWidget(self.song_list, 1)
        return panel

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.cover = QLabel("无封面")
        self.cover.setObjectName("cover")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setFixedSize(240, 240)
        self.cover.setFrameShape(QFrame.Shape.NoFrame)
        header.addWidget(self.cover)

        info = QVBoxLayout()
        self.title_label = QLabel("未选择歌曲")
        self.title_label.setObjectName("title")
        self.title_label.setWordWrap(True)
        self.artist_label = QLabel("")
        self.artist_label.setObjectName("artist")
        self.artist_label.setWordWrap(True)
        info.addWidget(self.title_label)
        info.addWidget(self.artist_label)

        form = QFormLayout()
        form.setSpacing(4)
        self.id_label = QLabel("—")
        self.ascii_label = QLabel("—")
        self.bpm_label = QLabel("—")
        self.version_label = QLabel("—")
        self.genre_label = QLabel("—")
        self.date_label = QLabel("—")
        self.output_label = QLabel("—")
        self.output_label.setWordWrap(True)
        for label in (
            self.id_label,
            self.ascii_label,
            self.bpm_label,
            self.version_label,
            self.genre_label,
            self.date_label,
            self.output_label,
        ):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("ID", self.id_label)
        form.addRow("ASCII", self.ascii_label)
        form.addRow("BPM", self.bpm_label)
        form.addRow("版本", self.version_label)
        form.addRow("流派", self.genre_label)
        form.addRow("配信", self.date_label)
        form.addRow("输出", self.output_label)
        info.addLayout(form)
        info.addStretch(1)
        header.addLayout(info, 1)
        layout.addLayout(header)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["难度", "等级", "谱师", "效果", "谱面"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(180)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_difficulty_changed)
        layout.addWidget(self.table)

        strength = QHBoxLayout()
        strength.addWidget(QLabel("物量抑制"))
        self.laser_slider = QSlider(Qt.Orientation.Horizontal)
        self.laser_slider.setRange(0, 100)
        self.laser_slider.setValue(DEFAULT_LASER_STRENGTH)
        self.laser_slider.setToolTip("0 保持逐点转换，100 允许大约 0.10 的横向偏差")
        self.laser_value = QLabel(str(DEFAULT_LASER_STRENGTH))
        self.laser_value.setMinimumWidth(28)
        self.laser_slider.valueChanged.connect(self._on_laser_strength)
        strength.addWidget(self.laser_slider, 1)
        strength.addWidget(self.laser_value)
        layout.addLayout(strength)
        strength_hint = QLabel("激光折线简化。0 保持原样，数值越大弧线越疏。")
        strength_hint.setObjectName("hint")
        layout.addWidget(strength_hint)

        straight = QHBoxLayout()
        straight.addWidget(QLabel("直线激光"))
        self.straight_box = QComboBox()
        self.straight_box.addItem("不抑制", "keep")
        self.straight_box.addItem("noinput", "noinput")
        self.straight_box.addItem("只显示黑线", "black")
        self.straight_box.setToolTip("位置不变的激光。noinput 仍显示彩色弧线但不判定")
        self.straight_box.currentIndexChanged.connect(self._on_straight_laser)
        straight.addWidget(self.straight_box, 1)
        layout.addLayout(straight)

        self.jacket_diff = QCheckBox("曲绘差分")
        self.jacket_diff.setToolTip(
            "为每个难度另写 0.jpg–4.jpg。base.jpg 始终用最高难度、分辨率最高的封面"
        )
        self.jacket_diff.toggled.connect(self._on_jacket_diff)
        layout.addWidget(self.jacket_diff)

        keys = QHBoxLayout()
        self.four_key = QCheckBox("4K 转铺")
        self.four_key.setToolTip("BT 1–4 对应四条地面轨，FX 写成天空音符")
        self.four_key.toggled.connect(self._on_key_mode)
        self.six_key = QCheckBox("6K 转铺")
        self.six_key.setToolTip(
            "BT 1–4 对应从左数第 1、3、4、6 轨，FX 对应第 2、5 轨。"
            "激光按六轨宽度向两侧展开，并写入 enwidenlanes / enwidencamera。"
            "与 4K 同时勾选时，一次转换写出两个文件夹"
        )
        self.six_key.toggled.connect(self._on_key_mode)
        keys.addWidget(self.four_key)
        keys.addWidget(self.six_key)
        keys.addStretch(1)
        layout.addLayout(keys)

        arrange = QHBoxLayout()
        arrange.addWidget(QLabel("配置"))
        self.arrange_box = QComboBox()
        self.arrange_box.addItem("关闭", "off")
        self.arrange_box.addItem("MIRROR", "mirror")
        self.arrange_box.addItem("RANDOM", "random")
        self.arrange_box.addItem("RANDOM+MIRROR", "random-mirror")
        self.arrange_box.addItem("S-RANDOM", "s-random")
        self.arrange_box.setToolTip(
            "MIRROR 左右翻转，激光也对调并反向。"
            "RANDOM 只在 BT 之间、FX 之间打乱，激光不动。"
            "S-RANDOM 每个按键单独换列，白键可以落到 FX，激光按段重抽。"
            "200 BPM 及以上时，16 分及更密的音符不会叠在同一列"
        )
        self.arrange_box.currentIndexChanged.connect(self._on_arrange)
        arrange.addWidget(self.arrange_box, 1)
        layout.addLayout(arrange)

        actions = QHBoxLayout()
        self.convert_button = QPushButton("转换铺面")
        self.convert_button.setObjectName("convert")
        self.convert_button.setEnabled(False)
        self.convert_button.clicked.connect(self._convert)
        self.note_label = QLabel("")
        self.note_label.setObjectName("hint")
        actions.addWidget(self.convert_button)
        actions.addWidget(self.note_label, 1)
        layout.addLayout(actions)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("转换日志")
        self.log.setMaximumHeight(140)
        layout.addWidget(self.log)
        return panel

    def _restore_paths(self) -> None:
        data = self.settings.value("data_dir", "")
        output = self.settings.value("output_dir", "")
        if isinstance(data, str) and data:
            self._start_song_load(Path(data), quiet=True)
        if isinstance(output, str) and output:
            self.output_dir = Path(output)
            self.output_edit.setText(output)
        stored = self.settings.value("laser_strength", DEFAULT_LASER_STRENGTH)
        try:
            strength = int(stored)
        except (TypeError, ValueError):
            strength = DEFAULT_LASER_STRENGTH
        self.laser_slider.setValue(max(0, min(100, strength)))
        stored_straight = self.settings.value("straight_laser", DEFAULT_STRAIGHT_LASER)
        straight_index = self.straight_box.findData(str(stored_straight))
        self.straight_box.setCurrentIndex(straight_index if straight_index >= 0 else 1)
        self.jacket_diff.setChecked(_stored_bool(self.settings.value("jacket_diff", True)))
        six_key = _stored_bool(self.settings.value("six_key", False))
        stored_four = self.settings.value("four_key")
        four_key = not six_key if stored_four is None else _stored_bool(stored_four)
        self.four_key.setChecked(four_key)
        self.six_key.setChecked(six_key)
        stored_arrange = self.settings.value("arrange", "off")
        arrange_index = self.arrange_box.findData(str(stored_arrange))
        self.arrange_box.setCurrentIndex(arrange_index if arrange_index >= 0 else 0)
        self._refresh_convert_button()

    def _browse(self, kind: str) -> None:
        current = self.data_edit.text() if kind == "data" else self.output_edit.text()
        start = current or str(Path.home())
        title = "选择 SDVX data 文件夹" if kind == "data" else "选择铺面输出文件夹"
        chosen = QFileDialog.getExistingDirectory(self, title, start)
        if not chosen:
            return
        path = Path(chosen)
        if kind == "data":
            self.settings.setValue("data_dir", str(path))
            self._start_song_load(path, quiet=False)
        else:
            self.output_dir = path
            self.output_edit.setText(str(path))
            self.settings.setValue("output_dir", str(path))
            self._show_song(self._current_song())
            self._refresh_convert_button()

    def _start_song_load(self, path: Path, quiet: bool) -> None:
        self._load_generation += 1
        generation = self._load_generation
        self.data_edit.setText(str(path))
        self.status_label.setText("正在读取曲库…")
        threading.Thread(
            target=self._load_songs_thread,
            args=(generation, path, quiet),
            name="sdvx-song-load",
            daemon=True,
        ).start()

    def _load_songs_thread(self, generation: int, path: Path, quiet: bool) -> None:
        try:
            songs = load_songs(path)
        except Exception as exc:
            self.songs_failed.emit(generation, quiet, str(exc))
            return
        self.songs_ready.emit(generation, path, songs)

    def _on_songs_ready(self, generation: int, path: Path, songs: list[Song]) -> None:
        if generation != self._load_generation:
            return
        self.data_dir = path
        self.data_edit.setText(str(path))
        self.songs = songs
        versions = sorted({song.version for song in songs if song.version not in VERSIONS})
        existing = {self.version_box.itemData(i) for i in range(self.version_box.count())}
        for number in versions:
            if number not in existing:
                self.version_box.addItem(version_text(number), number)
        self.model.set_songs(songs)
        self._apply_filter()
        self.status_label.setText(f"已加载 {len(songs)} 首歌曲")

    def _on_songs_failed(self, generation: int, quiet: bool, message: str) -> None:
        if generation != self._load_generation:
            return
        if self.data_dir is not None:
            self.data_edit.setText(str(self.data_dir))
        else:
            self.data_edit.clear()
        self.status_label.setText(message)
        if not quiet:
            QMessageBox.warning(self, "无法读取 data 文件夹", message)

    def _apply_filter(self) -> None:
        self.proxy.beginFilterChange()
        self.proxy.search = self.search_edit.text().strip()
        self.proxy.version = self.version_box.currentData()
        self.proxy.genre_bit = self.genre_box.currentData()
        self.proxy.local_only = self.local_only.isChecked()
        self.proxy.endFilterChange()
        self.proxy.sort_key = self.sort_box.currentData() or "id"
        self.proxy.invalidate()
        self.proxy.sort(0, Qt.SortOrder.AscendingOrder)
        shown = self.proxy.rowCount()
        total = len(self.songs)
        self.count_label.setText(f"{shown} 首" if shown == total else f"{shown} / {total} 首")
        if self.proxy.rowCount() and not self.song_list.selectionModel().hasSelection():
            self.song_list.setCurrentIndex(self.proxy.index(0, 0))
        elif self.proxy.rowCount() == 0:
            self._show_song(None)

    def _on_song_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        self._show_song(self._song_from_proxy(current))

    def _current_song(self) -> Song | None:
        return self._song_from_proxy(self.song_list.currentIndex())

    def _song_from_proxy(self, index: QModelIndex) -> Song | None:
        if not index.isValid():
            return None
        source = self.proxy.mapToSource(index)
        song = self.model.data(source, Qt.ItemDataRole.UserRole)
        return song if isinstance(song, Song) else None

    def _show_song(self, song: Song | None) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        if song is None:
            self.title_label.setText("未选择歌曲")
            self.artist_label.setText("")
            for label in (
                self.id_label,
                self.ascii_label,
                self.bpm_label,
                self.version_label,
                self.genre_label,
                self.date_label,
                self.output_label,
            ):
                label.setText("—")
            self._set_cover(None)
            self.note_label.setText("")
            self.table.blockSignals(False)
            self._refresh_convert_button()
            return

        self.title_label.setText(song.title)
        self.artist_label.setText(song.artist)
        self.id_label.setText(song.folder_id)
        self.ascii_label.setText(song.ascii_name or "—")
        bpm = format_bpm(song.bpm_max)
        low = format_bpm(song.bpm_min)
        self.bpm_label.setText(bpm if bpm == low or not low else f"{low} – {bpm}")
        self.version_label.setText(version_text(song.version) if song.version else "—")
        self.genre_label.setText(genre_text(song.genre))
        self.date_label.setText(format_date(song.release_date) or "—")
        self._show_output_names(song)

        indexes = sorted(set(song.difficulties) | set(song.charts) | set(song.jackets))
        self.table.setRowCount(len(indexes))
        preferred = 0
        for row, index in enumerate(indexes):
            info = song.difficulties.get(index)
            values = [
                song.difficulty_name(index),
                info.level if info and info.level else "—",
                info.illustrator if info and info.illustrator else "—",
                info.effector if info and info.effector else "—",
                "有" if index in song.charts else "无",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, index)
                self.table.setItem(row, column, item)
            if index in song.jackets:
                preferred = row
        self.table.blockSignals(False)
        if indexes:
            self.table.selectRow(preferred)
        else:
            self._set_cover(None)

        if song.folder is None:
            self.note_label.setText("music 文件夹里没有这首歌")
        elif not song.charts:
            self.note_label.setText("文件夹里没有 .vox 谱面")
        else:
            self.note_label.setText(self._output_note(song))
        self._refresh_convert_button()

    def _on_difficulty_changed(self) -> None:
        song = self._current_song()
        if song is None:
            return
        row = self.table.currentRow()
        item = self.table.item(row, 0)
        if item is None:
            self._set_cover(None)
            return
        index = item.data(Qt.ItemDataRole.UserRole)
        self._set_cover(song.jackets.get(index) if isinstance(index, int) else None)

    def _set_cover(self, path: Path | None) -> None:
        if path is None or not path.is_file():
            self.cover.setPixmap(QPixmap())
            self.cover.setText("无封面")
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.cover.setPixmap(QPixmap())
            self.cover.setText("无法读取封面")
            return
        self.cover.setText("")
        self.cover.setPixmap(
            pixmap.scaled(
                self.cover.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _refresh_convert_button(self) -> None:
        song = self._current_song()
        busy = self._converting
        ready = (
            song is not None
            and song.folder is not None
            and bool(song.charts)
            and self.output_dir is not None
            and self._selected_modes()
            and not busy
        )
        self.convert_button.setEnabled(ready)
        self.convert_button.setText("正在转换…" if busy else "转换铺面")

    def _on_jacket_diff(self, checked: bool) -> None:
        self.settings.setValue("jacket_diff", checked)

    def _selected_modes(self) -> list[bool]:
        modes: list[bool] = []
        if self.four_key.isChecked():
            modes.append(False)
        if self.six_key.isChecked():
            modes.append(True)
        return modes

    def _selected_arrange(self) -> str:
        mode = self.arrange_box.currentData()
        if not isinstance(mode, str) or mode not in MODES:
            return "off"
        return mode

    def _project_names(self, song: Song) -> list[str]:
        arrange = self._selected_arrange()
        return [project_folder_name(song.output_name, six_key, arrange) for six_key in self._selected_modes()]

    def _on_arrange(self) -> None:
        self.settings.setValue("arrange", self._selected_arrange())
        song = self._current_song()
        if song is None:
            return
        self._show_output_names(song)
        if song.folder is not None and song.charts:
            self.note_label.setText(self._output_note(song))

    def _output_note(self, song: Song) -> str:
        names = self._project_names(song)
        if not names:
            return "请至少勾选 4K 或 6K"
        return "输出文件夹名：" + "、".join(names)

    def _show_output_names(self, song: Song) -> None:
        names = self._project_names(song)
        if not names:
            text = "未选择 4K / 6K"
        elif self.output_dir is not None:
            text = "、".join(str(self.output_dir / name) for name in names)
        else:
            text = "、".join(names)
        self.output_label.setText(text)

    def _on_key_mode(self, checked: bool) -> None:
        del checked
        self.settings.setValue("four_key", self.four_key.isChecked())
        self.settings.setValue("six_key", self.six_key.isChecked())
        song = self._current_song()
        if song is None:
            return
        self._show_output_names(song)
        if song.folder is not None and song.charts:
            self.note_label.setText(self._output_note(song))
        self._refresh_convert_button()

    def _on_straight_laser(self) -> None:
        mode = self.straight_box.currentData()
        if isinstance(mode, str):
            self.settings.setValue("straight_laser", mode)

    def _on_laser_strength(self, value: int) -> None:
        self.laser_value.setText(str(value))
        self.settings.setValue("laser_strength", value)

    def _convert(self) -> None:
        song = self._current_song()
        if song is None or song.folder is None or self.output_dir is None:
            QMessageBox.information(self, "转换铺面", "请先选择歌曲和铺面输出文件夹")
            return
        modes = self._selected_modes()
        if not modes:
            QMessageBox.information(self, "转换铺面", "请至少勾选 4K 或 6K")
            return
        arrange = self._selected_arrange()
        jobs = [
            (self.output_dir / project_folder_name(song.output_name, six_key, arrange), six_key)
            for six_key in modes
        ]
        self._converting_name = "、".join(dest.name for dest, _six_key in jobs)
        self._converting = True
        strength = self.laser_slider.value()
        straight = self.straight_box.currentData()
        if not isinstance(straight, str):
            straight = DEFAULT_STRAIGHT_LASER
        jacket_diff = self.jacket_diff.isChecked()
        seed = random.SystemRandom().randrange(1, 2**31)
        targets = "\n".join(f"→ {dest}" for dest, _six_key in jobs)
        self.log.setPlainText(
            f"开始转换 {song.label}\n{targets}\n物量抑制 {strength}\n直线激光 {straight}\n"
            f"曲绘差分 {'开' if jacket_diff else '关'}\n"
            f"4K {'开' if False in modes else '关'}\n"
            f"6K {'开' if True in modes else '关'}\n"
            f"配置 {arrange}\nseed {seed}"
        )
        self.status_label.setText(f"正在转换 {self._converting_name}")
        self._refresh_convert_button()
        threading.Thread(
            target=self._convert_thread,
            args=(song, jobs, laser_epsilon(strength), straight, jacket_diff, arrange, seed),
            name="sdvx-convert",
            daemon=True,
        ).start()

    def _convert_thread(
        self,
        song: Song,
        jobs: list[tuple[Path, bool]],
        laser_epsilon: float,
        straight_laser: str,
        jacket_diff: bool,
        arrange: str,
        seed: int,
    ) -> None:
        folder = song.folder
        if folder is None:
            self.convert_failed.emit("这首歌没有谱面文件夹")
            return
        catalog = {
            folder.name: (song.title, song.artist, song.credit_map()),
        }
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                for dest, six_key in jobs:
                    _convert_song(
                        folder,
                        dest,
                        catalog,
                        media=True,
                        laser_epsilon=laser_epsilon,
                        straight_laser=straight_laser,
                        jacket_diff=jacket_diff,
                        six_key=six_key,
                        arrange=arrange,
                        seed=seed,
                    )
        except Exception:
            self.convert_failed.emit(buffer.getvalue() + traceback.format_exc())
            return
        self.convert_finished.emit(buffer.getvalue().strip())

    def _on_converted(self, log: str) -> None:
        if log:
            self.log.appendPlainText(log)
        self.status_label.setText(f"转换完成：{self._converting_name}")
        self._converting = False
        self._refresh_convert_button()

    def _on_convert_failed(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.status_label.setText("转换失败")
        self._converting = False
        self._refresh_convert_button()
        QMessageBox.critical(self, "转换失败", message[-1200:])

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
