"""Drawing functions for annotations."""

import colorsys
import cv2
import numpy as np


def polygon_centroid(points: np.ndarray) -> tuple[int, int]:
    """Calculate centroid of a polygon."""
    center_x = int(round(float(points[:, 0].mean())))
    center_y = int(round(float(points[:, 1].mean())))
    return center_x, center_y


def color_for_category(category_id: int) -> tuple[int, int, int]:
    """Generate BGR color for category based on ID."""
    hue = (category_id * 0.6180339887498949) % 1.0
    saturation = 0.65
    value = 0.95
    red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
    return (
        int(blue * 255),
        int(green * 255),
        int(red * 255),
    )


def draw_crisp_text(
    canvas: np.ndarray,
    text: str,
    org: tuple[int, int],
    color: tuple[int, int, int],
    scale: float,
    thickness: int = 2,
    font: int = cv2.FONT_HERSHEY_DUPLEX,
    outline_color: tuple[int, int, int] | None = None,
) -> None:
    """Draw crisp text with optional outline."""
    from .constants import DPI_SCALE, TEXT_SCALE

    s = float(scale) * DPI_SCALE * TEXT_SCALE
    t = max(1, int(round(thickness * DPI_SCALE * 1.1)))
    if outline_color is None:
        # Auto-contrast outline for readability on mixed backgrounds.
        brightness = int(color[0]) + int(color[1]) + int(color[2])
        outline_color = (0, 0, 0) if brightness > 382 else (255, 255, 255)

    cv2.putText(canvas, text, org, font, s, outline_color, t + 3, cv2.LINE_AA)
    cv2.putText(canvas, text, org, font, s, color, t, cv2.LINE_AA)


def draw_centered_text(
    canvas: np.ndarray,
    text: str,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    scale: float,
    thickness: int = 2,
    font: int = cv2.FONT_HERSHEY_DUPLEX,
) -> None:
    """Draw text centered in a box."""
    from .constants import DPI_SCALE, TEXT_SCALE
    
    left, top, right, bottom = box
    s = float(scale) * DPI_SCALE * TEXT_SCALE
    t = max(1, int(round(thickness * DPI_SCALE * 1.1)))
    (text_width, text_height), baseline = cv2.getTextSize(text, font, s, t)
    x = max(left + 2, int(left + (right - left - text_width) / 2.0))
    center_y = (top + bottom) // 2
    y = center_y + (text_height - baseline) // 2
    draw_crisp_text(canvas, text, (x, y), color, scale, thickness, font)


def draw_coco_polygons(
    image: np.ndarray,
    annotations,
    categories_by_id,
    visible_by_category,
    show_points: bool,
    opacity: float = 0.30,
) -> tuple[np.ndarray, list]:
    """Draw polygon annotations on image.
    
    Args:
        opacity: Fill opacity (0.0-1.0), default 0.30
    """
    rendered_image = image.copy()
    overlay = rendered_image.copy()
    visible_annotations = []
    label_items: list = []

    for annotation in annotations:
        category_id = annotation.get("category_id")
        if not visible_by_category.get(category_id, True):
            continue
        visible_annotations.append(annotation)

    for annotation in visible_annotations:
        category_id = annotation.get("category_id")
        polygon_color = color_for_category(int(category_id) if category_id is not None else 0)
        segmentation = annotation.get("segmentation", [])

        if isinstance(segmentation, dict):
            segmentation = []

        for segment in segmentation:
            if len(segment) < 6:
                continue

            polygon_points = np.array(segment, dtype=np.float32).reshape(-1, 2)
            polygon_points_int = np.round(polygon_points).astype(np.int32).reshape(-1, 1, 2)

            cv2.fillPoly(overlay, [polygon_points_int], polygon_color)
            cv2.polylines(rendered_image, [polygon_points_int], isClosed=True, color=polygon_color, thickness=1, lineType=cv2.LINE_AA)

            centroid_x, centroid_y = polygon_centroid(polygon_points)
            category_name = categories_by_id.get(annotation.get("category_id"), str(annotation.get("category_id", "unknown")))

            label_items.append({
                "x": int(round(centroid_x)),
                "y": int(round(centroid_y)),
                "label": str(category_name),
                "color": polygon_color,
                "points": [(int(round(p[0])), int(round(p[1]))) for p in polygon_points],
            })

    rendered_image = cv2.addWeighted(overlay, opacity, rendered_image, 1.0 - opacity, 0)

    return rendered_image, label_items


def draw_coco_bboxes(
    image: np.ndarray,
    annotations,
    categories_by_id,
    visible_by_category,
    opacity: float = 0.30,
) -> tuple[np.ndarray, list]:
    """Draw bounding box annotations on image.
    
    Args:
        opacity: Fill opacity (0.0-1.0), default 0.30
    """
    rendered_image = image.copy()
    overlay = rendered_image.copy()
    label_items: list = []
    visible_annotations = []

    for annotation in annotations:
        category_id = annotation.get("category_id")
        if not visible_by_category.get(category_id, True):
            continue
        visible_annotations.append(annotation)

    for annotation in visible_annotations:
        category_id = annotation.get("category_id")
        bbox_color = color_for_category(int(category_id) if category_id is not None else 0)
        bbox = annotation.get("bbox", [])

        if not bbox or len(bbox) < 4:
            continue

        x, y, width, height = bbox
        x1, y1 = int(round(x)), int(round(y))
        x2, y2 = int(round(x + width)), int(round(y + height))

        # Draw filled rectangle on overlay
        cv2.rectangle(overlay, (x1, y1), (x2, y2), bbox_color, thickness=-1)
        # Draw outline on rendered image
        cv2.rectangle(rendered_image, (x1, y1), (x2, y2), bbox_color, thickness=1, lineType=cv2.LINE_AA)

        # Calculate center for label
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2
        category_name = categories_by_id.get(annotation.get("category_id"), str(annotation.get("category_id", "unknown")))

        label_items.append({
            "x": center_x,
            "y": center_y,
            "label": str(category_name),
            "color": bbox_color,
            "points": [(x1, y1), (x2, y1), (x2, y2), (x1, y2)],
        })

    rendered_image = cv2.addWeighted(overlay, opacity, rendered_image, 1.0 - opacity, 0)

    return rendered_image, label_items
