# flash-cut

从视频中提取关键帧：首帧保留，之后每帧与上一个保留帧比较，差异超过阈值即切出。

默认启用跨帧去重，候选帧同时与全部已保留帧比较，同一画面不会重复导出。

## 特性

- **两遍扫描**：扫描阶段解码缩略灰度图，导出阶段读取原分辨率，兼顾速度与画质
- **三种度量**：`mad` 平均绝对差、`dhash` 感知哈希、`hist` 灰度直方图
- **跨帧去重**：A→B→A 只保留一张，去重阈值独立可调
- **文件夹批量**：每个视频一个子目录，单个失败不影响其余
- **图形界面**：Tkinter 实现，无第三方 UI 依赖
- **可追溯**：每次导出生成 `manifest.json` 与 `frames.csv`

## 环境要求

| 依赖 | 必需 | 说明 |
| --- | --- | --- |
| Python 3.9+ | 是 | |
| numpy | 是 | 差异计算 |
| ffmpeg | 二选一 | 推荐，解码与导出 |
| opencv-python | 二选一 | 无 ffmpeg 时兜底 |
| Pillow | 否 | 仅 `--sheet` 与界面缩略图 |

## 安装

```bash
pip install -r requirements.txt
```

ffmpeg 任选其一：

```bash
winget install Gyan.FFmpeg
scoop install ffmpeg
pip install imageio-ffmpeg          # 由脚本自动定位
```

也可用 `--ffmpeg D:\tools\ffmpeg.exe` 指定，或将 `ffmpeg.exe` 放在脚本同目录。

## 快速开始

```bash
python flash_cut.py --check                   # 检查依赖
python flash_cut.py input.mp4 --dry-run       # 仅列出切点
python flash_cut.py input.mp4 -o out          # 导出关键帧
python flash_cut.py input.mp4 -o out --sheet  # 附总览拼图
```

## 用法

### 单文件

```bash
python flash_cut.py input.mp4
python flash_cut.py input.mp4 -o out --sample-fps 25 --threshold 0.03
```

### 文件夹

```bash
python flash_cut.py D:\videos                 # 输出到 D:\videos\keyframes\<视频名>\
python flash_cut.py D:\videos -o D:\out       # 输出到 D:\out\<视频名>\
python flash_cut.py D:\videos --recursive     # 含子目录
```

- 仅处理视频扩展名（mp4/mkv/mov/avi/flv/webm/m4v/ts/m2ts/wmv/mpg/mpeg/3gp）
- 每个视频独立子目录，各自包含图片、`manifest.json`、`frames.csv`
- 单个视频失败只记录，不影响后续，结束时汇总 `批量完成：x/y`

### 图形界面

```bash
python flash_gui.py
```

Windows 下可双击 `run_gui.bat`。界面提供文件/文件夹选择、参数配置、实时进度、切点列表与缩略图，参数保存在同目录 `flash_gui_config.json`。

