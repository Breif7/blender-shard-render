# blender-shard-render

[![CI](https://github.com/Breif7/blender-shard-render/actions/workflows/ci.yml/badge.svg)](https://github.com/Breif7/blender-shard-render/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Blender](https://img.shields.io/badge/Blender-4.x%20%7C%205.x-orange) ![License](https://img.shields.io/badge/license-MIT-green)

**Render a Blender animation with several background processes, then prove every frame is on disk and intact.** It re-renders whatever is missing or truncated, and resumes after a crash without redoing finished frames.

```
$ blender-shard-render bench.blend --frames 1-24 --workers 4 --out frames/
24 frames: 0 already done, 24 rendered, 0 missing, 51.6s over 1 pass(es)
  pass 0: 24 frames on 4 workers in 51.6s, exit codes [0, 0, 0, 0]
```

Built for a pipeline that renders long animated videos from Python. It cut one long episode from **2 h 51 min to 1 h 55 min**, and it exists because a killed render once left 728 frames missing (and others truncated) while still exiting 0.

## Why not just `blender -b -a`?

| Problem | What this tool does |
| --- | --- |
| One process can't use the whole machine when each frame has serial work (Python scene updates, compositing, saving) | N workers render at the same time; frames are **interleaved** (worker *i* gets *i, i+N, …*) so expensive shots spread across workers |
| A killed or crashed render leaves half-written PNGs that look finished | Each frame is written to `00042.part.png` and renamed only when complete |
| Exit code 0 doesn't mean every frame exists | After each pass every frame is checked (PNG `IEND`, JPEG `EOI`); missing or broken frames are re-rendered (`--retries`, default 1); exit code 1 if any are still missing |
| Restarting re-renders everything | Complete frames are skipped, so re-running the same command resumes |

## Install

```
pip install git+https://github.com/Breif7/blender-shard-render
```

Needs Python 3.10+ and Blender (found on `PATH`, in `$BLENDER`, in the default install folders, or via `--blender`).

## Usage

```
blender-shard-render scene.blend --frames 1-250 --out frames/ [--workers 2] [--retries 1] [--threads 0|split|N]
```

- **`--frames`:** e.g. `1-250`, `1-100,120,200-300x2` (`x2` means every second frame).
- **Output:** frames are named `00001.png` and follow the scene's file format. There is one log per worker and pass in `frames/logs/`.
- **`--threads`:** render threads per worker. The default `0` lets Blender decide; `split` gives each worker cores ÷ workers. See the case study before you change it.

From Python:

```python
import shardrender
report = shardrender.render("scene.blend", list(range(1, 251)), "frames", workers=3)
assert report.ok, report.failed
```

## How many workers?

**Measure: it depends on the scene.** `python bench/benchmark.py` renders the same frames with 1–4 workers, then kills a render halfway and checks that the resume is clean.

Results on a 20-thread CPU, Cycles CPU at 640×360, 24 frames:

| Workers | Threads each | Seconds | Speed-up |
| --- | --- | --- | --- |
| 1 | auto | 62.4 | 1.00 |
| 2 | auto | 53.0 | 1.18 |
| 4 | auto | 51.6 | 1.21 |
| 4 | 5 (split) | 54.3 | 1.15 |

- **Pure Cycles CPU** already keeps every core busy, so extra workers add ~20 %.
- **Python-driven scenes** with per-frame serial work gain much more (~1.5× in production).
- **Kill-and-resume test:** 8 frames done before the kill, 0 partial files, 16 rendered on resume, 0 missing, 0 broken.

The whole measure → hypothesis → re-measure story is in [docs/case-study.md](docs/case-study.md).

## Tests

```
pip install -e . pytest
pytest -q
```

- **Coverage:** 11 tests. A fake Blender simulates success, crashes, skipped frames and truncated files.
- **Real Blender:** one end-to-end test runs when Blender is installed and is skipped otherwise.
- **CI:** Ubuntu and Windows, Python 3.10 and 3.13.

## Limits

- One machine only; for several machines, point each one at a different `--frames` range on a shared folder.
- Every worker loads the scene itself, which is a fixed cost per worker. On short renders that cost can outweigh the gain.
- Frame validation is structural for PNG and JPEG. For EXR/TIFF it only checks the file isn't empty.

MIT © Breif7
