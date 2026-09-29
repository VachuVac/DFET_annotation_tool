"""Project model for the DFET Annotation tool.

A *project* is a self-contained, portable folder that is the shared working
area for both validation and annotation modes. It copies images into itself and
writes its own per-level COCO JSONs -- it never touches the originals. Zip the
folder and everything both modes need travels with it.

Layout::

    <ProjectName>/
      project.json          # manifest: version, name, timestamps, image
                            #   registry [{id, file_name, width, height, source}],
                            #   settings (per-project class-color overrides)
      images/               # canonical copied images, dedup'd by stable_image_id
      annotations/
        level1.json         # COCO polygons       (images array = registry)
        level2.json         # COCO rotated bboxes (4-corner seg + rotation)
        level3.json         # COCO polygons
        _pending.json       # stash of orphan annotations whose image is not
                            #   (yet) in the project, tagged with level + id

The level COCO files are byte-compatible with the ones the annotation mode
already writes (``{images, categories, annotations, level}``; store keyed by
image basename), so the two code paths are interchangeable.

This module is deliberately Qt-light: image dimensions are read through an
injectable callback (defaulting to ``QImageReader``) so it can be unit-tested
headlessly without a running GUI.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import levels
from .data_loading import IMAGE_EXTENSIONS, list_images_in_dir, stable_image_id

FORMAT_VERSION = 1
MANIFEST_NAME = "project.json"
IMAGES_DIRNAME = "images"
ANNOTATIONS_DIRNAME = "annotations"
PENDING_NAME = "_pending.json"
UNMAPPED_NAME = "_unmapped.json"

# (width, height) reader: returns (None, None) when the size can't be determined.
ImageSizeFn = Callable[[str], "tuple[int | None, int | None]"]


class ProjectError(Exception):
    """Raised when a project cannot be created, opened, or is malformed."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _qimage_size(path: str) -> tuple[int | None, int | None]:
    """Default image-size reader using Qt; (None, None) if unavailable/unreadable."""
    try:
        from PyQt6.QtGui import QImageReader

        reader = QImageReader(str(path))
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            return int(size.width()), int(size.height())
    except Exception:
        pass
    return None, None


