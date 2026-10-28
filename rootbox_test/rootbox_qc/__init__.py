# 根盒拍摄图像质检包。
#
# 离线（批量）：run(input_dir) -> list[QCReport]
# 线上（拍摄即检）：verify_single(path) / verify_pair(path_a, path_b)

from .config import (
    PATHS, PAIRING, THRESHOLDS, FRAME_DETECT, NOTIFIER, RECAPTURE, MOSAIC_LAYOUTS,
    load_config, get_thresholds, get_frame_params,
)
from .runner import run, run_and_show
from .verify import (
    verify_single, verify_pair, QCReport, ImageResult, RecaptureTracker,
    CHECK_CATEGORY, CATEGORY_ORDER, category_of, failed_checks,
)
from . import checks, frame, verify, notifier

__all__ = [
    "run", "run_and_show",
    "verify_single", "verify_pair",
    "QCReport", "ImageResult", "RecaptureTracker",
    "CHECK_CATEGORY", "CATEGORY_ORDER", "category_of", "failed_checks",
    "PATHS", "PAIRING", "THRESHOLDS", "FRAME_DETECT", "NOTIFIER", "RECAPTURE", "MOSAIC_LAYOUTS",
    "load_config",
]
