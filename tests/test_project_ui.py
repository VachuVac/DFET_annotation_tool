"""Headless UI tests for project mode wiring in qt_main (Stage B).

Runs under the offscreen Qt platform so it needs no display. Modal popups are
neutralised (``_show_message`` is monkeypatched) because ``QMessageBox.exec()``
blocks forever offscreen.

Run from the repo root::

    QT_QPA_PLATFORM=offscreen python -m tests.test_project_ui
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QImage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from src import levels
from src.constants import APP_NAME, APP_VERSION
from src.project import Project
from src.qt_main import (
    ActionPickerDialog,
    ClassBubbleButton,
    CollapsibleClassGroup,
    ImageCanvas,
    KeyCapButton,
    ObjectRowButton,
    PyQtAnnotationReview,
    RedefineDialog,
    SettingsDialog,
    ToggleSwitch,
)

BASE_TITLE = f"{APP_NAME} {APP_VERSION}"

STUB_SIZE = lambda _path: (640, 480)  # noqa: E731


def _make_image(folder: Path, name: str, size: int = 4) -> None:
    # A real, decodable image so the window's QPixmap loader keeps it (validation
    # mode discards images it can't decode). ``size`` defaults to a tiny 4x4; box
    # editing tests pass a bigger one so geometry isn't clamped to the image edge.
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(0xFF4080A0)
    assert image.save(str(folder / name)), f"could not write {name}"


def _window() -> PyQtAnnotationReview:
    window = PyQtAnnotationReview(None)
    window._show_message = lambda *a, **k: None  # type: ignore[assignment]
    # The Redefine dialog auto-pops after an import with unknown classes; stub it
    # so the modal doesn't block headless runs (its logic is tested directly).
    window._open_redefine_dialog = lambda *a, **k: None  # type: ignore[assignment]
    return window


def test_starts_on_welcome_screen() -> None:
    window = _window()
    # Index 0 == welcome page; no project, import entry disabled.
    assert window.view_stack.currentIndex() == 0
    assert window.project is None
    assert window.import_action.isEnabled() is False
    assert window.action_close_project.isEnabled() is False
    assert window.windowTitle() == BASE_TITLE


def test_welcome_screen_help_link_opens_workflow() -> None:
    from PyQt6.QtWidgets import QPushButton

    calls = []
    original = PyQtAnnotationReview._show_workflow_help
    PyQtAnnotationReview._show_workflow_help = lambda self: calls.append(True)  # type: ignore[assignment]
    try:
        window = _window()
        links = [b for b in window.welcome_page.findChildren(QPushButton) if b.text() == "How it works"]
        assert len(links) == 1  # the onboarding link is present
        links[0].click()
        assert calls == [True]  # and wired to the workflow help
    finally:
        PyQtAnnotationReview._show_workflow_help = original  # type: ignore[assignment]


def test_workflow_help_has_en_and_cz_translations() -> None:
    en = PyQtAnnotationReview._HELP_EN
    cz = PyQtAnnotationReview._HELP_CZ
    assert en and cz and en != cz  # both present, genuinely different
    # Same structure: identical bullet count and section count.
    assert en.count("•") == cz.count("•")
    assert en.count("\n\n") == cz.count("\n\n")
    # Czech is actually translated (Czech section headers / diacritics present).
    assert "ÚROVNĚ" in cz and "REŽIMY" in cz and "ÚPRAVY" in cz
    # App UI labels stay in English in BOTH so they match the interface.
    for token in ("Project ▸", "Validation", "Annotate", "Edit objects"):
        assert token in en and token in cz


def test_busy_context_always_restores_cursor() -> None:
    window = _window()
    assert QApplication.overrideCursor() is None  # clean slate

    with window._busy("working…"):
        cursor = QApplication.overrideCursor()
        assert cursor is not None and cursor.shape() == Qt.CursorShape.WaitCursor
    assert QApplication.overrideCursor() is None  # restored on normal exit

    # And restored even when the wrapped work raises (no leaked wait cursor).
    try:
        with window._busy("working…"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert QApplication.overrideCursor() is None


def test_activate_project_switches_to_main_view() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent:
        project = Project.create(parent, "Demo", image_size=STUB_SIZE)
        window._activate_project(project)
        assert window.view_stack.currentIndex() == 1  # main working view
        assert window.import_action.isEnabled() is True
        assert window.action_close_project.isEnabled() is True
        assert "Demo" in window.windowTitle()
        # Empty project: no images yet, annotation_root points into the project.
        assert window.images == []
        assert window.annotation_root == str(project.annotations_dir)
        assert window.images_path == str(project.images_dir)


def test_level_selector_visible_after_project_activation() -> None:
    # Opening a project (mode still defaults to validation) must reveal the
    # L1/L2/L3 selector right away -- without first toggling the mode buttons.
    window = _window()
    assert window.mode == "validation"
    assert window.level_selector_widget.isHidden() is True  # no project yet
    with tempfile.TemporaryDirectory() as parent:
        project = Project.create(parent, "Levels", image_size=STUB_SIZE)
        window._activate_project(project)
        assert window.level_selector_widget.isHidden() is False  # now revealed
        # Closing the project hides it again (back to bare validation chrome).
        window._confirm_discard_unsaved = lambda *a, **k: True  # type: ignore[assignment]
        window._close_project()
        assert window.level_selector_widget.isHidden() is True


def test_import_images_into_open_project() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        src_path = Path(src)
        _make_image(src_path, "scene_000000001.png")
        _make_image(src_path, "scene_000000002.png")

        project = Project.create(parent, "Imp", image_size=STUB_SIZE)
        window._activate_project(project)

        # Drive the import backend directly (the file dialog can't run headless),
        # then refresh the session exactly as _import_image_folder does.
        project.import_images(src)
        project.promote_pending()
        window._apply_project_session(reset_index=False)
        window._update_project_chrome()

        assert len(window.images) == 2
        assert window.view_stack.currentIndex() == 1
        assert "2 images" in window.windowTitle()


def test_mode_switch_keeps_project_session_and_saves_into_project() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        src_path = Path(src)
        _make_image(src_path, "frame_000000001.png")
        _make_image(src_path, "frame_000000002.png")
        project = Project.create(parent, "Modes", image_size=STUB_SIZE)
        window._activate_project(project)
        project.import_images(src)
        project.promote_pending()
        window._apply_project_session(reset_index=False)

        # Switching to annotation mode must NOT wipe the shared project session.
        window._set_mode("annotation")
        assert window.mode == "annotation"
        assert len(window.images) == 2
        assert window.annotation_root == str(project.annotations_dir)

        # Draw something into level 1 and save the current level into the project.
        basename = window._current_image_basename()
        assert basename
        window.annotation_store[1].setdefault(basename, []).append(
            {
                "category_id": 1,
                "segmentation": [[0, 0, 4, 0, 4, 4, 0, 4]],
                "bbox": [0, 0, 4, 4],
                "area": 16.0,
                "rotation": 0.0,
                "iscrowd": 0,
            }
        )
        ok, count, _name = window._write_level_file(1)
        assert ok and count == 1
        level_path = project.level_json_path(1)
        assert level_path.is_file()  # written into the project, not the cwd

        # Switching back to validation still has the images.
        window._set_mode("validation")
        assert window.mode == "validation"
        assert len(window.images) == 2


def test_close_project_returns_to_welcome() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent:
        project = Project.create(parent, "Bye", image_size=STUB_SIZE)
        window._activate_project(project)
        assert window.view_stack.currentIndex() == 1
        window._close_project()
        assert window.project is None
        assert window.view_stack.currentIndex() == 0
        assert window.windowTitle() == BASE_TITLE


def _project_with_image(window, parent, src, basename="scene_000000001.png", size=4):
    """Create + activate a project containing one real image; return (project, basename)."""
    _make_image(Path(src), basename, size)
    project = Project.create(parent, "Route", image_size=STUB_SIZE)
    window._activate_project(project)
    project.import_images(src)
    window._apply_project_session(reset_index=False)
    return project, basename


def test_route_annotations_into_levels_and_stash_off_catalog() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        annotations = {
            1: [
                {"category_id": 100, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100},
                {"category_id": 200, "bbox": [1, 1, 4, 4], "area": 16},
                {"category_id": 999, "bbox": [0, 0, 2, 2], "area": 4},  # off-catalog -> stashed
            ]
        }
        categories = {100: "road_asphalt", 200: "high_vegetation", 999: "banana"}
        window._route_and_commit_annotations(images, annotations, categories)

        assert len(project.read_level(1).get(basename, [])) == 1  # polygon -> L1
        assert len(project.read_level(2).get(basename, [])) == 1  # box -> L2
        assert project.read_level(3) == {}
        # The unknown class is set aside (not dropped) for Redefine.
        assert project.unmapped_class_counts() == {"banana": 1}


def test_route_orphan_annotation_is_stashed() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, _basename = _project_with_image(window, parent, src)
        ghost = "ghost_000000077.png"  # not in the project
        images = [{"id": 5, "file_name": ghost}]
        annotations = {5: [{"category_id": 1, "segmentation": [[0, 0, 9, 0, 9, 9, 0, 9]], "bbox": [0, 0, 9, 9], "area": 81}]}
        window._route_and_commit_annotations(images, annotations, {1: "road_asphalt"})

        assert len(project.read_pending()) == 1
        assert project.read_level(1).get(ghost) is None


def test_route_matches_present_image_by_stable_id_not_filename() -> None:
    # The same photo re-exported under a different prefix shares a stable_image_id
    # but NOT a filename. Routing must recognise it as already present (by id) and
    # land the annotation on the registry's image -- never stash it as an orphan.
    from src.data_loading import stable_image_id

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        registered = "aaaaaaaa-4DDN-BNFL.png"  # what the project already holds
        reexported = "bbbbbbbb-4DDN-BNFL.png"  # same trailing photo-id, new prefix
        assert stable_image_id(registered) == stable_image_id(reexported)
        project, _ = _project_with_image(window, parent, src, basename=registered)

        images = [{"id": 5, "file_name": reexported}]
        annotations = {5: [{"category_id": 200, "bbox": [1, 1, 4, 4], "area": 16}]}
        window._route_and_commit_annotations(images, annotations, {200: "high_vegetation"})

        # Lands on the registry's image, nothing stashed.
        assert len(project.read_level(2).get(registered, [])) == 1
        assert project.read_pending() == []


def test_open_project_rescues_stuck_orphans() -> None:
    # Residue of the old routing bug: an orphan stashed for an image that IS in the
    # project (matching stable_image_id). Opening the project promotes it.
    from src.data_loading import stable_image_id

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        present = "cccccccc-7ZZ-QPQR.png"
        _make_image(Path(src), present)
        project = Project.create(parent, "Rescue", image_size=STUB_SIZE)
        project.import_images(src)
        project.stash_annotations(
            [
                {
                    "level": 2,
                    "stable_image_id": stable_image_id(present),
                    "file_name": "dddddddd-7ZZ-QPQR.png",  # different prefix, same id
                    "annotation": {"category_id": 1, "bbox": [0, 0, 3, 3], "area": 9},
                }
            ]
        )
        assert len(project.read_pending()) == 1

        window._activate_project(project)  # promote_pending runs on open
        assert project.read_pending() == []
        assert len(project.read_level(2).get(present, [])) == 1


def _conflict_setup(window, parent, src):
    project, basename = _project_with_image(window, parent, src)
    # Pre-existing L1 annotation for this image.
    project.write_level(1, {basename: [{"category_id": 1, "segmentation": [[0, 0, 1, 0, 1, 1, 0, 1]], "bbox": [0, 0, 1, 1], "area": 1}]})
    images = [{"id": 1, "file_name": basename}]
    annotations = {1: [{"category_id": 100, "segmentation": [[0, 0, 5, 0, 5, 5, 0, 5]], "bbox": [0, 0, 5, 5], "area": 25}]}
    return project, basename, images, annotations, {100: "road_asphalt"}


def test_conflict_close_aborts_whole_import() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename, images, annotations, cats = _conflict_setup(window, parent, src)
        window._ask_conflict_resolution = lambda _n: "close"  # type: ignore[assignment]
        window._route_and_commit_annotations(images, annotations, cats)
        assert len(project.read_level(1).get(basename, [])) == 1  # untouched


def test_conflict_append_and_replace() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename, images, annotations, cats = _conflict_setup(window, parent, src)
        window._ask_conflict_resolution = lambda _n: "append"  # type: ignore[assignment]
        window._route_and_commit_annotations(images, annotations, cats)
        assert len(project.read_level(1).get(basename, [])) == 2  # existing + imported

    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        window2 = _window()
        project, basename, images, annotations, cats = _conflict_setup(window2, parent, src)
        window2._ask_conflict_resolution = lambda _n: "replace"  # type: ignore[assignment]
        window2._route_and_commit_annotations(images, annotations, cats)
        assert len(project.read_level(1).get(basename, [])) == 1  # only the imported one


def test_zip_import_end_to_end() -> None:
    from src.data_loading import load_coco_data, resolve_dataset_paths

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as work:
        work_path = Path(work)
        staging = work_path / "export"
        (staging / "images").mkdir(parents=True)
        _make_image(staging / "images", "pic_000000001.png")
        _make_image(staging / "images", "pic_000000002.png")
        coco = {
            "images": [
                {"id": 1, "file_name": "pic_000000001.png", "width": 4, "height": 4},
                {"id": 2, "file_name": "pic_000000002.png", "width": 4, "height": 4},
            ],
            "categories": [{"id": 1, "name": "road_asphalt"}, {"id": 2, "name": "high_vegetation"}],
            "annotations": [
                {"id": 1, "image_id": 1, "category_id": 1, "segmentation": [[0, 0, 4, 0, 4, 4, 0, 4]], "bbox": [0, 0, 4, 4], "area": 16},
                {"id": 2, "image_id": 2, "category_id": 2, "bbox": [1, 1, 2, 2], "area": 4},
            ],
        }
        (staging / "result.json").write_text(json.dumps(coco), encoding="utf-8")

        zip_path = work_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            for file in staging.rglob("*"):
                if file.is_file():
                    archive.write(file, file.relative_to(staging))

        project = Project.create(parent, "Zip", image_size=STUB_SIZE)
        window._activate_project(project)

        # Replicate _import_zip's body without the (headless-unfriendly) file dialog.
        images_path, annotations_path, temp = resolve_dataset_paths(str(zip_path))
        project.import_images(images_path, source="zip:export.zip")
        project.promote_pending()
        images, _by_id, anns_by_id, cats = load_coco_data(annotations_path, images_path)
        if temp is not None:
            temp.cleanup()
        window._route_and_commit_annotations(images, anns_by_id, cats)

        assert len(project.registry_images()) == 2
        assert len(project.read_level(1).get("pic_000000001.png", [])) == 1  # polygon -> L1
        assert len(project.read_level(2).get("pic_000000002.png", [])) == 1  # box -> L2


def test_empty_overlay_shows_when_project_has_no_image() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project = Project.create(parent, "Overlay", image_size=STUB_SIZE)
        window._activate_project(project)
        # Empty project: the 3-button overlay is shown, annotations button disabled.
        assert not window.empty_overlay.isHidden()
        assert window.empty_annotations_button.isEnabled() is False

        _make_image(Path(src), "o_000000001.png")
        project.import_images(src)
        window._apply_project_session(reset_index=False)
        window._update_project_chrome()
        # With an image loaded, the overlay is hidden and annotations is enabled.
        assert window.empty_overlay.isHidden()
        assert window.empty_annotations_button.isEnabled() is True

        window._close_project()
        assert window.empty_overlay.isHidden()  # welcome screen instead


def test_drop_report_names_classes_and_reasons() -> None:
    window = _window()
    captured: dict[str, str] = {}
    window._show_message = lambda text, *a, **k: captured.__setitem__("msg", text)  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        annotations = {
            1: [
                {"category_id": 9, "bbox": [0, 0, 2, 2], "area": 4},  # off-catalog
                {"category_id": 1, "segmentation": {"size": [8, 8], "counts": "x"}},  # RLE-only -> no geometry
            ]
        }
        window._route_and_commit_annotations(images, annotations, {9: "banana", 1: "road_asphalt"})

        message = captured["msg"]
        assert "set aside for Redefine" in message
        assert "banana" in message  # the off-catalog class is named
        assert "no usable geometry" in message
        assert "road_asphalt" in message  # the geometry-less class is named


def test_redefine_remap_promotes_into_level() -> None:
    from src import levels

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        # Import a polygon annotation of an unknown class -> stashed.
        images = [{"id": 1, "file_name": basename}]
        annotations = {1: [{"category_id": 5, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}]}
        window._route_and_commit_annotations(images, annotations, {5: "truck_undamaged"})
        assert project.unmapped_class_counts() == {"truck_undamaged": 1}
        assert window.action_redefine.isEnabled()
        assert "(1)" in window.action_redefine.text()

        # Redefine: map it to an existing L3 class, then promote.
        project.set_class_remap("truck_undamaged", 3, "medium_truck_undamaged")
        result = window._promote_unmapped()
        assert result == {"promoted": 1, "remaining": 0}
        store = project.read_level(3)
        assert len(store.get(basename, [])) == 1
        assert store[basename][0]["category_id"] == levels.category_id_for_class(3, "medium_truck_undamaged")
        assert project.unmapped_class_counts() == {}


def test_redefine_remap_persists_and_auto_routes_on_reimport() -> None:
    window = _window()
    window._ask_conflict_resolution = lambda _n: "append"  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        project.set_class_remap("truck_undamaged", 3, "medium_truck_undamaged")
        # Saved remap round-trips through project.json.
        reopened = Project.open(str(project.root), image_size=STUB_SIZE)
        assert reopened.remap_for("truck_undamaged") == {"level": 3, "target": "medium_truck_undamaged"}

        # A fresh import of the same unknown class auto-routes (no stash).
        images = [{"id": 1, "file_name": basename}]
        annotations = {1: [{"category_id": 5, "segmentation": [[1, 1, 7, 1, 7, 7, 1, 7]], "bbox": [1, 1, 6, 6], "area": 36}]}
        window._route_and_commit_annotations(images, annotations, {5: "truck_undamaged"})
        assert project.unmapped_class_counts() == {}
        assert len(project.read_level(3).get(basename, [])) == 1


def test_level_only_park_renders_highlighted_overlay() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        annotations = {1: [{"category_id": 5, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}]}
        window._route_and_commit_annotations(images, annotations, {5: "truck_undamaged"})

        # Park the class in level 3 WITHOUT choosing a class.
        project.set_unmapped_level("truck_undamaged", 3)
        assert project.unmapped_assigned_levels() == {"truck_undamaged": 3}
        # It's NOT committed to the level file...
        assert project.read_level(3) == {}
        # ...but renders as a highlighted, read-only overlay in level 3.
        window._set_mode("annotation")
        window._set_level(3)
        overlays = window._pending_redefine_overlays(3)
        assert len(overlays) == 1
        assert overlays[0]["pending_redefine"] is True
        assert overlays[0]["editable"] is False
        assert "redefine" in overlays[0]["label"]
        # Not shown in a different level.
        assert window._pending_redefine_overlays(1) == []


def test_unassigned_redefine_shows_on_every_level() -> None:
    # Until it is parked in a level, a redefinable object must be visible on ALL
    # levels; once parked, it shows only in that level.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        annotations = {1: [{"category_id": 5, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}]}
        window._route_and_commit_annotations(images, annotations, {5: "truck_undamaged"})
        window._set_mode("annotation")

        # Not parked anywhere yet -> visible on every level.
        for lvl in (1, 2, 3):
            assert len(window._pending_redefine_overlays(lvl)) == 1, lvl

        # Park it in level 3 -> now only level 3 shows it.
        project.set_unmapped_level("truck_undamaged", 3)
        assert len(window._pending_redefine_overlays(3)) == 1
        assert window._pending_redefine_overlays(1) == []
        assert window._pending_redefine_overlays(2) == []


def test_image_selector_items_are_numbered() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _make_image(Path(src), "scene_000000001.png")
        _make_image(Path(src), "scene_000000002.png")
        project = Project.create(parent, "Nums", image_size=STUB_SIZE)
        window._activate_project(project)
        project.import_images(src)
        window._apply_project_session(reset_index=False)

        texts = [window.image_selector.itemText(i) for i in range(window.image_selector.count())]
        assert len(texts) == 2
        # Each row carries its "position/total" prefix (matching the status bar).
        assert texts[0].startswith("1/2") and texts[1].startswith("2/2")
        joined = " ".join(texts)
        assert "scene_000000001.png" in joined and "scene_000000002.png" in joined


def test_images_shown_in_windows_natural_order() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        # Codes chosen to distinguish natural from plain sort: "3SAEE" vs "21SRW".
        # Windows Explorer (natural) order puts 3SAEE before 21SRW (3 < 21); a plain
        # string sort would do the opposite ('2' < '3').
        _make_image(Path(src), "21SRW.png")
        _make_image(Path(src), "3SAEE.png")
        project = Project.create(parent, "Order", image_size=STUB_SIZE)
        window._activate_project(project)
        project.import_images(src)
        window._apply_project_session(reset_index=False)
        order = [os.path.basename(rec["file_name"]) for rec in window.images]
        assert order == ["3SAEE.png", "21SRW.png"], order
        # The selector follows the same order.
        texts = [window.image_selector.itemText(i) for i in range(window.image_selector.count())]
        assert "3SAEE.png" in texts[0] and "21SRW.png" in texts[1]


def test_image_selector_amber_flags_images_with_an_empty_level() -> None:
    from src.qt_main import RedefineRowDelegate

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _routed_project_window(window, parent, src)  # L1 + L2 filled, L3 EMPTY
        window._populate_image_selector()
        sel = window.image_selector
        pos = next(p for p in range(sel.count()) if sel.itemData(p) == 0)

        # L3 has nothing yet -> the row is flagged amber (incomplete).
        assert sel.itemData(pos, RedefineRowDelegate.INCOMPLETE_ROLE) is True

        # Fill L3 for this image -> a status refresh clears the amber flag.
        window.annotation_store[3].setdefault(basename, []).append(
            {"category_id": 1, "segmentation": [[0, 0, 5, 0, 5, 5, 0, 5]], "bbox": [0, 0, 5, 5], "area": 25}
        )
        window._update_status_labels()
        assert sel.itemData(pos, RedefineRowDelegate.INCOMPLETE_ROLE) in (None, False)


def test_vertex_editable_flag_is_polygon_only() -> None:
    # Add/remove vertices is allowed on TRUE polygons (L1/L3) only; an axis box and
    # an oriented (rotated) L2 box must keep their 4-corner shape.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        assert window._build_annotation_overlay_items([poly], 1)[0]["vertex_editable"] is True
        assert window._build_annotation_overlay_items([poly], 3)[0]["vertex_editable"] is True

        box = {"category_id": 1, "bbox": [0, 0, 10, 10], "area": 100, "rotation": 0}
        item_box = window._build_annotation_overlay_items([box], 2)[0]
        assert item_box["shape"] == "bbox" and item_box.get("vertex_editable") is not True

        obox = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100, "rotation": 30}
        item_obox = window._build_annotation_overlay_items([obox], 2)[0]
        assert item_obox["shape"] == "polygon" and item_obox.get("vertex_editable") is not True


def test_delete_selected_vertex_guard_and_undo() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)
        ann = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 5, 15, 0, 10]], "bbox": [0, 0, 10, 15], "area": 1}
        window.annotation_store[1].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        window.canvas.set_edit_enabled(True)

        item = next(it for it in window.canvas._overlay_items if it.get("annotation") is ann)
        window.canvas._selected_vertex = (item, 3)
        assert window.canvas.delete_selected_vertex() is True
        assert len(ann["segmentation"][0]) == 8  # 5 points -> 4

        window._undo()  # restores the removed vertex
        assert len(ann["segmentation"][0]) == 10

        # A triangle refuses further removal (polygon needs >=3 points).
        tri = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 5, 10]], "bbox": [0, 0, 10, 10], "area": 1}
        window.annotation_store[1][basename] = [tri]
        window._refresh_overlay_items()
        tri_item = next(it for it in window.canvas._overlay_items if it.get("annotation") is tri)
        window.canvas._selected_vertex = (tri_item, 0)
        assert window.canvas.delete_selected_vertex() is False
        assert len(tri["segmentation"][0]) == 6


def test_insert_vertex_on_nearest_edge() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)
        ann = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        window.canvas.set_edit_enabled(True)
        window.canvas._set_selected(ann)  # handles/edges are exposed for the SELECTED object only
        assert window.canvas.has_image  # _fit_display_rect needs a loaded pixmap

        # Screen point at the midpoint of the top edge (image (5, 0)).
        ix, iy, _w, _h = window.canvas._fit_display_rect()
        z = window.canvas._zoom
        pos = QPointF(ix + 5 * z, iy + 0 * z)
        assert window.canvas.insert_vertex_at(pos) is True
        assert len(ann["segmentation"][0]) == 10  # 4 points -> 5
        # The new vertex sits on the top edge (y ~ 0) and is selected.
        assert window.canvas._selected_vertex is not None
        window._undo()
        assert len(ann["segmentation"][0]) == 8


def test_vertex_handles_are_selected_object_only() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)
        a = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        b = {"category_id": 1, "segmentation": [[20, 20, 30, 20, 30, 30, 20, 30]], "bbox": [20, 20, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).extend([a, b])
        window._refresh_overlay_items()
        canvas = window.canvas
        canvas.set_edit_enabled(True)

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        corner_a = QPointF(ix + 0 * z, iy + 0 * z)   # a corner of object A
        corner_b = QPointF(ix + 20 * z, iy + 20 * z)  # a corner of object B

        # Nothing selected -> no grabbable handle anywhere (handles are hidden).
        assert canvas._hit_test_vertex(corner_a) is None

        # Select A -> only A's corners are grabbable; B's stay inert.
        canvas._set_selected(a)
        hit = canvas._hit_test_vertex(corner_a)
        assert hit is not None and hit[0].get("annotation") is a
        assert canvas._hit_test_vertex(corner_b) is None

        # Selecting B flips it: now only B's corners are grabbable.
        canvas._set_selected(b)
        assert canvas._hit_test_vertex(corner_a) is None
        hit_b = canvas._hit_test_vertex(corner_b)
        assert hit_b is not None and hit_b[0].get("annotation") is b


class _FakeMouse:
    """Minimal stand-in for QMouseEvent for headless press/move/release tests."""

    def __init__(self, x: float, y: float, button, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        self._p = QPointF(x, y)
        self._b = button
        self._m = modifiers

    def button(self):
        return self._b

    def buttons(self):
        return self._b

    def position(self):
        return self._p

    def modifiers(self):
        return self._m


def test_pending_polygon_point_move_select_delete_insert() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        canvas = window.canvas
        assert canvas.has_image
        window._set_mode("annotation")
        window._set_level(1)
        canvas.set_draw_shape("polygon", (255, 255, 255))
        canvas._poly_points = [(0.0, 0.0), (3.0, 0.0), (3.0, 3.0), (0.0, 3.0)]

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom

        def screen(px, py):
            return ix + px * z, iy + py * z

        # Hit-test finds the point at image (3, 0).
        sx, sy = screen(3, 0)
        assert canvas._hit_test_pending_vertex(QPointF(sx, sy)) == 1

        # Full grab→drag→release moves that point (the reported "can't move" case).
        canvas.mousePressEvent(_FakeMouse(sx, sy, Qt.MouseButton.LeftButton))
        assert canvas._drag_pending_index == 1
        tx, ty = screen(2, 2)
        canvas.mouseMoveEvent(_FakeMouse(tx, ty, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(tx, ty, Qt.MouseButton.LeftButton))
        assert canvas._drag_pending_index is None
        assert canvas._poly_points[1] != (3.0, 0.0)  # it moved
        assert len(canvas._poly_points) == 4  # a move never adds/removes points

        # Right-click inserts on the nearest edge (between points 2 and 3).
        mx, my = screen(1.5, 3)
        assert canvas._insert_pending_vertex_at(QPointF(mx, my)) is True
        assert len(canvas._poly_points) == 5
        assert canvas._selected_pending_index is not None

        # Delete removes the selected pending point.
        assert canvas.delete_selected_pending_vertex() is True
        assert len(canvas._poly_points) == 4


def test_drawing_preview_segment_hidden_while_dragging_point() -> None:
    from PyQt6.QtGui import QPainter

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        canvas = window.canvas
        window._set_mode("annotation")
        window._set_level(1)
        canvas.set_draw_shape("polygon", (255, 255, 255))
        canvas._poly_points = [(0.5, 0.5), (3.5, 0.5), (3.5, 3.5)]
        canvas._cursor_image = (2.0, 3.5)
        canvas._selected_pending_index = None

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # A point on the cursor-preview segment (last point -> cursor), clear of the
        # placed outline and point markers.
        lx, ly = canvas._poly_points[-1]
        cx, cy = canvas._cursor_image
        mx = int(round(ix + (lx + cx) / 2 * z))
        my = int(round(iy + (ly + cy) / 2 * z))

        def render() -> QImage:
            img = QImage(canvas.width(), canvas.height(), QImage.Format.Format_RGB32)
            img.fill(0)
            painter = QPainter(img)
            canvas._draw_pending(painter, ix, iy)
            painter.end()
            return img

        # Placing points: the rubber-band preview segment IS drawn.
        canvas._drag_pending_index = None
        assert render().pixelColor(mx, my).value() > 0

        # Dragging an already-placed point: the preview segment is suppressed.
        canvas._drag_pending_index = 1
        assert render().pixelColor(mx, my).value() == 0


def test_preview_segment_returns_only_after_move_post_release() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        canvas = window.canvas
        window._set_mode("annotation")
        window._set_level(1)
        canvas.set_draw_shape("polygon", (255, 255, 255))
        canvas._poly_points = [(0.5, 0.5), (3.5, 0.5), (3.5, 3.5)]

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom

        def screen(px, py):
            return ix + px * z, iy + py * z

        # Grab the last placed point and drag it.
        sx, sy = screen(3.5, 3.5)
        canvas.mousePressEvent(_FakeMouse(sx, sy, Qt.MouseButton.LeftButton))
        assert canvas._drag_pending_index == 2
        tx, ty = screen(2.5, 2.5)
        canvas.mouseMoveEvent(_FakeMouse(tx, ty, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(tx, ty, Qt.MouseButton.LeftButton))

        # On release the rubber-band cursor is cleared (no preview snaps back to the
        # grab position) — it must NOT reappear until the mouse actually moves.
        assert canvas._cursor_image is None

        # A subsequent mouse move repopulates it, so the preview returns then.
        mvx, mvy = screen(1.0, 1.0)
        canvas.mouseMoveEvent(_FakeMouse(mvx, mvy, Qt.MouseButton.NoButton))
        assert canvas._cursor_image is not None


def test_rotate_selected_l2_box_drag_and_undo() -> None:
    import math

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        # 64px image so the box + its rotation stay inside it (the corner clamp).
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(2)  # L2 = rotated boxes
        canvas = window.canvas
        assert canvas.has_image

        # An axis-aligned L2 box centred at (5, 5); no segmentation yet.
        ann = {"category_id": 1, "bbox": [0, 0, 10, 10], "area": 100, "rotation": 0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        canvas.set_edit_enabled(True)
        canvas._set_selected(ann)

        item = canvas._rotation_target()
        assert item is not None and item["shape"] == "bbox"  # offered for L2 boxes
        (hx, hy), _mid = canvas._rotation_handle_image_pos(item)
        assert abs(hx - 5.0) < 1e-6 and hy < 0.0  # handle floats straight above the box

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # Grab the handle (straight up = -90°), drag to the right of centre (0°):
        # a +90° turn around the centre.
        canvas.mousePressEvent(_FakeMouse(ix + hx * z, iy + hy * z, Qt.MouseButton.LeftButton))
        assert canvas._rotate_item is item
        off = math.hypot(hx - 5.0, hy - 5.0)
        tx, ty = 5.0 + off, 5.0
        canvas.mouseMoveEvent(_FakeMouse(ix + tx * z, iy + ty * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + tx * z, iy + ty * z, Qt.MouseButton.LeftButton))

        assert canvas._rotate_item is None
        assert abs(ann["rotation"] - 90.0) < 1e-3        # base 0 + 90°
        assert ann["segmentation"] is not None           # now a real quad
        assert item["shape"] == "polygon"                # renders/edits as a quad
        # A 90° turn of the unit square maps corner (0,0) -> (10,0).
        quad = ann["segmentation"][0]
        assert abs(quad[0] - 10.0) < 1e-3 and abs(quad[1] - 0.0) < 1e-3

        window._undo()  # back to the axis-aligned box
        assert ann["rotation"] == 0
        assert ann.get("segmentation") is None


def test_rotated_box_vertex_drag_stays_rectangular() -> None:
    import math

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(2)
        canvas = window.canvas

        # A box rotated 30° about its centre (2, 2) — kept inside the 4x4 test
        # image so the drag target isn't clamped. Stored as a 4-corner quad.
        cx, cy = 2.0, 2.0
        theta = math.radians(30)
        cos_t, sin_t = math.cos(theta), math.sin(theta)

        def rot(px, py):
            dx, dy = px - cx, py - cy
            return (cx + dx * cos_t - dy * sin_t, cy + dx * sin_t + dy * cos_t)

        quad = [rot(1, 1), rot(3, 1), rot(3, 3), rot(1, 3)]
        seg = [c for p in quad for c in p]
        ann = {"category_id": 1, "segmentation": [seg], "bbox": [1, 1, 2, 2],
               "area": 4, "rotation": 30.0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        canvas.set_edit_enabled(True)
        canvas._set_selected(ann)

        item = canvas._selected_overlay_item()
        assert item is not None and item["shape"] == "polygon" and item["rotatable"]

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # Grab corner 0 and drag it well off its rectangular position; a polygon
        # would warp, a box must stay rectangular.
        p0x, p0y = quad[0]
        canvas.mousePressEvent(_FakeMouse(ix + p0x * z, iy + p0y * z, Qt.MouseButton.LeftButton))
        assert canvas._drag_vertex is not None and canvas._drag_vertex[1] == 0
        tx, ty = p0x - 0.5, p0y - 0.3
        canvas.mouseMoveEvent(_FakeMouse(ix + tx * z, iy + ty * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + tx * z, iy + ty * z, Qt.MouseButton.LeftButton))

        pts = item["points"]
        # The opposite corner (index 2) is pinned.
        assert abs(pts[2][0] - quad[2][0]) < 1e-6 and abs(pts[2][1] - quad[2][1]) < 1e-6
        # The dragged corner landed exactly where the cursor was.
        assert abs(pts[0][0] - tx) < 1e-6 and abs(pts[0][1] - ty) < 1e-6
        # It is still a true rectangle: right angle at corner 1, equal opposite sides.
        e01 = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        e12 = (pts[2][0] - pts[1][0], pts[2][1] - pts[1][1])
        assert abs(e01[0] * e12[0] + e01[1] * e12[1]) < 1e-4   # perpendicular edges
        e32 = (pts[2][0] - pts[3][0], pts[2][1] - pts[3][1])
        assert abs(e01[0] - e32[0]) < 1e-4 and abs(e01[1] - e32[1]) < 1e-4  # parallel & equal
        # Orientation is preserved (edge 0->1 keeps the 30° box axis direction).
        ang = math.degrees(math.atan2(e01[1], e01[0])) % 180
        assert abs(ang - 30.0) < 1e-3
        assert abs(ann["rotation"] - 30.0) < 1e-6  # rotation unchanged by a resize


def test_move_whole_l2_box_drag_and_undo() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        # 64px image so a 10x10 box can move +3/+2 without hitting the edge clamp.
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(2)  # L2 boxes are movable whole
        canvas = window.canvas
        assert canvas.has_image

        ann = {"category_id": 1, "bbox": [0, 0, 10, 10], "area": 100, "rotation": 0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        canvas.set_edit_enabled(True)

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # Press the box body (image 5,5) and drag by (+3, +2) image units.
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is not None
        assert canvas.selected_annotation() is ann  # a body-press also selects it
        canvas.mouseMoveEvent(_FakeMouse(ix + 8 * z, iy + 7 * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + 8 * z, iy + 7 * z, Qt.MouseButton.LeftButton))

        assert canvas._box_move_item is None
        assert ann["bbox"] == [3.0, 2.0, 10.0, 10.0]  # whole box translated
        assert ann["area"] == 100                      # translation preserves area

        window._undo()
        assert ann["bbox"] == [0, 0, 10, 10]

        # A polygon body press first only SELECTS (it isn't selected yet); moving it
        # whole needs a second body-drag — see test_move_whole_polygon_after_select.
        window._set_level(1)
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is None           # first press only selects
        assert canvas.selected_annotation() is poly
        canvas.mouseReleaseEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))


def test_copy_paste_duplicates_selected_object_offset_and_undoable() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(1)
        canvas = window.canvas
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()

        # Copy/paste follow the editing state. In the new model editing is off only
        # while the Draw pen is armed, so copy is a no-op then.
        window.draw_button.setChecked(True)
        canvas._set_selected(poly)
        window._copy_selected_annotation()
        assert window._clipboard is None  # drawing -> editing off -> can't copy
        window.draw_button.setChecked(False)  # pen up -> editing (and copy/paste) active

        # Nothing selected -> copy is a no-op, paste has nothing to do.
        canvas._set_selected(None)
        window._copy_selected_annotation()
        assert window._clipboard is None
        window._paste_annotation()
        assert len(window._current_level_annotations()) == 1

        # Select + copy + paste: a second object appears, offset from the original so
        # it doesn't sit invisibly on top (cursor isn't over the image in headless).
        canvas._set_selected(poly)
        window._copy_selected_annotation()
        assert window._clipboard is not None and window._clipboard["level"] == 1
        window._paste_annotation()
        anns = window._current_level_annotations()
        assert len(anns) == 2
        pasted = anns[-1]
        assert pasted is not poly
        assert window.canvas.selected_annotation() is pasted  # paste is selected
        # Same shape/size, shifted away from the source (its bbox origin moved).
        assert pasted["bbox"][2:] == [10, 10]                 # width/height preserved
        assert (pasted["bbox"][0], pasted["bbox"][1]) != (0, 0)  # nudged off the original
        assert window._annotation_center(pasted) != window._annotation_center(poly)

        # A second paste steps further so it doesn't stack on the first copy.
        window._paste_annotation()
        anns = window._current_level_annotations()
        assert len(anns) == 3
        centers = [window._annotation_center(a) for a in anns]
        assert len(set(centers)) == 3  # all three objects are at distinct positions

        # Paste is undoable like any created object.
        window._undo()
        assert len(window._current_level_annotations()) == 2
        window._undo()
        assert len(window._current_level_annotations()) == 1


def test_paste_stays_inside_image_bounds() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=20)
        window._set_mode("annotation")
        window._set_level(1)
        canvas = window.canvas
        # A box hugging the bottom-right corner: the nudge must not push it off-image.
        poly = {"category_id": 1, "segmentation": [[12, 12, 20, 12, 20, 20, 12, 20]], "bbox": [12, 12, 8, 8], "area": 64}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()
        window.edit_objects_button.setChecked(True)  # a tool must be active for copy/paste
        canvas._set_selected(poly)
        window._copy_selected_annotation()
        window._paste_annotation()
        pasted = window._current_level_annotations()[-1]
        seg = pasted["segmentation"][0]
        xs, ys = seg[0::2], seg[1::2]
        assert min(xs) >= -0.01 and max(xs) <= 20.01
        assert min(ys) >= -0.01 and max(ys) <= 20.01


def test_new_annotation_model_editing_always_on() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        assert window._legacy_mode is False  # the new single-Draw model is the default
        window._set_mode("annotation")
        window._set_level(1)
        canvas = window.canvas
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()

        # No Edit button; editing is active with the pen up (draw off).
        assert window.edit_objects_button.isHidden() is True
        assert window.draw_button.isChecked() is False
        assert window._editing_active() is True
        assert canvas._edit_enabled is True

        # Selecting an object offers change-class + Delete + copy (all edit behaviours).
        canvas._set_selected(poly)
        assert window._redefine_panel_active is True
        assert window.delete_button.isEnabled() is True
        window._copy_selected_annotation()
        assert window._clipboard is not None

        # Arming Draw turns editing OFF and clears the selection (draw takes over).
        window.draw_button.setChecked(True)
        assert window._editing_active() is False
        assert canvas._edit_enabled is False
        assert canvas.selected_annotation() is None
        assert window._copy_paste_active() is False
        # Dropping the pen restores editing.
        window.draw_button.setChecked(False)
        assert window._editing_active() is True
        assert canvas._edit_enabled is True


def test_legacy_mode_two_button_workflow() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._legacy_mode = True  # set directly (avoids writing the settings file)
        window._apply_mode_chrome()
        window._set_mode("annotation")
        window._set_level(1)
        canvas = window.canvas
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()

        # Both toggles visible; editing is OFF by default (the old non-destructive state).
        assert window.draw_button.isHidden() is False
        assert window.edit_objects_button.isHidden() is False
        assert window._editing_active() is False
        assert canvas._edit_enabled is False

        # Turning Edit on enables editing; Draw and Edit stay mutually exclusive.
        window.edit_objects_button.setChecked(True)
        assert window._editing_active() is True
        assert canvas._edit_enabled is True
        window.draw_button.setChecked(True)
        assert window.edit_objects_button.isChecked() is False
        assert window._editing_active() is False


def test_legacy_mode_toggle_persists_and_swaps_chrome() -> None:
    import src.qt_main as qm

    saved: dict = {}
    original_save = qm.save_app_settings
    qm.save_app_settings = lambda s: saved.update(s)
    try:
        window = _window()
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
            _project, _basename = _project_with_image(window, parent, src)
            window._set_mode("annotation")
            assert window.edit_objects_button.isHidden() is True  # new model: no Edit button

            window._on_legacy_mode_toggled(True)  # flip to legacy (the Settings toggle handler)
            assert window._legacy_mode is True
            assert saved.get("legacy_draw_edit") is True          # persisted
            assert window.edit_objects_button.isHidden() is False  # legacy shows the Edit button

            window._on_legacy_mode_toggled(False)  # back to new
            assert window._legacy_mode is False
            assert window.edit_objects_button.isHidden() is True
    finally:
        qm.save_app_settings = original_save


def test_toggle_switch_widget_basic() -> None:
    sw = ToggleSwitch(False)
    seen = []
    sw.toggled.connect(seen.append)
    assert sw.isChecked() is False
    sw.setChecked(True, animate=False)
    assert sw.isChecked() is True and seen == [True]
    sw.setChecked(True, animate=False)  # no-op, no repeat signal
    assert seen == [True]


def test_settings_dialog_tabs_label_size_and_legacy() -> None:
    import src.qt_main as qm

    saved: dict = {}
    original_save = qm.save_app_settings
    qm.save_app_settings = lambda s: saved.update(s)
    try:
        window = _window()
        # Opens on the requested tab.
        d_general = SettingsDialog(window, initial_tab="general")
        assert d_general.tabs.currentIndex() == 0
        d_shortcuts = SettingsDialog(window, initial_tab="shortcuts")
        assert d_shortcuts.tabs.currentIndex() == 1

        # Label-size slider live-scales the canvas; releasing persists it.
        d_general._on_label_size_changed(150)
        assert window.label_scale == 1.5 and window.canvas.label_scale == 1.5
        d_general._on_label_size_released()
        assert saved.get("label_scale") == 1.5

        # The legacy switch drives + persists the annotation model.
        assert d_general.legacy_switch.isChecked() is False
        d_general.legacy_switch.setChecked(True)
        assert window._legacy_mode is True and saved.get("legacy_draw_edit") is True
        d_general.deleteLater()
        d_shortcuts.deleteLater()
    finally:
        qm.save_app_settings = original_save


def test_show_labels_switch_persists_and_toggles_canvas() -> None:
    import src.qt_main as qm

    saved: dict = {}
    original_save = qm.save_app_settings
    qm.save_app_settings = lambda s: saved.update(s)
    try:
        window = _window()
        assert window.show_labels is True
        window.labels_switch.setChecked(False)
        assert window.show_labels is False
        assert saved.get("show_labels") is False
        assert window.canvas.show_labels is False
    finally:
        qm.save_app_settings = original_save


def test_action_picker_dialog_renders_actions() -> None:
    from PyQt6.QtWidgets import QPushButton

    window = _window()
    dialog = ActionPickerDialog(window, "Test", [
        ("a", "Do A", "does A", True),
        ("b", "Do B", "does B (disabled)", False),
    ])
    buttons = [b for b in dialog.findChildren(QPushButton) if b.objectName() == "pickAction"]
    assert [b.text() for b in buttons] == ["Do A", "Do B"]
    assert [b.isEnabled() for b in buttons] == [True, False]
    dialog._pick("a")
    assert dialog.chosen == "a"


def _capture_picker(monkeypatch_target):
    """Stub ActionPickerDialog to capture the actions list without a real modal exec."""
    captured: dict = {}

    class _Fake:
        def __init__(self, owner, title, actions):
            captured["title"] = title
            captured["actions"] = actions
            self.chosen = None

        def exec(self):
            return 0

    monkeypatch_target.ActionPickerDialog = _Fake
    return captured


def test_import_window_actions_names_and_gating() -> None:
    import src.qt_main as qm

    original = qm.ActionPickerDialog
    try:
        # Project WITH an image -> annotations enabled.
        window = _window()
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
            _project, _basename = _project_with_image(window, parent, src)
            captured = _capture_picker(qm)
            window._open_import_window()
            labels = [a[1] for a in captured["actions"]]
            gating = {a[0]: a[3] for a in captured["actions"]}
            assert labels == ["Open images folder", "Open zip from LS", "Open annotations (JSON)"]
            assert gating == {"images": True, "zip": True, "annotations": True}

        # Empty project (no images) -> annotations disabled.
        window2 = _window()
        with tempfile.TemporaryDirectory() as parent:
            project = Project.create(parent, "Empty", image_size=STUB_SIZE)
            window2._activate_project(project)
            captured2 = _capture_picker(qm)
            window2._open_import_window()
            gating2 = {a[0]: a[3] for a in captured2["actions"]}
            assert gating2["annotations"] is False
    finally:
        qm.ActionPickerDialog = original


def test_project_window_actions_gating_and_routes() -> None:
    import src.qt_main as qm

    original = qm.ActionPickerDialog
    try:
        # No project: close / export / redefine are disabled.
        window = _window()
        captured = _capture_picker(qm)
        window._open_project_window()
        gating = {a[0]: a[3] for a in captured["actions"]}
        assert [a[0] for a in captured["actions"]] == ["new", "open", "close", "export", "redefine"]
        assert gating["new"] is True and gating["open"] is True
        assert gating["close"] is False and gating["export"] is False and gating["redefine"] is False

        # Routing: a chosen id runs the matching handler.
        called: list[str] = []
        window._open_project = lambda: called.append("open")  # type: ignore[assignment]

        class _Fake:
            def __init__(self, owner, title, actions):
                self.chosen = "open"

            def exec(self):
                return 0

        qm.ActionPickerDialog = _Fake
        window._open_project_window()
        assert called == ["open"]
    finally:
        qm.ActionPickerDialog = original


def test_open_import_window_routes_to_handler() -> None:
    import src.qt_main as qm

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, _basename = _project_with_image(window, parent, src)
        called: list[str] = []
        window._import_zip = lambda: called.append("zip")  # type: ignore[assignment]

        class _FakeImport:  # avoid a real modal exec() in headless
            def __init__(self, owner, title, actions):
                self.chosen = "zip"

            def exec(self):
                return 1

        original = qm.ActionPickerDialog
        qm.ActionPickerDialog = _FakeImport  # type: ignore[assignment]
        try:
            window._open_import_window()
            assert called == ["zip"]
        finally:
            qm.ActionPickerDialog = original  # type: ignore[assignment]


def test_unified_tool_shortcut_is_mode_aware() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, _basename = _project_with_image(window, parent, src)

        # Annotation: the single tool shortcut toggles Draw.
        window._set_mode("annotation")
        assert window.draw_button.isChecked() is False
        window._toggle_tool_shortcut()
        assert window.draw_button.isChecked() is True
        window._toggle_tool_shortcut()
        assert window.draw_button.isChecked() is False

        # Validation: the SAME shortcut toggles Edit objects instead.
        window._set_mode("validation")
        assert window.edit_objects_button.isChecked() is False
        window._toggle_tool_shortcut()
        assert window.edit_objects_button.isChecked() is True
        window._toggle_tool_shortcut()
        assert window.edit_objects_button.isChecked() is False


def test_draw_mode_disables_selection_and_deselects() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(1)
        canvas = window.canvas
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()

        # Selected with the pen up.
        canvas._set_selected(poly)
        assert canvas.selected_annotation() is poly

        # Switching to Draw mode deselects it and turns selection off.
        window.draw_button.setChecked(True)
        assert canvas.selected_annotation() is None
        assert canvas._select_enabled is False

        # A canvas click in draw mode (no active class -> pen inactive) still can't select.
        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas.selected_annotation() is None

        # Shift-clicking an object row can't select while drawing either.
        window._on_object_row_activated(poly, shift=True)
        assert canvas.selected_annotation() is None

        # Pen back up: selection is available again.
        window.draw_button.setChecked(False)
        assert canvas._select_enabled is True


def test_hover_highlights_object_and_its_label() -> None:
    from PyQt6.QtCore import QPointF

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        name, cid = _l1_class()
        # A small object with room around it so its label can sit off the body.
        ann = _poly_ann(cid, [30, 40, 40, 40, 40, 50, 30, 50])
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)  # validation on a project: selecting (and hover) is enabled
        canvas = window.canvas
        canvas.fit_to_view()

        def hover_image(px, py):
            ix, iy, _w, _h = canvas._fit_display_rect()
            z = canvas._zoom
            canvas.mouseMoveEvent(_FakeMouse(ix + px * z, iy + py * z, Qt.MouseButton.NoButton))

        # Over the object body -> it becomes the hover target.
        hover_image(35, 45)
        assert canvas._hover_annotation is ann
        # Empty space -> hover clears.
        hover_image(5, 5)
        assert canvas._hover_annotation is None

        # Pin the label far above the body (clamps to the image top), then hover it:
        # the LABEL alone highlights the object even though the body isn't under the cursor.
        canvas._label_offsets[id(ann)] = QPointF(0.0, -100.0)
        _render_overlays(canvas)
        label_center = canvas._label_hit_rects[-1][0].center()
        assert canvas._hit_test_annotation(label_center) is None  # cursor is off the body
        canvas.mouseMoveEvent(_FakeMouse(label_center.x(), label_center.y(), Qt.MouseButton.NoButton))
        assert canvas._hover_annotation is ann

        # Leaving the widget clears it.
        canvas.leaveEvent(None)
        assert canvas._hover_annotation is None

        # The already-selected object isn't hover-highlighted (it has the bright border).
        canvas._set_selected(ann)
        hover_image(35, 45)
        assert canvas._hover_annotation is None

        # Draw mode never hover-highlights (a click there draws, it can't select).
        canvas._set_selected(None)
        window._set_mode("annotation")
        window._set_level(1)
        window.draw_button.setChecked(True)
        canvas.fit_to_view()
        hover_image(35, 45)
        assert canvas._hover_annotation is None


def test_draw_mode_label_drag_repositions_without_selecting() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 4, 0, 4, 4, 0, 4])
        window.annotation_store[1][basename] = [ann]
        window._set_mode("annotation")
        window._set_level(1)
        canvas = window.canvas
        canvas.fit_to_view()

        # Draw ON with no class picked -> pen inactive (_draw_shape None), so a
        # Shift+label click still reaches the label-reposition path.
        window.draw_button.setChecked(True)
        assert canvas._draw_shape is None and canvas._draw_active is True

        _render_overlays(canvas)
        rect, hit_ann = canvas._label_hit_rects[-1]
        assert hit_ann is ann
        center = rect.center()
        shift = Qt.KeyboardModifier.ShiftModifier
        canvas.mousePressEvent(_FakeMouse(center.x(), center.y(), Qt.MouseButton.LeftButton, shift))
        # The label grabs for repositioning, but draw mode never selects.
        assert canvas._label_drag_ann is ann
        assert canvas.selected_annotation() is None
        canvas.mouseMoveEvent(_FakeMouse(center.x() + 10, center.y() + 6, Qt.MouseButton.LeftButton, shift))
        canvas.mouseReleaseEvent(_FakeMouse(center.x() + 10, center.y() + 6, Qt.MouseButton.LeftButton))
        assert canvas._label_drag_ann is None
        assert id(ann) in canvas._label_offsets      # the label actually moved
        assert canvas.selected_annotation() is None  # still nothing selected


def test_move_whole_polygon_after_select() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(1)  # L1 polygons
        canvas = window.canvas
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()
        canvas.set_edit_enabled(True)
        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom

        # First body press only SELECTS (nothing moves while unselected).
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is None
        assert canvas.selected_annotation() is poly
        canvas.mouseReleaseEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))

        # Now selected: a body press-drag moves the WHOLE polygon by (+3, +2).
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is not None
        canvas.mouseMoveEvent(_FakeMouse(ix + 8 * z, iy + 7 * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + 8 * z, iy + 7 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is None
        assert poly["segmentation"][0] == [3, 2, 13, 2, 13, 12, 3, 12]  # whole contour shifted
        assert poly["bbox"] == [3.0, 2.0, 10.0, 10.0]
        assert poly["area"] == 100  # translation preserves area

        window._undo()
        assert poly["segmentation"][0] == [0, 0, 10, 0, 10, 10, 0, 10]
        assert poly["bbox"] == [0, 0, 10, 10]

        # A press ON a vertex (corner 10,10) still edits that point, not a whole move.
        canvas._set_selected(poly)
        canvas.mousePressEvent(_FakeMouse(ix + 10 * z, iy + 10 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is None
        assert canvas._drag_vertex is not None
        canvas.mouseReleaseEvent(_FakeMouse(ix + 10 * z, iy + 10 * z, Qt.MouseButton.LeftButton))


def test_box_move_is_clamped_to_image_bounds() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=20)
        window._set_mode("annotation")
        window._set_level(2)
        canvas = window.canvas
        ann = {"category_id": 1, "bbox": [2, 2, 6, 6], "area": 36, "rotation": 0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        canvas.set_edit_enabled(True)
        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # Press the body, then drag WAY past the bottom-right corner of a 20px image.
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is not None
        canvas.mouseMoveEvent(_FakeMouse(ix + 999 * z, iy + 999 * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + 999 * z, iy + 999 * z, Qt.MouseButton.LeftButton))
        x, y, w, h = ann["bbox"]
        assert abs(w - 6) < 1e-6 and abs(h - 6) < 1e-6   # size preserved
        # No corner left the image: it slid to the far edge, not past it.
        assert -0.5 <= x and x + w <= 20.5 and -0.5 <= y and y + h <= 20.5
        assert abs(x - 14) < 1e-6 and abs(y - 14) < 1e-6  # pinned at the far corner


def test_box_rotation_rejected_when_a_corner_would_leave_image() -> None:
    from PyQt6.QtCore import QPointF

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=20)
        window._set_mode("annotation")
        window._set_level(2)
        canvas = window.canvas
        # A wide, thin box hugging the top edge: a 90° turn would swing corners to y<0.
        ann = {"category_id": 1, "bbox": [0, 0, 18, 2], "area": 36, "rotation": 0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        canvas.set_edit_enabled(True)
        canvas._set_selected(ann)
        item = canvas._selected_overlay_item()
        cx, cy = item["center"]
        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # Start to the right of centre (angle 0), drag straight down (would be +90°).
        canvas._begin_rotation(item, QPointF(ix + (cx + 5) * z, iy + cy * z))
        canvas._apply_rotation(QPointF(ix + cx * z, iy + (cy + 5) * z))
        # The out-of-bounds turn was rejected: box unchanged, every corner in bounds.
        assert ann["rotation"] == 0
        assert canvas._corners_in_bounds(item["points"], 20, 20)


def test_box_edge_resize_moves_one_side_and_undoes() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(2)
        canvas = window.canvas
        ann = {"category_id": 1, "bbox": [10, 10, 20, 20], "area": 400, "rotation": 0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        window.edit_objects_button.setChecked(True)
        canvas._set_selected(ann)
        item = canvas._selected_overlay_item()
        assert item is not None

        mids = canvas._box_edge_midpoints(item)
        assert abs(mids[0][0] - 20) < 1e-6 and abs(mids[0][1] - 10) < 1e-6  # top-edge midpoint

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # Grab ONLY the top edge and drag it up; left/right/bottom must stay put.
        canvas.mousePressEvent(_FakeMouse(ix + 20 * z, iy + 10 * z, Qt.MouseButton.LeftButton))
        assert canvas._drag_edge is not None and canvas._drag_edge[1] == 0
        canvas.mouseMoveEvent(_FakeMouse(ix + 20 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + 20 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._drag_edge is None
        x, y, w, h = ann["bbox"]
        assert abs(x - 10) < 1e-6 and abs(w - 20) < 1e-6   # left/right unchanged
        assert abs(y - 5) < 1e-6 and abs(h - 25) < 1e-6     # only the top moved up

        window._undo()  # one-side resize is undoable (geometry snapshot)
        assert ann["bbox"] == [10, 10, 20, 20]


def test_rotated_box_edge_resize_grabs_anywhere_along_edge() -> None:
    import math

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=64)
        window._set_mode("annotation")
        window._set_level(2)
        canvas = window.canvas
        cx, cy = 32.0, 32.0
        theta = math.radians(30)
        ct, st = math.cos(theta), math.sin(theta)

        def rot(px, py):
            dx, dy = px - cx, py - cy
            return (cx + dx * ct - dy * st, cy + dx * st + dy * ct)

        quad = [rot(20, 26), rot(44, 26), rot(44, 38), rot(20, 38)]
        ann = {"category_id": 1, "segmentation": [[v for p in quad for v in p]],
               "bbox": [20, 26, 24, 12], "area": 288, "rotation": 30.0}
        window.annotation_store[2].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()
        window.edit_objects_button.setChecked(True)
        canvas._set_selected(ann)
        item = canvas._selected_overlay_item()
        pts = item["points"]

        ix, iy, _w, _h = canvas._fit_display_rect()
        z = canvas._zoom
        # A point 25% along edge 1 (P1->P2) — NOT the midpoint dot — must still grab it.
        e = 1
        gx = pts[e][0] + 0.25 * (pts[(e + 1) % 4][0] - pts[e][0])
        gy = pts[e][1] + 0.25 * (pts[(e + 1) % 4][1] - pts[e][1])
        hit = canvas._hit_test_box_edge(QPointF(ix + gx * z, iy + gy * z))
        assert hit is not None and hit[1] == e  # grabbed along the slanted edge

        # Drag it outward along the edge's perpendicular and confirm a real resize.
        af = pts[(e + 3) % 4]
        nx, ny = pts[e][0] - af[0], pts[e][1] - af[1]
        nl = math.hypot(nx, ny)
        ux, uy = nx / nl, ny / nl
        before_area = ann["area"]
        canvas.mousePressEvent(_FakeMouse(ix + gx * z, iy + gy * z, Qt.MouseButton.LeftButton))
        assert canvas._drag_edge is not None and canvas._drag_edge[1] == e
        canvas.mouseMoveEvent(_FakeMouse(ix + (gx + ux * 5) * z, iy + (gy + uy * 5) * z, Qt.MouseButton.LeftButton))
        canvas.mouseReleaseEvent(_FakeMouse(ix + (gx + ux * 5) * z, iy + (gy + uy * 5) * z, Qt.MouseButton.LeftButton))
        assert ann["area"] > before_area + 1     # it grew
        assert abs(ann["rotation"] - 30.0) < 1e-6  # rotation preserved
        # still a true rectangle (right angle at corner 1)
        q = ann["segmentation"][0]
        P = [(q[i], q[i + 1]) for i in range(0, 8, 2)]
        e01 = (P[1][0] - P[0][0], P[1][1] - P[0][1])
        e12 = (P[2][0] - P[1][0], P[2][1] - P[1][1])
        assert abs(e01[0] * e12[0] + e01[1] * e12[1]) < 1e-3


def test_reclassify_selected_object_is_undoable() -> None:
    from src import levels

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(3)  # L3 polygons with several classes
        names = [entry[0] for entry in levels.level_classes(3)]  # (name, hex) -> name
        class_a, class_b = names[0], names[1]
        cat_a = levels.category_id_for_class(3, class_a)
        cat_b = levels.category_id_for_class(3, class_b)
        ann = {"category_id": cat_a, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}
        window.annotation_store[3].setdefault(basename, []).append(ann)
        window._refresh_overlay_items()

        # Change-class is an Edit-mode operation now.
        window.edit_objects_button.setChecked(True)
        # Selecting the object swaps the sidebar to the change-class catalog.
        window.canvas._set_selected(ann)
        assert window._redefine_panel_active is True

        # Reclassify to class B; the object STAYS selected (panel stays up).
        window._reclassify_selected(ann, class_b)
        assert ann["category_id"] == cat_b
        assert window.canvas.selected_annotation() is ann
        assert window._redefine_panel_active is True

        # undo/redo swaps the category back and forth.
        window._undo()
        assert ann["category_id"] == cat_a
        window._redo()
        assert ann["category_id"] == cat_b

        # Esc deselects (and restores the normal class list).
        assert window._handle_escape() is True
        assert window.canvas.selected_annotation() is None
        assert window._redefine_panel_active is False


def test_escape_exits_draw_and_edit_modes() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)

        window.draw_button.setChecked(True)
        assert window._handle_escape() is True
        assert window.draw_button.isChecked() is False  # draw mode turned off + unhighlighted

        window.edit_objects_button.setChecked(True)
        assert window._handle_escape() is True
        assert window.edit_objects_button.isChecked() is False  # edit mode turned off

        assert window._handle_escape() is False  # nothing active -> no-op (never quits)


def test_class_list_popout_and_dock() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        container = window.class_list_container

        # Pop out: the SAME container moves into the separate window.
        window._toggle_class_popout()
        assert window.class_popout is not None
        assert window.class_popout.scroll.widget() is container
        assert window.class_scroll.widget() is None
        assert window.class_scroll.isHidden()

        # Toggle again -> closes the window -> container docks back into the sidebar.
        window._toggle_class_popout()
        assert window.class_popout is None
        assert window.class_scroll.widget() is container
        assert not window.class_scroll.isHidden()


def test_canvas_popout_and_dock() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        container = window.canvas_container

        # Pop out: the SAME canvas container moves into the separate window; the
        # placeholder hint takes its spot in the main view.
        window._toggle_canvas_popout()
        assert window.canvas_popout is not None
        assert container.parent() is window.canvas_popout
        assert window.main_layout.indexOf(container) == -1
        assert not window.canvas_popout_hint.isHidden()
        # The canvas widget rode along and still belongs to the same container.
        assert window.canvas.parent() is container

        # Toggle again -> closes the window -> the canvas docks back into the main
        # view at the left (index 0, before the hint and sidebar).
        window._toggle_canvas_popout()
        assert window.canvas_popout is None
        assert window.main_layout.indexOf(container) == 0
        assert window.canvas_popout_hint.isHidden()


def test_canvas_docks_back_when_project_closes() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project_with_image(window, parent, src)
        container = window.canvas_container
        window._toggle_canvas_popout()
        assert window.canvas_popout is not None

        # Returning to the welcome screen (no project) must reclaim the canvas.
        window.project = None
        window._update_project_chrome()
        assert window.canvas_popout is None
        assert window.main_layout.indexOf(container) == 0


def test_convert_coerces_polygon_to_box_for_box_level() -> None:
    # A polygon remapped to the box level (L2) becomes its axis-aligned envelope.
    converted = PyQtAnnotationReview._convert_source_annotation(
        {"segmentation": [[0, 0, 10, 0, 10, 4, 0, 4]], "bbox": [0, 0, 10, 4], "area": 40},
        2,
        "high_vegetation",
    )
    assert converted is not None
    assert converted["rotation"] == 0.0
    assert converted["bbox"] == [0.0, 0.0, 10.0, 4.0]
    assert len(converted["segmentation"][0]) == 8  # 4-corner quad


def test_convert_preserves_oriented_box_rotation_for_l2() -> None:
    # A Label-Studio-style oriented box (4-corner rotated quad + rotation) imported
    # to L2 must KEEP its quad + angle — the bug collapsed it to an axis envelope,
    # silently dropping the rotation that only the LS export carries.
    quad = [10.0, 0.0, 20.0, 10.0, 10.0, 20.0, 0.0, 10.0]  # a 45°-ish diamond
    src = {"segmentation": [quad], "bbox": [0.0, 0.0, 20.0, 20.0], "area": 200.0, "rotation": 45.0}
    out = PyQtAnnotationReview._convert_source_annotation(src, 2, "high_vegetation")
    assert out is not None
    assert out["rotation"] == 45.0                 # angle preserved
    assert out["segmentation"][0] == quad          # rotated quad kept, not the envelope
    assert out["area"] == 200.0


def test_convert_collapses_real_polygon_to_box_envelope() -> None:
    # A genuine polygon (>4 points) remapped to the box level still becomes its
    # axis-aligned envelope (the polygon-to-box coercion path is unchanged).
    poly = [0.0, 0.0, 10.0, 0.0, 10.0, 4.0, 5.0, 6.0, 0.0, 4.0]  # 5 points
    src = {"segmentation": [poly], "bbox": [0.0, 0.0, 10.0, 6.0], "area": 50.0}
    out = PyQtAnnotationReview._convert_source_annotation(src, 2, "high_vegetation")
    assert out is not None
    assert out["rotation"] == 0.0
    assert out["segmentation"][0] == [0.0, 0.0, 10.0, 0.0, 10.0, 6.0, 0.0, 6.0]  # envelope quad


def _routed_project_window(window, parent, src):
    """A project with one image carrying an L1 polygon + an L2 box; return (project, basename)."""
    project, basename = _project_with_image(window, parent, src)
    images = [{"id": 1, "file_name": basename}]
    annotations = {
        1: [
            {"category_id": 100, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100},
            {"category_id": 200, "bbox": [1, 1, 4, 4], "area": 16},
        ]
    }
    window._route_and_commit_annotations(images, annotations, {100: "road_asphalt", 200: "high_vegetation"})
    return project, basename


def test_validation_renders_active_level_on_project() -> None:
    # Stage F: with a project open, validation reviews ONE level at a time and the
    # L1/L2/L3 selector drives which. (This path rendered nothing before Stage F.)
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _routed_project_window(window, parent, src)

        window._set_mode("validation")
        assert window.level_selector_widget.isHidden() is False  # selector shown in validation too
        window._set_level(1)
        assert [o["label"] for o in window.current_overlay_items] == ["road_asphalt"]

        window._set_level(2)
        assert [o["label"] for o in window.current_overlay_items] == ["high_vegetation"]


def test_validation_lists_only_present_classes_on_project() -> None:
    # The validation sidebar shows ONLY classes annotated on the current image
    # within the level (not the whole level catalog).
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _routed_project_window(window, parent, src)  # L1 has exactly one class: road_asphalt
        window._set_mode("validation")
        window._set_level(1)
        names = [name for _cid, name in window.current_class_items]
        assert names == ["road_asphalt"]  # not all ~20 L1 catalog classes
        assert set(window.class_checkboxes) == {levels.category_id_for_class(1, "road_asphalt")}

        # Level 2 likewise shows only its present class.
        window._set_level(2)
        names = [name for _cid, name in window.current_class_items]
        assert names == ["high_vegetation"]


def test_validation_respects_class_visibility_on_project() -> None:
    # Stage F: per-class show/hide toggles operate on the active level's data.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _routed_project_window(window, parent, src)
        window._set_mode("validation")
        window._set_level(1)
        cat_id = levels.category_id_for_class(1, "road_asphalt")
        window._on_class_toggled(cat_id, False)
        assert window.current_overlay_items == []
        window._on_class_toggled(cat_id, True)
        assert [o["label"] for o in window.current_overlay_items] == ["road_asphalt"]


def test_validation_saves_active_level_on_project() -> None:
    # Stage F: an edit in validation saves back to that level's level{N}.json.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _routed_project_window(window, parent, src)
        window._set_mode("validation")
        window._set_level(2)
        anns = window._current_level_annotations()
        assert len(anns) == 1
        anns[0]["bbox"] = [2.0, 2.0, 5.0, 5.0]  # mutate the shared level-store dict
        window._save_dataset()  # _show_message is stubbed; writes level2.json
        saved = project.read_level(2)
        assert saved[basename][0]["bbox"] == [2.0, 2.0, 5.0, 5.0]
        # The untouched level is unchanged.
        assert len(project.read_level(1).get(basename, [])) == 1


def test_validation_delete_selected_annotation_on_project() -> None:
    # Stage F follow-up: a reviewer can select a whole object and delete it
    # (Del / red ✕) in validation, undoably, on the active level's store.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _routed_project_window(window, parent, src)
        window._set_mode("validation")
        window._set_level(1)
        assert window.delete_button.isHidden() is False  # red ✕ available in validation

        window.edit_objects_button.setChecked(True)  # delete is an Edit-mode op now
        anns = window._current_level_annotations()
        assert len(anns) == 1
        window.canvas._set_selected(anns[0])  # simulate clicking the object body
        assert window.delete_button.isEnabled() is True
        assert window._delete_selected_annotation() is True
        assert window._current_level_annotations() == []

        window._undo()  # delete is undoable
        assert len(window._current_level_annotations()) == 1


def test_validation_shows_pending_redefine_overlays_on_project() -> None:
    # Stage F absorbs the Stage I note: parked "redefine" classes must show in
    # the VALIDATION lens too, not only annotation mode.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        annotations = {1: [{"category_id": 5, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}]}
        window._route_and_commit_annotations(images, annotations, {5: "banana"})  # off-catalog -> stashed
        project.set_unmapped_level("banana", 3)

        window._set_mode("validation")
        window._set_level(3)
        assert any(o.get("pending_redefine") for o in window.current_overlay_items)
        assert any("redefine" in o["label"] for o in window.current_overlay_items)


def test_per_level_dirty_tracking_on_project() -> None:
    # Stage G: edits mark only their level dirty; Save level clears just that
    # level, Save all clears everything.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _routed_project_window(window, parent, src)  # L1 polygon + L2 box
        window._set_mode("validation")
        window.edit_objects_button.setChecked(True)  # delete is an Edit-mode op now
        assert window.action_export_zip.isEnabled() is True

        window._set_level(1)
        window.canvas._set_selected(window._current_level_annotations()[0])
        window._delete_selected_annotation()
        assert window._dirty_levels == {1}

        window._set_level(2)
        window.canvas._set_selected(window._current_level_annotations()[0])
        window._delete_selected_annotation()
        assert window._dirty_levels == {1, 2}
        assert window._dirty is True

        # Save the active level (L2) -> only L2 clears.
        window._save_dataset()
        assert window._dirty_levels == {1}
        assert window._dirty is True

        # Save all -> everything clears.
        window._save_all_levels()
        assert window._dirty_levels == set()
        assert window._dirty is False


def test_status_shows_saved_confirmation_after_save() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _routed_project_window(window, parent, src)
        window._set_mode("validation")
        window._set_level(1)

        # A fresh session hasn't saved yet -> no save-state tail.
        assert window._last_saved_at is None
        assert window._save_state_suffix() == ""

        # An edit marks the level dirty -> the unsaved marker takes precedence.
        window.edit_objects_button.setChecked(True)  # delete is an Edit-mode op now
        window.canvas._set_selected(window._current_level_annotations()[0])
        window._delete_selected_annotation()
        assert "unsaved" in window._save_state_suffix()

        # Saving funnels through _write_level_file, stamping _last_saved_at; the tail
        # flips to the saved confirmation.
        window._save_dataset()
        assert window._last_saved_at is not None
        suffix = window._save_state_suffix()
        assert "saved" in suffix and "unsaved" not in suffix


def test_export_zip_button_visibility_follows_project() -> None:
    window = _window()
    assert window.export_zip_button.isHidden() is True  # hidden on the welcome screen
    with tempfile.TemporaryDirectory() as parent:
        project = Project.create(parent, "Exp", image_size=STUB_SIZE)
        window._activate_project(project)
        assert window.export_zip_button.isHidden() is False  # visible with a project open
        window._confirm_discard_unsaved = lambda *a, **k: True  # type: ignore[assignment]
        window._close_project()
        assert window.export_zip_button.isHidden() is True


def test_warn_on_unsaved_save_discard_cancel() -> None:
    # Stage G: the close/switch guard saves, discards, or cancels per the choice.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _routed_project_window(window, parent, src)
        window._set_mode("validation")
        window._set_level(1)
        window.edit_objects_button.setChecked(True)  # delete is an Edit-mode op now
        window.canvas._set_selected(window._current_level_annotations()[0])
        window._delete_selected_annotation()
        assert window._dirty is True

        # Cancel -> do NOT proceed; nothing saved.
        window._ask_unsaved_resolution = lambda *a, **k: "cancel"  # type: ignore[assignment]
        assert window._confirm_discard_unsaved("close") is False
        assert window._dirty is True

        # Discard -> proceed, still dirty in memory (not persisted).
        window._ask_unsaved_resolution = lambda *a, **k: "discard"  # type: ignore[assignment]
        assert window._confirm_discard_unsaved("close") is True
        assert window._dirty is True

        # Save -> proceed, dirty cleared AND the deletion is on disk.
        window._ask_unsaved_resolution = lambda *a, **k: "save"  # type: ignore[assignment]
        assert window._confirm_discard_unsaved("close") is True
        assert window._dirty is False and window._dirty_levels == set()
        assert project.read_level(1).get(basename, []) == []


def test_redefine_object_selectable_and_reclassified_per_object() -> None:
    # User request: a parked redefine object is selectable like any object, and
    # assigning a class resolves JUST that object (others of the same class stay).
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        ann1 = {"category_id": 9, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}
        ann2 = {"category_id": 9, "segmentation": [[10, 10, 16, 10, 16, 16, 10, 16]], "bbox": [10, 10, 6, 6], "area": 36}
        window._route_and_commit_annotations(images, {1: [ann1, ann2]}, {9: "banana"})  # off-catalog -> 2 stashed
        project.set_unmapped_level("banana", 1)  # park both in L1 -> render as redefine overlays

        window._set_mode("annotation")
        window._set_level(1)
        redefine_items = [it for it in window.current_overlay_items if it.get("pending_redefine")]
        assert len(redefine_items) == 2
        assert all(it.get("redefine_entry_id") for it in redefine_items)

        # Select one (as a click would) -> the sidebar swaps to the assign panel.
        window.canvas._set_selected_redefine(redefine_items[0])
        assert window.canvas.selected_redefine() is redefine_items[0]
        assert window._redefine_panel_active is True

        # Assign it to an existing L1 class -> only THAT object resolves.
        window._resolve_redefine_object(redefine_items[0].get("redefine_entry_id"), "road_asphalt")

        store = project.read_level(1)
        committed = [a.get("category_id") for items in store.values() for a in items]
        assert committed == [levels.category_id_for_class(1, "road_asphalt")]  # exactly one, right class
        assert project.unmapped_class_counts() == {"banana": 1}  # the other stays parked
        assert len(window._pending_redefine_overlays(1)) == 1
        assert window._redefine_panel_active is False  # normal sidebar restored


def test_redefine_resolve_is_undoable() -> None:
    from src import levels

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)
        images = [{"id": 1, "file_name": basename}]
        ann = {"category_id": 9, "segmentation": [[0, 0, 6, 0, 6, 6, 0, 6]], "bbox": [0, 0, 6, 6], "area": 36}
        window._route_and_commit_annotations(images, {1: [ann]}, {9: "banana"})  # off-catalog -> stashed
        assert project.unmapped_class_counts() == {"banana": 1}
        window._set_mode("annotation")
        window._set_level(1)
        overlay = next(o for o in window.current_overlay_items if o.get("pending_redefine"))
        entry_id = overlay["redefine_entry_id"]

        # Resolve -> object committed to L1 on disk, stash empty.
        window._resolve_redefine_object(entry_id, "road_asphalt")
        cat = levels.category_id_for_class(1, "road_asphalt")
        assert [a["category_id"] for a in project.read_level(1).get(basename, [])] == [cat]
        assert project.unmapped_class_counts() == {}

        # Undo -> object removed (re-persisted), stash entry restored, overlay back.
        window._undo()
        assert project.read_level(1).get(basename, []) == []
        assert project.unmapped_class_counts() == {"banana": 1}
        assert len([o for o in window.current_overlay_items if o.get("pending_redefine")]) == 1

        # Redo -> object re-committed, stash empty again.
        window._redo()
        assert [a["category_id"] for a in project.read_level(1).get(basename, [])] == [cat]
        assert project.unmapped_class_counts() == {}


def test_redefine_overlay_matches_image_by_stable_id_across_prefix() -> None:
    # Regression: a real export references the SAME photo under a different filename
    # prefix than the project's stored copy. The red row flag matched by stable id,
    # but the redefine overlay matched by exact basename -> the row went red yet no
    # overlay appeared, and resolving stored the object under the wrong key (it
    # "vanished"). Both must match by stable id.
    from src.data_loading import stable_image_id

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project, basename = _project_with_image(window, parent, src)  # scene_000000001.png
        alt_name = "batch07-uuidA-000000001.png"  # same last-9 stem => same stable id
        assert stable_image_id(alt_name) == stable_image_id(basename)

        images = [{"id": 1, "file_name": alt_name}]
        ann = {"category_id": 9, "segmentation": [[0, 0, 3, 0, 3, 3, 0, 3]], "bbox": [0, 0, 3, 3], "area": 9}
        window._route_and_commit_annotations(images, {1: [ann]}, {9: "banana"})  # off-catalog -> stashed
        assert project.unmapped_class_counts() == {"banana": 1}

        window._set_mode("annotation")
        window._set_level(1)
        # The redefine overlay appears on the project's image despite the prefix.
        overlays = [o for o in window.current_overlay_items if o.get("pending_redefine")]
        assert len(overlays) == 1

        # Resolving lands the object under the PROJECT's basename, so it stays visible.
        window.canvas._set_selected_redefine(overlays[0])
        window._resolve_redefine_object(overlays[0].get("redefine_entry_id"), "road_asphalt")
        store = project.read_level(1)
        assert basename in store and len(store[basename]) == 1
        assert project.unmapped_class_counts() == {}
        assert any(not o.get("pending_redefine") for o in window.current_overlay_items)


def test_image_id_badge_shows_current_image_id() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)  # scene_000000001.png
        window._update_status_labels()
        expected = os.path.splitext(basename)[0][-9:]  # the Copy ID value
        # The title badge shows the image position then the id (e.g. "1/1 - …id"); the
        # bare id is still what _current_image_id_text / Copy ID hand out.
        assert window.image_id_label.text() == f"1/1 - {expected}"
        assert window._current_image_id_text() == expected
        # The clipboard gets exactly the bare id, never the appended position.
        window._copy_image_id()
        assert QApplication.clipboard().text() == expected


def _l1_class():
    """First L1 (polygon) class as (name, category_id)."""
    name = levels.level_classes(1)[0][0]
    return name, levels.category_id_for_class(1, name)


def _poly_ann(category_id, pts):
    """A polygon annotation dict from a flat point list (image coords)."""
    xs, ys = pts[0::2], pts[1::2]
    return {
        "category_id": category_id,
        "segmentation": [list(pts)],
        "bbox": [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)],
        "area": 9,
        "iscrowd": 0,
    }


def test_version_shown_in_title_and_welcome() -> None:
    from PyQt6.QtWidgets import QLabel

    window = _window()
    # Welcome screen advertises the version.
    welcome_text = " ".join(lbl.text() for lbl in window.welcome_page.findChildren(QLabel))
    assert APP_VERSION in welcome_text
    with tempfile.TemporaryDirectory() as parent:
        project = Project.create(parent, "Ver", image_size=STUB_SIZE)
        window._activate_project(project)
        # Title carries the app name + version alongside the project name.
        assert APP_NAME in window.windowTitle() and APP_VERSION in window.windowTitle()
        # The sidebar subtitle shows the open project's name (styled "PROJECT — name").
        subtitle = window.subtitle_label.text()
        assert "Ver" in subtitle and "PROJECT" in subtitle


def test_class_rollout_lists_objects_and_per_object_visibility() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        anns = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3]), _poly_ann(cid, [0, 0, 2, 0, 2, 2, 0, 2])]
        window.annotation_store[1][basename] = list(anns)
        window._set_mode("validation")
        window._set_level(1)

        # One expandable class bubble (the class IS present on the image).
        bubbles = window.class_list_container.findChildren(ClassBubbleButton)
        assert len(bubbles) == 1 and bubbles[0].category_id == cid

        # Expand it -> one row per object.
        window._on_class_expand_toggled(cid, True)
        assert len(window.class_list_container.findChildren(ObjectRowButton)) == 2

        # Both objects render initially.
        shown = [o for o in window.current_overlay_items if o.get("annotation") is not None]
        assert len(shown) == 2

        # Plain click on the first object's row hides ONLY that object.
        window._on_object_row_activated(anns[0], shift=False)
        shown = [o for o in window.current_overlay_items if o.get("annotation") is not None]
        assert len(shown) == 1 and shown[0]["annotation"] is anns[1]

        # Clicking again un-hides it.
        window._on_object_row_activated(anns[0], shift=False)
        assert len([o for o in window.current_overlay_items if o.get("annotation") is not None]) == 2

        # Shift-click selects the object on the canvas (as if clicked in the image).
        window._on_object_row_activated(anns[1], shift=True)
        assert window.canvas.selected_annotation() is anns[1]


def test_expand_arrows_are_parented_not_floating_windows() -> None:
    # Regression: the disclosure arrow was created parentless and made visible
    # before being added to a layout, so it flashed as a tiny top-level window
    # (close/max frame) in the middle of the canvas ~10x per class-list rebuild.
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)
        bubbles = window.class_list_container.findChildren(ClassBubbleButton)
        assert bubbles  # full catalog is listed in annotation mode
        for bubble in bubbles:
            assert bubble._expand_btn.parent() is not None
            assert bubble._expand_btn.isWindow() is False
            assert bubble._expand_btn.isVisible() in (True, False)  # never floats as a window


def test_rollout_works_in_annotation_mode_too() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        window.annotation_store[1][basename] = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])]
        window._set_mode("annotation")
        window._set_level(1)
        # The class catalog bubble for our class is expandable (it has an object).
        bubble = next(b for b in window.class_list_container.findChildren(ClassBubbleButton) if b.category_id == cid)
        assert bubble._expand_btn.isVisible() in (True, False)  # widget exists
        window._on_class_expand_toggled(cid, True)
        assert len(window.class_list_container.findChildren(ObjectRowButton)) == 1


def test_annotation_pill_toggles_class_visibility_with_pen_up() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        window.annotation_store[1][basename] = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])]
        window._set_mode("annotation")
        window._set_level(1)
        assert window.draw_button.isChecked() is False
        assert window.class_action_widget.isHidden() is False  # show/hide-all available pen-up
        assert any(it.get("annotation") for it in window.current_overlay_items)

        bubble = next(b for b in window.class_list_container.findChildren(ClassBubbleButton) if b.category_id == cid)
        # Pen UP: clicking the pill HIDES the class (visibility toggle); it does NOT
        # become the active draw class, and its objects leave the canvas.
        bubble.toggled.emit(False)
        assert window.visible_by_category.get(cid) is False
        assert window.active_category_by_level.get(1) is None
        assert not any(it.get("annotation") for it in window.current_overlay_items)

        # Show all brings it back.
        window._show_all_classes()
        assert window.visible_by_category.get(cid) is True
        assert any(it.get("annotation") for it in window.current_overlay_items)


def test_annotation_pill_selects_draw_class_with_pen_down() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        window.annotation_store[1][basename] = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])]
        window._set_mode("annotation")
        window._set_level(1)
        window.draw_button.setChecked(True)  # pen down
        # Show/Hide-all stay available in draw mode too (they toggle overlay
        # visibility, independent of the pills picking the active draw class).
        assert window.class_action_widget.isHidden() is False

        bubble = next(b for b in window.class_list_container.findChildren(ClassBubbleButton) if b.category_id == cid)
        bubble.toggled.emit(True)
        # Pen DOWN: the pill picks the active draw class; the class stays visible.
        assert window.active_category_by_level.get(1) == cid
        assert window.visible_by_category.get(cid, True) is True
        assert any(it.get("annotation") for it in window.current_overlay_items)


def test_change_class_panel_uses_framed_class_pills() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _p, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])
        window.annotation_store[1][basename] = [ann]
        window._set_mode("annotation")
        window._set_level(1)
        window.edit_objects_button.setChecked(True)
        window.canvas._set_selected(ann)  # -> change-class catalog panel
        # The picker now uses the SAME framed class pills as the main list (not the
        # separate collapsible-group look), so it's visually consistent.
        bubbles = window.class_list_container.findChildren(ClassBubbleButton)
        assert bubbles, "the change-class panel should use the framed class pills"
        assert not window.class_list_container.findChildren(CollapsibleClassGroup)
        # Clicking a pill reclassifies the selected object to that class.
        target_name = levels.level_classes(1)[3][0]
        target_cid = levels.category_id_for_class(1, target_name)
        target = next(b for b in bubbles if b.category_id == target_cid)
        target.toggled.emit(False)  # a pill click fires the catalog pick
        assert ann["category_id"] == target_cid


def test_redefine_dialog_collects_remaps_and_parks() -> None:
    window = _window()
    dialog = RedefineDialog(window, {"weird_a": 3, "weird_b": 1})
    picker_a = next(p for p in dialog._pickers if p.raw_class == "weird_a")
    picker_b = next(p for p in dialog._pickers if p.raw_class == "weird_b")
    # weird_a -> full remap into L1's first class; weird_b -> parked in L2 (no class).
    l1_name = levels.level_classes(1)[0][0]
    picker_a._choose_level(1)
    picker_a._choose_target(l1_name)
    picker_b._choose_level(2)  # level chosen, no class picked = park
    assert dialog.mappings() == {"weird_a": (1, l1_name)}
    assert dialog.level_only() == {"weird_b": 2}
    dialog.deleteLater()


def test_redefine_dialog_preselects_parked_level() -> None:
    window = _window()
    # A class previously parked in L3 reopens with that level pre-chosen (park state).
    dialog = RedefineDialog(window, {"weird": 2}, {"weird": 3})
    picker = dialog._pickers[0]
    assert picker.choice() == ("park", 3)
    assert dialog.level_only() == {"weird": 3}
    assert dialog.mappings() == {}
    dialog.deleteLater()


def test_object_rows_dim_when_class_hidden() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        anns = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3]), _poly_ann(cid, [0, 0, 2, 0, 2, 2, 0, 2])]
        window.annotation_store[1][basename] = list(anns)
        window._set_mode("validation")
        window._set_level(1)
        window._on_class_expand_toggled(cid, True)

        rows = window.class_list_container.findChildren(ObjectRowButton)
        assert len(rows) == 2 and all(not r._is_dimmed() for r in rows)  # class visible -> bright

        # Hiding the whole class dims every object row under it.
        window._on_class_toggled(cid, False)
        assert all(r._is_dimmed() for r in window.class_list_container.findChildren(ObjectRowButton))

        # Showing it again un-dims them.
        window._on_class_toggled(cid, True)
        assert all(not r._is_dimmed() for r in window.class_list_container.findChildren(ObjectRowButton))


def test_object_row_dims_when_individually_hidden() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        anns = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3]), _poly_ann(cid, [0, 0, 2, 0, 2, 2, 0, 2])]
        window.annotation_store[1][basename] = list(anns)
        window._set_mode("validation")
        window._set_level(1)
        window._on_class_expand_toggled(cid, True)

        # Toggle the first object's visibility through its row signal (so the row
        # itself, not just the overlay, reflects the hide).
        row0 = next(r for r in window.class_list_container.findChildren(ObjectRowButton) if r.annotation is anns[0])
        row0.activated.emit(anns[0], False)

        rows = {id(r.annotation): r for r in window.class_list_container.findChildren(ObjectRowButton)}
        assert rows[id(anns[0])]._is_dimmed() is True   # this object hidden -> dim
        assert rows[id(anns[1])]._is_dimmed() is False  # its sibling stays bright


def test_default_shortcuts_only_undo_redo_save_enabled() -> None:
    window = _window()
    # Out of the box Undo / Redo / Save and copy/paste have keys; everything else is unbound.
    default_enabled = {"undo", "redo", "save", "copy_object", "paste_object"}
    enabled = {aid for aid in window.shortcut_order() if window.shortcut_primary(aid)}
    assert enabled == default_enabled
    # The live dispatch index resolves only those actions (redo's reserved alias too).
    assert set(window._shortcut_index.values()) == default_enabled
    assert window._shortcut_index.get("Ctrl+Z") == "undo"
    assert window._shortcut_index.get("Ctrl+S") == "save"
    assert window._shortcut_index.get("Ctrl+C") == "copy_object"
    assert window._shortcut_index.get("Ctrl+V") == "paste_object"


def test_clear_shortcut_disables_and_rebind_reenables() -> None:
    window = _window()
    window._save_shortcuts = lambda: None  # don't touch the real on-disk config
    assert window._shortcut_index.get("Ctrl+S") == "save"

    window.clear_shortcut("save")
    assert window.shortcut_primary("save") == ""
    assert "Ctrl+S" not in window._shortcut_index  # cleared -> disabled

    # The same (or any) key can be reassigned afterwards to re-enable it.
    ok, _msg = window.try_rebind_shortcut("save", "Ctrl+S")
    assert ok is True
    assert window._shortcut_index.get("Ctrl+S") == "save"


def test_no_fixed_alias_shortcuts_remain() -> None:
    window = _window()
    window._save_shortcuts = lambda: None
    # The old reserved aliases are gone: nothing has an alias, and Space / Return /
    # Enter / Shift+Space / Ctrl+Shift+Z resolve to no action.
    assert all(window.shortcut_alias_text(aid) == "" for aid in window.shortcut_order())
    for key in ("Space", "Return", "Enter", "Shift+Space", "Ctrl+Shift+Z"):
        assert key not in window._shortcut_index

    # Assigning a key enables exactly that one key — no alias tags along.
    ok, _msg = window.try_rebind_shortcut("next_image", "N")
    assert ok is True
    assert window._shortcut_index.get("N") == "next_image"
    assert "Space" not in window._shortcut_index


def test_space_swallower_eats_button_space_but_not_capture() -> None:
    from PyQt6.QtWidgets import QCheckBox, QPushButton

    from src.qt_main import _ButtonSpaceSwallower

    assert _ButtonSpaceSwallower._should_swallow(QPushButton("x")) is True
    assert _ButtonSpaceSwallower._should_swallow(QCheckBox("x")) is False  # checkboxes keep Space
    assert _ButtonSpaceSwallower._should_swallow(None) is False

    cap = KeyCapButton("")
    assert _ButtonSpaceSwallower._should_swallow(cap) is True  # not capturing -> swallow
    cap.set_listening(True)
    assert _ButtonSpaceSwallower._should_swallow(cap) is False  # capturing a key needs Space


def test_hiding_all_objects_dims_class_and_clicking_class_restores() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        anns = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3]), _poly_ann(cid, [0, 0, 2, 0, 2, 2, 0, 2])]
        window.annotation_store[1][basename] = list(anns)
        window._set_mode("validation")
        window._set_level(1)
        window._on_class_expand_toggled(cid, True)
        bubble = window.class_checkboxes[cid]

        # Hide each object individually; once the LAST one is hidden the class dims.
        for row in list(window.class_list_container.findChildren(ObjectRowButton)):
            row.activated.emit(row.annotation, False)
        assert window.visible_by_category[cid] is False     # class auto-dimmed
        assert bubble.isChecked() is False
        assert window.current_overlay_items == []           # nothing is visible

        # Clicking the class brings every object back in a single click.
        bubble.setChecked(True)  # emits toggled -> _on_class_toggled(cid, True)
        assert window.visible_by_category[cid] is True
        assert len([o for o in window.current_overlay_items if o.get("annotation") is not None]) == 2
        assert all(not r._is_dimmed() for r in window.class_list_container.findChildren(ObjectRowButton))


def test_clicking_one_object_while_class_hidden_isolates_it() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        anns = [
            _poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3]),
            _poly_ann(cid, [0, 0, 2, 0, 2, 2, 0, 2]),
            _poly_ann(cid, [0, 0, 1, 0, 1, 1, 0, 1]),
        ]
        window.annotation_store[1][basename] = list(anns)
        window._set_mode("validation")
        window._set_level(1)
        window._on_class_expand_toggled(cid, True)
        bubble = window.class_checkboxes[cid]

        # Hide the whole class via its pill — this path leaves _hidden_objects EMPTY
        # (the regression: a plain object toggle would then hide the clicked one and
        # reveal all the others).
        window._on_class_toggled(cid, False)
        assert window.current_overlay_items == []

        # Click one object row -> ONLY that object shows, and the class re-lights.
        target = anns[0]
        rows = {id(r.annotation): r for r in window.class_list_container.findChildren(ObjectRowButton)}
        rows[id(target)].activated.emit(target, False)

        shown = [o["annotation"] for o in window.current_overlay_items if o.get("annotation") is not None]
        assert shown == [target]                        # only the clicked object, not the others
        assert window.visible_by_category[cid] is True  # class on (>=1 object visible)
        assert bubble.isChecked() is True
        rows = {id(r.annotation): r for r in window.class_list_container.findChildren(ObjectRowButton)}
        assert rows[id(target)]._is_dimmed() is False
        assert all(rows[id(a)]._is_dimmed() for a in anns[1:])  # the rest stay dimmed/hidden


def test_class_visibility_resets_on_image_navigation() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        src_path = Path(src)
        _make_image(src_path, "scene_000000001.png")
        _make_image(src_path, "scene_000000002.png")
        project = Project.create(parent, "Nav", image_size=STUB_SIZE)
        window._activate_project(project)
        project.import_images(src)
        project.promote_pending()
        window._apply_project_session(reset_index=False)
        assert len(window.images) == 2

        name, cid = _l1_class()
        window.annotation_store[1]["scene_000000001.png"] = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])]
        window.annotation_store[1]["scene_000000002.png"] = [_poly_ann(cid, [0, 0, 2, 0, 2, 2, 0, 2])]
        window._set_mode("validation")
        window._set_level(1)
        window.index = 0
        window._load_current_image(reset_fit=False)

        # Hide the class on image 1.
        window._on_class_toggled(cid, False)
        assert window.visible_by_category.get(cid) is False
        assert window.current_overlay_items == []

        # Next image -> class visibility resets (pill visible again, object shows).
        window._next_image()
        assert window.visible_by_category.get(cid, True) is True
        assert window.class_checkboxes[cid].isChecked() is True
        assert [o["label"] for o in window.current_overlay_items if o.get("annotation") is not None] == [name]

        # Hide it again here, then go back -> image 1 is reset too (not still hidden).
        window._on_class_toggled(cid, False)
        window._prev_image()
        assert window.visible_by_category.get(cid, True) is True
        assert window.class_checkboxes[cid].isChecked() is True
        assert [o["label"] for o in window.current_overlay_items if o.get("annotation") is not None] == [name]


def test_keycap_shows_placeholder_when_unbound() -> None:
    cap = KeyCapButton("")
    assert cap.text() == KeyCapButton.UNBOUND_TEXT  # empty binding -> placeholder
    cap.set_binding_text("N")
    assert cap.text() == "N"
    cap.set_binding_text("")
    assert cap.text() == KeyCapButton.UNBOUND_TEXT


def test_select_outside_edit_is_non_destructive_with_row_highlight() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])
        window.annotation_store[1][basename] = [ann]
        window._set_mode("validation")
        window._set_level(1)

        # Outside Edit mode: selecting is non-destructive — the list stays (no
        # change-class panel), Delete is disabled, and a no-op.
        assert window.edit_objects_button.isChecked() is False
        window.canvas._set_selected(ann)
        assert window._redefine_panel_active is False
        assert window.delete_button.isEnabled() is False
        rows = [r for r in window.class_list_container.findChildren(ObjectRowButton) if r.annotation is ann]
        assert len(rows) == 1 and rows[0]._selected is True  # class auto-expanded + row highlighted
        assert window._delete_selected_annotation() is False  # delete gated to Edit mode
        assert window._current_level_annotations() == [ann]

        # Turn Edit on: now selection offers change-class + Delete works.
        window.edit_objects_button.setChecked(True)
        assert window._redefine_panel_active is True
        assert window.delete_button.isEnabled() is True
        assert window._delete_selected_annotation() is True
        assert window._current_level_annotations() == []


def _render_overlays(canvas) -> None:
    from PyQt6.QtGui import QPainter

    img = QImage(max(1, canvas.width()), max(1, canvas.height()), QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    ix, iy, _w, _h = canvas._fit_display_rect()
    canvas._draw_overlays(painter, ix, iy)
    painter.end()


def test_label_shift_drag_repositions_and_selects() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 4, 0, 4, 4, 0, 4])  # fills the 4x4 image
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)
        canvas = window.canvas
        canvas.fit_to_view()

        # Painting records the label rectangle so a press can land on it.
        _render_overlays(canvas)
        assert canvas._label_hit_rects, "label rect should be recorded for an on-screen object"
        rect, hit_ann = canvas._label_hit_rects[-1]
        assert hit_ann is ann

        center = rect.center()
        shift = Qt.KeyboardModifier.ShiftModifier
        canvas.mousePressEvent(_FakeMouse(center.x(), center.y(), Qt.MouseButton.LeftButton, shift))
        assert canvas._label_drag_ann is ann
        assert canvas.selected_annotation() is ann  # selected/highlighted while dragging

        canvas.mouseMoveEvent(_FakeMouse(center.x() + 20, center.y() + 12, Qt.MouseButton.LeftButton, shift))
        canvas.mouseReleaseEvent(_FakeMouse(center.x() + 20, center.y() + 12, Qt.MouseButton.LeftButton))
        assert canvas._label_drag_ann is None
        off = canvas._label_offsets[id(ann)]
        # Offsets are stored in IMAGE space now (screen drag / zoom) so the label
        # keeps its place on the object across zoom; scaling back by the zoom
        # recovers the 20x12 screen-pixel drag.
        zoom = canvas._zoom
        assert abs(off.x() * zoom - 20) < 1.5 and abs(off.y() * zoom - 12) < 1.5

        # Loading a new image clears the (session-only) offsets.
        canvas._clear_label_offsets()
        assert canvas._label_offsets == {}


def test_label_plain_click_selects_object() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 4, 0, 4, 4, 0, 4])
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)  # validation on a project: selection is enabled
        canvas = window.canvas
        canvas.fit_to_view()

        _render_overlays(canvas)
        rect, hit_ann = canvas._label_hit_rects[-1]
        assert hit_ann is ann
        center = rect.center()
        # A plain (no-modifier) click on the LABEL selects the object, just like
        # clicking its body — and does NOT start a reposition drag (that needs Shift).
        canvas.mousePressEvent(_FakeMouse(center.x(), center.y(), Qt.MouseButton.LeftButton))
        assert canvas.selected_annotation() is ann
        assert canvas._label_drag_ann is None


def test_selected_object_label_gets_white_highlight() -> None:
    from PyQt6.QtGui import QPainter

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 4, 0, 4, 4, 0, 4])
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)
        canvas = window.canvas
        canvas.fit_to_view()

        def render_and_count_border_white() -> int:
            img = QImage(max(1, canvas.width()), max(1, canvas.height()), QImage.Format.Format_ARGB32)
            img.fill(0)
            painter = QPainter(img)
            ix, iy, _w, _h = canvas._fit_display_rect()
            canvas._draw_overlays(painter, ix, iy)
            painter.end()
            rect = canvas._label_hit_rects[-1][0].adjusted(-4.0, -4.0, 4.0, 4.0)
            x0, y0 = max(0, int(rect.left())), max(0, int(rect.top()))
            x1, y1 = min(img.width(), int(rect.right()) + 1), min(img.height(), int(rect.bottom()) + 1)
            white = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    c = img.pixelColor(x, y)
                    if c.alpha() > 200 and c.red() > 245 and c.green() > 245 and c.blue() > 245:
                        white += 1
            return white

        canvas._set_selected(None)
        base_white = render_and_count_border_white()  # only the white glyph text
        canvas._set_selected(ann)
        selected_white = render_and_count_border_white()  # glyphs + the white border ring
        assert selected_white > base_white


def test_selected_object_row_keeps_white_border_on_hover() -> None:
    from PyQt6.QtGui import QColor

    row = ObjectRowButton({}, "Object 1", QColor(200, 90, 90), hidden=False)
    white = "2px solid rgba(255,255,255,0.95)"
    row.set_selected(True)
    # The white highlight must be in BOTH the base rule and the :hover rule so that
    # hovering a selected row doesn't erase the selection border.
    _base, hover_rule = row.styleSheet().split(":hover", 1)
    assert white in _base and white in hover_rule
    # Unselected rows carry no white border and just darken on hover.
    row.set_selected(False)
    assert white not in row.styleSheet()


def test_label_hidden_when_object_is_off_screen() -> None:
    from PyQt6.QtCore import QPoint

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 4, 0, 4, 4, 0, 4])
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)
        canvas = window.canvas
        canvas.fit_to_view()

        _render_overlays(canvas)
        assert canvas._label_hit_rects  # visible -> label drawn

        # Pan far away so the object leaves the view entirely.
        canvas._fit_mode = False
        canvas._zoom = 40.0
        canvas._offset = QPoint(12000, 12000)
        _render_overlays(canvas)
        assert canvas._label_hit_rects == []  # off-screen -> no label (no edge pile-up)


def test_label_kept_inside_image_rectangle() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=40)
        name, cid = _l1_class()
        # An object hugging the RIGHT edge — its centred label would naturally spill
        # past the image into the black area; it must be clamped back inside.
        ann = _poly_ann(cid, [30, 18, 40, 18, 40, 22, 30, 22])
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)
        cv = window.canvas
        cv.resize(400, 400)
        cv.fit_to_view()
        _render_overlays(cv)
        assert cv._label_hit_rects
        rect, _hit = cv._label_hit_rects[-1]
        ix, iy, sw, sh = cv._fit_display_rect()
        # The whole label box sits within the image's displayed rectangle.
        assert rect.left() >= ix - 1.0 and rect.right() <= ix + sw + 1.0
        assert rect.top() >= iy - 1.0 and rect.bottom() <= iy + sh + 1.0


def test_label_leader_line_drawn_when_dragged_far() -> None:
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainter

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src, size=20)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [6, 6, 12, 6, 12, 12, 6, 12])  # centred ~ (9, 9), clear of edges
        window.annotation_store[1][basename] = [ann]
        window._set_level(1)
        canvas = window.canvas
        canvas.resize(400, 400)
        canvas.fit_to_view()

        # Drag the label far from the object (well past the leader threshold).
        canvas._label_offsets[id(ann)] = QPointF(120.0, 120.0)

        ix, iy, _w, _h = canvas._fit_display_rect()
        item = next(o for o in canvas._overlay_items if o.get("annotation") is ann)
        cx, cy = item["center"]
        acx, acy = ix + cx * canvas._zoom, iy + cy * canvas._zoom  # object anchor on screen

        img = QImage(canvas.width(), canvas.height(), QImage.Format.Format_RGB32)
        img.fill(0)
        painter = QPainter(img)
        canvas._draw_overlays(painter, ix, iy)
        painter.end()

        rect, _hit = canvas._label_hit_rects[-1]   # the dragged label box
        lcx, lcy = rect.center().x(), rect.center().y()
        midx, midy = (acx + lcx) / 2.0, (acy + lcy) / 2.0  # midpoint of the leader line
        # A pixel on/near the leader is lit (scan a small neighbourhood for robustness).
        lit = any(
            0 <= int(midx) + dx < img.width() and 0 <= int(midy) + dy < img.height()
            and img.pixelColor(int(midx) + dx, int(midy) + dy).value() > 0
            for dx in range(-2, 3) for dy in range(-2, 3)
        )
        assert lit, "expected a leader line between the object and its dragged label"


def test_draw_mode_deselects_and_restores_class_list() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        name, cid = _l1_class()
        ann = _poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])
        window.annotation_store[1][basename] = [ann]
        window._set_mode("annotation")
        window._set_level(1)

        # In Edit mode, selecting the object swaps the sidebar to the change-class panel.
        window.edit_objects_button.setChecked(True)
        window.canvas._set_selected(ann)
        assert window.canvas.selected_annotation() is ann
        assert window._redefine_panel_active is True

        # Turning on Draw deselects AND restores the normal class list, so the next
        # class click sets the DRAW class instead of reclassifying the object.
        window.draw_button.setChecked(True)
        assert window.canvas.selected_annotation() is None
        assert window._redefine_panel_active is False


def _pump_until(predicate, timeout_ms: int = 4000) -> bool:
    """Spin the Qt event loop until ``predicate()`` or a timeout (for QThread tests)."""
    from PyQt6.QtCore import QThread

    app = QApplication.instance()
    for _ in range(timeout_ms):
        if predicate():
            return True
        app.processEvents()
        QThread.msleep(1)
    return predicate()


def test_run_in_background_runs_off_ui_thread_and_delivers_on_ui_thread() -> None:
    import threading

    window = _window()
    main_thread = threading.get_ident()
    captured: dict = {}

    def work():
        captured["work_thread"] = threading.get_ident()
        return 42

    def done(result, error):
        captured["done_thread"] = threading.get_ident()
        captured["result"] = result
        captured["error"] = error

    assert window._run_in_background("test", work, done) is True
    # A second task is refused while one is running.
    assert window._run_in_background("test2", lambda: None, lambda r, e: None) is False

    assert _pump_until(lambda: window._bg_task is None and "result" in captured)
    assert captured["result"] == 42 and captured["error"] is None
    assert captured["work_thread"] != main_thread   # work ran OFF the UI thread
    assert captured["done_thread"] == main_thread    # done ran ON the UI thread


def test_run_in_background_reports_worker_errors() -> None:
    window = _window()
    captured: dict = {}

    def work():
        raise RuntimeError("boom")

    def done(result, error):
        captured["result"] = result
        captured["error"] = error

    window._run_in_background("test", work, done)
    assert _pump_until(lambda: window._bg_task is None and "error" in captured)
    assert isinstance(captured["error"], RuntimeError) and captured["result"] is None


def test_oriented_box_width_clamped_to_image_bounds() -> None:
    # v1.2 fix: with p1->p2 along the top edge, dragging the thickness past the image
    # height must stop AT the border. Without bounds the far corners escaped (the bug:
    # you could set the width through the border after clicking near the edge).
    p1, p2 = (10.0, 0.0), (40.0, 0.0)
    p3 = (25.0, 30.0)  # thickness dragged 30px down; the image is only 20px tall
    unclamped, _rot = ImageCanvas._oriented_box(p1, p2, p3)
    assert max(y for _x, y in unclamped) == 30.0  # escapes the image with no bounds
    clamped, _rot = ImageCanvas._oriented_box(p1, p2, p3, (100.0, 20.0))
    assert clamped is not None
    for x, y in clamped:
        assert -0.5 <= x <= 100.5 and -0.5 <= y <= 20.5  # every corner stays inside
    assert max(y for _x, y in clamped) == 20.0  # thickness stops exactly at the border


def test_remove_last_point_any_steps_back_drawing() -> None:
    window = _window()
    canvas = window.canvas
    # Polygon: pops the most recently placed vertex, one at a time.
    canvas._poly_points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    assert canvas.remove_last_point_any() is True
    assert canvas._poly_points == [(0.0, 0.0), (1.0, 0.0)]
    # Rotated box mid-draw: pops the centre-line end and drops the rotated flag.
    canvas._poly_points = []
    canvas._box_pts = [(0.0, 0.0), (5.0, 0.0)]
    canvas._box_rotated = True
    assert canvas.remove_last_point_any() is True
    assert canvas._box_pts == [(0.0, 0.0)]
    assert canvas._box_rotated is False
    # Nothing pending -> no-op, so Ctrl+Z falls through to the normal object undo.
    canvas._box_pts = []
    assert canvas.remove_last_point_any() is False


def test_ctrl_z_removes_last_point_while_drawing() -> None:
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent

    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, _basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)
        window.canvas.set_draw_shape("polygon", (255, 255, 255))
        window.canvas._poly_points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
        assert window._shortcut_index.get("Ctrl+Z") == "undo"
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        window.keyPressEvent(event)
        # The in-progress vertex is dropped, and the committed-object undo stack is
        # left untouched (points never enter it).
        assert window.canvas._poly_points == [(0.0, 0.0), (1.0, 0.0)]
        assert window._undo_stack == []


def test_entering_draw_mode_shows_all_classes() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        _name, cid = _l1_class()
        window.annotation_store[1][basename] = [_poly_ann(cid, [0, 0, 3, 0, 3, 3, 0, 3])]
        window._set_mode("annotation")
        window._set_level(1)
        # Hide everything with the pen up.
        window._hide_all_classes()
        assert window.visible_by_category.get(cid) is False
        assert not any(it.get("annotation") for it in window.current_overlay_items)
        # Arming the pen reveals every class again (like clicking Show all).
        window.draw_button.setChecked(True)
        assert window.visible_by_category.get(cid) is True
        assert any(it.get("annotation") for it in window.current_overlay_items)


def test_shift_click_level_overlays_show_other_levels_readonly() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, basename = _project_with_image(window, parent, src)
        _l1_name, l1_cid = _l1_class()
        l2_cid = levels.category_id_for_class(2, "high_vegetation")
        window.annotation_store[1][basename] = [_poly_ann(l1_cid, [0, 0, 3, 0, 3, 3, 0, 3])]
        window.annotation_store[2][basename] = [{"category_id": l2_cid, "bbox": [0, 0, 2, 2], "area": 4}]
        window._set_mode("annotation")
        window._set_level(1)  # primary L1: only its objects show, editable
        assert not any(it.get("secondary_level") for it in window.current_overlay_items)

        # Shift+click L2: its box is layered read-only on top of L1.
        window._extra_overlay_levels.add(2)
        window._refresh_overlay_items()
        secondary = [it for it in window.current_overlay_items if it.get("secondary_level") == 2]
        assert len(secondary) == 1
        assert secondary[0]["annotation"] is None  # view-only, not selectable
        assert secondary[0]["editable"] is False
        # The L1 object is still present and still editable/selectable.
        assert any(it.get("annotation") and it.get("editable") for it in window.current_overlay_items)

        # A plain level switch collapses back to a single level (overlays dropped).
        window._set_level(1)
        assert window._extra_overlay_levels == set()
        assert not any(it.get("secondary_level") for it in window.current_overlay_items)


def test_shift_click_level_button_states_primary_vs_secondary() -> None:
    window = _window()
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        _project, _basename = _project_with_image(window, parent, src)
        window._set_mode("annotation")
        window._set_level(1)
        window._extra_overlay_levels.add(2)
        window._refresh_level_button_states()
        assert window.level_buttons[1].isChecked() is True
        assert bool(window.level_buttons[1].property("secondaryLevel")) is False  # active = primary
        assert window.level_buttons[2].isChecked() is True
        assert bool(window.level_buttons[2].property("secondaryLevel")) is True  # overlay = secondary
        assert window.level_buttons[3].isChecked() is False
        # Switching primary to L3 clears the L2 overlay's checked + secondary state.
        window._set_level(3)
        assert window.level_buttons[2].isChecked() is False
        assert bool(window.level_buttons[2].property("secondaryLevel")) is False
        assert window.level_buttons[3].isChecked() is True


def _run_all() -> int:
    # Hold the reference: a collected QApplication crashes window construction.
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as error:  # noqa: BLE001
            failures += 1
            import traceback

            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
            traceback.print_exc()
        else:
            print(f"PASS {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    # Tests never close their windows, so ~80 of them linger as top-level widgets.
    # Destroy them WHILE the QApplication is still alive: dangling QWidgets left for
    # Python's interpreter-shutdown GC can segfault under the offscreen platform.
    # Use deleteLater (NOT close) so we don't trigger any window's closeEvent — that
    # can pop an unsaved-changes modal or wait on a background task and hang.
    for widget in list(app.topLevelWidgets()):
        widget.deleteLater()
    app.processEvents()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
