"""PyQt-based main application for annotation review."""

from __future__ import annotations

import colorsys
import copy
import json
import math
import os
import shutil
import sys
from pathlib import Path

from .constants import DPI_SCALE, MONITOR_HEIGHT, MONITOR_WIDTH, SIDEBAR_WIDTH
from .data_loading import (
    discover_images_dir,
    find_coco_paths,
    load_coco_data,
    load_dataset_from_folder_and_json,
    load_dataset_from_input,
    load_images_only,
    resolve_dataset_paths,
    resolve_image_path,
    stable_image_id,
)
from .rle import is_rle_segmentation, rle_to_polygons
from .utils import parse_arguments
from . import levels
from .project import Project, ProjectError

try:
    from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, pyqtSignal
    from PyQt6.QtGui import (
        QAction,
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
        QButtonGroup,
        QCheckBox,
        QColorDialog,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QGridLayout,
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
        QStackedWidget,
        QStyledItemDelegate,
        QVBoxLayout,
        QWidget,
    )
except Exception as exc:
    raise RuntimeError(
        "PyQt6 is required for this version of the app. Install with: pip install PyQt6"
    ) from exc


# Dark stylesheet for our (non-native) file dialogs and message boxes. The main
# window's QSS only half-cascades into stock dialogs (styling buttons but not
# line edits / item views), which leaves text fields unreadable; this gives them
# a complete dark look so they match the app.
_DARK_DIALOG_QSS = """
QFileDialog, QDialog, QMessageBox { background-color: #1d2024; }
QWidget { color: #f0f1f3; }
QLabel { color: #f0f1f3; background: transparent; }
QLineEdit, QComboBox, QSpinBox {
    background-color: #111315; color: #f0f1f3;
    border: 1px solid #5a606a; border-radius: 4px; padding: 3px 6px;
    selection-background-color: #6a72e6; selection-color: #ffffff;
}
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView, QListView, QTreeView {
    background-color: #111315; color: #f0f1f3;
    border: 1px solid #5a606a; outline: none;
    selection-background-color: #6a72e6; selection-color: #ffffff;
}
QTreeView::item:hover, QListView::item:hover { background: rgba(106, 114, 230, 0.25); }
QHeaderView::section { background-color: #1d2024; color: #c2c6ce; border: none; padding: 4px; }
QPushButton, QToolButton {
    background-color: #505662; color: #fafafa;
    border: 1px solid #656d79; border-radius: 6px; padding: 5px 12px;
}
QPushButton:hover, QToolButton:hover { background-color: #5c6370; }
QPushButton:default { background-color: #6a72e6; border-color: #8088ff; }
QScrollBar:vertical { background: #1d2024; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4048; border-radius: 6px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


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


def color_for_class(category_name: str, category_id: int, default_hex: str | None = None) -> QColor:
    """Return the configured hex color for a class name.

    Precedence: a user override in ``class_colors.json`` first, then ``default_hex``
    (e.g. the annotation-level catalog color), then a color generated from the id.
    """
    hex_color = _COLOR_CONFIG.get(category_name) or default_hex
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


def polygon_area(points: list[tuple[float, float]]) -> float:
    """Shoelace area of a polygon given its vertices."""
    count = len(points)
    if count < 3:
        return 0.0
    running = 0.0
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        running += x1 * y2 - x2 * y1
    return abs(running) / 2.0


def resource_path(*parts: str) -> Path:
    """Resolve a bundled resource path in both source and frozen runs."""
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_path.joinpath(*parts)


class ImageCanvas(QWidget):
    """Image viewport widget with pan/zoom and Qt-native annotation overlays."""

    # Emitted after a vertex drag finishes with a before/after edit record (a dict),
    # so the window can mark unsaved edits and push an undo step.
    annotationsChanged = pyqtSignal(object)
    # Emitted when a new shape is drawn: {"shape": "polygon"|"bbox", "points": [(x, y), ...]}
    # in image coordinates. The window turns it into an annotation for the active level.
    annotationCreated = pyqtSignal(object)
    # Emitted when the selected annotation changes (so the window can enable Delete).
    selectionChanged = pyqtSignal()

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
        # Drawing state (annotation mode). None = not drawing.
        self._draw_shape: str | None = None  # "polygon" | "bbox"
        self._draw_color: tuple[int, int, int] = (255, 255, 255)
        self._poly_points: list[tuple[float, float]] = []
        # Box drawing is click-based: click start, click end (axis-aligned). Hold Shift
        # on the second click to instead define a center line, then a 3rd click for thickness.
        self._box_pts: list[tuple[float, float]] = []
        self._box_rotated = False
        self._box_shift_preview = False
        self._cursor_image: tuple[float, float] | None = None
        # Selection (annotation mode): click an annotation body to select; Delete removes it.
        self._select_enabled = False
        self._selected_annotation: dict | None = None
        # A parked "redefine" overlay can also be selected (to assign it a class).
        self._selected_redefine: dict | None = None
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
        painter.drawText(card_rect.adjusted(24, 58, -24, -16), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "Create or open a project, then use the Import menu to add images and annotations.")

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
        self._draw_pending(painter, image_x, image_y)

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

    # ----- drawing (annotation mode) --------------------------------------
    def set_draw_shape(self, shape: str | None, color: tuple[int, int, int] = (255, 255, 255)) -> None:
        """Arm drawing of ``shape`` ("polygon"|"bbox") with ``color``; None disarms."""
        self._draw_shape = shape
        self._draw_color = color
        self.cancel_drawing()
        self.setCursor(Qt.CursorShape.CrossCursor if shape else Qt.CursorShape.ArrowCursor)

    def has_pending_drawing(self) -> bool:
        return bool(self._poly_points) or bool(self._box_pts)

    def cancel_drawing(self) -> None:
        self._poly_points = []
        self._box_pts = []
        self._box_rotated = False
        self._cursor_image = None
        self.update()

    @staticmethod
    def _oriented_box(p1, p2, p3) -> tuple[list[tuple[float, float]] | None, float]:
        """Rectangle with one edge p1->p2 and the opposite edge through p3.

        Returns (corners, rotation_degrees). p1->p2 is one long side; p3's signed
        perpendicular distance from that line sets the thickness and side.
        """
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return None, 0.0
        nx, ny = -dy / length, dx / length  # unit perpendicular
        offset = (p3[0] - p1[0]) * nx + (p3[1] - p1[1]) * ny
        tx, ty = nx * offset, ny * offset
        corners = [p1, p2, (p2[0] + tx, p2[1] + ty), (p1[0] + tx, p1[1] + ty)]
        return corners, math.degrees(math.atan2(dy, dx))

    def remove_last_point(self) -> None:
        if self._poly_points:
            self._poly_points.pop()
            self.update()

    def finish_polygon(self) -> None:
        if self._draw_shape == "polygon" and len(self._poly_points) >= 3:
            points = list(self._poly_points)
            self._poly_points = []
            self._cursor_image = None
            self.update()
            self.annotationCreated.emit({"shape": "polygon", "points": points})

    def _near_first_point(self, position) -> bool:
        if not self._poly_points:
            return False
        image_x, image_y, _, _ = self._fit_display_rect()
        first_x, first_y = self._poly_points[0]
        screen_x = image_x + first_x * self._zoom
        screen_y = image_y + first_y * self._zoom
        return (screen_x - position.x()) ** 2 + (screen_y - position.y()) ** 2 <= float(self._handle_hit_radius) ** 2

    # ----- selection (annotation mode) ------------------------------------
    def set_select_enabled(self, enabled: bool) -> None:
        self._select_enabled = enabled
        if not enabled:
            self._set_selected(None)
        self.update()

    def selected_annotation(self) -> dict | None:
        return self._selected_annotation

    def selected_redefine(self) -> dict | None:
        return self._selected_redefine

    def clear_selection(self) -> None:
        self._set_selected(None)  # also clears any redefine selection

    def _set_selected(self, annotation: dict | None) -> None:
        changed = annotation is not self._selected_annotation or self._selected_redefine is not None
        self._selected_annotation = annotation
        self._selected_redefine = None
        if changed:
            self.selectionChanged.emit()
            self.update()

    def _set_selected_redefine(self, item: dict | None) -> None:
        changed = item is not self._selected_redefine or self._selected_annotation is not None
        self._selected_redefine = item
        self._selected_annotation = None
        if changed:
            self.selectionChanged.emit()
            self.update()

    def _hit_test_annotation(self, position) -> dict | None:
        """Return the topmost annotation whose shape contains the cursor, if any."""
        if self._pixmap is None:
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        target = QPointF(position.x(), position.y())
        for item in reversed(self._overlay_items):
            # Read-only overlays (e.g. parked redefine ones) have no annotation to
            # select; skip them so a real annotation behind stays reachable.
            if item.get("annotation") is None:
                continue
            points = item.get("points") or []
            if len(points) < 2:
                continue
            screen_points = [QPointF(image_x + x * self._zoom, image_y + y * self._zoom) for x, y in points]
            if item.get("shape") == "bbox":
                xs = [p.x() for p in screen_points]
                ys = [p.y() for p in screen_points]
                if QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)).contains(target):
                    return item.get("annotation")
            elif QPolygonF(screen_points).containsPoint(target, Qt.FillRule.OddEvenFill):
                return item.get("annotation")
        return None

    def _hit_test_redefine(self, position) -> dict | None:
        """Return the topmost parked-redefine overlay under the cursor, if any."""
        if self._pixmap is None:
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        target = QPointF(position.x(), position.y())
        for item in reversed(self._overlay_items):
            if not item.get("pending_redefine"):
                continue
            points = item.get("points") or []
            if len(points) < 2:
                continue
            screen_points = [QPointF(image_x + x * self._zoom, image_y + y * self._zoom) for x, y in points]
            if QPolygonF(screen_points).containsPoint(target, Qt.FillRule.OddEvenFill):
                return item
        return None

    def _image_from_screen(self, screen_x: float, screen_y: float, clamp: bool = False) -> tuple[float, float]:
        """Convert a screen position back into image (pixel) coordinates.

        When ``clamp`` is set, the result is confined to the image rectangle so a
        click/drag outside the picture lands on the nearest edge rather than in
        empty space. The cursor itself moves freely; only the point is pinned.
        """
        image_x, image_y, _, _ = self._fit_display_rect()
        if self._zoom <= 0:
            return 0.0, 0.0
        ix = (screen_x - image_x) / self._zoom
        iy = (screen_y - image_y) / self._zoom
        if clamp and self._pixmap is not None:
            ix = min(max(ix, 0.0), float(self._pixmap.width()))
            iy = min(max(iy, 0.0), float(self._pixmap.height()))
        return ix, iy

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

    def _draw_pending(self, painter: QPainter, image_x: int, image_y: int) -> None:
        """Render the in-progress drawn shape (rubber band) on top of everything."""
        if self._draw_shape is None or self._pixmap is None:
            return
        color = QColor(*self._draw_color)
        pen = QPen(color, max(1.5, 1.6 * DPI_SCALE))
        pen.setStyle(Qt.PenStyle.DashLine)

        if self._draw_shape == "bbox":
            fill = QColor(color)
            fill.setAlpha(55)
            radius = self._handle_size
            cursor = self._cursor_image

            def to_screen(pt):
                return QPointF(image_x + pt[0] * self._zoom, image_y + pt[1] * self._zoom)

            if len(self._box_pts) == 1 and cursor is not None:
                first = self._box_pts[0]
                if self._box_shift_preview:
                    painter.setPen(pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawLine(to_screen(first), to_screen(cursor))
                else:
                    start = to_screen(first)
                    end = to_screen(cursor)
                    painter.setPen(pen)
                    painter.setBrush(fill)
                    painter.drawRect(QRectF(min(start.x(), end.x()), min(start.y(), end.y()), abs(end.x() - start.x()), abs(end.y() - start.y())))
            elif len(self._box_pts) == 2 and cursor is not None:
                corners, _ = self._oriented_box(self._box_pts[0], self._box_pts[1], cursor)
                if corners is not None:
                    painter.setPen(pen)
                    painter.setBrush(fill)
                    painter.drawPolygon(QPolygonF([to_screen(pt) for pt in corners]))

            painter.setPen(QPen(QColor(20, 20, 20), 1))
            painter.setBrush(QColor(255, 255, 255))
            for pt in self._box_pts:
                painter.drawEllipse(to_screen(pt), radius, radius)
            return

        if self._draw_shape == "polygon" and self._poly_points:
            screen_points = [QPointF(image_x + x * self._zoom, image_y + y * self._zoom) for x, y in self._poly_points]
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if len(screen_points) >= 2:
                painter.drawPolyline(QPolygonF(screen_points))
            if self._cursor_image is not None:
                cursor_point = QPointF(image_x + self._cursor_image[0] * self._zoom, image_y + self._cursor_image[1] * self._zoom)
                painter.drawLine(screen_points[-1], cursor_point)
            radius = self._handle_size
            painter.setPen(QPen(QColor(20, 20, 20), 1))
            painter.setBrush(QColor(255, 255, 255))
            for point in screen_points:
                painter.drawEllipse(point, radius, radius)
            if len(screen_points) >= 3:
                painter.setBrush(QColor(106, 114, 230))
                painter.drawEllipse(screen_points[0], radius + 2, radius + 2)

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

            is_selected = (
                (self._selected_annotation is not None and item.get("annotation") is self._selected_annotation)
                or (item.get("pending_redefine") and item is self._selected_redefine)
            )
            if is_selected:
                painter.setPen(QPen(QColor(255, 255, 255), max(2.0, 2.4 * DPI_SCALE)))
            elif item.get("pending_redefine"):
                # Dashed, thicker outline + no fill = "this still needs redefining".
                pending_pen = QPen(outline_color, max(2.0, 2.2 * DPI_SCALE))
                pending_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pending_pen)
            else:
                painter.setPen(QPen(outline_color, max(1.0, 1.2 * DPI_SCALE)))
            painter.setBrush(Qt.BrushStyle.NoBrush if item.get("pending_redefine") else fill_color)

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
        # Middle button always pans (handy while drawing, where left is the pen).
        if event.button() == Qt.MouseButton.MiddleButton and self._pixmap is not None:
            self._dragging = True
            self._drag_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            # Drawing takes precedence over panning/editing when armed.
            if self._draw_shape is not None and self._pixmap is not None:
                # Drawing points are pinned to the image so nothing lands off-picture.
                image_x, image_y = self._image_from_screen(event.position().x(), event.position().y(), clamp=True)
                if self._draw_shape == "polygon":
                    if len(self._poly_points) >= 3 and self._near_first_point(event.position()):
                        self.finish_polygon()
                    else:
                        self._poly_points.append((image_x, image_y))
                        self.update()
                    return
                # bbox: click start, click end (axis-aligned); Shift on the 2nd click
                # defines a center line, then a 3rd click sets the thickness (rotated).
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._handle_box_click((image_x, image_y), shift)
                return

            # Grabbing a handle edits a vertex (only when editing is enabled).
            hit = self._hit_test_vertex(event.position())
            if hit is not None:
                self._drag_vertex = hit
                self._drag_before = self._snapshot_annotation(hit[0].get("annotation"))
                self._vertex_moved = False
                self._dragging = False
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                return
            # Clicking an annotation body selects it; a parked redefine overlay is
            # selectable too (to assign it a class); empty space deselects, then pans.
            if self._select_enabled:
                annotation = self._hit_test_annotation(event.position())
                if annotation is not None:
                    self._set_selected(annotation)
                    return
                redefine = self._hit_test_redefine(event.position())
                if redefine is not None:
                    self._set_selected_redefine(redefine)
                    return
                self._set_selected(None)
            self._dragging = True
            self._drag_start = event.position().toPoint()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._draw_shape == "polygon" and len(self._poly_points) >= 3:
            self.finish_polygon()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_vertex is not None:
            item, index = self._drag_vertex
            image_x, image_y = self._image_from_screen(event.position().x(), event.position().y(), clamp=True)
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

        if self._draw_shape is not None and self._pixmap is not None:
            # Preview cursor is clamped too, so the rubber-band matches the pinned point.
            self._cursor_image = self._image_from_screen(event.position().x(), event.position().y(), clamp=True)
            if self._draw_shape == "bbox" and len(self._box_pts) == 1:
                # Live feedback: Shift previews the rotated center line, else an axis box.
                self._box_shift_preview = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.update()
            return

        self._update_hover(event.position())

    def _handle_box_click(self, point: tuple[float, float], shift: bool) -> None:
        """Advance the click-based box state machine and emit when a box completes."""
        if not self._box_pts:
            self._box_pts = [point]
            self._cursor_image = point
        elif len(self._box_pts) == 1:
            if shift:
                # Shift on the 2nd click: this is the center-line end; await thickness.
                self._box_pts.append(point)
                self._box_rotated = True
            else:
                p1 = self._box_pts[0]
                self._box_pts = []
                if abs(point[0] - p1[0]) >= 2 and abs(point[1] - p1[1]) >= 2:
                    corners = [(p1[0], p1[1]), (point[0], p1[1]), (point[0], point[1]), (p1[0], point[1])]
                    self.annotationCreated.emit({"shape": "bbox", "points": corners})
        elif len(self._box_pts) == 2 and self._box_rotated:
            corners, rotation = self._oriented_box(self._box_pts[0], self._box_pts[1], point)
            self._box_pts = []
            self._box_rotated = False
            if corners is not None:
                self.annotationCreated.emit({"shape": "obbox", "points": corners, "rotation": rotation})
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._dragging = False
            self.setCursor(Qt.CursorShape.CrossCursor if self._draw_shape else Qt.CursorShape.ArrowCursor)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            # Box/polygon drawing is click-driven; nothing to settle on release.
            if self._draw_shape is not None:
                self._dragging = False
                self.setCursor(Qt.CursorShape.CrossCursor)
                return

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


class RedefineRowDelegate(QStyledItemDelegate):
    """Paints a red background behind image-selector rows flagged as needing
    redefinition. A delegate is used because the view's QSS (``::item`` background)
    overrides any model BackgroundRole, so colors set on the item don't show."""

    FLAG_ROLE = Qt.ItemDataRole.UserRole + 100

    def paint(self, painter, option, index) -> None:  # noqa: N802
        if index.data(self.FLAG_ROLE):
            painter.fillRect(option.rect, QColor(120, 45, 48))
        super().paint(painter, option, index)


