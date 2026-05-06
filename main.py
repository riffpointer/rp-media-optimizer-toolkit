from __future__ import annotations

# SPDX-License-Identifier: MPL-2.0

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QRectF, Qt, QThread, QTimer, Signal, QSize, QUrl
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPixmap, QPixmapCache
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QInputDialog,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QStatusBar,
    QStyle,
    QStyleFactory,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "RP Media Optimizer Toolkit"
APP_AUTHOR = "RiffPointer"
APP_LICENSE = "Mozilla Public License 2.0"
IMAGE_EXTENSIONS = {".png"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".oga", ".flac", ".aac", ".m4a"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
DEFAULT_SCAN_DIRS = ("textures", "models", "materials", "maps", "sounds", "videos")
SKIP_DIRS = {".git", ".godot", "build", "android", "__pycache__"}
LARGE_FILE_BYTES = 256 * 1024
HIGH_RES_EDGE = 2048


@dataclass(frozen=True)
class ImageAsset:
    path: Path
    relative_path: str
    size_bytes: int
    width: int
    height: int

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000

    @property
    def is_high_res(self) -> bool:
        return max(self.width, self.height) > HIGH_RES_EDGE

    @property
    def recommendation(self) -> str:
        if self.is_high_res and self.size_bytes >= LARGE_FILE_BYTES:
            return "Downscale + optimize"
        if self.is_high_res:
            return "Downscale"
        if self.size_bytes >= LARGE_FILE_BYTES:
            return "Optimize"
        return "Optional"


@dataclass(frozen=True)
class MediaAsset:
    path: Path
    relative_path: str
    size_bytes: int
    duration: float | None
    codec: str
    bitrate: int | None
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    @property
    def recommendation(self) -> str:
        suffix = self.path.suffix.lower()
        if suffix == ".wav":
            return "Convert to Ogg"
        if self.size_bytes >= 5 * 1024 * 1024:
            return "Compress"
        if suffix in {".avi", ".mov", ".mkv"}:
            return "Repack/compress"
        return "Optional"


@dataclass(frozen=True)
class ImageOptions:
    downscale_enabled: bool
    target_long_edge: int
    resample_name: str
    preserve_metadata: bool
    create_backup: bool
    run_pngquant: bool
    pngquant_quality: str
    run_optipng: bool


@dataclass(frozen=True)
class AudioOptions:
    output_format: str
    codec: str
    bitrate: str
    sample_rate: str
    channels: str
    normalize: bool
    trim_silence: bool
    create_backup: bool
    replace_same_extension: bool


@dataclass(frozen=True)
class VideoOptions:
    output_format: str
    video_codec: str
    quality_mode: str
    quality_value: int
    preset: str
    audio_codec: str
    audio_bitrate: str
    scale_long_edge: int
    fps: str
    strip_audio: bool
    faststart: bool
    create_backup: bool
    replace_same_extension: bool


class ScanWorker(QObject):
    finished = Signal(list, list, list)
    progress = Signal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = root

    def run(self) -> None:
        images: list[ImageAsset] = []
        audio: list[MediaAsset] = []
        video: list[MediaAsset] = []

        for path in iter_candidate_files(self.root):
            suffix = path.suffix.lower()
            self.progress.emit(path.name)
            try:
                if suffix in IMAGE_EXTENSIONS:
                    asset = scan_image(path, self.root)
                    if asset:
                        images.append(asset)
                elif suffix in AUDIO_EXTENSIONS:
                    audio.append(scan_media(path, self.root, is_video=False))
                elif suffix in VIDEO_EXTENSIONS:
                    video.append(scan_media(path, self.root, is_video=True))
            except Exception:
                continue

        images.sort(key=lambda item: (item.is_high_res, item.size_bytes), reverse=True)
        audio.sort(key=lambda item: item.size_bytes, reverse=True)
        video.sort(key=lambda item: item.size_bytes, reverse=True)
        self.finished.emit(images, audio, video)


class ImageConvertWorker(QObject):
    finished = Signal(int, int, list)
    progress = Signal(int, str)

    def __init__(self, assets: list[ImageAsset], options: ImageOptions):
        super().__init__()
        self.assets = assets
        self.options = options
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        run_batch(self.assets, self.options, optimize_png, self.progress, self.finished, lambda: self._abort)


class AudioConvertWorker(QObject):
    finished = Signal(int, int, list)
    progress = Signal(int, str)

    def __init__(self, assets: list[MediaAsset], options: AudioOptions):
        super().__init__()
        self.assets = assets
        self.options = options
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        run_batch(self.assets, self.options, convert_audio, self.progress, self.finished, lambda: self._abort)


class VideoConvertWorker(QObject):
    finished = Signal(int, int, list)
    progress = Signal(int, str)

    def __init__(self, assets: list[MediaAsset], options: VideoOptions):
        super().__init__()
        self.assets = assets
        self.options = options
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        run_batch(self.assets, self.options, convert_video, self.progress, self.finished, lambda: self._abort)


class BatchProgressDialog(QDialog):
    canceled = Signal()

    def __init__(self, title: str, total: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.total = total
        self.started_at = time.monotonic()
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.setModal(True)

        self.file_label = QLabel("Starting...")
        self.file_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, total)
        self.info_label = QLabel("Calculating time remaining...")
        self.info_label.setAlignment(Qt.AlignCenter)
        self.info_label.setStyleSheet("color: #777;")

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.canceled.emit)
        self.cancel_button.clicked.connect(lambda: self.cancel_button.setEnabled(False))
        self.cancel_button.clicked.connect(lambda: self.cancel_button.setText("Canceling..."))

        layout = QVBoxLayout(self)
        layout.addWidget(self.file_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.info_label)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.cancel_button)
        layout.addLayout(button_layout)

    def update_progress(self, completed: int, current_path: str) -> None:
        self.progress_bar.setValue(completed)
        self.file_label.setText(current_path)
        elapsed = max(0.1, time.monotonic() - self.started_at)
        rate_per_minute = completed / elapsed * 60 if completed else 0.0
        remaining = self.total - completed
        seconds_left = remaining / (completed / elapsed) if completed else None
        if seconds_left is None:
            remaining_text = "Calculating time remaining..."
        else:
            remaining_text = f"{format_duration(seconds_left)} remaining"
        self.info_label.setText(f"{rate_per_minute:.1f} files/minute - {remaining_text}")


class ImageOptionsDialog(QDialog):
    def __init__(self, selected_assets: list[ImageAsset], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Image Conversion Options")
        self.setMinimumWidth(460)

        largest_edge = max((max(asset.width, asset.height) for asset in selected_assets), default=HIGH_RES_EDGE)

        self.downscale_box = QCheckBox("Downscale images larger than the target edge")
        self.downscale_box.setChecked(True)
        self.target_edge_spin = QSpinBox()
        self.target_edge_spin.setRange(256, 8192)
        self.target_edge_spin.setSingleStep(256)
        self.target_edge_spin.setValue(1024)
        self.target_edge_spin.setSuffix(" px longest edge")
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["Lanczos (best quality)", "Bicubic", "Bilinear", "Nearest"])
        self.filter_combo.setCurrentText("Bilinear")
        self.metadata_box = QCheckBox("Preserve PNG metadata where possible")
        self.backup_box = QCheckBox("Create .bak copy before writing")
        self.backup_box.setChecked(False)
        self.pngquant_box = QCheckBox("Run pngquant after Pillow save if available")
        self.pngquant_box.setChecked(False)
        self.pngquant_quality = QComboBox()
        self.pngquant_quality.addItems(["90-100", "85-100", "80-100"])
        self.optipng_box = QCheckBox("Run optipng after Pillow save if available")
        self.optipng_box.setChecked(False)

        form = QFormLayout()
        form.addRow("", self.downscale_box)
        form.addRow("Target resolution", self.target_edge_spin)
        form.addRow("Resize filter", self.filter_combo)
        form.addRow("", self.metadata_box)
        form.addRow("", self.backup_box)
        form.addRow("", self.pngquant_box)
        form.addRow("pngquant quality", self.pngquant_quality)
        form.addRow("", self.optipng_box)
        self._finish_layout(form, f"{len(selected_assets)} selected files. Largest selected edge: {largest_edge}px.")

    def _finish_layout(self, form: QFormLayout, summary_text: str) -> None:
        summary = QLabel(summary_text)
        summary.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def options(self) -> ImageOptions:
        return ImageOptions(
            downscale_enabled=self.downscale_box.isChecked(),
            target_long_edge=self.target_edge_spin.value(),
            resample_name=self.filter_combo.currentText().split(" ", 1)[0].lower(),
            preserve_metadata=self.metadata_box.isChecked(),
            create_backup=self.backup_box.isChecked(),
            run_pngquant=self.pngquant_box.isChecked(),
            pngquant_quality=self.pngquant_quality.currentText(),
            run_optipng=self.optipng_box.isChecked(),
        )


class AudioOptionsDialog(QDialog):
    def __init__(self, selected_assets: list[MediaAsset], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Audio Conversion Options")
        self.setMinimumWidth(500)

        has_wav = any(asset.path.suffix.lower() == ".wav" for asset in selected_assets)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["ogg", "mp3", "wav", "flac"])
        self.format_combo.setCurrentText("ogg" if has_wav else "mp3")
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["libvorbis", "libmp3lame", "pcm_s16le", "flac"])
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["96k", "128k", "160k", "192k", "256k"])
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["keep", "48000", "44100", "32000", "22050"])
        self.channels_combo = QComboBox()
        self.channels_combo.addItems(["keep", "mono", "stereo"])
        self.normalize_box = QCheckBox("Normalize loudness")
        self.trim_box = QCheckBox("Trim leading/trailing silence")
        self.backup_box = QCheckBox("Create .bak copy before replacing")
        self.backup_box.setChecked(True)
        self.replace_box = QCheckBox("Replace source only when output extension is unchanged")
        self.replace_box.setChecked(True)
        self.format_combo.currentTextChanged.connect(self.apply_codec_default)
        self.apply_codec_default(self.format_combo.currentText())

        form = QFormLayout()
        form.addRow("Output format", self.format_combo)
        form.addRow("Codec", self.codec_combo)
        form.addRow("Target bitrate", self.bitrate_combo)
        form.addRow("Sample rate", self.sample_rate_combo)
        form.addRow("Channels", self.channels_combo)
        form.addRow("", self.normalize_box)
        form.addRow("", self.trim_box)
        form.addRow("", self.backup_box)
        form.addRow("", self.replace_box)
        finish_dialog_layout(self, form, f"{len(selected_assets)} selected audio files.")

    def apply_codec_default(self, output_format: str) -> None:
        defaults = {"ogg": "libvorbis", "mp3": "libmp3lame", "wav": "pcm_s16le", "flac": "flac"}
        self.codec_combo.setCurrentText(defaults.get(output_format, "libvorbis"))

    def options(self) -> AudioOptions:
        return AudioOptions(
            output_format=self.format_combo.currentText(),
            codec=self.codec_combo.currentText(),
            bitrate=self.bitrate_combo.currentText(),
            sample_rate=self.sample_rate_combo.currentText(),
            channels=self.channels_combo.currentText(),
            normalize=self.normalize_box.isChecked(),
            trim_silence=self.trim_box.isChecked(),
            create_backup=self.backup_box.isChecked(),
            replace_same_extension=self.replace_box.isChecked(),
        )


