from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector


def parse_args() -> argparse.Namespace:
    if "--" not in sys.argv:
        raise RuntimeError("Expected Blender script arguments after '--'.")

    argv = sys.argv[sys.argv.index("--") + 1 :]

    parser = argparse.ArgumentParser(
        description="Prepare an OBJ mesh for FoundationPose."
    )
    parser.add_argument("--input", required=True, help="Input OBJ path.")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for processed OBJ files.",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Base name for generated files.",
    )
    parser.add_argument(
        "--merge-distance",
        type=float,
        default=0.001,
        help="Remove-doubles distance in input OBJ coordinate units.",
    )
    parser.add_argument(
        "--coordinate-units",
        default="millimeters",
        choices=["millimeters", "meters", "centimeters"],
        help="Human-readable coordinate unit name recorded in metadata.",
    )
    parser.add_argument(
        "--scale-to-m",
        type=float,
        default=0.001,
        help="Multiplier that converts input mesh coordinates to metres.",
    )
    return parser.parse_args(argv)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def import_obj(path: Path) -> None:
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=str(path))
        return

    if hasattr(bpy.ops.import_scene, "obj"):
        bpy.ops.import_scene.obj(filepath=str(path))
        return

    raise RuntimeError("No supported OBJ importer is available in this Blender build.")


def export_obj(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if hasattr(bpy.ops.wm, "obj_export"):
        operator = bpy.ops.wm.obj_export
        properties = {
            prop.identifier
            for prop in operator.get_rna_type().properties
        }

        kwargs = {"filepath": str(path)}

        # Avoid writing data that can force downstream OBJ readers to split
        # otherwise-shared position vertices by corner attributes.
        optional = {
            "export_selected_objects": True,
            "export_materials": False,
            "export_triangulated_mesh": True,
            "export_normals": False,
            "export_uv": False,
            "export_colors": False,
        }

        for key, value in optional.items():
            if key in properties:
                kwargs[key] = value

        operator(**kwargs)
        return

    if hasattr(bpy.ops.export_scene, "obj"):
        bpy.ops.export_scene.obj(
            filepath=str(path),
            use_selection=True,
            use_materials=False,
            use_triangles=True,
            use_normals=False,
            use_uvs=False,
        )
        return

    raise RuntimeError("No supported OBJ exporter is available in this Blender build.")


def mesh_objects() -> list[bpy.types.Object]:
    objects = [
        obj
        for obj in bpy.context.scene.objects
        if obj.type == "MESH"
    ]

    if not objects:
        raise RuntimeError("The OBJ import produced no mesh objects.")

    return objects


def select_objects(objects: list[bpy.types.Object]) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]


def apply_world_transforms(objects: list[bpy.types.Object]) -> None:
    select_objects(objects)
    bpy.ops.object.transform_apply(
        location=True,
        rotation=True,
        scale=True,
    )


def join_objects(objects: list[bpy.types.Object]) -> bpy.types.Object:
    select_objects(objects)

    if len(objects) > 1:
        bpy.ops.object.join()

    obj = bpy.context.view_layer.objects.active
    if obj is None or obj.type != "MESH":
        raise RuntimeError("Failed to create a single mesh object.")

    obj.name = "FoundationPoseMesh"
    return obj


def blender_bounds(obj: bpy.types.Object) -> tuple[Vector, Vector]:
    if not obj.data.vertices:
        raise RuntimeError("Mesh contains no vertices.")

    coordinates = [vertex.co.copy() for vertex in obj.data.vertices]

    minimum = Vector(
        (
            min(value.x for value in coordinates),
            min(value.y for value in coordinates),
            min(value.z for value in coordinates),
        )
    )
    maximum = Vector(
        (
            max(value.x for value in coordinates),
            max(value.y for value in coordinates),
            max(value.z for value in coordinates),
        )
    )

    return minimum, maximum


def read_obj_bounds(path: Path) -> tuple[list[float], list[float], int, int]:
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    vertex_count = 0
    face_count = 0

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                if len(parts) < 4:
                    continue

                xyz = [float(parts[1]), float(parts[2]), float(parts[3])]
                for axis in range(3):
                    minimum[axis] = min(minimum[axis], xyz[axis])
                    maximum[axis] = max(maximum[axis], xyz[axis])
                vertex_count += 1

            elif line.startswith("f "):
                face_count += 1

    if vertex_count == 0:
        raise RuntimeError(f"No OBJ vertices were found in {path}")

    return minimum, maximum, vertex_count, face_count


def center_of_bounds(
    minimum: list[float],
    maximum: list[float],
) -> list[float]:
    return [
        (minimum[index] + maximum[index]) * 0.5
        for index in range(3)
    ]


def subtract(
    left: list[float],
    right: list[float],
) -> list[float]:
    return [
        left[index] - right[index]
        for index in range(3)
    ]


def negate(value: list[float]) -> list[float]:
    return [-item for item in value]


def clean_mesh(
    obj: bpy.types.Object,
    merge_distance: float,
) -> tuple[int, int, int, int]:
    mesh = obj.data

    vertices_before = len(mesh.vertices)
    faces_before = len(mesh.polygons)

    bm = bmesh.new()
    bm.from_mesh(mesh)

    bmesh.ops.remove_doubles(
        bm,
        verts=list(bm.verts),
        dist=float(merge_distance),
    )

    if bm.faces:
        bmesh.ops.recalc_face_normals(
            bm,
            faces=list(bm.faces),
        )

    bm.normal_update()
    bm.to_mesh(mesh)
    bm.free()

    mesh.update()

    vertices_after = len(mesh.vertices)
    faces_after = len(mesh.polygons)

    return (
        vertices_before,
        vertices_after,
        faces_before,
        faces_after,
    )


