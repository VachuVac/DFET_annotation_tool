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

from PyQt6.QtGui import QImage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

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