class VideoOptionsDialog(QDialog):
    def __init__(self, selected_assets: list[MediaAsset], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Video Conversion Options")
        self.setMinimumWidth(520)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["mp4", "webm", "mkv"])
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["libx264", "libx265", "libvpx-vp9", "libaom-av1"])
        self.quality_mode_combo = QComboBox()
        self.quality_mode_combo.addItems(["CRF", "Target bitrate"])
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(12, 40)
        self.quality_spin.setValue(24)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["slow", "medium", "fast", "veryfast"])
        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItems(["aac", "libopus", "libvorbis", "copy"])
        self.audio_bitrate_combo = QComboBox()
        self.audio_bitrate_combo.addItems(["96k", "128k", "160k", "192k"])
        self.scale_spin = QSpinBox()
        self.scale_spin.setRange(0, 8192)
        self.scale_spin.setSingleStep(256)
        self.scale_spin.setValue(suggest_video_edge(selected_assets))
        self.scale_spin.setSpecialValueText("Keep original")
        self.scale_spin.setSuffix(" px longest edge")
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["keep", "60", "30", "24"])
        self.strip_audio_box = QCheckBox("Remove audio track")
        self.faststart_box = QCheckBox("Move MP4 metadata to file start")
        self.faststart_box.setChecked(True)
        self.backup_box = QCheckBox("Create .bak copy before replacing")
        self.backup_box.setChecked(True)
        self.replace_box = QCheckBox("Replace source only when output extension is unchanged")
        self.replace_box.setChecked(True)
        self.quality_mode_combo.currentTextChanged.connect(self.update_quality_suffix)
        self.update_quality_suffix(self.quality_mode_combo.currentText())

        form = QFormLayout()
        form.addRow("Output format", self.format_combo)
        form.addRow("Video codec", self.codec_combo)
        form.addRow("Quality mode", self.quality_mode_combo)
        form.addRow("Quality value", self.quality_spin)
        form.addRow("Encode preset", self.preset_combo)
        form.addRow("Audio codec", self.audio_codec_combo)
        form.addRow("Audio bitrate", self.audio_bitrate_combo)
        form.addRow("Scale", self.scale_spin)
        form.addRow("Frame rate", self.fps_combo)
        form.addRow("", self.strip_audio_box)
        form.addRow("", self.faststart_box)
        form.addRow("", self.backup_box)
        form.addRow("", self.replace_box)
        finish_dialog_layout(self, form, f"{len(selected_assets)} selected video files.")

    def update_quality_suffix(self, mode: str) -> None:
        if mode == "CRF":
            self.quality_spin.setRange(12, 40)
            self.quality_spin.setValue(min(self.quality_spin.value(), 28))
            self.quality_spin.setSuffix(" CRF")
        else:
            self.quality_spin.setRange(250, 20000)
            self.quality_spin.setSingleStep(250)
            self.quality_spin.setValue(2500)
            self.quality_spin.setSuffix(" kbps")

    def options(self) -> VideoOptions:
        return VideoOptions(
            output_format=self.format_combo.currentText(),
            video_codec=self.codec_combo.currentText(),
            quality_mode=self.quality_mode_combo.currentText(),
            quality_value=self.quality_spin.value(),
            preset=self.preset_combo.currentText(),
            audio_codec=self.audio_codec_combo.currentText(),
            audio_bitrate=self.audio_bitrate_combo.currentText(),
            scale_long_edge=self.scale_spin.value(),
            fps=self.fps_combo.currentText(),
            strip_audio=self.strip_audio_box.isChecked(),
            faststart=self.faststart_box.isChecked(),
            create_backup=self.backup_box.isChecked(),
            replace_same_extension=self.replace_box.isChecked(),
        )


TOOLS_CONFIG = {
    "ffmpeg": {
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "binaries": ["ffmpeg.exe", "ffprobe.exe"],
        "description": "Essential for audio and video conversion.",
        "extract_all": True,
    },
    "optipng": {
        "url": "https://downloads.sourceforge.net/project/optipng/OptiPNG/optipng-0.7.8/optipng-0.7.8-win64.zip",
        "binaries": ["optipng.exe"],
        "description": "Optimizes PNG files without losing quality.",
        "extract_all": False,
    },
    "pngquant": {
        "url": "https://pngquant.org/pngquant-windows-binary.zip",
        "binaries": ["pngquant.exe"],
        "description": "Reduces PNG file size by converting to 8-bit palette.",
        "extract_all": False,
    },
}


class ToolDownloadWorker(QObject):
    finished = Signal(bool, str)
    progress = Signal(int, int, float, float)  # downloaded, total, speed (B/s), remaining (s)

    def __init__(self, tool_id: str):
        super().__init__()
        self.tool_id = tool_id
        self._abort = False
        self._response = None

    def abort(self) -> None:
        self._abort = True
        if self._response:
            try:
                # Closing the response will break a blocking read() in the worker thread
                self._response.close()
            except:
                pass

    def run(self) -> None:
        config = TOOLS_CONFIG.get(self.tool_id)
        if not config:
            self.finished.emit(False, "Invalid tool ID")
            return

        bin_dir = Path(sys.argv[0]).parent / "bin"
        bin_dir.mkdir(exist_ok=True)

        try:
            url = config["url"]
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as response:
                self._response = response
                total_size = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                start_time = time.monotonic()
                
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
                try:
                    while True:
                        if self._abort:
                            temp_file.close()
                            if os.path.exists(temp_file.name):
                                os.unlink(temp_file.name)
                            self.finished.emit(False, "Download canceled")
                            return
                        
                        try:
                            chunk = response.read(16 * 1024)
                        except (OSError, ValueError, Exception):
                            if self._abort:
                                break
                            raise
                            
                        if not chunk:
                            break
                        
                        temp_file.write(chunk)
                        downloaded += len(chunk)
                        
                        elapsed = time.monotonic() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        remaining = (total_size - downloaded) / speed if speed > 0 else 0
                        
                        self.progress.emit(downloaded, total_size, speed, remaining)
                    
                    temp_file.close()
                    
                    if self._abort:
                        if os.path.exists(temp_file.name):
                            os.unlink(temp_file.name)
                        self.finished.emit(False, "Download canceled")
                        return
                    
                    # Extraction
                    with zipfile.ZipFile(temp_file.name, "r") as zip_ref:
                        if config["extract_all"] or self.tool_id == "ffmpeg":
                            # For ffmpeg, we need to find the binaries in subfolders
                            for file_info in zip_ref.infolist():
                                filename = os.path.basename(file_info.filename)
                                if filename in config["binaries"]:
                                    # Extract specifically the binary to bin_dir
                                    with zip_ref.open(file_info) as source, open(bin_dir / filename, "wb") as target:
                                        shutil.copyfileobj(source, target)
                        else:
                            # Direct extraction for simpler zips
                            for binary in config["binaries"]:
                                # Find the binary in the zip (it might be in a subfolder)
                                binary_path = None
                                for name in zip_ref.namelist():
                                    if name.endswith(binary):
                                        binary_path = name
                                        break
                                
                                if binary_path:
                                    with zip_ref.open(binary_path) as source, open(bin_dir / binary, "wb") as target:
                                        shutil.copyfileobj(source, target)
                                else:
                                    raise Exception(f"Could not find {binary} in the downloaded archive.")
                
                finally:
                    if os.path.exists(temp_file.name):
                        os.unlink(temp_file.name)
            
            self.finished.emit(True, f"Successfully installed {self.tool_id}")
            
        except Exception as e:
            self.finished.emit(False, str(e))


