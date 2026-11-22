"""Runs inside Blender: blender -b scene.blend --python shardrender_worker.py -- '{"out": ..., "frames": [...]}'

Renders each frame to a temporary file and renames it only once Blender has finished writing,
so an interrupted render can't leave a half-written frame under the final name.
"""
import json
import os
import sys

import bpy

job = json.loads(sys.argv[sys.argv.index("--") + 1])
scene = bpy.context.scene
render = scene.render
render.use_file_extension = False  # we pass the full name ourselves
ext = render.file_extension  # e.g. ".png", follows the scene's output format
os.makedirs(job["out"], exist_ok=True)

for frame in job["frames"]:
    final = os.path.join(job["out"], f"{frame:05d}{ext}")
    if os.path.exists(final):
        continue
    tmp = os.path.join(job["out"], f"{frame:05d}.part{ext}")
    scene.frame_set(frame)
    render.filepath = tmp
    bpy.ops.render.render(write_still=True)
    os.replace(tmp, final)
    print(f"shard-render: frame {frame} -> {final}", flush=True)
