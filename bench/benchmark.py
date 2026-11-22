"""Benchmark: how many workers pay off, and does an interrupted render resume cleanly?

    python bench/benchmark.py [--frames 24] [--workers 1,2,3,4]

Builds a Cycles (CPU) scene, renders the same frames with each worker count into a fresh folder,
then kills a render halfway and resumes it. Writes bench/results.csv and prints a table.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shardrender as sr  # noqa: E402

SCENE = """
import bpy, math
bpy.ops.wm.read_factory_settings(use_empty=True)
s = bpy.context.scene
s.render.engine = 'CYCLES'; s.cycles.device = 'CPU'; s.cycles.samples = {samples}
s.render.resolution_x, s.render.resolution_y = 640, 360
s.frame_start, s.frame_end = 1, {frames}
for i in range(12):
    bpy.ops.mesh.primitive_monkey_add(location=((i % 4) * 2.5 - 3.75, (i // 4) * 2.5 - 2.5, 0))
    m = bpy.context.object.modifiers.new('s', 'SUBSURF'); m.levels = m.render_levels = 2
bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, -1))
bpy.ops.object.light_add(type='SUN', rotation=(0.6, 0.2, 0.8))
bpy.ops.object.camera_add(location=(0, -14, 7), rotation=(math.radians(62), 0, 0))
cam = bpy.context.object; s.camera = cam
cam.keyframe_insert('location', frame=1); cam.location.x = 6; cam.keyframe_insert('location', frame={frames})
w = bpy.data.worlds.new('w'); s.world = w
bpy.ops.wm.save_as_mainfile(filepath=r'{path}')
"""


def make_scene(blender: list[str], path: Path, frames: int, samples: int) -> None:
    code = SCENE.format(frames=frames, samples=samples, path=path)
    subprocess.run([*blender, "-b", "--factory-startup", "--python-expr", code], check=True, capture_output=True)


def interrupted_resume(blender, blend, frames, workers, root: Path, kill_after: float) -> dict:
    """Start a render, kill every worker after kill_after s, then resume with the tool."""
    out = root / "interrupt"
    out.mkdir()
    shards = sr.assign(frames, workers)
    procs = [subprocess.Popen(sr.worker_command(blender, blend, out, s), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) for s in shards]
    time.sleep(kill_after)
    for p in procs:
        p.kill()
        p.wait()
    done_before = len(frames) - len(sr.missing_frames(frames, out, remove_broken=False))
    partials = len(list(out.glob("*.part.*")))
    report = sr.render(blend, frames, out, workers=workers, blender=blender)
    broken = [f for f, p in sr.existing_frames(out).items() if not sr.is_complete(p)]
    return {"done_before_kill": done_before, "partial_tmp_files": partials, "resumed_skipped": report.skipped,
            "rendered_after": report.requested - report.skipped - len(report.failed),
            "missing_after": len(report.failed), "broken_after": len(broken)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--workers", default="1,2,3,4")
    ap.add_argument("--blender")
    a = ap.parse_args()
    blender = sr.find_blender(a.blender)
    frames = list(range(1, a.frames + 1))
    root = Path(tempfile.mkdtemp(prefix="shardbench_"))
    try:
        blend = root / "bench.blend"
        make_scene(blender, blend, a.frames, a.samples)
        rows = []
        counts = [int(x) for x in a.workers.split(",")]
        runs = [(w, 0) for w in counts] + [(w, sr.split_threads(w)) for w in counts if w > 1]
        for w, threads in runs:
            r = sr.render(blend, frames, root / f"w{w}_t{threads}", workers=w, retries=0, blender=blender,
                          threads=threads)
            assert r.ok, f"{w} workers left frames missing: {r.failed}"
            rows.append({"workers": w, "threads_per_worker": threads or "auto", "frames": a.frames,
                         "seconds": round(r.seconds, 1), "s_per_frame": round(r.seconds / a.frames, 2)})
            print(f"{w} workers x {threads or 'auto'} threads: {r.seconds:6.1f} s", flush=True)
        base = rows[0]["seconds"]
        for row in rows:
            row["speedup"] = round(base / row["seconds"], 2)
        best = min(rows, key=lambda r: r["seconds"])
        kill_after = max(3.0, best["seconds"] / 2)
        resume = interrupted_resume(blender, blend, frames, best["workers"], root, kill_after)
        dest = Path(__file__).with_name("results.csv")
        with dest.open("w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        print(f"\n{'workers':>8}{'threads':>9}{'seconds':>10}{'s/frame':>10}{'speedup':>10}")
        for r in rows:
            print(f"{r['workers']:>8}{r['threads_per_worker']:>9}{r['seconds']:>10}{r['s_per_frame']:>10}{r['speedup']:>10}")
        print(f"\nInterrupted after {kill_after:.0f}s with {best['workers']} workers, then resumed: {resume}")
        return 0 if resume["missing_after"] == 0 and resume["broken_after"] == 0 else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
