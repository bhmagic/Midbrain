from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile

import bpy
from mathutils import Matrix, Vector
import numpy as np


VIEWS = (
    ("front-right", 45.0, 28.0),
    ("rear-right", 135.0, 28.0),
    ("rear-left", 225.0, 28.0),
    ("front-left", 315.0, 28.0),
)


def parse_args() -> argparse.Namespace:
    if "--" not in sys.argv:
        raise RuntimeError("Expected Blender script arguments after '--'.")
    parser = argparse.ArgumentParser(description="Render a Skill-owned CAD preview.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--scale-to-m", required=True, type=float)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def import_mesh(path: Path, scale_to_m: float) -> bpy.types.Object:
    before = set(bpy.context.scene.objects)
    bpy.ops.wm.obj_import(filepath=str(path.resolve()))
    objects = [
        obj
        for obj in bpy.context.scene.objects
        if obj not in before and obj.type == "MESH"
    ]
    if not objects:
        raise RuntimeError(f"OBJ import produced no mesh: {path}")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    if len(objects) > 1:
        bpy.ops.object.join()
    mesh = bpy.context.view_layer.objects.active
    if mesh is None:
        raise RuntimeError("Unable to create one CAD preview mesh.")
    mesh.scale = (scale_to_m, scale_to_m, scale_to_m)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for polygon in mesh.data.polygons:
        polygon.use_smooth = False
    material = bpy.data.materials.new("CAD preview material")
    material.diffuse_color = (0.34, 0.48, 0.65, 1.0)
    mesh.data.materials.append(material)
    return mesh


def center_and_radius(mesh: bpy.types.Object) -> tuple[Vector, float]:
    vertices = [vertex.co.copy() for vertex in mesh.data.vertices]
    if not vertices:
        raise RuntimeError("CAD preview mesh has no vertices.")
    minimum = Vector(tuple(min(value[index] for value in vertices) for index in range(3)))
    maximum = Vector(tuple(max(value[index] for value in vertices) for index in range(3)))
    center = (minimum + maximum) * 0.5
    radius = max((value - center).length for value in vertices)
    if radius <= 0.0:
        raise RuntimeError("CAD preview mesh has zero radius.")
    return center, radius


def configure_scene(tile_width: int, tile_height: int, radius: float) -> bpy.types.Object:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = tile_width
    scene.render.resolution_y = tile_height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.film_transparent = False
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    scene.display.shading.show_shadows = True
    scene.display.shading.show_cavity = True
    scene.display.shading.cavity_type = "BOTH"
    scene.display.shading.background_type = "WORLD"
    scene.world.color = (0.025, 0.025, 0.025)
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "Medium High Contrast"
    camera_data = bpy.data.cameras.new("CADPreviewCamera")
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = radius * 3.15
    camera_data.clip_start = max(radius * 0.01, 0.0001)
    camera_data.clip_end = max(radius * 100.0, 100.0)
    camera = bpy.data.objects.new("CADPreviewCamera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = (0.0, 0.0, radius * 12.0)
    scene.camera = camera
    return camera


def render_preview(args: argparse.Namespace, mesh: bpy.types.Object) -> None:
    if args.width % 2 or args.height % 2:
        raise ValueError("CAD preview dimensions must divide evenly into a 2x2 atlas.")
    tile_width = args.width // 2
    tile_height = args.height // 2
    center, radius = center_and_radius(mesh)
    configure_scene(tile_width, tile_height, radius)
    atlas = np.ones((args.height, args.width, 4), dtype=np.float32)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="locate-arm-base-cad-", dir=args.output.parent) as temporary:
        for index, (name, azimuth, elevation) in enumerate(VIEWS):
            mesh.matrix_world = (
                Matrix.Rotation(math.radians(elevation), 4, "X")
                @ Matrix.Rotation(math.radians(azimuth), 4, "Z")
                @ Matrix.Translation(-center)
            )
            view_path = Path(temporary) / f"{index:02d}_{name}.png"
            bpy.context.scene.render.filepath = str(view_path.resolve())
            bpy.ops.render.render(write_still=True)
            rendered = bpy.data.images.load(str(view_path.resolve()), check_existing=False)
            pixels = np.empty(tile_width * tile_height * 4, dtype=np.float32)
            rendered.pixels.foreach_get(pixels)
            tile = pixels.reshape((tile_height, tile_width, 4))
            row, column = divmod(index, 2)
            y0 = (1 - row) * tile_height
            x0 = column * tile_width
            atlas[y0 : y0 + tile_height, x0 : x0 + tile_width] = tile
            bpy.data.images.remove(rendered)
    image = bpy.data.images.new(
        "Locate Arm Base CAD Preview",
        width=args.width,
        height=args.height,
        alpha=False,
    )
    image.pixels.foreach_set(atlas.reshape(-1))
    image.file_format = "PNG"
    image.filepath_raw = str(args.output.resolve())
    image.save()


def write_metadata(args: argparse.Namespace, mesh: bpy.types.Object) -> None:
    metadata = {
        "schema": "midbrain.skill.locate_arm_base.cad_preview",
        "schema_version": 1,
        "renderer": "Blender Workbench",
        "blender_version": bpy.app.version_string,
        "label": args.label,
        "input_file": args.input.name,
        "input_sha256": sha256(args.input),
        "output_file": args.output.name,
        "output_sha256": sha256(args.output),
        "scale_to_m": args.scale_to_m,
        "resolution": [args.width, args.height],
        "vertex_count": len(mesh.data.vertices),
        "face_count": len(mesh.data.polygons),
        "views": [
            {"name": name, "azimuth_deg": azimuth, "elevation_deg": elevation}
            for name, azimuth, elevation in VIEWS
        ],
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    args = parse_args()
    if args.scale_to_m <= 0.0 or args.width <= 0 or args.height <= 0:
        raise ValueError("CAD scale and preview dimensions must be positive.")
    clear_scene()
    mesh = import_mesh(args.input, args.scale_to_m)
    render_preview(args, mesh)
    write_metadata(args, mesh)
    print(f"Rendered exact CAD preview: {args.output.resolve()}")


if __name__ == "__main__":
    main()
