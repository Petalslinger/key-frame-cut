#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash_gui.py —— flash_cut 的桌面界面（Tkinter，Python 自带，无需第三方 UI 库）。

用法：
    python flash_gui.py

界面做的事很简单：把输入视频、输出目录、文件名前缀、格式、阈值等参数收集起来，
拼成 `python flash_cut.py ...` 子进程调用，实时解析它的进度/切点输出。
所以界面和命令行行为完全一致，出错也能在日志里看到原始信息。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except Exception:  # Pillow 只用于缩略图，没有也能用
    HAS_PIL = False

FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ENGINE = APP_DIR / "flash_cut.py"
CONFIG_FILE = APP_DIR / "flash_gui_config.json"


def engine_cmd() -> list[str]:
    """打包后没有 flash_cut.py 文件，让 exe 自己带 --engine 参数再进来跑命令行。"""
    if FROZEN:
        return [sys.executable, "--engine"]
    return [sys.executable, str(ENGINE)]


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_TYPES = [
    ("视频文件", "*.mp4 *.mkv *.mov *.avi *.flv *.webm *.m4v *.ts *.wmv *.mpg *.mpeg"),
    ("所有文件", "*.*"),
]

DEDUPE_MAP = {"全部已保留帧": "all", "仅上一帧": "last"}

# 引擎输出（stderr 与 stdout 已合并，\r 被文本模式转成 \n）
RE_PROGRESS = re.compile(r"扫描\s+(\S+)\s*/\s*(\S+)\s+([\d.]+)\s*%")
RE_PROGRESS_NA = re.compile(r"扫描\s+(\S+)\s*/\s*(\S+)\s+\?\?\?")
RE_CUT_DRY = re.compile(r"^\s*(\d+)\s+(\d{2}:\d{2}:\d{2}\.\d{3})\s+score=(\S+)(?:\s+\(\+[\d.]+s\))?\s*$")
RE_CUT_RUN = re.compile(r"^\s*\*\s+(\S+)\s+score=(\S+)\s*$")
RE_DONE = re.compile(r"完成：(\d+) 个关键帧，输出目录\s+(.+)")
RE_TOTAL = re.compile(r"共\s*(\d+)\s*个切点")
RE_ERROR = re.compile(r"^\[错误\]\s*(.*)")


