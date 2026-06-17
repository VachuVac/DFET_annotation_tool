"""PyQt-based main application for annotation review."""

from __future__ import annotations

import colorsys
import copy
import json
import os
import shutil
import sys
from pathlib import Path

from .constants import DPI_SCALE, MONITOR_HEIGHT, MONITOR_WIDTH, SIDEBAR_WIDTH
from .data_loading import (
    load_dataset_from_folder_and_json,
    load_dataset_from_input,
    resolve_image_path,
)
from .rle import is_rle_segmentation, rle_to_polygons
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
        QKeySequence,
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
        QDialog,
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
        QSlider,
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


def _shortcut_config_path() -> Path:
    """User-editable keyboard shortcut overrides (next to the exe when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "shortcuts.json"
    return Path(__file__).resolve().parent / "shortcuts.json"


def load_shortcut_config() -> dict[str, str]:
    path = _shortcut_config_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def save_shortcut_config(bindings: dict[str, str]) -> None:
    path = _shortcut_config_path()
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(bindings, f, indent=4, ensure_ascii=False)
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

    # Emitted after a vertex drag finishes with a before/after edit record (a dict),
    # so the window can mark unsaved edits and push an undo step.
    annotationsChanged = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._title = "Annotation Workbench"
        self._zoom = 1.0
        self._offset = QPoint(0, 0)
        self._dragging = False
        self._drag_start = QPoint(0, 0)
        self._fit_mode = False
        self._overlay_items: list[dict] = []
        self.show_points = False
        self.show_labels = True
        self.annotation_opacity = 0.30
        # Editing state: drag annotation vertices/corners directly on the canvas.
        # Off by default; toggled on via the "Edit objects" button.
        self._edit_enabled = False
        self._drag_vertex: tuple[dict, int] | None = None
        self._drag_before: dict | None = None
        self._vertex_moved = False
        self._hover_vertex: tuple[dict, int] | None = None
        self._handle_size = max(3, int(round(4 * DPI_SCALE)))
        self._handle_hit_radius = max(9, int(round(11 * DPI_SCALE)))
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

    def set_idle(self, title: str = "Annotation Workbench") -> None:
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
        painter.drawText(card_rect.adjusted(24, 58, -24, -16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "Open zip / folder to load a COCO export, or Open images + JSON to pick an images folder and a COCO JSON file.")

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
        self._draw_handles(painter, image_x, image_y)

        title_font = QFont()
        title_font.setPointSizeF(max(9.0, 9.0 * DPI_SCALE))
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(244, 244, 244))
        painter.drawText(14, 22, self._title)

    def set_edit_enabled(self, enabled: bool) -> None:
        """Enable/disable vertex editing (handles + drag). Clears transient state when off."""
        self._edit_enabled = enabled
        if not enabled:
            self._hover_vertex = None
            self._drag_vertex = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()

    def _image_from_screen(self, screen_x: float, screen_y: float) -> tuple[float, float]:
        """Convert a screen position back into image (pixel) coordinates."""
        image_x, image_y, _, _ = self._fit_display_rect()
        if self._zoom <= 0:
            return 0.0, 0.0
        return (screen_x - image_x) / self._zoom, (screen_y - image_y) / self._zoom

    def _hit_test_vertex(self, position) -> tuple[dict, int] | None:
        """Return (overlay_item, vertex_index) of the editable handle under the cursor, if any."""
        if not self._edit_enabled or self._pixmap is None:
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        cursor_x, cursor_y = position.x(), position.y()
        best_hit: tuple[dict, int] | None = None
        best_distance = float(self._handle_hit_radius) ** 2
        # Reverse so the topmost (last-drawn) handle wins on overlap.
        for item in reversed(self._overlay_items):
            if not item.get("editable"):
                continue
            for index, (point_x, point_y) in enumerate(item["points"]):
                screen_x = image_x + point_x * self._zoom
                screen_y = image_y + point_y * self._zoom
                distance = (screen_x - cursor_x) ** 2 + (screen_y - cursor_y) ** 2
                if distance <= best_distance:
                    best_distance = distance
                    best_hit = (item, index)
        return best_hit

    def _apply_vertex_drag(self, item: dict, index: int, image_x: float, image_y: float) -> None:
        """Move one vertex to (image_x, image_y) and write the change back to its annotation."""
        annotation = item.get("annotation")
        if item.get("shape") == "bbox":
            # Keep the rectangle axis-aligned by pinning the opposite corner.
            points = item["points"]
            fixed_x, fixed_y = points[(index + 2) % 4]
            new_x = min(fixed_x, image_x)
            new_y = min(fixed_y, image_y)
            width = abs(fixed_x - image_x)
            height = abs(fixed_y - image_y)
            item["points"] = [
                (new_x, new_y),
                (new_x + width, new_y),
                (new_x + width, new_y + height),
                (new_x, new_y + height),
            ]
            item["center"] = (new_x + width / 2.0, new_y + height / 2.0)
            if annotation is not None:
                annotation["bbox"] = [new_x, new_y, width, height]
                annotation["area"] = width * height
        else:
            points = item["points"]
            if 0 <= index < len(points):
                points[index] = (image_x, image_y)
                item["center"] = polygon_centroid(points)
            if annotation is not None:
                self._write_polygon_vertex(annotation, int(item.get("seg_index", 0)), index, image_x, image_y)

    @staticmethod
    def _write_polygon_vertex(annotation: dict, seg_index: int, point_index: int, x: float, y: float) -> None:
        """Write a moved polygon vertex back into the annotation, refreshing bbox/area."""
        segmentation = annotation.get("segmentation")
        if not isinstance(segmentation, list) or seg_index >= len(segmentation):
            return
        coordinates = segmentation[seg_index]
        if isinstance(coordinates, list) and 2 * point_index + 1 < len(coordinates):
            coordinates[2 * point_index] = x
            coordinates[2 * point_index + 1] = y

        xs: list[float] = []
        ys: list[float] = []
        area = 0.0
        for segment in segmentation:
            if not isinstance(segment, list) or len(segment) < 6:
                continue
            count = len(segment) // 2
            running = 0.0
            for k in range(count):
                x1, y1 = segment[2 * k], segment[2 * k + 1]
                nx, ny = segment[2 * ((k + 1) % count)], segment[2 * ((k + 1) % count) + 1]
                xs.append(x1)
                ys.append(y1)
                running += x1 * ny - nx * y1
            area += abs(running) / 2.0
        if xs and ys:
            min_x, min_y = min(xs), min(ys)
            annotation["bbox"] = [min_x, min_y, max(xs) - min_x, max(ys) - min_y]
            annotation["area"] = area

    def _draw_handles(self, painter: QPainter, image_x: int, image_y: int) -> None:
        """Draw grab handles at every editable vertex so they can be dragged."""
        if not self._edit_enabled or not self._overlay_items:
            return
        size = self._handle_size
        for item in self._overlay_items:
            if not item.get("editable"):
                continue
            hover_item = self._hover_vertex is not None and self._hover_vertex[0] is item
            for index, (point_x, point_y) in enumerate(item["points"]):
                screen_x = image_x + point_x * self._zoom
                screen_y = image_y + point_y * self._zoom
                if screen_x < -size or screen_y < -size or screen_x > self.width() + size or screen_y > self.height() + size:
                    continue
                hovered = hover_item and self._hover_vertex[1] == index
                radius = size + 2 if hovered else size
                painter.setPen(QPen(QColor(20, 20, 20), 1))
                painter.setBrush(QColor(106, 114, 230) if hovered else QColor(255, 255, 255))
                painter.drawRect(QRectF(screen_x - radius, screen_y - radius, radius * 2, radius * 2))

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
            # Grabbing a handle edits a vertex; otherwise the drag pans the image.
            hit = self._hit_test_vertex(event.position())
            if hit is not None:
                self._drag_vertex = hit
                self._drag_before = self._snapshot_annotation(hit[0].get("annotation"))
                self._vertex_moved = False
                self._dragging = False
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                return
            self._dragging = True
            self._drag_start = event.position().toPoint()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_vertex is not None:
            item, index = self._drag_vertex
            image_x, image_y = self._image_from_screen(event.position().x(), event.position().y())
            self._apply_vertex_drag(item, index, image_x, image_y)
            self._vertex_moved = True
            self.update()
            return

        if self._dragging:
            current = event.position().toPoint()
            delta = current - self._drag_start
            self._offset += delta
            self._drag_start = current
            self._fit_mode = False
            self.update()
            return

        self._update_hover(event.position())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            annotation = self._drag_vertex[0].get("annotation") if self._drag_vertex is not None else None
            edited = self._drag_vertex is not None and self._vertex_moved and annotation is not None
            before = self._drag_before
            self._drag_vertex = None
            self._drag_before = None
            self._vertex_moved = False
            self._dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._hover_vertex else Qt.CursorShape.ArrowCursor)
            if edited:
                record = {
                    "annotation": annotation,
                    "before": before,
                    "after": self._snapshot_annotation(annotation),
                }
                self.annotationsChanged.emit(record)

    @staticmethod
    def _snapshot_annotation(annotation: dict | None) -> dict | None:
        """Deep-copy the geometry fields that a vertex edit can change, for undo/redo."""
        if annotation is None:
            return None
        return {
            "bbox": copy.deepcopy(annotation.get("bbox")),
            "area": annotation.get("area"),
            "segmentation": copy.deepcopy(annotation.get("segmentation")),
        }

    def _update_hover(self, position) -> None:
        """Highlight the handle under the cursor and switch the cursor shape."""
        hit = self._hit_test_vertex(position)
        new_key = (id(hit[0]), hit[1]) if hit is not None else None
        old_key = (id(self._hover_vertex[0]), self._hover_vertex[1]) if self._hover_vertex is not None else None
        if new_key != old_key:
            self._hover_vertex = hit
            self.setCursor(Qt.CursorShape.OpenHandCursor if hit else Qt.CursorShape.ArrowCursor)
            self.update()

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


class KeyCapButton(QPushButton):
    """A pill button that shows a shortcut binding and, while listening, captures a new key.

    Left-click toggles listening (select / deselect). While listening, the next key
    combination is captured and emitted; lone modifiers and Esc are ignored (Esc cancels).
    """

    captured = pyqtSignal(str)  # canonical key string, e.g. "Ctrl+Shift+Z"
    cancelled = pyqtSignal()

    def __init__(self, binding_text: str, parent=None) -> None:
        super().__init__(binding_text, parent)
        self._binding_text = binding_text
        self._listening = False
        self.setObjectName("keyCap")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_binding_text(self, text: str) -> None:
        self._binding_text = text
        if not self._listening:
            self.setText(text)

    def set_listening(self, on: bool) -> None:
        self._listening = on
        self.setProperty("listening", "true" if on else "false")
        self.setText("Press a key…" if on else self._binding_text)
        self.style().unpolish(self)
        self.style().polish(self)
        if on:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if not self._listening:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in (
            Qt.Key.Key_Control,
            Qt.Key.Key_Shift,
            Qt.Key.Key_Alt,
            Qt.Key.Key_Meta,
        ):
            return  # wait for a non-modifier key
        if key == Qt.Key.Key_Escape:
            self.cancelled.emit()
            return
        seq = QKeySequence(event.keyCombination()).toString()
        if seq:
            self.captured.emit(seq)
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._listening:
            event.accept()
            return
        super().keyReleaseEvent(event)


class ShortcutsDialog(QDialog):
    """Dark-themed help & shortcuts window with inline shortcut rebinding."""

    def __init__(self, owner: "PyQtAnnotationReview") -> None:
        super().__init__(owner)
        self._owner = owner
        self._edit_mode = False
        self._listening_cap: KeyCapButton | None = None
        self._caps: dict[str, KeyCapButton] = {}
        self.setWindowTitle("Keyboard shortcuts")
        self.setModal(True)
        self.setMinimumWidth(max(420, int(round(440 * DPI_SCALE))))
        self._build_ui()
        self._apply_styles()

    # ----- layout ---------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = QLabel("Keyboard shortcuts")
        title.setObjectName("dlgHeader")
        header_row.addWidget(title)
        header_row.addStretch()

        self.edit_button = QPushButton("Edit")
        self.edit_button.setObjectName("toggleButton")
        self.edit_button.setCheckable(True)
        self.edit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_button.setToolTip("Edit shortcuts: click a key, then press a new one")
        self.edit_button.toggled.connect(self._on_edit_toggled)
        header_row.addWidget(self.edit_button)
        layout.addLayout(header_row)

        self.banner = QLabel("")
        self.banner.setObjectName("dlgBanner")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        layout.addWidget(self.banner)

        # Mouse section (fixed / descriptive)
        layout.addWidget(self._section_label("Mouse"))
        for desc, action in (
            ("Wheel", "zoom"),
            ("Drag empty space", "pan"),
            ("Drag a handle", "move a box corner / polygon vertex"),
        ):
            layout.addLayout(self._fixed_row(desc, action))

        # Keyboard section (editable)
        layout.addWidget(self._section_label("Keyboard"))
        for action_id in self._owner.shortcut_order():
            layout.addLayout(self._editable_row(action_id))

        layout.addStretch()

        button_row = QHBoxLayout()
        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.setToolTip("Restore the built-in keyboard shortcuts")
        self.reset_button.clicked.connect(self._reset_defaults)
        button_row.addWidget(self.reset_button)
        button_row.addStretch()
        self.close_button = QPushButton("Done")
        self.close_button.setObjectName("primaryButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.accept)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("dlgSection")
        return label

    def _fixed_row(self, description: str, action: str) -> QHBoxLayout:
        row = QHBoxLayout()
        action_label = QLabel(action)
        action_label.setObjectName("dlgAction")
        row.addWidget(action_label)
        row.addStretch()
        cap = QLabel(description)
        cap.setObjectName("keyCapFixed")
        row.addWidget(cap)
        return row

    def _editable_row(self, action_id: str) -> QHBoxLayout:
        row = QHBoxLayout()
        action_label = QLabel(self._owner.shortcut_label(action_id))
        action_label.setObjectName("dlgAction")
        row.addWidget(action_label)
        row.addStretch()

        alias_text = self._owner.shortcut_alias_text(action_id)
        if alias_text:
            alias_label = QLabel(f"or {alias_text}")
            alias_label.setObjectName("dlgAlias")
            row.addWidget(alias_label)

        cap = KeyCapButton(self._owner.shortcut_primary(action_id))
        cap.setEnabled(False)  # only clickable in edit mode
        cap.clicked.connect(lambda _=False, aid=action_id: self._toggle_listening(aid))
        cap.captured.connect(lambda seq, aid=action_id: self._on_captured(aid, seq))
        cap.cancelled.connect(self._cancel_listening)
        self._caps[action_id] = cap
        row.addWidget(cap)
        return row

    # ----- editing --------------------------------------------------------
    def _on_edit_toggled(self, checked: bool) -> None:
        self._edit_mode = checked
        self._cancel_listening()
        for cap in self._caps.values():
            cap.setEnabled(checked)
            cap.setProperty("editable", "true" if checked else "false")
            cap.style().unpolish(cap)
            cap.style().polish(cap)
        if checked:
            self._show_banner(
                "Editing shortcuts — click a key, then press the new key. "
                "Esc cancels. Left-click is reserved.",
                error=False,
            )
        else:
            self.banner.setVisible(False)

    def _toggle_listening(self, action_id: str) -> None:
        if not self._edit_mode:
            return
        cap = self._caps[action_id]
        if self._listening_cap is cap:
            self._cancel_listening()
            return
        self._cancel_listening()
        self._listening_cap = cap
        cap.set_listening(True)

    def _cancel_listening(self) -> None:
        if self._listening_cap is not None:
            self._listening_cap.set_listening(False)
            self._listening_cap = None

    def _on_captured(self, action_id: str, seq: str) -> None:
        ok, message = self._owner.try_rebind_shortcut(action_id, seq)
        if ok:
            self._caps[action_id].set_binding_text(self._owner.shortcut_primary(action_id))
            self._cancel_listening()
            self._show_banner(f"Set to {self._owner.shortcut_primary(action_id)}.", error=False)
        else:
            self._show_banner(message, error=True)
            # keep listening so the user can try again

    def _reset_defaults(self) -> None:
        self._owner.reset_shortcuts_to_default()
        self._cancel_listening()
        for action_id, cap in self._caps.items():
            cap.set_binding_text(self._owner.shortcut_primary(action_id))
        self._show_banner("Shortcuts reset to defaults.", error=False)

    def _show_banner(self, text: str, error: bool) -> None:
        self.banner.setText(text)
        self.banner.setProperty("error", "true" if error else "false")
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.banner.setVisible(True)

    def _apply_styles(self) -> None:
        base = max(10, int(round(10 * DPI_SCALE)))
        self.setStyleSheet(
            f"""
            QDialog {{
                background: #1c1f24;
            }}
            QLabel {{
                color: #f4f4f5;
                font-size: {base}pt;
            }}
            QLabel#dlgHeader {{
                font-size: {max(13, int(round(13 * DPI_SCALE)))}pt;
                font-weight: 700;
                color: #f6f7f8;
            }}
            QLabel#dlgSection {{
                color: #8b919c;
                font-weight: 700;
                letter-spacing: 1px;
                font-size: {max(8, int(round(8.5 * DPI_SCALE)))}pt;
                padding-top: 6px;
            }}
            QLabel#dlgAction {{
                color: #d7dae0;
            }}
            QLabel#dlgAlias {{
                color: #8b919c;
            }}
            QLabel#dlgBanner {{
                color: #cfd3da;
                background: #2a2e34;
                border: 1px solid #44494f;
                border-radius: 6px;
                padding: 6px 9px;
            }}
            QLabel#dlgBanner[error="true"] {{
                color: #ffd4d4;
                background: #4a2326;
                border: 1px solid #8a3a3f;
            }}
            QLabel#keyCapFixed {{
                background: #2a2e34;
                border: 1px solid #44494f;
                border-radius: 6px;
                padding: 3px 9px;
                color: #d7dae0;
            }}
            QPushButton#keyCap {{
                background: #2a2e34;
                border: 1px solid #44494f;
                border-radius: 6px;
                padding: 3px 12px;
                color: #f4f4f5;
                min-width: 70px;
            }}
            QPushButton#keyCap:disabled {{
                color: #f4f4f5;
                background: #2a2e34;
                border: 1px solid #3a3f46;
            }}
            QPushButton#keyCap[editable="true"] {{
                border: 1px dashed #6a72e6;
                background: #2f3340;
            }}
            QPushButton#keyCap[listening="true"] {{
                border: 1px solid #6a72e6;
                background: #6a72e6;
                color: #ffffff;
                font-weight: 600;
            }}
            QPushButton {{
                background: #505662;
                border: 1px solid #656d79;
                color: #fafafa;
                padding: 5px 12px;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: #5c6370;
            }}
            QPushButton#primaryButton {{
                background: #6a72e6;
                border: 1px solid #8088ff;
            }}
            QPushButton#primaryButton:hover {{
                background: #7780f0;
            }}
            QPushButton#toggleButton:checked {{
                background: #6a72e6;
                border: 1px solid #8088ff;
                color: #ffffff;
                font-weight: 600;
            }}
            """
        )


class PyQtAnnotationReview(QMainWindow):
    """Main PyQt window that mirrors the previous OpenCV app behavior."""

    LAST_OPENED_DIRECTORY_FILE = Path.home() / ".annotation_review_last_directory"

    def __init__(self, input_path: str | None) -> None:
        super().__init__()
        self.input_path = input_path

        self.show_points = False
        self.show_labels = True
        self.annotation_opacity = 0.30
        # Set True once any annotation vertex has been dragged; cleared on load.
        self._dirty = False
        # Undo/redo history of geometry edit records ({annotation, before, after, image_index}).
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []

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
        # Cache of decoded RLE polygons keyed by id(annotation); decoding full
        # masks is expensive and overlays rebuild on every class toggle.
        self._rle_polygon_cache: dict[int, list[list[float]]] = {}
        self.last_opened_directory = self._read_last_opened_directory()
        self._build_ui()
        self._apply_styles()
        self.canvas.annotationsChanged.connect(self._on_annotations_changed)

        self._init_shortcuts()

    # ----- editable keyboard shortcuts ------------------------------------
    @staticmethod
    def _canon(key: str) -> str:
        """Canonicalize a binding string the same way Qt renders captured keys."""
        return QKeySequence(key).toString()

    def _init_shortcuts(self) -> None:
        """Define the editable keyboard shortcut model.

        Each entry: (id, label, primary binding, fixed alias bindings, callback).
        The primary binding is user-editable; aliases are reserved and not editable.
        All keyboard dispatch goes through ``keyPressEvent`` so bindings stay live.
        """
        defs = [
            ("next_image", "Next image", "N", ["Space", "Return", "Enter"], self._next_image),
            ("prev_image", "Previous image", "P", ["Shift+Space"], self._prev_image),
            ("reset_view", "Reset view", "R", [], self._reset_view),
            ("undo", "Undo", "Ctrl+Z", [], self._undo),
            ("redo", "Redo", "Ctrl+Y", ["Ctrl+Shift+Z"], self._redo),
            ("save", "Save", "Ctrl+S", [], self._save_dataset),
            ("open", "Open", "O", [], self._open_folder),
            ("quit", "Quit", "Q", ["Esc"], self.close),
        ]
        self._shortcut_order = [d[0] for d in defs]
        self._shortcut_labels = {d[0]: d[1] for d in defs}
        self._shortcut_defaults = {d[0]: self._canon(d[2]) for d in defs}
        self._shortcut_primary = dict(self._shortcut_defaults)
        self._shortcut_aliases = {d[0]: [self._canon(a) for a in d[3]] for d in defs}
        self._shortcut_callbacks = {d[0]: d[4] for d in defs}
        self._rebuild_shortcut_index()
        # Apply persisted user overrides on top of the code defaults (skip invalid/colliding).
        for action_id, key in load_shortcut_config().items():
            if action_id in self._shortcut_primary:
                self._set_binding(action_id, key)

    def _rebuild_shortcut_index(self) -> None:
        """Map every active binding (primary + aliases) to its action id."""
        index: dict[str, str] = {}
        for action_id in self._shortcut_order:
            for binding in [self._shortcut_primary[action_id], *self._shortcut_aliases[action_id]]:
                if binding:
                    index[binding] = action_id
        self._shortcut_index = index

    # Accessors used by ShortcutsDialog.
    def shortcut_order(self) -> list[str]:
        return list(self._shortcut_order)

    def shortcut_label(self, action_id: str) -> str:
        return self._shortcut_labels[action_id]

    def shortcut_primary(self, action_id: str) -> str:
        return self._shortcut_primary[action_id]

    def shortcut_alias_text(self, action_id: str) -> str:
        return " / ".join(self._shortcut_aliases[action_id])

    def _set_binding(self, action_id: str, raw_key: str) -> tuple[bool, str]:
        """Validate + apply a primary binding (no persistence). Returns (ok, message)."""
        new_key = self._canon(raw_key)
        if not new_key:
            return False, "That key can't be used."
        if new_key == self._shortcut_primary[action_id]:
            return True, ""
        # Collision against any other binding (incl. this action's own aliases).
        for other_id in self._shortcut_order:
            bindings = [self._shortcut_primary[other_id], *self._shortcut_aliases[other_id]]
            if other_id == action_id:
                bindings = self._shortcut_aliases[other_id]  # skip our own primary
            if new_key in bindings:
                clash = self._shortcut_labels[other_id]
                return False, f"“{new_key}” is already used by “{clash}”."
        self._shortcut_primary[action_id] = new_key
        self._rebuild_shortcut_index()
        return True, ""

    def try_rebind_shortcut(self, action_id: str, raw_key: str) -> tuple[bool, str]:
        """Attempt a rebind from the editor UI; persists on success."""
        ok, message = self._set_binding(action_id, raw_key)
        if ok:
            self._save_shortcuts()
        return ok, message

    def _save_shortcuts(self) -> None:
        save_shortcut_config({aid: self._shortcut_primary[aid] for aid in self._shortcut_order})

    def reset_shortcuts_to_default(self) -> None:
        """Restore all primary bindings to the built-in defaults and persist."""
        self._shortcut_primary = dict(self._shortcut_defaults)
        self._rebuild_shortcut_index()
        self._save_shortcuts()

    @staticmethod
    def _make_divider() -> QFrame:
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _make_section(text: str) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("section")
        return label

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

        header = QLabel("Annotation Workbench")
        header.setObjectName("header")
        sidebar_layout.addWidget(header)

        subtitle = QLabel("COCO box & polygon editor")
        subtitle.setObjectName("muted")
        sidebar_layout.addWidget(subtitle)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        icon_button_size = max(28, int(round(30 * DPI_SCALE)))
        icon_pixels = max(14, int(round(16 * DPI_SCALE)))

        self.undo_button = QPushButton()
        self.undo_button.setObjectName("iconButton")
        self.undo_button.setIcon(QIcon(str(resource_path("assets", "undo_icon.svg"))))
        self.undo_button.setIconSize(QSize(icon_pixels, icon_pixels))
        self.undo_button.setFixedSize(icon_button_size, icon_button_size)
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.setToolTip("Undo (Ctrl+Z)")
        self.undo_button.setEnabled(False)
        self.undo_button.clicked.connect(self._undo)
        header_row.addWidget(self.undo_button)

        self.redo_button = QPushButton()
        self.redo_button.setObjectName("iconButton")
        self.redo_button.setIcon(QIcon(str(resource_path("assets", "redo_icon.svg"))))
        self.redo_button.setIconSize(QSize(icon_pixels, icon_pixels))
        self.redo_button.setFixedSize(icon_button_size, icon_button_size)
        self.redo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redo_button.setToolTip("Redo (Ctrl+Y)")
        self.redo_button.setEnabled(False)
        self.redo_button.clicked.connect(self._redo)
        header_row.addWidget(self.redo_button)

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
        header_row.addStretch()

        self.prev_button = QPushButton("‹")
        self.prev_button.setObjectName("iconButton")
        self.prev_button.setFixedSize(icon_button_size, icon_button_size)
        self.prev_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prev_button.setToolTip("Previous image (P)")
        self.prev_button.clicked.connect(self._prev_image)
        header_row.addWidget(self.prev_button)

        self.next_button = QPushButton("›")
        self.next_button.setObjectName("iconButton")
        self.next_button.setFixedSize(icon_button_size, icon_button_size)
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.setToolTip("Next image (N)")
        self.next_button.clicked.connect(self._next_image)
        header_row.addWidget(self.next_button)

        sidebar_layout.addLayout(header_row)

        sidebar_layout.addWidget(self._make_divider())

        self.open_button = QPushButton("Open zip / folder")
        self.open_button.clicked.connect(self._open_folder)
        sidebar_layout.addWidget(self.open_button)

        self.open_images_json_button = QPushButton("Open images + JSON")
        self.open_images_json_button.clicked.connect(self._open_images_and_json)
        sidebar_layout.addWidget(self.open_images_json_button)

        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setToolTip("Save annotations to a COCO JSON file (Ctrl+S)")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_dataset)

        self.copy_id_button = QPushButton("Copy ID")
        self.copy_id_button.setObjectName("copyIdButton")
        self.copy_id_button.setMinimumHeight(32)
        self.copy_id_button.setEnabled(False)
        self.copy_id_button.clicked.connect(self._copy_image_id)

        save_row = QHBoxLayout()
        save_row.addWidget(self.save_button, 1)
        save_row.addWidget(self.copy_id_button)
        sidebar_layout.addLayout(save_row)

        sidebar_layout.addWidget(self._make_divider())

        self.reset_button = QPushButton("Reset view")
        self.reset_button.setToolTip("Fit the image and clear pan (R)")
        self.reset_button.clicked.connect(self._reset_view)
        sidebar_layout.addWidget(self.reset_button)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Opacity"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(30)
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row.addWidget(self.opacity_slider, 1)
        self.opacity_value_label = QLabel("30%")
        self.opacity_value_label.setObjectName("muted")
        self.opacity_value_label.setMinimumWidth(max(34, int(round(36 * DPI_SCALE))))
        self.opacity_value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        opacity_row.addWidget(self.opacity_value_label)
        sidebar_layout.addLayout(opacity_row)

        self.edit_objects_button = QPushButton("Edit objects")
        self.edit_objects_button.setObjectName("toggleButton")
        self.edit_objects_button.setCheckable(True)
        self.edit_objects_button.setChecked(False)
        self.edit_objects_button.setToolTip("Toggle editing of box corners / polygon vertices (T)")
        self.edit_objects_button.toggled.connect(self._on_edit_objects_toggled)

        self.labels_checkbox = QCheckBox("Show labels")
        self.labels_checkbox.setChecked(True)
        self.labels_checkbox.stateChanged.connect(self._on_labels_toggled)
        self.reset_colors_button = QPushButton("Reset colors")
        self.reset_colors_button.clicked.connect(self._reset_colors_to_default)

        toggle_row = QHBoxLayout()
        toggle_row.addWidget(self.edit_objects_button)
        toggle_row.addWidget(self.labels_checkbox)
        toggle_row.addStretch()
        toggle_row.addWidget(self.reset_colors_button)
        sidebar_layout.addLayout(toggle_row)

        sidebar_layout.addWidget(self._make_divider())

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

        self.status_label = QLabel("No dataset loaded")
        self.status_label.setObjectName("statusBarLabel")

        self.help_button = QPushButton("⌨ Shortcuts")
        self.help_button.setObjectName("statusHelpButton")
        self.help_button.setFixedHeight(max(18, int(round(20 * DPI_SCALE))))
        self.help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_button.setToolTip("Keyboard shortcuts & help")
        self.help_button.clicked.connect(self._show_help)

        status_bar = self.statusBar()
        status_bar.setSizeGripEnabled(False)
        status_bar.addWidget(self.status_label, 1)
        status_bar.addPermanentWidget(self.help_button)

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
            QStatusBar {{
                background: #1c1f24;
                border-top: 1px solid #3a4048;
            }}
            QStatusBar::item {{
                border: none;
            }}
            QLabel#statusBarLabel {{
                color: #c2c6ce;
                padding: 2px 10px;
            }}
            QPushButton#statusHelpButton {{
                background: #2a2e34;
                border: 1px solid #44494f;
                border-radius: 4px;
                color: #c2c6ce;
                padding: 1px 10px;
                font-weight: 600;
            }}
            QPushButton#statusHelpButton:hover {{
                background: #343941;
                color: #f4f4f5;
            }}
            QLabel#header {{
                font-size: {header_size}pt;
                font-weight: 700;
                color: #f6f7f8;
            }}
            QLabel#section {{
                font-weight: 650;
                color: #aeb3bd;
                font-size: {max(8, int(round(8.5 * DPI_SCALE)))}pt;
                letter-spacing: 1px;
                padding-top: 4px;
            }}
            QFrame#divider {{
                background: #3a4048;
                border: none;
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
                padding: 5px 10px;
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
            QPushButton#toggleButton:checked {{
                background: #6a72e6;
                border: 1px solid #8088ff;
                color: #ffffff;
                font-weight: 600;
            }}
            QPushButton#toggleButton:checked:hover {{
                background: #7780f0;
            }}
            QPushButton#iconButton {{
                background: #505662;
                border: 1px solid #656d79;
                border-radius: 6px;
                padding: 0px;
                font-size: {max(13, int(round(15 * DPI_SCALE)))}pt;
                font-weight: 600;
            }}
            QPushButton#iconButton:hover {{
                background: #5c6370;
            }}
            QPushButton#iconButton:pressed {{
                background: #444a55;
            }}
            QPushButton#iconButton:disabled {{
                background: #34383f;
                border: 1px solid #44494f;
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
            QSlider::groove:horizontal {{
                height: 4px;
                background: #3a4048;
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: #6a72e6;
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: #ffffff;
                width: 14px;
                margin: -6px 0;
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                background: #e8e8ee;
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

    def _show_message(self, text: str, title: str = "Annotation Workbench") -> None:
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

    def _open_images_and_json(self) -> None:
        initial_directory = self.last_opened_directory or str(Path.cwd())
        images_directory = QFileDialog.getExistingDirectory(
            self, "Select images folder", initial_directory
        )
        if not images_directory:
            return

        annotations_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select COCO annotations JSON",
            images_directory,
            "JSON files (*.json);;All files (*.*)",
        )
        if not annotations_file:
            return

        try:
            loaded_dataset = load_dataset_from_folder_and_json(images_directory, annotations_file)
        except Exception as error:
            self._show_message(f"Dataset selection failed: {error}")
            return

        self._apply_loaded_dataset(loaded_dataset, images_directory)

    def _load_dataset(self, selected_path: str) -> None:
        try:
            loaded_dataset = load_dataset_from_input(selected_path)
        except Exception as error:
            self._show_message(f"Dataset selection failed: {error}")
            return

        self._apply_loaded_dataset(loaded_dataset, selected_path)

    def _apply_loaded_dataset(self, loaded_dataset: dict, directory_to_remember: str) -> None:
        self._cleanup_temp_extraction()
        self._save_last_opened_directory(directory_to_remember)
        self._rle_polygon_cache.clear()

        self._dirty = False
        self._undo_stack.clear()
        self._redo_stack.clear()
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
        self._update_action_buttons()
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

    def _on_edit_objects_toggled(self, checked: bool) -> None:
        """Enable/disable dragging of box corners and polygon vertices."""
        self.canvas.set_edit_enabled(checked)

    def _on_labels_toggled(self, state: int) -> None:
        self.show_labels = state == int(Qt.CheckState.Checked.value)
        self._sync_canvas_state()
        self.canvas.update()

    def _on_overlay_setting_changed(self) -> None:
        self._sync_canvas_state()
        self.canvas.update()

    def _on_opacity_changed(self, value: int) -> None:
        self.opacity_value_label.setText(f"{value}%")
        self._on_overlay_setting_changed()

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
        self.canvas.annotation_opacity = self.opacity_slider.value() / 100.0

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

    def _reset_view(self) -> None:
        """Reset the view: fit the image to the window and clear any pan."""
        self.canvas.fit_to_view()
        self._update_status_labels()

    def _show_help(self) -> None:
        """Show the dark-themed shortcuts window (with inline rebinding)."""
        dialog = ShortcutsDialog(self)
        dialog.exec()

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
        self._rle_polygon_cache.clear()
        self._dirty = False
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._update_action_buttons()
        self._cleanup_temp_extraction()
        self._clear_class_checkboxes()
        if self.image_selector:
            self.image_selector.clear()
        self.canvas.set_idle()
        self._sync_canvas_state()
        self._update_status_labels()

    def _polygons_for_annotation(self, annotation, segmentation) -> list[list[float]]:
        """Return polygon contours for an RLE segmentation, decoding once and caching."""
        cache_key = id(annotation)
        cached = self._rle_polygon_cache.get(cache_key)
        if cached is None:
            try:
                cached = rle_to_polygons(segmentation)
            except Exception:
                cached = []
            self._rle_polygon_cache[cache_key] = cached
        return cached

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
                is_rle = is_rle_segmentation(segmentation)
                if is_rle:
                    segments = self._polygons_for_annotation(annotation, segmentation)
                elif isinstance(segmentation, dict):
                    continue
                else:
                    segments = segmentation
                for seg_index, segment in enumerate(segments):
                    if not isinstance(segment, list) or len(segment) < 6:
                        continue
                    points = [(float(segment[index]), float(segment[index + 1])) for index in range(0, len(segment) - 1, 2)]
                    center = polygon_centroid(points)
                    overlay_items.append(
                        {
                            "shape": "polygon",
                            "points": points,
                            "center": center,
                            "label": str(category_name),
                            "color": color_rgb,
                            "annotation": annotation,
                            "seg_index": seg_index,
                            # RLE polygons are decoded for display only; editing them
                            # cannot be written back to the mask, so they are read-only.
                            "editable": not is_rle,
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
                        "annotation": annotation,
                        "editable": True,
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

    def _on_annotations_changed(self, record: dict | None = None) -> None:
        """A vertex was dragged on the canvas; record the edit for undo and refresh."""
        if record is not None:
            record["image_index"] = self.index
            self._undo_stack.append(record)
            self._redo_stack.clear()
        self._dirty = bool(self._undo_stack)
        self._update_action_buttons()
        self._update_status_labels()

    def _update_action_buttons(self) -> None:
        self.undo_button.setEnabled(bool(self._undo_stack))
        self.redo_button.setEnabled(bool(self._redo_stack))
        has_dataset = self.dataset is not None
        self.save_button.setEnabled(has_dataset)
        self.save_button.setText("● Save" if (has_dataset and self._dirty) else "Save")

    def _restore_snapshot(self, record: dict, key: str) -> None:
        """Apply the 'before'/'after' geometry snapshot of a record and show its image."""
        annotation = record["annotation"]
        snapshot = record[key]
        annotation["bbox"] = copy.deepcopy(snapshot.get("bbox"))
        annotation["area"] = snapshot.get("area")
        annotation["segmentation"] = copy.deepcopy(snapshot.get("segmentation"))

        target_index = record.get("image_index", self.index)
        if self.images and 0 <= target_index < len(self.images) and target_index != self.index:
            self.index = target_index
            self._load_current_image(reset_fit=True)
        else:
            self._refresh_overlay_items()

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        record = self._undo_stack.pop()
        self._redo_stack.append(record)
        self._restore_snapshot(record, "before")
        self._dirty = bool(self._undo_stack)
        self._update_action_buttons()
        self._update_status_labels()

    def _redo(self) -> None:
        if not self._redo_stack:
            return
        record = self._redo_stack.pop()
        self._undo_stack.append(record)
        self._restore_snapshot(record, "after")
        self._dirty = bool(self._undo_stack)
        self._update_action_buttons()
        self._update_status_labels()

    def _save_dataset(self) -> None:
        """Write the current (edited) annotations to a COCO JSON file."""
        if not self.dataset:
            return

        initial_directory = self.last_opened_directory or self.images_path or str(Path.cwd())
        default_path = os.path.join(initial_directory, "annotations_edited.json")
        selected_file, _ = QFileDialog.getSaveFileName(
            self,
            "Save annotations as COCO JSON",
            default_path,
            "JSON files (*.json);;All files (*.*)",
        )
        if not selected_file:
            return

        categories = [
            {"id": category_id, "name": name}
            for category_id, name in sorted(self.categories_by_id.items(), key=lambda item: item[0])
        ]
        annotations = [
            annotation
            for annotations_list in self.annotations_by_image_id.values()
            for annotation in annotations_list
        ]
        coco = {"images": self.images, "categories": categories, "annotations": annotations}

        try:
            with open(selected_file, "w", encoding="utf-8") as file_handle:
                json.dump(coco, file_handle, ensure_ascii=False)
        except OSError as error:
            self._show_message(f"Could not save: {error}")
            return

        self._dirty = False
        self._update_action_buttons()
        self._update_status_labels()
        self._save_last_opened_directory(selected_file)
        self._show_message(
            f"Saved {len(annotations)} annotations to\n{os.path.basename(selected_file)}"
        )

    @staticmethod
    def _unreadable_message(count: int) -> str:
        noun = "image" if count == 1 else "images"
        return f"Could not read {count} {noun}."

    def _load_current_image(self, reset_fit: bool) -> None:
        if not self.dataset or not self.images:
            self.canvas.set_idle()
            return

        # Advance past any images that cannot be read, counting them so we can
        # report a single summary instead of one error window per failure.
        failed_count = 0
        while True:
            if self.index >= len(self.images):
                if failed_count:
                    self._show_message(self._unreadable_message(failed_count))
                else:
                    self._show_message("Last image completed")
                self._clear_dataset()
                return

            image_info = self.images[self.index]
            image_name = str(image_info.get("file_name", ""))
            image_path = resolve_image_path(self.images_path, image_name)
            if self.canvas.load_image(image_path, ""):
                self.current_image_name = image_name
                self.current_image_path = image_path
                break
            failed_count += 1
            self.index += 1

        image_info = self.images[self.index]
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
        self.canvas.set_overlay_items(self.current_overlay_items)
        self._sync_canvas_state()
        if reset_fit:
            self.canvas.fit_to_view()
        self.setWindowTitle(title)
        self._populate_class_checkboxes()
        self._update_status_labels()

        if failed_count:
            self._show_message(self._unreadable_message(failed_count))

    def _update_status_labels(self) -> None:
        if not self.dataset:
            self.status_label.setText("No dataset loaded — open a zip/folder or images + JSON to begin")
            self.copy_id_button.setEnabled(False)
            return

        self.copy_id_button.setEnabled(True)
        zoom = int(round(self.canvas.zoom * 100))

        if self.images:
            current_name = os.path.basename(self.current_image_name) if self.current_image_name else ""
            visible_count = len(self.current_overlay_items)
            parts = [
                f"{self.index + 1}/{len(self.images)}",
                current_name,
                f"{len(self.class_items)} classes",
                f"{visible_count} visible",
                f"Zoom {zoom}%",
            ]
        else:
            parts = [f"{len(self.images)} images", f"{len(self.class_items)} classes", f"Zoom {zoom}%"]

        text = "   ·   ".join(part for part in parts if part)
        if self._dirty:
            text += "   ·   ● unsaved edits"
        self.status_label.setText(text)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        seq = QKeySequence(event.keyCombination()).toString()
        action_id = self._shortcut_index.get(seq)
        if action_id is not None:
            self._shortcut_callbacks[action_id]()
            event.accept()
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
    app.setApplicationName("Annotation Workbench")
    app.setStyle("Fusion")

    window = PyQtAnnotationReview(args.input_path)
    window.resize(int(round(MONITOR_WIDTH * 0.9)), int(round(MONITOR_HEIGHT * 0.87)))
    window.showMaximized()

    sys.exit(app.exec())
