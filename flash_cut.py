#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash_cut.py —— 视频“闪切”：抽出与上一个保留关键帧不同的一系列关键帧。

原理（两遍扫描）：

  第一遍（便宜）：ffmpeg 把视频按固定采样率缩放成灰度小图，通过管道交给 Python。
                 每个采样帧只与「上一个被保留的关键帧」做比较，超过阈值才保留。
  第二遍（精确）：对每个保留时间点，用 ffmpeg 定位并导出原始分辨率的图片。

为什么不是直接 `ffmpeg -vf select='gt(scene,0.3)'`：
  ffmpeg 的 scene 分数比较的是「相邻两帧」，本脚本比较的是「与上一个保留帧」，
  因此慢速渐变、缓慢推镜也能累计到阈值后被切出来，符合“不断切割与上一个关键帧不同的帧”。

用法示例：
  python flash_cut.py movie.mp4
  python flash_cut.py movie.mp4 -o out/ --sample-fps 10 --threshold 0.04
  python flash_cut.py movie.mp4 --dry-run --dump-scores scores.csv   # 先看分数再定阈值

依赖：Python 3.9+、numpy；ffmpeg（推荐）或 opencv-python（备选）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

__version__ = "1.1.0"

# 打包成窗口程序后（PyInstaller --windowed），子进程 ffmpeg 默认会弹黑框，这里统一隐藏
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 输出编码：终端里沿用系统编码（只放宽 errors）；重定向到管道/文件时强制 UTF-8，
# 避免 Windows 的 cp936/cp1252 把中文写成问号。
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream.isatty():
            _stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        else:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 2) -> "NoReturn":  # noqa: F821
    log(f"[错误] {msg}")
    raise SystemExit(code)


