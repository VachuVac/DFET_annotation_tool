"""PyQt-based main application for annotation review."""

from __future__ import annotations

import colorsys
import contextlib
import copy
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

from .constants import DPI_SCALE, MONITOR_HEIGHT, MONITOR_WIDTH, SIDEBAR_WIDTH
from .data_loading import (
    discover_images_dir,
    load_coco_data,
    resolve_dataset_paths,
    resolve_image_path,
    stable_image_id,
)
from .utils import parse_arguments
from . import levels
from .project import Project, ProjectError

try:
    from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
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
        QTextEdit,
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
        # A clicked (not dragged) polygon vertex stays SELECTED so Delete/Backspace
        # can remove it. Polygons only (L1/L3) — bboxes/oriented quads keep 4 corners.
        self._selected_vertex: tuple[dict, int] | None = None
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
        # Live editing of an in-progress polygon's points (same scheme as committed
        # vertices): grab one to drag, click to select, Delete to remove.
        self._drag_pending_index: int | None = None
        self._pending_moved = False
        self._selected_pending_index: int | None = None
        # Selection (annotation mode): click an annotation body to select; Delete removes it.
        self._select_enabled = False
        self._selected_annotation: dict | None = None
        # A parked "redefine" overlay can also be selected (to assign it a class).
        self._selected_redefine: dict | None = None
        # Rotating a selected L2 box via its rotate handle (drag around the center).
        self._rotate_item: dict | None = None
        self._rotate_before: dict | None = None
        self._rotate_center: tuple[float, float] | None = None
        self._rotate_origin_points: list[tuple[float, float]] = []
        self._rotate_start_angle = 0.0
        self._rotate_base_rotation = 0.0
        self._rotate_moved = False
        self._rotation_hover = False
        # Moving a whole selected L2 box by dragging its body (boxes only, not polygons).
        self._box_move_item: dict | None = None
        self._box_move_before: dict | None = None
        self._box_move_origin_points: list[tuple[float, float]] = []
        self._box_move_origin_center: tuple[float, float] | None = None
        self._box_move_start = QPoint(0, 0)
        self._box_move_start_image: tuple[float, float] = (0.0, 0.0)
        self._box_move_moved = False
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
        # The old overlay dicts are gone; drop any handle refs into them.
        self._selected_vertex = None
        self._hover_vertex = None
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
        # Draw ONLY the visible portion of the image, scaled during the blit. The
        # old code re-scaled the WHOLE pixmap to the zoomed size every frame, which
        # at high zoom builds a pixmap of hundreds of millions of pixels per repaint
        # — the cause of zoom-in lag. Clipping the source to the viewport keeps the
        # work proportional to the widget, not the zoom level.
        zoom = self._zoom if self._zoom > 0 else 1.0
        src_left = max(0.0, -image_x / zoom)
        src_top = max(0.0, -image_y / zoom)
        src_right = min(float(self._pixmap.width()), (self.width() - image_x) / zoom + 1.0)
        src_bottom = min(float(self._pixmap.height()), (self.height() - image_y) / zoom + 1.0)
        if src_right > src_left and src_bottom > src_top:
            source = QRectF(src_left, src_top, src_right - src_left, src_bottom - src_top)
            target = QRectF(
                image_x + src_left * zoom,
                image_y + src_top * zoom,
                (src_right - src_left) * zoom,
                (src_bottom - src_top) * zoom,
            )
            painter.drawPixmap(target, self._pixmap, source)
        self._draw_overlays(painter, image_x, image_y)
        self._draw_handles(painter, image_x, image_y)
        self._draw_rotation_handle(painter, image_x, image_y)
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
            self._selected_vertex = None
            self._rotate_item = None
            self._rotate_center = None
            self._rotate_origin_points = []
            self._rotate_moved = False
            self._rotation_hover = False
            self._box_move_item = None
            self._box_move_before = None
            self._box_move_origin_points = []
            self._box_move_origin_center = None
            self._box_move_moved = False
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
        self._drag_pending_index = None
        self._pending_moved = False
        self._selected_pending_index = None
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
            self._drag_pending_index = None
            self._pending_moved = False
            self._selected_pending_index = None
            self.update()
            self.annotationCreated.emit({"shape": "polygon", "points": points})

    def _hit_test_pending_vertex(self, position) -> int | None:
        """Index of the in-progress polygon point under the cursor, if any."""
        if not self._poly_points or self._pixmap is None:
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        cursor_x, cursor_y = position.x(), position.y()
        best_index: int | None = None
        best_distance = float(self._handle_hit_radius) ** 2
        for index, (point_x, point_y) in enumerate(self._poly_points):
            screen_x = image_x + point_x * self._zoom
            screen_y = image_y + point_y * self._zoom
            distance = (screen_x - cursor_x) ** 2 + (screen_y - cursor_y) ** 2
            if distance <= best_distance:
                best_distance = distance
                best_index = index
        return best_index

    def delete_selected_pending_vertex(self) -> bool:
        """Remove the selected in-progress polygon point (no minimum); True if done."""
        index = self._selected_pending_index
        if index is None or not (0 <= index < len(self._poly_points)):
            return False
        del self._poly_points[index]
        self._selected_pending_index = None
        self.update()
        return True

    def _insert_pending_vertex_at(self, position) -> bool:
        """Insert a point on the nearest edge of the in-progress polyline."""
        if self._pixmap is None or len(self._poly_points) < 2:
            return False
        image_x, image_y, _, _ = self._fit_display_rect()
        px, py = position.x(), position.y()
        best_index: int | None = None
        best_point: tuple[float, float] | None = None
        best_distance = float(max(20, self._handle_hit_radius * 2)) ** 2
        # Open polyline: edges join consecutive points only (no closing edge yet).
        for i in range(len(self._poly_points) - 1):
            ax, ay = self._poly_points[i]
            bx, by = self._poly_points[i + 1]
            sax, say = image_x + ax * self._zoom, image_y + ay * self._zoom
            sbx, sby = image_x + bx * self._zoom, image_y + by * self._zoom
            dx, dy = sbx - sax, sby - say
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else ((px - sax) * dx + (py - say) * dy) / length_sq
            t = min(1.0, max(0.0, t))
            distance = (sax + t * dx - px) ** 2 + (say + t * dy - py) ** 2
            if distance < best_distance:
                best_distance = distance
                best_index = i + 1
                best_point = (ax + t * (bx - ax), ay + t * (by - ay))
        if best_index is None or best_point is None:
            return False
        self._poly_points.insert(best_index, best_point)
        self._selected_pending_index = best_index
        self.update()
        return True

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

    def selected_vertex(self) -> tuple[dict, int] | None:
        return self._selected_vertex

    def clear_selection(self) -> None:
        self._set_selected(None)  # also clears any redefine selection

    def _set_selected(self, annotation: dict | None) -> None:
        changed = annotation is not self._selected_annotation or self._selected_redefine is not None
        self._selected_annotation = annotation
        self._selected_redefine = None
        self._selected_vertex = None  # body selection is exclusive with vertex selection
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

    def _selected_overlay_item(self) -> dict | None:
        """The overlay item backing the currently selected annotation, if any."""
        if self._selected_annotation is None:
            return None
        for item in self._overlay_items:
            if item.get("annotation") is self._selected_annotation:
                return item
        return None

    def _hit_test_vertex(self, position) -> tuple[dict, int] | None:
        """Return (overlay_item, vertex_index) of the editable handle under the cursor, if any.

        Only the SELECTED object exposes grabbable handles (like the L2 rotate
        handle) — so editing one object's points never fights its neighbours'.
        """
        if not self._edit_enabled or self._pixmap is None:
            return None
        item = self._selected_overlay_item()
        if item is None or not item.get("editable"):
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        cursor_x, cursor_y = position.x(), position.y()
        best_hit: tuple[dict, int] | None = None
        best_distance = float(self._handle_hit_radius) ** 2
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
        elif item.get("rotatable"):
            # A rotated L2 box renders as a polygon, but dragging a corner must
            # still resize it as a *rectangle* about its (rotated) axes — never
            # warp it into an arbitrary quad like a real polygon.
            self._apply_box_vertex_drag(item, index, image_x, image_y)
        else:
            points = item["points"]
            if 0 <= index < len(points):
                points[index] = (image_x, image_y)
                item["center"] = polygon_centroid(points)
            if annotation is not None:
                self._write_polygon_vertex(annotation, int(item.get("seg_index", 0)), index, image_x, image_y)

    def _apply_box_vertex_drag(self, item: dict, index: int, image_x: float, image_y: float) -> None:
        """Resize a rotated box by dragging one corner, pinning the opposite one.

        The box keeps its orientation and stays a true rectangle: the dragged
        corner and the fixed (diagonal) corner define the new rectangle in the
        box's own rotated frame; the two adjacent corners follow.
        """
        points = item["points"]
        if len(points) != 4 or not (0 <= index < 4):
            return
        annotation = item.get("annotation")
        fixed = points[(index + 2) % 4]
        fx, fy = float(fixed[0]), float(fixed[1])

        # Box-local x-axis: along the first edge (P0->P1); fall back to the stored
        # rotation when that edge has collapsed to ~zero length.
        ax = points[1][0] - points[0][0]
        ay = points[1][1] - points[0][1]
        length = math.hypot(ax, ay)
        if length > 1e-6:
            ux, uy = ax / length, ay / length
        else:
            theta = math.radians(float(annotation.get("rotation", 0) or 0)) if annotation else 0.0
            ux, uy = math.cos(theta), math.sin(theta)
        vx, vy = -uy, ux  # perpendicular (box-local y-axis)

        # New dragged corner in the box frame, measured from the fixed corner.
        ddx, ddy = image_x - fx, image_y - fy
        new_u = ddx * ux + ddy * uy
        new_v = ddx * vx + ddy * vy

        new_points: list[tuple[float, float]] = [(0.0, 0.0)] * 4
        for j in range(4):
            rel_x, rel_y = points[j][0] - fx, points[j][1] - fy
            # Each corner shares either the fixed corner's coordinate (0) or the
            # dragged corner's (full span) along each box axis.
            share_u = abs(rel_x * ux + rel_y * uy) > 1e-6
            share_v = abs(rel_x * vx + rel_y * vy) > 1e-6
            cu = new_u if share_u else 0.0
            cv = new_v if share_v else 0.0
            new_points[j] = (fx + cu * ux + cv * vx, fy + cu * uy + cv * vy)

        item["points"] = new_points
        item["center"] = ((new_points[0][0] + new_points[2][0]) / 2.0,
                          (new_points[0][1] + new_points[2][1]) / 2.0)
        if annotation is not None:
            self._write_rotated_box(item, float(annotation.get("rotation", 0) or 0))

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
        ImageCanvas._recompute_seg_bounds(annotation)

    @staticmethod
    def _recompute_seg_bounds(annotation: dict) -> None:
        """Recompute bbox + area from an annotation's polygon segmentation (in place)."""
        segmentation = annotation.get("segmentation")
        if not isinstance(segmentation, list):
            return
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

    def _sync_polygon_geometry(self, item: dict) -> None:
        """Rewrite ``item``'s annotation segmentation from its (mutated) points list."""
        points = item["points"]
        item["center"] = polygon_centroid(points)
        annotation = item.get("annotation")
        if annotation is None:
            return
        seg_index = int(item.get("seg_index", 0))
        segmentation = annotation.get("segmentation")
        if isinstance(segmentation, list) and seg_index < len(segmentation):
            flat: list[float] = []
            for px, py in points:
                flat.extend([float(px), float(py)])
            segmentation[seg_index] = flat
            self._recompute_seg_bounds(annotation)

    def delete_selected_vertex(self) -> bool:
        """Remove the selected polygon vertex (guards ≥3 pts); emits an undo record.

        Returns False when there's no eligible vertex selected, so the caller can
        fall back to whole-object delete.
        """
        if self._selected_vertex is None:
            return False
        item, index = self._selected_vertex
        if not item.get("vertex_editable") or not any(item is x for x in self._overlay_items):
            return False
        points = item["points"]
        if not (0 <= index < len(points)) or len(points) <= 3:
            return False  # a polygon needs at least 3 vertices
        annotation = item.get("annotation")
        before = self._snapshot_annotation(annotation)
        del points[index]
        self._sync_polygon_geometry(item)
        self._selected_vertex = None
        self._hover_vertex = None
        self.update()
        if annotation is not None:
            after = self._snapshot_annotation(annotation)
            self.annotationsChanged.emit({"annotation": annotation, "before": before, "after": after})
        return True

    def insert_vertex_at(self, position) -> bool:
        """Insert a vertex on the nearest polygon edge near ``position``; undo record."""
        hit = self._nearest_polygon_edge(position)
        if hit is None:
            return False
        item, insert_index, point = hit
        annotation = item.get("annotation")
        before = self._snapshot_annotation(annotation)
        item["points"].insert(insert_index, point)
        self._sync_polygon_geometry(item)
        self._selected_vertex = (item, insert_index)  # newly added vertex is selected
        self.update()
        if annotation is not None:
            after = self._snapshot_annotation(annotation)
            self.annotationsChanged.emit({"annotation": annotation, "before": before, "after": after})
        return True

    def _nearest_polygon_edge(self, position) -> tuple[dict, int, tuple[float, float]] | None:
        """Closest editable-polygon edge to the cursor → (item, insert_index, point).

        ``insert_index`` is where the new point goes in ``item['points']`` (the
        closing edge appends at the end). ``point`` is in image coordinates. Only
        edges within a small screen-pixel threshold qualify.
        """
        if not self._edit_enabled or self._pixmap is None:
            return None
        # Insert points only on the SELECTED polygon (handles are selected-only).
        item = self._selected_overlay_item()
        if item is None or not item.get("vertex_editable"):
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        px, py = position.x(), position.y()
        threshold = float(max(20, self._handle_hit_radius * 2)) ** 2
        best: tuple[dict, int, tuple[float, float]] | None = None
        best_distance = threshold
        points = item["points"]
        n = len(points)
        if n < 2:
            return None
        for i in range(n):
            ax, ay = points[i]
            bx, by = points[(i + 1) % n]
            sax, say = image_x + ax * self._zoom, image_y + ay * self._zoom
            sbx, sby = image_x + bx * self._zoom, image_y + by * self._zoom
            dx, dy = sbx - sax, sby - say
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else ((px - sax) * dx + (py - say) * dy) / length_sq
            t = min(1.0, max(0.0, t))
            proj_x, proj_y = sax + t * dx, say + t * dy
            distance = (proj_x - px) ** 2 + (proj_y - py) ** 2
            if distance < best_distance:
                best_distance = distance
                point = (ax + t * (bx - ax), ay + t * (by - ay))
                best = (item, i + 1, point)
        return best

    # ----- rotation of selected L2 boxes ----------------------------------
    def _rotation_target(self) -> dict | None:
        """The selected overlay item if it's a rotatable L2 box in edit mode, else None."""
        if not self._edit_enabled or self._selected_annotation is None:
            return None
        for item in self._overlay_items:
            if item.get("rotatable") and item.get("annotation") is self._selected_annotation:
                return item
        return None

    def _rotation_handle_image_pos(self, item: dict) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """((handle_x, handle_y), (edge_mid_x, edge_mid_y)) in image coords, or None.

        The handle floats a fixed *screen* distance beyond the midpoint of the box's
        first edge, along the outward direction from the box center — so it rides
        along as the box turns and stays the same size at any zoom.
        """
        points = item.get("points") or []
        center = item.get("center")
        if len(points) < 4 or center is None:
            return None
        cx, cy = center
        mx = (points[0][0] + points[1][0]) / 2.0
        my = (points[0][1] + points[1][1]) / 2.0
        dx, dy = mx - cx, my - cy
        dist = math.hypot(dx, dy)
        nx, ny = (dx / dist, dy / dist) if dist > 1e-6 else (0.0, -1.0)
        zoom = self._zoom if self._zoom > 0 else 1.0
        offset = (self._handle_hit_radius * 2.6) / zoom
        return (mx + nx * offset, my + ny * offset), (mx, my)

    def _hit_test_rotation_handle(self, position) -> dict | None:
        """Return the rotatable item whose rotate handle is under the cursor, if any."""
        item = self._rotation_target()
        if item is None:
            return None
        handle = self._rotation_handle_image_pos(item)
        if handle is None:
            return None
        (hx, hy), _ = handle
        image_x, image_y, _, _ = self._fit_display_rect()
        sx = image_x + hx * self._zoom
        sy = image_y + hy * self._zoom
        radius = self._handle_hit_radius
        if (sx - position.x()) ** 2 + (sy - position.y()) ** 2 <= radius ** 2:
            return item
        return None

    def _begin_rotation(self, item: dict, position) -> None:
        """Start a rotate-drag of ``item`` around its center."""
        center = item.get("center")
        if center is None:
            return
        annotation = item.get("annotation")
        self._rotate_item = item
        self._rotate_center = (float(center[0]), float(center[1]))
        self._rotate_origin_points = [(float(x), float(y)) for x, y in item["points"]]
        self._rotate_before = self._snapshot_annotation(annotation)
        self._rotate_base_rotation = float(annotation.get("rotation", 0) or 0) if annotation else 0.0
        ix, iy = self._image_from_screen(position.x(), position.y())
        self._rotate_start_angle = math.atan2(iy - self._rotate_center[1], ix - self._rotate_center[0])
        self._rotate_moved = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _apply_rotation(self, position) -> None:
        """Rotate the box's original corners by the pointer's angular delta."""
        item = self._rotate_item
        if item is None or self._rotate_center is None:
            return
        cx, cy = self._rotate_center
        ix, iy = self._image_from_screen(position.x(), position.y())
        delta = math.atan2(iy - cy, ix - cx) - self._rotate_start_angle
        cos_d, sin_d = math.cos(delta), math.sin(delta)
        rotated = []
        for px, py in self._rotate_origin_points:
            rx, ry = px - cx, py - cy
            rotated.append((cx + rx * cos_d - ry * sin_d, cy + rx * sin_d + ry * cos_d))
        item["points"] = rotated
        # An axis-aligned bbox item becomes a real rotated quad once turned, so it
        # must render (and edit) as a polygon from here on.
        item["shape"] = "polygon"
        item.setdefault("seg_index", 0)
        item["center"] = (cx, cy)
        self._write_rotated_box(item, self._rotate_base_rotation + math.degrees(delta))
        self._rotate_moved = True
        self.update()

    @staticmethod
    def _write_rotated_box(item: dict, rotation_degrees: float) -> None:
        """Write a rotated box's corners back into its annotation (segmentation/bbox/area/rotation)."""
        annotation = item.get("annotation")
        if annotation is None:
            return
        points = item["points"]
        annotation["segmentation"] = [[coord for point in points for coord in point]]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        min_x, min_y = min(xs), min(ys)
        annotation["bbox"] = [min_x, min_y, max(xs) - min_x, max(ys) - min_y]
        annotation["area"] = polygon_area(points)
        annotation["rotation"] = rotation_degrees % 360.0

    def _finish_rotation(self) -> dict | None:
        """End a rotate-drag; return a before/after edit record if it actually turned."""
        item = self._rotate_item
        annotation = item.get("annotation") if item is not None else None
        moved = self._rotate_moved
        before = self._rotate_before
        self._rotate_item = None
        self._rotate_center = None
        self._rotate_origin_points = []
        self._rotate_before = None
        self._rotate_moved = False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        if moved and annotation is not None:
            return {"annotation": annotation, "before": before, "after": self._snapshot_annotation(annotation)}
        return None

    # ----- moving a whole L2 box ------------------------------------------
    def _hit_test_box_body(self, position) -> dict | None:
        """The L2 box whose body is under the cursor in edit mode (boxes only), or None."""
        if not self._edit_enabled or self._pixmap is None:
            return None
        image_x, image_y, _, _ = self._fit_display_rect()
        target = QPointF(position.x(), position.y())
        for item in reversed(self._overlay_items):
            if not item.get("rotatable") or item.get("annotation") is None:
                continue
            points = item.get("points") or []
            if len(points) < 4:
                continue
            screen_points = [QPointF(image_x + x * self._zoom, image_y + y * self._zoom) for x, y in points]
            if QPolygonF(screen_points).containsPoint(target, Qt.FillRule.OddEvenFill):
                return item
        return None

    def _begin_box_move(self, item: dict, position) -> None:
        """Select ``item`` and arm a whole-box translate drag (commits past a threshold)."""
        annotation = item.get("annotation")
        self._set_selected(annotation)  # also reveals the rotate handle
        self._box_move_item = item
        self._box_move_before = self._snapshot_annotation(annotation)
        self._box_move_origin_points = [(float(x), float(y)) for x, y in item["points"]]
        center = item.get("center") or polygon_centroid(item["points"])
        self._box_move_origin_center = (float(center[0]), float(center[1]))
        self._box_move_start = position.toPoint()
        self._box_move_start_image = self._image_from_screen(position.x(), position.y())
        self._box_move_moved = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _apply_box_move(self, position) -> None:
        """Translate the box's original corners by the pointer's image-space delta."""
        item = self._box_move_item
        if item is None:
            return
        if not self._box_move_moved:
            if (position.toPoint() - self._box_move_start).manhattanLength() < 4:
                return  # sub-threshold jitter stays a click (a plain select)
            self._box_move_moved = True
        ix, iy = self._image_from_screen(position.x(), position.y())
        dx = ix - self._box_move_start_image[0]
        dy = iy - self._box_move_start_image[1]
        item["points"] = [(px + dx, py + dy) for px, py in self._box_move_origin_points]
        cx, cy = self._box_move_origin_center
        item["center"] = (cx + dx, cy + dy)
        self._write_translated_box(item)
        self.update()

    @staticmethod
    def _write_translated_box(item: dict) -> None:
        """Write a moved box's corners back into its annotation (bbox + quad; area/rotation kept)."""
        annotation = item.get("annotation")
        if annotation is None:
            return
        points = item["points"]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        min_x, min_y = min(xs), min(ys)
        annotation["bbox"] = [min_x, min_y, max(xs) - min_x, max(ys) - min_y]
        seg = annotation.get("segmentation")
        if isinstance(seg, list) and seg and isinstance(seg[0], list) and len(seg[0]) >= 8:
            annotation["segmentation"] = [[coord for point in points for coord in point]]

    def _finish_box_move(self) -> dict | None:
        """End a whole-box translate; return a before/after edit record if it actually moved."""
        item = self._box_move_item
        annotation = item.get("annotation") if item is not None else None
        moved = self._box_move_moved
        before = self._box_move_before
        self._box_move_item = None
        self._box_move_before = None
        self._box_move_origin_points = []
        self._box_move_origin_center = None
        self._box_move_moved = False
        self.setCursor(Qt.CursorShape.SizeAllCursor)  # still over the box it just moved
        self.update()
        if moved and annotation is not None:
            return {"annotation": annotation, "before": before, "after": self._snapshot_annotation(annotation)}
        return None

    @staticmethod
    def _arc_xy(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
        """Point on a QPainter arc (0° at 3 o'clock, CCW positive, y-down screen)."""
        rad = math.radians(deg)
        return cx + r * math.cos(rad), cy - r * math.sin(rad)

    def _draw_rotation_handle(self, painter: QPainter, image_x: int, image_y: int) -> None:
        """Draw the circular rotate handle next to the selected L2 box."""
        item = self._rotation_target()
        if item is None:
            return
        handle = self._rotation_handle_image_pos(item)
        if handle is None:
            return
        (hx, hy), (mx, my) = handle
        sx = image_x + hx * self._zoom
        sy = image_y + hy * self._zoom
        emx = image_x + mx * self._zoom
        emy = image_y + my * self._zoom
        # Connector from the box edge to the handle.
        painter.setPen(QPen(QColor(255, 255, 255, 150), max(1.0, 1.2 * DPI_SCALE)))
        painter.drawLine(QPointF(emx, emy), QPointF(sx, sy))
        # Handle disc: white fill, accent ring (slightly larger when hovered).
        radius = self._handle_hit_radius + (2 if (self._rotation_hover or self._rotate_item is item) else 0)
        painter.setPen(QPen(QColor(106, 114, 230), max(1.6, 1.8 * DPI_SCALE)))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QPointF(sx, sy), radius, radius)
        # Rotate glyph: an open arc with an arrowhead, drawn dark on the white disc.
        r = radius * 0.5
        pen = QPen(QColor(28, 28, 30), max(1.5, 1.7 * DPI_SCALE))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        start_deg, span_deg = 125.0, -290.0
        painter.drawArc(QRectF(sx - r, sy - r, 2 * r, 2 * r), int(round(start_deg * 16)), int(round(span_deg * 16)))
        end_deg = start_deg + span_deg
        tip = self._arc_xy(sx, sy, r, end_deg)
        prev = self._arc_xy(sx, sy, r, end_deg + 6.0)  # span<0, so +deg is back along the sweep
        tx, ty = tip[0] - prev[0], tip[1] - prev[1]
        length = math.hypot(tx, ty) or 1.0
        ux, uy = tx / length, ty / length      # unit tangent (sweep direction)
        nx, ny = -uy, ux                        # unit normal
        head = max(3.0, r * 0.9)
        half = max(2.0, r * 0.55)
        base_x, base_y = tip[0] - ux * head, tip[1] - uy * head
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(28, 28, 30))
        painter.drawPolygon(QPolygonF([
            QPointF(tip[0], tip[1]),
            QPointF(base_x + nx * half, base_y + ny * half),
            QPointF(base_x - nx * half, base_y - ny * half),
        ]))

    def _draw_handles(self, painter: QPainter, image_x: int, image_y: int) -> None:
        """Draw grab handles for the SELECTED object's vertices only (like the rotate handle)."""
        if not self._edit_enabled:
            return
        item = self._selected_overlay_item()
        if item is None or not item.get("editable"):
            return
        size = self._handle_size
        hover_item = self._hover_vertex is not None and self._hover_vertex[0] is item
        select_item = self._selected_vertex is not None and self._selected_vertex[0] is item
        for index, (point_x, point_y) in enumerate(item["points"]):
            screen_x = image_x + point_x * self._zoom
            screen_y = image_y + point_y * self._zoom
            if screen_x < -size or screen_y < -size or screen_x > self.width() + size or screen_y > self.height() + size:
                continue
            hovered = hover_item and self._hover_vertex[1] == index
            selected = select_item and self._selected_vertex[1] == index
            if selected:
                # The delete target — larger, accent fill, white ring.
                radius = size + 3
                painter.setPen(QPen(QColor(255, 255, 255), max(1.5, 1.6 * DPI_SCALE)))
                painter.setBrush(QColor(255, 92, 92))
            else:
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
            # The rubber-band segment to the cursor previews the NEXT point — show it
            # only while placing points, not while dragging an already-placed one.
            if self._cursor_image is not None and self._drag_pending_index is None:
                cursor_point = QPointF(image_x + self._cursor_image[0] * self._zoom, image_y + self._cursor_image[1] * self._zoom)
                painter.drawLine(screen_points[-1], cursor_point)
            radius = self._handle_size
            for index, point in enumerate(screen_points):
                if index == self._selected_pending_index:
                    # The delete target — larger, red, white ring (matches edit mode).
                    painter.setPen(QPen(QColor(255, 255, 255), max(1.5, 1.6 * DPI_SCALE)))
                    painter.setBrush(QColor(255, 92, 92))
                    painter.drawEllipse(point, radius + 3, radius + 3)
                else:
                    painter.setPen(QPen(QColor(20, 20, 20), 1))
                    painter.setBrush(QColor(255, 255, 255))
                    painter.drawEllipse(point, radius, radius)
            if len(screen_points) >= 3:
                painter.setPen(QPen(QColor(20, 20, 20), 1))
                painter.setBrush(QColor(106, 114, 230))
                painter.drawEllipse(screen_points[0], radius + 2, radius + 2)

    def _visible_image_rect(self, margin: float = 0.25) -> QRectF:
        """The image-coordinate rectangle currently on screen, grown by ``margin``.

        Used to skip drawing overlays/handles that are off-screen (the dominant
        cost when zoomed into a busy image). ``margin`` keeps a band around the
        viewport so objects just past the edge are still drawn (no pop-in).
        """
        image_x, image_y, _, _ = self._fit_display_rect()
        zoom = self._zoom if self._zoom > 0 else 1.0
        left = -image_x / zoom
        top = -image_y / zoom
        width = self.width() / zoom
        height = self.height() / zoom
        return QRectF(left - width * margin, top - height * margin, width * (1 + 2 * margin), height * (1 + 2 * margin))

    @staticmethod
    def _points_bounds(points) -> QRectF:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def _draw_overlays(self, painter: QPainter, image_x: int, image_y: int) -> None:
        if not self._overlay_items:
            return

        visible = self._visible_image_rect()
        alpha = max(0, min(255, int(round(self.annotation_opacity * 255))))
        point_radius = max(2, int(round(3 * DPI_SCALE)))
        label_font = QFont()
        label_font.setPointSizeF(max(8.5, 9.0 * DPI_SCALE))
        label_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(label_font)

        for item in self._overlay_items:
            points = item["points"]
            # Cull objects fully outside the viewport (+ margin) — the main cost
            # when zoomed into a crowded image.
            if points and not visible.intersects(self._points_bounds(points)):
                continue

            color_value = item["color"]
            color = QColor(*color_value) if isinstance(color_value, tuple) else QColor(color_value)
            fill_color = QColor(color)
            fill_color.setAlpha(alpha)
            outline_color = QColor(color)
            outline_color.setAlpha(230)

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

        # Right-click inserts a vertex on the nearest edge: a committed polygon (edit
        # mode), or the in-progress polyline while drawing.
        if event.button() == Qt.MouseButton.RightButton and self._pixmap is not None:
            if self._draw_shape == "polygon":
                self._insert_pending_vertex_at(event.position())
                return
            if self._edit_enabled and self._draw_shape is None:
                self.insert_vertex_at(event.position())
                return

        if event.button() == Qt.MouseButton.LeftButton:
            # The rotate handle of a selected L2 box wins over everything else.
            rotate_item = self._hit_test_rotation_handle(event.position())
            if rotate_item is not None:
                self._begin_rotation(rotate_item, event.position())
                return

            # Drawing takes precedence over panning/editing when armed.
            if self._draw_shape is not None and self._pixmap is not None:
                # Drawing points are pinned to the image so nothing lands off-picture.
                image_x, image_y = self._image_from_screen(event.position().x(), event.position().y(), clamp=True)
                if self._draw_shape == "polygon":
                    # Grabbing an existing point edits it (drag to move, click to
                    # select / close-on-first) instead of adding a new one.
                    hit_index = self._hit_test_pending_vertex(event.position())
                    if hit_index is not None:
                        self._drag_pending_index = hit_index
                        self._pending_moved = False
                        self._drag_start = event.position().toPoint()
                        self.setCursor(Qt.CursorShape.ClosedHandCursor)
                        return
                    self._poly_points.append((image_x, image_y))
                    self._selected_pending_index = None
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
                # Select the vertex (for keyboard delete) — only on polygons whose
                # vertex count may change; bbox/oriented corners can be dragged but
                # not added/removed, so they don't become "selected".
                self._selected_vertex = hit if hit[0].get("vertex_editable") else None
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
                return
            # Not on a corner: dragging an L2 box body (edit mode) moves it whole.
            box_body = self._hit_test_box_body(event.position())
            if box_body is not None:
                self._begin_box_move(box_body, event.position())
                return
            # Not on a vertex: any other left-click drops the vertex selection.
            self._selected_vertex = None
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
        if self._rotate_item is not None:
            self._apply_rotation(event.position())
            return

        if self._box_move_item is not None:
            self._apply_box_move(event.position())
            return

        if self._drag_pending_index is not None:
            current = event.position().toPoint()
            if not self._pending_moved and (current - self._drag_start).manhattanLength() < 4:
                return  # sub-threshold jitter: keep it a click (close / select still works)
            self._pending_moved = True
            image_x, image_y = self._image_from_screen(event.position().x(), event.position().y(), clamp=True)
            if 0 <= self._drag_pending_index < len(self._poly_points):
                self._poly_points[self._drag_pending_index] = (image_x, image_y)
            self.update()
            return

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
            # Settle a rotate-drag of a selected L2 box (emit a before/after record).
            if self._rotate_item is not None:
                record = self._finish_rotation()
                if record is not None:
                    self.annotationsChanged.emit(record)
                return

            # Settle a whole-box move (emit a before/after record if it actually moved).
            if self._box_move_item is not None:
                record = self._finish_box_move()
                if record is not None:
                    self.annotationsChanged.emit(record)
                return

            # Settling a grabbed in-progress polygon point: a drag finalises the move;
            # a click (no drag) closes on the first point, else selects it for delete.
            if self._drag_pending_index is not None:
                index = self._drag_pending_index
                moved = self._pending_moved
                self._drag_pending_index = None
                self._pending_moved = False
                # Drop the stale cursor so the rubber-band preview doesn't snap back
                # to where the point was grabbed; it returns on the next mouse move.
                self._cursor_image = None
                self.setCursor(Qt.CursorShape.CrossCursor)
                if not moved:
                    if index == 0 and len(self._poly_points) >= 3:
                        self.finish_polygon()
                    else:
                        self._selected_pending_index = index
                        self.update()
                else:
                    self.update()
                return

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
        """Deep-copy the geometry fields that a vertex/rotation edit can change, for undo/redo."""
        if annotation is None:
            return None
        snapshot = {
            "bbox": copy.deepcopy(annotation.get("bbox")),
            "area": annotation.get("area"),
            "segmentation": copy.deepcopy(annotation.get("segmentation")),
        }
        # Only L2 boxes carry rotation; keep it out of polygon snapshots so undo
        # never writes a stray rotation key onto an L1/L3 annotation.
        if "rotation" in annotation:
            snapshot["rotation"] = annotation.get("rotation")
        return snapshot

    def _update_hover(self, position) -> None:
        """Highlight the handle under the cursor and switch the cursor shape."""
        # The rotate handle (when a rotatable L2 box is selected) takes hover first.
        over_rotation = self._hit_test_rotation_handle(position) is not None
        if over_rotation != self._rotation_hover:
            self._rotation_hover = over_rotation
            self.update()
        if over_rotation:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            return

        hit = self._hit_test_vertex(position)
        new_key = (id(hit[0]), hit[1]) if hit is not None else None
        old_key = (id(self._hover_vertex[0]), self._hover_vertex[1]) if self._hover_vertex is not None else None
        if new_key != old_key:
            self._hover_vertex = hit
            self.update()
        if hit is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)  # a draggable corner
        elif self._hit_test_box_body(position) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)   # a movable L2 box body
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit_mode and self._pixmap is not None:
            self.fit_to_view()


