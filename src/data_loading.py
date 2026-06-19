"""Data loading functions for COCO annotations."""

import hashlib
import json
import math
import os
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path
import shutil

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def _load_json_safely(path: Path):
    """Read and parse a JSON file, returning None if it cannot be read/parsed."""
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            return json.load(file_handle)
    except Exception:
        return None


def is_label_studio_export(data) -> bool:
    """True if ``data`` is a Label Studio native export (a list of task dicts).

    Distinguished from a COCO results/predictions list (also a top-level list) by
    the per-task ``annotations`` + ``data`` keys that only Label Studio emits.
    """
    return (
        isinstance(data, list)
        and len(data) > 0
        and isinstance(data[0], dict)
        and "annotations" in data[0]
        and "data" in data[0]
    )


def label_studio_has_rotation(data) -> bool:
    """True if any rectangle region in a Label Studio export has a nonzero rotation."""
    if not isinstance(data, list):
        return False
    for task in data:
        if not isinstance(task, dict):
            continue
        for annotation in task.get("annotations", []) or []:
            for region in annotation.get("result", []) or []:
                if not isinstance(region, dict):
                    continue
                rotation = (region.get("value", {}) or {}).get("rotation", 0)
                if abs(float(rotation or 0)) > 1e-6:
                    return True
    return False


def _find_annotation_file(root_path: Path) -> Path | None:
    """Pick the best annotation JSON under ``root_path``.

    A Label Studio export that contains rotated boxes wins, because only it
    carries the orientation; otherwise the canonical COCO ``result.json`` is
    used, falling back to a rotation-free Label Studio export if that is all
    there is.
    """
    json_files = sorted(root_path.rglob("*.json"), key=lambda path: len(path.parts))

    result_files: list[Path] = []
    label_studio_files: list[tuple[Path, bool]] = []
    for json_file in json_files:
        if json_file.name == "result.json":
            result_files.append(json_file)
        data = _load_json_safely(json_file)
        if data is not None and is_label_studio_export(data):
            label_studio_files.append((json_file, label_studio_has_rotation(data)))

    rotated_label_studio = [path for path, has_rotation in label_studio_files if has_rotation]
    if rotated_label_studio:
        return rotated_label_studio[0]
    if result_files:
        return result_files[0]
    if label_studio_files:
        return label_studio_files[0][0]
    return None


def find_coco_paths(search_root: str) -> tuple[str, str]:
    """Find the annotation file and image directory in a COCO/Label Studio export."""
    root_path = Path(search_root)

    annotations_path = _find_annotation_file(root_path)
    if annotations_path is None:
        raise FileNotFoundError(
            "Could not find result.json or a Label Studio export JSON in selected input."
        )

    images_candidate = annotations_path.parent / "images"
    if not images_candidate.is_dir():
        images_candidate = root_path / "images"

    if not images_candidate.is_dir():
        image_dirs = sorted(
            (path for path in root_path.rglob("images") if path.is_dir()),
            key=lambda path: len(path.parts),
        )
        if image_dirs:
            images_candidate = image_dirs[0]

    if not images_candidate.is_dir():
        raise FileNotFoundError("Could not find an images directory in selected input.")

    return str(images_candidate), str(annotations_path)


