"""Render a Blender animation with N parallel background processes, then verify every frame.

    blender-shard-render scene.blend --frames 1-250 --workers 3 --out frames/

- Frames are interleaved (worker i gets frames i, i+N, ...), so expensive shots spread across workers.
- Each frame is written to a temporary name and renamed when complete: a killed render never
  leaves a truncated file that looks finished.
- After the run every frame is checked (PNG IEND / JPEG EOI); missing or broken frames are
  re-rendered up to --retries times. Exit code 1 if any frame is still missing.
- Frames already on disk and complete are skipped, so an interrupted job resumes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

WORKER = Path(__file__).with_name("shardrender_worker.py")
FRAME_RE = re.compile(r"^(\d{5,})\.(png|jpg|jpeg|exr|tif|tiff|bmp|webp)$", re.I)
COMMON_BLENDER_PATHS = [
    r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/snap/bin/blender",
]


def parse_frames(spec: str) -> list[int]:
    """'1-10,15,20-30x5' -> sorted unique frames. 'a-bxS' means every S-th frame from a to b."""
    frames: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+)(?:x(\d+))?)?", part)
        if not m:
            raise ValueError(f"bad frame range: {part!r}")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        step = int(m.group(3) or 1)
        if end < start or step < 1:
            raise ValueError(f"bad frame range: {part!r}")
        frames.update(range(start, end + 1, step))
    if not frames:
        raise ValueError("no frames selected")
    return sorted(frames)


def assign(frames: list[int], workers: int) -> list[list[int]]:
    """Interleaved split; never more shards than frames."""
    n = max(1, min(workers, len(frames)))
    return [frames[i::n] for i in range(n)]


def is_complete(path: Path) -> bool:
    """True if the image ends with its format's end marker (other formats: non-empty)."""
    try:
        size = path.stat().st_size
        if size == 0:
            return False
        with path.open("rb") as f:
            head = f.read(8)
            f.seek(max(0, size - 12))
            tail = f.read()
    except OSError:
        return False
    suffix = path.suffix.lower()
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n") and b"IEND" in tail
    if suffix in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8") and tail.rstrip(b"\x00").endswith(b"\xff\xd9")
    return True


def existing_frames(out: Path) -> dict[int, Path]:
    found = {}
    if out.is_dir():
        for p in out.iterdir():
            m = FRAME_RE.match(p.name)
            if m:
                found[int(m.group(1))] = p
    return found


def missing_frames(frames: list[int], out: Path, remove_broken: bool = True) -> list[int]:
    """Frames with no complete file. Broken files are deleted so they get re-rendered."""
    found = existing_frames(out)
    todo = []
    for f in frames:
        p = found.get(f)
        if p is not None and is_complete(p):
            continue
        if p is not None and remove_broken:
            p.unlink(missing_ok=True)
        todo.append(f)
    return todo


def find_blender(explicit: str | None = None) -> list[str]:
    if explicit:
        return [explicit]
    for candidate in (os.environ.get("BLENDER"), shutil.which("blender"), *COMMON_BLENDER_PATHS):
        if candidate and Path(candidate).exists():
            return [candidate]
    raise FileNotFoundError("Blender not found: pass --blender or set the BLENDER environment variable")


def worker_command(blender: list[str], blend: Path, out: Path, frames: list[int], threads: int = 0) -> list[str]:
    """threads=0 lets Blender use every core; N caps this worker (Blender's -t)."""
    job = json.dumps({"out": str(Path(out).resolve()), "frames": frames})
    cap = ["-t", str(threads)] if threads else []
    return [*blender, "-b", str(blend), *cap, "--python", str(WORKER), "--", job]


def split_threads(workers: int, cores: int | None = None) -> int:
    """Cores per worker so N workers don't oversubscribe the CPU (at least 1)."""
    return max(1, (cores or os.cpu_count() or 1) // workers)


@dataclass
class Attempt:
    frames: int
    workers: int
    seconds: float
    exit_codes: list[int]


@dataclass
class Report:
    requested: int
    skipped: int
    attempts: list[Attempt] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def seconds(self) -> float:
        return sum(a.seconds for a in self.attempts)


def render(blend: Path, frames: list[int], out: Path, workers: int = 2, retries: int = 1,
           blender: list[str] | None = None, log_dir: Path | None = None, threads: int = 0) -> Report:
    blend, out = Path(blend), Path(out)
    if not blend.exists():
        raise FileNotFoundError(blend)
    out.mkdir(parents=True, exist_ok=True)
    log_dir = Path(log_dir or out / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    blender = blender or find_blender()
    todo = missing_frames(frames, out)
    report = Report(requested=len(frames), skipped=len(frames) - len(todo))
    for attempt in range(retries + 1):
        if not todo:
            break
        shards = assign(todo, workers)
        start = time.perf_counter()
        running = []
        for i, shard in enumerate(shards):
            log = (log_dir / f"attempt{attempt}_shard{i}.log").open("w", encoding="utf-8", errors="replace")
            proc = subprocess.Popen(worker_command(blender, blend, out, shard, threads),
                                    stdout=log, stderr=subprocess.STDOUT)
            running.append((proc, log))
        codes = []
        try:
            for proc, log in running:
                codes.append(proc.wait())
                log.close()
        except KeyboardInterrupt:  # stop every worker, not only the one being waited on
            for proc, log in running:
                proc.kill()
                log.close()
            raise
        report.attempts.append(Attempt(len(todo), len(shards), time.perf_counter() - start, codes))
        todo = missing_frames(frames, out)
    report.failed = todo
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="blender-shard-render", description=__doc__.splitlines()[0])
    ap.add_argument("blend", type=Path)
    ap.add_argument("--frames", required=True, help="e.g. 1-250 or 1-100,120,200-300x2")
    ap.add_argument("--out", type=Path, required=True, help="output folder (frames are named 00001.png ...)")
    ap.add_argument("--workers", type=int, default=2, help="parallel Blender processes (default 2)")
    ap.add_argument("--retries", type=int, default=1, help="re-render passes for missing/broken frames")
    ap.add_argument("--threads", default="0",
                    help="render threads per worker: 0 = Blender decides, 'split' = cores / workers, or a number")
    ap.add_argument("--blender", help="path to the Blender executable")
    ap.add_argument("--version", action="version", version=__version__)
    a = ap.parse_args(argv)
    if a.workers < 1 or a.retries < 0:
        ap.error("--workers must be >= 1 and --retries >= 0")
    if a.threads != "split" and not a.threads.isdigit():
        ap.error("--threads must be 0, 'split' or a positive number")
    threads = split_threads(a.workers) if a.threads == "split" else int(a.threads)
    try:
        frames = parse_frames(a.frames)
        report = render(a.blend, frames, a.out, a.workers, a.retries, find_blender(a.blender), threads=threads)
    except (ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    rendered = report.requested - report.skipped - len(report.failed)
    print(f"{report.requested} frames: {report.skipped} already done, {rendered} rendered, "
          f"{len(report.failed)} missing, {report.seconds:.1f}s over {len(report.attempts)} pass(es)")
    for i, at in enumerate(report.attempts):
        print(f"  pass {i}: {at.frames} frames on {at.workers} workers in {at.seconds:.1f}s, exit codes {at.exit_codes}")
    if report.failed:
        shown = ", ".join(map(str, report.failed[:20])) + (" ..." if len(report.failed) > 20 else "")
        print(f"missing frames: {shown} (see {a.out / 'logs'})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
