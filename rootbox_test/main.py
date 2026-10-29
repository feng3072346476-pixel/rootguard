"""根盒拍摄图像质检 —— 主入口（离线批量版）。

线上（拍摄即检）接入请直接用包 API，不走本文件：
    from rootbox_qc import verify_single, verify_pair
    rep = verify_single(r"D:\\data\\001\\1.png", attempt=1)
    if rep.recapture_needed:
        print(rep.recapture_reasons)   # → 通知小车重拍第 1 张

用法：
    1. 设置环境变量 ROOTBOX_QC_INPUT_DIR，或修改 config.py 的 paths.input_dir
    2. 运行：右键 → Run 'main'（或 py main.py）

可选：
- THRESHOLDS_OVERRIDE 临时调整阈值（不改源文件）
- write_files=False 只看终端结果
- show=False 只拿 reports 自己处理
"""
import os

from rootbox_qc import run

# ============================================================================
# 你唯一需要改的地方
# ============================================================================
# 优先读环境变量 ROOTBOX_QC_INPUT_DIR；未设置时用 config.py 的 paths.input_dir。
# 不要再把某台机器的绝对路径写死在这里，否则换机即 FileNotFoundError。
INPUT_DIR = os.environ.get("ROOTBOX_QC_INPUT_DIR") or None

# 临时阈值覆盖示例（不改源码；置为 {} 表示用 config.py 里的默认）
# 例如：模糊方差阈值调整：
# THRESHOLDS_OVERRIDE = {"blur_var_min": 25.0}
THRESHOLDS_OVERRIDE = {}

# 是否写结果文件（报告 JSON + 重拍队列）到 ./results/（默认 True；想只看终端设 False）
WRITE_FILES = True

# ============================================================================
# 程序入口（一般不用动）
# ============================================================================
if __name__ == "__main__":
    run(
        input_dir=INPUT_DIR,
        thresholds=THRESHOLDS_OVERRIDE,
        write_files=WRITE_FILES,
        show=True,
    )
