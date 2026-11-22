import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import shardrender as sr

FAKE = [sys.executable, str(Path(__file__).with_name("fake_blender.py"))]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x00IEND\xaeB`\x82"


@pytest.fixture
def blend(tmp_path):
    p = tmp_path / "scene.blend"
    p.write_bytes(b"BLENDER")
    return p


def test_parse_frames():
    assert sr.parse_frames("1-5") == [1, 2, 3, 4, 5]
    assert sr.parse_frames("1-10x3, 2, 20") == [1, 2, 4, 7, 10, 20]
    for bad in ["", "5-1", "a", "1-10x0", "-3"]:
        with pytest.raises(ValueError):
            sr.parse_frames(bad)


def test_assign_interleaves_and_caps_workers():
    assert sr.assign([1, 2, 3, 4, 5], 2) == [[1, 3, 5], [2, 4]]
    assert sr.assign([7], 4) == [[7]]
    shards = sr.assign(list(range(100)), 3)
    assert sorted(sum(shards, [])) == list(range(100))


def test_thread_cap_in_command_and_split():
    cmd = sr.worker_command(["blender"], Path("s.blend"), Path("o"), [1], threads=5)
    assert cmd[1:5] == ["-b", "s.blend", "-t", "5"]
    assert "-t" not in sr.worker_command(["blender"], Path("s.blend"), Path("o"), [1])
    assert sr.split_threads(4, cores=20) == 5 and sr.split_threads(30, cores=20) == 1


def test_is_complete_detects_truncation(tmp_path):
    good, cut, empty, jpg, jcut = (tmp_path / n for n in ["a.png", "b.png", "c.png", "d.jpg", "e.jpg"])
    good.write_bytes(PNG)
    cut.write_bytes(PNG[:10])
    empty.write_bytes(b"")
    jpg.write_bytes(b"\xff\xd8\xff\xe0data\xff\xd9")
    jcut.write_bytes(b"\xff\xd8\xff\xe0da")
    assert sr.is_complete(good) and sr.is_complete(jpg)
    assert not any(sr.is_complete(p) for p in (cut, empty, jcut, tmp_path / "nope.png"))


def test_missing_frames_removes_broken_files(tmp_path):
    (tmp_path / "00001.png").write_bytes(PNG)
    (tmp_path / "00002.png").write_bytes(PNG[:5])
    (tmp_path / "00002.part.png").write_bytes(PNG)  # temp files never count as done
    assert sr.missing_frames([1, 2, 3], tmp_path) == [2, 3]
    assert not (tmp_path / "00002.png").exists()


def test_render_all_frames(tmp_path, blend, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    out = tmp_path / "frames"
    r = sr.render(blend, list(range(1, 11)), out, workers=3, blender=FAKE)
    assert r.ok and r.skipped == 0 and len(r.attempts) == 1
    assert r.attempts[0].workers == 3 and r.attempts[0].exit_codes == [0, 0, 0]
    assert sorted(sr.existing_frames(out)) == list(range(1, 11))
    assert len(list((out / "logs").glob("*.log"))) == 3


def test_feedback_loop_rerenders_missing_and_truncated(tmp_path, blend, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "flaky")
    out = tmp_path / "frames"
    r = sr.render(blend, list(range(1, 13)), out, workers=2, retries=1, blender=FAKE)
    assert r.ok, r.failed
    assert len(r.attempts) == 2 and r.attempts[1].frames < 12  # second pass only redoes the gaps
    assert all(sr.is_complete(p) for p in sr.existing_frames(out).values())


def test_resume_skips_finished_frames(tmp_path, blend, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    out = tmp_path / "frames"
    sr.render(blend, [1, 2, 3], out, blender=FAKE)
    r = sr.render(blend, [1, 2, 3, 4], out, blender=FAKE)
    assert r.skipped == 3 and r.attempts[0].frames == 1


def test_crash_reports_failed_frames(tmp_path, blend, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    r = sr.render(blend, [1, 2], tmp_path / "f", retries=2, blender=FAKE)
    assert r.failed == [1, 2] and len(r.attempts) == 3 and r.attempts[0].exit_codes == [1, 1]


def test_cli_exit_codes(tmp_path, blend, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    script = tmp_path / "fakeblender.cmd" if os.name == "nt" else tmp_path / "fakeblender"
    if os.name == "nt":
        script.write_text(f'@"{FAKE[0]}" "{FAKE[1]}" %*\n')
    else:
        script.write_text(f'#!/bin/sh\nexec "{FAKE[0]}" "{FAKE[1]}" "$@"\n')
        script.chmod(0o755)
    base = [str(blend), "--frames", "1-4", "--out", str(tmp_path / "o"), "--blender", str(script)]
    assert sr.main(base) == 0
    monkeypatch.setenv("FAKE_MODE", "crash")
    assert sr.main([*base[:2], "5-6", *base[3:]]) == 1
    assert sr.main([str(tmp_path / "missing.blend"), "--frames", "1", "--out", "x", "--blender", "b"]) == 2
    assert sr.main([str(blend), "--frames", "9-1", "--out", "x", "--blender", "b"]) == 2


@pytest.mark.skipif(not (os.environ.get("BLENDER") or shutil.which("blender")
                         or any(Path(p).exists() for p in sr.COMMON_BLENDER_PATHS)),
                    reason="Blender not installed")
def test_real_blender_end_to_end(tmp_path):
    blender = sr.find_blender()
    blend = tmp_path / "cube.blend"
    setup = ("import bpy; s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; "
             "s.render.resolution_x=64; s.render.resolution_y=64; s.frame_end=4; "
             f"bpy.ops.wm.save_as_mainfile(filepath=r'{blend}')")
    subprocess.run([*blender, "-b", "--factory-startup", "--python-expr", setup], check=True, capture_output=True)
    r = sr.render(blend, [1, 2, 3, 4], tmp_path / "frames", workers=2, blender=blender)
    assert r.ok
    assert sorted(sr.existing_frames(tmp_path / "frames")) == [1, 2, 3, 4]
    assert not list((tmp_path / "frames").glob("*.part.*"))
