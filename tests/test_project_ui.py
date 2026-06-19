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
from src.project import Project
from src.qt_main import PyQtAnnotationReview

STUB_SIZE = lambda _path: (640, 480)  # noqa: E731


def _make_image(folder: Path, name: str) -> None:
    # A real, decodable image so the window's QPixmap loader keeps it (validation
    # mode discards images it can't decode).
    image = QImage(4, 4, QImage.Format.Format_RGB32)
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
    # Index 0 == welcome page; no project, import menu disabled.
    assert window.view_stack.currentIndex() == 0
    assert window.project is None
    assert window.import_menu.isEnabled() is False
    assert window.action_close_project.isEnabled() is False
    assert window.windowTitle() == "Annotation Workbench"


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
    for token in ("Project ▸", "Validation", "Annotate", "Edit objects (T)"):
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
        assert window.import_menu.isEnabled() is True
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
        assert window.windowTitle() == "Annotation Workbench"


def _project_with_image(window, parent, src, basename="scene_000000001.png"):
    """Create + activate a project containing one real image; return (project, basename)."""
    _make_image(Path(src), basename)
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

    def __init__(self, x: float, y: float, button) -> None:
        self._p = QPointF(x, y)
        self._b = button

    def button(self):
        return self._b

    def buttons(self):
        return self._b

    def position(self):
        return self._p

    def modifiers(self):
        return Qt.KeyboardModifier.NoModifier


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
        _project, basename = _project_with_image(window, parent, src)
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
        _project, basename = _project_with_image(window, parent, src)
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

        # A polygon body (L1) is NOT movable — pressing it only selects.
        window._set_level(1)
        poly = {"category_id": 1, "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]], "bbox": [0, 0, 10, 10], "area": 100}
        window.annotation_store[1].setdefault(basename, []).append(poly)
        window._refresh_overlay_items()
        canvas.mousePressEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))
        assert canvas._box_move_item is None           # polygons don't move whole
        assert canvas.selected_annotation() is poly
        canvas.mouseReleaseEvent(_FakeMouse(ix + 5 * z, iy + 5 * z, Qt.MouseButton.LeftButton))


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
        assert window.image_id_label.text() == expected
        assert window._current_image_id_text() == expected
        # The badge mirrors exactly what Copy ID writes to the clipboard.
        window._copy_image_id()
        assert QApplication.clipboard().text() == expected


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
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
