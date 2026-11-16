#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的“假 ffmpeg”：不真正解码，按约定生成合成画面，用来验证 flash_cut 的流程。

约定：默认 10 秒、640x360、25fps，画面每 2 秒整体变一次（灰度底色 + 轻微运动亮条）。
环境变量可覆盖：FAKE_DURATION / FAKE_SEGMENT / FAKE_WIDTH / FAKE_HEIGHT / FAKE_FPS。
"""

import os
import re
import sys

import numpy as np


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


DURATION = _env_f("FAKE_DURATION", 10.0)
SEGMENT = max(0.1, _env_f("FAKE_SEGMENT", 2.0))
W0 = int(_env_f("FAKE_WIDTH", 640))
H0 = int(_env_f("FAKE_HEIGHT", 360))
FPS = _env_f("FAKE_FPS", 25.0)

_FLAGS_WITH_VALUE = {
    "-v", "-loglevel", "-ss", "-to", "-i", "-vf", "-f", "-pix_fmt", "-q:v",
    "-frames:v", "-update", "-t", "-c", "-c:v", "-c:a", "-b:a", "-preset",
    "-crf", "-movflags", "-map", "-r", "-b", "-filter_complex", "-threads",
    "-g", "-ar", "-ac", "-s", "-bufsize", "-max_muxing_queue_size",
}


def frame_at(t: float, w: int, h: int) -> np.ndarray:
    seg = int(t // SEGMENT) % 6
    base = (40 + 50 * seg) % 256
    img = np.full((h, w), base, np.uint8)
    x0 = int((t * 30.0) % max(w, 1))
    lo, hi = max(0, x0 - 3), min(w, x0 + 3)
    if hi > lo:  # 轻微运动，差异远低于阈值：用于验证相近帧不会被重复保留
        img[:, lo:hi] = np.minimum(img[:, lo:hi].astype(np.int16) + 10, 255).astype(np.uint8)
    return img


def parse(argv):
    o = {
        "inputs": [],
        "output": None,
        "ss": None,
        "to": None,
        "t": None,
        "vf": "",
        "raw": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-":
            o["output"] = "-"
            i += 1
            continue
        if a == "-i":
            o["inputs"].append(argv[i + 1])
            i += 2
            continue
        if a in ("-ss", "-to", "-t"):
            key = {"-ss": "ss", "-to": "to", "-t": "t"}[a]
            o[key] = float(argv[i + 1])
            i += 2
            continue
        if a == "-vf":
            o["vf"] = argv[i + 1]
            i += 2
            continue
        if a == "-f":
            if argv[i + 1] == "rawvideo":
                o["raw"] = True
            i += 2
            continue
        if a in _FLAGS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        o["output"] = a
        i += 1
    return o


def probe_text() -> str:
    hh = int(DURATION // 3600)
    mm = int(DURATION % 3600 // 60)
    ss = DURATION % 60
    return (
        "ffmpeg version 6.0-fake\n"
        "  built with fake decoder\n"
        f"  Duration: {hh:02d}:{mm:02d}:{ss:05.2f}, start: 0.000000, bitrate: 500 kb/s\n"
        f"  Stream #0:0: Video: h264 (High), yuv420p, {W0}x{H0} [SAR 1:1 DAR 16:9], "
        f"{FPS:g} fps, {FPS:g} tbr, 90k tbn, 50 tbc\n"
        "  Stream #0:1: Audio: aac, 48000 Hz, stereo, fltp, 128 kb/s\n"
    )


def emit_raw(o) -> int:
    m = re.search(r"fps=([\d.]+)", o["vf"])
    fps = float(m.group(1)) if m else 10.0
    sm = re.search(r"scale=(\d+):(\d+)", o["vf"])
    w, h = (int(sm.group(1)), int(sm.group(2))) if sm else (W0, H0)
    start = o["ss"] or 0.0
    if o["to"] is not None:
        end = o["to"]
    elif o["t"] is not None:
        end = start + o["t"]
    else:
        end = DURATION
    n = int(max(0.0, min(end, DURATION) - start) * fps)
    chunks = []
    for k in range(n):
        chunks.append(frame_at(start + k / fps, w, h).tobytes())
    sys.stdout.buffer.write(b"".join(chunks))
    return 0


def emit_file(o) -> int:
    out = o["output"]
    ext = os.path.splitext(out)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        t = o["ss"] or 0.0
        if t >= DURATION:
            return 1
        from PIL import Image

        Image.fromarray(frame_at(t, W0, H0)).save(out)
        return 0
    if ext in (".mp4", ".mkv", ".mov", ".webm"):
        with open(out, "wb") as fh:
            fh.write(b"FAKEVIDEO" * 64)
        return 0
    sys.stderr.write(f"fake ffmpeg: 不支持的输出 {out}\n")
    return 2


def main(argv) -> int:
    o = parse(argv)
    if not o["inputs"]:
        if o["output"] == "-":
            sys.stderr.write(probe_text())
            return 1
        sys.stderr.write("fake ffmpeg: 缺少输入\n")
        return 2
    if o["raw"]:
        return emit_raw(o)
    if o["output"] and o["output"] != "-":
        return emit_file(o)
    sys.stderr.write(probe_text())
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BrokenPipeError:
        raise SystemExit(0)
