# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-11-16

首个可用版本：把视频按「与上一个保留关键帧不同」切成一组关键帧。

### 新增

- **两遍扫描**：第一遍用 ffmpeg 把视频缩成灰度小图走管道比较，第二遍只对命中时间点导出原始分辨率图片
- **三种差异度量**：`mad`（平均绝对差）、`dhash`（感知哈希）、`hist`（灰度直方图），阈值独立默认
- **跨帧去重**：`--dedupe all` 与所有已保留帧比较，A→B→A 只保留一张；`--dupe-threshold` 控制「多像算重复」
- **节奏控制**：`--min-gap`、`--max-frames`、`--start/--end`
- **文件夹批量**：输入目录，每个视频一个子目录；`--recursive` 递归；单个失败不影响其余
- **图形界面** `flash_gui.py`：参数面板、实时进度、切点表、缩略图、环境检查、配置记忆
- **输出清单**：`manifest.json` + `frames.csv`，记录时间码、分数和生效参数
- 可选联系人拼图 `--sheet`；只预览不导出的 `--dry-run`；调阈值用的 `--dump-scores`

### 质量

- jpg 固定 4:4:4 编码并默认 `-q:v 1`，消除细边彩边与块状
- `--out-width` 超过源宽度时按原尺寸导出，不放大
- Windows 控制台按 GBK 兜底，非 GBK 字符降级为替换符而不是崩溃
- 所有 ffmpeg 子进程隐藏黑框，便于 GUI 场景

### 打包

- 源码里已支持 PyInstaller 冻结（`--engine` 自调用、`sys._MEIPASS` 查找 ffmpeg），但**不随仓库发布二进制**
