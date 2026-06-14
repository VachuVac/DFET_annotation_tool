"""Data loading functions for COCO annotations."""

import hashlib
import json
import os
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path
import shutil

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def find_coco_paths(search_root: str) -> tuple[str, str]:
    """Find COCO annotation and image directories."""
    root_path = Path(search_root)

    result_candidates = sorted(root_path.rglob("result.json"), key=lambda path: len(path.parts))
    if not result_candidates:
        raise FileNotFoundError("Could not find result.json in selected input.")

    annotations_path = result_candidates[0]

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


def load_coco_data(annotations_path: str, images_path: str | None = None):
    """Load annotation data from a COCO dict file or a COCO results list file."""
    with open(annotations_path, "r", encoding="utf-8") as file_handle:
        coco_data = json.load(file_handle)

    if isinstance(coco_data, list):
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


def resolve_image_path(images_path: str, coco_file_name: str) -> str:
    """Resolve full path to an image file."""
    normalized_file_name = coco_file_name.replace("\\", "/")
    candidate = os.path.join(images_path, normalized_file_name.replace("/", os.sep))
    if os.path.isfile(candidate):
        return candidate

    fallback = os.path.join(images_path, os.path.basename(normalized_file_name))
    return fallback