def _enable_dpi() -> None:
    """让窗口在高分屏上不糊。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程按 UTF-8 输出，父进程按 UTF-8 读
    env["PYTHONUNBUFFERED"] = "1"
    return env


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.queue: queue.Queue = queue.Queue()
        self._dry = False
        self._stopped = False
        self._thumbs: list = []
        self._out_done = ""

        self._make_vars()
        self._build_ui()
        self._load_config()
        self._maybe_open_arg()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._drain)

    def _maybe_open_arg(self) -> None:
        """支持把视频文件拖到 exe 图标上直接打开。"""
        for arg in sys.argv[1:]:
            if arg.startswith("-"):
                continue
            path = Path(arg)
            if path.is_file() or path.is_dir():
                resolved = path.resolve()
                self.v_video.set(str(resolved))
                if self.v_same.get() or not self.v_out.get().strip():
                    self.v_out.set(str(self._default_out(resolved)))
                break

    # ------------------------------------------------------------------ 变量
    def _make_vars(self) -> None:
        v = tk.StringVar
        self.v_video = v()
        self.v_out = v()
        self.v_prefix = v(value="frame_")
        self.v_fmt = v(value="jpg")
        self.v_quality = v(value="1")
        self.v_width = v()
        self.v_fps = v(value="10")
        self.v_metric = v(value="mad")
        self.v_thr = v(value="0.04")
        self.v_gap = v(value="0")
        self.v_scanh = v(value="72")
        self.v_max = v(value="0")
        self.v_start = v(value="0")
        self.v_end = v()
        self.v_dedupe = v(value="全部已保留帧")
        self.v_dupethr = v()
        self.v_ffmpeg = v()
        self.v_sheet = tk.BooleanVar(value=False)
        self.v_same = tk.BooleanVar(value=False)
        self.v_recursive = tk.BooleanVar(value=False)

    # ------------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        self.root.title("flash_cut — 视频关键帧提取")
        self.root.geometry("1000x760")
        self.root.minsize(880, 660)

        outer = ttk.Frame(self.root, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        self._build_io(outer)
        self._build_adv(outer)
        self._build_buttons(outer)
        self._build_progress(outer)
        self._build_notebook(outer)

    def _build_io(self, outer: ttk.Frame) -> None:
        io = ttk.LabelFrame(outer, text=" 输入 / 输出 ", padding=8)
        io.grid(row=0, column=0, sticky="ew")
        io.columnconfigure(1, weight=1)

        ttk.Label(io, text="视频/文件夹").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(io, textvariable=self.v_video).grid(row=0, column=1, sticky="ew", padx=6, pady=2)
        pick = ttk.Frame(io)
        pick.grid(row=0, column=2, sticky="w")
        ttk.Button(pick, text="选文件…", width=8, command=self.on_pick_video).pack(side="left")
        ttk.Button(pick, text="选文件夹…", width=10, command=self.on_pick_folder).pack(side="left", padx=(6, 0))
        ttk.Checkbutton(pick, text="含子目录", variable=self.v_recursive).pack(side="left", padx=(8, 0))

        ttk.Label(io, text="输出目录").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(io, textvariable=self.v_out).grid(row=1, column=1, sticky="ew", padx=6, pady=2)
        box = ttk.Frame(io)
        box.grid(row=1, column=2, sticky="w")
        ttk.Button(box, text="浏览…", width=8, command=self.on_pick_out).pack(side="left")
        ttk.Checkbutton(box, text="同视频目录", variable=self.v_same, command=self.on_same_dir).pack(side="left", padx=(6, 0))

        row = ttk.Frame(io)
        row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self._field(row, "文件名前缀", lambda p: ttk.Entry(p, textvariable=self.v_prefix, width=12))
        self._field(row, "格式", lambda p: ttk.Combobox(p, textvariable=self.v_fmt, values=("jpg", "png"), width=6, state="readonly"))
        self._field(row, "jpg 质量(1最好)", lambda p: ttk.Spinbox(p, from_=1, to=31, width=5, textvariable=self.v_quality))
        self._field(row, "宽度(px)", lambda p: ttk.Entry(p, textvariable=self.v_width, width=8))
        ttk.Label(row, text="留空 = 原始尺寸", foreground="#777").pack(side="left", padx=(4, 0))

    def _build_adv(self, outer: ttk.Frame) -> None:
        adv = ttk.LabelFrame(outer, text=" 高级参数 ", padding=8)
        adv.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        r1 = ttk.Frame(adv)
        r1.pack(fill="x")
        self._field(r1, "采样帧率", lambda p: ttk.Spinbox(p, from_=1, to=60, width=5, textvariable=self.v_fps))
        self._field(r1, "度量", lambda p: ttk.Combobox(p, textvariable=self.v_metric, values=("mad", "dhash", "hist"), width=7, state="readonly"))
        self._field(r1, "阈值", lambda p: ttk.Entry(p, textvariable=self.v_thr, width=8))
        ttk.Label(r1, text="mad≈0.04 / dhash≈8 / hist≈0.25", foreground="#777").pack(side="left")

        r2 = ttk.Frame(adv)
        r2.pack(fill="x", pady=(6, 0))
        self._field(r2, "最小间隔(秒)", lambda p: ttk.Entry(p, textvariable=self.v_gap, width=6))
        self._field(r2, "扫描高度", lambda p: ttk.Spinbox(p, from_=16, to=240, width=5, textvariable=self.v_scanh))
        self._field(r2, "最多帧数", lambda p: ttk.Entry(p, textvariable=self.v_max, width=6))
        self._field(r2, "起点(秒)", lambda p: ttk.Entry(p, textvariable=self.v_start, width=6))
        self._field(r2, "终点(秒)", lambda p: ttk.Entry(p, textvariable=self.v_end, width=6))
        ttk.Label(r2, text="最多帧数 0 = 不限", foreground="#777").pack(side="left")

        r3 = ttk.Frame(adv)
        r3.pack(fill="x", pady=(6, 0))
        self._field(r3, "去重", lambda p: ttk.Combobox(p, textvariable=self.v_dedupe, values=("全部已保留帧", "仅上一帧"), width=10, state="readonly"))
        self._field(r3, "去重阈值", lambda p: ttk.Entry(p, textvariable=self.v_dupethr, width=6))
        ttk.Label(r3, text="留空 = 阈值×0.5；与任何已保留帧太像就不再导出", foreground="#777").pack(side="left")

        r4 = ttk.Frame(adv)
        r4.pack(fill="x", pady=(6, 0))
        ttk.Label(r4, text="ffmpeg 路径").pack(side="left")
        ttk.Entry(r4, textvariable=self.v_ffmpeg).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(r4, text="浏览…", width=8, command=self.on_pick_ffmpeg).pack(side="left")
        ttk.Checkbutton(r4, text="生成总览拼图", variable=self.v_sheet).pack(side="left", padx=(10, 0))
        ttk.Label(r4, text="留空 = 自动查找", foreground="#777").pack(side="left", padx=(4, 0))

    def _build_buttons(self, outer: ttk.Frame) -> None:
        bar = ttk.Frame(outer)
        bar.grid(row=2, column=0, sticky="ew", pady=(10, 4))

        self.btn_dry = ttk.Button(bar, text="预览切点", command=self.on_dry)
        self.btn_run = ttk.Button(bar, text="开始导出", command=self.on_run)
        self.btn_stop = ttk.Button(bar, text="停止", command=self.on_stop, state="disabled")
        for w in (self.btn_dry, self.btn_run, self.btn_stop):
            w.pack(side="left")
        ttk.Button(bar, text="打开输出目录", command=self.on_open).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="环境检查", command=self.on_check).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="清空日志", command=self.clear_log).pack(side="right")

    def _build_progress(self, outer: ttk.Frame) -> None:
        pf = ttk.Frame(outer)
        pf.grid(row=3, column=0, sticky="ew", pady=(2, 6))
        pf.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb.grid(row=0, column=0, sticky="ew")
        self.status = ttk.Label(pf, text="就绪", width=34, anchor="w")
        self.status.grid(row=0, column=1, sticky="w", padx=(8, 0))

    def _build_notebook(self, outer: ttk.Frame) -> None:
        self.nb = ttk.Notebook(outer)
        self.nb.grid(row=4, column=0, sticky="nsew")

        # 日志
        self.tab_log = ttk.Frame(self.nb, padding=4)
        self.nb.add(self.tab_log, text="日志")
        self.log = tk.Text(self.tab_log, height=10, wrap="word", state="disabled")
        ys = ttk.Scrollbar(self.tab_log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        self.log.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")

        # 切点
        self.tab_cuts = ttk.Frame(self.nb, padding=4)
        self.nb.add(self.tab_cuts, text="切点")
        cols = ("idx", "tc", "sec", "score")
        self.tree = ttk.Treeview(self.tab_cuts, columns=cols, show="headings", height=10)
        for col, title, width in (("idx", "#", 50), ("tc", "时间码", 130), ("sec", "秒", 90), ("score", "分数", 100)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="center" if col != "tc" else "w")
        ys2 = ttk.Scrollbar(self.tab_cuts, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys2.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys2.pack(side="right", fill="y")

        # 缩略图
        self.tab_preview = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_preview, text="缩略图")
        self.thumb_frame = ttk.Frame(self.tab_preview)
        self.thumb_frame.pack(fill="both", expand=True)
        self.thumb_hint = ttk.Label(self.tab_preview, text="导出完成后在这里显示前 8 张关键帧", foreground="#777")
        self.thumb_hint.pack(anchor="w", pady=(6, 0))

    @staticmethod
    def _field(parent: ttk.Frame, label: str, factory) -> tk.Widget:
        box = ttk.Frame(parent)
        box.pack(side="left", padx=(0, 12))
        ttk.Label(box, text=label).pack(side="left")
        widget = factory(box)
        widget.pack(side="left", padx=(4, 0))
        return widget

    # ------------------------------------------------------------------ 选择
    def _initial_dir(self) -> str:
        video = self.v_video.get().strip().strip('"')
        if video:
            return str(Path(video).parent)
        out = self.v_out.get().strip().strip('"')
        if out:
            return out
        return str(Path.home())

    def on_pick_video(self) -> None:
        path = filedialog.askopenfilename(title="选择视频文件", filetypes=VIDEO_TYPES, initialdir=self._initial_dir())
        if not path:
            return
        self.v_video.set(path)
        if self.v_same.get() or not self.v_out.get().strip():
            self.v_out.set(str(self._default_out(Path(path))))

    def on_pick_folder(self) -> None:
        path = filedialog.askdirectory(title="选择视频文件夹", initialdir=self._initial_dir(), mustexist=True)
        if not path:
            return
        self.v_video.set(path)
        if self.v_same.get() or not self.v_out.get().strip():
            self.v_out.set(str(self._default_out(Path(path))))
        self.status.configure(text=f"文件夹模式：{Path(path).name}")

    @staticmethod
    def _default_out(path: Path) -> Path:
        """单文件 → <视频名>_frames；文件夹 → <文件夹>/keyframes。"""
        if path.is_dir():
            return path / "keyframes"
        return path.with_name(path.stem + "_frames")

    def on_pick_out(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录", initialdir=self._initial_dir(), mustexist=False)
        if path:
            self.v_same.set(False)
            self.v_out.set(path)

    def on_pick_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(title="选择 ffmpeg.exe", filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")])
        if path:
            self.v_ffmpeg.set(path)

    def on_same_dir(self) -> None:
        video = self.v_video.get().strip().strip('"')
        if not video:
            return
        if self.v_same.get():
            self.v_out.set(str(self._default_out(Path(video))))

    # ------------------------------------------------------------------ 命令
    def _resolve_out(self) -> str:
        out = self.v_out.get().strip().strip('"')
        if out:
            return out
        video = self.v_video.get().strip().strip('"')
        if video:
            return str(self._default_out(Path(video)))
        return ""

    @staticmethod
    def _opt(args: list[str], flag: str, var: tk.StringVar) -> None:
        value = var.get().strip()
        if value:
            args += [flag, value]

    def _build_args(self, dry: bool) -> list[str] | None:
        video = self.v_video.get().strip().strip('"')
        if not video:
            messagebox.showwarning("缺少输入", "请先选择要处理的视频文件。")
            return None
        if not Path(video).exists():
            messagebox.showerror("文件不存在", video)
            return None
        if not FROZEN and not ENGINE.exists():
            messagebox.showerror("缺少引擎", f"同目录下找不到 flash_cut.py：\n{ENGINE}")
            return None
        if self.v_same.get():
            self.v_out.set(str(self._default_out(Path(video))))

        args = engine_cmd() + [video]
        out = self.v_out.get().strip().strip('"')
        if out:
            args += ["-o", out]
        if self.v_prefix.get().strip():
            args += ["--prefix", self.v_prefix.get().strip()]
        args += ["--format", self.v_fmt.get() or "jpg"]
        self._opt(args, "--quality", self.v_quality)
        self._opt(args, "--out-width", self.v_width)
        self._opt(args, "--sample-fps", self.v_fps)
        args += ["--metric", self.v_metric.get() or "mad"]
        self._opt(args, "--threshold", self.v_thr)
        self._opt(args, "--min-gap", self.v_gap)
        self._opt(args, "--scan-height", self.v_scanh)
        self._opt(args, "--max-frames", self.v_max)
        self._opt(args, "--start", self.v_start)
        self._opt(args, "--end", self.v_end)
        args += ["--dedupe", DEDUPE_MAP.get(self.v_dedupe.get().strip(), "all")]
        self._opt(args, "--dupe-threshold", self.v_dupethr)
        if self.v_ffmpeg.get().strip():
            args += ["--ffmpeg", self.v_ffmpeg.get().strip()]
        if self.v_sheet.get():
            args.append("--sheet")
        if self.v_recursive.get():
            args.append("--recursive")
        if dry:
            args.append("--dry-run")
        return args

    # ------------------------------------------------------------------ 执行
    def on_dry(self) -> None:
        args = self._build_args(dry=True)
        if args:
            self._start(args, dry=True)

    def on_run(self) -> None:
        args = self._build_args(dry=False)
        if args:
            self._start(args, dry=False)

    def _start(self, args: list[str], dry: bool) -> None:
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("正在运行", "上一个任务还没结束。")
            return
        self._dry = dry
        self._stopped = False
        self._out_done = ""
        self._reset_results()
        self._set_busy(True)
        self.status.configure(text="运行中…")
        self._log("> " + " ".join(f'"{a}"' if " " in a else a for a in args) + "\n")
        self.thread = threading.Thread(target=self._worker, args=(args, dry), daemon=True)
        self.thread.start()

    def _worker(self, args: list[str], dry: bool) -> None:
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                creationflags=NO_WINDOW,
            )
            if self.proc.stdout is None:
                self.queue.put(("error", "无法读取子进程输出"))
                return
            for line in self.proc.stdout:
                self.queue.put(("line", line.rstrip("\r\n")))
            code = self.proc.wait()
            self.queue.put(("done", code, dry))
        except Exception as exc:  # 子进程起不来等
            self.queue.put(("error", str(exc)))

    def on_stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self._stopped = True
            self._log("\n… 正在停止\n")
            self.status.configure(text="正在停止…")
            self._kill_tree(self.proc)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen | None) -> None:
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    # ------------------------------------------------------------------ 轮询
    def _drain(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "line":
                    self._handle_line(item[1])
                elif kind == "done":
                    self._on_done(item[1], item[2])
                else:
                    self._on_error(item[1])
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._drain)
        except tk.TclError:  # 窗口已关闭
            pass

    def _handle_line(self, line: str) -> None:
        m = RE_PROGRESS.search(line)
        if m:
            self.pb.stop()
            self.pb.configure(mode="determinate")
            self.pb["value"] = float(m.group(3))
            self.status.configure(text=f"扫描 {m.group(1)} / {m.group(2)}")
            return

        m = RE_PROGRESS_NA.search(line)
        if m:
            self.pb.configure(mode="indeterminate")
            self.pb.start(12)
            self.status.configure(text=f"扫描 {m.group(1)}（时长未知）")
            return

        if self._dry:
            m = RE_CUT_DRY.match(line)
            if m:
                self._add_cut(m.group(2), m.group(3), int(m.group(1)))
                return
        else:
            m = RE_CUT_RUN.match(line)
            if m:
                self._add_cut(m.group(1), m.group(2))
                self._log(line + "\n")
                return

        m = RE_TOTAL.search(line)
        if m:
            self.status.configure(text=f"共 {m.group(1)} 个切点")
            self._log(line + "\n")
            return

        m = RE_DONE.search(line)
        if m:
            self._out_done = m.group(2).strip()
            self.status.configure(text=f"完成：{m.group(1)} 个关键帧")
            self._log(line + "\n")
            return

        m = RE_ERROR.match(line.strip())
        if m:
            self._log(line + "\n")
            return

        if line.strip():
            self._log(line + "\n")

    def _on_done(self, code: int, dry: bool) -> None:
        self.proc = None
        self._set_busy(False)
        try:
            self.pb.stop()
        except Exception:
            pass

        if self._stopped:
            self._stopped = False
            self.status.configure(text="已停止（已导出的图片保留）")
            return

        if code == 0:
            if dry:
                n = len(self.tree.get_children())
                self.status.configure(text=f"预览完成：{n} 个切点")
                self.nb.select(self.tab_cuts)
            else:
                self.pb.configure(mode="determinate")
                self.pb["value"] = 100
                target = self.v_video.get().strip().strip('"')
                self.status.configure(text="批量导出完成" if target and Path(target).is_dir() else "导出完成")
                self._load_thumbs()
                self.nb.select(self.tab_preview)
        else:
            self.status.configure(text=f"失败（退出码 {code}）")

    def _on_error(self, msg: str) -> None:
        self.proc = None
        self._set_busy(False)
        self.status.configure(text="启动失败")
        self._log(f"[错误] {msg}\n")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.btn_dry.configure(state=state)
        self.btn_run.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # ------------------------------------------------------------------ 结果
    def _add_cut(self, tc: str, score: str, idx: int | None = None) -> None:
        if idx is None:
            idx = len(self.tree.get_children()) + 1
        self.tree.insert("", "end", values=(idx, tc, f"{self._tc_to_sec(tc):.3f}", score))
        self.tree.yview_moveto(1)

    @staticmethod
    def _tc_to_sec(tc: str) -> float:
        try:
            h, m, s = tc.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception:
            return 0.0

    def _reset_results(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for w in self.thumb_frame.winfo_children():
            w.destroy()
        self._thumbs.clear()
        self.thumb_hint.configure(text="导出完成后在这里显示前 8 张关键帧")
        try:
            self.pb.stop()
            self.pb.configure(mode="determinate")
            self.pb["value"] = 0
        except Exception:
            pass

    def _load_thumbs(self) -> None:
        for w in self.thumb_frame.winfo_children():
            w.destroy()
        self._thumbs.clear()

        out_dir = self._out_done or self._resolve_out()
        prefix = self.v_prefix.get().strip() or "frame_"
        folder = Path(out_dir) if out_dir else None
        files: list[Path] = []
        if folder and folder.is_dir():
            files = sorted(
                p for p in folder.glob(prefix + "*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")
            )
        if not files:
            self.thumb_hint.configure(text="没有找到导出图片")
            return
        if not HAS_PIL:
            self.thumb_hint.configure(text=f"共 {len(files)} 张（未安装 Pillow，无法显示缩略图）")
            return

        cols = 4
        for i, path in enumerate(files[:8]):
            try:
                im = Image.open(path).convert("RGB")
                im.thumbnail((220, 130))
                photo = ImageTk.PhotoImage(im)
            except Exception:
                continue
            cell = ttk.Frame(self.thumb_frame)
            cell.grid(row=i // cols, column=i % cols, padx=4, pady=4, sticky="nw")
            label = ttk.Label(cell, image=photo)
            label.image = photo  # 必须保留引用，否则图会被回收
            label.pack()
            ttk.Label(cell, text=path.stem).pack()
            self._thumbs.append(photo)

        if len(files) > 8:
            self.thumb_hint.configure(text=f"共 {len(files)} 张，这里只显示前 8 张")
        else:
            self.thumb_hint.configure(text=f"共 {len(files)} 张")

    # ------------------------------------------------------------------ 工具
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def on_open(self) -> None:
        folder = self._out_done or self._resolve_out()
        if not folder or not Path(folder).exists():
            messagebox.showinfo("输出目录", "输出目录还不存在，先导出一次吧。")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def on_check(self) -> None:
        if not FROZEN and not ENGINE.exists():
            messagebox.showerror("缺少引擎", str(ENGINE))
            return
        try:
            r = subprocess.run(
                engine_cmd() + ["--check"],
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                creationflags=NO_WINDOW,
            )
            self._log((r.stdout or "") + (r.stderr or "") + "\n")
        except Exception as exc:
            self._log(f"[错误] 环境检查失败：{exc}\n")

    # ------------------------------------------------------------------ 配置
    def _config_vars(self) -> dict:
        return {
            "video": self.v_video,
            "out": self.v_out,
            "prefix": self.v_prefix,
            "fmt": self.v_fmt,
            "quality": self.v_quality,
            "width": self.v_width,
            "fps": self.v_fps,
            "metric": self.v_metric,
            "threshold": self.v_thr,
            "gap": self.v_gap,
            "scanh": self.v_scanh,
            "max": self.v_max,
            "start": self.v_start,
            "end": self.v_end,
            "dedupe": self.v_dedupe,
            "dupethr": self.v_dupethr,
            "ffmpeg": self.v_ffmpeg,
            "sheet": self.v_sheet,
            "same": self.v_same,
            "recursive": self.v_recursive,
        }

    def _load_config(self) -> None:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        for key, var in self._config_vars().items():
            if key in data:
                try:
                    var.set(data[key])
                except Exception:
                    pass
        geo = data.get("geometry")
        if isinstance(geo, str) and re.match(r"^\d+x\d+", geo):
            self.root.geometry(geo)

    def _save_config(self) -> None:
        data = {}
        for key, var in self._config_vars().items():
            try:
                data[key] = var.get()
            except Exception:
                pass
        data["geometry"] = self.root.geometry()
        try:
            CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("正在运行", "任务还在运行，确定要退出吗？"):
                return
            self._kill_tree(self.proc)
        self._save_config()
        self.root.destroy()


def main() -> int:
    # 打包后的 exe 既当界面，也当引擎：内部会用它自己再起一个 --engine 子进程
    if FROZEN and sys.argv[1:2] == ["--engine"]:
        import flash_cut

        try:
            return flash_cut.main(sys.argv[2:])
        except RuntimeError as exc:
            try:
                print(f"[错误] {exc}", file=sys.stderr)
            except Exception:
                pass
            return 2

    _enable_dpi()
    root = tk.Tk()
    try:
        import tkinter.font as tkfont

        if os.name == "nt":
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=9)
                except Exception:
                    pass
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