def resolve_dataset_paths(input_path: str | None) -> tuple[str, str, tempfile.TemporaryDirectory | None]:
    """Resolve dataset paths from zip or directory."""
    from .utils import pick_input_path_from_dialog
    
    extraction_directory = None

    selected_path = input_path
    if not selected_path:
        selected_path = pick_input_path_from_dialog()

    if not selected_path:
        raise ValueError("No input selected. Please provide a zip path or select a file in the dialog.")

    selected_path = os.path.abspath(selected_path)

    if os.path.isfile(selected_path) and selected_path.lower().endswith(".zip"):
        extraction_directory = tempfile.TemporaryDirectory(prefix="coco_review_")
        try:
            with zipfile.ZipFile(selected_path, "r") as archive:
                destination_root = Path(extraction_directory.name).resolve()
                for member in archive.infolist():
                    relative_name = member.filename.replace("\\", "/").lstrip("/")
                    if not relative_name or relative_name.endswith("/"):
                        continue

                    target_path = (destination_root / Path(*Path(relative_name).parts)).resolve()
                    if destination_root not in target_path.parents and target_path != destination_root:
                        continue

                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member, "r") as source, open(target_path, "wb") as target_handle:
                        shutil.copyfileobj(source, target_handle)
        except (zipfile.BadZipFile, OSError, RuntimeError) as error:
            if extraction_directory is not None:
                extraction_directory.cleanup()
                extraction_directory = None
            raise ValueError(f"Could not extract zip archive: {error}") from error
        images_path, annotations_path = find_coco_paths(extraction_directory.name)
        return images_path, annotations_path, extraction_directory

    if os.path.isdir(selected_path):
        images_path, annotations_path = find_coco_paths(selected_path)
        return images_path, annotations_path, extraction_directory

    raise ValueError("Input must be a .zip file or a directory containing COCO export files.")


