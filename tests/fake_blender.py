"""Stand-in for blender.exe in tests: same argv shape, writes tiny PNGs.

FAKE_MODE=ok      every frame written
FAKE_MODE=flaky   first run per shard skips every 3rd frame and leaves one truncated file
FAKE_MODE=crash   exits 1 without writing anything
"""
import json
import os
import sys
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00IEND\xaeB`\x82"

job = json.loads(sys.argv[sys.argv.index("--") + 1])
out = Path(job["out"])
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "crash":
    sys.exit(1)
marker = out / f".flaky_seen_{job['frames'][0]}"
first_run = mode == "flaky" and not marker.exists()
if mode == "flaky":
    marker.touch()
for i, frame in enumerate(job["frames"]):
    if first_run and i % 3 == 2:
        continue
    data = PNG[:10] if first_run and i == 0 else PNG  # truncated: no IEND
    (out / f"{frame:05d}.png").write_bytes(data)
print(f"fake blender rendered {job['frames']}")
