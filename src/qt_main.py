"""PyQt-based main application for annotation review."""

from __future__ import annotations

import colorsys
import json
import os
import shutil
import sys
from pathlib import Path

from .constants import DPI_SCALE, MONITOR_HEIGHT, MONITOR_WIDTH, SIDEBAR_WIDTH
from .data_loading import load_dataset_from_input, resolve_image_path
from .utils import parse_arguments

try:
    from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
    from PyQt6.QtGui import (
        QClipboard,
        QColor,
        QFont,
        QIcon,
        QImage,
        QImageReader,
        QKeyEvent,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
        QPolygonF,
        QWheelEvent,
    )
    from PyQt6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QColorDialog,
        QComboBox,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QListView,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:
    raise RuntimeError(
        "PyQt6 is required for this version of the app. Install with: pip install PyQt6"
    ) from exc


def color_for_category(category_id: int) -> QColor:
    """Generate a stable display color for a category."""
    hue = (category_id * 0.6180339887498949) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.65, 0.95)
    return QColor(int(red * 255), int(green * 255), int(blue * 255))


def _color_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "class_colors.json"
    return Path(__file__).resolve().parent / "class_colors.json"


def _default_color_config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "class_colors_default.json"
    return Path(__file__).resolve().parent / "class_colors_default.json"


def _load_color_config() -> dict[str, str]:
    path = _color_config_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def _ensure_color_config_files() -> None:
    """On first frozen run, copy bundled JSON configs next to the exe so the user can edit them."""
    if not getattr(sys, "frozen", False):
        return
    exe_dir = Path(sys.executable).parent
    meipass = Path(sys._MEIPASS)
    for filename in ("class_colors.json", "class_colors_default.json"):
        dest = exe_dir / filename
        if not dest.exists():
            src = meipass / filename
            if src.exists():
                shutil.copy2(src, dest)


_ensure_color_config_files()
_COLOR_CONFIG: dict[str, str] = _load_color_config()


def save_color_config() -> None:
    path = _color_config_path()
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(_COLOR_CONFIG, f, indent=4, ensure_ascii=False)
    except Exception:
        pass


def color_for_class(category_name: str, category_id: int) -> QColor:
    """Return the configured hex color for a class name, or generate one from the category ID."""
    hex_color = _COLOR_CONFIG.get(category_name)
    if hex_color:
        color = QColor(hex_color)
        if color.isValid():
            return color
    return color_for_category(category_id)