def stable_image_id(file_name: str) -> int:
    """Deterministic COCO image id from the last 9 chars of the filename stem.

    Mirrors the id used by the prediction pipeline so a results/predictions file
    (which only carries integer image_ids) can be matched back to image files.
    """
    photo_id = Path(file_name).stem[-9:]
    digest = hashlib.md5(photo_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def _parse_coco_dict(coco_data: dict):
    """Parse a standard COCO dict (images/annotations/categories)."""
    images = coco_data.get("images", [])
    images_by_id = {image["id"]: image for image in images}
    annotations_by_image_id = defaultdict(list)
    for annotation in coco_data.get("annotations", []):
        annotations_by_image_id[annotation["image_id"]].append(annotation)

    categories_by_id = {
        category["id"]: category["name"]
        for category in coco_data.get("categories", [])
    }

    return images, images_by_id, annotations_by_image_id, categories_by_id


def _parse_coco_results(annotations_list: list, images_path: str):
    """Rebuild dataset metadata from a COCO results/predictions list.

    Such files are a flat list of annotations with integer image_ids but no
    images or categories. Image filenames are recovered from the folder via
    stable_image_id; category names fall back to the numeric id.
    """
    image_files = sorted(
        entry.name
        for entry in os.scandir(images_path)
        if entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS
    )
    images = [{"id": stable_image_id(name), "file_name": name} for name in image_files]
    images_by_id = {image["id"]: image for image in images}

    annotations_by_image_id = defaultdict(list)
    category_ids: set = set()
    for annotation in annotations_list:
        if not isinstance(annotation, dict):
            continue
        image_id = annotation.get("image_id")
        if image_id is None:
            continue
        annotations_by_image_id[image_id].append(annotation)
        category_id = annotation.get("category_id")
        if category_id is not None:
            category_ids.add(category_id)

    categories_by_id = {category_id: str(category_id) for category_id in category_ids}
    return images, images_by_id, annotations_by_image_id, categories_by_id


def _rotated_rectangle_corners(x: float, y: float, width: float, height: float, rotation_deg: float):
    """Corners of a rectangle rotated clockwise around its top-left corner (x, y).

    Returns top-left, top-right, bottom-right, bottom-left in image pixels.
    """
    angle = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    local = [(0.0, 0.0), (width, 0.0), (width, height), (0.0, height)]
    return [(x + dx * cos_a - dy * sin_a, y + dx * sin_a + dy * cos_a) for dx, dy in local]


def _polygon_area(points) -> float:
    """Shoelace area of a polygon given its (x, y) vertices."""
    area = 0.0
    count = len(points)
    for index in range(count):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % count]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _parse_label_studio(tasks: list):
    """Build dataset metadata from a Label Studio native export.

    Handles both region kinds an export can carry:
    * ``rectanglelabels`` — a (possibly rotated) box; ``segmentation`` is the
      rotated 4-corner quad and ``rotation`` is degrees clockwise around the
      box's top-left corner (so oriented boxes ride the existing pipeline).
    * ``polygonlabels`` — a free polygon; ``segmentation`` is its outline.

    ``bbox`` is always the axis-aligned envelope. Coordinates in the export are
    percentages of the image, so they are scaled by the region's pixel size.
    """
    images: list[dict] = []
    images_by_id: dict[int, dict] = {}
    annotations_by_image_id: dict[int, list] = defaultdict(list)
    name_to_category_id: dict[str, int] = {}
    annotation_id = 0

    for image_id, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        image_reference = (task.get("data", {}) or {}).get("image") or task.get("file_upload") or ""
        file_name = os.path.basename(str(image_reference).replace("\\", "/"))

        regions = [
            region
            for annotation in (task.get("annotations", []) or [])
            for region in (annotation.get("result", []) or [])
            if isinstance(region, dict) and region.get("type") in ("rectanglelabels", "polygonlabels")
        ]

        width = regions[0].get("original_width") if regions else None
        height = regions[0].get("original_height") if regions else None
        images.append({"id": image_id, "file_name": file_name, "width": width, "height": height})
        images_by_id[image_id] = images[-1]

        if not width or not height:
            continue

        for region in regions:
            region_type = region.get("type")
            value = region.get("value", {}) or {}

            if region_type == "rectanglelabels":
                labels = value.get("rectanglelabels") or []
                if not labels:
                    continue
                name = labels[0]
                box_x = float(value.get("x", 0.0)) / 100.0 * width
                box_y = float(value.get("y", 0.0)) / 100.0 * height
                box_w = float(value.get("width", 0.0)) / 100.0 * width
                box_h = float(value.get("height", 0.0)) / 100.0 * height
                rotation = float(value.get("rotation", 0.0) or 0.0)
                corners = _rotated_rectangle_corners(box_x, box_y, box_w, box_h, rotation)
                xs = [corner[0] for corner in corners]
                ys = [corner[1] for corner in corners]
                segmentation = [[coord for corner in corners for coord in corner]]
                bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
                area = box_w * box_h
            else:  # polygonlabels (L1/L3 free polygons)
                labels = value.get("polygonlabels") or []
                raw_points = value.get("points") or []
                if not labels:
                    continue
                points = [
                    (float(point[0]) / 100.0 * width, float(point[1]) / 100.0 * height)
                    for point in raw_points
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                if len(points) < 3:
                    continue  # not a polygon
                name = labels[0]
                xs = [point[0] for point in points]
                ys = [point[1] for point in points]
                segmentation = [[coord for point in points for coord in point]]
                bbox = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
                area = _polygon_area(points)
                rotation = 0.0

            if name not in name_to_category_id:
                name_to_category_id[name] = len(name_to_category_id)
            annotations_by_image_id[image_id].append(
                {
                    "id": annotation_id,
                    "image_id": image_id,
                    "category_id": name_to_category_id[name],
                    "segmentation": segmentation,
                    "bbox": bbox,
                    "area": area,
                    "rotation": rotation,
                    "iscrowd": 0,
                    "ignore": 0,
                }
            )
            annotation_id += 1

    categories_by_id = {category_id: name for name, category_id in name_to_category_id.items()}
    return images, images_by_id, annotations_by_image_id, categories_by_id


def load_coco_data(annotations_path: str, images_path: str | None = None):
    """Load annotations from a COCO dict, COCO results list, or Label Studio export."""
    with open(annotations_path, "r", encoding="utf-8") as file_handle:
        coco_data = json.load(file_handle)

    if isinstance(coco_data, list):
        if is_label_studio_export(coco_data):
            return _parse_label_studio(coco_data)
        if not images_path:
            raise ValueError(
                "This JSON is a COCO results/predictions list with no image names. "
                "Use 'Open images + JSON' so the image folder can be matched."
            )
        return _parse_coco_results(coco_data, images_path)

    return _parse_coco_dict(coco_data)


def has_polygons_in_dataset(annotations_by_image_id: dict) -> bool:
    """Scan entire dataset to detect if any polygons (segmentations) exist."""
    from .rle import is_rle_segmentation

    for annotations in annotations_by_image_id.values():
        for annotation in annotations:
            segmentation = annotation.get("segmentation", [])
            if is_rle_segmentation(segmentation):
                return True
            if isinstance(segmentation, dict):
                continue
            if segmentation and len(segmentation) > 0:
                for segment in segmentation:
                    if isinstance(segment, list) and len(segment) >= 6:
                        return True
    return False


def _assemble_dataset(images_path: str, annotations_path: str, temp_extraction):
    """Build the dataset dict from resolved image and annotation paths."""
    images, _images_by_id, annotations_by_image_id, categories_by_id = load_coco_data(annotations_path, images_path)

    images = sorted(images, key=lambda image_data: image_data.get("id", 0))
    class_items = sorted(categories_by_id.items(), key=lambda item: str(item[1]).lower())
    category_ids = [category_id for category_id, _name in class_items]
    visible_by_category = {category_id: True for category_id in category_ids}

    # Auto-detect annotation type: polygons vs bboxes
    has_polygons = has_polygons_in_dataset(annotations_by_image_id)

    return {
        "images_path": images_path,
        "temp_extraction": temp_extraction,
        "images": images,
        "annotations_by_image_id": annotations_by_image_id,
        "categories_by_id": categories_by_id,
        "class_items": class_items,
        "visible_by_category": visible_by_category,
        "has_polygons": has_polygons,
    }


def load_dataset_from_input(input_path: str | None):
    """Load complete dataset from a zip or folder with auto-detection of annotation type."""
    images_path, annotations_path, temp_extraction = resolve_dataset_paths(input_path)
    return _assemble_dataset(images_path, annotations_path, temp_extraction)


def load_dataset_from_folder_and_json(images_path: str, annotations_path: str):
    """Load complete dataset from an explicit images folder and a COCO JSON file."""
    if not images_path or not os.path.isdir(images_path):
        raise ValueError("Selected images folder is not a valid directory.")
    if not annotations_path or not os.path.isfile(annotations_path):
        raise ValueError("Selected annotations file is not a valid file.")
    return _assemble_dataset(os.path.abspath(images_path), os.path.abspath(annotations_path), None)


def _dir_has_images(directory: Path) -> bool:
    try:
        return any(
            entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS
            for entry in os.scandir(directory)
        )
    except OSError:
        return False


def list_images_in_dir(images_dir: str) -> list[str]:
    """Sorted image file basenames directly inside ``images_dir``."""
    return sorted(
        entry.name
        for entry in os.scandir(images_dir)
        if entry.is_file() and Path(entry.name).suffix.lower() in IMAGE_EXTENSIONS
    )


def discover_images_dir(folder: str) -> str:
    """Find the directory that actually holds the images for an annotation session.

    Prefers ``<folder>/images``, then ``folder`` itself, then the shallowest
    nested directory that contains image files.
    """
    folder_path = Path(folder)
    images_subdir = folder_path / "images"
    if images_subdir.is_dir() and _dir_has_images(images_subdir):
        return str(images_subdir)
    if _dir_has_images(folder_path):
        return str(folder_path)
    for candidate in sorted((p for p in folder_path.rglob("*") if p.is_dir()), key=lambda p: len(p.parts)):
        if _dir_has_images(candidate):
            return str(candidate)
    return str(folder_path)


def load_images_only(folder: str) -> dict:
    """Build an images-only session (no annotations) from a plain folder.

    ``root`` is the folder the user picked (where per-level COCO JSONs live);
    ``images_path`` is the resolved directory the image files sit in.
    """
    if not folder or not os.path.isdir(folder):
        raise ValueError("Selected folder is not a valid directory.")
    images_dir = discover_images_dir(folder)
    names = list_images_in_dir(images_dir)
    if not names:
        raise FileNotFoundError("No image files found in the selected folder.")
    images = [{"id": stable_image_id(name), "file_name": name} for name in names]
    return {
        "root": os.path.abspath(folder),
        "images_path": os.path.abspath(images_dir),
        "images": images,
    }


def resolve_image_path(images_path: str, coco_file_name: str) -> str:
    """Resolve full path to an image file."""
    normalized_file_name = coco_file_name.replace("\\", "/")
    candidate = os.path.join(images_path, normalized_file_name.replace("/", os.sep))
    if os.path.isfile(candidate):
        return candidate

    fallback = os.path.join(images_path, os.path.basename(normalized_file_name))
    return fallback
