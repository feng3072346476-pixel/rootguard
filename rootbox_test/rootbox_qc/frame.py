"""特征检测：亮金属结构（凸包外接矩形四角 / 亮像素占比 / 质心）。

真实夜间大图特性：暗土壤 + 亮金属边。
输出字段供下游使用：
- bright_ratio : 全图亮像素占比（check_completeness 拍到支架/贴脸判定）
- centroid     : 亮像素质心 (cx, cy)（check_alignment_pair 位移合理性校验与质心兜底）
- corners      : 最大亮连通域凸包的外接矩形四角（check_blur / check_blur_pair 截取亮区 ROI）
- found        : 是否检出有效亮金属外接框
"""
from __future__ import annotations

import cv2
import numpy as np


def detect_bright_features(img_bgr: np.ndarray, bright_th: int = 130) -> dict:
    """提取亮金属结构特征（亮占比 + 质心 + 最大连通域凸包外接矩形四角）。"""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY) if img_bgr.ndim == 3 else img_bgr
    h, w = gray.shape

    # 开运算去除孤立亮点噪声，外接框估计更稳
    mask = (gray > bright_th).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    ratio = float(mask.sum() / mask.size)

    if ratio < 0.001:
        return {"bright_ratio": ratio, "centroid": None, "hull_corners": None}

    ys, xs = np.where(mask > 0)
    cx, cy = float(xs.mean()), float(ys.mean())

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if cnts:
        big = max(cnts, key=cv2.contourArea)
        if cv2.contourArea(big) > 0.001 * h * w:
            hull = cv2.convexHull(big)
            rect = cv2.minAreaRect(hull)
            box = cv2.boxPoints(rect)
            return {"bright_ratio": ratio, "centroid": (cx, cy),
                    "hull_corners": box.tolist()}

    return {"bright_ratio": ratio, "centroid": (cx, cy), "hull_corners": None}


def detect_frame(img_bgr: np.ndarray, params: dict | None = None) -> dict:
    """亮金属结构特征统一入口。

    Returns:
        dict: found / corners(np.ndarray shape=(4,2)) / bright_ratio / centroid
    """
    empty = {"found": False, "corners": None, "bright_ratio": 0.0, "centroid": None}
    if img_bgr is None:
        return empty
    bf_params = params or {}
    bright_th = int(bf_params.get("bright_threshold", 130))

    bf = detect_bright_features(img_bgr, bright_th=bright_th)
    frame = dict(empty)
    frame["bright_ratio"] = bf["bright_ratio"]
    frame["centroid"] = bf["centroid"]

    corners = bf.get("hull_corners")
    if corners is not None:
        frame["found"] = True
        frame["corners"] = np.array(corners, dtype=np.float32)
    return frame
