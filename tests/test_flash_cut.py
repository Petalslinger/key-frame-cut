#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash_cut 的自测：不需要真实 ffmpeg，用 tests/fake_ffmpeg.py 模拟解码。

运行：
    python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import flash_cut as fc  # noqa: E402

SHIM = str(Path(__file__).with_name("fake_ffmpeg.py"))
FAKE_FFMPEG = f"{sys.executable} {SHIM}"  # flash_cut 内部会按空格拆分
DURATION = 10.0
SEGMENT = 2.0  # 假视频每 2 秒整体换一次画面


def _engine_env() -> dict:
    """子进程统一按 UTF-8 输出，避免 Windows 默认编码把中文替换成问号。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def gray(value: int, h: int = 72, w: int = 128) -> np.ndarray:
    return np.full((h, w), value, np.uint8)


class SelectorTests(unittest.TestCase):
    def test_first_frame_is_always_kept(self):
        sel = fc.KeyframeSelector("mad", 0.04)
        kept, score = sel.feed(0.0, gray(30))
        self.assertTrue(kept)
        self.assertIsNone(score)

    def test_identical_and_near_identical_rejected(self):
        sel = fc.KeyframeSelector("mad", 0.04)
        sel.feed(0.0, gray(30))
        self.assertFalse(sel.feed(0.1, gray(30))[0])
        self.assertFalse(sel.feed(0.2, gray(34))[0])  # 4/255 ≈ 0.016 < 0.04

    def test_change_against_last_kept_is_accepted(self):
        sel = fc.KeyframeSelector("mad", 0.04)
        sel.feed(0.0, gray(30))
        kept, score = sel.feed(0.5, gray(200))
        self.assertTrue(kept)
        self.assertGreater(score, 0.5)

    def test_slow_drift_accumulates_against_reference(self):
        """每步只差 4/255，但参考帧不变，累计到第 3 步就应被切出来。"""
        sel = fc.KeyframeSelector("mad", 0.04)
        kept_at = []
        for i, value in enumerate(range(0, 40, 4)):
            if sel.feed(i * 0.1, gray(value))[0]:
                kept_at.append(i)
        self.assertEqual(kept_at, [0, 3, 6, 9])

    def test_min_gap_throttles_bursts(self):
        sel = fc.KeyframeSelector("mad", 0.0, min_gap=1.0, dedupe="last")
        kept_at = []
        for i in range(30):
            value = 255 if i % 2 else 0  # 每帧都“不同”
            if sel.feed(i * 0.1, gray(value))[0]:
                kept_at.append(i)
        self.assertEqual(kept_at[0], 0)
        self.assertTrue(all(b - a >= 10 for a, b in zip(kept_at, kept_at[1:])), kept_at)

    def test_max_frames(self):
        sel = fc.KeyframeSelector("mad", 0.0, max_frames=2, dedupe="last")
        sel.feed(0.0, gray(0))
        sel.feed(0.1, gray(255))
        self.assertFalse(sel.feed(0.2, gray(0))[0])
        self.assertEqual(sel.count, 2)

    def test_dhash_survives_small_noise(self):
        rng = np.random.default_rng(0)
        blocks = (np.add.outer(np.arange(8), np.arange(9)) % 2 * 255).astype(np.uint8)
        base = np.kron(blocks, np.ones((9, 15), np.uint8))  # 72x135 棋盘大色块
        noisy = np.clip(base.astype(np.int16) + rng.integers(-3, 4, base.shape), 0, 255).astype(np.uint8)
        self.assertEqual(fc._distance_dhash(fc._dhash(base), fc._dhash(noisy)), 0.0)
        self.assertGreaterEqual(fc._distance_dhash(fc._dhash(base), fc._dhash(gray(255))), 20.0)

    def test_hist_distance(self):
        a, b = fc._hist32(gray(30)), fc._hist32(gray(200))
        self.assertGreater(fc._distance_hist(a, b), 0.5)
        self.assertAlmostEqual(fc._distance_hist(a, fc._hist32(gray(30))), 0.0, places=6)

    def test_choose_scan_size(self):
        self.assertEqual(fc.choose_scan_size(1920, 1080, 72), (128, 72))
        w, h = fc.choose_scan_size(1080, 1920, 72)
        self.assertEqual(h, 72)
        self.assertEqual(w % 2, 0)


class DedupeTests(unittest.TestCase):
    def test_all_mode_drops_repeat(self):
        """A → B → A：第二个 A 和第一个 A 太像，不再导出。"""
        sel = fc.KeyframeSelector("mad", 0.04, dedupe="all")
        self.assertTrue(sel.feed(0.0, gray(0))[0])
        self.assertTrue(sel.feed(1.0, gray(255))[0])
        kept, score = sel.feed(2.0, gray(2))
        self.assertFalse(kept)
        self.assertGreater(score, 0.9)  # 它和“上一帧”确实不同，只是和更早的帧重复
        self.assertEqual(sel.duplicates, 1)

    def test_last_mode_keeps_repeat(self):
        sel = fc.KeyframeSelector("mad", 0.04, dedupe="last")
        self.assertTrue(sel.feed(0.0, gray(0))[0])
        self.assertTrue(sel.feed(1.0, gray(255))[0])
        self.assertTrue(sel.feed(2.0, gray(0))[0])

    def test_dupe_threshold_override(self):
        sel = fc.KeyframeSelector("mad", 0.04, dupe_threshold=0.2)
        self.assertTrue(sel.feed(0.0, gray(100))[0])
        self.assertTrue(sel.feed(0.5, gray(0))[0])      # 差 0.39，不算重复
        self.assertFalse(sel.feed(1.0, gray(105))[0])   # 和 100 只差 0.02，算重复

    def test_dhash_metric_dedupe(self):
        blocks = (np.add.outer(np.arange(8), np.arange(9)) % 2 * 255).astype(np.uint8)
        a = np.kron(blocks, np.ones((9, 15), np.uint8))  # 72x135 棋盘
        b = 255 - a
        sel = fc.KeyframeSelector("dhash", 8, dedupe="all")
        self.assertTrue(sel.feed(0.0, a)[0])
        self.assertTrue(sel.feed(1.0, b)[0])
        self.assertFalse(sel.feed(2.0, a)[0])


class ProbeTests(unittest.TestCase):
    def test_parse_ffmpeg_stderr(self):
        info = fc.probe_ffmpeg(fc._split_cmd(FAKE_FFMPEG), Path("whatever.mp4"))
        self.assertEqual((info.width, info.height), (640, 360))
        self.assertAlmostEqual(info.fps, 25.0)
        self.assertAlmostEqual(info.duration, DURATION)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.video = self.dir / "sample.mp4"
        self.video.write_bytes(b"fake-video")

    def tearDown(self):
        self._tmp.cleanup()

    def run_engine(self, target, *args):
        cmd = [sys.executable, str(ROOT / "flash_cut.py"), str(target), "--ffmpeg", FAKE_FFMPEG, *args]
        return subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=_engine_env()
        )

    def run_cli(self, *args):
        return self.run_engine(self.video, *args)

    def test_dry_run_prints_cut_points(self):
        r = self.run_cli("--dry-run", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("00:00:02.000", r.stdout)
        self.assertFalse((self.dir / "sample_frames").exists())

    def test_full_run_writes_frames_and_manifest(self):
        out = self.dir / "out"
        r = self.run_cli("-o", str(out), "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        frames = sorted(out.glob("frame_*.jpg"))
        self.assertEqual(len(frames), 5)
        meta = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([f["time"] for f in meta["frames"]], [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(meta["params"]["dedupe"], "all")
        self.assertAlmostEqual(meta["params"]["dupe_threshold"], 0.02)
        self.assertTrue((out / "frames.csv").exists())
        self.assertTrue(frames[0].stat().st_size > 0)
        self.assertEqual([p.name for p in frames], [f"frame_{i:05d}.jpg" for i in range(1, 6)])

    def test_custom_prefix_and_format(self):
        out = self.dir / "pref"
        r = self.run_cli("-o", str(out), "--prefix", "shot_", "--max-frames", "2", "--format", "png", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        names = sorted(p.name for p in out.glob("shot_*"))
        self.assertEqual(names, ["shot_00001.png", "shot_00002.png"])

    def test_max_frames_and_contact_sheet(self):
        out = self.dir / "out2"
        r = self.run_cli("-o", str(out), "--max-frames", "3", "--sheet", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list(out.glob("frame_*.jpg"))), 3)
        if not (out / "contact_sheet.jpg").exists():
            self.skipTest("Pillow 未安装，跳过拼图检查")

    def test_folder_input(self):
        folder = self.dir / "videos"
        folder.mkdir()
        for name in ("a.mp4", "b.mkv"):
            (folder / name).write_bytes(b"fake")
        (folder / "notes.txt").write_text("x", encoding="utf-8")
        out = self.dir / "batch"
        r = self.run_engine(folder, "-o", str(out), "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(list((out / "a").glob("frame_*.jpg"))), 5)
        self.assertEqual(len(list((out / "b").glob("frame_*.jpg"))), 5)
        self.assertTrue((out / "a" / "manifest.json").exists())
        self.assertIn("批量完成：2/2", r.stderr)

    def test_folder_recursive(self):
        folder = self.dir / "videos2"
        (folder / "sub").mkdir(parents=True)
        (folder / "a.mp4").write_bytes(b"fake")
        (folder / "sub" / "nested.mp4").write_bytes(b"fake")
        out = self.dir / "batch2"
        r = self.run_engine(folder, "-o", str(out), "--recursive", "-q")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((out / "a").is_dir())
        self.assertTrue((out / "nested").is_dir())

    def test_dump_scores(self):
        scores = self.dir / "scores.csv"
        r = self.run_cli("--dry-run", "-q", "--dump-scores", str(scores))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = scores.read_text(encoding="utf-8").strip().splitlines()
        self.assertGreater(len(lines), 50)  # 10s * 10fps + 表头
        self.assertIn("kept", lines[0])


class QualityTests(unittest.TestCase):
    def test_cv2_quality_mapping(self):
        self.assertEqual(fc.jpeg_quality_cv2(1), 97)
        self.assertEqual(fc.jpeg_quality_cv2(31), 50)
        qs = [fc.jpeg_quality_cv2(q) for q in range(1, 32)]
        self.assertEqual(qs, sorted(qs, reverse=True))  # q 越大质量越低
        self.assertGreater(fc.jpeg_quality_cv2(2), 90)


class GuiSmokeTests(unittest.TestCase):
    def test_gui_module_imports(self):
        """只导入、不开窗口：能抓住语法错误和顶层错误。"""
        try:
            import tkinter  # noqa: F401
        except Exception:
            self.skipTest("没有 tkinter")
        sys.path.insert(0, str(ROOT))
        import importlib

        mod = importlib.import_module("flash_gui")
        self.assertTrue(hasattr(mod, "App"))
        self.assertTrue(str(mod.ENGINE).endswith("flash_cut.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
