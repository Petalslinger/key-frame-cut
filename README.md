# flash_cut —— 按「与上一个关键帧不同」把视频切成一组关键帧

输入一个视频，输出一组图片：**第一帧永远保留，之后每一帧都只和「上一个被保留的帧」比较，差异超过阈值才切出来**。慢速推镜、渐变、缓慢转场会相对参考帧不断累积差异，累计够了照样会被切出来。

```
输入 movie.mp4
   │
   ├─ 第一遍：ffmpeg 把视频缩成 128x72 灰度小图（10fps）→ 管道 → Python
   │            每帧与「上一个保留帧」比差异，超过阈值 → 记下时间点
   │
   └─ 第二遍：对每个时间点用 ffmpeg 定位，导出原始分辨率图片
```

## 快速开始

```bash
# 0) 看依赖（有没有 ffmpeg / numpy）
python flash_cut.py --check

# 1) 先只看切点，不写文件（强烈建议第一步这样跑）
python flash_cut.py movie.mp4 --dry-run

# 2) 满意后正式导出 frame_00001.jpg ...
python flash_cut.py movie.mp4 -o out

# 3) 导出 + 一张总览拼图
python flash_cut.py movie.mp4 -o out --sheet
```

```bash
pip install -r requirements.txt   # 真正必需的只有 numpy
```

依赖：

| 依赖 | 是否必需 | 说明 |
| --- | --- | --- |
| Python 3.9+、numpy | 必需 | 比较差异用 |
| ffmpeg（推荐） | 二选一 | 解码 + 导出，最快最稳 |
| opencv-python | 二选一 | 没 ffmpeg 时兜底，逐帧解码，慢一些 |
| Pillow | 可选 | 只用于 `--sheet` 总览拼图 |

装 ffmpeg（任选其一）：

```bash
winget install Gyan.FFmpeg        # Windows，装完重开终端
scoop install ffmpeg              # 有 scoop 的话
pip install imageio-ffmpeg        # 不想装系统包，脚本会自动找到它
# 或者把 ffmpeg.exe 放到任意目录，用 --ffmpeg D:\tools\ffmpeg.exe 指定
```

## 为什么不是一行 `select='gt(scene,0.3)'`

ffmpeg 自带的场景检测（`select='gt(scene,0.3)'`）比较的是**相邻两帧**：画面缓慢变化时相邻帧永远“很像”，于是一个 20 秒的慢推镜只会被切一刀；而本脚本比较的是**与上一个保留帧**，差异会累积，慢变化也能在合适的位置切出来——这才是「不断切割与上一个关键帧不同的关键帧」。

核心循环就这几行（见 `KeyframeSelector`）：

```python
kept = []
for t, frame in 采样帧(10fps, 128x72 灰度):
    if not kept or 差异(frame, kept[-1]) >= 阈值:      # 和上一张比，变了
        if 和 kept 里的任何一张都明显不同:              # --dedupe all，默认开
            记录(t); kept.append(frame)
```

被拒绝的帧**不会**更新参考帧，这是能捕捉渐变的关键；同时因为要和 `kept` 里所有帧比一遍，A→B→A 这种回头镜头只会留下第一个 A，不会重复导出同一画面（`--dedupe last` 可关掉这一步）。

## 图形界面

```bash
python flash_gui.py
```

Windows 下也可以直接双击 `run_gui.bat`（它会自动找 `py -3` 或 `python`）。

窗口里能改的：

- **输入**：视频文件，或整个文件夹（「选文件夹…」，可勾「含子目录」批量处理）
- **输出**：输出目录（浏览选择，或勾「同视频目录」自动填 `<视频名>_frames`）、文件名前缀、jpg/png、jpg 质量、导出宽度
- **高级**：采样帧率、度量、阈值、去重方式（全部已保留帧 / 仅上一帧）、去重阈值、最小间隔、扫描高度、最多帧数、起止时间、ffmpeg 路径、生成总览拼图
- **按钮**：预览切点（只扫描、往表格里列切点，不写图片）/ 开始导出 / 停止 / 打开输出目录 / 环境检查

底部是进度条、日志、切点表，导出完成后「缩略图」标签页会显示前 8 张结果。参数会记在 `flash_gui_config.json`，下次打开自动还原。

界面不是另一套实现：它把上面这些控件拼成 `python flash_cut.py ...` 子进程调用，再实时解析输出。所以命令行和界面行为一致，出问题日志里能看到原始命令和报错。

依赖：Tkinter（Windows 官方 Python 自带）、Pillow（只用于缩略图，没有也能用）。

## 批量：输入一个文件夹

输入也可以是一个文件夹，里面每个视频单独处理：

```bash
python flash_cut.py D:\videos                 # 输出到 D:\videos\keyframes\<视频名>\
python flash_cut.py D:\videos -o D:\out       # 输出到 D:\out\<视频名>\
python flash_cut.py D:\videos --recursive     # 连子目录一起扫
```

