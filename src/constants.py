"""Application constants and configuration."""

import ctypes
import os


# Application identity / version. Pinned at 1.0 for now; bump here when releasing.
APP_NAME = "Annotation Workbench"
APP_VERSION = "1.2"


def _detect_monitor_size() -> tuple[int, int]:
	"""Detect primary monitor size with a safe fallback."""
	try:
		if os.name == "nt":
			user32 = ctypes.windll.user32
			width = int(user32.GetSystemMetrics(0))
			height = int(user32.GetSystemMetrics(1))
			if width > 0 and height > 0:
				return width, height
	except Exception:
		pass
	return 1920, 1080


def _detect_dpi_scale() -> float:
	"""Detect Windows DPI scaling factor with reasonable fallback."""
	try:
		if os.name == "nt":
			user32 = ctypes.windll.user32
			get_dpi_for_system = getattr(user32, "GetDpiForSystem", None)
			if get_dpi_for_system is not None:
				system_dpi = int(get_dpi_for_system())
				if system_dpi > 0:
					return max(1.0, float(system_dpi) / 96.0)
	except Exception:
		pass
	return 1.0


MONITOR_WIDTH, MONITOR_HEIGHT = _detect_monitor_size()

# UI scale used for spacing and geometry.
DPI_SCALE = _detect_dpi_scale()

# Extra text boost for readability on dense displays.
TEXT_SCALE = max(1.1, min(1.6, DPI_SCALE * 1.1))

# Base viewport and sidebar derived from monitor settings for easy tuning.
VIEWPORT_WIDTH = int(round(MONITOR_WIDTH * 0.69))
VIEWPORT_HEIGHT = int(round(MONITOR_HEIGHT * 0.78))
SIDEBAR_WIDTH = max(280, int(round(420 * DPI_SCALE)))
MIN_ZOOM = 0.1
MAX_ZOOM = 20.0

# Color definitions (BGR format for OpenCV)
LABEL_BACKGROUND = (20, 20, 20)
LABEL_COLOR = (16, 16, 16)
HELP_BACKGROUND = (26, 26, 26)
HELP_COLOR = (248, 248, 248)
SIDEBAR_BACKGROUND = (44, 45, 48)
SIDEBAR_HEADER = (62, 64, 68)
SIDEBAR_TEXT = (248, 248, 248)
SIDEBAR_MUTED_TEXT = (198, 201, 206)
BUTTON_BACKGROUND = (84, 89, 96)
BUTTON_TEXT = (250, 250, 250)

# Layout constants
SIDEBAR_LIST_START_Y = int(round(290 * DPI_SCALE))
SIDEBAR_FOOTER_HEIGHT = int(round(140 * DPI_SCALE))