def polygon_centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Calculate the centroid of a polygon or point list."""
    if not points:
        return 0.0, 0.0
    total_x = sum(point[0] for point in points)
    total_y = sum(point[1] for point in points)
    count = float(len(points))
    return total_x / count, total_y / count


def resource_path(*parts: str) -> Path:
    """Resolve a bundled resource path in both source and frozen runs."""
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path.joinpath(*parts)


class ImageCanvas(QWidget):
    """Image viewport widget with pan/zoom and Qt-native annotation overlays."""

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._title = "Annotation Review"
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self._dragging = False
        self._drag_start = QPoint(0, 0)
        self._fit_mode = False
        self._overlay_items: list[dict] = []
        self.show_points = False
        self.show_labels = True
        self.annotation_opacity = 0.30
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(640, 400)

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def has_image(self) -> bool:
        return self._pixmap is not None

    def set_idle(self, title: str = "Annotation Review") -> None:
        self._pixmap = None
        self._overlay_items = []
        self._title = title
        self._fit_mode = False
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self.update()

    def load_image(self, image_path: str, title: str) -> bool:
        """Load an image file into the canvas using Qt only."""
        reader = QImageReader(image_path)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return False

        self._pixmap = QPixmap.fromImage(image)
        self._title = title
        self._dragging = False
        self._drag_start = QPoint(0, 0)
        self._fit_mode = True
        self.fit_to_view()
        return True

    def set_overlay_items(self, overlay_items: list[dict]) -> None:
        self._overlay_items = list(overlay_items)
        self.update()

    def _base_image_size(self) -> tuple[int, int]:
        if self._pixmap is None:
            return 0, 0
        return self._pixmap.width(), self._pixmap.height()

    def fit_to_view(self) -> None:
        if self._pixmap is None:
            return

        image_width, image_height = self._base_image_size()
        if image_width <= 0 or image_height <= 0:
            return

        available_width = max(1, self.width() - 16)
        available_height = max(1, self.height() - 16)
        self._zoom = max(0.05, min(40.0, min(available_width / image_width, available_height / image_height)))
        self._offset = QPoint(0, 0)
        self._fit_mode = True
        self.update()

    def reset_to_original_size(self) -> None:
        if self._pixmap is None:
            return
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self._fit_mode = False
        self.update()

    def _fit_display_rect(self) -> tuple[int, int, int, int]:
        if self._pixmap is None:
            return 0, 0, 0, 0

        scaled_width = max(1, int(round(self._pixmap.width() * self._zoom)))
        scaled_height = max(1, int(round(self._pixmap.height() * self._zoom)))
        x = (self.width() - scaled_width) // 2 + self._offset.x()
        y = (self.height() - scaled_height) // 2 + self._offset.y()
        return x, y, scaled_width, scaled_height

    def _draw_idle(self, painter: QPainter) -> None:
        painter.fillRect(self.rect(), QColor(20, 22, 25))

        card_rect = QRectF(self.width() * 0.12, self.height() * 0.22, self.width() * 0.76, self.height() * 0.28)
        painter.setPen(QPen(QColor(78, 84, 93), 1))
        painter.setBrush(QColor(28, 31, 36))
        painter.drawRoundedRect(card_rect, 18, 18)

        title_font = QFont()
        title_font.setPointSizeF(max(15.0, 16.0 * DPI_SCALE))
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(244, 244, 246))
        painter.drawText(card_rect.adjusted(24, 16, -24, -card_rect.height() * 0.5), Qt.AlignmentFlag.AlignLeft, "No dataset loaded")

        body_font = QFont()
        body_font.setPointSizeF(max(10.0, 11.0 * DPI_SCALE))
        painter.setFont(body_font)
        painter.setPen(QColor(198, 201, 206))
        painter.drawText(card_rect.adjusted(24, 58, -24, -16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "Use Open folder to load a COCO zip export or extracted folder.")

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self._pixmap is None:
            self._draw_idle(painter)
            return

        painter.fillRect(self.rect(), QColor(17, 18, 20))

        image_x, image_y, scaled_width, scaled_height = self._fit_display_rect()
        scaled_pixmap = self._pixmap.scaled(
            scaled_width,
            scaled_height,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(image_x, image_y, scaled_pixmap)
        self._draw_overlays(painter, image_x, image_y)

        title_font = QFont()
        title_font.setPointSizeF(max(9.0, 9.0 * DPI_SCALE))
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(244, 244, 244))
        painter.drawText(14, 22, self._title)


    def _draw_overlays(self, painter: QPainter, image_x: int, image_y: int) -> None:
        if not self._overlay_items:
            return

        alpha = max(0, min(255, int(round(self.annotation_opacity * 255))))
        point_radius = max(2, int(round(3 * DPI_SCALE)))
        label_font = QFont()
        label_font.setPointSizeF(max(8.5, 9.0 * DPI_SCALE))
        label_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(label_font)

        for item in self._overlay_items:
            color_value = item["color"]
            color = QColor(*color_value) if isinstance(color_value, tuple) else QColor(color_value)
            fill_color = QColor(color)
            fill_color.setAlpha(alpha)
            outline_color = QColor(color)
            outline_color.setAlpha(230)

            points = item["points"]
            screen_points = [QPointF(image_x + x * self._zoom, image_y + y * self._zoom) for x, y in points]

            painter.setPen(QPen(outline_color, max(1.0, 1.2 * DPI_SCALE)))
            painter.setBrush(fill_color)

            if item["shape"] == "polygon":
                polygon = QPolygonF(screen_points)
                path = QPainterPath()
                path.addPolygon(polygon)
                path.closeSubpath()
                painter.drawPath(path)
            else:
                if len(screen_points) < 2:
                    continue
                left = min(point.x() for point in screen_points)
                top = min(point.y() for point in screen_points)
                right = max(point.x() for point in screen_points)
                bottom = max(point.y() for point in screen_points)
                painter.drawRect(QRectF(left, top, right - left, bottom - top))

            if self.show_points:
                painter.setBrush(QColor(255, 255, 255))
                painter.setPen(Qt.PenStyle.NoPen)
                for point in screen_points:
                    if point.x() < -point_radius or point.y() < -point_radius or point.x() > self.width() + point_radius or point.y() > self.height() + point_radius:
                        continue
                    painter.drawEllipse(point, point_radius, point_radius)

            if not self.show_labels:
                continue

            label = item["label"]
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(label)
            text_height = metrics.height()
            baseline = metrics.ascent()
            pad_x = max(4, int(round(6 * DPI_SCALE)))
            pad_y = max(3, int(round(4 * DPI_SCALE)))

            anchor = item["center"]
            anchor_x = image_x + anchor[0] * self._zoom
            anchor_y = image_y + anchor[1] * self._zoom
            box_width = text_width + pad_x * 2
            box_height = text_height + pad_y * 2
            box_x = max(0, min(int(round(anchor_x)), max(0, self.width() - box_width - 2)))
            box_y = max(0, min(int(round(anchor_y - box_height - 4)), max(0, self.height() - box_height - 2)))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 235))
            painter.drawRoundedRect(box_x, box_y, box_width, box_height, 4, 4)

            painter.setPen(QColor(18, 18, 18))
            text_x = box_x + pad_x
            text_y = box_y + box_height - pad_y - (text_height - baseline)
            painter.drawText(text_x, text_y, label)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._pixmap is None:
            return

        angle = event.angleDelta().y()
        if angle == 0:
            return

        cursor_position = event.position()
        cursor_x = cursor_position.x()
        cursor_y = cursor_position.y()
        image_x, image_y, _, _ = self._fit_display_rect()
        image_point_x = (cursor_x - image_x) / self._zoom
        image_point_y = (cursor_y - image_y) / self._zoom

        self._fit_mode = False
        zoom_factor = 1.12 if angle > 0 else 1.0 / 1.12
        new_zoom = max(0.05, min(40.0, self._zoom * zoom_factor))

        self._zoom = new_zoom
        new_scaled_width = max(1, int(round(self._pixmap.width() * self._zoom)))
        new_scaled_height = max(1, int(round(self._pixmap.height() * self._zoom)))
        self._offset = QPoint(
            int(round(cursor_x - (self.width() - new_scaled_width) / 2.0 - image_point_x * self._zoom)),
            int(round(cursor_y - (self.height() - new_scaled_height) / 2.0 - image_point_y * self._zoom)),
        )
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return

        current = event.position().toPoint()
        delta = current - self._drag_start
        self._offset += delta
        self._drag_start = current
        self._fit_mode = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode and self._pixmap is not None:
            self.fit_to_view()


class ClassBubbleButton(QWidget):
    """Colored pill button for a single class filter, with an inline color-picker pencil."""

    toggled = pyqtSignal(bool)

    def __init__(self, category_id: int, category_name: str, color: QColor, on_color_changed, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.category_id = category_id
        self.category_name = category_name
        self.category_color = color
        self._on_color_changed = on_color_changed

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(max(30, int(round(30 * DPI_SCALE))))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._pill = QPushButton(category_name)
        self._pill.setCheckable(True)
        self._pill.setChecked(True)
        self._pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pill.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._pill.setMinimumHeight(max(30, int(round(30 * DPI_SCALE))))
        self._pill.toggled.connect(self.toggled)
        self._pill.toggled.connect(self._refresh_style)
        layout.addWidget(self._pill)

        edit_size = max(30, int(round(30 * DPI_SCALE)))
        self._edit_btn = QPushButton()
        self._edit_btn.setIcon(QIcon(str(resource_path("assets", "edit_icon.svg"))))
        icon_px = max(16, int(round(16 * DPI_SCALE)))
        self._edit_btn.setIconSize(QSize(icon_px, icon_px))
        self._edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_btn.setFixedSize(edit_size, edit_size)
        self._edit_btn.clicked.connect(self._open_color_picker)
        layout.addWidget(self._edit_btn)

        self._refresh_style()

    def setChecked(self, checked: bool) -> None:
        self._pill.setChecked(checked)

    def isChecked(self) -> bool:
        return self._pill.isChecked()

    def blockSignals(self, block: bool) -> bool:
        self._pill.blockSignals(block)
        return super().blockSignals(block)

    def _refresh_style(self) -> None:
        base = self.category_color
        if self._pill.isChecked():
            background = f"rgba({base.red()}, {base.green()}, {base.blue()}, 255)"
            border = "rgba(0, 0, 0, 0.22)"
        else:
            background = f"rgba({base.red()}, {base.green()}, {base.blue()}, 120)"
            border = "rgba(0, 0, 0, 0.14)"

        self._pill.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {background};
                color: #111111;
                border: 1px solid {border};
                border-radius: 14px;
                padding: 7px 12px;
                text-align: left;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border: 1px solid rgba(0, 0, 0, 0.35);
            }}
            QPushButton:pressed {{
                background-color: rgba({base.red()}, {base.green()}, {base.blue()}, 210);
            }}
            """
        )
        self._edit_btn.setStyleSheet(
            """
            QPushButton {
                background: rgba(0, 0, 0, 0.20);
                border: none;
                border-radius: 12px;
                color: #ffffff;
                font-size: 13px;
            }
            QPushButton:hover {
                background: rgba(0, 0, 0, 0.38);
            }
            QPushButton:pressed {
                background: rgba(0, 0, 0, 0.52);
            }
            """
        )

    def _open_color_picker(self) -> None:
        dialog = QColorDialog(self.category_color, self)
        dialog.setWindowTitle(f"Color for {self.category_name}")
        dialog.setStyleSheet(
            """
            QDialog, QWidget {
                background: #26282d;
                color: #f4f4f5;
            }
            QPushButton {
                background: #505662;
                border: 1px solid #656d79;
                color: #fafafa;
                padding: 6px 14px;
                border-radius: 8px;
            }
            QPushButton:hover { background: #5c6370; }
            QPushButton:pressed { background: #444a55; }
            QLineEdit {
                background: #1d2024;
                border: 1px solid #5a606a;
                color: #f4f4f5;
                border-radius: 4px;
                padding: 2px 6px;
            }
            QSpinBox {
                background: #1d2024;
                border: 1px solid #5a606a;
                color: #f4f4f5;
                border-radius: 4px;
                padding: 2px 4px;
            }
            QSpinBox::up-button, QSpinBox::down-button { width: 0px; }
            QLabel { color: #f4f4f5; }
            """
        )
        if dialog.exec():
            color = dialog.selectedColor()
            if color.isValid():
                self.category_color = color
                self._refresh_style()
                self._on_color_changed(self.category_name, color)