class OutlinedTextButton(QPushButton):
    """A QPushButton whose label is drawn white with a thin black outline.

    The colored pill background, border and hover/pressed states still come from
    the widget stylesheet (the base ``paintEvent``); only the text is painted by
    hand on top, so the label stays legible on *any* fill color (dark or light).
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__("", parent)  # blank so the style engine paints no text itself
        self._label = text

    def setText(self, text: str) -> None:  # noqa: N802 - keep our label, never hand it to the base
        self._label = text
        self.update()

    def text(self) -> str:
        return self._label

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)  # rounded background + border + hover/pressed via QSS
        if not self._label:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = self.font()
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        pad = max(12, int(round(12 * DPI_SCALE)))  # match the QSS "padding: 7px 12px"
        avail = max(1, self.width() - 2 * pad)
        text = metrics.elidedText(self._label, Qt.TextElideMode.ElideRight, avail)
        baseline = (self.height() + metrics.ascent() - metrics.descent()) / 2.0
        path = QPainterPath()
        path.addText(float(pad), float(baseline), font, text)
        outline = QPen(QColor(0, 0, 0, 235), max(2.0, 2.0 * DPI_SCALE))
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)  # black halo around each glyph
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPath(path)  # white glyph fill on top
        painter.end()


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

        self._pill = OutlinedTextButton(category_name)
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
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        """Open at a comfortable height that never exceeds the screen work area.

        Without this the all-rows layout asks for a window taller than the
        display; Windows clamps it and logs a ``setGeometry`` warning. The scroll
        area absorbs any overflow, so capping the height is purely cosmetic-safe.
        """
        screen = self.screen() or QApplication.primaryScreen()
        avail_h = screen.availableGeometry().height() if screen is not None else 900
        target_h = min(max(400, avail_h - 80), int(round(820 * DPI_SCALE)))
        self.resize(self.sizeHint().width(), target_h)
        self.setMaximumHeight(avail_h)

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

        # The shortcut list can be taller than the screen (many rows + high-DPI),
        # which made the dialog request an oversized window that Windows clamps
        # (the QWindowsWindow::setGeometry warning). A scroll area keeps the list
        # within the work area and scrolls instead; header + buttons stay fixed.
        scroll = QScrollArea()
        scroll.setObjectName("shortcutsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("shortcutsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 6, 0)  # room so the scrollbar doesn't overlap rows
        content_layout.setSpacing(12)

        # Mouse section (fixed / descriptive)
        content_layout.addWidget(self._section_label("Mouse"))
        for desc, action in (
            ("Wheel", "zoom"),
            ("Drag empty space", "pan"),
            ("Drag a handle", "move a box corner / polygon vertex"),
        ):
            content_layout.addLayout(self._fixed_row(desc, action))

        # Keyboard section (editable)
        content_layout.addWidget(self._section_label("Keyboard"))
        for action_id in self._owner.shortcut_order():
            content_layout.addLayout(self._editable_row(action_id))

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

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
            /* Keep the scroll area + its viewport + inner widget on the dialog's
               dark base (they default to the light palette otherwise). */
            QScrollArea#shortcutsScroll, QScrollArea#shortcutsScroll > QWidget,
            QWidget#shortcutsContent {{
                background: #1c1f24;
                border: none;
            }}
            QScrollArea#shortcutsScroll QScrollBar:vertical {{
                background: #1c1f24;
                width: 11px;
                margin: 0px;
            }}
            QScrollArea#shortcutsScroll QScrollBar::handle:vertical {{
                background: #3a3f46;
                border-radius: 5px;
                min-height: 28px;
            }}
            QScrollArea#shortcutsScroll QScrollBar::handle:vertical:hover {{
                background: #4a4f57;
            }}
            QScrollArea#shortcutsScroll QScrollBar::add-line:vertical,
            QScrollArea#shortcutsScroll QScrollBar::sub-line:vertical {{
                height: 0px;
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


_REDEFINE_QSS = _DARK_DIALOG_QSS + """
#redefineDialog { background: #1d2024; }
#redefineDialog QLabel { color: #eef0f3; font-size: 10pt; }
#redefineDialog QLabel#redefineIntro { color: #c2c6ce; }
#redefineDialog QLabel#redefineHeader { color: #aeb3bd; font-weight: 700; }
#redefineDialog QLabel#redefineClass { color: #ffffff; font-weight: 700; font-size: 11pt; }
#redefineDialog QLabel#redefineCount { color: #c2c6ce; }
#redefineDialog QComboBox { min-width: 150px; padding: 5px 8px; }
/* The scroll area + its viewport + the inner container default to the light
   palette base (the bright table). Darken all three to match the dialog. */
