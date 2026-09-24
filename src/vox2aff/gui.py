"""Desktop shell for picking an SDVX song and converting its charts."""

from __future__ import annotations

import contextlib
import io
import sys
import traceback
from pathlib import Path

# ``src/`` is the import root, so ``import vox2aff`` works when this file is launched directly.
_src = Path(__file__).resolve().parents[1]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QThread,
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

from vox2aff.__main__ import _convert_song
from vox2aff.catalog import (
    GENRE_BITS,
    VERSIONS,
    Song,
    format_bpm,
    format_date,
    genre_text,
    load_songs,
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
    return (song.number,)


class ConvertWorker(QObject):
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, song: Song, dest: Path) -> None:
        super().__init__()
        self.song = song
        self.dest = dest

    def run(self) -> None:
        folder = self.song.folder
        if folder is None:
            self.failed.emit("这首歌没有谱面文件夹")
            return
        catalog = {
            folder.name: (self.song.title, self.song.artist, self.song.rating_map()),
        }
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                _convert_song(folder, self.dest, catalog, media=True)
        except Exception:
            self.failed.emit(buffer.getvalue() + traceback.format_exc())
            return
        self.finished.emit(buffer.getvalue().strip())


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("vox2aff")
        self.resize(1180, 760)
        self.settings = QSettings("vox2aff", "vox2aff")
        self.data_dir: Path | None = None
        self.output_dir: Path | None = None
        self.songs: list[Song] = []
        self._thread: QThread | None = None
        self._worker: ConvertWorker | None = None

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
            self._set_data_dir(Path(data), quiet=True)
        if isinstance(output, str) and output:
            self.output_dir = Path(output)
            self.output_edit.setText(output)
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
            self._set_data_dir(path, quiet=False)
            self.settings.setValue("data_dir", str(path))
        else:
            self.output_dir = path
            self.output_edit.setText(str(path))
            self.settings.setValue("output_dir", str(path))
            self._show_song(self._current_song())
            self._refresh_convert_button()

    def _set_data_dir(self, path: Path, quiet: bool) -> None:
        try:
            songs = load_songs(path)
        except Exception as exc:
            if not quiet:
                QMessageBox.warning(self, "无法读取 data 文件夹", str(exc))
            self.status_label.setText(str(exc))
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

    def _apply_filter(self) -> None:
        self.proxy.beginFilterChange()
        self.proxy.search = self.search_edit.text().strip()
        self.proxy.version = self.version_box.currentData()
        self.proxy.genre_bit = self.genre_box.currentData()
        self.proxy.local_only = self.local_only.isChecked()
        self.proxy.endFilterChange()
        self.proxy.sort_key = self.sort_box.currentData() or "id"
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
        if self.output_dir is not None:
            self.output_label.setText(str(self.output_dir / song.output_name))
        else:
            self.output_label.setText(song.output_name)

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
            self.note_label.setText(f"输出文件夹名：{song.output_name}")
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
        busy = self._thread is not None
        ready = (
            song is not None
            and song.folder is not None
            and bool(song.charts)
            and self.output_dir is not None
            and not busy
        )
        self.convert_button.setEnabled(ready)
        self.convert_button.setText("正在转换…" if busy else "转换铺面")

    def _convert(self) -> None:
        song = self._current_song()
        if song is None or song.folder is None or self.output_dir is None:
            QMessageBox.information(self, "转换铺面", "请先选择歌曲和铺面输出文件夹")
            return
        dest = self.output_dir / song.output_name
        self.log.setPlainText(f"开始转换 {song.label}\n→ {dest}")
        self.status_label.setText(f"正在转换 {song.output_name}")
        thread = QThread(self)
        worker = ConvertWorker(song, dest)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_converted)
        worker.failed.connect(self._on_convert_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        self._worker = worker
        self._refresh_convert_button()
        thread.start()

    def _on_converted(self, log: str) -> None:
        if log:
            self.log.appendPlainText(log)
        song = self._current_song()
        name = song.output_name if song is not None else ""
        self.status_label.setText(f"转换完成：{name}")

    def _on_convert_failed(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.status_label.setText("转换失败")
        QMessageBox.critical(self, "转换失败", message[-1200:])

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._refresh_convert_button()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
