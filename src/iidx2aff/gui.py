"""Desktop shell for picking an IIDX song and converting its charts."""

from __future__ import annotations

import contextlib
import io
import random
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path

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
    QUrl,
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
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from iidx2aff.__main__ import DIFFICULTY_COUNT, _copy_media, _write_song
from iidx2aff.arrange import MODES, arrange_suffix
from iidx2aff.catalog import Song, format_bpm, load_songs, version_text
from iidx2aff.iidx import SP_HARD_TO_EASY, SP_NAMES, read_charts
from iidx2aff.ifs import extract

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
except ImportError:  # pragma: no cover - optional at runtime
    QAudioOutput = None
    QMediaPlayer = None
    QVideoWidget = None

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
QFrame#cover, QFrame#bga {
    background: #23272f;
    border: 1px solid #343a44;
    border-radius: 8px;
}
QSplitter::handle { background: #2a2f38; }
QStatusBar { color: #b7bdc7; }
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
        self.genre: str | None = None
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
        if self.genre is not None and song.genre != self.genre:
            return False
        if self.local_only and not song.has_chart:
            return False
        if self.search:
            blob = "\n".join(
                (
                    song.folder_id,
                    str(song.song_id),
                    song.title,
                    song.artist,
                    song.ascii_name,
                    song.genre,
                    song.title_yomi,
                    song.artist_yomi,
                    song.subtitle,
                )
            ).casefold()
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
        return ((song.title_yomi or song.title).casefold(), song.song_id)
    if key == "artist":
        return ((song.artist_yomi or song.artist).casefold(), song.song_id)
    if key == "bpm":
        return (song.bpm_max <= 0, song.bpm_max, song.song_id)
    if key == "version":
        return (song.version, song.song_id)
    if key == "level":
        return (song.peak_level <= 0, song.peak_level, song.song_id)
    return (song.song_id,)


class MainWindow(QMainWindow):
    songs_ready = Signal(int, object, object)
    songs_failed = Signal(int, bool, str)
    convert_finished = Signal(str)
    convert_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("iidx2aff")
        self.resize(1180, 760)
        self.settings = QSettings("vox2aff", "iidx2aff")
        self.data_dir: Path | None = None
        self.output_dir: Path | None = None
        self.songs: list[Song] = []
        self._load_generation = 0
        self._converting = False
        self._converting_name = ""
        self._filling_table = False
        self._player = None
        self._audio = None
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

        layout.addLayout(self._path_row("IIDX data 文件夹", "data"))
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
        self.status_label = QLabel("请选择 IIDX 的 data 文件夹")
        status.addWidget(self.status_label, 1)

    def _path_row(self, title: str, kind: str) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(title)
        label.setMinimumWidth(140)
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
        self.search_edit.setPlaceholderText("搜索 id、曲名、艺术家、流派")
        self.search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_edit)

        filters = QHBoxLayout()
        self.sort_box = QComboBox()
        self.sort_box.addItem("按 ID", "id")
        self.sort_box.addItem("按曲名", "title")
        self.sort_box.addItem("按艺术家", "artist")
        self.sort_box.addItem("按 BPM", "bpm")
        self.sort_box.addItem("按版本", "version")
        self.sort_box.addItem("按难度", "level")
        self.sort_box.currentIndexChanged.connect(self._apply_filter)
        self.version_box = QComboBox()
        self.version_box.addItem("全部版本", None)
        self.version_box.currentIndexChanged.connect(self._apply_filter)
        self.genre_box = QComboBox()
        self.genre_box.addItem("全部流派", None)
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
        self.genre_label = QLabel("—")
        self.version_label = QLabel("—")
        self.bpm_label = QLabel("—")
        self.subtitle_label = QLabel("—")
        self.output_label = QLabel("—")
        self.output_label.setWordWrap(True)
        self.subtitle_label.setWordWrap(True)
        for label in (
            self.id_label,
            self.ascii_label,
            self.genre_label,
            self.version_label,
            self.bpm_label,
            self.subtitle_label,
            self.output_label,
        ):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("ID", self.id_label)
        form.addRow("ASCII", self.ascii_label)
        form.addRow("流派", self.genre_label)
        form.addRow("版本", self.version_label)
        form.addRow("BPM", self.bpm_label)
        self.subtitle_row = form.rowCount()
        form.addRow("副标题", self.subtitle_label)
        form.addRow("输出", self.output_label)
        self.form = form
        info.addLayout(form)
        info.addStretch(1)
        header.addLayout(info, 1)
        layout.addLayout(header)

        self.bga_frame = QFrame()
        self.bga_frame.setObjectName("bga")
        self.bga_frame.setVisible(False)
        bga_layout = QVBoxLayout(self.bga_frame)
        bga_layout.setContentsMargins(0, 0, 0, 0)
        if QVideoWidget is not None and QMediaPlayer is not None and QAudioOutput is not None:
            self.video = QVideoWidget()
            self.video.setMinimumHeight(200)
            bga_layout.addWidget(self.video)
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setVolume(0.5)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self.video)
        else:
            self.video = None
        layout.addWidget(self.bga_frame)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["转换", "模式", "难度", "等级", "谱面"])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(180)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)

        side_row = QHBoxLayout()
        side_row.addWidget(QLabel("玩家"))
        self.side_box = QComboBox()
        self.side_box.addItem("1P（盘子在 1 轨左侧）", "1p")
        self.side_box.addItem("2P（盘子在 4 轨右侧）", "2p")
        self.side_box.setToolTip("白键顺序不变。1P 的盘子在最左，2P 的盘子在最右")
        self.side_box.currentIndexChanged.connect(self._on_side)
        side_row.addWidget(self.side_box, 1)
        layout.addLayout(side_row)

        arrange_row = QHBoxLayout()
        arrange_row.addWidget(QLabel("配置"))
        self.arrange_box = QComboBox()
        self.arrange_box.addItem("关闭", "off")
        self.arrange_box.addItem("MIRROR", "mirror")
        self.arrange_box.addItem("R-RANDOM", "r-random")
        self.arrange_box.addItem("RANDOM", "random")
        self.arrange_box.addItem("S-RANDOM", "s-random")
        self.arrange_box.setToolTip(
            "只改 1–7 键，盘子不动。"
            "MIRROR 左右对调。"
            "R-RANDOM 在 12 种旋转里抽，不会抽到正规或纯镜像。"
            "RANDOM 整列打乱。"
            "S-RANDOM 每个音符单独换列"
        )
        self.arrange_box.currentIndexChanged.connect(self._on_arrange)
        arrange_row.addWidget(self.arrange_box, 1)
        layout.addLayout(arrange_row)

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
        stored_side = self.settings.value("side", "1p")
        side_index = self.side_box.findData(str(stored_side))
        self.side_box.setCurrentIndex(side_index if side_index >= 0 else 0)
        stored_arrange = self.settings.value("arrange", "off")
        arrange_index = self.arrange_box.findData(str(stored_arrange))
        self.arrange_box.setCurrentIndex(arrange_index if arrange_index >= 0 else 0)
        self._refresh_convert_button()

    def _browse(self, kind: str) -> None:
        current = self.data_edit.text() if kind == "data" else self.output_edit.text()
        start = current or str(Path.home())
        title = "选择 IIDX data 文件夹" if kind == "data" else "选择铺面输出文件夹"
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
            name="iidx-song-load",
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
        self._fill_filters(songs)
        self.model.set_songs(songs)
        self._apply_filter()
        with_chart = sum(song.has_chart for song in songs)
        with_movie = sum(song.movie is not None for song in songs)
        movie_note = f"，{with_movie} 首有 BGA" if with_movie else "，data 里没有 movie"
        self.status_label.setText(f"已加载 {len(songs)} 首歌曲，{with_chart} 首有谱面{movie_note}")

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

    def _fill_filters(self, songs: list[Song]) -> None:
        self.version_box.blockSignals(True)
        self.genre_box.blockSignals(True)
        current_version = self.version_box.currentData()
        current_genre = self.genre_box.currentData()
        self.version_box.clear()
        self.version_box.addItem("全部版本", None)
        for number in sorted({song.version for song in songs}):
            self.version_box.addItem(version_text(number), number)
        self.genre_box.clear()
        self.genre_box.addItem("全部流派", None)
        for name in sorted({song.genre for song in songs if song.genre}):
            self.genre_box.addItem(name, name)
        version_index = self.version_box.findData(current_version)
        genre_index = self.genre_box.findData(current_genre)
        self.version_box.setCurrentIndex(version_index if version_index >= 0 else 0)
        self.genre_box.setCurrentIndex(genre_index if genre_index >= 0 else 0)
        self.version_box.blockSignals(False)
        self.genre_box.blockSignals(False)

    def _apply_filter(self) -> None:
        self.proxy.beginFilterChange()
        self.proxy.search = self.search_edit.text().strip()
        self.proxy.version = self.version_box.currentData()
        self.proxy.genre = self.genre_box.currentData()
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
        self.table.setRowCount(0)
        if song is None:
            self.title_label.setText("未选择歌曲")
            self.artist_label.setText("")
            for label in (
                self.id_label,
                self.ascii_label,
                self.genre_label,
                self.version_label,
                self.bpm_label,
                self.subtitle_label,
                self.output_label,
            ):
                label.setText("—")
            self.form.setRowVisible(self.subtitle_row, False)
            self._set_cover(None)
            self._set_movie(None)
            self.note_label.setText("")
            self._refresh_convert_button()
            return

        self.title_label.setText(song.title)
        self.artist_label.setText(song.artist)
        self.id_label.setText(song.folder_id)
        self.ascii_label.setText(song.ascii_name or "—")
        self.genre_label.setText(song.genre or "—")
        self.version_label.setText(version_text(song.version))
        bpm = format_bpm(song.bpm_max)
        low = format_bpm(song.bpm_min)
        if not bpm:
            self.bpm_label.setText("—" if not song.has_chart else "谱面里没有 BPM")
        else:
            self.bpm_label.setText(bpm if bpm == low or not low else f"{low} – {bpm}")
        self.subtitle_label.setText(song.subtitle or "—")
        self.form.setRowVisible(self.subtitle_row, bool(song.subtitle))
        self._show_output(song)
        self._set_cover(song.jacket)
        self._set_movie(song.movie)

        rows = song.chart_rows()
        self._filling_table = True
        self.table.setRowCount(len(rows))
        for row, info in enumerate(rows):
            self.table.setCellWidget(row, 0, self._slot_checkbox(info.side, info.slot, info.name, info.present))
            values = [info.side, info.name, str(info.level) if info.level else "—", "有" if info.present else "无"]
            for column, text in enumerate(values, start=1):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self._filling_table = False

        if not song.has_chart:
            self.note_label.setText("sound 文件夹里没有这首歌的 .1 谱面")
        else:
            self._show_selection_note(song)
        self._refresh_convert_button()

    def _slot_checkbox(self, side: str, slot: int, name: str, present: bool) -> QWidget:
        box = QCheckBox()
        box.setProperty("slot", slot)
        box.setProperty("name", name)
        playable = side == "SP" and present
        box.setEnabled(playable)
        box.setChecked(playable)
        if side == "DP":
            box.setToolTip("DP 的映射还没定")
        elif not present:
            box.setToolTip("没有谱面文件")
        box.toggled.connect(self._on_slot_toggled)
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(box)
        return holder

    def _checkboxes(self) -> list[QCheckBox]:
        boxes: list[QCheckBox] = []
        for row in range(self.table.rowCount()):
            holder = self.table.cellWidget(row, 0)
            if holder is None:
                continue
            box = holder.findChild(QCheckBox)
            if box is not None:
                boxes.append(box)
        return boxes

    def _selected_slots(self) -> list[int]:
        easy_to_hard = tuple(reversed(SP_HARD_TO_EASY))
        chosen: set[int] = set()
        for box in self._checkboxes():
            slot = box.property("slot")
            if box.isChecked() and isinstance(slot, int):
                chosen.add(slot)
        return [slot for slot in easy_to_hard if slot in chosen]

    def _on_slot_toggled(self, checked: bool) -> None:
        if self._filling_table:
            return
        if checked and len(self._selected_slots()) > DIFFICULTY_COUNT:
            box = self.sender()
            if isinstance(box, QCheckBox):
                box.blockSignals(True)
                box.setChecked(False)
                box.blockSignals(False)
            self.status_label.setText(f"最多转换 {DIFFICULTY_COUNT} 个难度")
        song = self._current_song()
        if song is not None and song.has_chart:
            self._show_selection_note(song)
        self._refresh_convert_button()

    def _show_selection_note(self, song: Song) -> None:
        chosen = [
            (str(box.property("name")), box.property("slot"))
            for box in self._checkboxes()
            if box.isChecked()
        ]
        slots = self._selected_slots()
        names = [name for name, slot in chosen if slot in slots]
        if not names:
            self.note_label.setText("请至少勾选一个 SP 难度")
            return
        files = "、".join(f"{index}.aff" for index in range(len(names)))
        folder = song.output_name + arrange_suffix(self._selected_arrange())
        self.note_label.setText(f"输出 {folder}：" + "、".join(names) + f" → {files}")

    def _output_folder(self, song: Song) -> str:
        return song.output_name + arrange_suffix(self._selected_arrange())

    def _show_output(self, song: Song) -> None:
        name = self._output_folder(song)
        if self.output_dir is None:
            self.output_label.setText(name)
        else:
            self.output_label.setText(str(self.output_dir / name))

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

    def _set_movie(self, path: Path | None) -> None:
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QUrl())
        if path is None or self.video is None:
            self.bga_frame.setVisible(False)
            return
        self.bga_frame.setVisible(True)
        if self._player is not None:
            self._player.setSource(QUrl.fromLocalFile(str(path)))
            self._player.play()

    def _refresh_convert_button(self) -> None:
        song = self._current_song()
        ready = (
            song is not None
            and song.has_chart
            and self.output_dir is not None
            and bool(self._selected_slots())
            and not self._converting
        )
        self.convert_button.setEnabled(ready)
        self.convert_button.setText("正在转换…" if self._converting else "转换铺面")

    def _thumbs_dir(self) -> Path | None:
        if self.data_dir is None:
            return None
        from iidx2aff.catalog import find_data_root

        folder = find_data_root(self.data_dir) / "graphic" / "thumbnail"
        return folder if folder.is_dir() else None

    def _convert(self) -> None:
        song = self._current_song()
        slots = self._selected_slots()
        if song is None or not song.has_chart or self.output_dir is None:
            QMessageBox.information(self, "转换铺面", "请先选择歌曲和铺面输出文件夹")
            return
        if not slots:
            QMessageBox.information(self, "转换铺面", "请至少勾选一个 SP 难度")
            return
        arrange = self._selected_arrange()
        dest = self.output_dir / self._output_folder(song)
        self._converting_name = dest.name
        self._converting = True
        names = "、".join(SP_NAMES[slot] for slot in slots)
        side = self._selected_side()
        seed = random.SystemRandom().randrange(1, 2**31)
        self.log.setPlainText(
            f"开始转换 {song.label}\n{side.upper()}\n{names}\n配置 {arrange}\nseed {seed}\n→ {dest}"
        )
        self.status_label.setText(f"正在转换 {self._converting_name}")
        self._refresh_convert_button()
        thumbs = self._thumbs_dir()
        threading.Thread(
            target=self._convert_thread,
            args=(song, dest, thumbs, slots, side, arrange, seed),
            name="iidx-convert",
            daemon=True,
        ).start()

    def _convert_thread(
        self,
        song: Song,
        dest: Path,
        thumbs: Path | None,
        slots: list[int],
        side: str,
        arrange: str,
        seed: int,
    ) -> None:
        buffer = io.StringIO()
        scratch: Path | None = None
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                source, chart, scratch = _song_files(song)
                charts = read_charts(chart.read_bytes())
                sounds = _write_song(
                    chart,
                    charts,
                    dest,
                    song.title,
                    song.artist,
                    song.rating_map(),
                    slots,
                    side,
                    arrange,
                    seed,
                )
                _copy_media(song.folder_id, source, dest, thumbs, sounds)
        except Exception:
            self.convert_failed.emit(buffer.getvalue() + traceback.format_exc())
            return
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
        self.convert_finished.emit(buffer.getvalue().strip())

    def _selected_side(self) -> str:
        side = self.side_box.currentData()
        return side if isinstance(side, str) else "1p"

    def _on_side(self) -> None:
        self.settings.setValue("side", self._selected_side())

    def _selected_arrange(self) -> str:
        mode = self.arrange_box.currentData()
        if not isinstance(mode, str) or mode not in MODES:
            return "off"
        return mode

    def _on_arrange(self) -> None:
        self.settings.setValue("arrange", self._selected_arrange())
        song = self._current_song()
        if song is None:
            return
        self._show_output(song)
        if song.has_chart:
            self._show_selection_note(song)

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

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._player is not None:
            self._player.stop()
        super().closeEvent(event)


def _song_files(song: Song) -> tuple[Path, Path, Path | None]:
    if song.folder is not None and song.chart_path is not None:
        return song.folder, song.chart_path, None
    if song.pack is None:
        raise FileNotFoundError("这首歌没有谱面文件")
    root = Path(tempfile.mkdtemp())
    extract(song.pack, root)
    charts = sorted(root.rglob("*.1"))
    if not charts:
        shutil.rmtree(root, ignore_errors=True)
        raise FileNotFoundError(f"{song.pack.name} 里没有 .1 谱面")
    return charts[0].parent, charts[0], root


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