class DownloadProgressDialog(QDialog):
    def __init__(self, tool_name: str, url: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Downloading {tool_name}")
        self.setMinimumWidth(500)
        self.setModal(True)

        # Main Layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Content Area
        self.content_container = QWidget()
        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(16, 16, 16, 16)
        self.content_layout.setSpacing(12)

        self.url_label = QLabel(f"URL: {url}")
        self.progress_bar = QProgressBar()
        self.status_label = QLabel("Starting download...")
        self.stats_label = QLabel("")

        self.content_layout.addWidget(QLabel(f"<b>Downloading {tool_name}</b>"))
        self.content_layout.addWidget(self.url_label)
        self.content_layout.addSpacing(4)
        self.content_layout.addWidget(self.progress_bar)
        self.content_layout.addWidget(self.status_label)
        self.content_layout.addWidget(self.stats_label)

        # Bottom Panel
        self.bottom_panel = QWidget()
        self.bottom_panel.setObjectName("bottom_panel")
        self.bottom_panel.setStyleSheet("#bottom_panel { background-color: #ffffff; border-top: 1px solid #e2e8f0; }")
        self.bottom_layout = QHBoxLayout(self.bottom_panel)
        self.bottom_layout.setContentsMargins(16, 12, 16, 12)
        
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setMinimumWidth(80)
        self.bottom_layout.addStretch()
        self.bottom_layout.addWidget(self.cancel_button)

        self.main_layout.addWidget(self.content_container, 1)
        self.main_layout.addWidget(self.bottom_panel)

    def update_stats(self, downloaded: int, total: int, speed: float, remaining: float) -> None:
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(downloaded)
            percent = (downloaded / total) * 100
            self.status_label.setText(f"Downloaded {format_bytes(downloaded)} of {format_bytes(total)} ({percent:.1f}%)")
        else:
            self.progress_bar.setMaximum(0)
            self.status_label.setText(f"Downloaded {format_bytes(downloaded)} (total size unknown)")
        
        speed_text = f"{format_bytes(int(speed))}/s"
        time_text = f"{format_duration(remaining)} remaining" if total > 0 else "N/A"
        self.stats_label.setText(f"Speed: {speed_text} | Time: {time_text}")


class ExternalToolsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("External Tools Management")
        self.setMinimumWidth(600)
        self.setMinimumHeight(420)

        # Main Layout
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        # Content Area
        self.content_container = QWidget()
        self.content_layout = QVBoxLayout(self.content_container)
        self.content_layout.setContentsMargins(16, 16, 16, 16)
        self.content_layout.setSpacing(12)

        # Bottom Panel (Action Bar)
        self.bottom_panel = QWidget()
        self.bottom_panel.setObjectName("bottom_panel")
        self.bottom_panel.setStyleSheet("#bottom_panel { background-color: #ffffff; border-top: 1px solid #e2e8f0; }")
        self.bottom_layout = QHBoxLayout(self.bottom_panel)
        self.bottom_layout.setContentsMargins(16, 12, 16, 12)
        
        close_btn = QPushButton("Close")
        close_btn.setMinimumWidth(80)
        close_btn.clicked.connect(self.accept)
        self.bottom_layout.addStretch()
        self.bottom_layout.addWidget(close_btn)

        self.main_layout.addWidget(self.content_container, 1)
        self.main_layout.addWidget(self.bottom_panel)

        self.refresh_list()

    def refresh_list(self) -> None:
        # Clear layout
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        header_label = QLabel("<b>Manage External Dependencies</b>")
        header_label.setStyleSheet("font-size: 13px;")
        desc_label = QLabel("The toolkit uses these third-party tools for processing. You can use system-installed versions or download them locally into the toolkit folder.")
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #444;")
        
        self.content_layout.addWidget(header_label)
        self.content_layout.addWidget(desc_label)
        self.content_layout.addSpacing(10)

        for tool_id, config in TOOLS_CONFIG.items():
            self.content_layout.addWidget(self.create_tool_row(tool_id, config))
        
        self.content_layout.addStretch(1)

    def create_tool_row(self, tool_id, config) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 8, 0, 8)

        info = QVBoxLayout()
        name_label = QLabel(f"<b>{tool_id}</b>")
        desc_label = QLabel(config["description"])
        desc_label.setStyleSheet("color: #64748b; font-size: 11px;")
        info.addWidget(name_label)
        info.addWidget(desc_label)

        path = find_tool(tool_id)
        is_installed = path is not None
        
        status_text = "✅ Installed" if is_installed else "❌ Not Found"
        status_label = QLabel(status_text)
        status_label.setStyleSheet("color: #16a34a; font-weight: bold;" if is_installed else "color: #dc2626; font-weight: bold;")
        
        if is_installed:
            status_label.setToolTip(f"Located at: {path}")
        else:
            status_label.setToolTip("Tool not found on PATH or in local bin folder.")

        btn_text = "Re-download" if is_installed else "Download"
        download_btn = QPushButton(btn_text)
        download_btn.clicked.connect(lambda: self.start_download(tool_id))

        layout.addLayout(info, 1)
        layout.addWidget(status_label)
        layout.addSpacing(12)
        layout.addWidget(download_btn)
        
        return row

    def start_download(self, tool_id) -> None:
        config = TOOLS_CONFIG[tool_id]
        progress_dialog = DownloadProgressDialog(tool_id, config["url"], self)
        
        worker = ToolDownloadWorker(tool_id)
        thread = QThread()
        worker.moveToThread(thread)
        
        thread.started.connect(worker.run)
        worker.progress.connect(progress_dialog.update_stats)
        
        def handle_finished(success, message):
            if thread.isRunning():
                thread.quit()
                thread.wait()
            
            # If the dialog is still open, close it and show result
            if progress_dialog.isVisible():
                progress_dialog.accept()
                if success:
                    QMessageBox.information(self, "Download Complete", message)
                elif not worker._abort:
                    QMessageBox.warning(self, "Download Failed", message)
            
            self.refresh_list()

        worker.finished.connect(handle_finished)
        
        # Connect cancel button to abort AND close dialog immediately
        progress_dialog.cancel_button.clicked.connect(worker.abort)
        progress_dialog.cancel_button.clicked.connect(progress_dialog.reject)
        
        thread.start()
        
        # If dialog is rejected (canceled or closed), ensure worker is aborted
        if progress_dialog.exec() == QDialog.Rejected:
            worker.abort()
            # Thread will finish its current chunk and exit due to abort flag


def find_tool(name: str) -> str | None:
    # 1. Check local bin folder first
    local_bin = Path(sys.argv[0]).parent / "bin" / f"{name}.exe"
    if local_bin.exists():
        return str(local_bin)
    
    # 2. Check system path
    return shutil.which(name)


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(520)

        title = QLabel(APP_NAME)
        title_font = QFont("Segoe UI", 15)
        title_font.setBold(True)
        title.setFont(title_font)

        subtitle = QLabel("Batch media optimizer for Godot projects.")
        subtitle.setStyleSheet("color: #666;")

        credits = QLabel(f"Created by {APP_AUTHOR}.")
        license_info = QLabel(
            f"License: {APP_LICENSE}\n"
            "Source files in this toolkit are distributed under MPL-2.0."
        )
        license_info.setWordWrap(True)

        libraries_label = QLabel("Libraries and external tools")
        libraries = QListWidget()
        libraries.addItems(
            [
                "PySide6 - desktop user interface",
                "Pillow - PNG inspection, resizing, and optimization",
                "ffmpeg / ffprobe - audio and video conversion metadata",
                "pngquant - optional PNG quantization when available",
                "optipng - optional PNG optimization when available",
            ]
        )

        how_it_works_button = QPushButton("How it works?")
        how_it_works_button.clicked.connect(self.show_how_it_works)
        external_tools_button = QPushButton("External Tools")
        external_tools_button.clicked.connect(self.show_external_tools)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        layout.addWidget(credits)
        layout.addWidget(license_info)
        layout.addSpacing(8)
        layout.addWidget(libraries_label)
        layout.addWidget(libraries)
        footer = QHBoxLayout()
        footer.addWidget(how_it_works_button)
        footer.addWidget(external_tools_button)
        footer.addStretch(1)
        footer.addWidget(close_button)
        layout.addLayout(footer)

    def show_how_it_works(self) -> None:
        HowItWorksDialog(self).exec()

    def show_external_tools(self) -> None:
        ExternalToolsDialog(self).exec()


class HowItWorksDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("How It Works")
        self.setMinimumSize(620, 440)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setHtml(
            """
            <h2>How the tool works</h2>
            <p>This toolkit scans your Godot project for media assets, classifies them, and shows the files in separate Image, Audio, and Video tabs.</p>
            <h3>What it uses</h3>
            <ul>
              <li><b>PySide6</b> for the desktop UI, dialogs, preview windows, and playback controls.</li>
              <li><b>Pillow</b> for image inspection, metadata, and PNG optimization workflows.</li>
              <li><b>ffmpeg / ffprobe</b> for audio and video metadata, waveform extraction, preview playback, and conversion.</li>
              <li><b>pngquant</b> and <b>optipng</b> for optional PNG optimization when available on PATH.</li>
            </ul>
            <h3>What you need installed</h3>
            <ul>
              <li>Python with the dependencies from <code>requirements.txt</code>.</li>
              <li>ffmpeg if you want audio/video preview, waveform generation, or conversion to work.</li>
              <li>pngquant and optipng only if you want the optional PNG optimization passes.</li>
            </ul>
            <h3>Scanning</h3>
            <ul>
              <li>Images are listed when they are at least 1024 px on one edge or 256 KB on disk.</li>
              <li>Audio and video assets are detected by extension and metadata probing.</li>
              <li>Scan results update the tables and summary labels after each pass.</li>
            </ul>
            <h3>Previewing</h3>
            <ul>
              <li>Double-click an image, audio, or video row to open a dedicated preview window.</li>
              <li>Image preview supports zooming, panning, and a checkerboard transparency background.</li>
              <li>Audio preview includes waveform rendering, scrubbing, and live playback position.</li>
              <li>Video preview provides a dedicated player with seek and playback controls.</li>
            </ul>
            <h3>Conversion</h3>
            <ul>
              <li>Select files in a tab, then convert them with the controls at the bottom.</li>
              <li>ffmpeg is required for audio and video conversion paths.</li>
              <li>Image optimization uses Pillow and can optionally call pngquant and optipng when they are installed.</li>
            </ul>
            <h3>Workflow notes</h3>
            <ul>
              <li>Use the bottom bar in the image viewer for quick navigation and file actions.</li>
              <li>Use the About dialog for app information and this help panel for behavior details.</li>
              <li>If a tool is missing, the UI keeps previews available where possible and only disables the feature that depends on it.</li>
            </ul>
            """
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(text, 1)
        layout.addWidget(buttons)


class AssetTableWidget(QTableWidget):
    def __init__(self, *args, context_menu_handler=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._context_menu_handler = context_menu_handler

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self._context_menu_handler is None:
            super().contextMenuEvent(event)
            return
        item = self.itemAt(event.pos())
        if item is None:
            return
        self._context_menu_handler(self, item, event.globalPos())


class ImageGraphicsView(QGraphicsView):
    def __init__(self, pixmap: QPixmap, parent: QWidget | None = None):
        super().__init__(parent)
        self._pixmap_item = QGraphicsPixmapItem(pixmap)
        self._pixmap_item.setTransformationMode(Qt.SmoothTransformation)
        self._zoom = 0

        scene = QGraphicsScene(self)
        scene.addItem(self._pixmap_item)
        margin = max(pixmap.width(), pixmap.height(), 512) * 2
        scene.setSceneRect(QRectF(pixmap.rect()).adjusted(-margin, -margin, margin, margin))
        self.setScene(scene)

        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.viewport().setAttribute(Qt.WA_OpaquePaintEvent, True)

    def fit_image(self) -> None:
        if self._pixmap_item.pixmap().isNull():
            return
        self._zoom = 0
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.KeepAspectRatio)

    def wheelEvent(self, event) -> None:
        if event.angleDelta().y() == 0:
            return

        zoom_in = event.angleDelta().y() > 0
        if zoom_in:
            factor = 1.25
            self._zoom += 1
        else:
            factor = 0.8
            self._zoom -= 1

        if self._zoom < -20:
            self._zoom = -20
            return
        if self._zoom > 40:
            self._zoom = 40
            return

        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.fit_image()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        del rect
        painter.save()
        painter.resetTransform()
        painter.fillRect(self.viewport().rect(), make_checkerboard_brush())
        painter.restore()


class ImageInfoDialog(QDialog):
    def __init__(self, asset: ImageAsset, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Image Info")
        self.setMinimumWidth(420)

        title = QLabel(asset.path.name)
        title_font = QFont("Segoe UI", 12)
        title_font.setBold(True)
        title.setFont(title_font)

        info = QLabel(
            f"Path: {asset.relative_path}\n"
            f"Resolution: {asset.width} x {asset.height}\n"
            f"Size: {format_bytes(asset.size_bytes)}\n"
            f"Format: {asset.path.suffix.lower().lstrip('.') or 'unknown'}"
        )
        info.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(info)
        layout.addWidget(buttons)

class ImageViewerWindow(QMainWindow):
    def __init__(
        self,
        asset: ImageAsset,
        parent: QWidget | None = None,
        *,
        open_next: Callable[[ImageAsset], None] | None = None,
        open_previous: Callable[[ImageAsset], None] | None = None,
    ):
        super().__init__(parent)
        self.asset = asset
        self._open_next = open_next
        self._open_previous = open_previous
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(f"{asset.relative_path} - Image Viewer")
        self.resize(940, 720)

        self.prev_button = QPushButton("Previous")
        self.prev_button.clicked.connect(self.go_previous)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self.go_next)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self.rename_asset)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_asset)
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #666;")
        self.info_button = QPushButton("Image Info")
        self.info_button.clicked.connect(self.show_info)

        self.viewer_stack = QStackedWidget()
        self.image_view = QWidget()
        self.error_view = QLabel("")
        self.error_view.setAlignment(Qt.AlignCenter)
        self.error_view.setWordWrap(True)
        self.viewer_stack.addWidget(self.image_view)
        self.viewer_stack.addWidget(self.error_view)
        self.set_asset(asset)

        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(10, 8, 10, 10)
        bottom_bar.setSpacing(8)
        bottom_bar.addWidget(self.prev_button)
        bottom_bar.addWidget(self.next_button)
        bottom_bar.addWidget(self.rename_button)
        bottom_bar.addWidget(self.delete_button)
        bottom_bar.addWidget(self.info_label, 1)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.info_button)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.viewer_stack, 1)
        layout.addLayout(bottom_bar)
        self.setCentralWidget(central)
        self.update_info_label()

    def set_asset(self, asset: ImageAsset) -> None:
        self.asset = asset
        self.setWindowTitle(f"{asset.relative_path} - Image Viewer")
        pixmap = QPixmap(str(asset.path))
        if pixmap.isNull():
            self.error_view.setText(f"Could not load image:\n{asset.path}")
            self.viewer_stack.setCurrentWidget(self.error_view)
            return

        self.viewer = ImageGraphicsView(pixmap, self)
        self.viewer_stack.removeWidget(self.image_view)
        self.image_view.deleteLater()
        self.image_view = self.viewer
        self.viewer_stack.insertWidget(0, self.image_view)
        self.viewer_stack.setCurrentWidget(self.image_view)
        self.viewer.fit_image()
        self.update_info_label()

    def update_info_label(self) -> None:
        self.info_label.setText(
            f"{self.asset.width} x {self.asset.height} — {format_bytes(self.asset.size_bytes)} — Wheel zoom, drag pan"
        )

    def go_next(self) -> None:
        if self._open_next:
            self._open_next(self)

    def go_previous(self) -> None:
        if self._open_previous:
            self._open_previous(self)

    def show_info(self) -> None:
        ImageInfoDialog(self.asset, self).exec()

    def rename_asset(self) -> None:
        current_name = self.asset.path.name
        new_name, accepted = QInputDialog.getText(self, "Rename Image", "New file name:", text=current_name)
        if not accepted:
            return
        new_name = new_name.strip()
        if not new_name:
            return

        if Path(new_name).suffix.lower() != self.asset.path.suffix.lower():
            new_name = f"{Path(new_name).stem}{self.asset.path.suffix}"

        target = self.asset.path.with_name(new_name)
        if target == self.asset.path:
            return
        if target.exists():
            QMessageBox.warning(self, "Rename Failed", "A file with that name already exists.")
            return

        try:
            self.asset.path.rename(target)
        except OSError as exc:
            QMessageBox.warning(self, "Rename Failed", str(exc))
            return

        self.asset = ImageAsset(
            path=target,
            relative_path=target.as_posix(),
            width=self.asset.width,
            height=self.asset.height,
            size_bytes=self.asset.size_bytes,
        )
        self.setWindowTitle(f"{self.asset.relative_path} - Image Viewer")
        self.update_info_label()
        parent = self.parentWidget()
        if parent and hasattr(parent, "start_scan"):
            parent.start_scan()

    def delete_asset(self) -> None:
        if QMessageBox.question(
            self,
            "Delete Image",
            f"Delete {self.asset.path.name}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        try:
            self.asset.path.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "Delete Failed", str(exc))
            return

        parent = self.parentWidget()
        if parent and hasattr(parent, "start_scan"):
            parent.start_scan()
        self.close()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if hasattr(self, "viewer"):
            self.viewer.fit_image()
        center_window_on_parent(self, self.parentWidget())


