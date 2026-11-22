# Case study: making long Blender renders faster and trustworthy

## Context

A Python pipeline renders animated explainer videos in Blender: characters, charts and text, with every frame driven by a timeline. A long episode is ~2,000+ frames.

## Iteration 1 (production): find the idle cores

**Measure.** One background Blender process rendered at 0.449 s/frame while 18 of 20 logical cores sat idle. Most of each frame was serial Python work (moving objects, rebuilding text and charts), not path tracing.

**Change.**
- Split the frames across N processes.
- **Interleave** them (worker *i* takes *i, i+N, i+2N…*) instead of contiguous blocks, because cost varies a lot by shot: a block split left one worker grinding every expensive "screen" shot while the others finished early.

**Re-measure.**
- A long episode went from **2 h 51 min to 1 h 55 min** (1.49×) with 2–3 workers.
- Each worker rebuilds the whole scene first (~1 min 55 s), so short videos stay at one worker.

## Iteration 2 (production): a green exit code that lied

**Incident.** An interrupted render was resumed. The command exited 0 and printed "Frames rendered", but:

- only 1,433 of 2,161 frames existed;
- several files were truncated at scattered indices;
- there were gaps of hundreds of frames.

The two workers had been killed unevenly.

**Root cause.** Blender writes straight to the final file name, so a killed process leaves a partial PNG that looks done, and a missing frame never fails the process.

**Change.** This is where the tool comes from:
1. Write each frame to `NNNNN.part.ext` and rename it only after Blender finishes. A kill can't produce a fake "done" frame.
2. After every pass, check every requested frame structurally: PNG must end with `IEND`, JPEG with `EOI`.
3. Delete broken files and re-render the missing frames (`--retries`).
4. Exit 1 if anything is still missing.

## Iteration 3 (this repo): measure on a neutral scene

`bench/benchmark.py` builds a Cycles CPU scene (12 subdivided meshes, 640×360, 32 samples) and renders the same 24 frames with 1–4 workers on a 20-thread machine.

**Run A** (Blender decides threads):

| Workers | Seconds | Speed-up |
| --- | --- | --- |
| 1 | 58.9 | 1.00 |
| 2 | 52.4 | 1.12 |
| 3 | 50.7 | 1.16 |
| 4 | 51.9 | 1.13 |

**Reading.** The gain is far smaller than in production. Unlike the production scenes, pure Cycles CPU already spreads one frame across all 20 threads, so a second process mostly fills the gaps (scene sync, image save) between renders.

**Hypothesis.** Several workers that each use all 20 threads oversubscribe the CPU. Capping each worker at cores ÷ workers should help.

**Change.** Added `--threads split` (Blender's `-t`) and benchmarked both modes.

**Run B** (same scene, both modes):

| Workers | Threads each | Seconds | Speed-up |
| --- | --- | --- | --- |
| 1 | auto | 62.4 | 1.00 |
| 2 | auto | 53.0 | 1.18 |
| 3 | auto | 54.2 | 1.15 |
| 4 | auto | 51.6 | 1.21 |
| 2 | 10 | 59.1 | 1.06 |
| 3 | 6 | 58.2 | 1.07 |
| 4 | 5 | 54.3 | 1.15 |

**Result: hypothesis rejected.** Capping threads was slower in every configuration. When one worker is in a serial phase, the others can borrow its idle cores, and a hard cap prevents that. `--threads` stays available, but the default is "let Blender decide". The one-worker time also moved 3.5 s between runs, so differences under ~5 % are noise.

**Kill-and-resume check** (4 workers, killed after 26 s, then the same command re-run):

| Done before kill | Partial `.part` files | Skipped on resume | Rendered on resume | Missing after | Broken after |
| --- | --- | --- | --- | --- | --- |
| 8 | 0 | 8 | 16 | 0 | 0 |

The production incident can't happen again: the kill left no fake frames, and the resume did only the missing work.

## Takeaways

1. **Sharding pays when a frame has serial work.** In Python-driven scenes it gave ~1.5×; in pure Cycles CPU it gave ~1.2×. Benchmark your own scene: `python bench/benchmark.py`.
2. **Interleave, don't block**, when shot cost varies.
3. **Never trust the exit code of a parallel render.** Verify the artifacts.
4. **Test the obvious optimization.** Splitting threads "should" help; measured, it hurt.

## Reproduce

```
python bench/benchmark.py --frames 24 --workers 1,2,3,4
```

Writes `bench/results.csv`. It takes about 10 minutes on a 20-thread CPU.
