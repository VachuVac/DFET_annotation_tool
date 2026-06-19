"""Annotation-mode level catalog.

Three annotation "levels", each a separate annotation pass over the same
images with its own COCO JSON output. The class lists, colors and group
headers are transcribed from the Label Studio interface configs used to
collect this data:

- Level 1: polygons   (surfaces / terrain / objects)
- Level 2: rotatable boxes (vegetation / marks / materials / debris)
- Level 3: polygons   (vehicles / traffic elements / people & objects)

Category ids are 1-based in list order within each level (see ``level_categories``).
"""

from __future__ import annotations

# geometry kinds understood by the annotation canvas
GEOMETRY_POLYGON = "polygon"
GEOMETRY_ROTATED_BBOX = "rotated_bbox"

# Each level: a display title, the geometry its annotations use, and ordered
# groups of (class_name, hex_color). Order is significant -- it drives both the
# sidebar layout and the COCO category ids.
LEVELS: dict[int, dict] = {
    1: {
        "title": "Surfaces & terrain",
        "geometry": GEOMETRY_POLYGON,
        "groups": [
            ("INFRASTRUCTURE", [
                ("road_asphalt", "#FFE119"),
                ("road_concrete", "#3CB44B"),
                ("road_paved", "#F58231"),
                ("road_unpaved", "#4363D8"),
                ("sidewalk_asphalt", "#E6194B"),
                ("sidewalk_concrete", "#911EB4"),
                ("sidewalk_paved", "#42D4F4"),
                ("train_tracks", "#000075"),
                ("ditch", "#BFEF45"),
            ]),
            ("MATERIALS & TERRAIN", [
                ("asphalt", "#FABED4"),
                ("concrete", "#469990"),
                ("sett_pavement", "#DCBEFF"),
                ("gravel", "#9A6324"),
                ("vegetation", "#F032E6"),
                ("natural_ground", "#AAFFC3"),
                ("snow", "#843939"),
                ("water", "#008080"),
            ]),
            ("OBJECTS & OTHER", [
                ("building", "#808000"),
                ("structure", "#A9A9A9"),
                ("undefined", "#00FF00"),
            ]),
        ],
    },
    2: {
        "title": "Details (boxes)",
        "geometry": GEOMETRY_ROTATED_BBOX,
        "groups": [
            ("VEGETATION", [
                ("medium_vegetation", "#42D4F4"),
                ("high_vegetation", "#F032E6"),
            ]),
            ("MARKINGS & TRACES", [
                ("road_sign_pictogram", "#FFE119"),
                ("vehicle_altered_surface", "#E6194B"),
                ("spray_mark", "#F58231"),
            ]),
            ("MATERIALS & DIRT", [
                ("oil_absorbent", "#BFEF45"),
                ("liquid", "#4363D8"),
            ]),
            ("DEBRIS & OTHER", [
                ("debris_small", "#DCBEFF"),
                ("debris_large", "#9A6324"),
                ("other", "#AAFFC3"),
            ]),
        ],
    },
    3: {
        "title": "Objects",
        "geometry": GEOMETRY_POLYGON,
        "groups": [
            ("VEHICLES & TRANSPORT", [
                ("micromobility", "#FFE119"),
                ("cyclist", "#4363D8"),
                ("motorcycle_undamaged", "#3CB44B"),
                ("motorcycle_damaged", "#E6194B"),
                ("quad_bike_undamaged", "#F58231"),
                ("quad_bike_damaged", "#911EB4"),
                ("car_undamaged", "#42D4F4"),
                ("car_damaged", "#F032E6"),
                ("minibus_undamaged", "#BFEF45"),
                ("minibus_damaged", "#FABED4"),
                ("van_undamaged", "#469990"),
                ("van_damaged", "#DCBEFF"),
                ("light_truck_undamaged", "#9A6324"),
                ("light_truck_damaged", "#FFFAC8"),
                ("medium_truck_undamaged", "#800000"),
                ("medium_truck_damaged", "#AAFFC3"),
                ("heavy_truck_undamaged", "#808000"),
                ("heavy_truck_damaged", "#FFD8B1"),
                ("heavy_truck_cab_undamaged", "#000075"),
                ("heavy_truck_cab_damaged", "#A9A9A9"),
                ("semi_trailer_truck_undamaged", "#FFFFFF"),
                ("semi_trailer_truck_damaged", "#000000"),
                ("emergency_vehicle", "#FF1493"),
                ("tractor", "#7CFC00"),
                ("special_vehicle", "#008080"),
                ("bus", "#4B0082"),
                ("articulated_bus", "#FF7F50"),
                ("caravan", "#E6BEFF"),
                ("trailer", "#00FF00"),
            ]),
            ("TRAFFIC ELEMENTS & SIGNAGE", [
                ("road_marking", "#DCDCDC"),
                ("traffic_sign", "#B03060"),
                ("traffic_light", "#ADFF2F"),
                ("traffic_cone", "#FF4500"),
                ("guardrail_metal", "#708090"),
                ("guardrail_concrete", "#556B2F"),
                ("curb", "#8B4513"),
            ]),
            ("PEOPLE & OTHER OBJECTS", [
                ("animal", "#A52A2A"),
                ("human", "#F08080"),
                ("emergency_responders", "#00FFFF"),
                ("stroller", "#DAA520"),
                ("wheelchair", "#8FBC8F"),
                ("pole", "#191970"),
                ("street_lamp", "#FFFAC8"),
                ("drainage", "#6A5ACD"),
                ("evidence_marker", "#FF00FF"),
                ("photogrammetric_target", "#32CD32"),
                ("other_objects_of_interest", "#D2B48C"),
            ]),
        ],
    },
}

LEVEL_IDS: list[int] = sorted(LEVELS.keys())


def level_title(level: int) -> str:
    return LEVELS[level]["title"]


def level_geometry(level: int) -> str:
    return LEVELS[level]["geometry"]


def level_groups(level: int) -> list[tuple[str, list[tuple[str, str]]]]:
    """Ordered [(group_header, [(class_name, hex_color), ...]), ...] for a level."""
    return LEVELS[level]["groups"]


def level_classes(level: int) -> list[tuple[str, str]]:
    """Flat ordered [(class_name, hex_color), ...] for a level."""
    return [pair for _, classes in LEVELS[level]["groups"] for pair in classes]


def level_categories(level: int) -> list[dict]:
    """COCO ``categories`` for a level: 1-based ids in list order."""
    return [
        {"id": index, "name": name}
        for index, (name, _color) in enumerate(level_classes(level), start=1)
    ]


def level_class_colors(level: int) -> dict[str, str]:
    """Map of class_name -> hex_color for a level."""
    return {name: color for name, color in level_classes(level)}


def category_id_for_class(level: int, class_name: str) -> int | None:
    for index, (name, _color) in enumerate(level_classes(level), start=1):
        if name == class_name:
            return index
    return None


def class_name_for_category(level: int, category_id: int) -> str | None:
    classes = level_classes(level)
    if 1 <= category_id <= len(classes):
        return classes[category_id - 1][0]
    return None


def level_for_class(class_name: str) -> int | None:
    """Which level a class name belongs to (first match), or None if unknown."""
    for level in LEVEL_IDS:
        if any(name == class_name for name, _ in level_classes(level)):
            return level
    return None