def timecode(seconds: float) -> str:
    """秒 -> 00:00:03.200"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _split_cmd(raw: str) -> list[str]:
    """把 `--ffmpeg` 的值拆成命令列表；支持 "C:\\path\\ffmpeg.exe" 或 "py script.py"。"""
    raw = raw.strip().strip('"').strip("'")
    if os.path.exists(raw):
        return [raw]
    parts = shlex.split(raw, posix=False)
    return [p.strip('"').strip("'") for p in parts if p.strip('"').strip("'")]


def _run(cmd: Sequence[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(c) for c in cmd],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=NO_WINDOW,
        **kw,
    )


# ---------------------------------------------------------------------------
# 探测视频信息
# ---------------------------------------------------------------------------

_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_SIZE = re.compile(r"(\d{2,5})x(\d{2,5})")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_RE_ROT = re.compile(r"rotation of\s*(-?\d+(?:\.\d+)?)")


@dataclass
class MediaInfo:
    src: Path
    width: int
    height: int
    fps: float
    duration: float


def _ffprobe_cmd(ffmpeg_cmd: Sequence[str]) -> Optional[list[str]]:
    exe = Path(ffmpeg_cmd[-1])
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    cand = exe.with_name(name)
    if cand.exists():
        return [str(cand)]
    found = shutil.which("ffprobe")
    return [found] if found else None


def probe_ffmpeg(ffmpeg_cmd: Sequence[str], src: Path) -> MediaInfo:
    """优先 ffprobe，其次解析 `ffmpeg -i` 的 stderr（ffmpeg 无输出时返回码为 1）。"""
    width = height = 0
    fps = 0.0
    duration = 0.0

    probe = _ffprobe_cmd(ffmpeg_cmd)
    if probe:
        r = _run(
            probe
            + [
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,r_frame_rate,duration:format=duration",
                "-of",
                "json",
                str(src),
            ]
        )
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = json.loads(r.stdout)
                st = (data.get("streams") or [{}])[0]
                width = int(st.get("width") or 0)
                height = int(st.get("height") or 0)
                for key in ("avg_frame_rate", "r_frame_rate"):
                    raw = st.get(key) or ""
                    if "/" in raw:
                        num, den = raw.split("/", 1)
                        if float(den or 0) > 0:
                            fps = float(num) / float(den)
                            break
                duration = float((data.get("format") or {}).get("duration") or st.get("duration") or 0)
            except Exception:
                pass

    if not (width and height and fps and duration):
        r = _run(list(ffmpeg_cmd) + ["-hide_banner", "-nostdin", "-i", str(src)])
        err = (r.stderr or "") + (r.stdout or "")
        m = _RE_DURATION.search(err)
        if m:
            duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        for line in err.splitlines():
            if "Video:" not in line:
                continue
            sizes = [s for s in _RE_SIZE.findall(line) if int(s[0]) >= 16 and int(s[1]) >= 16]
            if sizes and not (width and height):
                width, height = int(sizes[0][0]), int(sizes[0][1])
            fm = _RE_FPS.search(line)
            if fm and not fps:
                fps = float(fm.group(1))
            break
        rot = _RE_ROT.search(err)
        if rot and abs(float(rot.group(1))) % 180 == 90:
            width, height = height, width

    if not width or not height:
        raise RuntimeError(f"无法解析视频尺寸，可能不是有效视频文件：{src}")
    return MediaInfo(src=src, width=int(width), height=int(height), fps=float(fps or 25.0), duration=float(duration))


def choose_scan_size(width: int, height: int, target_h: int) -> tuple[int, int]:
    """按比例缩放，高度固定 target_h，宽度取偶数（缩小后比较，快且抗噪）。"""
    if width <= 0 or height <= 0:
        return 128, 72
    target_h = max(2, int(target_h))
    w = int(round(width * target_h / height / 2.0) * 2)
    return max(2, w), target_h


# ---------------------------------------------------------------------------
# 图像指纹与距离
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS = {"mad": 0.04, "dhash": 8.0, "hist": 0.25}


def _nearest_resize(img: np.ndarray, h: int, w: int) -> np.ndarray:
    H, W = img.shape[:2]
    ys = np.clip(((np.arange(h) + 0.5) * H / h).astype(np.int64), 0, H - 1)
    xs = np.clip(((np.arange(w) + 0.5) * W / w).astype(np.int64), 0, W - 1)
    return img[ys][:, xs]


def _low_res(gray: np.ndarray, h: int = 9, w: int = 16) -> np.ndarray:
    """区域平均缩到 h×w，给「跨帧去重」用（比最近邻稳，不受噪点影响）。"""
    H, W = gray.shape[:2]
    if H < h or W < w:
        return _nearest_resize(gray, h, w).astype(np.float32)
    ys = np.linspace(0, H, h + 1).astype(np.int64)
    xs = np.linspace(0, W, w + 1).astype(np.int64)
    sums = np.add.reduceat(np.add.reduceat(gray.astype(np.float32), ys[:-1], axis=0), xs[:-1], axis=1)
    counts = np.outer(np.diff(ys), np.diff(xs)).astype(np.float32)
    return sums / counts


def _dhash(gray: np.ndarray) -> np.ndarray:
    """64 位 dHash：缩到 9x8，比较相邻像素亮度，抗压缩噪声/轻微曝光变化。"""
    small = _nearest_resize(gray, 8, 9)
    bits = (small[:, 1:] > small[:, :-1]).ravel()
    return np.packbits(bits)


def _hist32(gray: np.ndarray) -> np.ndarray:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    hist = hist.reshape(32, 8).sum(axis=1)
    total = hist.sum()
    return (hist / total if total else hist).astype(np.float32)


def _distance_mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32)))) / 255.0


def _distance_dhash(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.unpackbits(np.bitwise_xor(a, b)).sum())


def _distance_hist(a: np.ndarray, b: np.ndarray) -> float:
    return float(0.5 * np.sum((a - b) ** 2 / (a + b + 1e-12)))


FINGERPRINTS = {
    "mad": lambda gray: gray.astype(np.float32),
    "dhash": _dhash,
    "hist": _hist32,
}
DISTANCES = {"mad": _distance_mad, "dhash": _distance_dhash, "hist": _distance_hist}


@dataclass
class Cut:
    index: int
    time: float
    score: Optional[float] = None
    file: str = ""


class KeyframeSelector:
    """决定哪些帧成为关键帧。

    - 候选帧先与「上一个被保留的帧」比较，超过阈值才算“变了”；
    - 默认（dedupe="all"）还要与「已经保留的所有帧」比较，和任何一张太像就丢弃，
      这样 A→B→A 这类回头镜头只会保留第一个 A；
    - 被拒绝的帧不会更新参考帧，所以慢速渐变仍会累积到阈值后切出来。
    """

    def __init__(
        self,
        metric: str = "mad",
        threshold: Optional[float] = None,
        min_gap: float = 0.0,
        max_frames: int = 0,
        dedupe: str = "all",
        dupe_threshold: Optional[float] = None,
    ) -> None:
        if metric not in DISTANCES:
            raise ValueError(f"未知度量: {metric}")
        if dedupe not in ("all", "last"):
            raise ValueError(f"未知去重方式: {dedupe}")
        self.metric = metric
        self.threshold = float(threshold if threshold is not None else DEFAULT_THRESHOLDS[metric])
        self.min_gap = max(0.0, float(min_gap))
        self.max_frames = max(0, int(max_frames))
        self.dedupe = dedupe
        # 去重阈值：与任一已保留帧的差异小于它就算“同一张画面”，默认取判定阈值的一半
        self.dupe_threshold = float(dupe_threshold) if dupe_threshold is not None else self.threshold * 0.5
        self._fingerprint = FINGERPRINTS[metric]
        self._distance = DISTANCES[metric]
        self.last_fp: Optional[np.ndarray] = None
        self.last_time: float = 0.0
        self.count: int = 0
        self.duplicates: int = 0
        self.full: bool = False
        self._kept_dedupe: list[np.ndarray] = []

    def feed(self, t: float, gray: np.ndarray) -> tuple[bool, Optional[float]]:
        """返回 (是否保留, 与上一个保留帧的距离)。第一帧总是保留，score 为 None。"""
        if self.full:
            return False, None
        fp = self._fingerprint(gray)
        if self.last_fp is None:
            self._accept(t, fp, None, self._dedupe_fp(gray, fp))
            return True, None
        score = self._distance(fp, self.last_fp)
        if score >= self.threshold and (t - self.last_time) >= self.min_gap - 1e-9:
            dup_fp = self._dedupe_fp(gray, fp)
            if self.dedupe == "all" and self._is_duplicate(dup_fp):
                self.duplicates += 1
                return False, score
            if self.max_frames and self.count >= self.max_frames:
                self.full = True
                return False, score
            self._accept(t, fp, score, dup_fp)
            return True, score
        return False, score

    def _accept(self, t: float, fp: np.ndarray, score: Optional[float], dup_fp: np.ndarray) -> None:
        self.last_fp = fp
        self.last_time = float(t)
        self.count += 1
        self._kept_dedupe.append(dup_fp)

    # -- 跨帧去重 -----------------------------------------------------------
    def _dedupe_fp(self, gray: np.ndarray, fp: np.ndarray) -> np.ndarray:
        """去重用的紧凑指纹：mad 用 9x16 低分辨率图，其他度量直接复用原指纹。"""
        if self.metric == "mad":
            return _low_res(gray)
        return fp

    def _is_duplicate(self, cand: np.ndarray) -> bool:
        if not self._kept_dedupe:
            return False
        arr = np.stack(self._kept_dedupe)
        if self.metric == "mad":
            dist = np.abs(arr - cand).mean(axis=(1, 2)) / 255.0
        elif self.metric == "dhash":
            dist = np.unpackbits(arr ^ cand, axis=1).sum(axis=1).astype(np.float32)
        else:  # hist
            dist = 0.5 * np.sum((arr - cand) ** 2 / (arr + cand + 1e-12), axis=1)
        return bool(np.min(dist) < self.dupe_threshold)


# ---------------------------------------------------------------------------
# 解码后端
# ---------------------------------------------------------------------------


class FFmpegBackend:
    name = "ffmpeg"

    def __init__(self, cmd: Sequence[str]) -> None:
        self.cmd = [str(c) for c in cmd]
        self.last_scan_error = ""

    # -- 探测 ---------------------------------------------------------------
    def probe(self, src: Path) -> MediaInfo:
        return probe_ffmpeg(self.cmd, src)

    # -- 第一遍：低分辨率灰度流 ---------------------------------------------
    def scan(
        self,
        src: Path,
        sample_fps: float,
        size: tuple[int, int],
        start: float = 0.0,
        end: Optional[float] = None,
    ) -> Iterator[tuple[int, np.ndarray]]:
        W, H = size
        frame_bytes = W * H
        cmd = list(self.cmd) + ["-hide_banner", "-nostdin", "-v", "error"]
        if start > 0:
            cmd += ["-ss", f"{start:.3f}"]
        if end is not None:
            # 用输入侧的 -t（读多久）而不是 -to，避免与 -ss 组合时的解释歧义
            cmd += ["-t", f"{max(0.0, end - start):.3f}"]
        cmd += [
            "-i",
            str(src),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"fps={sample_fps:g},scale={W}:{H}:flags=area,format=gray",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ]
        errf = tempfile.TemporaryFile()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, creationflags=NO_WINDOW)
        index = 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes) if proc.stdout else b""
                if not buf or len(buf) < frame_bytes:
                    break
                yield index, np.frombuffer(buf, dtype=np.uint8).reshape(H, W)
                index += 1
        finally:
            if proc.stdout:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            try:
                errf.seek(0)
                self.last_scan_error = errf.read().decode("utf-8", "replace").strip()
            finally:
                errf.close()
        if index == 0 and self.last_scan_error:
            raise RuntimeError(f"ffmpeg 解码失败：{self.last_scan_error}")

    # -- 第二遍：导出单帧 ---------------------------------------------------
    def _frame_cmd(
        self,
        src: Path,
        t: float,
        out_path: Path,
        quality: int,
        out_width: Optional[int],
        seek_before_input: bool,
    ) -> list[str]:
        seek = ["-ss", f"{max(0.0, t):.3f}"]
        cmd = list(self.cmd) + ["-hide_banner", "-nostdin", "-v", "error", "-y"]
        cmd += (seek + ["-i", str(src)]) if seek_before_input else (["-i", str(src)] + seek)
        cmd += ["-frames:v", "1", "-an", "-sn", "-dn"]
        if out_width:
            cmd += ["-vf", f"scale={int(out_width)}:-2:flags=lanczos"]
        if out_path.suffix.lower() in (".jpg", ".jpeg"):
            # yuvj444p = 不做色度抽样，避免细边上出现彩色毛刺；q:v 1 为最高质量
            cmd += ["-pix_fmt", "yuvj444p", "-q:v", str(max(1, min(31, int(quality))))]
        cmd += ["-update", "1", str(out_path)]
        return cmd

    def extract(self, src: Path, t: float, out_path: Path, quality: int = 2, out_width: Optional[int] = None) -> None:
        # 先删掉可能残留的同名文件，避免把上一次的旧图当成成功
        try:
            out_path.unlink()
        except FileNotFoundError:
            pass
        proc: Optional[subprocess.CompletedProcess] = None
        for seek_before in (True, False):  # 先快速定位；失败再用精确（慢）定位
            cmd = self._frame_cmd(src, t, out_path, quality, out_width, seek_before)
            proc = _run(cmd)
            if out_path.exists() and out_path.stat().st_size > 0:
                return
        err = (proc.stderr or "").strip()[:300] if proc else ""
        raise RuntimeError(f"导出帧失败 t={timecode(t)}：{err}")


def jpeg_quality_cv2(q: int) -> int:
    """把 ffmpeg 风格的 -q:v（1 最好 / 31 最差）换算成 OpenCV 的 0-100 质量。"""
    q = max(1, min(31, int(q)))
    return int(round(97 - (q - 1) * 47 / 30.0))


class Cv2Backend:
    """没有 ffmpeg 时的备选：opencv-python，逐帧解码，慢一些但不需要装 ffmpeg。"""

    name = "cv2"

    def __init__(self) -> None:
        import cv2  # noqa: F401

        self.cv2 = cv2

    def probe(self, src: Path) -> MediaInfo:
        cap = self.cv2.VideoCapture(str(src))
        if not cap.isOpened():
            raise RuntimeError(f"OpenCV 无法打开视频：{src}")
        try:
            w = int(cap.get(self.cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(self.cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(self.cv2.CAP_PROP_FPS) or 25.0)
            frames = float(cap.get(self.cv2.CAP_PROP_FRAME_COUNT) or 0)
        finally:
            cap.release()
        return MediaInfo(src=src, width=w, height=h, fps=fps, duration=frames / fps if fps else 0.0)

    def scan(
        self,
        src: Path,
        sample_fps: float,
        size: tuple[int, int],
        start: float = 0.0,
        end: Optional[float] = None,
    ) -> Iterator[tuple[int, np.ndarray]]:
        W, H = size
        cap = self.cv2.VideoCapture(str(src))
        if not cap.isOpened():
            raise RuntimeError(f"OpenCV 无法打开视频：{src}")
        native_fps = float(cap.get(self.cv2.CAP_PROP_FPS) or 25.0)
        step = max(1, int(round(native_fps / sample_fps))) if sample_fps > 0 else 1
        index = 0
        frame_no = 0
        try:
            if start > 0:
                cap.set(self.cv2.CAP_PROP_POS_MSEC, start * 1000.0)
                frame_no = int(start * native_fps)
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                t = frame_no / native_fps if native_fps else 0.0
                frame_no += 1
                if end is not None and t > end:
                    break
                if (frame_no - 1) % step:
                    continue
                gray = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
                yield index, self.cv2.resize(gray, (W, H), interpolation=self.cv2.INTER_AREA)
                index += 1
        finally:
            cap.release()

    def extract(self, src: Path, t: float, out_path: Path, quality: int = 2, out_width: Optional[int] = None) -> None:
        cap = self.cv2.VideoCapture(str(src))
        if not cap.isOpened():
            raise RuntimeError(f"OpenCV 无法打开视频：{src}")
        try:
            cap.set(self.cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
            ok, frame = cap.read()
            if not ok:
                raise RuntimeError(f"导出帧失败 t={timecode(t)}")
            if out_width:
                h = int(round(frame.shape[0] * out_width / frame.shape[1]))
                frame = self.cv2.resize(frame, (int(out_width), h), interpolation=self.cv2.INTER_AREA)
            params = []
            if out_path.suffix.lower() in (".jpg", ".jpeg"):
                params = [int(self.cv2.IMWRITE_JPEG_QUALITY), jpeg_quality_cv2(quality)]
                sampling = getattr(self.cv2, "IMWRITE_JPEG_SAMPLING_FACTOR", None)
                factor = getattr(self.cv2, "IMWRITE_JPEG_SAMPLING_FACTOR_444", None)
                if sampling is not None and factor is not None:
                    params += [int(sampling), int(factor)]  # 4:4:4，避免彩边
            try:
                ok = self.cv2.imwrite(str(out_path), frame, params)
            except Exception:
                ok = self.cv2.imwrite(str(out_path), frame, params[:2])
            if not ok:
                raise RuntimeError(f"写入图片失败：{out_path}")
        finally:
            cap.release()


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------


def find_ffmpeg(explicit: Optional[str] = None) -> Optional[list[str]]:
    if explicit:
        parts = _split_cmd(explicit)
        if Path(parts[-1]).exists() or shutil.which(parts[-1]):
            return parts
        die(f"--ffmpeg 指定的命令不可用：{explicit}")
    env = os.environ.get("FLASH_CUT_FFMPEG")
    if env:
        return _split_cmd(env)
    # 依次找：exe/脚本同目录（便携版）→ PyInstaller 解包目录（--add-binary 打进去的）→ PATH
    bases = [Path(sys.executable).resolve().parent, Path(__file__).resolve().parent]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bases.append(Path(meipass))
    for base in bases:
        for name in ("ffmpeg.exe", "ffmpeg"):
            cand = base / name
            if cand.exists():
                return [str(cand)]
    found = shutil.which("ffmpeg")
    if found:
        return [found]
    try:
        import imageio_ffmpeg  # type: ignore

        return [imageio_ffmpeg.get_ffmpeg_exe()]
    except Exception:
        pass
    return None


def make_backend(args: argparse.Namespace):
    backend = args.backend
    if backend in ("auto", "ffmpeg"):
        cmd = find_ffmpeg(args.ffmpeg)
        if cmd:
            return FFmpegBackend(cmd)
        if backend == "ffmpeg":
            die("找不到 ffmpeg。安装方法见 README，或用 --ffmpeg 指定路径。")
    try:
        return Cv2Backend()
    except Exception:
        die(
            "找不到可用的解码器：既没有 ffmpeg，也没有 opencv-python。\n"
            "  Windows: winget install Gyan.FFmpeg   （或 scoop install ffmpeg）\n"
            "  之后重开终端，用 `ffmpeg -version` 验证；或用 --ffmpeg 指定 ffmpeg.exe 路径。"
        )


def check_env(explicit_ffmpeg: Optional[str] = None) -> None:
    print(f"flash_cut {__version__}")
    print(f"python     : {sys.version.split()[0]}  ({sys.executable})")
    print(f"numpy      : {np.__version__}")
    try:
        import PIL

        print(f"pillow     : {PIL.__version__}")
    except Exception:
        print("pillow     : 未安装（只影响 --sheet 拼图）")
    cmd = find_ffmpeg(explicit_ffmpeg)
    if cmd:
        r = _run(list(cmd) + ["-version"])
        first = (r.stdout or r.stderr).splitlines()
        print(f"ffmpeg     : {' '.join(cmd)}")
        print(f"             {first[0] if first else '无法执行'}")
    else:
        print("ffmpeg     : 未找到（推荐安装）")
    try:
        import cv2

        print(f"opencv     : {cv2.__version__}")
    except Exception:
        print("opencv     : 未安装")


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def frame_name(index: int, ext: str, prefix: str = "frame_") -> str:
    return f"{prefix}{index:05d}{ext}"


def make_contact_sheet(paths: Sequence[Path], out_path: Path, labels: Sequence[str], cols: int = 5, thumb_w: int = 320) -> None:
    from PIL import Image, ImageDraw

    if not paths:
        return
    thumbs = []
    for p, label in zip(paths, labels):
        im = Image.open(p).convert("RGB")
        h = max(1, int(im.height * thumb_w / im.width))
        im = im.resize((thumb_w, h), Image.LANCZOS)
        canvas = Image.new("RGB", (thumb_w, h + 22), (20, 20, 20))
        canvas.paste(im, (0, 0))
        ImageDraw.Draw(canvas).text((6, h + 5), label, fill=(230, 230, 230))
        thumbs.append(canvas)
    cols = max(1, min(cols, len(thumbs)))
    rows = math.ceil(len(thumbs) / cols)
    cell_w = max(t.width for t in thumbs)
    cell_h = max(t.height for t in thumbs)
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (0, 0, 0))
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % cols) * cell_w, (i // cols) * cell_h))
    sheet.save(out_path, quality=90)


def write_manifest(out_dir: Path, args: argparse.Namespace, info: MediaInfo, cuts: Sequence[Cut]) -> Path:
    meta = {
        "source": str(info.src),
        "generated_by": f"flash_cut {__version__}",
        "video": {
            "width": info.width,
            "height": info.height,
            "fps": round(info.fps, 3),
            "duration": round(info.duration, 3),
        },
        "params": {
            "sample_fps": args.sample_fps,
            "metric": args.metric,
            "threshold": args.threshold if args.threshold is not None else DEFAULT_THRESHOLDS[args.metric],
            "min_gap": args.min_gap,
            "scan_height": args.scan_height,
            "format": args.image_format,
            "prefix": args.prefix,
            "dedupe": args.dedupe,
            "dupe_threshold": args.dupe_threshold,
            "mode": "interval" if args.every is not None else "threshold",
            "every": args.every,
        },
        "frames": [
            {
                "index": c.index,
                "time": round(c.time, 3),
                "timecode": timecode(c.time),
                "score": None if c.score is None else round(float(c.score), 4),
                "file": c.file,
            }
            for c in cuts
        ],
    }
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "frames.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["index", "time_seconds", "timecode", "score", "file"])
        for c in cuts:
            writer.writerow([c.index, f"{c.time:.3f}", timecode(c.time), "" if c.score is None else f"{c.score:.4f}", c.file])
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flash_cut.py",
        description="把视频切成“与上一个保留关键帧不同”的一组关键帧。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python flash_cut.py movie.mp4                       # 默认：10fps 采样，mad 阈值 0.04
  python flash_cut.py movie.mp4 -o out --dry-run       # 只列出切点，不导出
  python flash_cut.py movie.mp4 --threshold 0.02       # 更敏感，切得更碎
  python flash_cut.py movie.mp4 --metric dhash --threshold 10
  python flash_cut.py movie.mp4 --sheet                # 顺带生成总览拼图
  python flash_cut.py --check                          # 查看依赖状态
""",
    )
    p.add_argument("video", nargs="?", help="输入视频文件，或包含视频的文件夹（批量）")
    p.add_argument("-o", "--out", help="输出目录（单文件）；文件夹输入时是输出根目录，每个视频一个子目录")
    p.add_argument("--recursive", action="store_true", help="输入是文件夹时，连同子目录一起扫描")
    p.add_argument("--sample-fps", type=float, default=10.0, help="采样帧率，默认 10；越快越不会漏掉闪切，也越慢")
    p.add_argument("--metric", choices=sorted(DISTANCES), default="mad", help="差异度量（默认 mad）")
    p.add_argument("--threshold", type=float, default=None, help="差异阈值；默认 mad=0.04 / dhash=8 / hist=0.25")
    p.add_argument("--min-gap", type=float, default=0.0, help="两个关键帧的最小间隔秒数，默认 0")
    p.add_argument("--dedupe", choices=["all", "last"], default="all", help="去重方式：all=与所有已保留帧比较（默认，相同画面只留一张）；last=只与上一帧比较")
    p.add_argument("--dupe-threshold", type=float, default=None, help="去重阈值，默认取阈值的一半；与任一已保留帧的差异小于它就不再导出")
    p.add_argument("--every", type=float, default=None, metavar="SECONDS", help="固定间隔模式：每 N 秒截一帧，不比较、不去重（阈值/度量参数失效）")
    p.add_argument("--scan-height", type=int, default=72, help="扫描用灰度图高度，默认 72（越大越灵敏也越慢）")
    p.add_argument("--start", type=float, default=0.0, help="只处理该时间之后的视频（秒）")
    p.add_argument("--end", type=float, default=None, help="只处理到该时间（秒）")
    p.add_argument("--max-frames", type=int, default=0, help="最多保留多少个关键帧，0=不限")
    p.add_argument("--format", choices=["jpg", "png"], default="jpg", dest="image_format", help="图片格式，默认 jpg")
    p.add_argument("--prefix", default="frame_", help="输出文件名前缀，默认 frame_")
    p.add_argument("--quality", type=int, default=1, help="jpg 质量（ffmpeg -q:v，1 最好 31 最差），默认 1")
    p.add_argument("--out-width", type=int, default=None, help="导出图片宽度（等比缩放）；默认原始尺寸")
    p.add_argument("--sheet", action="store_true", help="生成 contact_sheet.jpg 总览拼图（需要 Pillow）")
    p.add_argument("--dry-run", action="store_true", help="只扫描并打印切点，不写任何文件")
    p.add_argument("--dump-scores", metavar="CSV", help="把每个采样点的差异分数写入 CSV，便于调阈值")
    p.add_argument("--backend", choices=["auto", "ffmpeg", "cv2"], default="auto", help="解码后端，默认 auto")
    p.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径（或 'py shim.py' 这类命令）")
    p.add_argument("--check", action="store_true", help="打印环境依赖状态后退出")
    p.add_argument("-q", "--quiet", action="store_true", help="不打印进度")
    p.add_argument("--version", action="version", version=f"flash_cut {__version__}")
    return p


VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".m4v",
    ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".3gp",
}


def collect_videos(folder: Path, recursive: bool = False) -> list[Path]:
    """列出文件夹里的视频文件（文件夹输入模式用）。"""
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def _score_path(base: Optional[str], video: Optional[Path] = None) -> Optional[Path]:
    if not base:
        return None
    path = Path(base)
    if video is None:
        return path
    return path.with_name(f"{path.stem}_{video.stem}{path.suffix or '.csv'}")


def interval_times(
    duration: float,
    every: float,
    start: float = 0.0,
    end: Optional[float] = None,
    max_frames: int = 0,
) -> list[float]:
    """固定间隔模式下的采样时间点（秒）。"""
    if every <= 0:
        return []
    stop = min(end, duration) if end is not None else duration
    times: list[float] = []
    t = max(0.0, start)
    while t < stop - 1e-9:
        if max_frames and len(times) >= max_frames:
            break
        times.append(round(t, 3))
        t += every
    return times


def export_cuts(
    backend,
    args: argparse.Namespace,
    src: Path,
    info: MediaInfo,
    out_dir: Path,
    cuts: list[Cut],
    scores_path: Optional[Path] = None,
    duplicates: int = 0,
) -> int:
    """把 cuts 逐个导出为图片，并写清单，返回帧数。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = ".jpg" if args.image_format == "jpg" else ".png"
    out_w = args.out_width
    if out_w and info.width and out_w > info.width:
        log(f"[提示] --out-width {out_w} 超过源宽度 {info.width}，按原尺寸导出（放大只会更糊）")
        out_w = info.width
    if not args.quiet:
        log(f"导出   : {ext.lstrip('.')}，宽 {out_w or info.width}px，质量 {args.quality}")
    made: list[Path] = []
    total = len(cuts)
    for i, c in enumerate(cuts, 1):
        path = out_dir / frame_name(i, ext, args.prefix)
        backend.extract(src, c.time, path, args.quality, out_w)
        c.file = path.name
        made.append(path)
        if not args.quiet:
            print(f"\r  导出 {i}/{total}  {timecode(c.time)}", end="", file=sys.stderr, flush=True)
    if not args.quiet and cuts:
        print(file=sys.stderr)

    if args.sheet and made:
        try:
            make_contact_sheet(made, out_dir / "contact_sheet.jpg", [timecode(c.time) for c in cuts])
        except Exception as exc:  # Pillow 缺失不应让整件事失败
            log(f"[警告] 生成拼图失败：{exc}")

    manifest = write_manifest(out_dir, args, info, cuts)
    log("")
    log(f"完成：{len(cuts)} 个关键帧，输出目录 {out_dir}")
    if duplicates:
        log(f"      另有 {duplicates} 帧与已保留画面重复，已跳过")
    log(f"清单：{manifest.name} / frames.csv")
    if scores_path is not None:
        log(f"分数：{scores_path}（用 --dry-run 结合它挑阈值）")
    return len(cuts)


def process_video(
    backend,
    args: argparse.Namespace,
    src: Path,
    out_dir: Optional[Path],
    scores_path: Optional[Path],
) -> int:
    """处理单个视频，返回导出的关键帧数量。"""
    if args.every is not None:
        return process_interval(backend, args, src, out_dir, scores_path)
    info = backend.probe(src)
    W, H = choose_scan_size(info.width, info.height, args.scan_height)
    out_dir = out_dir or src.with_name(f"{src.stem}_frames")
    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLDS[args.metric]

    selector = KeyframeSelector(
        args.metric, args.threshold, args.min_gap, args.max_frames, args.dedupe, args.dupe_threshold
    )
    args.dupe_threshold = selector.dupe_threshold  # 记录生效值，方便写进 manifest

    if not args.quiet:
        log(f"输入   : {src}")
        log(f"视频   : {info.width}x{info.height} @ {info.fps:.3f}fps, 时长 {timecode(info.duration)}")
        log(f"后端   : {backend.name}；扫描 {W}x{H} 灰度 @ {args.sample_fps:g}fps")
        dedupe_txt = f"开（{selector.dupe_threshold:g}）" if args.dedupe == "all" else "关（只比上一帧）"
        log(f"度量   : {args.metric}，阈值 {threshold:g}；跨帧去重 {dedupe_txt}；最小间隔 {args.min_gap:g}s")

    cuts: list[Cut] = []
    scores_fh = scores_writer = None
    if scores_path is not None:
        if scores_path.parent and not scores_path.parent.exists():
            scores_path.parent.mkdir(parents=True, exist_ok=True)
        scores_fh = scores_path.open("w", newline="", encoding="utf-8")
        scores_writer = csv.writer(scores_fh)
        scores_writer.writerow(["index", "time_seconds", "distance", "kept"])

    progress_every = 25
    try:
        for k, frame in backend.scan(src, args.sample_fps, (W, H), args.start, args.end):
            t = args.start + k / args.sample_fps
            kept, score = selector.feed(t, frame)
            if scores_writer is not None:
                scores_writer.writerow([k, f"{t:.3f}", "" if score is None else f"{score:.5f}", int(kept)])
            if kept:
                cuts.append(Cut(len(cuts) + 1, t, score))
                if not args.quiet:
                    shown = "首个" if score is None else f"{score:.4f}"
                    log(f"  * {timecode(t)}  score={shown}")
                if args.max_frames and len(cuts) >= args.max_frames:
                    break
            elif not args.quiet and k % progress_every == 0:
                done = t
                total = info.duration or 0.0
                pct = f"{done / total * 100:5.1f}%" if total > 0 else "  ??? "
                print(f"\r  扫描 {timecode(done)} / {timecode(total)}  {pct}", end="", file=sys.stderr, flush=True)
    finally:
        if scores_fh is not None:
            scores_fh.close()

    if not args.quiet:
        print(file=sys.stderr)  # 收尾可能停在行中的进度行

    if args.dry_run:
        print(f"\n共 {len(cuts)} 个切点（{args.metric} 阈值 {threshold:g}）：")
        prev = None
        for c in cuts:
            gap = "" if prev is None else f"  (+{c.time - prev:.2f}s)"
            score = "首个" if c.score is None else f"{c.score:.4f}"
            print(f"  {c.index:4d}  {timecode(c.time)}  score={score}{gap}")
            prev = c.time
        if selector.duplicates:
            print(f"  （另有 {selector.duplicates} 帧与已保留画面重复，已跳过）")
        return len(cuts)

    return export_cuts(backend, args, src, info, out_dir, cuts, scores_path, selector.duplicates)


def process_interval(
    backend,
    args: argparse.Namespace,
    src: Path,
    out_dir: Optional[Path],
    scores_path: Optional[Path] = None,
) -> int:
    """固定间隔模式：每 every 秒取一帧，不做比较、不去重。"""
    info = backend.probe(src)
    out_dir = out_dir or src.with_name(f"{src.stem}_frames")
    every = float(args.every)
    if info.duration <= 0 and args.end is None:
        raise RuntimeError(f"无法获取视频时长，无法按固定间隔截取：{src}（可用 --end 指定结束时间）")

    times = interval_times(info.duration, every, args.start, args.end, args.max_frames)
    if not args.quiet:
        log(f"输入   : {src}")
        log(f"视频   : {info.width}x{info.height} @ {info.fps:.3f}fps, 时长 {timecode(info.duration)}")
        log(f"模式   : 固定间隔，每 {every:g}s 一帧，不比较、不去重（共 {len(times)} 帧）")

    cuts = [Cut(i + 1, t, None) for i, t in enumerate(times)]

    if args.dry_run:
        print(f"\n共 {len(cuts)} 个切点（固定间隔 {every:g}s）：")
        for c in cuts:
            print(f"  {c.index:4d}  {timecode(c.time)}  score=固定")
        return len(cuts)

    return export_cuts(backend, args, src, info, out_dir, cuts, scores_path)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        check_env(args.ffmpeg)
        return 0
    if not args.video:
        die("需要输入视频文件或文件夹，例如：python flash_cut.py movie.mp4")

    src = Path(args.video).expanduser()
    if not src.exists():
        die(f"文件或文件夹不存在：{src}")
    if args.sample_fps <= 0:
        die("--sample-fps 必须大于 0")
    if args.end is not None and args.end <= args.start:
        die("--end 必须大于 --start")
    if args.threshold is not None and args.threshold < 0:
        die("--threshold 不能为负数")
    if args.dupe_threshold is not None and args.dupe_threshold < 0:
        die("--dupe-threshold 不能为负数")
    if args.every is not None:
        if args.every <= 0:
            die("--every 必须大于 0")
        if args.threshold is not None or args.dupe_threshold is not None or args.min_gap > 0 or args.dedupe != "all" or args.dump_scores:
            log("[提示] --every 模式下阈值、去重、分数导出均不生效")

    backend = make_backend(args)

    # 文件夹输入：批量处理，每个视频一个子目录
    if src.is_dir():
        try:
            videos = collect_videos(src, args.recursive)
        except OSError as exc:
            die(f"无法读取文件夹：{src}（{exc}）")
        if not videos:
            die(f"文件夹里没有可处理的视频：{src}\n支持：{' '.join(sorted(VIDEO_EXTS))}")
        root = Path(args.out) if args.out else (src / "keyframes")
        if not args.quiet:
            scope = "含子目录" if args.recursive else "不含子目录"
            log(f"文件夹 : {src}")
            log(f"视频   : 共 {len(videos)} 个（{scope}）；输出根目录 {root}")

        total = 0
        failures: list[tuple[str, str]] = []
        for i, video in enumerate(videos, 1):
            if not args.quiet:
                log("")
                log(f"[{i}/{len(videos)}] {video.name}")
            try:
                total += process_video(
                    backend, args, video, root / video.stem, _score_path(args.dump_scores, video)
                )
            except Exception as exc:  # 单个视频失败不影响后面的
                failures.append((video.name, str(exc)))
                log(f"[失败] {video.name}: {exc}")

        log("")
        log(f"批量完成：{len(videos) - len(failures)}/{len(videos)} 个视频，共 {total} 个关键帧")
        if failures:
            for name, msg in failures:
                log(f"  - 失败：{name}: {msg}")
            return 0 if len(failures) < len(videos) else 1
        return 0

    process_video(backend, args, src, Path(args.out) if args.out else None, _score_path(args.dump_scores))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("已中断")
        raise SystemExit(130)
    except RuntimeError as exc:
        die(str(exc))