#redefineDialog QScrollArea, #redefineDialog QScrollArea > QWidget { background: #1d2024; border: none; }
#redefineContainer { background: #1d2024; }
#redefineRow { border-bottom: 1px solid #2c3036; }
"""


class RedefineDialog(QDialog):
    """Map imported off-catalog classes onto a level (and, optionally, a class).

    One row per unknown class: a Level dropdown and a Class dropdown (the class
    list follows the chosen level). Three outcomes per row:
      * level + class  -> full remap (annotations move into that level/class),
      * level only     -> annotations become VISIBLE in that level, highlighted
                          as "needs redefine" (kept in the stash, not committed),
      * neither        -> left for later.
    Remap-to-existing only (no new classes).
    """

    def __init__(
        self,
        owner: "PyQtAnnotationReview",
        class_counts: dict[str, int],
        assigned_levels: dict[str, int] | None = None,
    ) -> None:
        super().__init__(owner)
        self.setObjectName("redefineDialog")
        self.setWindowTitle("Redefine classes")
        self.setStyleSheet(_REDEFINE_QSS)
        self.setMinimumSize(620, 320)
        assigned_levels = assigned_levels or {}
        self._rows: list[tuple[str, QComboBox, QComboBox]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        intro = QLabel(
            "These imported classes aren't in the catalog. Choose a Level and an "
            "existing Class to remap each. Pick a Level only (leave Class blank) to "
            "keep the annotations visible in that level, highlighted as needing "
            "redefinition. Mappings are saved with the project."
        )
        intro.setObjectName("redefineIntro")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Only show a horizontal scrollbar when a row's text is genuinely too wide.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        container = QWidget()
        container.setObjectName("redefineContainer")
        grid = QGridLayout(container)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        for column, heading in enumerate(("Unknown class", "Count", "Level", "Map to class (optional)")):
            label = QLabel(heading)
            label.setObjectName("redefineHeader")
            grid.addWidget(label, 0, column)

        for row, (raw_class, count) in enumerate(sorted(class_counts.items()), start=1):
            name_label = QLabel(raw_class)
            name_label.setObjectName("redefineClass")
            grid.addWidget(name_label, row, 0)
            count_label = QLabel(f"×{count}")
            count_label.setObjectName("redefineCount")
            grid.addWidget(count_label, row, 1)

            level_combo = QComboBox()
            level_combo.addItem("— pick level —", None)
            for level_id in levels.LEVEL_IDS:
                level_combo.addItem(f"L{level_id} · {levels.level_title(level_id)}", level_id)
            grid.addWidget(level_combo, row, 2)

            class_combo = QComboBox()
            class_combo.setEnabled(False)
            grid.addWidget(class_combo, row, 3)

            level_combo.currentIndexChanged.connect(
                lambda _i, lc=level_combo, cc=class_combo: self._on_level_changed(lc, cc)
            )
            # Pre-select a previously chosen level-only assignment.
            preset = assigned_levels.get(raw_class)
            if preset is not None:
                index = level_combo.findData(preset)
                if index >= 0:
                    level_combo.setCurrentIndex(index)
            self._rows.append((raw_class, level_combo, class_combo))

        grid.setColumnStretch(3, 1)
        grid.setRowStretch(len(self._rows) + 1, 1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(cancel_button)
        apply_button = QPushButton("Apply")
        apply_button.setObjectName("primaryButton")
        apply_button.clicked.connect(self.accept)
        button_row.addWidget(apply_button)
        layout.addLayout(button_row)

    @staticmethod
    def _on_level_changed(level_combo: QComboBox, class_combo: QComboBox) -> None:
        class_combo.clear()
        level = level_combo.currentData()
        if level is None:
            class_combo.setEnabled(False)
            return
        class_combo.setEnabled(True)
        class_combo.addItem("— leave for later —", None)
        for class_name, _color in levels.level_classes(level):
            class_combo.addItem(class_name, class_name)

    def mappings(self) -> dict[str, tuple[int, str]]:
        """{raw_class: (level, target_class)} for rows where BOTH are chosen."""
        result: dict[str, tuple[int, str]] = {}
        for raw_class, level_combo, class_combo in self._rows:
            level = level_combo.currentData()
            target = class_combo.currentData()
            if level is not None and target:
                result[raw_class] = (int(level), str(target))
        return result

    def level_only(self) -> dict[str, int]:
        """{raw_class: level} for rows with a Level but no Class (mark-for-redefine)."""
        result: dict[str, int] = {}
        for raw_class, level_combo, class_combo in self._rows:
            level = level_combo.currentData()
            target = class_combo.currentData()
            if level is not None and not target:
                result[raw_class] = int(level)
        return result


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
        # Which level files have unsaved edits (per-level dirty tracking). The
        # master ``_dirty`` flag mirrors ``bool(self._dirty_levels)``.
        self._dirty_levels: set[int] = set()
        # True while the sidebar shows the redefine "assign a class" catalog panel.
        self._redefine_panel_active = False
        # Undo/redo history of geometry edit records ({annotation, before, after, image_index}).
        self._undo_stack: list[dict] = []
        self._redo_stack: list[dict] = []

        # Validation (review) vs annotation (draw) mode. Validation is the default tool.
        self.mode = "validation"
        self.annotation_level = levels.LEVEL_IDS[0]
        # Per-level "active" drawing class id (single-select). None = nothing selected.
        self.active_category_by_level: dict[int, int | None] = {lvl: None for lvl in levels.LEVEL_IDS}
        # Annotation-mode stores: level -> {image basename -> [annotation dicts]}.
        # Keyed by basename so they survive id-scheme changes across sessions.
        self.annotation_store: dict[int, dict[str, list]] = {lvl: {} for lvl in levels.LEVEL_IDS}
        # Folder the per-level COCO JSON files live in (the images folder's parent pick).
        # When a project is open this points at <project>/annotations.
        self.annotation_root = ""
        # The open project (shared working area for both modes), or None on the
        # welcome screen. Replaces ad-hoc folder picking once a project exists.
        self.project: Project | None = None
        # Each mode keeps its own loaded session so toggling doesn't lose work.
        self._sessions: dict[str, dict] = {}

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
        # Current-image working state (set when an image loads; defaulted so mode
        # switching is safe before any dataset/images are opened).
        self.index = 0
        self.current_annotations: list = []
        self.current_overlay_items: list[dict] = []
        self.current_image_name = ""
        self.current_image_path = ""
        self.has_polygons = False
        # Cache of decoded RLE polygons keyed by id(annotation); decoding full
        # masks is expensive and overlays rebuild on every class toggle.
        self._rle_polygon_cache: dict[int, list[list[float]]] = {}
        self.last_opened_directory = self._read_last_opened_directory()
        self._build_ui()
        self._apply_styles()
        self.canvas.annotationsChanged.connect(self._on_annotations_changed)
        self.canvas.annotationCreated.connect(self._on_annotation_created)
        self.canvas.selectionChanged.connect(self._on_selection_changed)

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
            ("open", "Open", "O", [], self._open_shortcut),
            ("toggle_mode", "Switch validation/annotate", "M", [], self._toggle_mode),
            ("toggle_draw", "Toggle draw (annotate)", "D", [], self._toggle_draw_shortcut),
            ("level_1", "Annotate level 1", "1", [], lambda: self._shortcut_level(1)),
            ("level_2", "Annotate level 2", "2", [], lambda: self._shortcut_level(2)),
            ("level_3", "Annotate level 3", "3", [], lambda: self._shortcut_level(3)),
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
        self._build_menu_bar()

        # Two-page central area: a welcome screen (no project) and the working
        # view (canvas + sidebar). _update_project_chrome() picks which shows.
        self.view_stack = QStackedWidget()
        self.setCentralWidget(self.view_stack)

        self.welcome_page = self._build_welcome_page()
        self.view_stack.addWidget(self.welcome_page)  # index 0

        root = QWidget()
        self.view_stack.addWidget(root)  # index 1

        main_layout = QHBoxLayout(root)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # The canvas and an "empty state" overlay share one grid cell so the
        # overlay (3 import buttons) covers the canvas when no image is shown.
        self.canvas = ImageCanvas()
        canvas_container = QWidget()
        canvas_grid = QGridLayout(canvas_container)
        canvas_grid.setContentsMargins(0, 0, 0, 0)
        canvas_grid.addWidget(self.canvas, 0, 0)
        self.empty_overlay = self._build_empty_overlay()
        canvas_grid.addWidget(self.empty_overlay, 0, 0)
        self.empty_overlay.raise_()
        self.empty_overlay.setVisible(False)
        main_layout.addWidget(canvas_container, 1)

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

        # ----- Mode toggle: Validation (review) vs Annotate (draw) -----
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.validation_mode_button = QPushButton("Validation")
        self.validation_mode_button.setObjectName("toggleButton")
        self.validation_mode_button.setCheckable(True)
        self.validation_mode_button.setChecked(True)
        self.validation_mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.validation_mode_button.setToolTip("Review existing annotations (read/edit)")
        self.validation_mode_button.clicked.connect(lambda: self._set_mode("validation"))
        self.annotation_mode_button = QPushButton("Annotate")
        self.annotation_mode_button.setObjectName("toggleButton")
        self.annotation_mode_button.setCheckable(True)
        self.annotation_mode_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.annotation_mode_button.setToolTip("Draw new annotations per level")
        self.annotation_mode_button.clicked.connect(lambda: self._set_mode("annotation"))
        self.mode_group.addButton(self.validation_mode_button)
        self.mode_group.addButton(self.annotation_mode_button)
        mode_row.addWidget(self.validation_mode_button, 1)
        mode_row.addWidget(self.annotation_mode_button, 1)
        sidebar_layout.addLayout(mode_row)

        # ----- Level selector (annotation mode only) -----
        self.level_selector_widget = QWidget()
        level_row = QHBoxLayout(self.level_selector_widget)
        level_row.setContentsMargins(0, 0, 0, 0)
        level_row.setSpacing(6)
        self.level_group = QButtonGroup(self)
        self.level_group.setExclusive(True)
        self.level_buttons: dict[int, QPushButton] = {}
        for level_id in levels.LEVEL_IDS:
            level_button = QPushButton(f"L{level_id}")
            level_button.setObjectName("toggleButton")
            level_button.setCheckable(True)
            level_button.setCursor(Qt.CursorShape.PointingHandCursor)
            level_button.setToolTip(f"Level {level_id} — {levels.level_title(level_id)}")
            level_button.clicked.connect(lambda _checked=False, lvl=level_id: self._set_level(lvl))
            self.level_group.addButton(level_button)
            self.level_buttons[level_id] = level_button
            level_row.addWidget(level_button, 1)
        self.level_buttons[self.annotation_level].setChecked(True)
        sidebar_layout.addWidget(self.level_selector_widget)
        self.level_selector_widget.setVisible(False)

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
        image_selector_view.setItemDelegate(RedefineRowDelegate(image_selector_view))
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

        # Retired: loading now flows through New/Open project + the Import menu.
        # Kept (hidden) only so existing references stay valid.
        self.open_button = QPushButton("Open zip / folder")
        self.open_button.clicked.connect(self._open_folder)
        self.open_button.setVisible(False)
        sidebar_layout.addWidget(self.open_button)

        self.open_images_json_button = QPushButton("Open images + JSON")
        self.open_images_json_button.clicked.connect(self._open_images_and_json)
        self.open_images_json_button.setVisible(False)
        sidebar_layout.addWidget(self.open_images_json_button)

        # Annotation-mode entry: pick an images folder (resumes per-level JSONs if present).
        self.open_images_button = QPushButton("Open images")
        self.open_images_button.setToolTip("Pick an images folder to annotate (loads existing per-level JSONs if found)")
        self.open_images_button.clicked.connect(self._open_images_for_annotation)
        self.open_images_button.setVisible(False)
        sidebar_layout.addWidget(self.open_images_button)

        # Annotation-mode entry: convert an existing COCO dataset into the 3 levels.
        self.open_dataset_button = QPushButton("Open dataset (convert)")
        self.open_dataset_button.setToolTip("Load an existing COCO dataset folder and sort its annotations into the 3 levels by class name")
        self.open_dataset_button.clicked.connect(self._open_dataset_for_annotation)
        self.open_dataset_button.setVisible(False)
        sidebar_layout.addWidget(self.open_dataset_button)

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

        self.save_all_button = QPushButton("Save all")
        self.save_all_button.setToolTip("Save all three levels' COCO JSON files")
        self.save_all_button.setEnabled(False)
        self.save_all_button.setVisible(False)
        self.save_all_button.clicked.connect(self._save_all_levels)

        save_row = QHBoxLayout()
        save_row.addWidget(self.save_button, 1)
        save_row.addWidget(self.save_all_button)
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

        self.draw_button = QPushButton("Draw")
        self.draw_button.setObjectName("toggleButton")
        self.draw_button.setCheckable(True)
        self.draw_button.setChecked(False)
        self.draw_button.setToolTip("Draw new annotations with the selected class (pan with middle mouse)")
        self.draw_button.setVisible(False)
        self.draw_button.toggled.connect(self._on_draw_toggled)

        self.edit_objects_button = QPushButton("Edit objects")
        self.edit_objects_button.setObjectName("toggleButton")
        self.edit_objects_button.setCheckable(True)
        self.edit_objects_button.setChecked(False)
        self.edit_objects_button.setToolTip("Toggle editing of box corners / polygon vertices (T)")
        self.edit_objects_button.toggled.connect(self._on_edit_objects_toggled)

        self.delete_button = QPushButton("✕")  # white cross
        self.delete_button.setObjectName("dangerButton")
        self.delete_button.setToolTip("Delete the selected annotation (Del)")
        self.delete_button.setEnabled(False)
        self.delete_button.setVisible(False)
        delete_side = int(round(30 * DPI_SCALE))
        self.delete_button.setFixedSize(delete_side, delete_side)
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self._delete_selected_annotation)

        self.labels_checkbox = QCheckBox("Show labels")
        self.labels_checkbox.setChecked(True)
        self.labels_checkbox.stateChanged.connect(self._on_labels_toggled)
        self.reset_colors_button = QPushButton("Reset colors")
        self.reset_colors_button.clicked.connect(self._reset_colors_to_default)

        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(6)
        toggle_row.addWidget(self.draw_button)
        toggle_row.addWidget(self.edit_objects_button)
        toggle_row.addWidget(self.delete_button)
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

        self.class_action_widget = QWidget()
        class_action_row = QHBoxLayout(self.class_action_widget)
        class_action_row.setContentsMargins(0, 0, 0, 0)
        self.show_all_button = QPushButton("Show all")
        self.show_all_button.clicked.connect(self._show_all_classes)
        class_action_row.addWidget(self.show_all_button)

        self.hide_all_button = QPushButton("Hide all")
        self.hide_all_button.clicked.connect(self._hide_all_classes)
        class_action_row.addWidget(self.hide_all_button)
        sidebar_layout.addWidget(self.class_action_widget)

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

        # Start on the welcome screen until a project is created/opened.
        self._update_project_chrome()

    # ----- project: menu bar, welcome screen, open/close -------------------
    def _build_menu_bar(self) -> None:
        """Top menu bar: Project / Import / Settings (the project control surface)."""
        bar = self.menuBar()

        project_menu = bar.addMenu("&Project")
        self.action_new_project = QAction("New project…", self)
        self.action_new_project.triggered.connect(self._new_project)
        project_menu.addAction(self.action_new_project)
        self.action_open_project = QAction("Open project…", self)
        self.action_open_project.triggered.connect(self._open_project)
        project_menu.addAction(self.action_open_project)
        project_menu.addSeparator()
        self.action_close_project = QAction("Close project", self)
        self.action_close_project.triggered.connect(self._close_project)
        project_menu.addAction(self.action_close_project)
        self.action_export_zip = QAction("Export as zip…", self)
        self.action_export_zip.setToolTip("Bundle the whole project (images + level JSONs) into one shareable zip")
        self.action_export_zip.triggered.connect(self._export_project_zip)
        self.action_export_zip.setEnabled(False)
        project_menu.addAction(self.action_export_zip)
        project_menu.addSeparator()
        self.action_redefine = QAction("Redefine classes…", self)
        self.action_redefine.setToolTip("Assign imported off-catalog classes to a level + existing class")
        self.action_redefine.triggered.connect(self._open_redefine_dialog)
        self.action_redefine.setEnabled(False)
        project_menu.addAction(self.action_redefine)
        project_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        project_menu.addAction(quit_action)

        # Import actions only make sense inside an open project. Zip is first:
        # it is the fullest import (images + annotation JSONs in one archive).
        self.import_menu = bar.addMenu("&Import")
        self.action_import_zip = QAction("Open zip…", self)
        self.action_import_zip.setToolTip("Import an export zip (images + annotation JSONs)")
        self.action_import_zip.triggered.connect(self._import_zip)
        self.import_menu.addAction(self.action_import_zip)
        self.action_import_images = QAction("Open image folder…", self)
        self.action_import_images.triggered.connect(self._import_image_folder)
        self.import_menu.addAction(self.action_import_images)
        self.action_import_annotations = QAction("Open annotations…", self)
        self.action_import_annotations.setToolTip("Import annotation JSON(s) — available once the project has images")
        self.action_import_annotations.triggered.connect(self._import_annotations)
        self.import_menu.addAction(self.action_import_annotations)

        settings_menu = bar.addMenu("&Settings")
        shortcuts_action = QAction("Keyboard shortcuts…", self)
        shortcuts_action.triggered.connect(self._show_help)
        settings_menu.addAction(shortcuts_action)

    def _build_welcome_page(self) -> QWidget:
        """The opening screen shown when no project is open."""
        page = QWidget()
        page.setObjectName("welcomePage")
        outer = QVBoxLayout(page)
        outer.addStretch(1)

        column = QVBoxLayout()
        column.setSpacing(14)

        title = QLabel("Annotation Workbench")
        title.setObjectName("header")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)

        subtitle = QLabel("Create a new project or open an existing one to begin.")
        subtitle.setObjectName("muted")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(subtitle)
        column.addSpacing(8)

        new_button = QPushButton("New project…")
        new_button.setObjectName("primaryButton")
        new_button.setMinimumHeight(44)
        new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        new_button.clicked.connect(self._new_project)
        column.addWidget(new_button)

        open_button = QPushButton("Open project…")
        open_button.setMinimumHeight(44)
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.clicked.connect(self._open_project)
        column.addWidget(open_button)

        container = QWidget()
        container.setLayout(column)
        container.setFixedWidth(340)

        centering = QHBoxLayout()
        centering.addStretch(1)
        centering.addWidget(container)
        centering.addStretch(1)
        outer.addLayout(centering)
        outer.addStretch(1)
        return page

    def _build_empty_overlay(self) -> QWidget:
        """Covers the canvas when a project is open but no image is shown.

        Holds the same three import actions as the Import menu, centered, so the
        user can load data without going to the menu bar.
        """
        overlay = QWidget()
        overlay.setObjectName("emptyOverlay")
        outer = QVBoxLayout(overlay)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("emptyCard")
        card.setFixedWidth(440)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 26, 28, 26)
        card_layout.setSpacing(12)

        title = QLabel("No images yet")
        title.setObjectName("header")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)
        subtitle = QLabel("Import a zip, an image folder, or annotations to start working.")
        subtitle.setObjectName("muted")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(6)

        zip_button = QPushButton("Open zip…")
        zip_button.setObjectName("primaryButton")
        zip_button.setMinimumHeight(40)
        zip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        zip_button.clicked.connect(self._import_zip)
        card_layout.addWidget(zip_button)

        images_button = QPushButton("Open image folder…")
        images_button.setMinimumHeight(40)
        images_button.setCursor(Qt.CursorShape.PointingHandCursor)
        images_button.clicked.connect(self._import_image_folder)
        card_layout.addWidget(images_button)

        self.empty_annotations_button = QPushButton("Open annotations…")
        self.empty_annotations_button.setMinimumHeight(40)
        self.empty_annotations_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.empty_annotations_button.setToolTip("Available once the project has images")
        self.empty_annotations_button.clicked.connect(self._import_annotations)
        card_layout.addWidget(self.empty_annotations_button)

        centering = QHBoxLayout()
        centering.addStretch(1)
        centering.addWidget(card)
        centering.addStretch(1)
        outer.addLayout(centering)
        outer.addStretch(1)
        return overlay

    def _refresh_canvas_placeholder(self) -> None:
        """Show the 3-button empty overlay when a project is open but no image shows."""
        if not hasattr(self, "empty_overlay"):
            return
        has_images = self.project is not None and bool(self.project.registry_images())
        show = self.project is not None and not self.canvas.has_image
        self.empty_overlay.setVisible(show)
        if show:
            self.empty_overlay.raise_()
        if hasattr(self, "empty_annotations_button"):
            self.empty_annotations_button.setEnabled(has_images)

    def _update_project_chrome(self) -> None:
        """Reflect project-open state in the window: which page shows, title, menus."""
        has_project = self.project is not None
        has_images = has_project and bool(self.project.registry_images())
        # view_stack/import_menu exist only after _build_ui has created them.
        if hasattr(self, "view_stack"):
            self.view_stack.setCurrentIndex(1 if has_project else 0)
        if hasattr(self, "action_close_project"):
            self.action_close_project.setEnabled(has_project)
        if hasattr(self, "action_export_zip"):
            self.action_export_zip.setEnabled(has_project)
        if hasattr(self, "import_menu"):
            self.import_menu.setEnabled(has_project)
            # Annotations need images to attach to: greyed until the project has any.
            self.action_import_annotations.setEnabled(has_images)
        if has_project:
            counts = self.project.level_counts()
            total = sum(counts.values())
            self.setWindowTitle(
                f"Annotation Workbench — {self.project.name}"
                f"  ({len(self.project.registry_images())} images, {total} annotations)"
            )
        else:
            self.setWindowTitle("Annotation Workbench")
        self._refresh_canvas_placeholder()
        self._update_redefine_action()

    def _ask_unsaved_resolution(self, levels_text: str) -> str:
        """Modal Save all / Discard / Cancel for unsaved edits; returns the choice id.

        Split out from the guard so headless tests can stub the modal (it blocks
        forever under the offscreen platform).
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setWindowTitle("Unsaved changes")
        box.setText(f"You have unsaved edits in {levels_text}.\nSave them first?")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.addButton("Save all", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setStyleSheet(_DARK_DIALOG_QSS + "QPushButton { min-width: 84px; }")
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return "cancel"
        if clicked is discard_btn:
            return "discard"
        return "save"

    def _confirm_discard_unsaved(self, action: str) -> bool:
        """Return True if it's OK to proceed past unsaved edits (save/discard chosen)."""
        if self.project is None or not self._dirty:
            return True
        levels_text = ", ".join(f"L{n}" for n in sorted(self._dirty_levels)) or "this project"
        choice = self._ask_unsaved_resolution(levels_text)
        if choice == "cancel":
            return False
        if choice == "save":
            if not self.annotation_root:
                return False
            ok, _total, _names = self._persist_all_levels()
            return ok
        return True  # discard

    def _export_project_zip(self) -> None:
        """Bundle the open project into one shareable zip (flushing level edits first)."""
        if self.project is None:
            return
        default = os.path.join(self.last_opened_directory or str(Path.cwd()), f"{self.project.name}.zip")
        dest, _filter = QFileDialog.getSaveFileName(
            self, "Export project as zip", default, "Zip archives (*.zip);;All files (*.*)"
        )
        if not dest:
            return
        # Flush in-memory level edits so the archive matches the on-screen state.
        if self.dataset is not None and self.annotation_root:
            ok, _total, _names = self._persist_all_levels()
            if not ok:
                return
        try:
            out = self.project.export_zip(dest)
        except ProjectError as error:
            self._show_message(str(error))
            return
        self._save_last_opened_directory(dest)
        self._show_message(f"Exported project to\n{out.name}")

    def _new_project(self) -> None:
        if not self._confirm_discard_unsaved("start a new project"):
            return
        # One step using the NATIVE Windows explorer: its "Save As" dialog is real
        # explorer with a filename field, so the user browses to the destination
        # and types the project name in the bottom box (no separate name prompt).
        target_path, _filter = QFileDialog.getSaveFileName(
            self,
            "New project — choose a location and type the project name",
            self.last_opened_directory or str(Path.cwd()),
        )
        if not target_path:
            return
        target = Path(target_path)
        try:
            project = Project.create(str(target.parent), target.name)
        except ProjectError as error:
            self._show_message(str(error))
            return
        self._save_last_opened_directory(str(target.parent))
        self._activate_project(project)

    def _open_project(self) -> None:
        if not self._confirm_discard_unsaved("open another project"):
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Open project folder", self.last_opened_directory or str(Path.cwd())
        )
        if not folder:
            return
        try:
            project = Project.open(folder)
        except ProjectError as error:
            self._show_message(str(error))
            return
        self._save_last_opened_directory(folder)
        self._activate_project(project)

    def _activate_project(self, project: Project) -> None:
        """Make ``project`` the open project and load its images/levels into the session."""
        self.project = project
        self._sessions.clear()  # drop any previous project's per-mode sessions
        # Rescue any stashed orphans whose image is already present (e.g. ones a
        # past routing bug stashed despite the image being in the project) before
        # we read the level files into the session.
        try:
            project.promote_pending()
        except ProjectError:
            pass
        self._apply_project_session()
        self._update_project_chrome()

    def _close_project(self) -> None:
        if self.project is None:
            return
        if not self._confirm_discard_unsaved("close the project"):
            return
        self.project = None
        self._sessions.clear()
        self._reset_session_state()
        self.canvas.set_idle()
        self.current_overlay_items = []
        self._apply_mode_chrome()  # project gone: hide level selector again in validation
        self._populate_image_selector()
        self._update_status_labels()
        self._update_action_buttons()
        self._update_project_chrome()

    def _apply_project_session(self, reset_index: bool = True) -> None:
        """Load the open project's images + per-level stores into the current session.

        Bridges the project layout onto the existing annotation plumbing:
        ``images_path`` -> <project>/images and ``annotation_root`` ->
        <project>/annotations, so the existing per-level load/save code writes
        into the project unchanged. ``images`` comes from the master registry.
        """
        if self.project is None:
            return
        self._cleanup_temp_extraction()
        self._rle_polygon_cache.clear()
        self._dirty = False
        self._dirty_levels.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.dataset = {"annotation": True}  # truthy sentinel so shared guards pass
        self.temp_extraction = None
        self.images_path = str(self.project.images_dir)
        self.annotation_root = str(self.project.annotations_dir)
        self.images = [dict(record) for record in self.project.registry_images()]
        self.annotations_by_image_id = {}
        self.annotation_store = {lvl: {} for lvl in levels.LEVEL_IDS}
        self.categories_by_id = {}
        self.class_items = []
        self.current_category_ids = []
        self.current_class_items = []
        if reset_index:
            self.index = 0
        elif self.images:
            self.index = max(0, min(self.index, len(self.images) - 1))
        self._load_existing_level_files()
        self._populate_image_selector()
        # A project is now open: reveal the level selector + per-level controls
        # for the current mode (opening a project no longer requires toggling the
        # mode buttons first to surface the L1/L2/L3 row).
        self._apply_mode_chrome()
        # Fill the sidebar for the current mode (validation lists the level catalog).
        if self.mode == "annotation":
            self._populate_level_classes()
        else:
            self._populate_class_checkboxes()
        if self.images:
            self._load_current_image(reset_fit=True)
        else:
            self.canvas.set_idle()
            self.current_overlay_items = []
        self._update_status_labels()
        self._update_action_buttons()
        self._sync_canvas_state()
        self._sync_draw_state()

    def _import_image_folder(self) -> None:
        """Import: copy an image folder into the project (dedup), then promote stash."""
        if self.project is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose an image folder to import", self.last_opened_directory or str(Path.cwd())
        )
        if not folder:
            return
        source_dir = discover_images_dir(folder)
        try:
            report = self.project.import_images(source_dir)
            promoted = self.project.promote_pending()
        except ProjectError as error:
            self._show_message(str(error))
            return
        self._save_last_opened_directory(folder)
        self._apply_project_session(reset_index=False)
        self._update_project_chrome()
        message = (
            f"Imported {len(report['copied'])} image(s); "
            f"skipped {len(report['skipped'])} already present."
        )
        if promoted.get("promoted"):
            message += f"\nPromoted {promoted['promoted']} stashed annotation(s) into their levels."
        self._show_message(message)

    def _import_zip(self) -> None:
        """Import an export zip: copy its images (dedup) + route its annotations."""
        if self.project is None:
            return
        zip_path, _filter = QFileDialog.getOpenFileName(
            self,
            "Open export zip (images + annotation JSONs)",
            self.last_opened_directory or str(Path.cwd()),
            "Zip archives (*.zip);;All files (*.*)",
        )
        if not zip_path:
            return
        temp_extraction = None
        try:
            images_path, annotations_path, temp_extraction = resolve_dataset_paths(zip_path)
            self.project.import_images(images_path, source=f"zip:{Path(zip_path).name}")
            self.project.promote_pending()
            images, _by_id, annotations_by_image_id, categories_by_id = load_coco_data(
                annotations_path, images_path
            )
        except Exception as error:  # noqa: BLE001 - surface any extract/parse failure
            self._show_message(f"Could not import zip: {error}")
            return
        finally:
            if temp_extraction is not None:
                # Images are already copied into the project; the temp extract can go.
                try:
                    temp_extraction.cleanup()
                except Exception:
                    pass
        self._save_last_opened_directory(zip_path)
        self._route_and_commit_annotations(images, annotations_by_image_id, categories_by_id)

    def _import_annotations(self) -> None:
        """Import annotation JSON(s) into the project's levels (image must be present)."""
        if self.project is None:
            return
        json_path, _filter = QFileDialog.getOpenFileName(
            self,
            "Open annotations JSON",
            self.last_opened_directory or str(Path.cwd()),
            "JSON files (*.json);;All files (*.*)",
        )
        if not json_path:
            return
        try:
            images, _by_id, annotations_by_image_id, categories_by_id = load_coco_data(
                json_path, str(self.project.images_dir)
            )
        except Exception as error:  # noqa: BLE001
            self._show_message(f"Could not load annotations: {error}")
            return
        self._save_last_opened_directory(json_path)
        self._route_and_commit_annotations(images, annotations_by_image_id, categories_by_id)

    def _ask_conflict_resolution(self, conflict_count: int) -> str:
        """Ask how to handle images that already have annotations. Returns the choice.

        'append' (add to existing), 'replace' (overwrite existing), or 'close'
        (cancel the WHOLE import — nothing is written).
        """
        box = QMessageBox(self)
        box.setWindowTitle("Annotation conflicts")
        box.setIcon(QMessageBox.Icon.NoIcon)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            f"{conflict_count} image/level pair(s) already have annotations.\n\n"
            "Append the imported annotations to them, replace them, or cancel "
            "the whole import?"
        )
        append_button = box.addButton("Append", QMessageBox.ButtonRole.AcceptRole)
        replace_button = box.addButton("Replace", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Close (cancel import)", QMessageBox.ButtonRole.RejectRole)
        box.setStyleSheet(_DARK_DIALOG_QSS + "QPushButton { min-width: 96px; }")
        box.exec()
        clicked = box.clickedButton()
        if clicked is append_button:
            return "append"
        if clicked is replace_button:
            return "replace"
        return "close"

    def _route_and_commit_annotations(self, images, annotations_by_image_id, categories_by_id) -> None:
        """Sort a loaded dataset's annotations into the project's levels and commit.

        Routes each annotation to a level by class name, dropping off-catalog and
        geometry-less ones (reported). Annotations whose image is present are
        added (or, on conflict, resolved via the Append/Replace/Close dialog);
        annotations whose image is absent are stashed. Close aborts the entire
        import, writing nothing.
        """
        if self.project is None:
            return

        id_to_name = {
            image.get("id"): os.path.basename(str(image.get("file_name", "")))
            for image in images
        }

        # Bucket converted annotations by level -> image basename, recording WHY
        # anything was dropped so the report can explain it.
        buckets: dict[int, dict[str, list]] = {lvl: {} for lvl in levels.LEVEL_IDS}
        off_catalog: dict[str, int] = {}   # unknown class name -> count
        no_geometry: dict[str, int] = {}   # known class but unusable geometry -> count
        no_image_ref = 0                    # annotation whose image_id isn't in the file
        unmapped_entries: list[dict] = []   # off-catalog -> stashed raw for Redefine
        for image_id, annotations in annotations_by_image_id.items():
            basename = id_to_name.get(image_id)
            if not basename:
                no_image_ref += len(annotations)
                continue
            for annotation in annotations:
                class_name = categories_by_id.get(annotation.get("category_id"))
                label = str(class_name) if class_name is not None else f"category {annotation.get('category_id')}"
                resolved = self._resolve_class(class_name)
                if resolved is None:
                    # Off-catalog: keep the raw annotation for Redefine (not dropped).
                    off_catalog[label] = off_catalog.get(label, 0) + 1
                    unmapped_entries.append(
                        {
                            "raw_class": label,
                            "stable_image_id": stable_image_id(basename),
                            "file_name": basename,
                            "source_annotation": annotation,
                        }
                    )
                    continue
                level, target = resolved
                converted = self._convert_source_annotation(annotation, level, target)
                if converted is None:
                    no_geometry[label] = no_geometry.get(label, 0) + 1
                    continue
                buckets[level].setdefault(basename, []).append(converted)

        existing_stores = {lvl: self.project.read_level(lvl) for lvl in levels.LEVEL_IDS}

        additions: list[tuple[int, str, list]] = []   # present, no existing -> just add
        conflicts: list[tuple[int, str, list]] = []   # present, existing -> ask
        orphans: list[tuple[int, str, list]] = []     # image absent -> stash
        for level, by_name in buckets.items():
            for source_basename, anns in by_name.items():
                # Identity is by stable_image_id (the same key import/dedup use),
                # NOT exact filename: the same photo can be re-exported under a new
                # prefix (e.g. "<uuidA>-PHOTO.jpg" vs "<uuidB>-PHOTO.jpg") yet share
                # an id. Matching on the raw string would wrongly stash an
                # already-present image. Resolve to the registry's own basename so
                # the annotation lands on the image the project actually holds.
                record = self.project.image_record(stable_image_id(source_basename))
                if record is None:
                    orphans.append((level, source_basename, anns))
                    continue
                basename = os.path.basename(str(record["file_name"]))
                if existing_stores[level].get(basename):
                    conflicts.append((level, basename, anns))
                else:
                    additions.append((level, basename, anns))

        mode = "append"
        if conflicts:
            mode = self._ask_conflict_resolution(len(conflicts))
            if mode == "close":
                self._show_message("Import cancelled — nothing was changed.")
                return

        # Commit. (All decisions are made; only now do we touch disk.)
        changed_levels: set[int] = set()
        for level, basename, anns in additions:
            existing_stores[level].setdefault(basename, []).extend(anns)
            changed_levels.add(level)
        for level, basename, anns in conflicts:
            if mode == "replace":
                existing_stores[level][basename] = list(anns)
            else:
                existing_stores[level].setdefault(basename, []).extend(anns)
            changed_levels.add(level)
        for level in changed_levels:
            self.project.write_level(level, existing_stores[level])

        stash_entries = [
            {
                "level": level,
                "stable_image_id": stable_image_id(basename),
                "file_name": basename,
                "annotation": annotation,
            }
            for level, basename, anns in orphans
            for annotation in anns
        ]
        if stash_entries:
            self.project.stash_annotations(stash_entries)
        if unmapped_entries:
            self.project.stash_unmapped(unmapped_entries)

        self._apply_project_session(reset_index=False)
        self._update_project_chrome()

        added = sum(len(a) for _l, _b, a in additions) + sum(len(a) for _l, _b, a in conflicts)
        message = f"Imported {added} annotation(s) into levels."
        if conflicts:
            message += f"\n{len(conflicts)} conflict(s) {mode}d."
        if stash_entries:
            message += f"\n{len(stash_entries)} stashed (image not in project yet)."

        # Off-catalog classes are set aside (not dropped) — they're resolvable in Redefine.
        off_total = sum(off_catalog.values())
        geo_total = sum(no_geometry.values())
        if off_total:
            message += (
                f"\n\n{off_total} annotation(s) use {len(off_catalog)} unknown class(es), "
                "set aside for Redefine:\n  " + self._format_class_counts(off_catalog)
            )
        if geo_total:
            message += (
                f"\n\n{geo_total} dropped — no usable geometry (e.g. RLE-only masks):\n  "
                + self._format_class_counts(no_geometry)
            )
        if no_image_ref:
            message += f"\n\n{no_image_ref} skipped — annotation referenced an image not in the file."
        self._show_message(message)

        # Auto-pop Redefine when this import introduced new unknown classes.
        if unmapped_entries:
            self._open_redefine_dialog()

    def _resolve_class(self, class_name) -> "tuple[int, str] | None":
        """Resolve a source class name to (level, target_class), honouring saved remaps.

        Returns None for off-catalog classes that have no saved remap yet.
        """
        if class_name is None:
            return None
        name = str(class_name)
        level = levels.level_for_class(name)
        if level is not None:
            return level, name
        remap = self.project.remap_for(name) if self.project is not None else None
        if remap:
            return remap["level"], remap["target"]
        return None

    @staticmethod
    def _format_class_counts(counts: dict[str, int], limit: int = 12) -> str:
        """'name ×N, other ×M (+k more)' for a drop-reason breakdown."""
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        shown = ", ".join(f"{name} ×{count}" for name, count in ordered[:limit])
        if len(ordered) > limit:
            shown += f" (+{len(ordered) - limit} more)"
        return shown

    # ----- Redefine: resolve off-catalog classes -------------------------
    def _promote_unmapped(self) -> dict:
        """Convert+route stashed off-catalog annotations whose class is now remapped.

        Returns ``{"promoted": N, "remaining": M}``. Resolved annotations land in
        the target level (or the image-orphan stash if their image isn't present).
        """
        if self.project is None:
            return {"promoted": 0, "remaining": 0}
        pending = self.project.read_unmapped()
        if not pending:
            return {"promoted": 0, "remaining": 0}

        present = set(self.project.basename_to_id().keys())
        additions: dict[int, dict[str, list]] = {}
        orphan_entries: list[dict] = []
        remaining: list[dict] = []
        promoted = 0
        for entry in pending:
            remap = self.project.remap_for(str(entry.get("raw_class", "")))
            if not remap:
                remaining.append(entry)
                continue
            converted = self._convert_source_annotation(
                entry.get("source_annotation", {}), remap["level"], remap["target"]
            )
            if converted is None:
                remaining.append(entry)  # geometry-less: keep stashed rather than lose it
                continue
            basename = os.path.basename(str(entry.get("file_name", "")))
            if basename in present:
                additions.setdefault(remap["level"], {}).setdefault(basename, []).append(converted)
            else:
                orphan_entries.append(
                    {
                        "level": remap["level"],
                        "stable_image_id": int(entry.get("stable_image_id", stable_image_id(basename))),
                        "file_name": basename,
                        "annotation": converted,
                    }
                )
            promoted += 1

        for level, by_name in additions.items():
            store = self.project.read_level(level)
            for basename, anns in by_name.items():
                store.setdefault(basename, []).extend(anns)
            self.project.write_level(level, store)
        if orphan_entries:
            self.project.stash_annotations(orphan_entries)
        self.project.write_unmapped(remaining)
        return {"promoted": promoted, "remaining": len(remaining)}

    def _open_redefine_dialog(self) -> None:
        """Show the Redefine dialog for the project's stashed off-catalog classes."""
        if self.project is None:
            return
        counts = self.project.unmapped_class_counts()
        if not counts:
            self._show_message("No unknown classes to redefine.")
            self._update_redefine_action()
            return
        dialog = RedefineDialog(self, counts, self.project.unmapped_assigned_levels())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mappings = dialog.mappings()
        level_only = dialog.level_only()
        if not mappings and not level_only:
            self._update_redefine_action()
            return
        # Level-only: park the class in a level (visible + highlighted, not committed).
        for raw_class, level in level_only.items():
            self.project.set_unmapped_level(raw_class, level)
        # Full remaps: record + promote into the level files.
        for raw_class, (level, target) in mappings.items():
            self.project.set_class_remap(raw_class, level, target)
        result = self._promote_unmapped()
        self._apply_project_session(reset_index=False)
        self._refresh_overlay_items()
        self._update_project_chrome()
        self._update_redefine_action()
        parts = []
        if mappings:
            parts.append(f"Redefined {len(mappings)} class(es); moved {result['promoted']} annotation(s) into levels.")
        if level_only:
            parts.append(f"Parked {len(level_only)} class(es) for redefinition (shown highlighted in their level).")
        if result["remaining"]:
            parts.append(f"{result['remaining']} still awaiting redefinition.")
        self._show_message("\n".join(parts))

    def _update_redefine_action(self) -> None:
        """Enable + badge the Redefine menu item with the pending-class count."""
        if not hasattr(self, "action_redefine"):
            return
        count = len(self.project.unmapped_class_counts()) if self.project is not None else 0
        self.action_redefine.setEnabled(self.project is not None and count > 0)
        self.action_redefine.setText(f"Redefine classes… ({count})" if count else "Redefine classes…")

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
            QWidget#welcomePage {{
                background: #111315;
            }}
            QWidget#emptyOverlay {{
                background: #141619;
            }}
            QFrame#emptyCard {{
                background: #1c1f24;
                border: 1px solid #4e545d;
                border-radius: 18px;
            }}
            QMenuBar {{
                background: #1c1f24;
                color: #e6e8ec;
                border-bottom: 1px solid #3a4048;
            }}
            QMenuBar::item {{
                background: transparent;
                padding: 5px 12px;
            }}
            QMenuBar::item:selected {{
                background: #2f343c;
            }}
            QMenu {{
                background: #1d2024;
                color: #e6e8ec;
                border: 1px solid #3a4048;
            }}
            QMenu::item:selected {{
                background: #6a72e6;
                color: #ffffff;
            }}
            QMenu::item:disabled {{
                color: #7b818b;
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
            QPushButton#toggleButton {{
                padding: 5px 9px;
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
            QPushButton#dangerButton {{
                background: #c0392b;
                border: 1px solid #e05545;
                color: #ffffff;
                border-radius: 6px;
                padding: 0px;
                font-size: {max(11, int(round(12 * DPI_SCALE)))}pt;
                font-weight: 700;
            }}
            QPushButton#dangerButton:hover {{
                background: #d24433;
            }}
            QPushButton#dangerButton:pressed {{
                background: #a32f23;
            }}
            QPushButton#dangerButton:disabled {{
                background: #4a3330;
                border: 1px solid #5a3b38;
                color: #8a6f6c;
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
        # On a project, validation colours the bubbles from the level catalog, but
        # the LIST is built from the classes actually present on the current image
        # (see _set_validation_level_class_items), like plain validation mode.
        level_colors = (
            levels.level_class_colors(self.annotation_level)
            if (self.project is not None and self.mode == "validation")
            else {}
        )
        for category_id, category_name in self.current_class_items:
            color = color_for_class(str(category_name), int(category_id), level_colors.get(str(category_name)))
            bubble = ClassBubbleButton(category_id, str(category_name), color, self._on_bubble_color_changed, self.class_list_container)
            bubble.setChecked(self.visible_by_category.get(category_id, True))
            bubble.toggled.connect(lambda checked, cid=category_id: self._on_class_toggled(cid, checked))
            self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, bubble)
            self.class_checkboxes[category_id] = bubble

    def _set_validation_level_class_items(self) -> None:
        """Build the validation sidebar from the classes PRESENT in the current
        image's active-level annotations (not the whole level catalog)."""
        level = self.annotation_level
        seen: dict[int, str] = {}
        for annotation in self._current_level_annotations():
            category_id = annotation.get("category_id")
            if category_id is None or category_id in seen:
                continue
            name = levels.class_name_for_category(level, category_id)
            if name is None:
                continue
            seen[category_id] = name
        items = sorted(seen.items(), key=lambda kv: str(kv[1]).lower())
        self.current_class_items = [(cid, name) for cid, name in items]
        self.current_category_ids = [cid for cid, _ in items]
        for category_id in self.current_category_ids:
            self.visible_by_category.setdefault(category_id, True)

    def _show_message(self, text: str, title: str = "Annotation Workbench") -> None:
        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.NoIcon)
        message_box.setWindowTitle(title)
        message_box.setText(text)
        message_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        message_box.setTextFormat(Qt.TextFormat.PlainText)
        message_box.setStyleSheet(_DARK_DIALOG_QSS + "QPushButton { min-width: 72px; }")
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
        self._dirty_levels.clear()
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
        if checked and self.draw_button.isChecked():
            self.draw_button.setChecked(False)  # editing and drawing are exclusive
        self.canvas.set_edit_enabled(checked)

    def _on_draw_toggled(self, checked: bool) -> None:
        """Arm/disarm drawing. Draw can be turned on without a class selected —
        the pen just stays inactive (you can't draw) until you pick one; it then
        activates immediately via ``_sync_draw_state`` when a class is chosen."""
        if checked and self.edit_objects_button.isChecked():
            self.edit_objects_button.setChecked(False)  # editing and drawing are exclusive
        self._sync_draw_state()

    def _sync_draw_state(self) -> None:
        """Tell the canvas what (if anything) to draw, based on mode/level/active class."""
        if self.mode != "annotation" or not self.draw_button.isChecked():
            self.canvas.set_draw_shape(None)
            return
        active = self._active_category()
        if active is None:
            self.canvas.set_draw_shape(None)
            return
        class_name = levels.class_name_for_category(self.annotation_level, active)
        default_hex = levels.level_class_colors(self.annotation_level).get(str(class_name))
        color = color_for_class(str(class_name), int(active), default_hex)
        geometry = levels.level_geometry(self.annotation_level)
        shape = "polygon" if geometry == levels.GEOMETRY_POLYGON else "bbox"
        self.canvas.set_draw_shape(shape, (color.red(), color.green(), color.blue()))

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

    # ----- mode + level (validation vs annotation) ------------------------
    # Per-mode session state (each mode keeps its own loaded data on toggle).
    _SESSION_KEYS = (
        "dataset", "temp_extraction", "images_path", "images", "index",
        "annotations_by_image_id", "categories_by_id", "class_items", "category_ids",
        "current_category_ids", "current_class_items", "current_annotations",
        "current_overlay_items", "current_image_name", "current_image_path",
        "visible_by_category", "has_polygons", "annotation_store", "annotation_root",
        "_undo_stack", "_redo_stack", "_dirty",
    )

    def _snapshot_session(self) -> dict:
        return {key: getattr(self, key, None) for key in self._SESSION_KEYS}

    def _restore_session(self, snapshot: dict) -> None:
        for key in self._SESSION_KEYS:
            setattr(self, key, snapshot.get(key))

    def _reset_session_state(self) -> None:
        self.dataset = None
        self.temp_extraction = None
        self.images_path = ""
        self.images = []
        self.index = 0
        self.annotations_by_image_id = {}
        self.categories_by_id = {}
        self.class_items = []
        self.category_ids = []
        self.current_category_ids = []
        self.current_class_items = []
        self.current_annotations = []
        self.current_overlay_items = []
        self.current_image_name = ""
        self.current_image_path = ""
        self.visible_by_category = {}
        self.has_polygons = False
        self.annotation_store = {lvl: {} for lvl in levels.LEVEL_IDS}
        self.annotation_root = ""
        self._undo_stack = []
        self._redo_stack = []
        self._dirty = False

    def _apply_mode_chrome(self) -> None:
        """Show/hide the mode- and project-dependent sidebar controls.

        Pulled out of :meth:`_set_mode` so it can also run on project
        activation/close — opening a project must reveal the L1/L2/L3 selector
        immediately, without first toggling Validation/Annotate by hand.
        """
        is_annotation = self.mode == "annotation"
        has_project = self.project is not None
        # In a project both modes review the same per-level data, so the L1/L2/L3
        # selector is shown for validation too (it picks which level to review).
        self.level_selector_widget.setVisible(is_annotation or has_project)
        # Show-all / hide-all only make sense for multi-visibility validation.
        self.class_action_widget.setVisible(not is_annotation)
        # Standalone open buttons are superseded by the project import menu; keep
        # them hidden (loading now flows through New/Open project + Import).
        self.open_button.setVisible(False)
        self.open_images_json_button.setVisible(False)
        self.open_images_button.setVisible(False)
        self.open_dataset_button.setVisible(False)
        self.draw_button.setVisible(is_annotation)
        # Whole-object select+delete works in both project modes (validation
        # reviewers remove wrong objects too); gated to projects so the dormant
        # legacy non-project validation path is left untouched.
        select_and_delete = is_annotation or has_project
        self.delete_button.setVisible(select_and_delete)
        # "Save all" writes all three level files; useful in either project mode.
        self.save_all_button.setVisible(is_annotation or has_project)
        self.canvas.set_select_enabled(select_and_delete)

    def _set_mode(self, mode: str) -> None:
        """Switch between validation (review) and annotation (draw); each keeps its own session."""
        if mode not in ("validation", "annotation"):
            return
        if mode != self.mode:
            self.canvas.cancel_drawing()
            self.canvas.clear_selection()
            # In a project both modes share one set of images + per-level stores,
            # so we keep the session intact and just re-render. Without a project
            # each mode keeps its own independent dataset (legacy behaviour).
            if self.project is None:
                self._sessions[self.mode] = self._snapshot_session()
                if mode in self._sessions:
                    self._restore_session(self._sessions[mode])
                else:
                    self._reset_session_state()
            self._rle_polygon_cache.clear()
        self.mode = mode
        is_annotation = mode == "annotation"
        # Keep the toggle buttons in sync (also covers programmatic calls).
        self.validation_mode_button.setChecked(not is_annotation)
        self.annotation_mode_button.setChecked(is_annotation)
        self._apply_mode_chrome()
        if not is_annotation:
            # Leaving annotation: stop drawing.
            self.draw_button.setChecked(False)
            self.canvas.set_draw_shape(None)
        if is_annotation:
            self._populate_level_classes()
        else:
            self._populate_class_checkboxes()
        # Show the restored session's current image, or an idle canvas.
        if self.dataset is not None and self.images:
            self._load_current_image(reset_fit=True)
        else:
            self.canvas.set_idle()
            self.current_overlay_items = []
        self._sync_draw_state()
        self._update_action_buttons()
        self._update_status_labels()

    def _set_level(self, level: int) -> None:
        """Choose the active annotation level and show its class list."""
        if level not in levels.LEVELS:
            return
        self.annotation_level = level
        button = self.level_buttons.get(level)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        if self.mode == "annotation":
            self.canvas.clear_selection()
            self._populate_level_classes()
            self._sync_draw_state()
            self._refresh_overlay_items()
            self._update_status_labels()
        elif self.project is not None:
            # Validation on a project: switching level re-reviews that level's data.
            self.canvas.clear_selection()
            self.visible_by_category = {}  # fresh per-level visibility
            self._set_validation_level_class_items()
            self._populate_class_checkboxes()
            self._refresh_overlay_items()
            self._update_status_labels()

    def _active_category(self) -> int | None:
        return self.active_category_by_level.get(self.annotation_level)

    # ----- keyboard-shortcut helpers --------------------------------------
    def _toggle_mode(self) -> None:
        self._set_mode("annotation" if self.mode == "validation" else "validation")

    def _shortcut_level(self, level: int) -> None:
        if self.mode == "annotation":
            self._set_level(level)

    def _toggle_draw_shortcut(self) -> None:
        if self.mode == "annotation":
            self.draw_button.setChecked(not self.draw_button.isChecked())

    def _open_shortcut(self) -> None:
        # With a project open, "O" imports images; otherwise it opens a project.
        if self.project is not None:
            self._import_image_folder()
        else:
            self._open_project()

    def _populate_level_classes(self) -> None:
        """Fill the sidebar with the current level's classes (grouped, single-select)."""
        self._clear_class_checkboxes()
        active = self._active_category()
        for header, class_list in levels.level_groups(self.annotation_level):
            section = self._make_section(header)
            self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, section)
            for class_name, default_hex in class_list:
                category_id = levels.category_id_for_class(self.annotation_level, class_name) or 0
                color = color_for_class(class_name, category_id, default_hex)
                bubble = ClassBubbleButton(category_id, class_name, color, self._on_bubble_color_changed, self.class_list_container)
                bubble.setChecked(category_id == active)
                bubble.toggled.connect(lambda checked, cid=category_id: self._on_level_class_clicked(cid, checked))
                self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, bubble)
                self.class_checkboxes[category_id] = bubble

    def _on_level_class_clicked(self, category_id: int, checked: bool) -> None:
        """Single-select: one active class per level (click the active one again to clear)."""
        if checked:
            self.active_category_by_level[self.annotation_level] = category_id
            for other_id, bubble in self.class_checkboxes.items():
                if other_id != category_id and bubble.isChecked():
                    bubble.blockSignals(True)
                    bubble.setChecked(False)
                    bubble.blockSignals(False)
                    bubble._refresh_style()
        elif self.active_category_by_level.get(self.annotation_level) == category_id:
            self.active_category_by_level[self.annotation_level] = None
        self._sync_draw_state()
        self._update_status_labels()

    # ----- annotation: open / load / per-level stores ---------------------
    def _open_images_for_annotation(self) -> None:
        initial_directory = self.last_opened_directory or str(Path.cwd())
        folder = QFileDialog.getExistingDirectory(self, "Select images folder to annotate", initial_directory)
        if not folder:
            return
        try:
            loaded = load_images_only(folder)
        except Exception as error:
            self._show_message(f"Could not open images: {error}")
            return
        self._save_last_opened_directory(folder)
        self._apply_images_only(loaded)

    def _apply_images_only(self, loaded: dict) -> None:
        """Set up an annotation session from an images-only folder (+ resume per-level JSONs)."""
        self._cleanup_temp_extraction()
        self._rle_polygon_cache.clear()
        self._dirty = False
        self._dirty_levels.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        # Truthy sentinel so shared guards (which test ``self.dataset``) pass.
        self.dataset = {"annotation": True}
        self.temp_extraction = None
        self.annotation_root = loaded["root"]
        self.images_path = loaded["images_path"]
        self.images = loaded["images"]
        self.annotation_store = {lvl: {} for lvl in levels.LEVEL_IDS}
        self.categories_by_id = {}
        self.class_items = []
        self.current_category_ids = []
        self.current_class_items = []
        self.index = 0
        self._load_existing_level_files()
        self._populate_image_selector()
        self._load_current_image(reset_fit=True)
        self._update_status_labels()
        self._update_action_buttons()
        self._sync_canvas_state()
        self._sync_draw_state()

    def _level_json_path(self, level: int) -> Path:
        return Path(self.annotation_root) / f"level{level}.json"

    def _load_existing_level_files(self) -> None:
        """Resume: read any level{N}.json next to the images into the per-level stores."""
        for level in levels.LEVEL_IDS:
            path = self._level_json_path(level)
            if not path.is_file():
                continue
            try:
                with path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            id_to_name = {
                image.get("id"): os.path.basename(str(image.get("file_name", "")))
                for image in data.get("images", [])
            }
            store: dict[str, list] = {}
            for annotation in data.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                name = id_to_name.get(annotation.get("image_id")) or os.path.basename(str(annotation.get("file_name", "")))
                if not name:
                    continue
                store.setdefault(name, []).append(annotation)
            self.annotation_store[level] = store

    def _current_image_basename(self) -> str:
        return os.path.basename(self.current_image_name) if self.current_image_name else ""

    def _current_level_annotations(self) -> list:
        """The active level's annotation list for the current image (created on demand)."""
        basename = self._current_image_basename()
        if not basename:
            return []
        return self.annotation_store[self.annotation_level].setdefault(basename, [])

    def _open_dataset_for_annotation(self) -> None:
        """Convert an existing COCO dataset folder into the 3 per-level annotation stores."""
        initial_directory = self.last_opened_directory or str(Path.cwd())
        folder = QFileDialog.getExistingDirectory(self, "Select a dataset folder to convert for annotation", initial_directory)
        if not folder:
            return
        try:
            images_path, annotations_path = find_coco_paths(folder)
            images, _images_by_id, annotations_by_image_id, categories_by_id = load_coco_data(annotations_path, images_path)
        except Exception as error:
            self._show_message(f"Could not load dataset: {error}")
            return
        self._save_last_opened_directory(folder)
        self._apply_converted_dataset(folder, images_path, images, annotations_by_image_id, categories_by_id)

    @staticmethod
    def _convert_source_annotation(annotation: dict, level: int, class_name: str) -> dict | None:
        """Map one source COCO annotation into our per-level annotation shape."""
        category_id = levels.category_id_for_class(level, class_name)
        if category_id is None:
            return None
        out: dict = {"category_id": category_id, "iscrowd": annotation.get("iscrowd", 0)}
        is_box_level = levels.level_geometry(level) == levels.GEOMETRY_ROTATED_BBOX
        segmentation = annotation.get("segmentation")
        bbox = annotation.get("bbox")
        if isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list) and len(segmentation[0]) >= 6:
            contour = segmentation[0]
            points = [(contour[i], contour[i + 1]) for i in range(0, len(contour) - 1, 2)]
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            envelope = list(bbox[:4]) if (bbox and len(bbox) >= 4) else [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
            if is_box_level:
                # A polygon class remapped to the box level becomes its envelope.
                x, y, width, height = (float(v) for v in envelope[:4])
                out["bbox"] = [x, y, width, height]
                out["area"] = width * height
                out["rotation"] = 0.0
                out["segmentation"] = [[x, y, x + width, y, x + width, y + height, x, y + height]]
                return out
            out["segmentation"] = [list(contour)]
            out["bbox"] = envelope
            out["area"] = annotation.get("area") or polygon_area(points)
            out["rotation"] = float(annotation.get("rotation", 0) or 0)
            return out
        if bbox and len(bbox) >= 4:
            x, y, width, height = (float(v) for v in bbox[:4])
            out["bbox"] = [x, y, width, height]
            out["area"] = annotation.get("area") or (width * height)
            out["rotation"] = float(annotation.get("rotation", 0) or 0)
            out["segmentation"] = [[x, y, x + width, y, x + width, y + height, x, y + height]]
            return out
        return None  # RLE-only / geometry-less annotations are skipped

    def _apply_converted_dataset(self, root: str, images_path: str, images: list, annotations_by_image_id: dict, categories_by_id: dict) -> None:
        self._cleanup_temp_extraction()
        self._rle_polygon_cache.clear()
        self._dirty_levels.clear()
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.dataset = {"annotation": True}
        self.temp_extraction = None
        self.annotation_root = os.path.abspath(root)
        self.images_path = images_path
        self.images = sorted(images, key=lambda image_data: image_data.get("id", 0))
        self.categories_by_id = {}
        self.class_items = []
        self.current_category_ids = []
        self.current_class_items = []
        self.index = 0
        self.annotation_store = {lvl: {} for lvl in levels.LEVEL_IDS}

        id_to_name = {image.get("id"): os.path.basename(str(image.get("file_name", ""))) for image in images}
        converted = 0
        dropped = 0
        for image_id, annotations in annotations_by_image_id.items():
            basename = id_to_name.get(image_id)
            if not basename:
                continue
            for annotation in annotations:
                class_name = categories_by_id.get(annotation.get("category_id"))
                level = levels.level_for_class(str(class_name)) if class_name is not None else None
                if level is None:
                    dropped += 1
                    continue
                new_annotation = self._convert_source_annotation(annotation, level, str(class_name))
                if new_annotation is None:
                    dropped += 1
                    continue
                self.annotation_store[level].setdefault(basename, []).append(new_annotation)
                self._dirty_levels.add(level)
                converted += 1

        self._dirty = bool(self._dirty_levels)
        self._populate_image_selector()
        self._load_current_image(reset_fit=True)
        self._update_status_labels()
        self._update_action_buttons()
        self._sync_canvas_state()
        self._sync_draw_state()
        message = f"Converted {converted} annotation(s) into the 3 levels."
        if dropped:
            message += f"\n{dropped} skipped (class not in any level, or RLE-only)."
        message += "\nSave / Save all to write the level JSON files."
        self._show_message(message)

    def _populate_image_selector(self) -> None:
        """Fill the image selector combo box with all available images."""
        if not hasattr(self, 'image_selector') or self.image_selector is None:
            return
        
        self.image_selector.blockSignals(True)
        self.image_selector.clear()
        
        # Images that still carry an off-catalog object awaiting redefinition;
        # their rows are coloured red so they're easy to find. Match by
        # stable_image_id (the entry's file_name can differ from the registry's
        # across re-exports) -- the same identity rule import/routing use.
        redefine_ids: set[int] = set()
        if self.project is not None:
            for entry in self.project.read_unmapped():
                try:
                    redefine_ids.add(int(entry.get("stable_image_id")))
                except (TypeError, ValueError):
                    redefine_ids.add(stable_image_id(os.path.basename(str(entry.get("file_name", "")))))

        # Add all images to the selector, each prefixed with its position (matching
        # the "N/total" counter in the status bar) so the list is easy to scan.
        if self.images:
            total = len(self.images)
            for i, image_info in enumerate(self.images):
                if isinstance(image_info, dict):
                    full_path = image_info.get("file_name", f"Image {i+1}")
                    # Extract only the filename without the path
                    filename = os.path.basename(str(full_path))
                else:
                    filename = f"Image {i+1}"
                self.image_selector.addItem(f"{i + 1}/{total}  {filename}", i)
                sid = image_info.get("id") if isinstance(image_info, dict) else None
                if sid is None:
                    sid = stable_image_id(filename)
                if int(sid) in redefine_ids:
                    self.image_selector.setItemData(i, True, RedefineRowDelegate.FLAG_ROLE)
            
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
        # Project sessions (either mode) never auto-close; stop at the last image.
        if (self.mode == "annotation" or self.project is not None) and self.index >= len(self.images) - 1:
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
        self._dirty_levels.clear()
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

    def _build_annotation_overlay_items(self, annotations, level: int, respect_visibility: bool = False) -> list[dict]:
        """Overlays for the active level (other levels hidden).

        ``respect_visibility`` skips classes hidden via the validation toggles;
        annotation mode passes False (it always shows the whole active level).
        """
        geometry = levels.level_geometry(level)
        class_colors = levels.level_class_colors(level)
        overlay_items: list[dict] = []
        for annotation in annotations:
            category_id = annotation.get("category_id")
            if respect_visibility and not self.visible_by_category.get(category_id, True):
                continue
            class_name = levels.class_name_for_category(level, category_id) if category_id is not None else None
            label = str(class_name) if class_name else str(category_id)
            color = color_for_class(str(class_name), int(category_id) if category_id is not None else 0, class_colors.get(str(class_name)))
            color_rgb = (color.red(), color.green(), color.blue())

            rotation = float(annotation.get("rotation", 0) or 0)
            if geometry == levels.GEOMETRY_ROTATED_BBOX and rotation == 0:
                bbox = annotation.get("bbox") or []
                if len(bbox) < 4:
                    continue
                x, y, width, height = (float(v) for v in bbox[:4])
                points = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
                overlay_items.append({
                    "shape": "bbox", "points": points, "center": (x + width / 2.0, y + height / 2.0),
                    "label": label, "color": color_rgb, "annotation": annotation, "editable": True,
                })
                continue

            segmentation = annotation.get("segmentation")
            if not (isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list) and len(segmentation[0]) >= 6):
                continue
            segment = segmentation[0]
            points = [(float(segment[i]), float(segment[i + 1])) for i in range(0, len(segment) - 1, 2)]
            overlay_items.append({
                "shape": "polygon", "points": points, "center": polygon_centroid(points),
                "label": label, "color": color_rgb, "annotation": annotation, "seg_index": 0, "editable": True,
            })
        return overlay_items

    @staticmethod
    def _raw_geometry_points(annotation: dict) -> list[tuple[float, float]]:
        """Extract polygon/box points from a raw (unconverted) annotation, or []."""
        segmentation = annotation.get("segmentation")
        if isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list) and len(segmentation[0]) >= 6:
            segment = segmentation[0]
            return [(float(segment[i]), float(segment[i + 1])) for i in range(0, len(segment) - 1, 2)]
        bbox = annotation.get("bbox")
        if bbox and len(bbox) >= 4:
            x, y, width, height = (float(v) for v in bbox[:4])
            return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
        return []

    def _pending_redefine_overlays(self, level: int) -> list[dict]:
        """Highlighted, read-only overlays for unredefined classes shown on ``level``.

        Includes entries parked in this level AND not-yet-parked entries (which
        show on every level until assigned), so a redefinable object is visible
        regardless of the active level until the user picks its real one.
        """
        if self.project is None:
            return []
        basename = self._current_image_basename()
        if not basename:
            return []
        overlays: list[dict] = []
        for entry in self.project.unmapped_entries_visible_on_level(level):
            if os.path.basename(str(entry.get("file_name", ""))) != basename:
                continue
            points = self._raw_geometry_points(entry.get("source_annotation", {}) or {})
            if len(points) < 2:
                continue
            overlays.append({
                "shape": "polygon",
                "points": points,
                "center": polygon_centroid(points),
                "label": f"{entry.get('raw_class', '?')} • redefine",
                "color": (255, 92, 196),  # distinct magenta = needs redefinition
                "annotation": None,
                "editable": False,
                "pending_redefine": True,
                "redefine_entry_id": entry.get("id"),  # selectable -> per-object resolve
                "redefine_raw_class": str(entry.get("raw_class", "")),
            })
        return overlays

    def _refresh_overlay_items(self) -> None:
        if self.mode == "annotation":
            annotations = self._current_level_annotations()
            self.current_annotations = annotations
            overlays = self._build_annotation_overlay_items(annotations, self.annotation_level)
            overlays += self._pending_redefine_overlays(self.annotation_level)
            self.current_overlay_items = overlays
            self.canvas.set_overlay_items(self.current_overlay_items)
            self.canvas.update()
            self._update_status_labels()
            return
        if self.project is not None:
            # Validation on a project shares annotation mode's per-level store, but
            # honours the per-class visibility toggles (and still shows parked
            # "redefine" overlays so they're visible in this lens too).
            annotations = self._current_level_annotations()
            self.current_annotations = annotations
            overlays = self._build_annotation_overlay_items(annotations, self.annotation_level, respect_visibility=True)
            overlays += self._pending_redefine_overlays(self.annotation_level)
            self.current_overlay_items = overlays
            self.canvas.set_overlay_items(self.current_overlay_items)
            self.canvas.update()
            self._update_status_labels()
            return
        if not self.dataset:
            return
        self.current_overlay_items = self._build_overlay_items(self.current_annotations)
        self.canvas.set_overlay_items(self.current_overlay_items)
        self.canvas.update()
        self._update_status_labels()

    def _mark_dirty_level(self, level: int | None = None) -> None:
        """Flag a level (default: the active one) as having unsaved edits."""
        self._dirty_levels.add(self.annotation_level if level is None else int(level))
        self._dirty = True

    def _unsaved_suffix(self) -> str:
        """Status-bar tail naming the dirty levels (project) or a generic note."""
        if not self._dirty:
            return ""
        if self.project is not None and self._dirty_levels:
            return "   ·   ● unsaved: " + ", ".join(f"L{n}" for n in sorted(self._dirty_levels))
        return "   ·   ● unsaved edits"

    def _on_annotations_changed(self, record: dict | None = None) -> None:
        """A vertex was dragged on the canvas; record the edit for undo and refresh."""
        if record is not None:
            record["image_index"] = self.index
            record.setdefault("level", self.annotation_level)
            self._undo_stack.append(record)
            self._redo_stack.clear()
            self._mark_dirty_level(record["level"])
        self._update_action_buttons()
        self._update_status_labels()

    def _make_annotation(self, level: int, category_id: int, payload: dict) -> dict | None:
        """Build an annotation dict for the active level from a drawn-shape payload."""
        shape = payload.get("shape")
        points = [(float(x), float(y)) for x, y in payload.get("points", [])]
        if shape == "bbox":
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            x, y = min(xs), min(ys)
            width, height = max(xs) - x, max(ys) - y
            if width < 1 or height < 1:
                return None
            quad = [x, y, x + width, y, x + width, y + height, x, y + height]
            return {
                "category_id": category_id,
                "bbox": [x, y, width, height],
                "area": width * height,
                "segmentation": [quad],
                "rotation": 0,
                "iscrowd": 0,
            }
        if shape == "obbox":
            if len(points) < 4:
                return None
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            x, y = min(xs), min(ys)
            width, height = max(xs) - x, max(ys) - y
            if width < 1 and height < 1:
                return None
            return {
                "category_id": category_id,
                "segmentation": [[coord for point in points for coord in point]],
                "bbox": [x, y, width, height],
                "area": polygon_area(points),
                "rotation": float(payload.get("rotation", 0) or 0),
                "iscrowd": 0,
            }
        # polygon
        if len(points) < 3:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        x, y = min(xs), min(ys)
        width, height = max(xs) - x, max(ys) - y
        return {
            "category_id": category_id,
            "segmentation": [[coord for point in points for coord in point]],
            "bbox": [x, y, width, height],
            "area": polygon_area(points),
            "iscrowd": 0,
        }

    def _on_annotation_created(self, payload: dict) -> None:
        """Canvas finished a new shape: attach the active class and store it (undoable)."""
        if self.mode != "annotation":
            return
        active = self._active_category()
        if active is None:
            return
        annotation = self._make_annotation(self.annotation_level, active, payload)
        if annotation is None:
            return
        annotations = self._current_level_annotations()
        annotations.append(annotation)
        record = {
            "kind": "create",
            "level": self.annotation_level,
            "basename": self._current_image_basename(),
            "annotation": annotation,
            "image_index": self.index,
        }
        self._undo_stack.append(record)
        self._redo_stack.clear()
        self._mark_dirty_level(self.annotation_level)
        self._refresh_overlay_items()
        self._update_action_buttons()
        self._update_status_labels()

    def _on_selection_changed(self) -> None:
        # A selected parked-redefine object swaps the sidebar to the "assign a
        # class" catalog panel; any other selection restores the normal class list.
        redefine_item = self.canvas.selected_redefine()
        if redefine_item is not None:
            self._show_redefine_assign_panel(redefine_item)
            self.delete_button.setEnabled(False)
            return
        if self._redefine_panel_active:
            self._redefine_panel_active = False
            if self.project is not None and self.mode == "validation":
                self._set_validation_level_class_items()
            self._populate_class_checkboxes()
        can_delete = self.mode == "annotation" or self.project is not None
        self.delete_button.setEnabled(can_delete and self.canvas.selected_annotation() is not None)

    def _show_redefine_assign_panel(self, item: dict) -> None:
        """Show the active level's catalog so a click assigns the selected redefine object."""
        entry_id = item.get("redefine_entry_id")
        level = self.annotation_level
        self._redefine_panel_active = True
        self._clear_class_checkboxes()
        raw = str(item.get("redefine_raw_class") or str(item.get("label", "")).split(" • ")[0])
        self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, self._make_section(f"ASSIGN “{raw}”"))
        note = QLabel("Click a class to reclassify this object (it leaves the redefine list).")
        note.setObjectName("muted")
        note.setWordWrap(True)
        self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, note)
        class_colors = levels.level_class_colors(level)
        handle_height = max(30, int(round(30 * DPI_SCALE)))
        for header, class_list in levels.level_groups(level):
            self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, self._make_section(header))
            for class_name, default_hex in class_list:
                category_id = levels.category_id_for_class(level, class_name) or 0
                color = color_for_class(class_name, category_id, class_colors.get(class_name, default_hex))
                button = OutlinedTextButton(class_name)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setMinimumHeight(handle_height)
                button.setStyleSheet(
                    f"QPushButton {{ background-color: rgba({color.red()},{color.green()},{color.blue()},235);"
                    " border:1px solid rgba(0,0,0,0.22); border-radius:14px;"
                    " padding:7px 12px; text-align:left; font-weight:600; }"
                    " QPushButton:hover { border:1px solid rgba(0,0,0,0.45); }"
                )
                button.clicked.connect(
                    lambda _checked=False, eid=entry_id, name=class_name: self._resolve_redefine_object(eid, name)
                )
                self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, button)

    def _resolve_redefine_object(self, entry_id, class_name: str) -> None:
        """Reclassify ONE parked redefine object into ``class_name`` on the active level."""
        if self.project is None or not entry_id:
            return
        level = self.annotation_level
        entry = self.project.get_unmapped_entry(entry_id)
        if entry is None:  # already resolved elsewhere
            self.canvas.clear_selection()
            return
        converted = self._convert_source_annotation(entry.get("source_annotation", {}) or {}, level, class_name)
        if converted is None:
            self._show_message("Could not assign: this object has no usable geometry for this level.")
            return
        basename = os.path.basename(str(entry.get("file_name", ""))) or self._current_image_basename()
        store = self.annotation_store[level].setdefault(basename, [])
        store.append(converted)
        ok, _count, _name = self._write_level_file(level)  # commit this level to disk
        if not ok:
            store.remove(converted)  # rollback the in-memory add on write failure
            return
        self.project.pop_unmapped_entry(entry_id)  # commit: drop it from the stash
        self._dirty_levels.discard(level)  # the write persisted this level
        self._dirty = bool(self._dirty_levels)
        self.canvas.clear_selection()  # restores the normal sidebar (rebuilds class list)
        self._populate_image_selector()  # clears this image's red row if nothing's left
        self._refresh_overlay_items()
        self._update_action_buttons()
        self._update_status_labels()
        self._update_redefine_action()

    def _delete_selected_annotation(self) -> bool:
        """Remove the selected annotation from the active level's store (undoable)."""
        if self.mode != "annotation" and self.project is None:
            return False
        annotation = self.canvas.selected_annotation()
        if annotation is None:
            return False
        annotations = self._current_level_annotations()
        if not any(item is annotation for item in annotations):
            return False
        annotations[:] = [item for item in annotations if item is not annotation]
        record = {
            "kind": "delete",
            "level": self.annotation_level,
            "basename": self._current_image_basename(),
            "annotation": annotation,
            "image_index": self.index,
        }
        self._undo_stack.append(record)
        self._redo_stack.clear()
        self._mark_dirty_level(record["level"])
        self.canvas.clear_selection()
        self._refresh_overlay_items()
        self._update_action_buttons()
        self._update_status_labels()
        return True

    def _update_action_buttons(self) -> None:
        self.undo_button.setEnabled(bool(self._undo_stack))
        self.redo_button.setEnabled(bool(self._redo_stack))
        has_dataset = self.dataset is not None
        is_annotation = self.mode == "annotation"
        # In a project both modes save per-level (current level / all levels).
        per_level = is_annotation or self.project is not None
        self.save_button.setEnabled(has_dataset)
        self.save_all_button.setEnabled(has_dataset and per_level)
        save_label = "● Save level" if (has_dataset and self._dirty) else ("Save level" if per_level else "Save")
        self.save_button.setText(save_label)

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

    def _apply_record(self, record: dict, undo: bool) -> None:
        """Apply one undo/redo record: geometry snapshot, or add/remove an annotation."""
        kind = record.get("kind", "edit")
        if kind not in ("create", "delete"):
            self._restore_snapshot(record, "before" if undo else "after")
            return

        target_index = record.get("image_index", self.index)
        if self.images and 0 <= target_index < len(self.images) and target_index != self.index:
            self.index = target_index
            self._load_current_image(reset_fit=True)
        annotations = self.annotation_store[record["level"]].setdefault(record["basename"], [])
        annotation = record["annotation"]
        should_have = (undo and kind == "delete") or (not undo and kind == "create")
        present = any(item is annotation for item in annotations)
        if should_have and not present:
            annotations.append(annotation)
        elif not should_have and present:
            annotations[:] = [item for item in annotations if item is not annotation]
        self._refresh_overlay_items()

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        record = self._undo_stack.pop()
        self._redo_stack.append(record)
        self._apply_record(record, undo=True)
        # An undo changes the level's data vs disk, so it stays dirty until saved.
        self._mark_dirty_level(record.get("level"))
        self._update_action_buttons()
        self._update_status_labels()

    def _redo(self) -> None:
        if not self._redo_stack:
            return
        record = self._redo_stack.pop()
        self._undo_stack.append(record)
        self._apply_record(record, undo=False)
        self._mark_dirty_level(record.get("level"))
        self._update_action_buttons()
        self._update_status_labels()

    @staticmethod
    def _read_image_size(path: str) -> tuple[int | None, int | None]:
        reader = QImageReader(path)
        size = reader.size()
        if size.isValid():
            return size.width(), size.height()
        return None, None

    def _finalize_annotation_for_save(self, annotation: dict, level: int) -> dict:
        """Copy + normalize an annotation for COCO output (ensure box segmentation/area)."""
        out = copy.deepcopy(annotation)
        if levels.level_geometry(level) == levels.GEOMETRY_ROTATED_BBOX:
            bbox = out.get("bbox") or []
            rotation = float(out.get("rotation", 0) or 0)
            segmentation = out.get("segmentation")
            has_quad = isinstance(segmentation, list) and segmentation and isinstance(segmentation[0], list) and len(segmentation[0]) >= 8
            if len(bbox) >= 4 and (rotation == 0 or not has_quad):
                x, y, width, height = (float(v) for v in bbox[:4])
                out["segmentation"] = [[x, y, x + width, y, x + width, y + height, x, y + height]]
                if rotation == 0:
                    out["area"] = width * height
        out.setdefault("iscrowd", 0)
        return out

    def _build_level_coco(self, level: int) -> dict:
        """Assemble a COCO dict for one level from its in-memory store."""
        store = self.annotation_store[level]
        images_out: list[dict] = []
        annotations_out: list[dict] = []
        annotation_id = 1
        for image_info in self.images:
            file_name = image_info.get("file_name", "")
            basename = os.path.basename(str(file_name))
            width = image_info.get("width")
            height = image_info.get("height")
            if not width or not height:
                width, height = self._read_image_size(resolve_image_path(self.images_path, file_name))
                if width and height:
                    image_info["width"], image_info["height"] = width, height
            image_id = image_info.get("id")
            images_out.append({"id": image_id, "file_name": file_name, "width": width, "height": height})
            for annotation in store.get(basename, []):
                out = self._finalize_annotation_for_save(annotation, level)
                out["id"] = annotation_id
                out["image_id"] = image_id
                annotations_out.append(out)
                annotation_id += 1
        return {
            "images": images_out,
            "categories": levels.level_categories(level),
            "annotations": annotations_out,
            "level": level,
        }

    def _write_level_file(self, level: int) -> tuple[bool, int, str]:
        """Write level{N}.json next to the images. Returns (ok, annotation_count, name)."""
        coco = self._build_level_coco(level)
        path = self._level_json_path(level)
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(coco, handle, ensure_ascii=False)
        except OSError as error:
            self._show_message(f"Could not save {path.name}: {error}")
            return False, 0, path.name
        return True, len(coco["annotations"]), path.name

    def _persist_all_levels(self) -> tuple[bool, int, list[str]]:
        """Write all three level files (no popup); clear dirty state. (ok, total, names)."""
        total = 0
        names: list[str] = []
        for level in levels.LEVEL_IDS:
            ok, count, name = self._write_level_file(level)
            if not ok:
                return False, total, names
            total += count
            names.append(name)
        self._dirty_levels.clear()
        self._dirty = False
        self._update_action_buttons()
        self._update_status_labels()
        return True, total, names

    def _save_all_levels(self) -> None:
        if not self.dataset or not self.annotation_root:
            return
        if self.mode != "annotation" and self.project is None:
            return
        ok, total, names = self._persist_all_levels()
        if not ok:
            return
        self._show_message(f"Saved {total} annotations to:\n{', '.join(names)}")

    def _save_dataset(self) -> None:
        """Write the current annotations to disk (per-level JSON in annotation mode)."""
        if not self.dataset:
            return

        if self.mode == "annotation" or self.project is not None:
            # Both project modes save the active level back to its level{N}.json.
            if not self.annotation_root:
                return
            ok, count, name = self._write_level_file(self.annotation_level)
            if not ok:
                return
            # Saving one level clears only that level's dirty flag.
            self._dirty_levels.discard(self.annotation_level)
            self._dirty = bool(self._dirty_levels)
            self._update_action_buttons()
            self._update_status_labels()
            self._show_message(f"Saved {count} annotations to {name}")
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

        self._dirty_levels.clear()
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

    def _load_current_image_annotation(self, reset_fit: bool) -> None:
        """Load the current image for an annotation session (per-level overlays)."""
        failed_count = 0
        while True:
            if self.index >= len(self.images):
                # Annotation sessions never auto-close; clamp to the last image.
                self.index = max(0, len(self.images) - 1)
                if failed_count:
                    self._show_message(self._unreadable_message(failed_count))
                return
            image_info = self.images[self.index]
            image_name = str(image_info.get("file_name", ""))
            image_path = resolve_image_path(self.images_path, image_name)
            if self.canvas.load_image(image_path, ""):
                self.current_image_name = image_name
                self.current_image_path = image_path
                width, height = self.canvas._base_image_size()
                if width and height:
                    image_info["width"] = width
                    image_info["height"] = height
                break
            failed_count += 1
            self.index += 1

        self.canvas.cancel_drawing()
        self.canvas.clear_selection()
        title = f"{self.index + 1}/{len(self.images)} - {os.path.basename(self.current_image_name)}"
        self.setWindowTitle(title)
        self._sync_canvas_state()
        # Validation sidebar lists only the classes present on THIS image (rebuilt
        # per image, like plain validation), coloured from the level catalog.
        if self.project is not None and self.mode == "validation":
            self._set_validation_level_class_items()
            self._populate_class_checkboxes()
        self._refresh_overlay_items()
        if reset_fit:
            self.canvas.fit_to_view()
        self._update_status_labels()
        if failed_count:
            self._show_message(self._unreadable_message(failed_count))

    def _load_current_image(self, reset_fit: bool) -> None:
        if not self.dataset or not self.images:
            self.canvas.set_idle()
            return

        if self.mode == "annotation" or self.project is not None:
            # Both project modes share the per-level loader (clamp at the last
            # image, never auto-close — closing would discard the project session).
            self._load_current_image_annotation(reset_fit)
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
        self._refresh_canvas_placeholder()
        if self.mode == "annotation":
            active = self._active_category()
            active_name = levels.class_name_for_category(self.annotation_level, active) if active is not None else None
            parts = [
                "Annotate",
                f"Level {self.annotation_level} · {levels.level_title(self.annotation_level)}",
            ]
            if self.images:
                parts.insert(1, f"{self.index + 1}/{len(self.images)}")
            parts.append(f"class: {active_name}" if active_name else "class: none")
            text = "   ·   ".join(parts)
            text += self._unsaved_suffix()
            self.status_label.setText(text)
            self.copy_id_button.setEnabled(bool(self.images))
            return
        if not self.dataset:
            self.status_label.setText("No images yet — use the Import menu to add some")
            self.copy_id_button.setEnabled(False)
            return

        self.copy_id_button.setEnabled(True)
        zoom = int(round(self.canvas.zoom * 100))

        # On a project, validation reviews one level; show it and the level's
        # catalog size rather than the (unused) flat ``class_items`` count.
        if self.project is not None:
            level_part = f"L{self.annotation_level} · {levels.level_title(self.annotation_level)}"
            class_count = len(self.current_class_items)  # classes present on this image
        else:
            level_part = None
            class_count = len(self.class_items)

        if self.images:
            current_name = os.path.basename(self.current_image_name) if self.current_image_name else ""
            visible_count = len(self.current_overlay_items)
            parts = [
                f"{self.index + 1}/{len(self.images)}",
                current_name,
                level_part,
                f"{class_count} classes",
                f"{visible_count} visible",
                f"Zoom {zoom}%",
            ]
        else:
            parts = [level_part, f"{len(self.images)} images", f"{class_count} classes", f"Zoom {zoom}%"]

        text = "   ·   ".join(part for part in parts if part)
        text += self._unsaved_suffix()
        self.status_label.setText(text)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # While drawing, Enter finishes a polygon, Esc cancels, Backspace drops a vertex.
        # These must beat the global shortcuts (Enter=next image, Esc=quit).
        if self.mode == "annotation" and self.canvas.has_pending_drawing():
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.canvas.finish_polygon()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self.canvas.cancel_drawing()
                event.accept()
                return
            if key == Qt.Key.Key_Backspace:
                self.canvas.remove_last_point()
                event.accept()
                return

        # Delete/Backspace removes the selected annotation (when not mid-drawing).
        if (self.mode == "annotation" or self.project is not None) and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._delete_selected_annotation():
                event.accept()
                return

        seq = QKeySequence(event.keyCombination()).toString()
        action_id = self._shortcut_index.get(seq)
        if action_id is not None:
            self._shortcut_callbacks[action_id]()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._confirm_discard_unsaved("quit"):
            event.ignore()
            return
        self._cleanup_temp_extraction()
        for snapshot in self._sessions.values():
            stashed = snapshot.get("temp_extraction")
            if stashed is not None:
                try:
                    stashed.cleanup()
                except Exception:
                    pass
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

    from PyQt6.QtCore import QTimer

    window = PyQtAnnotationReview(args.input_path)
    # Remember a sensible restore (un-maximized) size, then show normally and
    # maximize on the next event-loop tick. Calling showMaximized() before the
    # window is realized leaves it flagged maximized but sized to the resize()
    # on some Windows/DPI setups; deferring it makes the maximize actually take.
    window.resize(int(round(MONITOR_WIDTH * 0.9)), int(round(MONITOR_HEIGHT * 0.87)))
    window.show()
    QTimer.singleShot(0, window.showMaximized)

    sys.exit(app.exec())