class RedefineRowDelegate(QStyledItemDelegate):
    """Paints image-selector row backgrounds to flag work to do:
      • RED  (FLAG_ROLE)       — the image still has an object to redefine.
      • AMBER (INCOMPLETE_ROLE) — at least one level is completely empty for it.
    Red wins when both apply. A delegate is used because the view's QSS
    (``::item`` background) overrides any model BackgroundRole."""

    FLAG_ROLE = Qt.ItemDataRole.UserRole + 100
    INCOMPLETE_ROLE = Qt.ItemDataRole.UserRole + 101

    def paint(self, painter, option, index) -> None:  # noqa: N802
        if index.data(self.FLAG_ROLE):
            painter.fillRect(option.rect, QColor(120, 45, 48))   # needs redefine
        elif index.data(self.INCOMPLETE_ROLE):
            painter.fillRect(option.rect, QColor(122, 82, 28))   # a level is empty
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
        pad = max(10, int(round(10 * DPI_SCALE)))  # match the QSS "padding: 3px 10px"
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

        pill_height = max(24, int(round(26 * DPI_SCALE)))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(pill_height)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._pill = OutlinedTextButton(category_name)
        self._pill.setCheckable(True)
        self._pill.setChecked(True)
        self._pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pill.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._pill.setMinimumHeight(pill_height)
        self._pill.toggled.connect(self.toggled)
        self._pill.toggled.connect(self._refresh_style)
        layout.addWidget(self._pill)

        edit_size = pill_height
        self._edit_btn = QPushButton()
        self._edit_btn.setIcon(QIcon(str(resource_path("assets", "edit_icon.svg"))))
        icon_px = max(13, int(round(14 * DPI_SCALE)))
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
                border-radius: 11px;
                padding: 3px 10px;
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
        # Wide enough that the longer mouse-gesture rows (description + key cap)
        # aren't clipped on the right.
        self.setMinimumWidth(max(640, int(round(720 * DPI_SCALE))))
        self._build_ui()
        self._apply_styles()
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        """Open at a comfortable width/height that never exceeds the screen work area.

        Without this the all-rows layout can ask for a window taller (and, with the
        long gesture rows, the right column gets clipped) than the display; Windows
        clamps it and logs a ``setGeometry`` warning. The scroll area absorbs height
        overflow, so capping is cosmetic-safe.
        """
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else None
        avail_h = avail.height() if avail is not None else 900
        avail_w = avail.width() if avail is not None else 1200
        target_h = min(max(400, avail_h - 80), int(round(820 * DPI_SCALE)))
        target_w = min(avail_w - 80, max(self.minimumWidth(), self.sizeHint().width()))
        self.resize(target_w, target_h)
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
            ("Click an object (Edit)", "select it — only the selected object shows handles"),
            ("Drag a handle (Edit)", "move a box corner / polygon vertex of the selected object"),
            ("Drag an L2 box body (Edit)", "move the whole box; its round handle rotates it"),
            ("Click a vertex (Edit)", "select it; Delete/Backspace removes it (polygons only)"),
            ("Right-click (Edit)", "add a polygon vertex on the nearest edge"),
            ("Drag a point while drawing", "move it; click an existing point to select, Delete removes"),
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


