# 开发说明

## 环境

- Python 3.9+
- numpy
- ffmpeg 仅在处理真实视频时需要；测试使用 `tests/fake_ffmpeg.py` 模拟解码

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `flash_cut.py` | 引擎：探测、扫描、判定、导出、清单 |
| `flash_gui.py` | 界面：拼接命令行、启动子进程、解析输出 |
| `run_gui.bat` | Windows 启动脚本 |
| `tests/fake_ffmpeg.py` | 合成画面的假解码器 |
| `tests/test_flash_cut.py` | 单元测试与端到端测试 |

## 架构

引擎按 `探测 → 扫描 → 判定 → 导出 → 清单` 单向执行，`KeyframeSelector` 是无副作用的判定单元，可独立测试。

界面不直接调用引擎，而是启动 `flash_cut.py` 子进程并解析其输出，因此两种入口行为始终一致。

## 修改约定

1. **输出协议**：`flash_gui.py` 顶部正则解析引擎输出（进度、切点、汇总、错误）。修改日志文案需同步更新正则，否则进度与切点列表会静默失效。
2. **字符集**：Windows 管道编码可能为 cp936，避免在控制台输出中使用非 GBK 字符。源码已为 stdout/stderr 设置 `errors="replace"` 作为兜底。
3. **新增度量**：在 `FINGERPRINTS`、`DISTANCES`、`DEFAULT_THRESHOLDS` 三处注册，`--metric` 的候选值会自动扩展；同时补充距离计算测试。
4. **依赖**：除 numpy 外一律可选。Pillow 仅用于拼图与缩略图，OpenCV 仅作无 ffmpeg 时的兜底。
5. **参数变更**：同步 README 参数表与界面控件，避免界面缺项。

## 提交

- 一个提交对应一项改动
- 提交前运行完整测试
- 界面改动需手动验证：环境检查 → 预览切点 → 开始导出
