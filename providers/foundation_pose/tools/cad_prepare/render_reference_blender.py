from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix, Vector


VIEWS = (
    ("front-low", 0.0, 18.0),
    ("front-high", 45.0, 36.0),
    ("left-low", 90.0, 18.0),
    ("rear-high", 135.0, 36.0),
    ("rear-low", 180.0, 18.0),
    ("rear-high-opposite", 225.0, 36.0),
    ("right-low", 270.0, 18.0),
    ("front-high-opposite", 315.0, 36.0),
)


def parse_args() -> argparse.Namespace:
    if "--" not in sys.argv:
        raise RuntimeError("Expected Blender script arguments after '--'.")
    parser = argparse.ArgumentParser(
        description="Render a full-face CAD reference atlas with Blender."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--scale-to-m", type=float, default=0.001)
    parser.add_argument(
        "--ortho-scale",
        type=float,
        default=None,
        help="Optional fixed orthographic span in metres shared across atlases.",
    )
    parser.add_argument("--width", type=int, default=2048)
    parser.add_argument("--height", type=int, default=1024)
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for block in (
        bpy.data.meshes,
        bpy.data.curves,
        bpy.data.materials,
        bpy.data.cameras,
        bpy.data.lights,
    ):
        for item in list(block):
            if item.users == 0:
                block.remove(item)


def import_mesh(path: Path, scale_to_m: float) -> bpy.types.Object:
    before = set(bpy.context.scene.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=str(path.resolve()))
    elif hasattr(bpy.ops.import_scene, "obj"):
        bpy.ops.import_scene.obj(filepath=str(path.resolve()))
    else:
        raise RuntimeError("This Blender build has no supported OBJ importer.")

    objects = [
        obj
        for obj in bpy.context.scene.objects
        if obj not in before and obj.type == "MESH"
    ]
    if not objects:
        raise RuntimeError(f"OBJ import produced no mesh objects: {path}")

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    if len(objects) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    if obj is None:
        raise RuntimeError("Failed to create a single reference mesh object.")

    obj.scale = (scale_to_m, scale_to_m, scale_to_m)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for polygon in obj.data.polygons:
        polygon.use_smooth = False
    obj.data.update()
    obj.name = "ReferenceMeshSource"
    return obj


def mesh_center_and_radius(obj: bpy.types.Object) -> tuple[Vector, float]:
    vertices = [vertex.co.copy() for vertex in obj.data.vertices]
    if not vertices:
        raise RuntimeError("Reference mesh has no vertices.")
    minimum = Vector(
        (
            min(value.x for value in vertices),
            min(value.y for value in vertices),
            min(value.z for value in vertices),
        )
    )
    maximum = Vector(
        (
            max(value.x for value in vertices),
            max(value.y for value in vertices),
            max(value.z for value in vertices),
        )
    )
    center = (minimum + maximum) * 0.5
    radius = max((value - center).length for value in vertices)
    if radius <= 0.0:
        raise RuntimeError("Reference mesh has zero radius.")
    return center, radius


def make_material(
    name: str,
    color: tuple[float, float, float, float],
) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    if principled is not None:
        principled.inputs["Base Color"].default_value = color
        principled.inputs["Roughness"].default_value = 0.58
        principled.inputs["Metallic"].default_value = 0.08
    return material


def prepare_scene_objects(source: bpy.types.Object) -> None:
    mesh_material = make_material("CAD clay", (0.22, 0.39, 0.58, 1.0))
    source.data.materials.clear()
    source.data.materials.append(mesh_material)


def create_camera(radius: float) -> bpy.types.Object:
    camera_data = bpy.data.cameras.new("AtlasCamera")
    camera_data.type = "ORTHO"
    camera_data.lens = 50.0
    camera_data.clip_start = max(0.0001, radius * 0.01)
    camera_data.clip_end = max(100.0, radius * 100.0)
    camera = bpy.data.objects.new("AtlasCamera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = (0.0, 0.0, radius * 12.0)
    camera.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.scene.camera = camera
    return camera


def configure_render(width: int, height: int) -> None:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.film_transparent = False

    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "MATERIAL"
    shading.show_shadows = False
    shading.show_cavity = False
    shading.show_specular_highlight = False
    shading.background_type = "WORLD"
    scene.world.color = (0.92, 0.94, 0.97)

    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "Medium High Contrast"
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0


def render_atlas(
    args: argparse.Namespace,
    source: bpy.types.Object,
    camera: bpy.types.Object,
    center: Vector,
    radius: float,
) -> float:
    columns = 4
    rows = 2
    if args.width % columns != 0 or args.height % rows != 0:
        raise ValueError("Atlas width must divide by 4 and height must divide by 2.")
    tile_width = args.width // columns
    tile_height = args.height // rows
    configure_render(tile_width, tile_height)

    rotations = [
        Matrix.Rotation(math.radians(elevation_deg), 4, "X")
        @ Matrix.Rotation(math.radians(azimuth_deg), 4, "Z")
        for _, azimuth_deg, elevation_deg in VIEWS
    ]
    required_scales = []
    for rotation in rotations:
        transform = rotation @ Matrix.Translation(-center)
        projected = [transform @ Vector(corner) for corner in source.bound_box]
        extent_x = max(value.x for value in projected) - min(
            value.x for value in projected
        )
        extent_y = max(value.y for value in projected) - min(
            value.y for value in projected
        )
        required_scales.append(max(extent_y, extent_x))
    minimum_ortho_scale = max(required_scales) * 1.02
    shared_ortho_scale = (
        args.ortho_scale
        if args.ortho_scale is not None
        else max(required_scales) * 1.22
    )
    if shared_ortho_scale < minimum_ortho_scale:
        raise ValueError(
            f"--ortho-scale {shared_ortho_scale} clips this model; "
            f"use at least {minimum_ortho_scale}."
        )

    atlas = np.ones((args.height, args.width, 4), dtype=np.float32)
    with tempfile.TemporaryDirectory(
        prefix="foundationpose-cad-reference-",
        dir=str(args.output.parent),
    ) as temporary_dir:
        temp_root = Path(temporary_dir)
        for view_index, (view_name, _, _) in enumerate(VIEWS):
            rotation = rotations[view_index]
            source.matrix_world = rotation @ Matrix.Translation(-center)
            camera.data.ortho_scale = shared_ortho_scale

            view_path = temp_root / f"{view_index:02d}_{view_name}.png"
            bpy.context.scene.render.filepath = str(view_path.resolve())
            bpy.ops.render.render(write_still=True)

            rendered = bpy.data.images.load(str(view_path.resolve()), check_existing=False)
            pixels = np.empty(tile_width * tile_height * 4, dtype=np.float32)
            rendered.pixels.foreach_get(pixels)
            tile = pixels.reshape((tile_height, tile_width, 4))
            row = view_index // columns
            column = view_index % columns
            y0 = (rows - 1 - row) * tile_height
            x0 = column * tile_width
            atlas[y0 : y0 + tile_height, x0 : x0 + tile_width] = tile
            bpy.data.images.remove(rendered)

    atlas_image = bpy.data.images.new(
        "CAD Reference Atlas",
        width=args.width,
        height=args.height,
        alpha=False,
        float_buffer=False,
    )
    atlas_image.pixels.foreach_set(atlas.reshape(-1))
    atlas_image.file_format = "PNG"
    atlas_image.filepath_raw = str(args.output.resolve())
    atlas_image.save()
    return shared_ortho_scale


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_metadata(
    args: argparse.Namespace,
    source: bpy.types.Object,
    shared_ortho_scale: float,
) -> None:
    metadata = {
        "schema": "foundation-pose-cad-reference-render-v1",
        "renderer": "Blender Workbench",
        "blender_version": bpy.app.version_string,
        "label": args.label,
        "input_file": args.input.name,
        "input_sha256": sha256(args.input),
        "output_file": args.output.name,
        "output_sha256": sha256(args.output),
        "vertex_count": len(source.data.vertices),
        "face_count": len(source.data.polygons),
        "face_sampling": False,
        "consistent_orthographic_scale": True,
        "orthographic_scale_m": shared_ortho_scale,
        "surface_shading": "flat_faces",
        "workbench_cavity": False,
        "workbench_specular": False,
        "workbench_shadows": False,
        "scale_to_m": args.scale_to_m,
        "resolution": [args.width, args.height],
        "views": [
            {
                "name": name,
                "azimuth_deg": azimuth,
                "elevation_deg": elevation,
            }
            for name, azimuth, elevation in VIEWS
        ],
    }
    metadata_path = args.output.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    if args.width <= 0 or args.height <= 0:
        raise ValueError("Render dimensions must be positive.")
    if args.scale_to_m <= 0.0:
        raise ValueError("--scale-to-m must be positive.")
    if args.ortho_scale is not None and args.ortho_scale <= 0.0:
        raise ValueError("--ortho-scale must be positive.")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    clear_scene()
    source = import_mesh(args.input, args.scale_to_m)
    center, radius = mesh_center_and_radius(source)
    prepare_scene_objects(source)
    camera = create_camera(radius)
    shared_ortho_scale = render_atlas(args, source, camera, center, radius)
    write_metadata(args, source, shared_ortho_scale)
    print(f"Rendered full-face CAD reference atlas: {args.output.resolve()}")


if __name__ == "__main__":
    main()
