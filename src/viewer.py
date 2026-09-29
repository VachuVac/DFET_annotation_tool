"""Annotation viewer GUI using OpenCV."""

import os
import ctypes
from typing import Callable, cast

import cv2
import numpy as np

from .constants import (
    MONITOR_WIDTH, MONITOR_HEIGHT, DPI_SCALE, TEXT_SCALE, VIEWPORT_WIDTH, VIEWPORT_HEIGHT,
    SIDEBAR_WIDTH, MIN_ZOOM, MAX_ZOOM, LABEL_BACKGROUND, LABEL_COLOR,
    HELP_BACKGROUND, HELP_COLOR, SIDEBAR_BACKGROUND, SIDEBAR_HEADER,
    SIDEBAR_TEXT, SIDEBAR_MUTED_TEXT, BUTTON_BACKGROUND, BUTTON_TEXT,
    SIDEBAR_LIST_START_Y, SIDEBAR_FOOTER_HEIGHT,
)
from .drawing import draw_crisp_text, draw_centered_text, color_for_category


class AnnotationViewer:
    """GUI viewer for COCO annotations."""

    def __init__(self, window_name: str) -> None:
        self.window_name = window_name
        self.image = None
        self.zoom = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self.dragging = False
        self.last_mouse_position = None
        self.title = window_name
        self.class_items = []
        self.visible_by_category = {}
        self.show_points = False
        self.show_labels = True
        self.class_scroll = 0
        self.row_height = max(12, int(round(26 * DPI_SCALE)))
        self.checkbox_hitboxes = []
        self.button_hitboxes = {}
        self.points_hitbox = None
        self.labels_hitbox = None
        self.label_items = []
        self.redraw_requested = False
        self.pending_action = None
        self.raise_window_requested = False

        self.opacity = 0.30
        self.opacity_input_hitbox = None
        self.opacity_input_active = False
        self.opacity_input_buffer = ""

        self.configure_windows_dpi_awareness()

        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.window_name, VIEWPORT_WIDTH + SIDEBAR_WIDTH, VIEWPORT_HEIGHT)
        cv2.setMouseCallback(self.window_name, self.handle_mouse)
        self.maximize_window()

    def configure_windows_dpi_awareness(self) -> None:
        """Try to make the process per-monitor DPI aware to avoid blurry scaling."""
        if os.name != "nt":
            return

        try:
            user32 = ctypes.windll.user32
            set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
            if set_context is not None:
                # PER_MONITOR_AWARE_V2 gives the sharpest text when moving across displays.
                if bool(set_context(ctypes.c_void_p(-4))):
                    return
        except Exception:
            pass

        try:
            shcore = ctypes.windll.shcore
            if shcore.SetProcessDpiAwareness(2) == 0:
                return
        except Exception:
            pass

        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def set_image(self, image: np.ndarray, title: str, reset_view: bool = True) -> None:
        """Set the image to display."""
        self.image = image
        self.title = title
        if reset_view:
            self.reset_view()
        self.raise_window_requested = True

    def set_idle_view(self, title: str = "DFET Annotation tool") -> None:
        """Set idle view (no image)."""
        self.image = None
        self.title = title
        self.raise_window_requested = True

    def set_controls_state(
        self,
        class_items,
        visible_by_category,
        show_points: bool,
        show_labels: bool,
    ) -> None:
        """Update control state."""
        self.class_items = list(class_items)
        self.visible_by_category = visible_by_category
        self.show_points = show_points
        self.show_labels = show_labels

    def request_redraw(self) -> None:
        """Request a redraw on next render."""
        self.redraw_requested = True

    def consume_redraw_request(self) -> bool:
        """Check if redraw was requested and consume the request."""
        if self.redraw_requested:
            self.redraw_requested = False
            return True
        return False

    def request_action(self, action: str) -> None:
        """Request an action."""
        self.pending_action = action

    def consume_action(self) -> str | None:
        """Get pending action and consume it."""
        action = self.pending_action
        self.pending_action = None
        return action

    def set_opacity_percent(self, percent: int) -> None:
        """Set annotation opacity from a 0-100 integer value."""
        value = max(0, min(100, int(percent)))
        self.opacity = value / 100.0

    def begin_opacity_edit(self) -> None:
        """Start editing the opacity percentage."""
        self.opacity_input_active = True
        self.opacity_input_buffer = str(int(round(self.opacity * 100)))

    def commit_opacity_edit(self) -> None:
        """Commit the typed opacity value."""
        if self.opacity_input_buffer:
            try:
                self.set_opacity_percent(int(self.opacity_input_buffer))
            except ValueError:
                pass
        self.opacity_input_active = False
        self.opacity_input_buffer = ""

    def cancel_opacity_edit(self) -> None:
        """Cancel opacity editing."""
        self.opacity_input_active = False
        self.opacity_input_buffer = ""

    def handle_key(self, key: int) -> bool:
        """Handle key input for in-viewer controls."""
        if not self.opacity_input_active:
            return False

        if ord("0") <= key <= ord("9"):
            if len(self.opacity_input_buffer) < 3:
                self.opacity_input_buffer += chr(key)
                self.request_redraw()
            return True

        if key in (8, 127):
            self.opacity_input_buffer = self.opacity_input_buffer[:-1]
            self.request_redraw()
            return True

        if key in (13, 10):
            self.commit_opacity_edit()
            self.request_redraw()
            return True

        if key == 27:
            self.cancel_opacity_edit()
            self.request_redraw()
            return True

        return False

    def is_window_open(self) -> bool:
        """Check if window is still open."""
        try:
            return cv2.getWindowProperty(self.window_name, cv2.WND_PROP_VISIBLE) >= 1
        except Exception:
            return False

    def bring_window_to_front(self) -> None:
        """Bring window to front (Windows only)."""
        if os.name != "nt":
            return

        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, self.title)
            if not hwnd:
                hwnd = user32.FindWindowW(None, self.window_name)
            if hwnd:
                topmost = -1
                no_move = 0x0002
                no_size = 0x0001
                show_window = 0x0040
                user32.ShowWindow(hwnd, 5)
                user32.SetWindowPos(hwnd, topmost, 0, 0, 0, 0, no_move | no_size | show_window)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.BringWindowToTop(hwnd)
        except Exception:
            pass

    def maximize_window(self) -> None:
        """Maximize window (Windows only)."""
        if os.name != "nt":
            return

        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, self.window_name)
            if hwnd:
                user32.ShowWindow(hwnd, 3)
        except Exception:
            pass

    def max_visible_class_rows(self) -> int:
        """Calculate maximum visible class rows."""
        top = SIDEBAR_LIST_START_Y
        footer_top = VIEWPORT_HEIGHT - SIDEBAR_FOOTER_HEIGHT
        available = footer_top - top - 14
        return max(1, available // self.row_height)

    def clamp_class_scroll(self) -> None:
        """Clamp class scroll to valid range."""
        max_scroll = max(0, len(self.class_items) - self.max_visible_class_rows())
        self.class_scroll = max(0, min(self.class_scroll, max_scroll))

    def reset_view(self) -> None:
        """Reset zoom and pan."""
        if self.image is None:
            return

        image_height, image_width = self.image.shape[:2]
        # Default to 1:1 (original resolution) so user can zoom out manually
        # to make the image smaller than the viewport if desired.
        self.zoom = 1.0
        self.zoom = max(self.zoom, MIN_ZOOM)

        # Center the image in the viewport initially
        scaled_width = max(1, int(round(image_width * self.zoom)))
        scaled_height = max(1, int(round(image_height * self.zoom)))
        self.offset_x = -(VIEWPORT_WIDTH - scaled_width) // 2
        self.offset_y = -(VIEWPORT_HEIGHT - scaled_height) // 2
        self.clamp_offset()

    def clamp_offset(self) -> None:
        """Clamp pan offset to valid range."""
        if self.image is None:
            return

        scaled_width = max(1, int(round(self.image.shape[1] * self.zoom)))
        scaled_height = max(1, int(round(self.image.shape[0] * self.zoom)))

        # Allow broad panning in every zoom level, including when image is
        # smaller than viewport, by adding overscroll margins.
        margin_x = VIEWPORT_WIDTH // 2
        margin_y = VIEWPORT_HEIGHT // 2

        min_offset_x = -(VIEWPORT_WIDTH - scaled_width) - margin_x
        max_offset_x = max(0, scaled_width - VIEWPORT_WIDTH) + margin_x
        self.offset_x = max(min_offset_x, min(self.offset_x, max_offset_x))

        min_offset_y = -(VIEWPORT_HEIGHT - scaled_height) - margin_y
        max_offset_y = max(0, scaled_height - VIEWPORT_HEIGHT) + margin_y
        self.offset_y = max(min_offset_y, min(self.offset_y, max_offset_y))

    def handle_mouse(self, event: int, x: int, y: int, flags: int, param) -> None:
        """Handle mouse events."""
        if self.image is None:
            if event == cv2.EVENT_LBUTTONDOWN:
                for button_name, rect in self.button_hitboxes.items():
                    x1, y1, x2, y2 = rect
                    if x1 <= x <= x2 and y1 <= y <= y2:
                        if button_name == "open_folder":
                            self.request_action("open_folder")
                            self.request_redraw()
                        return
            return

        over_sidebar = x >= VIEWPORT_WIDTH

        if over_sidebar and event == cv2.EVENT_MOUSEWHEEL:
            if self.opacity_input_hitbox is not None:
                x1, y1, x2, y2 = self.opacity_input_hitbox
                if x1 <= x <= x2 and y1 <= y <= y2:
                    get_mouse_wheel_delta = cast(Callable[[int], int] | None, getattr(cv2, "getMouseWheelDelta", None))
                    wheel_delta = get_mouse_wheel_delta(flags) if get_mouse_wheel_delta is not None else (1 if flags > 0 else -1)
                    if wheel_delta > 0:
                        self.set_opacity_percent(round(self.opacity * 100) + 1)
                    elif wheel_delta < 0:
                        self.set_opacity_percent(round(self.opacity * 100) - 1)
                    self.request_redraw()
                    return

            get_mouse_wheel_delta = cast(Callable[[int], int] | None, getattr(cv2, "getMouseWheelDelta", None))
            wheel_delta = get_mouse_wheel_delta(flags) if get_mouse_wheel_delta is not None else (1 if flags > 0 else -1)
            if wheel_delta > 0:
                self.class_scroll -= 1
            elif wheel_delta < 0:
                self.class_scroll += 1
            self.clamp_class_scroll()
            self.request_redraw()
            return

        if over_sidebar and event == cv2.EVENT_LBUTTONDOWN:
            self.dragging = False
            self.last_mouse_position = None

            if self.points_hitbox is not None:
                x1, y1, x2, y2 = self.points_hitbox
                if x1 <= x <= x2 and y1 <= y <= y2:
                    self.show_points = not self.show_points
                    self.request_redraw()
                    return

            if self.labels_hitbox is not None:
                x1, y1, x2, y2 = self.labels_hitbox
                if x1 <= x <= x2 and y1 <= y <= y2:
                    self.show_labels = not self.show_labels
                    self.request_redraw()
                    return

            if self.opacity_input_hitbox is not None:
                x1, y1, x2, y2 = self.opacity_input_hitbox
                if x1 <= x <= x2 and y1 <= y <= y2:
                    self.begin_opacity_edit()
                    self.request_redraw()
                    return

            for button_name, rect in self.button_hitboxes.items():
                x1, y1, x2, y2 = rect
                if x1 <= x <= x2 and y1 <= y <= y2:
                    if button_name == "all_on":
                        for category_id, _name in self.class_items:
                            self.visible_by_category[category_id] = True
                    elif button_name == "all_off":
                        for category_id, _name in self.class_items:
                            self.visible_by_category[category_id] = False
                    elif button_name == "reset":
                        self.request_action("reset")
                    elif button_name == "prev":
                        self.request_action("prev")
                    elif button_name == "next":
                        self.request_action("next")
                    elif button_name == "open_folder":
                        self.request_action("open_folder")
                    self.request_redraw()
                    return

            for category_id, rect in self.checkbox_hitboxes:
                x1, y1, x2, y2 = rect
                if x1 <= x <= x2 and y1 <= y <= y2:
                    self.visible_by_category[category_id] = not self.visible_by_category.get(category_id, True)
                    self.request_redraw()
                    return

            return

        if event == cv2.EVENT_LBUTTONDOWN:
            self.dragging = True
            self.last_mouse_position = (x, y)
        elif event == cv2.EVENT_LBUTTONUP:
            self.dragging = False
            self.last_mouse_position = None
        elif event == cv2.EVENT_MOUSEMOVE and self.dragging and self.last_mouse_position is not None:
            last_x, last_y = self.last_mouse_position
            over_sidebar = x >= VIEWPORT_WIDTH

            self.offset_x -= x - last_x
            self.offset_y -= y - last_y
            self.last_mouse_position = (x, y)
            self.clamp_offset()
        elif event == cv2.EVENT_MOUSEWHEEL:
            if over_sidebar:
                return

            get_mouse_wheel_delta = cast(Callable[[int], int] | None, getattr(cv2, "getMouseWheelDelta", None))
            wheel_delta = get_mouse_wheel_delta(flags) if get_mouse_wheel_delta is not None else (1 if flags > 0 else -1)

            if wheel_delta == 0:
                return

            image_x = (self.offset_x + x) / self.zoom
            image_y = (self.offset_y + y) / self.zoom

            zoom_factor = 1.15 if wheel_delta > 0 else 1 / 1.15
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom * zoom_factor))

            self.offset_x = int(round(image_x * self.zoom - x))
            self.offset_y = int(round(image_y * self.zoom - y))
            self.clamp_offset()

    def render(self) -> None:
        """Render the current view."""
        if self.image is None:
            self.render_idle()
            return

        self.clamp_class_scroll()

        scaled_width = max(1, int(round(self.image.shape[1] * self.zoom)))
        scaled_height = max(1, int(round(self.image.shape[0] * self.zoom)))
        interp = cv2.INTER_AREA if self.zoom < 1.0 else cv2.INTER_LANCZOS4
        scaled_image = cv2.resize(self.image, (scaled_width, scaled_height), interpolation=interp)

        canvas = np.full((VIEWPORT_HEIGHT, VIEWPORT_WIDTH + SIDEBAR_WIDTH, 3), 18, dtype=np.uint8)

        source_x1 = max(0, self.offset_x)
        source_y1 = max(0, self.offset_y)
        source_x2 = min(scaled_width, self.offset_x + VIEWPORT_WIDTH)
        source_y2 = min(scaled_height, self.offset_y + VIEWPORT_HEIGHT)

        destination_x1 = max(0, -self.offset_x)
        destination_y1 = max(0, -self.offset_y)

        if source_x2 > source_x1 and source_y2 > source_y1:
            visible = scaled_image[source_y1:source_y2, source_x1:source_x2]
            canvas[
                destination_y1:destination_y1 + visible.shape[0],
                destination_x1:destination_x1 + visible.shape[1]
            ] = visible

        self.draw_help(canvas)
        self.draw_sidebar(canvas)

        # Draw labels and points on the canvas (after image scaled/composited)
        if hasattr(self, "label_items") and self.label_items:
            source_x1 = max(0, self.offset_x)
            source_y1 = max(0, self.offset_y)
            source_x2 = min(scaled_width, self.offset_x + VIEWPORT_WIDTH)
            source_y2 = min(scaled_height, self.offset_y + VIEWPORT_HEIGHT)
            destination_x1 = max(0, -self.offset_x)
            destination_y1 = max(0, -self.offset_y)

            for item in self.label_items:
                if self.show_points:
                    for px, py in item.get("points", []):
                        sx = int(round(px * self.zoom))
                        sy = int(round(py * self.zoom))
                        if sx < source_x1 or sx >= source_x2 or sy < source_y1 or sy >= source_y2:
                            continue

                        dot_x = sx - source_x1 + destination_x1
                        dot_y = sy - source_y1 + destination_y1
                        radius = max(2, int(round(3 * DPI_SCALE)))
                        cv2.circle(canvas, (dot_x, dot_y), radius, (255, 255, 255), thickness=-1, lineType=cv2.LINE_AA)

                if self.show_labels:
                    ox = int(round(item.get("x", 0) * self.zoom))
                    oy = int(round(item.get("y", 0) * self.zoom))
                    if ox < source_x1 or ox >= source_x2 or oy < source_y1 or oy >= source_y2:
                        continue

                    canvas_x = ox - source_x1 + destination_x1
                    canvas_y = oy - source_y1 + destination_y1

                    label = item.get("label", "")
                    color = item.get("color", (80, 80, 80))

                    font = cv2.FONT_HERSHEY_DUPLEX
                    font_scale = 0.5 * DPI_SCALE * TEXT_SCALE
                    font_thickness = max(1, int(round(1 * DPI_SCALE)))
                    pad_x = max(4, int(round(6 * DPI_SCALE)))
                    pad_y = max(3, int(round(4 * DPI_SCALE)))

                    display_label = str(label)
                    max_label_width = max(40, VIEWPORT_WIDTH - 12)
                    while display_label:
                        candidate = display_label
                        if candidate != str(label):
                            candidate = candidate.rstrip() + "..."
                        candidate_width = cv2.getTextSize(candidate, font, font_scale, font_thickness)[0][0] + pad_x * 2
                        if candidate_width <= max_label_width:
                            display_label = candidate
                            break
                        display_label = display_label[:-1]

                    if not display_label:
                        continue

                    (text_width, text_height), baseline = cv2.getTextSize(display_label, font, font_scale, font_thickness)
                    box_width = text_width + pad_x * 2
                    box_height = text_height + baseline + pad_y * 2

                    label_x = min(max(0, canvas_x), max(0, VIEWPORT_WIDTH - box_width - 2))
                    label_y = max(0, canvas_y - box_height - 2)
                    label_tl = (label_x, label_y)
                    label_br = (label_x + box_width, label_y + box_height)

                    cv2.rectangle(canvas, label_tl, label_br, color, thickness=-1)
                    text_org = (label_x + pad_x, label_y + box_height - baseline - pad_y)
                    draw_crisp_text(canvas, display_label, text_org, LABEL_COLOR, 0.5, 1)

        cv2.imshow(self.window_name, canvas)
        if hasattr(cv2, "setWindowTitle"):
            cv2.setWindowTitle(self.window_name, self.title)

        if self.raise_window_requested:
            self.raise_window_requested = False
            self.bring_window_to_front()

    def render_idle(self) -> None:
        """Render idle view."""
        canvas = np.full((VIEWPORT_HEIGHT, VIEWPORT_WIDTH + SIDEBAR_WIDTH, 3), 22, dtype=np.uint8)
        panel_x = VIEWPORT_WIDTH
        cv2.rectangle(canvas, (panel_x, 0), (panel_x + SIDEBAR_WIDTH, VIEWPORT_HEIGHT), SIDEBAR_BACKGROUND, thickness=-1)
        cv2.rectangle(canvas, (panel_x, 0), (panel_x + SIDEBAR_WIDTH, 58), SIDEBAR_HEADER, thickness=-1)

        self.checkbox_hitboxes = []
        self.button_hitboxes = {}
        self.points_hitbox = None
        self.labels_hitbox = None

        draw_crisp_text(canvas, "DFET Annotation tool", (panel_x + 16, 34), SIDEBAR_TEXT, 0.78, 1)

        message = "No dataset loaded"
        text_x = 24
        text_y = VIEWPORT_HEIGHT // 2 - 18
        draw_crisp_text(canvas, message, (text_x, text_y), SIDEBAR_TEXT, 0.92, 1)

        helper_lines = [
            "Use Open folder to load a ZIP export or dataset folder.",
            "After the last image, this screen appears again so you can continue.",
        ]
        for index, line in enumerate(helper_lines):
            draw_crisp_text(canvas, line, (text_x, text_y + 34 + index * 24), SIDEBAR_MUTED_TEXT, 0.52, 1)

        button_height = 34
        button_y1 = 68
        open_folder_rect = (panel_x + 16, button_y1, panel_x + SIDEBAR_WIDTH - 16, button_y1 + button_height)
        self.button_hitboxes["open_folder"] = open_folder_rect

        cv2.rectangle(canvas, (open_folder_rect[0], open_folder_rect[1]), (open_folder_rect[2], open_folder_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        draw_centered_text(canvas, "Open folder", open_folder_rect, BUTTON_TEXT, 0.56, 1)

        footer_text = "Press q or close window to exit"
        draw_crisp_text(canvas, footer_text, (panel_x + 16, VIEWPORT_HEIGHT - 18), SIDEBAR_MUTED_TEXT, 0.50, 1)

        cv2.imshow(self.window_name, canvas)
        if hasattr(cv2, "setWindowTitle"):
            cv2.setWindowTitle(self.window_name, self.title)

        if self.raise_window_requested:
            self.raise_window_requested = False
            self.bring_window_to_front()

    def draw_overlay(self, canvas: np.ndarray) -> None:
        """Draw overlay info."""
        text = "Zoom: {:.2f}x | Drag pan | Wheel zoom | r reset | n next | p previous | q quit".format(self.zoom)
        cv2.putText(canvas, text, (18, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(canvas, text, (18, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)

        filter_text = "Use the sidebar to toggle classes and points"
        cv2.putText(canvas, filter_text, (18, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(canvas, filter_text, (18, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)

    def draw_sidebar(self, canvas: np.ndarray) -> None:
        """Draw sidebar with controls."""
        panel_x = VIEWPORT_WIDTH
        cv2.rectangle(canvas, (panel_x, 0), (panel_x + SIDEBAR_WIDTH, VIEWPORT_HEIGHT), SIDEBAR_BACKGROUND, thickness=-1)
        cv2.rectangle(canvas, (panel_x, 0), (panel_x + SIDEBAR_WIDTH, 58), SIDEBAR_HEADER, thickness=-1)

        self.checkbox_hitboxes = []
        self.button_hitboxes = {}
        self.points_hitbox = None
        self.labels_hitbox = None
        self.opacity_input_hitbox = None

        draw_crisp_text(canvas, "Class Filters", (panel_x + 16, 34), SIDEBAR_TEXT, 0.78, 1)

        open_folder_rect = (panel_x + 16, 68, panel_x + SIDEBAR_WIDTH - 16, 100)
        self.button_hitboxes["open_folder"] = open_folder_rect
        cv2.rectangle(canvas, (open_folder_rect[0], open_folder_rect[1]), (open_folder_rect[2], open_folder_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        draw_centered_text(canvas, "Open folder", open_folder_rect, BUTTON_TEXT, 0.56, 1)

        button_y1 = 108
        button_y2 = 136
        button_padding = 12
        button_width = (SIDEBAR_WIDTH - button_padding * 3) // 2

        all_on_rect = (panel_x + button_padding, button_y1, panel_x + button_padding + button_width, button_y2)
        all_off_rect = (all_on_rect[2] + button_padding, button_y1, all_on_rect[2] + button_padding + button_width, button_y2)
        self.button_hitboxes["all_on"] = all_on_rect
        self.button_hitboxes["all_off"] = all_off_rect

        cv2.rectangle(canvas, (all_on_rect[0], all_on_rect[1]), (all_on_rect[2], all_on_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        cv2.rectangle(canvas, (all_off_rect[0], all_off_rect[1]), (all_off_rect[2], all_off_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        draw_centered_text(canvas, "Show all", all_on_rect, BUTTON_TEXT, 0.54, 1)
        draw_centered_text(canvas, "Hide all", all_off_rect, BUTTON_TEXT, 0.54, 1)

        nav_y1 = 144
        nav_y2 = 172
        nav_button_width = (SIDEBAR_WIDTH - button_padding * 4) // 3
        prev_rect = (panel_x + button_padding, nav_y1, panel_x + button_padding + nav_button_width, nav_y2)
        reset_rect = (prev_rect[2] + button_padding, nav_y1, prev_rect[2] + button_padding + nav_button_width, nav_y2)
        next_rect = (reset_rect[2] + button_padding, nav_y1, reset_rect[2] + button_padding + nav_button_width, nav_y2)

        self.button_hitboxes["prev"] = prev_rect
        self.button_hitboxes["reset"] = reset_rect
        self.button_hitboxes["next"] = next_rect

        cv2.rectangle(canvas, (prev_rect[0], prev_rect[1]), (prev_rect[2], prev_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        cv2.rectangle(canvas, (reset_rect[0], reset_rect[1]), (reset_rect[2], reset_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        cv2.rectangle(canvas, (next_rect[0], next_rect[1]), (next_rect[2], next_rect[3]), BUTTON_BACKGROUND, thickness=-1)
        draw_centered_text(canvas, "Prev", prev_rect, BUTTON_TEXT, 0.50, 1)
        draw_centered_text(canvas, "Reset", reset_rect, BUTTON_TEXT, 0.50, 1)
        draw_centered_text(canvas, "Next", next_rect, BUTTON_TEXT, 0.50, 1)

        opacity_label_y = 190
        draw_crisp_text(canvas, "Fill opacity", (panel_x + 16, opacity_label_y), SIDEBAR_TEXT, 0.54, 1)
        input_y = opacity_label_y + 18
        input_left = panel_x + 16
        input_right = panel_x + 92
        input_height = 20
        self.opacity_input_hitbox = (input_left, input_y - 12, input_right, input_y + 6)

        input_background = (72, 72, 72) if self.opacity_input_active else (56, 56, 56)
        input_border = (255, 190, 100) if self.opacity_input_active else (180, 180, 180)
        cv2.rectangle(canvas, (input_left, input_y - 12), (input_right, input_y + 6), input_background, thickness=-1)
        cv2.rectangle(canvas, (input_left, input_y - 12), (input_right, input_y + 6), input_border, thickness=1)

        display_opacity = self.opacity_input_buffer if self.opacity_input_active and self.opacity_input_buffer else str(int(round(self.opacity * 100)))
        draw_centered_text(canvas, display_opacity, (input_left + 2, input_y - 12, input_right - 2, input_y + 6), SIDEBAR_TEXT, 0.50, 1)
        draw_crisp_text(canvas, "%", (input_right + 8, input_y + 3), SIDEBAR_TEXT, 0.54, 1)

        if self.opacity_input_active:
            draw_crisp_text(canvas, "Enter to apply, Esc to cancel", (panel_x + 16, input_y + 20), SIDEBAR_MUTED_TEXT, 0.40, 1)

        points_row_y = 232
        points_box = (panel_x + 16, points_row_y - 12, panel_x + 32, points_row_y + 4)
        self.points_hitbox = points_box
        cv2.rectangle(canvas, (points_box[0], points_box[1]), (points_box[2], points_box[3]), (230, 230, 230), thickness=1)
        if self.show_points:
            cv2.rectangle(canvas, (points_box[0] + 3, points_box[1] + 3), (points_box[2] - 3, points_box[3] - 3), (230, 230, 230), thickness=-1)
        draw_crisp_text(canvas, "Show polygon points", (panel_x + 42, points_row_y), SIDEBAR_TEXT, 0.54, 1)

        labels_row_y = 254
        labels_box = (panel_x + 16, labels_row_y - 12, panel_x + 32, labels_row_y + 4)
        self.labels_hitbox = labels_box
        cv2.rectangle(canvas, (labels_box[0], labels_box[1]), (labels_box[2], labels_box[3]), (230, 230, 230), thickness=1)
        if self.show_labels:
            cv2.rectangle(canvas, (labels_box[0] + 3, labels_box[1] + 3), (labels_box[2] - 3, labels_box[3] - 3), (230, 230, 230), thickness=-1)
        draw_crisp_text(canvas, "Show class labels", (panel_x + 42, labels_row_y), SIDEBAR_TEXT, 0.54, 1)

        class_count = len(self.class_items)

        first_row_y = SIDEBAR_LIST_START_Y
        max_rows = self.max_visible_class_rows()
        start_index = self.class_scroll
        end_index = min(len(self.class_items), start_index + max_rows)

        for row_index, item_index in enumerate(range(start_index, end_index)):
            category_id, category_name = self.class_items[item_index]
            row_y = first_row_y + row_index * self.row_height

            checkbox_rect = (panel_x + 16, row_y - 13, panel_x + 32, row_y + 3)
            self.checkbox_hitboxes.append((category_id, checkbox_rect))

            cv2.rectangle(canvas, (checkbox_rect[0], checkbox_rect[1]), (checkbox_rect[2], checkbox_rect[3]), (225, 225, 225), thickness=1)
            if self.visible_by_category.get(category_id, True):
                cv2.rectangle(canvas, (checkbox_rect[0] + 3, checkbox_rect[1] + 3), (checkbox_rect[2] - 3, checkbox_rect[3] - 3), (225, 225, 225), thickness=-1)

            swatch_color = color_for_category(int(category_id))
            cv2.rectangle(canvas, (panel_x + 40, row_y - 10), (panel_x + 56, row_y + 2), swatch_color, thickness=-1)

            display_name = category_name
            if len(display_name) > 28:
                display_name = display_name[:25] + "..."
            draw_crisp_text(canvas, display_name, (panel_x + 64, row_y), SIDEBAR_TEXT, 0.52, 1)

        # Draw scrollbar for class list
        if class_count > max_rows:
            list_height = max_rows * self.row_height
            list_top = first_row_y
            list_bottom = list_top + list_height
            scroll_bar_x1 = panel_x + SIDEBAR_WIDTH - 6
            scroll_bar_x2 = panel_x + SIDEBAR_WIDTH - 1
            scroll_bar_height = max(20, int(list_height * max_rows / class_count))
            scroll_pos = int((start_index / max(1, class_count - max_rows)) * (list_height - scroll_bar_height))
            scroll_bar_y1 = list_top + scroll_pos
            scroll_bar_y2 = scroll_bar_y1 + scroll_bar_height
            cv2.rectangle(canvas, (scroll_bar_x1, scroll_bar_y1), (scroll_bar_x2, scroll_bar_y2), (120, 120, 120), thickness=-1)

        footer_top = VIEWPORT_HEIGHT - SIDEBAR_FOOTER_HEIGHT
        cv2.rectangle(canvas, (panel_x, footer_top), (panel_x + SIDEBAR_WIDTH, VIEWPORT_HEIGHT), SIDEBAR_HEADER, thickness=-1)
        if class_count > max_rows:
            scroll_hint = f"Scroll: {start_index + 1}-{end_index} / {class_count}"
            draw_crisp_text(canvas, scroll_hint, (panel_x + 16, footer_top + 20), SIDEBAR_MUTED_TEXT, 0.46, 1)

        # Instructions moved to sidebar (below class list) so they don't obscure image
        instr_start_y = footer_top + 42
        instr_lines = [
            "Zoom: Mouse wheel",
            "Pan: Left drag",
            "Reset: r    Next: n / Enter / Space    Prev: p",
            "Quit: q or close window    Points: t    Labels: l",
        ]
        for i, line in enumerate(instr_lines):
            y = instr_start_y + i * 20
            if y < VIEWPORT_HEIGHT - 8:
                draw_crisp_text(canvas, line, (panel_x + 16, y), SIDEBAR_MUTED_TEXT, 0.48, 1)

    def draw_help(self, canvas: np.ndarray) -> None:
        """Draw help text."""
        line = self.title
        help_scale = 0.58 * DPI_SCALE * TEXT_SCALE
        help_thickness = max(1, int(round(1 * DPI_SCALE)))
        (text_width, text_height), baseline = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, help_scale, help_thickness)
        top_left = (16, VIEWPORT_HEIGHT - text_height - baseline - 12)
        bottom_right = (16 + text_width + 14, VIEWPORT_HEIGHT - 4)
        cv2.rectangle(canvas, top_left, bottom_right, HELP_BACKGROUND, thickness=-1)
        draw_crisp_text(canvas, line, (23, VIEWPORT_HEIGHT - 11), HELP_COLOR, 0.58, 1, font=cv2.FONT_HERSHEY_SIMPLEX)
