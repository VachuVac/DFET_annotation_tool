"""Headless tests for src/project.py (Stage A of project mode).

Pure stdlib + the ``src`` package -- no PyQt6, no pytest required. Image sizes
are injected via a stub so nothing here needs a GUI.

Run from the repo root::

    python -m tests.test_project
    # or: python tests/test_project.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Allow ``python tests/test_project.py`` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import levels
from src.data_loading import stable_image_id
from src.project import (
    FORMAT_VERSION,
    Project,
    ProjectError,
    create_project,
    open_project,
)

STUB_SIZE = lambda _path: (640, 480)  # noqa: E731 - tiny injected size reader


def _make_image(folder: Path, name: str, content: bytes = b"\xff\xd8stub") -> Path:
    path = folder / name
    path.write_bytes(content)
    return path


def test_create_and_open_round_trip() -> None:
    with tempfile.TemporaryDirectory() as parent:
        project = create_project(parent, "MyProj", image_size=STUB_SIZE)
        assert project.root == Path(parent) / "MyProj"
        assert project.manifest_path.is_file()
        assert project.images_dir.is_dir()
        assert project.annotations_dir.is_dir()
        assert project.pending_path.is_file()  # empty stash written eagerly
        assert project.manifest["format_version"] == FORMAT_VERSION
        assert project.name == "MyProj"

        # Re-open the folder and the manifest survives.
        reopened = open_project(str(project.root), image_size=STUB_SIZE)
        assert reopened.name == "MyProj"
        assert reopened.registry_images() == []

        # Opening via the manifest path directly also works.
        via_manifest = open_project(str(project.manifest_path), image_size=STUB_SIZE)
        assert via_manifest.root == project.root


def test_create_rejects_bad_names_and_collisions() -> None:
    with tempfile.TemporaryDirectory() as parent:
        for bad in ("", "   ", "a/b", "a\\b", ".", ".."):
            try:
                Project.create(parent, bad, image_size=STUB_SIZE)
            except ProjectError:
                pass
            else:
                raise AssertionError(f"expected ProjectError for name {bad!r}")

        Project.create(parent, "Dup", image_size=STUB_SIZE)
        try:
            Project.create(parent, "Dup", image_size=STUB_SIZE)
        except ProjectError:
            pass
        else:
            raise AssertionError("expected ProjectError on duplicate folder")


def test_open_rejects_missing_and_corrupt_and_future() -> None:
    with tempfile.TemporaryDirectory() as parent:
        empty = Path(parent) / "empty"
        empty.mkdir()
        for func in (lambda: open_project(str(empty), image_size=STUB_SIZE),):
            try:
                func()
            except ProjectError:
                pass
            else:
                raise AssertionError("expected ProjectError for missing manifest")

        corrupt = Path(parent) / "corrupt"
        corrupt.mkdir()
        (corrupt / "project.json").write_text("{not json", encoding="utf-8")
        try:
            open_project(str(corrupt), image_size=STUB_SIZE)
        except ProjectError:
            pass
        else:
            raise AssertionError("expected ProjectError for corrupt manifest")

        future = Path(parent) / "future"
        future.mkdir()
        (future / "project.json").write_text(
            '{"format_version": 999, "name": "x"}', encoding="utf-8"
        )
        try:
            open_project(str(future), image_size=STUB_SIZE)
        except ProjectError:
            pass
        else:
            raise AssertionError("expected ProjectError for future format_version")


def test_import_images_dedup() -> None:
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        src_path = Path(src)
        _make_image(src_path, "img_000000001.jpg")
        _make_image(src_path, "img_000000002.jpg")
        # Same last-9-of-stem as image 1 -> same stable id -> must dedup.
        _make_image(src_path, "other_000000001.png")
        _make_image(src_path, "notanimage.txt")

        project = create_project(parent, "Imp", image_size=STUB_SIZE)
        report = project.import_images(str(src_path))

        # Exactly two distinct ids land; the collider and the .txt are not copied.
        assert len(report["copied"]) == 2, report
        assert "notanimage.txt" not in report["copied"]
        assert len(project.registry_images()) == 2
        for record in project.registry_images():
            assert record["width"] == 640 and record["height"] == 480
            assert (project.images_dir / record["file_name"]).is_file()

        # The colliding filename was skipped, not copied (no overwrite).
        assert "other_000000001.png" in report["skipped"]

        # Re-import is fully idempotent.
        report2 = project.import_images(str(src_path))
        assert report2["copied"] == []
        assert len(project.registry_images()) == 2

        # Manifest persisted the registry across a re-open.
        reopened = open_project(str(project.root), image_size=STUB_SIZE)
        assert len(reopened.registry_images()) == 2


def test_level_round_trip() -> None:
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        src_path = Path(src)
        _make_image(src_path, "scene_000000001.jpg")
        project = create_project(parent, "Lvl", image_size=STUB_SIZE)
        project.import_images(str(src_path))

        # A level-1 polygon for the imported image.
        poly = {
            "category_id": 1,
            "segmentation": [[0, 0, 10, 0, 10, 10, 0, 10]],
            "bbox": [0, 0, 10, 10],
            "area": 100.0,
            "rotation": 0.0,
            "iscrowd": 0,
        }
        store = {"scene_000000001.jpg": [poly]}
        count = project.write_level(1, store)
        assert count == 1

        path = project.level_json_path(1)
        assert path.is_file()

        back = project.read_level(1)
        assert "scene_000000001.jpg" in back
        restored = back["scene_000000001.jpg"][0]
        assert restored["segmentation"] == poly["segmentation"]
        assert restored["category_id"] == 1
        # ids/image_id are (re)assigned on write.
        assert "id" in restored and "image_id" in restored

        # The COCO file embeds the master registry + level categories.
        import json

        with path.open(encoding="utf-8") as handle:
            coco = json.load(handle)
        assert coco["level"] == 1
        assert coco["categories"] == levels.level_categories(1)
        assert len(coco["images"]) == 1
        assert coco["images"][0]["id"] == stable_image_id("scene_000000001.jpg")


def test_stash_then_promote_on_image_add() -> None:
    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src:
        project = create_project(parent, "Stash", image_size=STUB_SIZE)

        orphan_name = "ghost_000000042.jpg"
        orphan_id = stable_image_id(orphan_name)
        annotation = {
            "category_id": 2,
            "segmentation": [[1, 1, 5, 1, 5, 5, 1, 5]],
            "bbox": [1, 1, 4, 4],
            "area": 16.0,
            "rotation": 0.0,
            "iscrowd": 0,
        }
        project.stash_annotations(
            [{"level": 3, "stable_image_id": orphan_id, "file_name": orphan_name, "annotation": annotation}]
        )
        assert len(project.read_pending()) == 1

        # Image not present yet -> nothing promotes.
        report = project.promote_pending()
        assert report["promoted"] == 0
        assert len(project.read_pending()) == 1
        assert project.read_level(3) == {}

        # Add the image; now the stashed annotation is rescued into level 3.
        src_path = Path(src)
        _make_image(src_path, orphan_name)
        project.import_images(str(src_path))
        report = project.promote_pending()
        assert report["promoted"] == 1
        assert report["by_level"] == {3: 1}
        assert project.read_pending() == []

        store = project.read_level(3)
        assert orphan_name in store
        assert store[orphan_name][0]["segmentation"] == annotation["segmentation"]


def test_unmapped_entry_ids_and_pop() -> None:
    with tempfile.TemporaryDirectory() as parent:
        project = create_project(parent, "Ids", image_size=STUB_SIZE)
        ann = {"bbox": [0, 0, 4, 4], "segmentation": [[0, 0, 4, 0, 4, 4, 0, 4]], "area": 16}
        project.stash_unmapped(
            [
                {"raw_class": "banana", "stable_image_id": 1, "file_name": "a.png", "source_annotation": ann},
                {"raw_class": "banana", "stable_image_id": 1, "file_name": "a.png", "source_annotation": ann},
            ]
        )
        ids = [entry["id"] for entry in project.read_unmapped()]
        assert len(ids) == 2 and all(ids) and ids[0] != ids[1]  # unique per object

        assert project.get_unmapped_entry(ids[0])["raw_class"] == "banana"
        popped = project.pop_unmapped_entry(ids[0])
        assert popped["id"] == ids[0]
        assert [entry["id"] for entry in project.read_unmapped()] == [ids[1]]  # only one removed
        assert project.pop_unmapped_entry("nope") is None  # unknown id is a no-op
        assert len(project.read_unmapped()) == 1

        # restore_unmapped_entry re-inserts a popped entry verbatim (undo support).
        project.restore_unmapped_entry(popped)
        restored = {entry["id"] for entry in project.read_unmapped()}
        assert restored == {ids[0], ids[1]}  # the popped one is back, same id
        project.restore_unmapped_entry(popped)  # idempotent: no duplicate
        assert len(project.read_unmapped()) == 2


def test_export_zip_round_trips() -> None:
    import zipfile

    with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        _make_image(Path(src), "scene_000000001.png")
        project = create_project(parent, "Exp", image_size=STUB_SIZE)
        project.import_images(str(src))
        project.write_level(1, {"scene_000000001.png": [{"category_id": 1, "bbox": [0, 0, 4, 4], "area": 16}]})

        written = project.export_zip(str(Path(out) / "bundle"))  # no .zip -> enforced
        assert written.suffix == ".zip" and written.is_file()

        with zipfile.ZipFile(written) as archive:
            names = archive.namelist()
        # Contents live at the zip ROOT (no <ProjectName>/ wrapper), forward slashes.
        # Extracting <ProjectName>.zip then yields a single openable folder instead
        # of a doubly-nested one.
        assert "project.json" in names, names
        assert "images/scene_000000001.png" in names, names
        assert "annotations/level1.json" in names, names
        assert not any(name.startswith("Exp/") for name in names), names

        # Unzipping the root-level entries yields a folder Project.open accepts directly.
        extract = Path(out) / "x"
        with zipfile.ZipFile(written) as archive:
            archive.extractall(extract)
        reopened = open_project(str(extract), image_size=STUB_SIZE)
        assert len(reopened.registry_images()) == 1
        assert reopened.read_level(1).get("scene_000000001.png")


def test_applog_prune_keeps_most_recent() -> None:
    import os as _os

    from src import applog

    with tempfile.TemporaryDirectory() as parent:
        directory = Path(parent)
        for i in range(5):
            path = directory / f"session_{i}.log"
            path.write_text("x", encoding="utf-8")
            _os.utime(path, (i, i))  # ascending mtime -> i=4 is newest
        applog._prune(directory, keep=3)
        remaining = sorted(p.name for p in directory.glob("session_*.log"))
        assert remaining == ["session_2.log", "session_3.log", "session_4.log"]


def test_label_studio_parses_polygons_and_boxes() -> None:
    # L1/L3 Label Studio exports use polygonlabels; L2 uses (rotated) rectanglelabels.
    # Both must parse from one export (the L1/L3 polygon case was silently dropped).
    from src.data_loading import _parse_label_studio

    tasks = [
        {
            "data": {"image": "/up/img_000000001.jpg"},
            "annotations": [
                {
                    "result": [
                        {
                            "type": "polygonlabels",
                            "original_width": 100,
                            "original_height": 200,
                            "value": {
                                "points": [[10, 10], [50, 10], [50, 40]],  # percentages
                                "polygonlabels": ["road_asphalt"],
                            },
                        },
                        {
                            "type": "rectanglelabels",
                            "original_width": 100,
                            "original_height": 200,
                            "value": {
                                "x": 10, "y": 5, "width": 20, "height": 10, "rotation": 30,
                                "rectanglelabels": ["car"],
                            },
                        },
                    ]
                }
            ],
        }
    ]
    _images, _by_id, annotations_by_image_id, categories = _parse_label_studio(tasks)
    anns = annotations_by_image_id[0]
    assert len(anns) == 2
    by_class = {categories[a["category_id"]]: a for a in anns}

    poly = by_class["road_asphalt"]
    assert len(poly["segmentation"][0]) == 6  # 3 points -> 6 coords (a real polygon)
    assert poly["segmentation"][0][:2] == [10.0, 20.0]  # 10% of (100, 200) -> pixels
    assert poly["rotation"] == 0.0

    box = by_class["car"]
    assert len(box["segmentation"][0]) == 8  # rotated 4-corner quad
    assert abs(box["rotation"] - 30.0) < 1e-9  # rotation preserved


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except Exception as error:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"FAIL {test.__name__}: {type(error).__name__}: {error}")
        else:
            print(f"PASS {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