- 只认视频扩展名（mp4/mkv/mov/avi/flv/webm/m4v/ts/m2ts/wmv/mpg/mpeg/3gp），其它文件忽略；
- 每个视频一个子目录，各自带 `frame_*.jpg` 和 `manifest.json`；
- 单个视频失败（文件损坏、无权限）只记一条，不影响后面的，最后汇总 `批量完成：x/y`；
- `--dump-scores` 在批量下会按 `<名字>_<视频名>.csv` 分开存，不会互相覆盖；
- 界面里点「选文件夹…」即可，「含子目录」勾上等于 `--recursive`。

## 输出

```
out/
├── frame_00001.jpg      # 第 1 个关键帧（视频首帧）
├── frame_00002.jpg
├── ...
├── manifest.json        # 全部时间点 + 分数 + 参数
├── frames.csv           # 同上，表格版
└── contact_sheet.jpg    # 可选，--sheet
```

文件名前缀默认 `frame_`，用 `--prefix shot_`（或界面里的「文件名前缀」）就能改成 `shot_00001.jpg`。

`manifest.json` 示例：

```json
{
  "source": "movie.mp4",
  "video": { "width": 1920, "height": 1080, "fps": 29.97, "duration": 61.2 },
  "params": { "sample_fps": 10.0, "metric": "mad", "threshold": 0.04, "min_gap": 0.0, "scan_height": 72, "format": "jpg", "prefix": "frame_", "dedupe": "all", "dupe_threshold": 0.02 },
  "frames": [
    { "index": 1, "time": 0.0, "timecode": "00:00:00.000", "score": null, "file": "frame_00001.jpg" },
    { "index": 2, "time": 3.2, "timecode": "00:00:03.200", "score": 0.3187, "file": "frame_00002.jpg" }
  ]
}
```

`score` 是「该帧与上一个保留帧的差异」，调阈值时看这个数最直观。

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-o, --out` | `<视频名>_frames` / `<文件夹>/keyframes` | 输出目录；文件夹输入时是根目录，每个视频一个子目录 |
| `--recursive` | — | 文件夹输入时递归子目录 |
| `--sample-fps` | `10` | 采样帧率。闪切很快的片子用 25/30；只求大切换用 2~4 |
| `--metric` | `mad` | `mad` 平均绝对差 / `dhash` 感知哈希 / `hist` 直方图 |
| `--threshold` | `0.04` / `8` / `0.25` | 阈值，见下表 |
| `--min-gap` | `0` | 两个关键帧最小间隔（秒）。防止同一处切换连出好几张 |
| `--dedupe` | `all` | `all`=与所有已保留帧比较，相同画面只留一张；`last`=只与上一帧比较（旧行为） |
| `--dupe-threshold` | 阈值×0.5 | 去重阈值：与任一已保留帧的差异小于它就算重复，不再导出 |
| `--scan-height` | `72` | 扫描灰度图高度；越大越灵敏也越慢 |
| `--start` / `--end` | 全片 | 只处理某一段（秒） |
| `--max-frames` | 不限 | 最多保留几张 |
| `--format` | `jpg` | 图片格式 `jpg`/`png` |
| `--prefix` | `frame_` | 输出文件名前缀，如 `shot_` → `shot_00001.jpg` |
| `--quality` | `1` | jpg 质量（ffmpeg `-q:v`，1 最好 31 最差）。jpg 固定按 4:4:4 编码，不做色度抽样 |
| `--out-width` | 原始尺寸 | 导出图宽度，等比缩放；超过源宽度会按原宽度导出，不放大 |
| `--sheet` | — | 生成 `contact_sheet.jpg` 总览 |
| `--dry-run` | — | 只打印切点，不写文件 |
| `--dump-scores CSV` | — | 每个采样点的差异分数，调阈值用 |
| `--backend` | `auto` | `ffmpeg` / `cv2` / `auto` |
| `--ffmpeg` | 自动查找 | 指定 ffmpeg 路径 |
| `--check` | — | 打印依赖状态 |

三种度量的量纲不同，默认阈值：

| metric | 范围 | 默认阈值 | 适合 |
| --- | --- | --- | --- |
| `mad` | 0..1（平均像素差/255） | `0.04` | 通用，最快，对整体亮度/内容变化敏感 |
| `dhash` | 0..64（汉明距离，bit） | `8` | 抗压缩噪声、轻微曝光/对焦抖动，但对“同一构图换颜色”不敏感 |
| `hist` | 0..1（卡方距离） | `0.25` | 只看颜色/亮度分布，画面内部换位置也能过 |

## 阈值怎么调（三步）

1. `python flash_cut.py movie.mp4 --dry-run` 看切出来的数量和位置对不对。
2. 不对就导出分数：`python flash_cut.py movie.mp4 --dry-run --dump-scores scores.csv`，看 `kept=1` 附近那些 `distance` 是多少，再把阈值定在「想要的切换」之上、「不想要的抖动」之下。
3. 切得太碎 → 调大阈值 / 加 `--min-gap 0.5` / 降 `--sample-fps`；漏切 → 调小阈值 / 提高 `--sample-fps` 到 25。

经验值：正常剪辑的片子 `mad 0.03~0.06`；全片长镜头、缓慢运镜 `0.02~0.03`；快速闪剪（每 0.2 秒一刀）用 `--sample-fps 25 --min-gap 0.15`。

## 常见坑

- **画面里有台标/水印/跑马灯**：它们持续在动，会把 `mad` 分数垫高或不断触发切换，可用 `--scan-height 36` 降低灵敏度，或改用 `dhash`。
- **同一镜头反复出现（A→B→A）**：默认把第二个 A 当重复丢掉。想保留就 `--dedupe last`，或把 `--dupe-threshold` 调大/调小控制“多像才算同一张”。
- **渐变/淡入淡出**：会在一段区间内连续出好几张，用 `--min-gap` 拉开。
- **`-ss` 定位精度**：导出的图可能比记录时间差 ±1 帧，正常现象。
- **导出图有彩边/块状**：jpg 现在默认 `-q:v 1` + `yuvj444p`（不抽样色度），还嫌不够就直接 `--format png` 出无损图。注意界面里的「缩略图」标签只是 220px 预览，看图请打开输出目录里的原图。
- **HDR / 10bit 素材**：扫描时 ffmpeg 会转成 8bit 灰度比较，导出 jpg 也可能偏灰；需要保留原始色彩时用 `--format png` 并自行处理色彩空间。
- **竖屏/带旋转元数据的手机视频**：脚本会读 rotation 并交换宽高，正常情况无需干预。
- **VFR（可变帧率）视频**：采样按目标帧率重排，时间点以 ffmpeg 输出为准。

## 打包成 exe（Windows）

```bash
pip install pyinstaller