class Project:
    """An open project rooted at a folder on disk.

    Construct via :meth:`create` or :meth:`open`; do not instantiate directly
    unless you already have a validated manifest.
    """

    def __init__(self, root: Path, manifest: dict, image_size: ImageSizeFn = _qimage_size) -> None:
        self.root = Path(root)
        self.manifest = manifest
        self._image_size = image_size
        self._images_by_id: dict[int, dict] = {
            int(record["id"]): record for record in manifest.get("images", [])
        }

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #
    @classmethod
    def create(cls, parent_dir: str, name: str, image_size: ImageSizeFn = _qimage_size) -> "Project":
        """Create a brand-new project folder ``<parent_dir>/<name>`` and return it."""
        cleaned = (name or "").strip()
        if not cleaned:
            raise ProjectError("Project name must not be empty.")
        if any(sep in cleaned for sep in ("/", "\\")) or cleaned in (".", ".."):
            raise ProjectError(f"Invalid project name: {name!r}")

        parent = Path(parent_dir)
        if not parent.is_dir():
            raise ProjectError(f"Parent directory does not exist: {parent}")

        root = parent / cleaned
        if root.exists():
            raise ProjectError(f"A folder named {cleaned!r} already exists here.")

        (root / IMAGES_DIRNAME).mkdir(parents=True)
        (root / ANNOTATIONS_DIRNAME).mkdir(parents=True)

        now = _utc_now_iso()
        manifest = {
            "format_version": FORMAT_VERSION,
            "name": cleaned,
            "created": now,
            "modified": now,
            "images": [],
            "settings": {"class_colors": {}, "class_remaps": {}},
        }
        project = cls(root, manifest, image_size)
        project.save_manifest()
        # An empty stash keeps the folder shape predictable for colleagues.
        project._write_json(project.pending_path, [])
        return project

    @classmethod
    def open(cls, path: str, image_size: ImageSizeFn = _qimage_size) -> "Project":
        """Open an existing project folder (the folder itself or its project.json)."""
        candidate = Path(path)
        if candidate.is_file() and candidate.name == MANIFEST_NAME:
            root = candidate.parent
        else:
            root = candidate

        manifest_path = root / MANIFEST_NAME
        if not manifest_path.is_file():
            raise ProjectError(f"Not a project (no {MANIFEST_NAME}): {root}")

        try:
            with manifest_path.open(encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ProjectError(f"Could not read {MANIFEST_NAME}: {error}") from error

        manifest = _normalize_manifest(manifest, root.name)
        project = cls(root, manifest, image_size)
        # Make sure the expected subfolders exist even if hand-edited.
        project.images_dir.mkdir(parents=True, exist_ok=True)
        project.annotations_dir.mkdir(parents=True, exist_ok=True)
        project._ensure_unmapped_ids()  # backfill ids onto older stashes
        return project

    # ------------------------------------------------------------------ #
    # paths
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self.manifest.get("name", self.root.name)

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    @property
    def images_dir(self) -> Path:
        return self.root / IMAGES_DIRNAME

    @property
    def annotations_dir(self) -> Path:
        return self.root / ANNOTATIONS_DIRNAME

    @property
    def pending_path(self) -> Path:
        return self.annotations_dir / PENDING_NAME

    @property
    def unmapped_path(self) -> Path:
        return self.annotations_dir / UNMAPPED_NAME

    def level_json_path(self, level: int) -> Path:
        return self.annotations_dir / f"level{level}.json"

    # ------------------------------------------------------------------ #
    # class remaps (off-catalog class -> level + existing target class)
    # ------------------------------------------------------------------ #
    @property
    def class_remaps(self) -> dict[str, dict]:
        settings = self.manifest.setdefault("settings", {})
        remaps = settings.setdefault("class_remaps", {})
        return remaps

    def remap_for(self, raw_class: str) -> dict | None:
        """Return ``{level, target}`` for a previously redefined class, or None."""
        entry = self.class_remaps.get(str(raw_class))
        if isinstance(entry, dict) and "level" in entry and "target" in entry:
            return {"level": int(entry["level"]), "target": str(entry["target"])}
        return None

    def set_class_remap(self, raw_class: str, level: int, target: str) -> None:
        """Record (and persist) that ``raw_class`` maps to ``target`` in ``level``."""
        self.class_remaps[str(raw_class)] = {"level": int(level), "target": str(target)}
        self.save_manifest()

    # ------------------------------------------------------------------ #
    # manifest / registry
    # ------------------------------------------------------------------ #
    def save_manifest(self) -> None:
        self.manifest["modified"] = _utc_now_iso()
        self.manifest["images"] = list(self._images_by_id.values())
        self._write_json(self.manifest_path, self.manifest)

    def has_image(self, image_id: int) -> bool:
        return int(image_id) in self._images_by_id

    def image_record(self, image_id: int) -> dict | None:
        return self._images_by_id.get(int(image_id))

    def registry_images(self) -> list[dict]:
        """The master image array, sorted by id, ready to embed in a level COCO."""
        return [
            {
                "id": record["id"],
                "file_name": record["file_name"],
                "width": record.get("width"),
                "height": record.get("height"),
            }
            for record in sorted(self._images_by_id.values(), key=lambda r: int(r["id"]))
        ]

    def basename_to_id(self) -> dict[str, int]:
        return {
            os.path.basename(str(record["file_name"])): int(record["id"])
            for record in self._images_by_id.values()
        }

    def add_image_record(
        self,
        image_id: int,
        file_name: str,
        width: int | None,
        height: int | None,
        source: str,
    ) -> dict:
        """Insert (or overwrite) a registry record. Does not persist on its own."""
        record = {
            "id": int(image_id),
            "file_name": os.path.basename(str(file_name)),
            "width": width,
            "height": height,
            "source": source,
        }
        self._images_by_id[int(image_id)] = record
        return record

    # ------------------------------------------------------------------ #
    # image import (copy + dedup by stable_image_id)
    # ------------------------------------------------------------------ #
    def import_images(self, src_dir: str, file_names: Iterable[str] | None = None, source: str | None = None) -> dict:
        """Copy images from ``src_dir`` into the project, skipping ids already present.

        Dedup is by :func:`stable_image_id` of the basename. Because that id is
        derived from the filename, "same destination filename" implies "same id",
        so id-dedup also guarantees we never overwrite an existing different image.

        Returns ``{"copied": [names], "skipped": [names]}``. The caller is
        expected to follow up with :meth:`promote_pending` so any newly added
        image can rescue stashed orphan annotations.
        """
        src = Path(src_dir)
        if not src.is_dir():
            raise ProjectError(f"Not an image folder: {src_dir}")

        if file_names is None:
            names = list_images_in_dir(str(src))
        else:
            names = [os.path.basename(str(name)) for name in file_names]

        tag = source or f"folder:{src.name}"
        copied: list[str] = []
        skipped: list[str] = []
        for name in names:
            if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            source_path = src / name
            if not source_path.is_file():
                continue
            image_id = stable_image_id(name)
            if self.has_image(image_id):
                skipped.append(name)
                continue
            destination = self.images_dir / name
            shutil.copy2(source_path, destination)
            width, height = self._image_size(str(destination))
            self.add_image_record(image_id, name, width, height, tag)
            copied.append(name)

        if copied:
            self.save_manifest()
        return {"copied": copied, "skipped": skipped}

    # ------------------------------------------------------------------ #
    # level COCO read / write
    # ------------------------------------------------------------------ #
    def read_level(self, level: int) -> dict[str, list]:
        """Read ``levelN.json`` into a ``{image_basename: [annotation, ...]}`` store.

        Mirrors the annotation mode's resume loader: annotations are bucketed by
        the basename of their image (so they survive id changes). Returns an
        empty store if the file is absent or unreadable.
        """
        path = self.level_json_path(level)
        if not path.is_file():
            return {}
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

        id_to_name = {
            image.get("id"): os.path.basename(str(image.get("file_name", "")))
            for image in data.get("images", [])
        }
        store: dict[str, list] = {}
        for annotation in data.get("annotations", []):
            if not isinstance(annotation, dict):
                continue
            name = id_to_name.get(annotation.get("image_id")) or os.path.basename(
                str(annotation.get("file_name", ""))
            )
            if not name:
                continue
            store.setdefault(name, []).append(annotation)
        return store

    def build_level_coco(self, level: int, store: dict[str, list]) -> dict:
        """Flatten a basename-keyed store into a COCO dict embedding the registry."""
        basename_to_id = self.basename_to_id()
        images_out = self.registry_images()
        annotations_out: list[dict] = []
        annotation_id = 1
        for image in images_out:
            basename = os.path.basename(str(image["file_name"]))
            image_id = image["id"]
            for annotation in store.get(basename, []):
                out = dict(annotation)
                out["id"] = annotation_id
                out["image_id"] = image_id
                annotations_out.append(out)
                annotation_id += 1

        # Annotations whose image is not in the registry should never reach here
        # (they belong in the stash), but route any stragglers by hashed basename
        # so nothing is silently dropped.
        for basename, annotations in store.items():
            if basename in basename_to_id:
                continue
            image_id = stable_image_id(basename)
            for annotation in annotations:
                out = dict(annotation)
                out["id"] = annotation_id
                out["image_id"] = image_id
                annotations_out.append(out)
                annotation_id += 1

        return {
            "images": images_out,
            "categories": levels.level_categories(level),
            "annotations": annotations_out,
            "level": level,
        }

    def write_level(self, level: int, store: dict[str, list]) -> int:
        """Write ``levelN.json`` from a basename-keyed store; returns annotation count."""
        coco = self.build_level_coco(level, store)
        self._write_json(self.level_json_path(level), coco)
        return len(coco["annotations"])

    def level_counts(self) -> dict[int, int]:
        """Annotation count per level (0 for absent files)."""
        counts: dict[int, int] = {}
        for level in levels.LEVEL_IDS:
            store = self.read_level(level)
            counts[level] = sum(len(items) for items in store.values())
        return counts

    # ------------------------------------------------------------------ #
    # export
    # ------------------------------------------------------------------ #
    def export_zip(self, dest_path: str) -> Path:
        """Bundle the whole project into a single shareable ``.zip``.

        The archive holds the manifest, ``images/`` and the level JSONs at the zip
        ROOT (no extra ``<ProjectName>/`` wrapper inside). Since the archive itself
        is named ``<ProjectName>.zip``, extracting it with Windows Explorer — which
        drops the contents into a folder named after the zip — yields a single
        ``<ProjectName>/`` folder that :meth:`open` accepts directly, instead of a
        doubly-nested ``<ProjectName>/<ProjectName>/``. Returns the written path
        (``.zip`` enforced).
        """
        import zipfile

        dest = Path(dest_path)
        if dest.suffix.lower() != ".zip":
            dest = dest.with_suffix(".zip")
        self.save_manifest()  # make sure project.json reflects the live registry
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(self.root.rglob("*")):
                    # Skip the archive itself if the user wrote it inside the project.
                    if path.is_file() and path.resolve() != dest.resolve():
                        # Entries live at the zip root; as_posix(): zip uses '/'.
                        arcname = path.relative_to(self.root).as_posix()
                        archive.write(path, arcname)
        except OSError as error:
            raise ProjectError(f"Could not export zip: {error}") from error
        return dest

    # ------------------------------------------------------------------ #
    # stash (orphan annotations) + promotion
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # unmapped stash (off-catalog annotations awaiting Redefine)
    # ------------------------------------------------------------------ #
    def read_unmapped(self) -> list[dict]:
        path = self.unmapped_path
        if not path.is_file():
            return []
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def write_unmapped(self, entries: list[dict]) -> None:
        self._write_json(self.unmapped_path, entries)

    def stash_unmapped(self, entries: Iterable[dict]) -> int:
        """Append off-catalog annotations awaiting redefinition; returns new total.

        Each entry carries the RAW ``source_annotation`` (geometry not yet
        converted, since the target level isn't known until the user redefines
        the class), plus ``raw_class``, ``stable_image_id`` and ``file_name``.
        """
        unmapped = self.read_unmapped()
        for entry in entries:
            unmapped.append(
                {
                    "id": uuid.uuid4().hex,  # stable per-object handle (for per-object resolve)
                    "raw_class": str(entry["raw_class"]),
                    "stable_image_id": int(entry["stable_image_id"]),
                    "file_name": os.path.basename(str(entry.get("file_name", ""))),
                    "source_annotation": entry["source_annotation"],
                }
            )
        self.write_unmapped(unmapped)
        return len(unmapped)

    def _ensure_unmapped_ids(self) -> None:
        """Backfill stable ``id``s onto any pre-existing stash entries lacking one."""
        unmapped = self.read_unmapped()
        changed = False
        for entry in unmapped:
            if not entry.get("id"):
                entry["id"] = uuid.uuid4().hex
                changed = True
        if changed:
            self.write_unmapped(unmapped)

    def get_unmapped_entry(self, entry_id: str) -> dict | None:
        """Return the stashed entry with this id, or None."""
        for entry in self.read_unmapped():
            if entry.get("id") == entry_id:
                return entry
        return None

    def pop_unmapped_entry(self, entry_id: str) -> dict | None:
        """Remove + return the stashed entry with this id (persisted), or None."""
        unmapped = self.read_unmapped()
        kept: list[dict] = []
        popped: dict | None = None
        for entry in unmapped:
            if popped is None and entry.get("id") == entry_id:
                popped = entry
            else:
                kept.append(entry)
        if popped is not None:
            self.write_unmapped(kept)
        return popped

    def restore_unmapped_entry(self, entry: dict) -> None:
        """Re-insert a previously popped stash entry verbatim (preserving its id).

        Used to UNDO a per-object redefine resolve: the object reappears in the
        redefine list exactly as it was (same id -> re-resolvable, red row returns).
        No-op if an entry with that id is already present.
        """
        unmapped = self.read_unmapped()
        if any(e.get("id") == entry.get("id") for e in unmapped):
            return
        unmapped.append(dict(entry))
        self.write_unmapped(unmapped)

    def unmapped_class_counts(self) -> dict[str, int]:
        """Distinct unknown class names still awaiting redefinition -> count."""
        counts: dict[str, int] = {}
        for entry in self.read_unmapped():
            name = str(entry.get("raw_class", ""))
            if name:
                counts[name] = counts.get(name, 0) + 1
        return counts

    def unmapped_assigned_levels(self) -> dict[str, int]:
        """Per unknown class, the level it has been parked in (level-only redefine)."""
        out: dict[str, int] = {}
        for entry in self.read_unmapped():
            name = str(entry.get("raw_class", ""))
            level = entry.get("assigned_level")
            if name and level is not None:
                out[name] = int(level)
        return out

    def set_unmapped_level(self, raw_class: str, level: int | None) -> None:
        """Park every stashed entry of ``raw_class`` in ``level`` (None clears it).

        A parked class isn't committed to the level file; it's rendered there,
        highlighted as needing redefinition, until a class is finally chosen.
        """
        unmapped = self.read_unmapped()
        changed = False
        for entry in unmapped:
            if str(entry.get("raw_class", "")) == str(raw_class):
                entry["assigned_level"] = None if level is None else int(level)
                changed = True
        if changed:
            self.write_unmapped(unmapped)

    def unmapped_entries_for_level(self, level: int) -> list[dict]:
        """Stashed entries parked in ``level`` (for highlighted rendering)."""
        return [
            entry
            for entry in self.read_unmapped()
            if entry.get("assigned_level") is not None and int(entry["assigned_level"]) == int(level)
        ]

    def unmapped_entries_visible_on_level(self, level: int) -> list[dict]:
        """Stashed entries to highlight on ``level``.

        An entry parked in a specific level (``assigned_level`` set) shows only
        there. An entry not yet parked anywhere (``assigned_level`` None/absent)
        shows on EVERY level, so a reviewer sees it regardless of which level they
        are on until it is assigned to the right one.
        """
        return [
            entry
            for entry in self.read_unmapped()
            if entry.get("assigned_level") is None or int(entry["assigned_level"]) == int(level)
        ]

    def read_pending(self) -> list[dict]:
        path = self.pending_path
        if not path.is_file():
            return []
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def write_pending(self, entries: list[dict]) -> None:
        self._write_json(self.pending_path, entries)

    def stash_annotations(self, entries: Iterable[dict]) -> int:
        """Append orphan-annotation entries to the stash.

        Each entry must carry ``level``, ``stable_image_id``, ``file_name`` and
        ``annotation``. Returns the new total stash size.
        """
        pending = self.read_pending()
        added = 0
        for entry in entries:
            pending.append(
                {
                    "level": int(entry["level"]),
                    "stable_image_id": int(entry["stable_image_id"]),
                    "file_name": os.path.basename(str(entry.get("file_name", ""))),
                    "annotation": entry["annotation"],
                }
            )
            added += 1
        self.write_pending(pending)
        return added

    def promote_pending(self) -> dict:
        """Move stashed orphans whose image is now present into their level files.

        Call after any import that adds images. Returns
        ``{"promoted": N, "by_level": {level: count}}``.
        """
        pending = self.read_pending()
        if not pending:
            return {"promoted": 0, "by_level": {}}

        promotable: dict[int, list[dict]] = {}
        still_pending: list[dict] = []
        for entry in pending:
            image_id = int(entry.get("stable_image_id", -1))
            level = int(entry.get("level", 0))
            if level in levels.LEVELS and self.has_image(image_id):
                promotable.setdefault(level, []).append(entry)
            else:
                still_pending.append(entry)

        if not promotable:
            return {"promoted": 0, "by_level": {}}

        by_level: dict[int, int] = {}
        for level, entries in promotable.items():
            store = self.read_level(level)
            for entry in entries:
                record = self.image_record(int(entry["stable_image_id"]))
                basename = os.path.basename(str(record["file_name"])) if record else os.path.basename(
                    str(entry.get("file_name", ""))
                )
                if not basename:
                    still_pending.append(entry)
                    continue
                store.setdefault(basename, []).append(entry["annotation"])
                by_level[level] = by_level.get(level, 0) + 1
            self.write_level(level, store)

        self.write_pending(still_pending)
        return {"promoted": sum(by_level.values()), "by_level": by_level}

    # ------------------------------------------------------------------ #
    # internal
    # ------------------------------------------------------------------ #
    @staticmethod
    def _write_json(path: Path, payload) -> None:
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except OSError as error:
            raise ProjectError(f"Could not write {path.name}: {error}") from error


def _normalize_manifest(manifest, fallback_name: str) -> dict:
    """Validate and fill in a manifest read from disk; raise on irrecoverable shape."""
    if not isinstance(manifest, dict):
        raise ProjectError("project.json is not a JSON object.")

    version = manifest.get("format_version")
    if version is not None and int(version) > FORMAT_VERSION:
        raise ProjectError(
            f"Project was made by a newer version (format {version} > {FORMAT_VERSION})."
        )

    raw_images = manifest.get("images")
    images: list[dict] = []
    seen_ids: set[int] = set()
    if isinstance(raw_images, list):
        for record in raw_images:
            if not isinstance(record, dict) or "id" not in record or "file_name" not in record:
                continue
            try:
                image_id = int(record["id"])
            except (TypeError, ValueError):
                continue
            if image_id in seen_ids:
                continue
            seen_ids.add(image_id)
            images.append(
                {
                    "id": image_id,
                    "file_name": os.path.basename(str(record["file_name"])),
                    "width": record.get("width"),
                    "height": record.get("height"),
                    "source": record.get("source", ""),
                }
            )

    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    if not isinstance(settings.get("class_colors"), dict):
        settings["class_colors"] = {}
    if not isinstance(settings.get("class_remaps"), dict):
        settings["class_remaps"] = {}

    now = _utc_now_iso()
    return {
        "format_version": FORMAT_VERSION,
        "name": str(manifest.get("name") or fallback_name),
        "created": str(manifest.get("created") or now),
        "modified": str(manifest.get("modified") or now),
        "images": images,
        "settings": settings,
    }


def create_project(parent_dir: str, name: str, image_size: ImageSizeFn = _qimage_size) -> Project:
    """Module-level alias for :meth:`Project.create`."""
    return Project.create(parent_dir, name, image_size)


def open_project(path: str, image_size: ImageSizeFn = _qimage_size) -> Project:
    """Module-level alias for :meth:`Project.open`."""
    return Project.open(path, image_size)
