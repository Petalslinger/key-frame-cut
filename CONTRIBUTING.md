# 参与开发

## 环境

- Python 3.9+
- numpy（唯一运行依赖）
- ffmpeg：**运行需要，跑测试不需要**（`tests/fake_ffmpeg.py` 是假解码器，按约定合成画面）

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

跑真实视频前先 `python flash_cut.py --check` 看 ffmpeg 有没有找到。

## 结构

| 文件 | 职责 |
| --- | --- |
| `flash_cut.py` | 引擎：探测 → 扫描 → 判定（阈值 + 去重）→ 导出 → 清单 |
| `flash_gui.py` | 界面：拼命令行、起子进程、解析输出 |
| `tests/fake_ffmpeg.py` | 假 ffmpeg，生成「每 2 秒换一次画面」的合成视频 |
| `tests/test_flash_cut.py` | 单元 + 端到端测试（不依赖真 ffmpeg） |

## 改代码前必须知道的四件事

1. **GUI 依赖引擎的输出格式**。`flash_gui.py` 顶部有一组正则（`扫描`、`* 时间码 score=`、`共 N 个切点`、`完成：N 个关键帧`），改日志文案要同步改正则，否则进度条和切点表会失效。
2. **控制台输出只用 GBK 能表示的字符**。Windows 下 stdout/stderr 被重定向到管道时编码可能是 cp936，`✓`、`→` 这类字符会出问题（源码里已有 `errors="replace"` 兜底，但别依赖它）。
3. **新增度量**：在 `FINGERPRINTS`、`DISTANCES`、`DEFAULT_THRESHOLDS` 三处注册即可，`--metric` 的 choices 会自动带上；再补一个距离计算的单测。
4. **不要新增依赖**。除 numpy 外一律可选：Pillow 只用于 `--sheet` 和界面缩略图，OpenCV 只在没有 ffmpeg 时兜底。

## 改动规范

- 改 CLI 参数：同步 README 的参数表 + GUI 控件（GUI 是把参数拼成命令行，漏一个就等于界面上没有）
- 改输出文件命名：同步 `frame_name()`、README、`--prefix` 的测试
- 改判定逻辑：至少补一个 `KeyframeSelector` 的单测，说明「为什么保留/丢弃」
- 保持 Windows 可运行：`os.startfile`、`taskkill`、`CREATE_NO_WINDOW` 都有平台分支

## 提交

- 一个提交做一件事，信息中英文都行但别混
- 提交前跑一遍 `python -m unittest discover -s tests -v`
- 动了界面的话，手动点一遍：环境检查 → 选视频 → 预览切点 → 开始导出 → 打开输出目录