def center_mesh_in_blender(obj: bpy.types.Object) -> Vector:
    minimum, maximum = blender_bounds(obj)
    center = (minimum + maximum) * 0.5

    for vertex in obj.data.vertices:
        vertex.co -= center

    obj.data.update()
    return center


def select_only(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def vector_to_list(value: Vector) -> list[float]:
    return [
        float(value.x),
        float(value.y),
        float(value.z),
    ]


def main() -> None:
    args = parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()

    if not input_path.is_file():
        raise FileNotFoundError(f"Input OBJ does not exist: {input_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    original_frame_path = output_dir / f"{args.name}_clean_original_frame.obj"
    centered_path = output_dir / f"{args.name}_clean_centered.obj"
    metadata_path = output_dir / f"{args.name}_mesh_metadata.json"

    source_min, source_max, source_vertices, source_faces = read_obj_bounds(input_path)
    source_center = center_of_bounds(source_min, source_max)

    clear_scene()
    import_obj(input_path)

    objects = mesh_objects()
    apply_world_transforms(objects)
    obj = join_objects(objects)
    select_only(obj)

    (
        blender_vertices_before,
        blender_vertices_after,
        blender_faces_before,
        blender_faces_after,
    ) = clean_mesh(
        obj,
        merge_distance=args.merge_distance,
    )

    # Export once before centering. Blender performs its OBJ axis conversion on
    # export, so parse this file to get metadata in the original OBJ axes.
    export_obj(original_frame_path)

    (
        clean_original_min,
        clean_original_max,
        clean_original_vertices,
        clean_original_faces,
    ) = read_obj_bounds(original_frame_path)

    clean_original_center = center_of_bounds(
        clean_original_min,
        clean_original_max,
    )

    # Center in Blender's internal frame; OBJ export converts the axes back.
    blender_center = center_mesh_in_blender(obj)
    export_obj(centered_path)

    (
        centered_min,
        centered_max,
        centered_vertices,
        centered_faces,
    ) = read_obj_bounds(centered_path)

    centered_center = center_of_bounds(centered_min, centered_max)

    metadata = {
        "input_file": str(input_path),
        "coordinate_units": args.coordinate_units,
        "scale_to_m": float(args.scale_to_m),
        "merge_distance_input_units": float(args.merge_distance),
        "source_obj": {
            "vertices": source_vertices,
            "faces": source_faces,
            "bounds_input_units": {
                "minimum": source_min,
                "maximum": source_max,
            },
            "bounds_center_input_units": source_center,
        },
        "clean_original_frame_obj": {
            "vertices": clean_original_vertices,
            "faces": clean_original_faces,
            "bounds_input_units": {
                "minimum": clean_original_min,
                "maximum": clean_original_max,
            },
            "bounds_center_input_units": clean_original_center,
        },
        "clean_centered_obj": {
            "vertices": centered_vertices,
            "faces": centered_faces,
            "bounds_input_units": {
                "minimum": centered_min,
                "maximum": centered_max,
            },
            "bounds_center_input_units": centered_center,
        },
        "blender_internal_cleanup": {
            "vertices_before": blender_vertices_before,
            "vertices_after": blender_vertices_after,
            "faces_before": blender_faces_before,
            "faces_after": blender_faces_after,
            "center_removed_in_blender_axes_input_units": vector_to_list(blender_center),
        },
        "center_transform_in_original_obj_axes": {
            "mesh_center_in_original_frame_input_units": clean_original_center,
            "centered_from_original_translation_input_units": negate(clean_original_center),
            "original_from_centered_translation_input_units": clean_original_center,
        },
        "preparation_tool": {
            "name": "Midbrain FoundationPose CAD preparation helper",
            "method": "minimal duplicate-vertex cleanup, normal recalculation, preserve original frame, then bounding-box center",
        },
        "validation": {
            "source_and_clean_original_bounds_match": all(
                abs(source_min[index] - clean_original_min[index]) <= 1e-5
                and abs(source_max[index] - clean_original_max[index]) <= 1e-5
                for index in range(3)
            ),
            "centered_bounds_center_near_zero": all(
                abs(value) <= 1e-4
                for value in centered_center
            ),
        },
        "outputs": {
            "clean_original_frame": str(original_frame_path),
            "clean_centered": str(centered_path),
        },
    }

    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print(f"Processed: {input_path.name}")
    print("=" * 72)
    print(
        f"Blender vertices: "
        f"{blender_vertices_before:,} -> {blender_vertices_after:,}"
    )
    print(
        f"Blender faces:    "
        f"{blender_faces_before:,} -> {blender_faces_after:,}"
    )
    print(
        f"Exported OBJ vertices: "
        f"{clean_original_vertices:,}"
    )
    print(
        f"Center removed in original OBJ axes ({args.coordinate_units}): "
        f"[{clean_original_center[0]:.9f}, "
        f"{clean_original_center[1]:.9f}, "
        f"{clean_original_center[2]:.9f}]"
    )
    print(
        "Source/original-frame bounds match: "
        f"{metadata['validation']['source_and_clean_original_bounds_match']}"
    )
    print(
        "Centered OBJ center near zero: "
        f"{metadata['validation']['centered_bounds_center_near_zero']}"
    )
    print(f"Original-frame OBJ: {original_frame_path}")
    print(f"Centered OBJ:       {centered_path}")
    print(f"Metadata:           {metadata_path}")


if __name__ == "__main__":
    main()