界面以子进程方式调用 `flash_cut.py`，行为与命令行一致。

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-o, --out` | `<视频名>_frames` / `<文件夹>/keyframes` | 输出目录；文件夹输入时为根目录 |
| `--recursive` | 关 | 文件夹输入时递归子目录 |
| `--sample-fps` | `10` | 采样帧率，越高越不易漏切 |
| `--metric` | `mad` | 差异度量：`mad` / `dhash` / `hist` |
| `--threshold` | `0.04` / `8` / `0.25` | 差异阈值 |
| `--dedupe` | `all` | `all` 与全部已保留帧比较；`last` 仅与上一帧 |
| `--dupe-threshold` | 阈值 × 0.5 | 去重阈值，低于此值视为同一画面 |
| `--min-gap` | `0` | 两个关键帧的最小间隔（秒） |
| `--scan-height` | `72` | 扫描灰度图高度 |
| `--start` / `--end` | 全片 | 处理区间（秒） |
| `--max-frames` | 不限 | 最多保留帧数，0 为不限 |
| `--format` | `jpg` | 图片格式 `jpg` / `png` |
| `--prefix` | `frame_` | 输出文件名前缀 |
| `--quality` | `1` | jpg 质量，`1` 最好 `31` 最差 |
| `--out-width` | 原始尺寸 | 导出宽度，超过源宽度时按原尺寸 |
| `--sheet` | 关 | 生成 `contact_sheet.jpg` |
| `--dry-run` | 关 | 只输出切点，不写文件 |
| `--dump-scores` | 关 | 导出每帧差异分数 CSV |
| `--backend` | `auto` | `ffmpeg` / `cv2` / `auto` |
| `--ffmpeg` | 自动查找 | ffmpeg 路径或命令 |
| `--check` | | 打印依赖状态 |
| `-q, --quiet` | | 不打印进度 |

阈值参考：常规剪辑 `mad 0.03~0.06`；长镜头、缓慢运镜 `0.02~0.03`；快速闪剪建议 `--sample-fps 25 --min-gap 0.15`。

调整流程：`--dry-run` 观察切点数量与位置；偏差较大时用 `--dump-scores` 导出分数，将阈值置于目标切换与噪声之间。

## 输出

```
out/
├── frame_00001.jpg
├── frame_00002.jpg
├── ...
├── manifest.json
├── frames.csv
└── contact_sheet.jpg     # 可选
```

`manifest.json` 记录源信息、生效参数与每个关键帧的时间、分数、文件名：

```json
{
  "video": { "width": 1920, "height": 1080, "fps": 29.97, "duration": 61.2 },
  "params": { "sample_fps": 10.0, "metric": "mad", "threshold": 0.04, "dedupe": "all", "dupe_threshold": 0.02 },
  "frames": [
    { "index": 1, "time": 0.0, "timecode": "00:00:00.000", "score": null, "file": "frame_00001.jpg" },
    { "index": 2, "time": 3.2, "timecode": "00:00:03.200", "score": 0.3187, "file": "frame_00002.jpg" }
  ]
}
```

`score` 为该帧与上一个保留帧的差异，用于评估阈值。

## 原理

```python
kept = []
for t, frame in 采样帧(10fps, 128x72 灰度):
    if not kept or 差异(frame, kept[-1]) >= 阈值:
        if 与 kept 中任一帧的差异 >= 去重阈值:   # 首帧恒真
            记录(t); kept.append(frame)
```

被拒绝的帧不更新参考帧，因此缓慢变化会持续累积直至超过阈值。

不使用 `ffmpeg -vf select='gt(scene,0.3)'`：场景分数比较相邻帧，缓慢运镜或渐变难以触发；此处以保留帧为参考，可累积差异。

## 限制

- **HDR / 10bit**：扫描转换为 8bit 灰度比较，导出 jpg 可能偏灰；需保留原始色彩时使用 `--format png`
- **VFR**：按目标帧率重采样，时间点以 ffmpeg 输出为准
- **定位精度**：导出帧与记录时间可能相差 ±1 帧
- **去重取舍**：`--dedupe all` 会丢弃回头镜头，需要保留时改用 `--dedupe last`
- **性能**：第一遍为全片解码，受限于 CPU 与源分辨率

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用 `tests/fake_ffmpeg.py` 模拟解码，不依赖真实 ffmpeg 或视频文件。覆盖阈值判定、跨帧去重、`min-gap`、`max-frames`、度量距离、探测解析、端到端导出、批量处理与界面模块导入。

## 打包（可选）

```bash
pyinstaller --noconfirm --clean --onefile --windowed --name FlashCut --hidden-import flash_cut flash_gui.py
```

ffmpeg 不随包分发，将 `ffmpeg.exe` 置于 exe 同目录即可；配置文件写在 exe 同目录，避免放在 `C:\Program Files` 等无写权限路径。

## 许可

[MIT](LICENSE)。变更记录见 [CHANGELOG.md](CHANGELOG.md)，开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。
