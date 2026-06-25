# AGENTS: Chat agent guidance for this repo

Purpose
-------
Concise, actionable guidance for coding agents working in this repository.

What this app is
----------------
**Annotation Workbench** — a PyQt6 desktop app for reviewing **and editing** COCO/Label-Studio
training-data annotations (a 2026 annotation-training workflow). It began as a COCO *viewer* and
has grown into a full annotation **editor** organized around portable **projects** with 3 levels.
It is no longer read-only.

App version is **pinned at 1.0**: `APP_NAME` / `APP_VERSION` live in [src/constants.py](src/constants.py) and surface
(display-only) in the window title, sidebar subtitle, welcome page, and the "How it works" help. Bump
`APP_VERSION` there when releasing.

Quick entry points
------------------
- **Run locally**: `python review.py` → [src/qt_main.py](src/qt_main.py) `main()`. Conda env `review` at
  `<USER_HOME>\.conda\envs\review` has PyQt6 + numpy + cv2.
- **Build / bundle**: [build_review_app.ps1](build_review_app.ps1) (or [build_command.txt](build_command.txt)) runs
  PyInstaller against [review_app.spec](review_app.spec). Output is a **single one-file** exe:
  `dist/Annotation Workbench.exe` (windowed, no console; `name=` in the spec sets the exe name).
  User-editable config (`class_colors*.json`, `shortcuts.json`) lives in **`Documents/Annotation Workbench/`**
  (NOT next to the exe — keeps a desktop-placed exe tidy); seeded/migrated on first frozen run by
  `_ensure_user_config_files()` via `_user_data_dir()`. Source/test runs keep these next to the package.
  Session logs are separate, in `%LOCALAPPDATA%/Annotation Workbench/logs` (`applog.log_dir`).

Architecture (key files)
------------------------
- **Primary implementation**: [src/qt_main.py](src/qt_main.py) (~5,900 lines). Top-level classes:
  `ImageCanvas(QWidget)` (paint + mouse + all geometry editing) and `PyQtAnnotationReview(QMainWindow)`
  (app logic), plus dialogs (`RedefineDialog`, `ShortcutsDialog`, pop-out `ClassListWindow`/`CanvasWindow`, …).
- **Project model**: [src/project.py](src/project.py) — `Project` = the portable folder (create/open/import/
  export, image registry + dedup, level COCO read/write, the `_pending`/`_unmapped` stashes).
- **Level catalogs**: [src/levels.py](src/levels.py) — L1/L2/L3 class lists, colors, group headers, geometry kinds,
  and class↔category/level helpers.
- **Data loading**: [src/data_loading.py](src/data_loading.py) — COCO + Label Studio parsing and `stable_image_id`.
- **Support**: [src/applog.py](src/applog.py) (per-session log files — the windowed build has no console),
  [src/constants.py](src/constants.py) (DPI/constants), [src/utils.py](src/utils.py), [src/rle.py](src/rle.py) (RLE→polygon, no pycocotools).
- **Legacy / unused** (OpenCV-only, not part of the GUI flow): [src/main.py](src/main.py), [src/viewer.py](src/viewer.py),
  [src/drawing.py](src/drawing.py). Don't extend these; the live app is the PyQt6 path above.

Core concepts (read before changing behavior)
---------------------------------------------
- **A project is always required.** The workbench UI only appears with a project open; the old
  no-project loaders were removed. A project is a self-contained folder you can zip and hand to a
  colleague. It **never modifies originals** — it copies images in and writes its own JSONs.
  On disk: `project.json` (manifest + master image registry) + `images/` +
  `annotations/level{1,2,3}.json` (each a valid COCO dict) + `annotations/_pending.json`
  (orphan-image stash) + `annotations/_unmapped.json` (off-catalog "redefine" stash).
- **3 levels**: L1 polygons, **L2 rotated bboxes**, L3 polygons (geometry via `levels.level_geometry`).
  **L2 rotation is lossless** — stored as BOTH a 4-corner `segmentation` quad (source of truth) AND a
  `rotation` degrees field. The COCO `bbox` is the axis-aligned envelope (can go slightly negative on
  rotated boxes that overrun the image edge — the quad keeps the true corners).
- **Identity invariant (critical)**: map any image reference by `stable_image_id`
  (md5 of last-9-chars-of-stem), **never** by raw filename — the same photo re-exports under different
  prefixes. Routing, the red/amber image-row flags, redefine overlays, and resolve all key on it.
  Code deciding "is this image already here?" must compare `stable_image_id` and resolve the store key
  from the registry record's `file_name`. (This has caused regressions — keep it consistent.)
- **Two modes, same per-level data**: Validation (review/fix) and Annotate (draw from scratch), both
  per-level on the same `annotation_store = {level: {basename: [ann]}}`.
- **Off-catalog classes are stashed, not dropped** → resolved via the **Redefine** flow (remap an unknown
  class to an existing one). Unresolved objects render as dashed magenta read-only overlays; images that
  still hold one get a red row in the selector (amber = some level empty for that image).

Rules for adding files
----------------------
- **File placement**: any file that is source code or loaded at runtime (config, assets, data) must live
  inside `src/`. Only project-level meta files (spec, build scripts, docs) belong at the repo root.
- **Bundling (one-file build)**: every new runtime file must be added to the `datas` list in
  [review_app.spec](review_app.spec) or it will be missing for end users. Static assets (SVGs, icons) → an
  `'assets'` subfolder; user-editable configs → `'.'` so they land in `_MEIPASS` and can be seeded next to
  the exe on first run. New modules may also need a `hiddenimports` entry. **Never assume a file exists on
  the user's machine — bundle it or generate it at startup.**

Testing
-------
- Tests are **NOT pytest** (pytest isn't installed). They use a custom `_run_all` and must run headless:
  - `QT_QPA_PLATFORM=offscreen python -m tests.test_project_ui` (offscreen Qt UI tests)
  - `python -m tests.test_project` (pure-stdlib model tests)
- Headless gotchas:
  - Hold a reference to `QApplication` (`app = QApplication.instance() or QApplication(sys.argv)`) — a
    GC'd QApplication makes window construction hard-crash with exit 127 and no output.
  - Modal `_show_message(...).exec()` / `_open_redefine_dialog` **block forever** offscreen — stub them.
  - An exception raised **inside a Qt signal handler** makes PyQt6 hard-abort the process (exit 9, no
    traceback) — looks like the QApplication-GC crash but isn't.
  - Use REAL images for fixtures (`QImage(4,4,...).save(path)`); undecodable images get silently dropped.
    The 4×4 test image means vertex-drag coords must stay within `[0,4]`.

Common pitfalls
---------------
- **GUI/headless**: full GUI needs a display; run UI tests with `QT_QPA_PLATFORM=offscreen`.
- **Dependencies**: requires `pyqt6`, `opencv-python` (cv2), `numpy`. Use the conda `review` env.
- **DPI**: Windows per-monitor DPI scaling is handled at startup; many sizes are scaled by a DPI factor.
- **Native file dialogs**: keep OS-native file pickers (do not replace them to fix theming — only our own
  Qt dialogs need the dark QSS).
- **Headless packaging gaps**: tests don't catch missing `datas`/`hiddenimports` or bad asset paths.
  After build changes, smoke-test the FROZEN exe, not just `python -m`.
