# 变更记录

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-11-16

### 新增

- 两遍扫描：扫描阶段解码缩略灰度图，导出阶段读取原分辨率
- 三种差异度量：`mad`、`dhash`、`hist`
- 跨帧去重：与全部已保留帧比较，同一画面只导出一次；`--dupe-threshold` 可调
- 节奏控制：`--min-gap`、`--max-frames`、`--start`、`--end`
- 文件夹批量：每个视频独立子目录，`--recursive` 支持递归，单个失败不影响其余
- 图形界面：参数配置、实时进度、切点列表、缩略图预览、配置记忆
- 输出清单：`manifest.json` 与 `frames.csv`，含时间码、分数与生效参数
- 辅助命令：`--dry-run` 预览切点，`--dump-scores` 导出差异分数，`--sheet` 生成拼图

### 变更

- jpg 固定 4:4:4 编码并默认 `-q:v 1`，消除细边彩边与块状
- `--out-width` 超过源宽度时按原尺寸导出，避免放大失真
- Windows 控制台按 GBK 降级替换，ffmpeg 子进程隐藏控制台窗口

### 说明

- 源码支持 PyInstaller 冻结（`--engine` 自调用、`sys._MEIPASS` 查找），仓库不提供二进制