class ClassListWindow(QDialog):
    """A resizable, free-floating window that hosts the sidebar's class list.

    The SAME class-list container is re-parented in here (not duplicated), so all
    its live wiring — toggles, color pickers, the redefine/change-class panels —
    keeps working; closing docks it back into the sidebar.
    """

    closed = pyqtSignal()

    def __init__(self, owner: QWidget) -> None:
        # No Qt parent: an *owned* dialog shares the main window's taskbar button
        # and can't be alt+tab'd independently. As a parentless top-level
        # ``Qt.Window`` it gets its own taskbar entry and tabs like a real app
        # window (e.g. onto a second monitor).
        super().__init__(None)
        self._owner = owner
        self.setWindowTitle("Class list")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowIcon(owner.windowIcon())
        # A standard top-level window: own taskbar button + real min/max/close.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self.resize(max(320, int(round(360 * DPI_SCALE))), max(480, int(round(640 * DPI_SCALE))))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("classScroll")
        layout.addWidget(self.scroll)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


class CanvasWindow(QDialog):
    """A resizable, free-floating window that hosts the image canvas.

    The SAME canvas container is re-parented in here (not duplicated), so all the
    rendering, zoom/pan, drawing and vertex-editing wiring keeps working — the
    canvas just paints into whatever size this window gives it, so aspect ratio
    and fit-scaling are unaffected. Closing docks it back into the main view.
    Key presses are forwarded to the owner so navigation shortcuts still work
    while this window holds focus on a second monitor.
    """

    closed = pyqtSignal()

    def __init__(self, owner: QWidget) -> None:
        # Parentless top-level window (see ClassListWindow): own taskbar button,
        # independently alt+tab-able onto a second monitor.
        super().__init__(None)
        self._owner = owner
        self.setWindowTitle("Image view")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowIcon(owner.windowIcon())
        # A standard top-level window: own taskbar button + real min/max/close.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self.resize(max(640, int(round(900 * DPI_SCALE))), max(480, int(round(680 * DPI_SCALE))))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._layout = layout

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Let the main window's shortcut dispatch handle navigation etc.
        self._owner.keyPressEvent(event)
        if not event.isAccepted():
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit()
        super().closeEvent(event)


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
        # Wall-clock of the last successful level-file write this session (None until
        # the first save). Drives the "✓ saved …" status reassurance.
        self._last_saved_at: float | None = None
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

        self.dataset = None
        self.temp_extraction = None
        self.images_path = ""
        self.images = []
        self.categories_by_id = {}
        self.current_class_items: list[tuple[int, str]] = []
        self.current_category_ids: list[int] = []
        self.visible_by_category: dict[int, bool] = {}
        self.class_checkboxes: dict[int, ClassBubbleButton] = {}
        self.class_popout: ClassListWindow | None = None  # detached class-list window
        self.canvas_popout: CanvasWindow | None = None  # detached image-canvas window
        # Current-image working state (set when an image loads; defaulted so mode
        # switching is safe before any dataset/images are opened).
        self.index = 0
        self.current_annotations: list = []
        self.current_overlay_items: list[dict] = []
        self.current_image_name = ""
        self.current_image_path = ""
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

        # Keep the "✓ saved … ago" tail current while the user is idle (the status
        # bar otherwise only refreshes on interaction).
        self._status_tick = QTimer(self)
        self._status_tick.setInterval(30_000)
        self._status_tick.timeout.connect(self._update_status_labels)
        self._status_tick.start()

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
            # No keyboard "quit" — the app closes only via the window's close button
            # (which still runs the unsaved-changes prompt in closeEvent).
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

        # Kept so the canvas can be detached into its own window (e.g. a second
        # monitor) and docked back. The hint fills the canvas's spot meanwhile.
        self.main_layout = main_layout
        self.canvas_container = canvas_container
        self.canvas_popout_hint = QLabel(
            "Image view is open in a separate window.\nClose that window to dock it back here."
        )
        self.canvas_popout_hint.setObjectName("muted")
        self.canvas_popout_hint.setWordWrap(True)
        self.canvas_popout_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas_popout_hint.setVisible(False)
        main_layout.addWidget(self.canvas_popout_hint, 1)

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
        header = QLabel("Annotation Workbench")
        header.setObjectName("header")
        header_row.addWidget(header)
        header_row.addStretch(1)
        # The current image's id (last 9 chars of the filename — the Copy ID value),
        # shown beside the title so the annotator always sees which image they're on.
        self.image_id_label = QLabel("")
        self.image_id_label.setObjectName("imageIdBadge")
        self.image_id_label.setToolTip("Current image id — the last 9 characters copied by Copy ID")
        self.image_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header_row.addWidget(self.image_id_label)
        sidebar_layout.addLayout(header_row)

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

        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primaryButton")
        self.save_button.setToolTip("Save annotations to a COCO JSON file (Ctrl+S)")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_dataset)

        self.copy_id_button = QPushButton("Copy ID")
        self.copy_id_button.setObjectName("copyIdButton")
        self.copy_id_button.setMinimumHeight(28)
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

        self.reset_button = QPushButton("Reset view")
        self.reset_button.setToolTip("Fit the image and clear pan (R)")
        self.reset_button.clicked.connect(self._reset_view)
        sidebar_layout.addWidget(self.reset_button)

        opacity_row = QHBoxLayout()
        opacity_row.addWidget(QLabel("Opacity"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        # Tall enough that the round handle (which overflows the groove) isn't clipped.
        self.opacity_slider.setMinimumHeight(max(20, int(round(22 * DPI_SCALE))))
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

        self.class_list_container = QWidget()
        self.class_list_container.setObjectName("classListContainer")
        self.class_list_layout = QVBoxLayout(self.class_list_container)
        self.class_list_layout.setContentsMargins(0, 0, 0, 0)
        self.class_list_layout.setSpacing(4)
        self.class_list_layout.addStretch(1)

        self.class_scroll = QScrollArea()
        self.class_scroll.setWidgetResizable(True)
        self.class_scroll.setWidget(self.class_list_container)
        self.class_scroll.setObjectName("classScroll")
        sidebar_layout.addWidget(self.class_scroll, 1)

        # Shown in place of the scroll while the class list is popped out.
        self.class_popout_hint = QLabel("Class list is open in a separate window.\nClose that window to dock it back here.")
        self.class_popout_hint.setObjectName("muted")
        self.class_popout_hint.setWordWrap(True)
        self.class_popout_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.class_popout_hint.setVisible(False)
        sidebar_layout.addWidget(self.class_popout_hint, 1)

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

        self.canvas_popout_button = QPushButton("⧉ Image view")
        self.canvas_popout_button.setObjectName("statusHelpButton")
        self.canvas_popout_button.setFixedHeight(max(18, int(round(20 * DPI_SCALE))))
        self.canvas_popout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.canvas_popout_button.setToolTip("Open the image view in a separate, resizable window (e.g. a second monitor)")
        self.canvas_popout_button.clicked.connect(self._toggle_canvas_popout)

        self.popout_button = QPushButton("⧉ Class list")
        self.popout_button.setObjectName("statusHelpButton")
        self.popout_button.setFixedHeight(max(18, int(round(20 * DPI_SCALE))))
        self.popout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.popout_button.setToolTip("Open the class list in a separate, resizable window")
        self.popout_button.clicked.connect(self._toggle_class_popout)

        self.export_zip_button = QPushButton("⤓ Export zip")
        self.export_zip_button.setObjectName("statusHelpButton")
        self.export_zip_button.setFixedHeight(max(18, int(round(20 * DPI_SCALE))))
        self.export_zip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_zip_button.setToolTip("Bundle the whole project into one shareable / back-up zip")
        self.export_zip_button.clicked.connect(self._export_project_zip)

        self.help_button = QPushButton("⌨ Shortcuts")
        self.help_button.setObjectName("statusHelpButton")
        self.help_button.setFixedHeight(max(18, int(round(20 * DPI_SCALE))))
        self.help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_button.setToolTip("Keyboard shortcuts & help")
        self.help_button.clicked.connect(self._show_help)

        status_bar = self.statusBar()
        status_bar.setSizeGripEnabled(False)
        status_bar.addWidget(self.status_label, 1)
        status_bar.addPermanentWidget(self.canvas_popout_button)
        status_bar.addPermanentWidget(self.popout_button)
        status_bar.addPermanentWidget(self.help_button)
        status_bar.addPermanentWidget(self.export_zip_button)  # far-right corner, right of Shortcuts

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
        workflow_action = QAction("How it works…", self)
        workflow_action.triggered.connect(self._show_workflow_help)
        settings_menu.addAction(workflow_action)
        shortcuts_action = QAction("Keyboard shortcuts…", self)
        shortcuts_action.triggered.connect(self._show_help)
        settings_menu.addAction(shortcuts_action)
        logs_action = QAction("Open logs folder…", self)
        logs_action.triggered.connect(self._open_logs_folder)
        settings_menu.addAction(logs_action)

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

        column.addSpacing(4)
        help_button = QPushButton("How it works")
        help_button.setObjectName("statusHelpButton")  # quiet link-style, not a primary action
        help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        help_button.setToolTip("A quick tour of projects, levels, modes and editing")
        help_button.clicked.connect(self._show_workflow_help)
        column.addWidget(help_button, 0, Qt.AlignmentFlag.AlignHCenter)

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
        # Leaving the working view: pull a detached canvas back so it can't linger.
        if not has_project and getattr(self, "canvas_popout", None) is not None:
            self.canvas_popout.close()
        # view_stack/import_menu exist only after _build_ui has created them.
        if hasattr(self, "view_stack"):
            self.view_stack.setCurrentIndex(1 if has_project else 0)
        if hasattr(self, "action_close_project"):
            self.action_close_project.setEnabled(has_project)
        if hasattr(self, "action_export_zip"):
            self.action_export_zip.setEnabled(has_project)
        if hasattr(self, "export_zip_button"):
            self.export_zip_button.setVisible(has_project)
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
        self._last_saved_at = None  # "✓ saved" is scoped to this project session
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
        self.annotation_store = {lvl: {} for lvl in levels.LEVEL_IDS}
        self.categories_by_id = {}
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

    @contextlib.contextmanager
    def _busy(self, message: str):
        """Show a wait cursor + status message while a blocking operation runs.

        Imports copy/extract/parse on the UI thread; without feedback the window
        looks frozen ("Not Responding"). This flips the cursor and the status line
        so the user sees progress, and always restores them — even on error.
        """
        previous = self.status_label.text()
        self.status_label.setText(message)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()  # paint the cursor + status before we block
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()
            self.status_label.setText(previous)  # _update_status_labels resets it shortly after

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
            with self._busy("Importing images…"):
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
            with self._busy("Importing zip…"):
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
            with self._busy("Loading annotations…"):
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
            # Resolve the image by stable id (robust to re-export prefixes), so a
            # present image is recognised even when its filename differs, and the
            # annotation is stored under the project's own basename.
            basename = os.path.basename(str(entry.get("file_name", "")))
            sid = int(entry.get("stable_image_id", stable_image_id(basename)))
            record = self.project.image_record(sid)
            if record is not None:
                canonical = os.path.basename(str(record["file_name"]))
                additions.setdefault(remap["level"], {}).setdefault(canonical, []).append(converted)
            else:
                orphan_entries.append(
                    {
                        "level": remap["level"],
                        "stable_image_id": sid,
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
            QLabel#imageIdBadge {{
                color: #d6e2ff;
                background: #2a3340;
                border: 1px solid #3a4656;
                border-radius: 6px;
                padding: 2px 8px;
                font-family: "Consolas", "Courier New", monospace;
                font-weight: 700;
                font-size: {max(8, int(round(9 * DPI_SCALE)))}pt;
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
                padding: 4px 9px;
                border-radius: 7px;
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
                padding: 5px 11px;
                border-radius: 7px;
                min-width: 72px;
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

    def _current_image_id_text(self) -> str:
        """The current image's id: last 9 chars of the filename stem (Copy ID value)."""
        if not self.current_image_name:
            return ""
        stem = os.path.splitext(os.path.basename(self.current_image_name))[0]
        return stem[-9:] if len(stem) >= 9 else stem

    def _copy_image_id(self) -> None:
        """Copy the last 9 characters of the image filename (without extension) to clipboard."""
        image_id = self._current_image_id_text()
        if not image_id:
            return

        # Copy to clipboard
        clipboard = QApplication.clipboard()
        clipboard.setText(image_id)
        
        # Optional: Show brief confirmation
        self.copy_id_button.setText("Copied!")
        # Reset button text after 1.5 seconds
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self.copy_id_button.setText("Copy ID") if self.copy_id_button else None)

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
        if checked:
            self.canvas.clear_selection()  # starting to draw drops any selected object
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
    def _reset_session_state(self) -> None:
        """Clear all loaded-data state (used when closing a project)."""
        self.dataset = None
        self.temp_extraction = None
        self.images_path = ""
        self.images = []
        self.index = 0
        self.categories_by_id = {}
        self.current_category_ids = []
        self.current_class_items = []
        self.current_annotations = []
        self.current_overlay_items = []
        self.current_image_name = ""
        self.current_image_path = ""
        self.visible_by_category = {}
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
            # Both modes share one project session (same images + per-level stores),
            # so we keep it intact and just re-render for the new mode.
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

    # ----- annotation: per-level stores -----------------------------------
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
                if len(points) == 4:
                    # An oriented box (e.g. a Label Studio rotated rectangle): KEEP its
                    # rotated 4-corner quad + angle. Collapsing to the envelope here was
                    # silently dropping rotation when importing such exports.
                    out["segmentation"] = [list(contour)]
                    out["bbox"] = [float(v) for v in envelope[:4]]
                    out["area"] = annotation.get("area") or polygon_area(points)
                    out["rotation"] = float(annotation.get("rotation", 0) or 0)
                    return out
                # A real polygon class remapped to the box level becomes its envelope.
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
                # Amber when any level is completely empty for this image.
                if self.project is not None and any(
                    not self.annotation_store[lvl].get(filename) for lvl in levels.LEVEL_IDS
                ):
                    self.image_selector.setItemData(i, True, RedefineRowDelegate.INCOMPLETE_ROLE)
            
            # Keep the placeholder text visible by not setting current index
            # This way users always see "Select image" in the combo box
            self.image_selector.setCurrentIndex(-1)
        
        self.image_selector.blockSignals(False)

        # Manually trigger the selection handler
        if self.images:
            self._on_image_selected(self.index)

    def _refresh_incomplete_flags(self) -> None:
        """Re-mark image-selector rows amber when any level is empty for that image.

        Cheap (just toggles the row role, no combo rebuild) so it can run after every
        edit — the amber clears the moment an image's last empty level gets an object.
        """
        selector = getattr(self, "image_selector", None)
        if selector is None or not self.images or self.project is None:
            return
        for pos in range(selector.count()):
            index = selector.itemData(pos)
            if not isinstance(index, int) or not (0 <= index < len(self.images)):
                continue
            image_info = self.images[index]
            filename = os.path.basename(str(image_info.get("file_name", ""))) if isinstance(image_info, dict) else ""
            incomplete = any(not self.annotation_store[lvl].get(filename) for lvl in levels.LEVEL_IDS)
            selector.setItemData(pos, True if incomplete else None, RedefineRowDelegate.INCOMPLETE_ROLE)

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
        # A project session never auto-closes; stop at the last image.
        if self.index >= len(self.images) - 1:
            return
        self.index += 1
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

    def _toggle_class_popout(self) -> None:
        """Detach the class list into a resizable window (or dock it back)."""
        if self.class_popout is not None:
            self.class_popout.close()  # -> closed signal -> _dock_class_list
            return
        popout = ClassListWindow(self)
        popout.setStyleSheet(self.styleSheet() + "\nQDialog { background: #14161a; }")  # inherit the dark theme
        popout.closed.connect(self._dock_class_list)
        container = self.class_scroll.takeWidget()  # move the SAME widget out
        popout.scroll.setWidget(container)
        self.class_scroll.setVisible(False)
        self.class_popout_hint.setVisible(True)
        self.popout_button.setText("⧉ Dock list")
        self.class_popout = popout
        popout.show()
        popout.raise_()

    def _dock_class_list(self) -> None:
        """Move the class list back into the sidebar when its window closes."""
        if self.class_popout is None:
            return
        container = self.class_popout.scroll.takeWidget()
        if container is not None:
            self.class_scroll.setWidget(container)
        self.class_scroll.setVisible(True)
        self.class_popout_hint.setVisible(False)
        self.popout_button.setText("⧉ Class list")
        self.class_popout = None

    def _toggle_canvas_popout(self) -> None:
        """Detach the image view into a resizable window (or dock it back)."""
        if self.canvas_popout is not None:
            self.canvas_popout.close()  # -> closed signal -> _dock_canvas
            return
        popout = CanvasWindow(self)
        popout.setStyleSheet(self.styleSheet() + "\nQDialog { background: #14161a; }")
        popout.closed.connect(self._dock_canvas)
        self.main_layout.removeWidget(self.canvas_container)  # move the SAME widget out
        popout._layout.addWidget(self.canvas_container)
        self.canvas_container.setVisible(True)
        self.canvas_popout_hint.setVisible(True)
        self.canvas_popout_button.setText("⧉ Dock view")
        self.canvas_popout = popout
        popout.show()
        popout.raise_()

    def _dock_canvas(self) -> None:
        """Move the image view back into the main window when its window closes."""
        if self.canvas_popout is None:
            return
        popout, self.canvas_popout = self.canvas_popout, None
        popout._layout.removeWidget(self.canvas_container)
        self.main_layout.insertWidget(0, self.canvas_container, 1)
        self.canvas_container.setVisible(True)
        self.canvas_popout_hint.setVisible(False)
        self.canvas_popout_button.setText("⧉ Image view")

    def _open_logs_folder(self) -> None:
        """Open the folder holding this build's per-session log files."""
        from . import applog
        try:
            directory = applog.log_dir()
            os.startfile(str(directory))  # noqa: S606 - Windows file explorer
        except Exception as error:  # noqa: BLE001
            self._show_message(f"Could not open the logs folder: {error}")

    _HELP_EN = (
        "PROJECTS\n"
        "• Project ▸ New / Open creates or opens a self-contained project folder\n"
        "  (images + annotations/level1‑3.json). Everything lives there.\n"
        "• Import ▸ Open zip / image folder / annotations adds data. Images are\n"
        "  de-duplicated; annotations are sorted into the 3 levels by class name.\n"
        "• Project ▸ Export as zip… bundles the whole project to share.\n\n"
        "LEVELS  (L1 surfaces · L2 objects/boxes · L3 details)\n"
        "• The L1/L2/L3 selector picks which level you review or draw. L1 & L3 are\n"
        "  polygons; L2 is (optionally rotated) boxes.\n\n"
        "MODES\n"
        "• Validation — review existing annotations; the class list shows only the\n"
        "  classes present on the current image (toggle visibility, Show/Hide all).\n"
        "• Annotate — draw new objects with the Draw tool (D). Both modes share the\n"
        "  same per-level data and save the same level files.\n\n"
        "EDITING\n"
        "• Edit objects (T): select an object first — only the SELECTED object shows\n"
        "  its handles. Drag a corner/vertex to move it. On polygons, click a vertex\n"
        "  then Delete to remove it, or right-click an edge to add one. Drag an L2 box\n"
        "  body to move it whole, or its round handle to rotate it.\n"
        "• While drawing: click to place points, drag a point to move it, right-click\n"
        "  to insert, Enter/double-click to finish, Esc to cancel.\n"
        "• Select an object to change its class (sidebar catalog) or delete it (Del).\n"
        "  Edits are undoable (Ctrl+Z / Ctrl+Y) and saved per level (Ctrl+S / Save all).\n\n"
        "REDEFINE\n"
        "• Imported classes not in the catalog are parked (dashed magenta) and shown\n"
        "  on every level until assigned. Images holding one are marked red in the\n"
        "  image list. Click such an object to assign it a class, or use\n"
        "  Project ▸ Redefine classes… to remap them in bulk.\n\n"
        "The app closes only via the window's close button (it prompts to save)."
    )

    # Czech translation of _HELP_EN. App UI labels (menu / button names) are kept in
    # English on purpose so they match the actual (English) interface.
    _HELP_CZ = (
        "PROJEKTY\n"
        "• Project ▸ New / Open vytvoří nebo otevře samostatnou složku projektu\n"
        "  (obrázky + annotations/level1‑3.json). Vše je uloženo zde.\n"
        "• Import ▸ Open zip / image folder / annotations přidá data. Obrázky se\n"
        "  deduplikují; anotace se třídí do 3 úrovní podle názvu třídy.\n"
        "• Project ▸ Export as zip… zabalí celý projekt ke sdílení.\n\n"
        "ÚROVNĚ  (L1 povrchy · L2 objekty/boxy · L3 detaily)\n"
        "• Přepínač L1/L2/L3 určuje, kterou úroveň prohlížíte nebo kreslíte. L1 a L3\n"
        "  jsou polygony; L2 jsou (volitelně otočené) boxy.\n\n"
        "REŽIMY\n"
        "• Validation — kontrola existujících anotací; seznam tříd ukazuje pouze třídy\n"
        "  přítomné na aktuálním obrázku (přepínání viditelnosti, Show/Hide all).\n"
        "• Annotate — kreslení nových objektů nástrojem Draw (D). Oba režimy sdílejí\n"
        "  stejná data podle úrovní a ukládají stejné soubory úrovní.\n\n"
        "ÚPRAVY\n"
        "• Edit objects (T): nejprve vyberte objekt — úchyty zobrazuje pouze VYBRANÝ\n"
        "  objekt. Tažením rohu/vrcholu jej posunete. U polygonů klikněte na vrchol a\n"
        "  klávesou Delete jej odeberete, nebo pravým tlačítkem na hranu přidáte nový.\n"
        "  Tažením těla L2 boxu jej posunete celý, kulatým úchytem jej otočíte.\n"
        "• Při kreslení: klikáním umísťujete body, tažením bodu jej posunete, pravým\n"
        "  tlačítkem vložíte bod, Enter/dvojklik dokončí, Esc zruší.\n"
        "• Vyberte objekt pro změnu jeho třídy (katalog v postranním panelu) nebo jeho\n"
        "  smazání (Del). Úpravy lze vrátit zpět (Ctrl+Z / Ctrl+Y) a ukládají se po\n"
        "  úrovních (Ctrl+S / Save all).\n\n"
        "REDEFINICE\n"
        "• Importované třídy, které nejsou v katalogu, se odloží (čárkovaně purpurově)\n"
        "  a zobrazují se na každé úrovni, dokud nejsou přiřazeny. Obrázky, které\n"
        "  takový objekt obsahují, jsou v seznamu obrázků označeny červeně. Klikněte na\n"
        "  takový objekt pro přiřazení třídy, nebo použijte Project ▸ Redefine classes…\n"
        "  pro hromadné přemapování.\n\n"
        "Aplikaci lze zavřít pouze tlačítkem pro zavření okna (vyzve k uložení)."
    )

    def _show_workflow_help(self) -> None:
        """Explain the project → levels → modes → editing workflow (EN / CZ toggle)."""
        dialog = QDialog(self)
        dialog.setWindowTitle("How it works")
        dialog.setStyleSheet(
            _DARK_DIALOG_QSS
            + "QLabel#helpHeading { font-size: 15px; font-weight: 600; }"
            + "QTextEdit#helpBody { background:#15171a; border:1px solid #2a2e34;"
            "  border-radius:6px; padding:8px; font-family:Consolas,'Courier New',monospace; }"
            + "QPushButton#langButton { min-width:38px; padding:3px 10px; }"
            + "QPushButton#langButton:checked { background:#6a72e6; border-color:#8088ff; color:#ffffff; }"
        )
        dialog.resize(max(680, int(round(740 * DPI_SCALE))), max(480, int(round(560 * DPI_SCALE))))
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        # Header row: title on the left, EN / CZ language toggle in the top-right.
        top = QHBoxLayout()
        heading = QLabel("How it works")
        heading.setObjectName("helpHeading")
        top.addWidget(heading)
        top.addStretch(1)
        lang_group = QButtonGroup(dialog)
        lang_group.setExclusive(True)
        en_button = QPushButton("EN")
        cz_button = QPushButton("CZ")
        for button in (en_button, cz_button):
            button.setObjectName("langButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            lang_group.addButton(button)
            top.addWidget(button)
        en_button.setChecked(True)  # default to English
        layout.addLayout(top)

        body = QTextEdit()
        body.setObjectName("helpBody")
        body.setReadOnly(True)
        body.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        body.setPlainText(self._HELP_EN)
        layout.addWidget(body, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_button = QPushButton("Close")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(dialog.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        en_button.clicked.connect(lambda: body.setPlainText(self._HELP_EN))
        cz_button.clicked.connect(lambda: body.setPlainText(self._HELP_CZ))
        dialog.exec()

    def _original_size(self) -> None:
        self.canvas.reset_to_original_size()
        self._update_status_labels()

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
                    "rotatable": True,  # axis-aligned L2 box — the rotate handle turns it
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
                # Add/remove vertices only on TRUE polygons (L1/L3). An oriented L2
                # box also renders as a 4-point polygon but must stay a quad.
                "vertex_editable": geometry == levels.GEOMETRY_POLYGON,
                # The rotate handle is offered for L2 boxes (oriented quads) only.
                "rotatable": geometry == levels.GEOMETRY_ROTATED_BBOX,
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

    @staticmethod
    def _entry_image_id(entry: dict) -> int | None:
        """Stable image id for a stashed entry (its own id, else hashed from name)."""
        sid = entry.get("stable_image_id")
        if sid is not None:
            try:
                return int(sid)
            except (TypeError, ValueError):
                pass
        name = os.path.basename(str(entry.get("file_name", "")))
        return stable_image_id(name) if name else None

    def _current_stable_image_id(self) -> int | None:
        """Stable image id of the image on screen (the registry id, robust to prefixes)."""
        if not self.images or not (0 <= self.index < len(self.images)):
            return None
        record = self.images[self.index]
        sid = record.get("id") if isinstance(record, dict) else None
        if sid is None:
            basename = self._current_image_basename()
            sid = stable_image_id(basename) if basename else None
        try:
            return int(sid) if sid is not None else None
        except (TypeError, ValueError):
            return None

    def _pending_redefine_overlays(self, level: int) -> list[dict]:
        """Highlighted, read-only overlays for unredefined classes shown on ``level``.

        Includes entries parked in this level AND not-yet-parked entries (which
        show on every level until assigned), so a redefinable object is visible
        regardless of the active level until the user picks its real one.
        """
        if self.project is None:
            return []
        if not self._current_image_basename():
            return []
        # Match by stable_image_id, NOT exact filename: a re-exported photo can
        # carry a different prefix in the import than the project's stored copy,
        # yet shares an id. (Filename matching is why the red row could flag an
        # image while no redefine overlay appeared on it.)
        current_sid = self._current_stable_image_id()
        overlays: list[dict] = []
        for entry in self.project.unmapped_entries_visible_on_level(level):
            if current_sid is None or self._entry_image_id(entry) != current_sid:
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

    def _saved_suffix(self) -> str:
        """Status-bar tail confirming the last save ('✓ saved … ago'), or ''."""
        if self._last_saved_at is None:
            return ""
        elapsed = max(0.0, time.time() - self._last_saved_at)
        if elapsed < 60:
            when = "just now"
        elif elapsed < 3600:
            when = f"{int(elapsed // 60)}m ago"
        else:
            when = f"{int(elapsed // 3600)}h ago"
        return f"   ·   ✓ saved {when}"

    def _save_state_suffix(self) -> str:
        """Unsaved marker when dirty, else the last-saved confirmation."""
        return self._unsaved_suffix() or self._saved_suffix()

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
        # class" catalog; a selected normal object swaps to the "change class"
        # catalog; deselecting restores the normal class list.
        redefine_item = self.canvas.selected_redefine()
        if redefine_item is not None:
            self._show_redefine_assign_panel(redefine_item)
            self.delete_button.setEnabled(False)
            return
        annotation = self.canvas.selected_annotation()
        can_edit = self.mode == "annotation" or self.project is not None
        if annotation is not None and can_edit:
            self._show_reclass_panel(annotation)
            self.delete_button.setEnabled(True)
            return
        if self._redefine_panel_active:
            self._restore_normal_class_list()
        self.delete_button.setEnabled(can_edit and annotation is not None)

    def _restore_normal_class_list(self) -> None:
        """Rebuild the normal sidebar after a catalog panel, per current mode."""
        self._redefine_panel_active = False
        if self.mode == "annotation":
            self._populate_level_classes()
        else:
            if self.project is not None:
                self._set_validation_level_class_items()
            self._populate_class_checkboxes()

    def _show_class_catalog_panel(self, title: str, note_text: str, on_class) -> None:
        """Swap the sidebar to the active level's catalog as click-to-pick pills.

        Shared by the redefine-assign panel and the change-class panel; ``on_class``
        is called with the chosen class name. ``_redefine_panel_active`` marks the
        sidebar as "swapped" so deselecting restores the normal class list.
        """
        level = self.annotation_level
        self._redefine_panel_active = True
        self._clear_class_checkboxes()
        self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, self._make_section(title))
        note = QLabel(note_text)
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
                button.clicked.connect(lambda _checked=False, name=class_name: on_class(name))
                self.class_list_layout.insertWidget(self.class_list_layout.count() - 1, button)

    def _show_redefine_assign_panel(self, item: dict) -> None:
        """Show the active level's catalog so a click assigns the selected redefine object."""
        entry_id = item.get("redefine_entry_id")
        raw = str(item.get("redefine_raw_class") or str(item.get("label", "")).split(" • ")[0])
        self._show_class_catalog_panel(
            f"ASSIGN “{raw}”",
            "Click a class to reclassify this object (it leaves the redefine list).",
            lambda name: self._resolve_redefine_object(entry_id, name),
        )

    def _show_reclass_panel(self, annotation: dict) -> None:
        """Show the active level's catalog so a click changes the selected object's class."""
        current = levels.class_name_for_category(self.annotation_level, annotation.get("category_id"))
        self._show_class_catalog_panel(
            "CHANGE CLASS",
            f"Selected: {current or '?'} — click a class to reassign it (undoable).",
            lambda name: self._reclassify_selected(annotation, name),
        )

    def _reclassify_selected(self, annotation: dict, class_name: str) -> None:
        """Change ``annotation``'s class on the active level; recorded for undo/redo.

        The object STAYS selected afterwards so you can keep adjusting it — only
        Esc, entering draw mode, or selecting another object deselects.
        """
        level = self.annotation_level
        new_category_id = levels.category_id_for_class(level, class_name)
        if new_category_id is None:
            return
        old_category_id = annotation.get("category_id")
        if old_category_id != new_category_id:
            annotation["category_id"] = new_category_id
            self._undo_stack.append({
                "kind": "reclass",
                "level": level,
                "basename": self._current_image_basename(),
                "annotation": annotation,
                "old_category_id": old_category_id,
                "new_category_id": new_category_id,
                "image_index": self.index,
            })
            self._redo_stack.clear()
            self._mark_dirty_level(level)
            self._refresh_overlay_items()  # keeps the same annotation dict selected
        # Re-show the panel so its "Selected: X" note reflects the new class.
        self._show_reclass_panel(annotation)
        self._update_action_buttons()
        self._update_status_labels()

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
        # Store under the PROJECT's basename for this image (resolved by stable id),
        # so the new object lands on the key the canvas actually renders — not the
        # import filename, which may carry a different prefix (else it "vanishes").
        sid = self._entry_image_id(entry)
        record = self.project.image_record(sid) if sid is not None else None
        basename = (
            os.path.basename(str(record["file_name"])) if record is not None
            else os.path.basename(str(entry.get("file_name", ""))) or self._current_image_basename()
        )
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
        if "rotation" in snapshot:
            annotation["rotation"] = snapshot.get("rotation")

        target_index = record.get("image_index", self.index)
        if self.images and 0 <= target_index < len(self.images) and target_index != self.index:
            self.index = target_index
            self._load_current_image(reset_fit=True)
        else:
            self._refresh_overlay_items()

    def _apply_record(self, record: dict, undo: bool) -> None:
        """Apply one undo/redo record: geometry snapshot, or add/remove an annotation."""
        kind = record.get("kind", "edit")
        if kind == "reclass":
            target_index = record.get("image_index", self.index)
            if self.images and 0 <= target_index < len(self.images) and target_index != self.index:
                self.index = target_index
                self._load_current_image(reset_fit=True)
            record["annotation"]["category_id"] = (
                record["old_category_id"] if undo else record["new_category_id"]
            )
            self._refresh_overlay_items()
            # The class changed membership, so the validation present-class list
            # (built from the classes actually on the image) needs rebuilding.
            if self.project is not None and self.mode == "validation":
                self._set_validation_level_class_items()
                self._populate_class_checkboxes()
            return
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
        self._last_saved_at = time.time()  # every save path funnels through here
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
        """Write the active level's annotations back to its level{N}.json."""
        if not self.dataset or not self.annotation_root:
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
        # Both project modes share the per-level loader (clamp at the last image,
        # never auto-close — closing would discard the project session).
        self._load_current_image_annotation(reset_fit)

    def _update_status_labels(self) -> None:
        self._refresh_canvas_placeholder()
        self._refresh_incomplete_flags()  # keep the amber "empty level" rows current
        if getattr(self, "image_id_label", None) is not None:
            self.image_id_label.setText(self._current_image_id_text())
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
            text += self._save_state_suffix()
            self.status_label.setText(text)
            self.copy_id_button.setEnabled(bool(self.images))
            return
        if not self.dataset:
            self.status_label.setText("No images yet — use the Import menu to add some")
            self.copy_id_button.setEnabled(False)
            return

        self.copy_id_button.setEnabled(True)
        zoom = int(round(self.canvas.zoom * 100))

        # On a project, validation reviews one level; show it and the count of
        # classes present on the current image.
        if self.project is not None:
            level_part = f"L{self.annotation_level} · {levels.level_title(self.annotation_level)}"
            class_count = len(self.current_class_items)  # classes present on this image
        else:
            level_part = None
            class_count = 0

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
        text += self._save_state_suffix()
        self.status_label.setText(text)

    def _handle_escape(self) -> bool:
        """Esc closes whatever tool is active. Returns True if it changed anything.

        Cancels an in-progress drawing, turns OFF draw and edit modes (which also
        unhighlights their toggle buttons), and clears any selection.
        """
        handled = False
        if self.canvas.has_pending_drawing():
            self.canvas.cancel_drawing()
            handled = True
        if self.draw_button.isChecked():
            self.draw_button.setChecked(False)  # disarms drawing + unhighlights the button
            handled = True
        if self.edit_objects_button.isChecked():
            self.edit_objects_button.setChecked(False)  # exits edit mode + unhighlights
            handled = True
        if self.canvas.selected_annotation() is not None or self.canvas.selected_redefine() is not None:
            self.canvas.clear_selection()
            handled = True
        return handled

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # Esc closes the active tool: cancels an in-progress shape, turns OFF draw
        # and edit modes (unchecking their buttons), and deselects. It never quits.
        if event.key() == Qt.Key.Key_Escape:
            if self._handle_escape():
                event.accept()
                return

        # While drawing, Enter finishes a polygon, Backspace drops a vertex. Enter
        # must beat the global next-image shortcut.
        if self.mode == "annotation" and self.canvas.has_pending_drawing():
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.canvas.finish_polygon()
                event.accept()
                return
            if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                # A selected pending point is removed first; plain Backspace otherwise
                # drops the last-placed point (the long-standing behaviour).
                if self.canvas.delete_selected_pending_vertex():
                    event.accept()
                    return
                if key == Qt.Key.Key_Backspace:
                    self.canvas.remove_last_point()
                    event.accept()
                    return

        # Delete/Backspace: a selected polygon VERTEX takes precedence over the
        # whole-object delete (so you can prune a point without losing the object).
        if (self.mode == "annotation" or self.project is not None) and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.canvas.delete_selected_vertex():
                event.accept()
                return
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
        # These pop-outs are now parentless top-level windows, so closing the main
        # window won't auto-close them (and the app wouldn't quit). Close them here.
        for popout in (self.canvas_popout, self.class_popout):
            if popout is not None:
                popout.close()
        self._cleanup_temp_extraction()
        super().closeEvent(event)


def main() -> None:
    """Run the PyQt annotation review application."""
    args = parse_arguments()

    if args.self_test:
        print("Self-test OK: arguments parsed, dependencies imported.")
        return

    # Windowed build has no console: capture all output to a rotating per-session
    # log file instead (best-effort; never blocks startup).
    from . import applog
    applog.install()

    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    app = QApplication(sys.argv)
    app.setApplicationName("Annotation Workbench")
    app.setWindowIcon(QIcon(str(resource_path("assets", "app_icon.png"))))
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