# 单文件：dist/FlashCut.exe，双击即用（约 40–70MB，启动 1–3 秒）
pyinstaller --noconfirm --clean --onefile --windowed --name FlashCut --hidden-import flash_cut flash_gui.py

# 目录版：启动更快、杀软误报少，分发 dist/FlashCut/ 整个文件夹
pyinstaller --noconfirm --clean --onedir --windowed --name FlashCut --hidden-import flash_cut flash_gui.py
```

要图标就加 `--icon app.ico`。

几点说明：

- **ffmpeg 默认不打包**（体积 + 许可证）。把 `ffmpeg.exe`（想更快定位再加 `ffprobe.exe`）放到 `FlashCut.exe` 同目录，程序会自动用；也可以在界面的「ffmpeg 路径」里指定。
- 如果就是要单文件分发，也可以把它塞进去（程序会从 `sys._MEIPASS` 里找）：

```bash
pyinstaller --noconfirm --clean --onefile --windowed --name FlashCut --hidden-import flash_cut --add-binary "ffmpeg.exe;." flash_gui.py
```

  代价：完整版 ffmpeg 约 80–120MB，单文件会变成 150MB+，而且 onefile 每次启动都要解包，冷启动会到 5 秒以上。想控制体积就用 `--onedir`，或者自己编一个只留 decoder 的精简版（能压到 20MB 左右）。
- 打包后 exe 会**自己再起一个 `--engine` 子进程**跑命令行引擎，所以进度条、停止、日志都和源码版一致，不需要额外的 `flash_cut.exe`。
- 配置文件写在 exe 同目录（`flash_gui_config.json`）。**别放在 `C:\Program Files`** 这类无写权限的目录，放 D 盘或桌面即可。
- 支持把视频文件拖到 exe 图标上直接打开。
- `--windowed` 版没有控制台，CLI 输出只在界面日志里可见；想要独立命令行程序可以再打一个：

```bash
pyinstaller --noconfirm --clean --onefile --console --name flash-cut flash_cut.py
```

## 自测（不需要真 ffmpeg）

`tests/fake_ffmpeg.py` 是一个假 ffmpeg，按约定生成「每 2 秒换一次画面」的合成视频，所以逻辑可以在没有 ffmpeg 的机器上验证：

```bash
python -m unittest discover -s tests -v
```

覆盖：首帧必留、相近帧拒绝、慢变化累计、跨帧去重（A→B→A）、`min-gap` 限流、`max-frames`、dhash/hist 距离、ffmpeg 信息解析、端到端导出 + 清单、文件夹批量（含递归）、界面模块可导入（不开窗）。

## 文件

- `flash_cut.py` —— 主脚本（可直接当库用：`KeyframeSelector`、`FFmpegBackend`）
- `flash_gui.py` —— 图形界面（Tkinter，拼命令行调用主脚本）
- `run_gui.bat` —— Windows 双击启动界面
- `tests/fake_ffmpeg.py` —— 测试用假 ffmpeg
- `tests/test_flash_cut.py` —— 测试

## 仓库

- 许可证：[MIT](LICENSE)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 开发说明：[CONTRIBUTING.md](CONTRIBUTING.md)（改日志/改参数前先看那四条约束）
- CI：[.github/workflows/ci.yml](.github/workflows/ci.yml)，Windows + Ubuntu、Python 3.9/3.12，跑假 ffmpeg 测试
