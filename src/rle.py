"""COCO RLE (run-length encoding) segmentation support.

COCO masks can be stored either as polygon point-lists or as RLE dicts of the
form ``{"size": [h, w], "counts": "..."}``. The viewer already renders polygon
point-lists; this module converts RLE masks into polygon contours so they flow
through the exact same overlay pipeline.

Only depends on numpy and OpenCV (both already used in this project's
environment); pycocotools is intentionally not required.
"""

from __future__ import annotations


def is_rle_segmentation(segmentation) -> bool:
    """True if the segmentation is a COCO RLE dict (compressed or uncompressed)."""
    return (
        isinstance(segmentation, dict)
        and "counts" in segmentation
        and "size" in segmentation
    )


def _decode_counts_string(counts: bytes) -> list[int]:
    """Decode COCO's compressed RLE ``counts`` string into a list of run lengths.

    Mirrors pycocotools' ``rleFrString``: a LEB128-style variable-length integer
    encoding offset by 48, where each value after the second is stored as a delta
    from the value two positions earlier.
    """
    run_lengths: list[int] = []
    position = 0
    count = len(counts)
    while position < count:
        value = 0
        shift = 0
        more = True
        while more:
            byte = counts[position] - 48
            value |= (byte & 0x1F) << (5 * shift)
            more = bool(byte & 0x20)
            position += 1
            shift += 1
            if not more and (byte & 0x10):
                value |= (-1 << (5 * shift))
        if len(run_lengths) > 2:
            value += run_lengths[-2]
        run_lengths.append(value)
    return run_lengths


def decode_rle_to_mask(segmentation):
    """Decode a COCO RLE segmentation into an ``(H, W)`` uint8 numpy mask."""
    import numpy as np

    height, width = int(segmentation["size"][0]), int(segmentation["size"][1])
    counts = segmentation["counts"]
    if isinstance(counts, str):
        counts = counts.encode("ascii")

    if isinstance(counts, (bytes, bytearray)):
        run_lengths = _decode_counts_string(bytes(counts))
    else:
        # Uncompressed RLE: counts is already a list of run lengths.
        run_lengths = [int(value) for value in counts]

    mask = np.zeros(height * width, dtype=np.uint8)
    index = 0
    value = 0
    for run in run_lengths:
        if value:
            mask[index:index + run] = 1
        index += run
        value ^= 1

    # COCO RLE runs are column-major (Fortran) order.
    return mask.reshape((height, width), order="F")


def rle_to_polygons(segmentation, min_points: int = 3) -> list[list[float]]:
    """Convert a COCO RLE segmentation to a list of flat ``[x1, y1, ...]`` polygons.

    The output matches COCO's polygon point-list format so it can be consumed by
    the same overlay-building code path.
    """
    import cv2

    mask = decode_rle_to_mask(segmentation)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    polygons: list[list[float]] = []
    for contour in contours:
        if len(contour) < min_points:
            continue
        # Light simplification to keep the rendered point count manageable.
        epsilon = 0.001 * cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, epsilon, True)
        if len(approx) < min_points:
            approx = contour
        flat: list[float] = []
        for point in approx.reshape(-1, 2):
            flat.append(float(point[0]))
            flat.append(float(point[1]))
        polygons.append(flat)
    return polygons
