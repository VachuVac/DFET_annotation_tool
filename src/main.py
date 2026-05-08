"""Main application entry point for Annotation Review."""

import os
import cv2

from .viewer import AnnotationViewer
from .data_loading import load_dataset_from_input, resolve_image_path
from .drawing import draw_coco_polygons, draw_coco_bboxes
from .utils import pick_input_path_from_dialog, show_info_popup, parse_arguments


def main() -> None:
    """Main application loop."""
    args = parse_arguments()

    if args.self_test:
        print("Self-test OK: arguments parsed, dependencies imported.")
        return

    viewer = AnnotationViewer("Annotation Review")

    dataset = None
    temp_extraction = None
    images_path = ""
    images = []
    annotations_by_image_id = {}
    categories_by_id = {}
    class_items = []
    category_ids = []
    visible_by_category = {}
    show_points = False
    show_labels = True
    has_polygons = False

    if args.input_path:
        try:
            loaded_dataset = load_dataset_from_input(args.input_path)
        except Exception as error:
            print(f"Dataset selection failed: {error}")
            return

        dataset = loaded_dataset
        temp_extraction = loaded_dataset["temp_extraction"]
        images_path = loaded_dataset["images_path"]
        images = loaded_dataset["images"]
        annotations_by_image_id = loaded_dataset["annotations_by_image_id"]
        categories_by_id = loaded_dataset["categories_by_id"]
        class_items = loaded_dataset["class_items"]
        category_ids = [category_id for category_id, _name in class_items]
        visible_by_category = loaded_dataset["visible_by_category"]
        has_polygons = loaded_dataset.get("has_polygons", False)
        annotation_type = "polygons" if has_polygons else "bboxes"
        print(f"Loaded {len(images)} images from: {images_path}")
        print(f"Annotation type detected: {annotation_type}")
        print("Use mouse wheel zoom, left drag pan, r reset, n next, p previous, q quit.")
        print("Use the right sidebar checkboxes for class visibility. Buttons: Show all / Hide all / Prev / Reset / Next.")
        if has_polygons:
            print("Toggle polygon points from the sidebar checkbox or press t.")
    else:
        viewer.set_idle_view()
        print("No dataset loaded yet. Use Open folder to load a ZIP export or folder.")

    def clear_dataset() -> None:
        nonlocal dataset, images_path, images, annotations_by_image_id, categories_by_id, class_items, category_ids, visible_by_category
        dataset = None
        images_path = ""
        images = []
        annotations_by_image_id = {}
        categories_by_id = {}
        class_items = []
        category_ids = []
        visible_by_category = {}
        viewer.set_idle_view()

    def cleanup_temp_extraction() -> None:
        nonlocal temp_extraction
        if temp_extraction is not None:
            temp_extraction.cleanup()
            temp_extraction = None

    index = 0
    while True:
        if dataset is None:
            viewer.set_idle_view()

            while dataset is None:
                if not viewer.is_window_open():
                    cv2.destroyAllWindows()
                    cleanup_temp_extraction()
                    return

                viewer.render()

                pending_action = viewer.consume_action()
                if pending_action == "open_folder":
                    selected_path = pick_input_path_from_dialog()
                    if selected_path:
                        try:
                            loaded_dataset = load_dataset_from_input(selected_path)
                        except Exception as error:
                            show_info_popup(f"Dataset selection failed: {error}")
                        else:
                            cleanup_temp_extraction()
                            dataset = loaded_dataset
                            temp_extraction = loaded_dataset["temp_extraction"]
                            images_path = loaded_dataset["images_path"]
                            images = loaded_dataset["images"]
                            annotations_by_image_id = loaded_dataset["annotations_by_image_id"]
                            categories_by_id = loaded_dataset["categories_by_id"]
                            class_items = loaded_dataset["class_items"]
                            category_ids = [category_id for category_id, _name in class_items]
                            visible_by_category = loaded_dataset["visible_by_category"]
                            has_polygons = loaded_dataset.get("has_polygons", False)
                            index = 0
                            annotation_type = "polygons" if has_polygons else "bboxes"
                            print(f"Loaded {len(images)} images from: {images_path}")
                            print(f"Annotation type detected: {annotation_type}")
                            print("Use mouse wheel zoom, left drag pan, r reset, n next, p previous, q quit.")
                            print("Use the right sidebar checkboxes for class visibility. Buttons: Show all / Hide all / Prev / Reset / Next.")
                            if has_polygons:
                                print("Toggle polygon points from the sidebar checkbox or press t.")
                    continue

                if viewer.consume_redraw_request():
                    continue

                key = cv2.waitKey(20) & 0xFF
                if key == ord("q") or key == 27:
                    cv2.destroyAllWindows()
                    cleanup_temp_extraction()
                    return
                if key == ord("o"):
                    viewer.request_action("open_folder")

            continue

        if not images or index >= len(images):
            show_info_popup("Last image completed")
            cleanup_temp_extraction()
            clear_dataset()
            index = 0
            continue

        image_info = images[index]
        image_name = str(image_info.get("file_name", ""))
        image_path = resolve_image_path(images_path, image_name)

        image = cv2.imread(image_path)
        if image is None:
            print(f"Could not read image at: {image_path}")
            index += 1
            continue

        image_id = image_info.get("id")
        annotations = annotations_by_image_id.get(image_id, []) if image_id is not None else []

        viewer.set_controls_state(class_items, visible_by_category, show_points, show_labels)

        if has_polygons:
            annotated_image, label_items = draw_coco_polygons(
                image,
                annotations,
                categories_by_id,
                visible_by_category,
                viewer.show_points,
                viewer.opacity,
            )
        else:
            annotated_image, label_items = draw_coco_bboxes(
                image,
                annotations,
                categories_by_id,
                visible_by_category,
                viewer.opacity,
            )
        viewer.label_items = label_items

        title = f"{index + 1}/{len(images)} - {os.path.basename(image_name)}"
        viewer.set_image(annotated_image, title, reset_view=True)

        while True:
            if not viewer.is_window_open():
                cv2.destroyAllWindows()
                cleanup_temp_extraction()
                return

            viewer.render()

            pending_action = viewer.consume_action()
            if pending_action is not None:
                if pending_action == "open_folder":
                    selected_path = pick_input_path_from_dialog()
                    if selected_path:
                        try:
                            loaded_dataset = load_dataset_from_input(selected_path)
                        except Exception as error:
                            show_info_popup(f"Dataset selection failed: {error}")
                        else:
                            cleanup_temp_extraction()
                            dataset = loaded_dataset
                            temp_extraction = loaded_dataset["temp_extraction"]
                            images_path = loaded_dataset["images_path"]
                            images = loaded_dataset["images"]
                            annotations_by_image_id = loaded_dataset["annotations_by_image_id"]
                            categories_by_id = loaded_dataset["categories_by_id"]
                            class_items = loaded_dataset["class_items"]
                            category_ids = [category_id for category_id, _name in class_items]
                            visible_by_category = loaded_dataset["visible_by_category"]
                            has_polygons = loaded_dataset.get("has_polygons", False)
                            index = 0
                    break
                if pending_action == "next":
                    index += 1
                    break
                if pending_action == "prev":
                    index = max(0, index - 1)
                    break
                if pending_action == "reset":
                    viewer.reset_view()

            if viewer.consume_redraw_request():
                show_points = viewer.show_points
                show_labels = viewer.show_labels
                if has_polygons:
                    annotated_image, label_items = draw_coco_polygons(
                        image,
                        annotations,
                        categories_by_id,
                        visible_by_category,
                        viewer.show_points,
                        viewer.opacity,
                    )
                else:
                    annotated_image, label_items = draw_coco_bboxes(
                        image,
                        annotations,
                        categories_by_id,
                        visible_by_category,
                        viewer.opacity,
                    )
                viewer.label_items = label_items
                viewer.set_image(annotated_image, title, reset_view=False)
                continue

            key = cv2.waitKey(20) & 0xFF

            if viewer.handle_key(key):
                continue

            if key == ord("q") or key == 27:
                cv2.destroyAllWindows()
                cleanup_temp_extraction()
                return
            if key == ord("n") or key == 13 or key == 32:
                index += 1
                break
            if key == ord("p"):
                index = max(0, index - 1)
                break
            if key == ord("r"):
                viewer.reset_view()
            if key == ord("o"):
                viewer.request_action("open_folder")
                break
            if key == ord("a"):
                for category_id in category_ids:
                    visible_by_category[category_id] = True
                break
            if key == ord("x"):
                for category_id in category_ids:
                    visible_by_category[category_id] = False
                break
            if key == ord("t"):
                show_points = not viewer.show_points
                viewer.show_points = show_points
                break
            if key == ord("l"):
                show_labels = not viewer.show_labels
                viewer.show_labels = show_labels
                break

        if dataset is None:
            continue

        if index >= len(images):
            show_info_popup("Last image completed")
            cleanup_temp_extraction()
            clear_dataset()
            index = 0

    cv2.destroyAllWindows()
    cleanup_temp_extraction()