def make_checkerboard_brush() -> QBrush:
    cached = QPixmapCache.find("rpgt_checkerboard")
    if cached is not None:
        return QBrush(cached)

    tile = QPixmap(32, 32)
    tile.fill(QColor(60, 60, 60))
    painter = QPainter(tile)
    painter.fillRect(0, 0, 16, 16, QColor(88, 88, 88))
    painter.fillRect(16, 16, 16, 16, QColor(88, 88, 88))
    painter.end()
    QPixmapCache.insert("rpgt_checkerboard", tile)
    return QBrush(tile)


class MediaPlayerWindow(QMainWindow):
    def __init__(self, asset: MediaAsset, parent: QWidget | None, *, title: str, size: QSize):
        super().__init__(parent)
        self.asset = asset
        self._duration_ms = 0
        self._seeking = False

        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(title)
        self.setFixedSize(size)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(1.0)
        self.player.setAudioOutput(self.audio_output)

        self.play_button = QToolButton()
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.setText("Play")
        self.play_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.play_button.setToolTip("Play / pause")
        self.play_button.clicked.connect(self.toggle_playback)

        self.stop_button = QToolButton()
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.setText("Stop")
        self.stop_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.stop_button.setToolTip("Stop")
        self.stop_button.clicked.connect(self.player.stop)

        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderPressed.connect(self.begin_seek)
        self.position_slider.sliderReleased.connect(self.finish_seek)
        self.position_slider.sliderMoved.connect(self.preview_seek)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(104)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(96)
        self.volume_slider.setToolTip("Volume")
        self.volume_slider.valueChanged.connect(lambda value: self.audio_output.setVolume(value / 100))
        self.volume_value_label = QLabel("100%")
        self.volume_value_label.setMinimumWidth(36)
        self.volume_slider.valueChanged.connect(lambda value: self.volume_value_label.setText(f"{value}%"))

        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        self.player.errorOccurred.connect(self.on_error)
        self.player.setSource(QUrl.fromLocalFile(str(asset.path)))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        center_window_on_parent(self, self.parentWidget())

    def closeEvent(self, event) -> None:
        self.player.stop()
        super().closeEvent(event)

    def controls_layout(self) -> QVBoxLayout:
        return self.build_controls_layout(show_position_slider=True)

    def build_controls_layout(self, show_position_slider: bool) -> QVBoxLayout:
        transport = QHBoxLayout()
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setSpacing(8)

        # Snug buttons by default
        self.play_button.setMinimumWidth(0)
        self.stop_button.setMinimumWidth(0)

        transport.addWidget(self.play_button)
        transport.addWidget(self.stop_button)
        if show_position_slider:
            self.position_slider.setMinimumWidth(220)
            transport.addWidget(self.position_slider, 1)
        transport.addWidget(self.time_label)
        self.volume_slider.setFixedWidth(100)
        transport.addWidget(self.volume_slider)
        transport.addWidget(self.volume_value_label)

        layout = QVBoxLayout()
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)
        layout.addLayout(transport)
        return layout

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def begin_seek(self) -> None:
        self._seeking = True

    def preview_seek(self, position: int) -> None:
        self.time_label.setText(f"{format_milliseconds(position)} / {format_milliseconds(self._duration_ms)}")

    def finish_seek(self) -> None:
        self.player.setPosition(self.position_slider.value())
        self._seeking = False

    def on_duration_changed(self, duration: int) -> None:
        self._duration_ms = max(0, duration)
        self.position_slider.setRange(0, self._duration_ms)
        self.time_label.setText(f"{format_milliseconds(self.player.position())} / {format_milliseconds(self._duration_ms)}")

    def on_position_changed(self, position: int) -> None:
        if not self._seeking:
            self.position_slider.setValue(position)
        self.time_label.setText(f"{format_milliseconds(position)} / {format_milliseconds(self._duration_ms)}")

    def on_playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if state == QMediaPlayer.PlaybackState.PlayingState
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self.play_button.setIcon(self.style().standardIcon(icon))

    def on_error(self) -> None:
        if self.player.error() == QMediaPlayer.Error.NoError:
            return
        self.statusBar().showMessage(self.player.errorString() or "Could not play media")