class PyQtAnnotationReview(QMainWindow):
    """Main PyQt window that mirrors the previous OpenCV app behavior."""

    LAST_OPENED_DIRECTORY_FILE = Path.home() / ".annotation_review_last_directory"

    def __init__(self, input_path: str | None) -> None:
        super().__init__()
        self.input_path = input_path

        self._pixmap = None
        self._title = "Annotation Review"
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self._dragging = False
        self._drag_start = QPoint(0, 0)
        self._fit_mode = False
        self._overlay_items: list[dict] = []
        self.show_points = False
        self.show_labels = True
        self.annotation_opacity = 0.30

        self.dataset = None
        self.temp_extraction = None
        self.images_path = ""
        self.images = []
        self.annotations_by_image_id = {}
        self.categories_by_id = {}
        self.class_items: list[tuple[int, str]] = []
        self.current_class_items: list[tuple[int, str]] = []
        self.category_ids: list[int] = []
        self.current_category_ids: list[int] = []
        self.visible_by_category: dict[int, bool] = {}
        self.class_checkboxes: dict[int, ClassBubbleButton] = {}
        self.last_opened_directory = self._read_last_opened_directory()
        self._build_ui()
        self._apply_styles()

    def _draw_overlays(self, painter: QPainter, image_x: int, image_y: int) -> None:
        if not self._overlay_items:
            return

        alpha = max(0, min(255, int(round(self.annotation_opacity * 255))))
        point_radius = max(2, int(round(3 * DPI_SCALE)))
        label_font = QFont()
        label_font.setPointSizeF(max(8.5, 9.0 * DPI_SCALE))
        label_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(label_font)

        for item in self._overlay_items:
            color_value = item["color"]
            color = QColor(*color_value) if isinstance(color_value, tuple) else QColor(color_value)
            fill_color = QColor(color)
            fill_color.setAlpha(alpha)
            outline_color = QColor(color)
            outline_color.setAlpha(230)

            points = item["points"]
            screen_points = [QPointF(image_x + x * self._zoom, image_y + y * self._zoom) for x, y in points]

            painter.setPen(QPen(outline_color, max(1.0, 1.2 * DPI_SCALE)))
            painter.setBrush(fill_color)

            if item["shape"] == "polygon":
                polygon = QPolygonF(screen_points)
                path = QPainterPath()
                path.addPolygon(polygon)
                path.closeSubpath()
                painter.drawPath(path)
            else:
                if len(screen_points) < 2:
                    continue
                left = min(point.x() for point in screen_points)
                top = min(point.y() for point in screen_points)
                right = max(point.x() for point in screen_points)
                bottom = max(point.y() for point in screen_points)
                painter.drawRect(QRectF(left, top, right - left, bottom - top))

            if self.show_points:
                painter.setBrush(QColor(255, 255, 255))
                painter.setPen(Qt.PenStyle.NoPen)
                for point in screen_points:
                    if point.x() < -point_radius or point.y() < -point_radius or point.x() > self.width() + point_radius or point.y() > self.height() + point_radius:
                        continue
                    painter.drawEllipse(point, point_radius, point_radius)

            if not self.show_labels:
                continue

            label = item["label"]
            metrics = painter.fontMetrics()
            text_width = metrics.horizontalAdvance(label)
            text_height = metrics.height()
            baseline = metrics.ascent()
            pad_x = max(4, int(round(6 * DPI_SCALE)))
            pad_y = max(3, int(round(4 * DPI_SCALE)))

            anchor = item["center"]
            anchor_x = image_x + anchor[0] * self._zoom
            anchor_y = image_y + anchor[1] * self._zoom
            box_width = text_width + pad_x * 2
            box_height = text_height + pad_y * 2
            box_x = max(0, min(int(round(anchor_x)), max(0, self.width() - box_width - 2)))
            box_y = max(0, min(int(round(anchor_y - box_height - 4)), max(0, self.height() - box_height - 2)))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 235))
            painter.drawRoundedRect(box_x, box_y, box_width, box_height, 4, 4)

            painter.setPen(QColor(18, 18, 18))
            text_x = box_x + pad_x
            text_y = box_y + box_height - pad_y - (text_height - baseline)
            painter.drawText(text_x, text_y, label)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._pixmap is None:
            return

        angle = event.angleDelta().y()
        if angle == 0:
            return

        cursor_position = event.position()
        cursor_x = cursor_position.x()
        cursor_y = cursor_position.y()
        image_x, image_y, _, _ = self._fit_display_rect()
        image_point_x = (cursor_x - image_x) / self._zoom
        image_point_y = (cursor_y - image_y) / self._zoom

        self._fit_mode = False
        zoom_factor = 1.12 if angle > 0 else 1.0 / 1.12
        new_zoom = max(0.05, min(40.0, self._zoom * zoom_factor))

        self._zoom = new_zoom
        new_scaled_width = max(1, int(round(self._pixmap.width() * self._zoom)))
        new_scaled_height = max(1, int(round(self._pixmap.height() * self._zoom)))
        self._offset = QPoint(
            int(round(cursor_x - (self.width() - new_scaled_width) / 2.0 - image_point_x * self._zoom)),
            int(round(cursor_y - (self.height() - new_scaled_height) / 2.0 - image_point_y * self._zoom)),
        )
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return

        current = event.position().toPoint()
        delta = current - self._drag_start
        self._offset += delta
        self._drag_start = current
        self._fit_mode = False
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode and self._pixmap is not None:
            self.fit_to_view()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.canvas = ImageCanvas()
        main_layout.addWidget(self.canvas, 1)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(SIDEBAR_WIDTH)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(16, 16, 16, 16)
        sidebar_layout.setSpacing(12)
        main_layout.addWidget(self.sidebar)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        header = QLabel("Annotation Review")
        header.setObjectName("header")
        header_row.addWidget(header)
        header_row.addStretch()

        self.image_selector = QComboBox()
        self.image_selector.setMaximumWidth(200)
        self.image_selector.setPlaceholderText("Select image")
        image_selector_view = QListView()
        image_selector_view.setFrameShape(QFrame.Shape.NoFrame)
        image_selector_view.setContentsMargins(0, 0, 0, 0)
        image_selector_view.setSpacing(0)
        image_selector_view.setUniformItemSizes(True)
        image_selector_view.setAlternatingRowColors(False)
        image_selector_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        image_selector_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        image_selector_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        image_selector_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        image_selector_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        image_selector_view.setAutoFillBackground(True)
        image_selector_view.viewport().setAutoFillBackground(True)
        image_selector_view.setStyleSheet(
            """
            QListView {
                background: #1d2024;
                color: #f4f4f5;
                border-left: 1px solid #5a606a;
                border-right: 1px solid #5a606a;
                outline: none;
                padding: 0px;
                margin: 0px;
            }
            QListView::item {
                min-height: 24px;
                padding: 4px 6px;
                margin: 0px;
                border: none;
                background: transparent;
            }
            QListView::item:selected {
                background: #6a72e6;
                color: #ffffff;
            }
            QListView::item:hover {
                background: rgba(106, 114, 230, 0.35);
            }
            QListView::viewport {
                background: #1d2024;
                margin: 0px;
                padding: 0px;
            }
            """
        )
        self.image_selector.setView(image_selector_view)
        popup = self.image_selector.view().window()
        popup.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        popup.setAutoFillBackground(True)
        popup.setContentsMargins(0, 0, 0, 0)
        popup.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        if hasattr(popup, "setFrameShape"):
            popup.setFrameShape(QFrame.Shape.NoFrame)
            popup.setLineWidth(0)
            popup.setMidLineWidth(0)
        popup.setStyleSheet(
            """
            QFrame {
                background: #1d2024;
                border: none;
                margin: 0px;
                padding: 0px;
            }
            """
        )
        if popup.layout() is not None:
            popup.layout().setContentsMargins(0, 0, 0, 0)
            popup.layout().setSpacing(0)
        self.image_selector._pixmap = None
        self.image_selector._fit_mode = False
        self.image_selector._zoom = 1.0
        self.image_selector._offset = QPoint(0, 0)
        self.image_selector._dragging = False
        self.image_selector._drag_start = QPoint(0, 0)
        self.image_selector.currentIndexChanged.connect(self._on_image_selected)
        header_row.addWidget(self.image_selector)

        sidebar_layout.addLayout(header_row)

        subtitle = QLabel("PyQt viewer for COCO boxes and polygons")
        subtitle.setObjectName("muted")
        sidebar_layout.addWidget(subtitle)

        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        status_layout = QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(12, 12, 12, 12)
        status_layout.setSpacing(6)

        self.dataset_status = QLabel("No dataset loaded")
        self.dataset_status.setObjectName("statPrimary")
        status_layout.addWidget(self.dataset_status)

        self.image_status = QLabel("Open a dataset to begin")
        self.image_status.setObjectName("statSecondary")
        self.image_status.setWordWrap(True)
        status_layout.addWidget(self.image_status)

        zoom_copy_row = QHBoxLayout()
        self.zoom_status = QLabel("Zoom: 100%")
        self.zoom_status.setObjectName("statSecondary")
        zoom_copy_row.addWidget(self.zoom_status)
        zoom_copy_row.addStretch()
        self.copy_id_button = QPushButton("Copy ID")
        self.copy_id_button.setObjectName("copyIdButton")
        self.copy_id_button.setMaximumWidth(80)
        self.copy_id_button.setMinimumHeight(32)
        self.copy_id_button.clicked.connect(self._copy_image_id)
        zoom_copy_row.addWidget(self.copy_id_button)
        status_layout.addLayout(zoom_copy_row)

        sidebar_layout.addWidget(self.status_card)

        self.open_button = QPushButton("Open folder")
        self.open_button.setObjectName("primaryButton")
        self.open_button.clicked.connect(self._open_folder)
        sidebar_layout.addWidget(self.open_button)

        self.points_checkbox = QCheckBox("Show points")
        self.points_checkbox.stateChanged.connect(self._on_points_toggled)
        self.labels_checkbox = QCheckBox("Show labels")
        self.labels_checkbox.setChecked(True)
        self.labels_checkbox.stateChanged.connect(self._on_labels_toggled)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(self.points_checkbox)
        toggle_row.addWidget(self.labels_checkbox)
        sidebar_layout.addLayout(toggle_row)

        opacity_row = QHBoxLayout()
        opacity_label = QLabel("Opacity")
        opacity_row.addWidget(opacity_label)
        self.opacity_input = QSpinBox()
        self.opacity_input.setRange(0, 100)
        self.opacity_input.setValue(30)
        self.opacity_input.setFixedWidth(52)
        self.opacity_input.valueChanged.connect(self._on_overlay_setting_changed)
        opacity_row.addWidget(self.opacity_input)
        opacity_row.addStretch()
        self.reset_colors_button = QPushButton("Reset colors")
        self.reset_colors_button.clicked.connect(self._reset_colors_to_default)
        opacity_row.addWidget(self.reset_colors_button)
        sidebar_layout.addLayout(opacity_row)

        zoom_row = QHBoxLayout()
        self.fit_button = QPushButton("Fit")
        self.fit_button.clicked.connect(self._fit_to_view)
        zoom_row.addWidget(self.fit_button)

        self.original_button = QPushButton("100%")
        self.original_button.clicked.connect(self._original_size)
        zoom_row.addWidget(self.original_button)

        self.reset_button = QPushButton("Reset")
        self.reset_button.clicked.connect(self._original_size)
        zoom_row.addWidget(self.reset_button)
        sidebar_layout.addLayout(zoom_row)

        nav_row = QHBoxLayout()
        self.prev_button = QPushButton("Prev")
        self.prev_button.clicked.connect(self._prev_image)
        nav_row.addWidget(self.prev_button)

        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(self._next_image)
        nav_row.addWidget(self.next_button)
        sidebar_layout.addLayout(nav_row)

        class_label = QLabel("Classes")
        class_label.setObjectName("section")
        sidebar_layout.addWidget(class_label)

        self.class_list_container = QWidget()
        self.class_list_container.setObjectName("classListContainer")
        self.class_list_layout = QVBoxLayout(self.class_list_container)
        self.class_list_layout.setContentsMargins(0, 0, 0, 0)
        self.class_list_layout.setSpacing(8)
        self.class_list_layout.addStretch(1)

        self.class_scroll = QScrollArea()
        self.class_scroll.setWidgetResizable(True)
        self.class_scroll.setWidget(self.class_list_container)
        self.class_scroll.setObjectName("classScroll")
        sidebar_layout.addWidget(self.class_scroll, 1)

        class_action_row = QHBoxLayout()
        self.show_all_button = QPushButton("Show all")
        self.show_all_button.clicked.connect(self._show_all_classes)
        class_action_row.addWidget(self.show_all_button)

        self.hide_all_button = QPushButton("Hide all")
        self.hide_all_button.clicked.connect(self._hide_all_classes)
        class_action_row.addWidget(self.hide_all_button)
        sidebar_layout.addLayout(class_action_row)

        self.footer_hint = QLabel("Wheel to zoom | Drag to pan | F fit | 1 100% | R reset | O open")
        self.footer_hint.setObjectName("muted")
        self.footer_hint.setWordWrap(True)
        sidebar_layout.addWidget(self.footer_hint)

    def _apply_styles(self) -> None:
        base_font_size = max(10, int(round(10 * DPI_SCALE)))
        header_size = max(14, int(round(14 * DPI_SCALE)))
        checkmark_path = resource_path("assets", "checkbox_checked.svg").as_posix()
        self.setStyleSheet(
            f"""
            QWidget {{
                font-size: {base_font_size}pt;
                color: #f4f4f5;
            }}
            QMainWindow {{
                background: #111315;
            }}
            #sidebar {{
                background: #26282d;
                border-left: 1px solid #3a3f46;
            }}
            QLabel#header {{
                font-size: {header_size}pt;
                font-weight: 700;
                color: #f6f7f8;
            }}
            QLabel#section {{
                font-weight: 650;
                color: #eceef2;
                padding-top: 2px;
            }}
            QLabel#muted {{
                color: #c2c6ce;
            }}
            QLabel#statPrimary {{
                font-weight: 700;
                color: #f6f7f8;
            }}
            QLabel#statSecondary {{
                color: #c2c6ce;
            }}
            QFrame#statusCard {{
                background: #1c1f24;
                border: 1px solid #3a4048;
                border-radius: 12px;
            }}
            QPushButton {{
                background: #505662;
                border: 1px solid #656d79;
                color: #fafafa;
                padding: 7px 10px;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: #5c6370;
            }}
            QPushButton:pressed {{
                background: #444a55;
            }}
            QPushButton#copyIdButton {{
                background: #505662;
                border: 1px solid #656d79;
                color: #fafafa;
                padding: 7px 14px;
                border-radius: 8px;
                min-width: 80px;
            }}
            QPushButton#copyIdButton:hover {{
                background: #5c6370;
            }}
            QPushButton#copyIdButton:pressed {{
                background: #444a55;
            }}
            QPushButton#primaryButton {{
                background: #6a72e6;
                border: 1px solid #8088ff;
            }}
            QPushButton#primaryButton:hover {{
                background: #7780f0;
            }}
            QCheckBox {{
                spacing: 6px;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 3px;
                border: 1px solid #b9b9b9;
                background: #ffffff;
            }}
            QCheckBox::indicator:checked {{
                background: #ffffff;
                border: 1px solid #b9b9b9;
                image: url("{checkmark_path}");
            }}
            QSpinBox {{
                background: #1d2024;
                border: 1px solid #5a606a;
                border-radius: 6px;
                padding: 3px 6px;
                min-height: 24px;
            }}
            QComboBox {{
                background: #1d2024;
                border: 1px solid #5a606a;
                border-radius: 6px;
                padding: 3px 6px;
                min-height: 24px;
                color: #f4f4f5;
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox::down-arrow {{
                image: none;
                color: #f4f4f5;
            }}
            QComboBox QAbstractItemView {{
                background: #1d2024;
                color: #f4f4f5;
                selection-background-color: #6a72e6;
                border: none;
                outline: none;
                show-decoration-selected: 0;
                margin: 0px;
                padding: 0px;
                spacing: 0px;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 4px 6px;
                margin: 0px;
                height: 24px;
                border: none;
            }}
            QComboBox QAbstractItemView::item:selected {{
                background-color: #6a72e6;
            }}
            QComboBox QFrame {{
                margin: 0px;
                padding: 0px;
                border: none;
            }}
            QComboBox QScrollBar {{     
                background: transparent;
                margin: 0px;
                    padding: 0px;
                }}
                QAbstractItemView {{
                    margin: 0px;
                    padding: 0px;
                }}
                QAbstractScrollArea {{
                    margin: 0px;
                    padding: 0px;
                }}
            QScrollArea#classScroll {{
                background: #1c1f24;
                border: 1px solid #3a4048;
                border-radius: 10px;
            }}
            QScrollArea#classScroll QWidget#classListContainer {{
                background: #1c1f24;
            }}
            QScrollArea#classScroll::viewport {{
                background: #1c1f24;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 12px;
                margin: 4px 3px 4px 3px;
            }}
            QScrollBar::handle:vertical {{
                background: #ffffff;
                min-height: 28px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #f3f3f3;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                background: transparent;
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            """
        )

    def _clear_class_checkboxes(self) -> None:
        while self.class_list_layout.count() > 1:
            item = self.class_list_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.class_checkboxes.clear()

    def _populate_class_checkboxes(self) -> None:
        self._clear_class_checkboxes()
        for category_id, category_name in self.current_class_items:
            bubble = ClassBubbleButton(category_id, str(category_name), color_for_class(str(category_name), int(category_id)), self._on_bubble_color_changed, self.class_list_container)
            bubble.setChecked(self.visible_by_category.get(category_id, True))
            bubble.toggled.connect(lambda checked, cid=category_id: self._on_class_toggled(cid, checked))
            self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, bubble)
            self.class_checkboxes[category_id] = bubble

    def _show_message(self, text: str, title: str = "Annotation Review") -> None:
        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.NoIcon)
        message_box.setWindowTitle(title)
        message_box.setText(text)
        message_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setStyleSheet(
            "QLabel { color: #1f2328; } QMessageBox { background: #ffffff; } QPushButton { min-width: 72px; }"
        )
        message_box.exec()

    def _read_last_opened_directory(self) -> str | None:
        try:
            stored_path = self.LAST_OPENED_DIRECTORY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            return None

        if stored_path and Path(stored_path).is_dir():
            return stored_path
        return None

    def _save_last_opened_directory(self, selected_path: str) -> None:
        path = Path(selected_path)
        directory = path if path.is_dir() else path.parent
        self.last_opened_directory = str(directory)
        try:
            self.LAST_OPENED_DIRECTORY_FILE.write_text(str(directory), encoding="utf-8")
        except OSError:
            pass

    def _copy_image_id(self) -> None:
        """Copy the last 9 characters of the image filename (without extension) to clipboard."""
        if not self.current_image_name:
            return
        
        basename = os.path.basename(self.current_image_name)
        # Remove file extension
        name_without_ext = os.path.splitext(basename)[0]
        # Get last 9 characters
        image_id = name_without_ext[-9:] if len(name_without_ext) >= 9 else name_without_ext
        
        # Copy to clipboard
        clipboard = QApplication.clipboard()
        clipboard.setText(image_id)
        
        # Optional: Show brief confirmation
        self.copy_id_button.setText("Copied!")
        # Reset button text after 1.5 seconds
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self.copy_id_button.setText("Copy ID") if self.copy_id_button else None)

    def _open_folder(self) -> None:
        initial_directory = self.last_opened_directory or str(Path.cwd())
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select COCO export zip",
            initial_directory,
            "Zip archives (*.zip);;All files (*.*)",
        )
        selected_path = selected_file
        if not selected_path:
            selected_path = QFileDialog.getExistingDirectory(self, "Select extracted COCO folder", initial_directory)
        if selected_path:
            self._save_last_opened_directory(selected_path)
            self._load_dataset(selected_path)

    def _load_dataset(self, selected_path: str) -> None:
        try:
            loaded_dataset = load_dataset_from_input(selected_path)
        except Exception as error:
            self._show_message(f"Dataset selection failed: {error}")
            return

        self._cleanup_temp_extraction()
        self._save_last_opened_directory(selected_path)

        self.dataset = loaded_dataset
        self.temp_extraction = loaded_dataset["temp_extraction"]
        self.images_path = loaded_dataset["images_path"]
        self.images = loaded_dataset["images"]
        self.annotations_by_image_id = loaded_dataset["annotations_by_image_id"]
        self.categories_by_id = loaded_dataset["categories_by_id"]
        self.class_items = loaded_dataset["class_items"]
        self.category_ids = [category_id for category_id, _name in self.class_items]
        self.current_class_items = []
        self.current_category_ids = []
        self.visible_by_category = loaded_dataset["visible_by_category"]
        self.has_polygons = loaded_dataset.get("has_polygons", False)
        self.index = 0

        self._populate_image_selector()
        self._load_current_image(reset_fit=True)
        self._update_status_labels()
        self._sync_canvas_state()

    def _cleanup_temp_extraction(self) -> None:
        if self.temp_extraction is not None:
            self.temp_extraction.cleanup()
            self.temp_extraction = None

    def _on_class_toggled(self, category_id: int, checked: bool) -> None:
        self.visible_by_category[category_id] = bool(checked)
        self._refresh_overlay_items()

    def _on_bubble_color_changed(self, category_name: str, color: QColor) -> None:
        _COLOR_CONFIG[category_name] = color.name().upper()
        save_color_config()
        self._refresh_overlay_items()

    def _reset_colors_to_default(self) -> None:
        path = _default_color_config_path()
        if not path.exists():
            return
        try:
            with path.open(encoding="utf-8") as f:
                defaults = json.load(f)
        except Exception:
            return
        _COLOR_CONFIG.clear()
        _COLOR_CONFIG.update({k: v for k, v in defaults.items() if isinstance(k, str) and isinstance(v, str)})
        save_color_config()
        self._populate_class_checkboxes()
        self._refresh_overlay_items()

    def _on_points_toggled(self, state: int) -> None:
        self.show_points = state == int(Qt.CheckState.Checked.value)
        self._sync_canvas_state()
        self.canvas.update()

    def _on_labels_toggled(self, state: int) -> None:
        self.show_labels = state == int(Qt.CheckState.Checked.value)
        self._sync_canvas_state()
        self.canvas.update()

    def _on_overlay_setting_changed(self) -> None:
        self._sync_canvas_state()
        self.canvas.update()

    def _show_all_classes(self) -> None:
        for category_id in self.current_category_ids:
            self.visible_by_category[category_id] = True
            bubble = self.class_checkboxes.get(category_id)
            if bubble is not None:
                bubble.blockSignals(True)
                bubble.setChecked(True)
                bubble.blockSignals(False)
                bubble._refresh_style()  # Manually refresh visual style
        self._refresh_overlay_items()

    def _hide_all_classes(self) -> None:
        for category_id in self.current_category_ids:
            self.visible_by_category[category_id] = False
            bubble = self.class_checkboxes.get(category_id)
            if bubble is not None:
                bubble.blockSignals(True)
                bubble.setChecked(False)
                bubble.blockSignals(False)
                bubble._refresh_style()  # Manually refresh visual style
        self._refresh_overlay_items()

    def _populate_image_selector(self) -> None:
        """Fill the image selector combo box with all available images."""
        if not hasattr(self, 'image_selector') or self.image_selector is None:
            return
        
        self.image_selector.blockSignals(True)
        self.image_selector.clear()
        
        # Add all images to the selector
        if self.images:
            for i, image_info in enumerate(self.images):
                if isinstance(image_info, dict):
                    full_path = image_info.get("file_name", f"Image {i+1}")
                    # Extract only the filename without the path
                    filename = os.path.basename(str(full_path))
                else:
                    filename = f"Image {i+1}"
                self.image_selector.addItem(filename, i)
            
            # Keep the placeholder text visible by not setting current index
            # This way users always see "Select image" in the combo box
            self.image_selector.setCurrentIndex(-1)
        
        self.image_selector.blockSignals(False)
        
        # Manually trigger the selection handler
        if self.images:
            self._on_image_selected(self.index)

    def _on_image_selected(self, index: int) -> None:
        """Handle image selection from the combo box."""
        if not self.images or index < 0 or index >= len(self.images):
            return
        
        # Always load the selected image, even if index hasn't changed
        # This ensures visibility is reset
        self.index = index
        self._load_current_image(reset_fit=True)
        
        # Reset to placeholder after loading
        if self.image_selector:
            self.image_selector.blockSignals(True)
            self.image_selector.setCurrentIndex(-1)
            self.image_selector.blockSignals(False)

    def _sync_canvas_state(self) -> None:
        self.canvas.show_points = self.show_points
        self.canvas.show_labels = self.show_labels
        self.canvas.annotation_opacity = self.opacity_input.value() / 100.0

    def _next_image(self) -> None:
        if not self.images:
            return
        self.index += 1
        if self.index >= len(self.images):
            self._show_message("Last image completed")
            self._clear_dataset()
            return
        self._load_current_image(reset_fit=True)
        if self.image_selector:
            self.image_selector.blockSignals(True)
            self.image_selector.setCurrentIndex(-1)  # Keep placeholder visible
            self.image_selector.blockSignals(False)

    def _prev_image(self) -> None:
        if not self.images:
            return
        self.index = max(0, self.index - 1)
        self._load_current_image(reset_fit=True)
        if self.image_selector:
            self.image_selector.blockSignals(True)
            self.image_selector.setCurrentIndex(-1)  # Keep placeholder visible
            self.image_selector.blockSignals(False)

    def _fit_to_view(self) -> None:
        self.canvas.fit_to_view()
        self._update_status_labels()

    def _original_size(self) -> None:
        self.canvas.reset_to_original_size()
        self._update_status_labels()

    def _clear_dataset(self) -> None:
        self.dataset = None
        self.images_path = ""
        self.images = []
        self.annotations_by_image_id = {}
        self.categories_by_id = {}
        self.class_items = []
        self.current_class_items = []
        self.category_ids = []
        self.current_category_ids = []
        self.visible_by_category = {}
        self.current_image_path = ""
        self.current_annotations = []
        self.current_image_name = ""
        self.current_overlay_items = []
        self._cleanup_temp_extraction()
        self._clear_class_checkboxes()
        if self.image_selector:
            self.image_selector.clear()
        self.canvas.set_idle()
        self._sync_canvas_state()
        self._update_status_labels()

    def _build_overlay_items(self, annotations) -> list[dict]:
        overlay_items: list[dict] = []

        for annotation in annotations:
            category_id = annotation.get("category_id")
            if not self.visible_by_category.get(category_id, True):
                continue

            category_name = self.categories_by_id.get(category_id, str(category_id if category_id is not None else "unknown"))
            category_color = color_for_class(str(category_name), int(category_id) if category_id is not None else 0)
            color_rgb = (category_color.red(), category_color.green(), category_color.blue())

            if self.has_polygons:
                segmentation = annotation.get("segmentation", [])
                if isinstance(segmentation, dict):
                    continue
                for segment in segmentation:
                    if not isinstance(segment, list) or len(segment) < 6:
                        continue
                    points = [(float(segment[index]), float(segment[index + 1])) for index in range(0, len(segment), 2)]
                    center = polygon_centroid(points)
                    overlay_items.append(
                        {
                            "shape": "polygon",
                            "points": points,
                            "center": center,
                            "label": str(category_name),
                            "color": color_rgb,
                        }
                    )
            else:
                bbox = annotation.get("bbox", [])
                if not bbox or len(bbox) < 4:
                    continue
                x, y, width, height = bbox
                points = [(float(x), float(y)), (float(x + width), float(y)), (float(x + width), float(y + height)), (float(x), float(y + height))]
                center = (float(x + width / 2.0), float(y + height / 2.0))
                overlay_items.append(
                    {
                        "shape": "bbox",
                        "points": points,
                        "center": center,
                        "label": str(category_name),
                        "color": color_rgb,
                    }
                )

        return overlay_items

    def _refresh_overlay_items(self) -> None:
        if not self.dataset:
            return
        self.current_overlay_items = self._build_overlay_items(self.current_annotations)
        self.canvas.set_overlay_items(self.current_overlay_items)
        self.canvas.update()
        self._update_status_labels()

    def _load_current_image(self, reset_fit: bool) -> None:
        if not self.dataset or not self.images:
            self.canvas.set_idle()
            return

        if self.index >= len(self.images):
            self._show_message("Last image completed")
            self._clear_dataset()
            return

        image_info = self.images[self.index]
        self.current_image_name = str(image_info.get("file_name", ""))
        self.current_image_path = resolve_image_path(self.images_path, self.current_image_name)
        image_id = image_info.get("id")
        self.current_annotations = self.annotations_by_image_id.get(image_id, []) if image_id is not None else []
        current_category_ids = []
        current_class_items = []
        seen_category_ids: set[int] = set()

        for annotation in self.current_annotations:
            category_id = annotation.get("category_id")
            if category_id is None or category_id in seen_category_ids:
                continue
            seen_category_ids.add(category_id)
            current_category_ids.append(category_id)
            category_name = self.categories_by_id.get(category_id, str(category_id))
            current_class_items.append((category_id, str(category_name)))

        current_class_items.sort(key=lambda item: str(item[1]).lower())
        self.current_category_ids = current_category_ids
        self.current_class_items = current_class_items
        
        # Reset visibility for all current classes to True (show all by default)
        for category_id in self.current_category_ids:
            self.visible_by_category[category_id] = True
        
        self.current_overlay_items = self._build_overlay_items(self.current_annotations)

        title = f"{self.index + 1}/{len(self.images)} - {os.path.basename(self.current_image_name)}"
        if not self.canvas.load_image(self.current_image_path, ""):
            self._show_message(f"Could not read image at: {self.current_image_path}")
            self._next_image()
            return

        self.canvas.set_overlay_items(self.current_overlay_items)
        self._sync_canvas_state()
        if reset_fit:
            self.canvas.fit_to_view()
        self.setWindowTitle(title)
        self._populate_class_checkboxes()
        self._update_status_labels()

    def _update_status_labels(self) -> None:
        if not self.dataset:
            self.dataset_status.setText("No dataset loaded")
            self.image_status.setText("Open a dataset to begin")
            self.zoom_status.setText("Zoom: 100%")
            self.copy_id_button.setEnabled(False)
            return

        self.copy_id_button.setEnabled(True)
        self.dataset_status.setText(f"{len(self.images)} images • {len(self.class_items)} classes")

        if self.images:
            visible_count = len(self.current_overlay_items)
            current_name = os.path.basename(self.current_image_name) if self.current_image_name else ""
            self.image_status.setText(
                f"Image {self.index + 1}/{len(self.images)} • {current_name} • {len(self.current_class_items)} classes on image • {visible_count} visible annotations"
            )
        else:
            self.image_status.setText("Dataset loaded")

        self.zoom_status.setText(f"Zoom: {int(round(self.canvas.zoom * 100))}%")

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
            self.close()
            return
        if key in (Qt.Key.Key_N, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._next_image()
            return
        if key == Qt.Key.Key_P:
            self._prev_image()
            return
        if key == Qt.Key.Key_R:
            self._original_size()
            return
        if key == Qt.Key.Key_F:
            self._fit_to_view()
            return
        if key == Qt.Key.Key_1:
            self._original_size()
            return
        if key == Qt.Key.Key_O:
            self._open_folder()
            return
        if key == Qt.Key.Key_A:
            self._show_all_classes()
            return
        if key == Qt.Key.Key_X:
            self._hide_all_classes()
            return
        if key == Qt.Key.Key_T:
            self.points_checkbox.toggle()
            return
        if key == Qt.Key.Key_L:
            self.labels_checkbox.toggle()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._cleanup_temp_extraction()
        super().closeEvent(event)


def main() -> None:
    """Run the PyQt annotation review application."""
    args = parse_arguments()

    if args.self_test:
        print("Self-test OK: arguments parsed, dependencies imported.")
        return

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("Annotation Review")
    app.setStyle("Fusion")

    window = PyQtAnnotationReview(args.input_path)
    window.resize(int(round(MONITOR_WIDTH * 0.9)), int(round(MONITOR_HEIGHT * 0.87)))
    window.showMaximized()

    sys.exit(app.exec())
