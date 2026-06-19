# AGENTS: Chat agent guidance for this repo

Purpose
-------
This file gives concise, actionable guidance for coding agents to get productive in this repository.

Quick entry points
------------------
- **Run locally**: The main application entry is [review.py](review.py). Open it to inspect CLI options and behavior.
- **Build / bundle**: See [build_command.txt](build_command.txt) and [build_review_app.ps1](build_review_app.ps1) for the project's packaging steps (PyInstaller spec at [review_app.spec](review_app.spec)).

What agents should know (concise)
--------------------------------
- **Primary implementation**: PyQt6-based GUI with OpenCV image processing (`src/qt_main.py`)
- **Alternative implementation**: OpenCV-only version also available (`src/main.py`) but not used by default
- **Core dependencies**: PyQt6, OpenCV (cv2), NumPy for image manipulation and annotation rendering
- **Features**: COCO annotation viewer with opacity control, zoom/pan, class filtering, visibility toggling
- **Build**: PyInstaller with spec file (`review_app.spec`) → standalone `Annotation Workbench.exe` under `dist/Annotation Workbench/` (the spec's `name=` sets the exe/output-folder name)
- **Workflow**: Quick checks with `python review.py`; build with `build_review_app.ps1` or `build_command.txt`
- **File placement rule**: Any new file that is part of the source code or is loaded by the application at runtime (config files, assets, data files) must live inside the `src/` folder. Only project-level meta files (spec, build scripts, documentation) belong at the repo root.
- **Distribution model**: The app is always distributed as a single `review_app.exe` built with PyInstaller (`review_app.spec`, one-file mode). Every new runtime file (asset, config, icon, etc.) must be added to the `datas` list in `review_app.spec` or it will be missing for end users. Static assets (SVGs, images) go to their subfolder (e.g. `'assets'`); user-editable configs go to `'.'` so they land in `_MEIPASS` and can be seeded next to the exe on first run via `_ensure_color_config_files()`. Never assume a file will exist on the user's machine — bundle it or generate it at startup.

Common pitfalls and checks
-------------------------
- **GUI windows**: PyQt6 and OpenCV windows may fail in CI/headless environments; test locally or skip GUI tests
- **Missing dependencies**: Requires `pyqt6`, `opencv-python`, `numpy` — install via conda or pip before running
- **File dialogs**: Tkinter fallback for file dialogs if primary method unavailable; handles gracefully
- **DPI awareness**: Windows per-monitor DPI scaling is handled; changes take effect on startup

Suggested next agent customizations
----------------------------------
- **Dependency validation skill**: Create a check that validates required packages (`pyqt6`, `opencv-python`, `numpy`) and reports versions
- **Build verification hook**: Validate that `review_app.spec` matches current code structure and confirm build artifacts exist after `build_review_app.ps1`
- **UI state documentation**: Document keybindings and UI controls in a separate KEYBINDINGS.md for end users

Current state (May 18, 2026)
---------------------------
- ✓ Opacity slider fully functional (default 0.30 = 30%)
- ✓ Image quality optimization (INTER_AREA for zoom-out, INTER_LANCZOS4 for zoom-in)
- ✓ Shadowed visibility for hidden classes (opacity 0.05)
- ✓ Navigation resets visibility state (next/previous commands)
- ✓ PyQt6-based primary implementation stable
- ✓ No active TODO/FIXME/BUG markers in codebase