class WaveformWidget(QWidget):
    seekRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._left_peaks: list[float] = []
        self._right_peaks: list[float] = []
        self._duration_ms = 0
        self._position_ms = 0
        self._seeking = False
        self._display_position_ms = 0.0
        self._position_start_ms = 0.0
        self._position_target_ms = 0.0
        self._position_elapsed = QElapsedTimer()
        self._position_elapsed.start()
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(16)
        self._animation_timer.timeout.connect(self.update)
        self._animation_timer.start()
        self.setMinimumHeight(54)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_waveform(self, peaks: list[tuple[float, float]]) -> None:
        self._left_peaks = [left for left, _ in peaks]
        self._right_peaks = [right for _, right in peaks]
        self.update()

    def set_duration(self, duration_ms: int) -> None:
        self._duration_ms = max(0, duration_ms)
        self.update()

    def set_position(self, position_ms: int) -> None:
        position_ms = max(0, position_ms)
        self._position_ms = position_ms
        if self._seeking:
            self._display_position_ms = float(position_ms)
            self._position_start_ms = self._display_position_ms
            self._position_target_ms = self._display_position_ms
            return
        self._position_start_ms = self._display_position_ms
        self._position_target_ms = float(position_ms)
        self._position_elapsed.restart()
        self.update()

    def animated_position(self) -> float:
        elapsed_ms = float(self._position_elapsed.elapsed())
        if self._display_position_ms == self._position_target_ms:
            return self._display_position_ms
        if elapsed_ms >= 120.0:
            self._display_position_ms = self._position_target_ms
            return self._display_position_ms
        progress = elapsed_ms / 120.0
        self._display_position_ms = self._position_start_ms + (self._position_target_ms - self._position_start_ms) * progress
        return self._display_position_ms

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 4, 0, -4)
        painter.fillRect(self.rect(), QColor(247, 248, 249))

        if not self._left_peaks or not self._right_peaks:
            painter.setPen(QColor(170, 176, 180))
            painter.drawText(rect, Qt.AlignCenter, "Waveform unavailable")
            return

        progress = 0.0 if not self._duration_ms else min(1.0, self.animated_position() / self._duration_ms)
        playhead_x = rect.left() + int(rect.width() * progress)
        sample_count = min(len(self._left_peaks), len(self._right_peaks), max(16, rect.width() // 2))
        step_left = len(self._left_peaks) / sample_count
        step_right = len(self._right_peaks) / sample_count

        top_points: list[tuple[float, float]] = []
        bottom_points: list[tuple[float, float]] = []
        for index in range(sample_count):
            x = rect.left() + (rect.width() * index / max(1, sample_count - 1))
            top_peak = self._right_peaks[min(len(self._right_peaks) - 1, int(index * step_right))]
            bottom_peak = self._left_peaks[min(len(self._left_peaks) - 1, int(index * step_left))]
            top_points.append((x, rect.center().y() - (rect.height() * 0.40 * max(0.12, min(1.0, top_peak)))))
            bottom_points.append((x, rect.center().y() + (rect.height() * 0.40 * max(0.12, min(1.0, bottom_peak)))))

        top_path = QPainterPath()
        top_path.moveTo(top_points[0][0], top_points[0][1])
        for index in range(1, len(top_points)):
            prev_x, prev_y = top_points[index - 1]
            x, y = top_points[index]
            top_path.quadTo((prev_x + x) / 2, prev_y, x, y)

        top_fill = QPainterPath(top_path)
        top_fill.lineTo(rect.right(), rect.center().y())
        top_fill.lineTo(rect.left(), rect.center().y())
        top_fill.closeSubpath()

        bottom_path = QPainterPath()
        bottom_path.moveTo(bottom_points[0][0], bottom_points[0][1])
        for index in range(1, len(bottom_points)):
            prev_x, prev_y = bottom_points[index - 1]
            x, y = bottom_points[index]
            bottom_path.quadTo((prev_x + x) / 2, prev_y, x, y)

        bottom_fill = QPainterPath(bottom_path)
        bottom_fill.lineTo(rect.right(), rect.center().y())
        bottom_fill.lineTo(rect.left(), rect.center().y())
        bottom_fill.closeSubpath()

        played_color = QColor(196, 224, 243, 170)
        waveform_color = QColor(41, 112, 138)

        def interpolate(points: list[tuple[float, float]], x: float) -> tuple[float, float]:
            if x <= points[0][0]:
                return points[0]
            if x >= points[-1][0]:
                return points[-1]
            for index in range(1, len(points)):
                x1, y1 = points[index - 1]
                x2, y2 = points[index]
                if x <= x2:
                    ratio = 0.0 if x2 == x1 else (x - x1) / (x2 - x1)
                    return x, y1 + (y2 - y1) * ratio
            return points[-1]

        top_play = [point for point in top_points if point[0] < playhead_x]
        bottom_play = [point for point in bottom_points if point[0] < playhead_x]
        top_play.append(interpolate(top_points, playhead_x))
        bottom_play.append(interpolate(bottom_points, playhead_x))

        played_path = QPainterPath()
        played_path.moveTo(top_play[0][0], top_play[0][1])
        for index in range(1, len(top_play)):
            prev_x, prev_y = top_play[index - 1]
            x, y = top_play[index]
            played_path.quadTo((prev_x + x) / 2, prev_y, x, y)
        played_path.lineTo(bottom_play[-1][0], bottom_play[-1][1])
        for index in range(len(bottom_play) - 1, 0, -1):
            prev_x, prev_y = bottom_play[index]
            x, y = bottom_play[index - 1]
            played_path.quadTo((prev_x + x) / 2, prev_y, x, y)
        played_path.closeSubpath()

        painter.setPen(Qt.NoPen)
        painter.setBrush(played_color)
        painter.drawPath(played_path)

        painter.setBrush(waveform_color)
        painter.drawPath(top_fill)
        painter.drawPath(bottom_fill)

        painter.setPen(QColor(18, 74, 96))
        painter.drawLine(playhead_x, rect.top(), playhead_x, rect.bottom())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._seeking = True
        self.seek_from_x(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.LeftButton:
            self._seeking = True
            self.seek_from_x(event.position().x())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._seeking = False
            self.seek_from_x(event.position().x())
        super().mouseReleaseEvent(event)

    def seek_from_x(self, x: float) -> None:
        if not self._duration_ms:
            return
        width = max(1, self.width())
        ratio = min(1.0, max(0.0, x / width))
        self.seekRequested.emit(int(self._duration_ms * ratio))


class AudioPlayerWindow(MediaPlayerWindow):
    def __init__(self, asset: MediaAsset, parent: QWidget | None = None):
        super().__init__(asset, parent, title=f"{asset.relative_path} - Audio Player", size=QSize(600, 160))

        # Info Panel
        info_panel = QWidget()
        info_panel.setObjectName("info_panel")
        info_panel.setStyleSheet("#info_panel { background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; }")
        info_layout = QVBoxLayout(info_panel)
        info_layout.setContentsMargins(12, 8, 12, 8)
        info_layout.setSpacing(2)

        title = QLabel(asset.path.name)
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title_font = QFont("Segoe UI", 10)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #0f172a;")

        details_text = f"{asset.codec} | {format_bitrate(asset.bitrate)} | {format_bytes(asset.size_bytes)} | {asset.relative_path}"
        details = QLabel(details_text)
        details.setStyleSheet("color: #64748b; font-size: 11px;")

        info_layout.addWidget(title)
        info_layout.addWidget(details)

        # Waveform
        self.waveform = WaveformWidget()
        self.waveform.set_waveform(load_audio_waveform(asset.path))
        self.waveform.seekRequested.connect(self.player.setPosition)
        
        waveform_container = QWidget()
        waveform_layout = QVBoxLayout(waveform_container)
        waveform_layout.setContentsMargins(12, 4, 12, 4)
        waveform_layout.addWidget(self.waveform)

        # Controls & Error
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #9a3412; font-size: 11px; margin-left: 12px;")
        self.error_label.setWordWrap(True)
        self.error_label.setHidden(True)

        controls_container = QWidget()
        controls_layout = QVBoxLayout(controls_container)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(0)
        controls_layout.addLayout(self.build_audio_controls_layout())
        controls_layout.addWidget(self.error_label)

        # Main Layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(info_panel)
        layout.addWidget(waveform_container, 1)
        layout.addWidget(controls_container)

        widget = QWidget()
        widget.setLayout(layout)
        self.setCentralWidget(widget)

    def on_duration_changed(self, duration: int) -> None:
        super().on_duration_changed(duration)
        self.waveform.set_duration(duration)

    def on_position_changed(self, position: int) -> None:
        super().on_position_changed(position)
        self.waveform.set_position(position)

    def on_error(self) -> None:
        if self.player.error() == QMediaPlayer.Error.NoError:
            self.error_label.setHidden(True)
            return
        self.error_label.setText(self.player.errorString() or "Could not play audio")
        self.error_label.setHidden(False)

    def build_audio_controls_layout(self) -> QVBoxLayout:
        transport = QHBoxLayout()
        transport.setContentsMargins(12, 4, 12, 10)
        transport.setSpacing(8)

        # Consistent widths for labels to prevent layout shifting
        self.time_label.setMinimumWidth(100)
        self.volume_slider.setFixedWidth(100)
        self.volume_value_label.setFixedWidth(36)

        transport.addWidget(self.play_button)
        transport.addWidget(self.stop_button)
        transport.addWidget(self.time_label)
        transport.addStretch(1)
        transport.addWidget(self.volume_slider)
        transport.addWidget(self.volume_value_label)
        transport.setAlignment(Qt.AlignVCenter)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(transport)
        return layout


class VideoPlayerWindow(MediaPlayerWindow):
    def __init__(self, asset: MediaAsset, parent: QWidget | None = None):
        super().__init__(asset, parent, title=f"{asset.relative_path} - Video Player", size=QSize(900, 620))
        self.setMinimumSize(780, 520)
        self.setMaximumSize(QSize(16777215, 16777215))

        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background: #000000;")
        self.player.setVideoOutput(self.video_widget)

        # Info Panel
        info_panel = QWidget()
        info_panel.setObjectName("info_panel")
        info_panel.setStyleSheet("#info_panel { background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; }")
        info_layout = QVBoxLayout(info_panel)
        info_layout.setContentsMargins(12, 8, 12, 8)
        info_layout.setSpacing(2)

        title = QLabel(asset.path.name)
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title_font = QFont("Segoe UI", 10)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #0f172a;")

        resolution = f"{asset.width} x {asset.height}" if asset.width and asset.height else "Unknown resolution"
        details_text = f"{resolution} | {asset.codec} | {format_fps(asset.fps)} | {format_bytes(asset.size_bytes)} | {asset.relative_path}"
        details = QLabel(details_text)
        details.setStyleSheet("color: #64748b; font-size: 11px;")

        info_layout.addWidget(title)
        info_layout.addWidget(details)

        # Middle (Video)
        middle_container = QWidget()
        middle_layout = QVBoxLayout(middle_container)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.setSpacing(0)
        middle_layout.addWidget(self.video_widget, 1)

        # Controls
        bottom_container = QWidget()
        bottom_layout = QVBoxLayout(bottom_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)
        
        bottom_layout.addLayout(self.controls_layout())

        # Main Layout
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(info_panel)
        layout.addWidget(middle_container, 1)
        layout.addWidget(bottom_container)

        widget = QWidget()
        widget.setLayout(layout)
        self.setCentralWidget(widget)
        self.player.play()


def center_window_on_parent(window: QWidget, parent: QWidget | None) -> None:
    if parent:
        parent_geometry = parent.frameGeometry()
        top_left = parent_geometry.center() - window.rect().center()
        window.move(top_left)


def format_milliseconds(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def load_audio_waveform(path: Path, buckets: int = 320) -> list[tuple[float, float]]:
    if not find_tool("ffmpeg"):
        return []

    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "8000",
        "-ac",
        "2",
        "-",
    ]
    try:
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []

    data = result.stdout
    if result.returncode != 0 or len(data) < 2:
        return []

    frame_count = len(data) // 4
    bucket_size = max(1, frame_count // buckets)
    left_peaks: list[float] = []
    right_peaks: list[float] = []
    left_peak = 0
    right_peak = 0
    in_bucket = 0

    for offset in range(0, frame_count * 4, 4):
        left_sample = int.from_bytes(data[offset : offset + 2], byteorder="little", signed=True)
        right_sample = int.from_bytes(data[offset + 2 : offset + 4], byteorder="little", signed=True)
        left_peak = max(left_peak, abs(left_sample))
        right_peak = max(right_peak, abs(right_sample))
        in_bucket += 1
        if in_bucket >= bucket_size:
            left_peaks.append(left_peak / 32768)
            right_peaks.append(right_peak / 32768)
            left_peak = 0
            right_peak = 0
            in_bucket = 0

    if in_bucket:
        left_peaks.append(left_peak / 32768)
        right_peaks.append(right_peak / 32768)

    loudest = max(left_peaks + right_peaks, default=0)
    if loudest <= 0:
        return []
    left = [max(0.04, min(1.0, value / loudest)) for value in left_peaks[:buckets]]
    right = [max(0.04, min(1.0, value / loudest)) for value in right_peaks[:buckets]]
    if len(left) < len(right):
        left.extend([left[-1] if left else 0.04] * (len(right) - len(left)))
    elif len(right) < len(left):
        right.extend([right[-1] if right else 0.04] * (len(left) - len(right)))
    return list(zip(left, right))


class MainWindow(QMainWindow):
    def __init__(self, project_root: Path):
        super().__init__()
        self.project_root = project_root
        self.image_assets: list[ImageAsset] = []
        self.audio_assets: list[MediaAsset] = []
        self.video_assets: list[MediaAsset] = []
        self.scan_thread: QThread | None = None
        self.scan_worker: ScanWorker | None = None
        self.convert_thread: QThread | None = None
        self.convert_worker: QObject | None = None
        self.media_tabs_checked: set[str] = set()
        self.image_viewers: list[ImageViewerWindow] = []
        self.audio_players: list[AudioPlayerWindow] = []
        self.video_players: list[VideoPlayerWindow] = []

        self.setWindowTitle(APP_NAME)
        self.resize(1040, 680)

        self.tabs = QTabWidget()
        self.image_tab = self.build_image_tab()
        self.audio_tab = self.build_audio_tab()
        self.video_tab = self.build_video_tab()
        self.tabs.addTab(self.image_tab, "Image")
        self.tabs.addTab(self.audio_tab, "Audio")
        self.tabs.addTab(self.video_tab, "Video")
        self.tabs.currentChanged.connect(self.on_tab_changed)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.tabs, 1)
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(f"Project root: {self.project_root}")
        QApplication.instance().installEventFilter(self)
        self.start_scan()

    def build_image_tab(self) -> QWidget:
        self.image_table = make_table(["Use", "File", "Resolution", "Size", "MP", "Recommendation"], self.show_asset_context_menu)
        self.image_table.itemDoubleClicked.connect(self.open_image_viewer)
        self.image_summary_label = QLabel("Scanning project images...")
        self.image_summary_label.setContentsMargins(12, 10, 0, 0)
        controls = self.build_controls(
            self.start_scan,
            self.choose_project_root,
            lambda: self.set_all_checked(self.image_table, True),
            lambda: self.set_all_checked(self.image_table, False),
            self.convert_selected_images,
        )
        tab, self.image_stack = assemble_tab(
            self.image_summary_label, self.image_table, controls, "Scanning for files...", "No items of this type found in this project"
        )
        return tab

    def build_audio_tab(self) -> QWidget:
        self.audio_table = make_table(["Use", "File", "Duration", "Codec", "Bitrate", "Size", "Recommendation"], self.show_asset_context_menu)
        self.audio_table.itemDoubleClicked.connect(self.open_audio_player)
        self.audio_summary_label = QLabel("Scanning project audio...")
        self.audio_summary_label.setContentsMargins(12, 10, 0, 0)
        controls = self.build_controls(
            self.start_scan,
            self.choose_project_root,
            lambda: self.set_all_checked(self.audio_table, True),
            lambda: self.set_all_checked(self.audio_table, False),
            self.convert_selected_audio,
        )
        self.audio_content, self.audio_stack = assemble_tab(
            self.audio_summary_label, self.audio_table, controls, "Scanning for files...", "No items of this type found in this project"
        )
        return self.audio_content

    def build_video_tab(self) -> QWidget:
        self.video_table = make_table(["Use", "File", "Resolution", "Duration", "Codec", "FPS", "Size", "Recommendation"], self.show_asset_context_menu)
        self.video_table.itemDoubleClicked.connect(self.open_video_player)
        self.video_summary_label = QLabel("Scanning project video...")
        self.video_summary_label.setContentsMargins(12, 10, 0, 0)
        controls = self.build_controls(
            self.start_scan,
            self.choose_project_root,
            lambda: self.set_all_checked(self.video_table, True),
            lambda: self.set_all_checked(self.video_table, False),
            self.convert_selected_video,
        )
        self.video_content, self.video_stack = assemble_tab(
            self.video_summary_label, self.video_table, controls, "Scanning for files...", "No items of this type found in this project"
        )
        return self.video_content

    def show_about(self) -> None:
        AboutDialog(self).exec()

    def show_asset_context_menu(self, table: QTableWidget, item: QTableWidgetItem, global_pos) -> None:
        asset = self.asset_for_table_item(table, item)
        if asset is None:
            return

        menu = QMenu(self)
        menu.addAction("Preview", lambda: self.preview_asset(asset))
        menu.addAction("Open in External Viewer", lambda: self.open_asset_externally(asset))
        menu.addAction("Show in Explorer", lambda: self.show_asset_in_explorer(asset))
        menu.addSeparator()
        menu.addAction("Rename", lambda: self.rename_asset_file(asset))
        menu.addAction("Delete", lambda: self.delete_asset_file(asset))
        menu.addSeparator()
        menu.addAction("Properties", lambda: self.show_asset_properties(asset))
        menu.exec(global_pos)

    def asset_for_table_item(self, table: QTableWidget, item: QTableWidgetItem):
        row = item.row()
        if table is self.image_table and 0 <= row < len(self.image_assets):
            return self.image_assets[row]
        if table is self.audio_table and 0 <= row < len(self.audio_assets):
            return self.audio_assets[row]
        if table is self.video_table and 0 <= row < len(self.video_assets):
            return self.video_assets[row]
        return None

    def preview_asset(self, asset) -> None:
        if isinstance(asset, ImageAsset):
            viewer = ImageViewerWindow(
                asset,
                self,
                open_next=self.advance_image_viewer,
                open_previous=self.retreat_image_viewer,
            )
            viewer.destroyed.connect(lambda _=None, window=viewer: self.forget_image_viewer(window))
            self.image_viewers.append(viewer)
            viewer.show()
            return
        if asset.path.suffix.lower() in AUDIO_EXTENSIONS:
            player = AudioPlayerWindow(asset, self)
            player.destroyed.connect(lambda _=None, window=player: self.forget_audio_player(window))
            self.audio_players.append(player)
            player.show()
            return
        player = VideoPlayerWindow(asset, self)
        player.destroyed.connect(lambda _=None, window=player: self.forget_video_player(window))
        self.video_players.append(player)
        player.show()

    def open_asset_externally(self, asset) -> None:
        path = str(asset.path)
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as exc:
            QMessageBox.warning(self, "Open Failed", str(exc))

    def show_asset_in_explorer(self, asset) -> None:
        path = str(asset.path)
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", str(asset.path.parent)])
        except OSError as exc:
            QMessageBox.warning(self, "Show in Explorer Failed", str(exc))

    def rename_asset_file(self, asset) -> None:
        new_name, accepted = QInputDialog.getText(self, "Rename File", "New file name:", text=asset.path.name)
        if not accepted:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        target = asset.path.with_name(new_name)
        if target == asset.path:
            return
        if target.exists():
            QMessageBox.warning(self, "Rename Failed", "A file with that name already exists.")
            return
        try:
            asset.path.rename(target)
        except OSError as exc:
            QMessageBox.warning(self, "Rename Failed", str(exc))
            return
        self.start_scan()

    def delete_asset_file(self, asset) -> None:
        if QMessageBox.question(
            self,
            "Delete File",
            f"Delete {asset.path.name}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            asset.path.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "Delete Failed", str(exc))
            return
        self.start_scan()

    def show_asset_properties(self, asset) -> None:
        path = str(asset.path)
        try:
            if sys.platform.startswith("win"):
                os.startfile(path, "properties")
            else:
                QMessageBox.information(
                    self,
                    "Properties",
                    f"Path: {asset.path}\nSize: {format_bytes(asset.size_bytes)}",
                )
        except OSError as exc:
            QMessageBox.warning(self, "Properties Failed", str(exc))

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key_Left, Qt.Key_Right):
            focused = QApplication.focusWidget()
            if focused and self.isAncestorOf(focused):
                table = self.table_for_current_tab()
                if table is not None and table.rowCount() > 0:
                    direction = -1 if event.key() == Qt.Key_Left else 1
                    current_row = table.currentRow()
                    if current_row < 0:
                        current_row = 0
                    next_row = (current_row + direction) % table.rowCount()
                    table.selectRow(next_row)
                    item = table.item(next_row, 0) or table.item(next_row, 1)
                    if item is not None:
                        table.setCurrentItem(item)
                        table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                    else:
                        table.setCurrentCell(next_row, 0)
                    return True
        return super().eventFilter(watched, event)

    def table_for_current_tab(self) -> QTableWidget | None:
        tab = self.tabs.currentWidget()
        if tab is self.image_tab:
            return self.image_table
        if tab is self.audio_tab:
            return self.audio_table
        if tab is self.video_tab:
            return self.video_table
        return None

    def build_controls(self, scan, choose_root, select_all, select_none, convert) -> QHBoxLayout:
        scan_button = QPushButton("Rescan")
        scan_button.clicked.connect(scan)
        choose_root_button = QPushButton("Choose Project Root")
        choose_root_button.clicked.connect(choose_root)
        about_button = QPushButton("About")
        about_button.clicked.connect(self.show_about)
        select_all_button = QPushButton("Select All")
        select_all_button.clicked.connect(select_all)
        select_none_button = QPushButton("Select None")
        select_none_button.clicked.connect(select_none)
        convert_button = QPushButton("Convert Selected")
        convert_button.clicked.connect(convert)
        controls = QHBoxLayout()
        controls.addWidget(scan_button)
        controls.addWidget(choose_root_button)
        controls.addWidget(about_button)
        controls.addStretch(1)
        controls.addWidget(select_all_button)
        controls.addWidget(select_none_button)
        controls.addWidget(convert_button)
        return controls

    def on_tab_changed(self, index: int) -> None:
        label = self.tabs.tabText(index).lower()
        if label in {"audio", "video"} and label not in self.media_tabs_checked:
            self.media_tabs_checked.add(label)
            self.check_ffmpeg_for_tab(label)

    def check_ffmpeg_for_tab(self, label: str) -> None:
        if find_tool("ffmpeg"):
            return
        QMessageBox.warning(
            self,
            "ffmpeg Not Found",
            f"{label.title()} conversion requires ffmpeg on PATH. Preview playback remains available.",
        )

    def start_scan(self) -> None:
        if self.thread_is_running(self.scan_thread):
            return
        self.image_table.setRowCount(0)
        self.audio_table.setRowCount(0)
        self.video_table.setRowCount(0)
        self.set_loading(True)
        self.image_summary_label.setText("Scanning project images...")
        self.audio_summary_label.setText("Scanning project audio...")
        self.video_summary_label.setText("Scanning project video...")
        self.statusBar().showMessage("Scanning...")

        self.scan_thread = QThread()
        self.scan_worker = ScanWorker(self.project_root)
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.progress.connect(lambda name: self.statusBar().showMessage(f"Scanning {name}"))
        self.scan_worker.finished.connect(self.scan_finished)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.finished.connect(lambda: setattr(self, "scan_worker", None))
        self.scan_thread.finished.connect(lambda: setattr(self, "scan_thread", None))
        self.scan_thread.start()

    def scan_finished(self, images: list[ImageAsset], audio: list[MediaAsset], video: list[MediaAsset]) -> None:
        self.image_assets = images
        self.audio_assets = audio
        self.video_assets = video
        self.populate_image_table()
        self.populate_media_table(self.audio_table, audio, is_video=False)
        self.populate_media_table(self.video_table, video, is_video=True)
        self.image_summary_label.setText(f"{len(images)} image candidates found ({format_bytes(sum_size(images))}).")
        self.audio_summary_label.setText(f"{len(audio)} audio candidates found ({format_bytes(sum_size(audio))}).")
        self.video_summary_label.setText(f"{len(video)} video candidates found ({format_bytes(sum_size(video))}).")
        self.set_loading(False, len(images), len(audio), len(video))
        self.statusBar().showMessage("Scan complete")

    def set_loading(self, loading: bool, img_count: int = 0, audio_count: int = 0, video_count: int = 0) -> None:
        if loading:
            self.image_stack.setCurrentIndex(0)
            self.audio_stack.setCurrentIndex(0)
            self.video_stack.setCurrentIndex(0)
        else:
            self.image_stack.setCurrentIndex(1 if img_count > 0 else 2)
            self.audio_stack.setCurrentIndex(1 if audio_count > 0 else 2)
            self.video_stack.setCurrentIndex(1 if video_count > 0 else 2)

    def populate_image_table(self) -> None:
        self.image_table.setRowCount(len(self.image_assets))
        for row, asset in enumerate(self.image_assets):
            add_checkbox(self.image_table, row, asset.recommendation != "Optional")
            values = [
                asset.relative_path,
                f"{asset.width} x {asset.height}",
                format_bytes(asset.size_bytes),
                f"{asset.megapixels:.2f}",
                asset.recommendation,
            ]
            set_row_values(self.image_table, row, values, right_columns={2, 3, 4})

    def populate_media_table(self, table: QTableWidget, assets: list[MediaAsset], is_video: bool) -> None:
        table.setRowCount(len(assets))
        for row, asset in enumerate(assets):
            add_checkbox(table, row, asset.recommendation != "Optional")
            if is_video:
                resolution = f"{asset.width} x {asset.height}" if asset.width and asset.height else "Unknown"
                values = [
                    asset.relative_path,
                    resolution,
                    format_duration(asset.duration),
                    asset.codec,
                    format_fps(asset.fps),
                    format_bytes(asset.size_bytes),
                    asset.recommendation,
                ]
                set_row_values(table, row, values, right_columns={2, 5, 6})
            else:
                values = [
                    asset.relative_path,
                    format_duration(asset.duration),
                    asset.codec,
                    format_bitrate(asset.bitrate),
                    format_bytes(asset.size_bytes),
                    asset.recommendation,
                ]
                set_row_values(table, row, values, right_columns={2, 4, 5})

    def set_all_checked(self, table: QTableWidget, checked: bool) -> None:
        for row in range(table.rowCount()):
            checkbox = row_checkbox(table, row)
            if checkbox:
                checkbox.setChecked(checked)

    def selected_images(self) -> list[ImageAsset]:
        return [asset for row, asset in enumerate(self.image_assets) if is_checked(self.image_table, row)]

    def selected_audio(self) -> list[MediaAsset]:
        return [asset for row, asset in enumerate(self.audio_assets) if is_checked(self.audio_table, row)]

    def selected_video(self) -> list[MediaAsset]:
        return [asset for row, asset in enumerate(self.video_assets) if is_checked(self.video_table, row)]

    def open_image_viewer(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self.image_assets):
            return
        viewer = ImageViewerWindow(
            self.image_assets[row],
            self,
            open_next=self.advance_image_viewer,
            open_previous=self.retreat_image_viewer,
        )
        viewer.destroyed.connect(lambda _=None, window=viewer: self.forget_image_viewer(window))
        self.image_viewers.append(viewer)
        viewer.show()

    def advance_image_viewer(self, viewer: ImageViewerWindow) -> None:
        self.update_image_viewer(viewer, 1)

    def retreat_image_viewer(self, viewer: ImageViewerWindow) -> None:
        self.update_image_viewer(viewer, -1)

    def update_image_viewer(self, viewer: ImageViewerWindow, direction: int) -> None:
        if not self.image_assets or viewer.asset not in self.image_assets:
            return
        index = self.image_assets.index(viewer.asset)
        next_index = (index + direction) % len(self.image_assets)
        viewer.set_asset(self.image_assets[next_index])

    def forget_image_viewer(self, viewer: ImageViewerWindow) -> None:
        if viewer in self.image_viewers:
            self.image_viewers.remove(viewer)

    def open_audio_player(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self.audio_assets):
            return
        player = AudioPlayerWindow(self.audio_assets[row], self)
        player.destroyed.connect(lambda _=None, window=player: self.forget_audio_player(window))
        self.audio_players.append(player)
        player.show()

    def forget_audio_player(self, player: AudioPlayerWindow) -> None:
        if player in self.audio_players:
            self.audio_players.remove(player)

    def open_video_player(self, item: QTableWidgetItem) -> None:
        row = item.row()
        if row < 0 or row >= len(self.video_assets):
            return
        player = VideoPlayerWindow(self.video_assets[row], self)
        player.destroyed.connect(lambda _=None, window=player: self.forget_video_player(window))
        self.video_players.append(player)
        player.show()

    def forget_video_player(self, player: VideoPlayerWindow) -> None:
        if player in self.video_players:
            self.video_players.remove(player)

    def choose_project_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose Godot Project Root", str(self.project_root))
        if not chosen:
            return
        self.project_root = Path(chosen)
        self.statusBar().showMessage(f"Project root: {self.project_root}")
        self.start_scan()

    def convert_selected_images(self) -> None:
        selected = self.selected_images()
        if not selected:
            QMessageBox.information(self, "No Files Selected", "Select one or more image files first.")
            return
        dialog = ImageOptionsDialog(selected, self)
        if dialog.exec() == QDialog.Accepted:
            self.start_convert("Optimizing images", ImageConvertWorker(selected, dialog.options()))

    def convert_selected_audio(self) -> None:
        if not find_tool("ffmpeg"):
            self.check_ffmpeg_for_tab("audio")
            return
        selected = self.selected_audio()
        if not selected:
            QMessageBox.information(self, "No Files Selected", "Select one or more audio files first.")
            return
        dialog = AudioOptionsDialog(selected, self)
        if dialog.exec() == QDialog.Accepted:
            self.start_convert("Converting audio", AudioConvertWorker(selected, dialog.options()))

    def convert_selected_video(self) -> None:
        if not find_tool("ffmpeg"):
            self.check_ffmpeg_for_tab("video")
            return
        selected = self.selected_video()
        if not selected:
            QMessageBox.information(self, "No Files Selected", "Select one or more video files first.")
            return
        dialog = VideoOptionsDialog(selected, self)
        if dialog.exec() == QDialog.Accepted:
            self.start_convert("Converting video", VideoConvertWorker(selected, dialog.options()))

    def start_convert(self, title: str, worker: QObject) -> None:
        if self.thread_is_running(self.convert_thread):
            return
        
        total = len(worker.assets)
        if total == 0:
            return

        self.progress = BatchProgressDialog(title, total, self)
        self.progress.canceled.connect(self.abort_conversion)
        self.progress.show()
        
        self.convert_thread = QThread()
        self.convert_worker = worker
        self.convert_worker.moveToThread(self.convert_thread)
        
        self.convert_thread.started.connect(self.convert_worker.run)
        self.convert_worker.progress.connect(self.update_convert_progress)
        self.convert_worker.finished.connect(self.convert_finished)
        
        self.convert_worker.finished.connect(self.convert_thread.quit)
        self.convert_worker.finished.connect(self.convert_worker.deleteLater)
        self.convert_thread.finished.connect(self.convert_thread.deleteLater)
        self.convert_thread.finished.connect(self.clear_convert_thread)
        
        self.convert_thread.start()

    def clear_convert_thread(self) -> None:
        # Crucial: Reset the reference so we don't try to access a deleted C++ object
        self.convert_thread = None
        self.convert_worker = None

    def abort_conversion(self) -> None:
        if self.convert_worker:
            try:
                self.convert_worker.abort()
            except RuntimeError:
                pass

    def update_convert_progress(self, value: int, path: str) -> None:
        if self.progress:
            try:
                self.progress.update_progress(value, path)
            except RuntimeError:
                pass

    def convert_finished(self, successes: int, total: int, failures: list[str]) -> None:
        if self.progress:
            try:
                self.progress.update_progress(total, "Complete")
                QTimer.singleShot(500, self.progress.close)
            except RuntimeError:
                pass
        
        # Capture aborted state before the worker potentially gets cleaned up
        is_aborted = False
        if self.convert_worker:
            try:
                is_aborted = getattr(self.convert_worker, "_abort", False)
            except RuntimeError:
                pass
        
        if is_aborted:
            QMessageBox.information(self, "Conversion Canceled", f"Operation canceled. Processed {successes}/{total} files.")
        elif failures:
            QMessageBox.warning(
                self,
                "Conversion Complete With Errors",
                f"Processed {successes}/{total} files.\n\n" + "\n".join(failures[:10]),
            )
        else:
            QMessageBox.information(self, "Conversion Complete", f"Processed {successes}/{total} files.")
        self.start_scan()

    def closeEvent(self, event) -> None:
        if self.thread_is_running(self.convert_thread):
            reply = QMessageBox.question(
                self,
                "Cancel Operation?",
                "An operation is currently running. Do you want to cancel it and exit?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.abort_conversion()
                # Give it a moment to stop
                self.convert_thread.quit()
                self.convert_thread.wait(1000)
            else:
                event.ignore()
                return

        if self.thread_is_running(self.scan_thread):
            self.scan_thread.quit()
            self.scan_thread.wait(2000)
        self.scan_thread = None
        self.convert_thread = None
        super().closeEvent(event)

    @staticmethod
    def thread_is_running(thread: QThread | None) -> bool:
        if thread is None:
            return False
        try:
            # Check if the underlying C++ object still exists and is running
            return thread.isRunning()
        except (RuntimeError, AttributeError):
            return False


def run_batch(assets, options, function, progress_signal, finished_signal, abort_flag=None) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    successes = 0
    failures: list[str] = []
    total = len(assets)
    completed = 0
    
    # Using 4 threads for images (or others if they use run_batch)
    max_workers = 4
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_asset = {executor.submit(function, asset.path, options): asset for asset in assets}
        
        for future in as_completed(future_to_asset):
            if abort_flag and abort_flag():
                # Note: ThreadPoolExecutor doesn't have a clean way to cancel running futures, 
                # but we can stop submitting new ones and returning early.
                # Already submitted ones will finish their current task.
                break
                
            asset = future_to_asset[future]
            try:
                future.result()
                successes += 1
            except Exception as exc:
                failures.append(f"{asset.relative_path}: {exc}")
            
            completed += 1
            progress_signal.emit(completed, asset.relative_path)

    finished_signal.emit(successes, total, failures)


def scan_image(path: Path, root: Path) -> ImageAsset | None:
    size_bytes = path.stat().st_size
    with Image.open(path) as image:
        width, height = image.size
    if size_bytes >= LARGE_FILE_BYTES or max(width, height) >= 1024:
        return ImageAsset(path, path.relative_to(root).as_posix(), size_bytes, width, height)
    return None


def scan_media(path: Path, root: Path, is_video: bool) -> MediaAsset:
    size_bytes = path.stat().st_size
    probe = ffprobe(path)
    stream = first_stream(probe, "video" if is_video else "audio")
    duration = parse_float(stream.get("duration") or probe.get("format", {}).get("duration"))
    bitrate = parse_int(stream.get("bit_rate") or probe.get("format", {}).get("bit_rate"))
    codec = stream.get("codec_name") or path.suffix.lower().lstrip(".")
    width = parse_int(stream.get("width")) if is_video else None
    height = parse_int(stream.get("height")) if is_video else None
    fps = parse_fps(stream.get("avg_frame_rate")) if is_video else None
    return MediaAsset(path, path.relative_to(root).as_posix(), size_bytes, duration, codec, bitrate, width, height, fps)


def ffprobe(path: Path) -> dict:
    if not find_tool("ffprobe"):
        return {}
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    return json.loads(result.stdout or "{}")


def first_stream(probe: dict, codec_type: str) -> dict:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    return {}


def iter_candidate_files(root: Path):
    explicit_dirs = [root / name for name in DEFAULT_SCAN_DIRS if (root / name).exists()]
    roots = explicit_dirs or [root]
    for scan_root in roots:
        for path in scan_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.name.endswith(".import") or path.name.endswith(".uid") or path.name.endswith(".bak"):
                continue
            if path.suffix.lower() in IMAGE_EXTENSIONS | MEDIA_EXTENSIONS:
                yield path


def optimize_png(path: Path, options: ImageOptions) -> None:
    original_size = path.stat().st_size
    backup_if_needed(path, options.create_backup)
    temp_path = temp_output_path(path, ".png")
    try:
        with Image.open(path) as image:
            image.load()
            output = image
            if options.downscale_enabled and max(image.size) > options.target_long_edge:
                output = image.resize(scaled_size(image.size, options.target_long_edge), resample_filter(options.resample_name))
            save_kwargs = {"optimize": True}
            if not options.preserve_metadata:
                output.save(temp_path, format="PNG", **save_kwargs)
            else:
                output.save(temp_path, format="PNG", pnginfo=png_metadata(image), **save_kwargs)
        if options.run_pngquant and find_tool("pngquant"):
            run_quiet(["pngquant", "--force", "--skip-if-larger", "--quality", options.pngquant_quality, "--ext", ".png", str(temp_path)])
        if options.run_optipng and find_tool("optipng"):
            run_quiet(["optipng", "-quiet", "-o2", str(temp_path)])
        if options.downscale_enabled or temp_path.stat().st_size <= original_size:
            temp_path.replace(path)
        else:
            temp_path.unlink(missing_ok=True)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def convert_audio(path: Path, options: AudioOptions) -> None:
    output_path = output_path_for(path, options.output_format, options.replace_same_extension)
    backup_if_needed(path, options.create_backup and output_path == path)
    temp_path = temp_output_path(output_path, "." + options.output_format)
    command = ["ffmpeg", "-y", "-i", str(path)]
    filters = []
    if options.normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if options.trim_silence:
        filters.append("silenceremove=start_periods=1:start_threshold=-50dB:detection=peak,areverse,silenceremove=start_periods=1:start_threshold=-50dB:detection=peak,areverse")
    if filters:
        command += ["-af", ",".join(filters)]
    command += ["-c:a", options.codec]
    if options.codec not in {"pcm_s16le", "flac"}:
        command += ["-b:a", options.bitrate]
    if options.sample_rate != "keep":
        command += ["-ar", options.sample_rate]
    if options.channels != "keep":
        command += ["-ac", "1" if options.channels == "mono" else "2"]
    command.append(str(temp_path))
    run_ffmpeg(command)
    replace_or_keep(path, output_path, temp_path)


def convert_video(path: Path, options: VideoOptions) -> None:
    output_path = output_path_for(path, options.output_format, options.replace_same_extension)
    backup_if_needed(path, options.create_backup and output_path == path)
    temp_path = temp_output_path(output_path, "." + options.output_format)
    command = ["ffmpeg", "-y", "-i", str(path), "-c:v", options.video_codec]
    if options.quality_mode == "CRF":
        command += ["-crf", str(options.quality_value)]
    else:
        command += ["-b:v", f"{options.quality_value}k"]
    if options.video_codec in {"libx264", "libx265"}:
        command += ["-preset", options.preset]
    video_filters = []
    if options.scale_long_edge:
        video_filters.append(f"scale='if(gt(iw,ih),{options.scale_long_edge},-2)':'if(gt(ih,iw),{options.scale_long_edge},-2)'")
    if video_filters:
        command += ["-vf", ",".join(video_filters)]
    if options.fps != "keep":
        command += ["-r", options.fps]
    if options.strip_audio:
        command += ["-an"]
    else:
        command += ["-c:a", options.audio_codec]
        if options.audio_codec != "copy":
            command += ["-b:a", options.audio_bitrate]
    if options.faststart and options.output_format == "mp4":
        command += ["-movflags", "+faststart"]
    command.append(str(temp_path))
    run_ffmpeg(command)
    replace_or_keep(path, output_path, temp_path)


def output_path_for(path: Path, output_format: str, replace_same_extension: bool) -> Path:
    suffix = "." + output_format
    if replace_same_extension and path.suffix.lower() == suffix:
        return path
    return path.with_name(f"{path.stem}_optimized{suffix}")


def replace_or_keep(source_path: Path, output_path: Path, temp_path: Path) -> None:
    if output_path == source_path:
        if temp_path.stat().st_size <= source_path.stat().st_size:
            temp_path.replace(source_path)
        else:
            temp_path.unlink(missing_ok=True)
    else:
        temp_path.replace(output_path)


def run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(last_error_line(result.stderr))


def run_quiet(command: list[str]) -> None:
    subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def temp_output_path(path: Path, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=path.parent)
    temp_path = Path(handle.name)
    handle.close()
    return temp_path


def backup_if_needed(path: Path, create_backup: bool) -> None:
    if create_backup:
        backup_path = path.with_suffix(path.suffix + ".bak")
        if not backup_path.exists():
            shutil.copy2(path, backup_path)


def make_table(headers: list[str], context_menu_handler=None) -> QTableWidget:
    table = AssetTableWidget(0, len(headers), context_menu_handler=context_menu_handler)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
    for column in range(2, len(headers)):
        table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
    return table


def assemble_tab(
    summary: QLabel,
    table: QTableWidget,
    controls: QHBoxLayout,
    loading_text: str,
    empty_text: str,
    extra: QWidget | None = None,
) -> tuple[QWidget, QStackedWidget]:
    content_layout = QVBoxLayout()
    content_layout.setContentsMargins(0, 0, 0, 0)
    content_layout.addWidget(summary)
    content_layout.addWidget(table, 1)
    content_layout.addLayout(controls)
    if extra:
        content_layout.addWidget(extra)
    content_widget = QWidget()
    content_widget.setLayout(content_layout)

    stack = QStackedWidget()
    stack.addWidget(make_loading_widget(loading_text))
    stack.addWidget(content_widget)
    stack.addWidget(make_empty_widget(empty_text))
    stack.setCurrentIndex(0)

    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(stack)
    widget = QWidget()
    widget.setLayout(layout)
    return widget, stack


def make_empty_widget(text: str) -> QWidget:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet("color: #777;")
    font = QFont("Segoe UI", 11)
    font.setWeight(QFont.Normal)
    label.setFont(font)

    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addStretch(1)
    layout.addWidget(label)
    layout.addStretch(1)

    widget = QWidget()
    widget.setLayout(layout)
    return widget


def make_loading_widget(text: str) -> QWidget:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet("color: #777;")
    font = QFont("Segoe UI", 11)
    font.setWeight(QFont.Normal)
    label.setFont(font)

    progress = QProgressBar()
    progress.setRange(0, 0)
    progress.setMinimumWidth(300)
    progress.setMaximumWidth(720)

    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    row.addWidget(progress)
    row.addStretch(1)

    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    layout.addStretch(1)
    layout.addWidget(label)
    layout.addLayout(row)
    layout.addStretch(1)

    widget = QWidget()
    widget.setLayout(layout)
    return widget


def add_checkbox(table: QTableWidget, row: int, checked: bool) -> None:
    checkbox = QCheckBox()
    checkbox.setChecked(checked)
    checkbox_widget = QWidget()
    checkbox_layout = QHBoxLayout(checkbox_widget)
    checkbox_layout.addWidget(checkbox)
    checkbox_layout.setAlignment(Qt.AlignCenter)
    checkbox_layout.setContentsMargins(0, 0, 0, 0)
    checkbox_widget.setProperty("checkbox", checkbox)
    table.setCellWidget(row, 0, checkbox_widget)


def set_row_values(table: QTableWidget, row: int, values: list[str], right_columns: set[int]) -> None:
    for column, value in enumerate(values, start=1):
        item = QTableWidgetItem(value)
        if column in right_columns:
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        table.setItem(row, column, item)


def row_checkbox(table: QTableWidget, row: int) -> QCheckBox | None:
    widget = table.cellWidget(row, 0)
    if not widget:
        return None
    return widget.property("checkbox")


def is_checked(table: QTableWidget, row: int) -> bool:
    checkbox = row_checkbox(table, row)
    return bool(checkbox and checkbox.isChecked())


def finish_dialog_layout(dialog: QDialog, form: QFormLayout, summary_text: str) -> None:
    summary = QLabel(summary_text)
    summary.setWordWrap(True)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout = QVBoxLayout(dialog)
    layout.addWidget(summary)
    layout.addLayout(form)
    layout.addWidget(buttons)


def png_metadata(image: Image.Image) -> PngInfo:
    metadata = PngInfo()
    for key, value in image.info.items():
        if isinstance(key, str) and isinstance(value, str):
            metadata.add_text(key, value)
    return metadata


def scaled_size(size: tuple[int, int], target_long_edge: int) -> tuple[int, int]:
    width, height = size
    longest = max(width, height)
    if longest <= target_long_edge:
        return size
    scale = target_long_edge / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def resample_filter(name: str) -> int:
    filters = {
        "lanczos": Image.Resampling.LANCZOS,
        "bicubic": Image.Resampling.BICUBIC,
        "bilinear": Image.Resampling.BILINEAR,
        "nearest": Image.Resampling.NEAREST,
    }
    return filters.get(name, Image.Resampling.LANCZOS)


def suggest_video_edge(assets: list[MediaAsset]) -> int:
    largest = max((max(asset.width or 0, asset.height or 0) for asset in assets), default=0)
    if largest > 1920:
        return 1920
    return 0


def parse_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_fps(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        denominator_value = parse_float(denominator)
        if not denominator_value:
            return None
        return (parse_float(numerator) or 0) / denominator_value
    return parse_float(value)


def format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


def format_duration(value: float | None) -> str:
    if value is None:
        return "Unknown"
    minutes, seconds = divmod(int(value), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def format_bitrate(value: int | None) -> str:
    if not value:
        return "Unknown"
    return f"{round(value / 1000)} kbps"


def format_fps(value: float | None) -> str:
    if value is None:
        return "Unknown"
    return f"{value:.2f}"


def sum_size(assets) -> int:
    return sum(asset.size_bytes for asset in assets)


def last_error_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "ffmpeg failed"


def find_project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "project.godot").exists():
            return candidate
    return current


def main() -> int:
    app = QApplication(sys.argv)
    if "windowsvista" in QStyleFactory.keys():
        app.setStyle("windowsvista")
    root = find_project_root(Path.cwd())
    window = MainWindow(root)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
