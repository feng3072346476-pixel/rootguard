"""5 类生产质量检测：损坏、模糊、拍摄不全、错位（成对+单张）、重复拍摄。

生产有效判据体系：
1. 损坏（check_corruption + check_illumination_pair）：
   - 文件大小 / 魔数 / JPEG EOI 截断 / 解码尺寸核对 / 每百万像素体积
   - 全图灰度标准差过低（近纯色）+ 6x8 分块局部纯黑/极暗无纹理
   - 成对单灯管照明缺失（两帧亮像素占比严重不对称）
2. 模糊（check_blur + check_blur_pair）：
   - 单张亮区 ROI Laplacian 方差 + 角色感知拼接存活列/无反光取片列方差
   - 成对锐度不对称（同盒两帧一张清晰一张拖影/失焦）
3. 拍摄不全（check_completeness）：
   - 亮像素占比超限（拍到支架/贴脸）
   - 底部金属长边结构缺失（Rule A，含暗帧守卫与拼缝台阶错位豁免）
4. 错位（check_alignment_pair + check_alignment_single）：
   - 成对：上/中/下三条区域带相位上下/水平位移 + ORB 特征匹配（含相位印证）+ 亮区质心兜底
   - 单张（拼接平图）：自适应找缝 + 自洽交替缝组（DP）+ 固定产线缝窗兜底
5. 重复拍摄（check_repeat_pair）：
   - 文件字节完全一致 + 低频照明差（同一次照明状态）+ 特征高内点零位移
"""
from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image

from .config import THRESHOLDS, MOSAIC_LAYOUTS


def _with_defaults(th: dict | None) -> dict:
    """将调用方传入的阈值字典与 config.THRESHOLDS 合并，杜绝局部硬编码旧默认值漂移。"""
    if not th:
        return dict(THRESHOLDS)
    if len(th) >= len(THRESHOLDS) and "input_kind" in th:
        return th
    return {**THRESHOLDS, **th}


def _active_cuts(th: dict) -> list:
    """当前生效的三刀口：layout 优先（整套切换），否则用自定义 blur_strip_cuts。"""
    layout = th.get("layout")
    if layout in MOSAIC_LAYOUTS:
        return list(MOSAIC_LAYOUTS[layout])
    cuts = th.get("blur_strip_cuts")
    if cuts and len(cuts) >= 3:
        return [int(c) for c in cuts[:3]]
    return list(MOSAIC_LAYOUTS.get("C", (2200, 2780, 3300)))


# ---------------------------------------------------------------------------
# 1. 图片损坏
# ---------------------------------------------------------------------------
_MAGIC = {
    "jpg": (b"\xff\xd8", "JPEG"),
    "jpeg": (b"\xff\xd8", "JPEG"),
    "png": (b"\x89PNG\r\n\x1a\n", "PNG"),
    "bmp": (b"BM", "BMP"),
    "tif": (b"II*\x00", "TIFF"),
    "tiff": (b"II*\x00", "TIFF"),
}


def check_corruption(path: str | None = None, img_bgr=None, th: dict | None = None) -> dict:
    """文件级 + 解码级 + 内容级完整性检查。"""
    th = _with_defaults(th)
    metric: dict = {}
    threshold: dict = {}
    reasons = []

    min_size = int(th.get("corruption_min_filesize", 100_000))
    std_min = float(th.get("corruption_gray_std_min", 15.0))
    metric["filesize"] = None
    threshold["filesize_min"] = min_size
    threshold["gray_std_min"] = std_min

    ext = ""
    if path is not None:
        try:
            fsize = os.path.getsize(path)
        except OSError as e:
            return _fail("corruption", metric, threshold, f"文件无法读取: {e}")
        if fsize == 0:
            return _fail("corruption", metric, threshold, "文件为空（疑似损坏）")
        metric["filesize"] = fsize
        if fsize < min_size:
            reasons.append(f"文件过小（{fsize/1024:.0f}KB < {min_size/1024:.0f}KB），"
                           "疑似未完整写入")

        # 文件头魔数：与扩展名不符说明写入/损坏
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        magic = _MAGIC.get(ext)
        if magic is not None:
            try:
                with open(path, "rb") as f:
                    head = f.read(len(magic[0]))
                if head != magic[0]:
                    reasons.append(f"文件头异常（期望 {magic[1]}）")
            except OSError as e:
                reasons.append(f"文件读取失败: {e}")

        # JPEG 尾部 EOI（FFD9）截断检查：cv2 对下半截丢失的 JPEG 会静默返回全尺寸灰底图
        if ext in ("jpg", "jpeg") and fsize >= 4:
            try:
                with open(path, "rb") as f:
                    f.seek(-min(64, fsize), os.SEEK_END)
                    tail = f.read()
                eoi_ok = b"\xff\xd9" in tail
                metric["jpeg_eoi_ok"] = eoi_ok
                if not eoi_ok:
                    reasons.append("JPEG 文件尾缺少结束标记（FFD9），疑似文件未完整写入/截断")
            except OSError:
                pass

    if img_bgr is None:
        reasons.append("图片无法解码（文件损坏或格式错误）")
        return _fail("corruption", metric, threshold, "；".join(reasons))

    hdr_size = None
    if path is not None:
        try:
            with Image.open(path) as im:
                hdr_size = im.size          # 文件头声明的尺寸
                im.verify()
        except Exception as e:  # noqa: BLE001
            reasons.append(f"图片解码异常: {e}")

    if img_bgr.ndim == 3:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    else:
        gray = img_bgr
    h, w = gray.shape

    # 体积按分辨率归一（每百万像素字节数），替代纯绝对下限：小图不误伤、大图更严
    min_bpm = float(th.get("corruption_min_bytes_per_mpx", 5000.0))
    threshold["bytes_per_mpx_min"] = min_bpm
    if metric.get("filesize") and h * w > 0:
        bpm = metric["filesize"] / (h * w / 1e6)
        metric["bytes_per_mpx"] = round(bpm)
        if bpm < min_bpm:
            reasons.append(f"文件体积极低（{bpm:.0f}B/MP < {min_bpm:.0f}B/MP），疑似未完整写入")

    # 截断检查：cv2 对截断图会静默返回部分图，核对解码尺寸与文件头声明。
    # load_image 会把竖拍大图顺时针旋转 90°，此时解码尺寸与文件头恰好横竖互换；
    # 只接受"同尺寸"或"精确转置"两种，不能因为正常竖拍误报截断。
    if hdr_size is not None and (hdr_size[1], hdr_size[0]) not in ((h, w), (w, h)):
        reasons.append(f"解码尺寸 {w}x{h} 与文件头 {hdr_size[0]}x{hdr_size[1]} 不符，疑似文件截断")

    g_std = float(np.std(gray))
    metric["gray_std"] = round(g_std, 3)
    if g_std < std_min:
        reasons.append(f"灰度标准差 {g_std:.1f} 过低（<{std_min}），画面近纯色")

    # 局部纯黑 / 极暗无纹理（明暗极不均）：6x8 分块均值/标准差由 INTER_AREA 缩放一次求出
    blk_rows = int(th.get("corruption_block_rows", 6))
    blk_cols = int(th.get("corruption_block_cols", 8))
    dark_min = int(th.get("corruption_dark_blocks_min", 4))
    dim_min = int(th.get("corruption_dim_blocks_min", 4))
    bright_min = float(th.get("corruption_dim_bright_min", 60.0))
    threshold["dark_blocks_min"] = dark_min
    threshold["dim_blocks_min"] = dim_min
    gf = gray.astype(np.float32)
    blk_mean = cv2.resize(gf, (blk_cols, blk_rows), interpolation=cv2.INTER_AREA)
    blk_e2 = cv2.resize(cv2.multiply(gf, gf), (blk_cols, blk_rows),
                        interpolation=cv2.INTER_AREA)
    blk_std = np.sqrt(np.maximum(0.0, blk_e2 - blk_mean * blk_mean))
    n_dark = int(((blk_mean < 12) & (blk_std < 8)).sum())
    n_dim = int(((blk_mean < 25) & (blk_std < 12)).sum())
    bright_max = float(blk_mean.max())
    metric["dark_blocks"] = n_dark
    metric["dim_blocks"] = n_dim
    metric["bright_block"] = round(bright_max, 1)
    total_blk = blk_rows * blk_cols
    if n_dark >= dark_min:
        reasons.append(f"局部纯黑区：{blk_rows}x{blk_cols} 分块中 {n_dark} 块 "
                       f"mean<12 且 std<8（约 {n_dark / total_blk * 100:.0f}% 面积），"
                       "画面局部全黑")
    elif n_dim >= dim_min and bright_max >= bright_min:
        reasons.append(f"明暗极不均：{n_dim} 块极暗无纹理（mean<25 且 std<12）"
                       f"与亮区（最亮块 {bright_max:.0f}）并存")

    if reasons:
        return _fail("corruption", metric, threshold, "；".join(reasons))
    return _ok("corruption", metric, threshold)


# ---------------------------------------------------------------------------
# 2. 模糊（单张 ROI 方差 + 角色感知存活列 + 成对锐度不对称）
# ---------------------------------------------------------------------------
def _bright_roi(gray, frame, margin_ratio: float = 0.20):
    """亮金属外接矩形（外扩 20%）作为纹理分析 ROI；无亮区时退回全图。"""
    corners = (frame or {}).get("corners")
    h, w = gray.shape
    if corners is not None and len(corners) >= 4:
        xs = corners[:, 0]
        ys = corners[:, 1]
        x0, x1 = float(xs.min()), float(xs.max())
        y0, y1 = float(ys.min()), float(ys.max())
        mx = (x1 - x0) * margin_ratio
        my = (y1 - y0) * margin_ratio
        x0, x1 = int(max(0, x0 - mx)), int(min(w, x1 + mx))
        y0, y1 = int(max(0, y0 - my)), int(min(h, y1 + my))
        if x1 - x0 > 32 and y1 - y0 > 32:
            return gray[y0:y1, x0:x1]
    return gray


def _mosaic_strip_var(gray, cuts):
    """拼接马赛克"存活列"的锐度（Laplacian 方差）。

    单次全图 Laplacian 后按列分组取方差，避免额外卷积。
    返回 (var_a, var_b)；画幅/刀口不匹配时返回 (None, None)。
    """
    if gray is None or len(cuts) < 3:
        return None, None
    g = gray if gray.shape[0] <= gray.shape[1] else cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
    h, w = g.shape
    c1, c2, c3 = int(cuts[0]), int(cuts[1]), int(cuts[2])
    if not (0 < c1 < c2 < c3 < w):
        return None, None
    lap = cv2.Laplacian(g, cv2.CV_32F)
    reg_a = np.concatenate([lap[:, 0:c1], lap[:, c2:c3]], axis=1)
    reg_b = np.concatenate([lap[:, c1:c2], lap[:, c3:w]], axis=1)
    return round(float(reg_a.var()), 2), round(float(reg_b.var()), 2)


def _mosaic_strip_role(gray, cuts, path: str | None = None,
                       sa: float | None = None, sb: float | None = None) -> str | None:
    """判定原图所属灯光角色（'1' 或 '2'）。

    优先按文件名（1.ext / 2.ext 或 *_1.ext / *_2.ext）识别；
    散装未命名原图按条带平均亮度比值（stripB 均值 > stripA 均值 => '1'）识别。
    """
    if path is not None:
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        if stem == "1" or stem.endswith("_1"):
            return "1"
        if stem == "2" or stem.endswith("_2"):
            return "2"
    if gray is not None and cuts and len(cuts) >= 3:
        g = gray if gray.shape[0] <= gray.shape[1] else cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
        w = g.shape[1]
        c1, c2, c3 = int(cuts[0]), int(cuts[1]), int(cuts[2])
        if 0 < c1 < c2 < c3 < w:
            ma = float(np.concatenate([g[:, 0:c1], g[:, c2:c3]], axis=1).mean())
            mb = float(np.concatenate([g[:, c1:c2], g[:, c3:w]], axis=1).mean())
            return "1" if mb > ma else "2"
    if sa is not None and sb is not None:
        return "1" if sb > sa else "2"
    return None


def _strip_led_ratio(gray, cuts, role: str | None) -> float | None:
    """存活列里高亮灯管像素占比：用于判断“该侧是否真的有一根灯管”。

    灯管未开/不在该侧时，存活列 Laplacian 方差天然偏低，不能据此判模糊；
    正常无灯图片的占比 <0.3%，有灯管拖影图约 3%。
    """
    if gray is None or not cuts or len(cuts) < 3 or role not in ("1", "2"):
        return None
    g = gray if gray.shape[0] <= gray.shape[1] else cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
    w = g.shape[1]
    c1, c2, c3 = int(cuts[0]), int(cuts[1]), int(cuts[2])
    if not (0 < c1 < c2 < c3 < w):
        return None
    reg_a = np.concatenate([g[:, 0:c1], g[:, c2:c3]], axis=1)
    reg_b = np.concatenate([g[:, c1:c2], g[:, c3:w]], axis=1)
    alive = reg_b if role == "1" else reg_a
    return round(float((alive > 200).mean()), 5)


def check_blur_pair(img_a, img_b, frame_a, frame_b, th: dict | None = None, *,
                    path_a: str | None = None, path_b: str | None = None) -> dict:
    """成对模糊：两张里一张锐、一张糊（亮区 Laplacian 方差不对称）。"""
    th = _with_defaults(th)
    metric: dict = {}
    threshold: dict = {}
    if not bool(th.get("blur_pair_check_enabled", True)):
        return _ok("blur_pair", {"blur_pair_check": "disabled"}, {}, "成对模糊判据已禁用")
    if img_a is None or img_b is None:
        return _ok("blur_pair", {"blur_pair": "unavailable"}, {}, "缺图，跳过成对模糊判定")

    rois = []
    for img, frame in ((img_a, frame_a), (img_b, frame_b)):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        rois.append(_bright_roi(gray, frame))
    vars_ = [round(float(cv2.Laplacian(r, cv2.CV_64F).var()), 2) for r in rois]
    lo_i = int(np.argmin(vars_))
    hi_i = 1 - lo_i
    ratio = round(vars_[hi_i] / vars_[lo_i], 3) if vars_[lo_i] > 0 else None
    var_max = float(th.get("blur_pair_roi_var_max", 25.5))
    ratio_min = float(th.get("blur_pair_ratio_min", 2.2))
    paths = (path_a, path_b)
    metric["roi_var"] = {"1": vars_[0], "2": vars_[1]}
    metric["soft_role"] = "1" if lo_i == 0 else "2"
    metric["soft_roi_var"] = vars_[lo_i]
    metric["sharp_roi_var"] = vars_[hi_i]
    metric["roi_var_ratio"] = ratio
    metric["soft_path"] = paths[lo_i]
    threshold["blur_pair_roi_var_max"] = var_max
    threshold["blur_pair_ratio_min"] = ratio_min

    if ratio is not None and vars_[lo_i] <= var_max and ratio >= ratio_min:
        return _fail("blur_pair", metric, threshold,
                     f"{metric['soft_role']} 号图亮区纹理锐度不足（Laplacian 方差 "
                     f"{vars_[lo_i]:.1f} <= {var_max:.1f}，另一张 {vars_[hi_i]:.1f} / "
                     f"{ratio:.1f} 倍），疑似灯照拖影/失焦")
    return _ok("blur_pair", metric, threshold,
               f"两张亮区锐度 {vars_[0]:.1f} / {vars_[1]:.1f}，无单张拖影")


def check_illumination_pair(img_a, img_b, th: dict | None = None) -> dict:
    """单灯管/单侧照明缺失（损坏）：两帧亮像素占比严重不对称。"""
    th = _with_defaults(th)
    if not bool(th.get("illum_pair_check_enabled", True)):
        return _ok("illum_pair", {"illum_pair_check": "disabled"}, {}, "单灯管照明判据已禁用")
    if img_a is None or img_b is None:
        return _ok("illum_pair", {"illum_pair": "unavailable"}, {}, "缺图，跳过照明判定")
    ratios = []
    for img in (img_a, img_b):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        ratios.append(round(float((gray > 90).mean()), 4))
    lo_i = int(np.argmin(ratios))
    hi_i = 1 - lo_i
    dark_max = float(th.get("illum_pair_bright_dark_max", 0.10))
    bright_min = float(th.get("illum_pair_bright_ratio_min", 0.25))
    metric = {"bright_ratio": {"1": ratios[0], "2": ratios[1]},
              "dark_role": "1" if lo_i == 0 else "2",
              "dark_ratio": ratios[lo_i], "bright_ratio_max": ratios[hi_i]}
    threshold = {"bright_ratio_dark_max": dark_max,
                 "bright_ratio_bright_min": bright_min}
    if ratios[lo_i] <= dark_max and ratios[hi_i] >= bright_min:
        return _fail("illum_pair", metric, threshold,
                     f"{metric['dark_role']} 号图亮像素占比 {ratios[lo_i]*100:.1f}% 远低于"
                     f"另一张 {ratios[hi_i]*100:.1f}%，疑似只有一根灯管亮（照明异常）")
    return _ok("illum_pair", metric, threshold,
               f"两张亮像素占比 {ratios[0]*100:.1f}% / {ratios[1]*100:.1f}%，照明无单侧缺失")


def check_blur(img_bgr, gray, frame, th: dict | None = None, path: str | None = None) -> dict:
    th = _with_defaults(th)
    metric: dict = {}
    threshold: dict = {}
    reasons = []
    notes = []
    undetermined = []

    roi = _bright_roi(gray, frame)
    var_min = float(th.get("blur_var_min", 20.0))
    var = float(cv2.Laplacian(roi, cv2.CV_64F).var())
    metric["roi_laplacian_var"] = round(var, 2)
    threshold["roi_laplacian_var_min"] = var_min
    if var < var_min:
        reasons.append(f"亮区纹理锐度不足（Laplacian 方差 {var:.1f} < {var_min:.1f}）")

    if bool(th.get("blur_strip_metric_enabled", True)):
        cuts = _active_cuts(th)
        sa, sb = _mosaic_strip_var(gray, cuts)
        metric["blur_strip_a_var"] = sa
        metric["blur_strip_b_var"] = sb
        role = _mosaic_strip_role(gray, cuts, path=path, sa=sa, sb=sb)
        alive = None
        surviving = None
        if sa is not None and sb is not None:
            alive = sb if role == "1" else sa
            surviving = sa if role == "1" else sb
        metric["blur_strip_role"] = role
        metric["blur_strip_alive_var"] = alive
        metric["blur_strip_surviving_var"] = surviving
        led_ratio = _strip_led_ratio(gray, cuts, role)
        metric["blur_strip_alive_led_ratio"] = led_ratio
        threshold["blur_strip_alive_var_min"] = float(th.get("blur_strip_alive_var_min", 46.0))
        threshold["blur_strip_surviving_var_min"] = float(
            th.get("blur_strip_surviving_var_min", 26.5))
        threshold["blur_strip_alive_led_ratio_min"] = float(
            th.get("blur_strip_alive_led_ratio_min", 0.005))
        if th.get("input_kind") == "mosaic":
            metric["blur_strip_check"] = "skipped_mosaic"
            notes.append("拼接成品图模式（input_kind=mosaic），跳过原图存活列模糊判定")
        elif bool(th.get("blur_strip_check_enabled", True)):
            if sa is None or sb is None:
                # 刀口与画幅不匹配时旧代码会静默 pass；生产模式改为无法判定，交重拍/人工。
                if bool(th.get("resolution_guard_enabled", True)):
                    undetermined.append(
                        f"刀口 {cuts} 与画幅宽度 {gray.shape[1]} 不匹配，无法计算存活列方差")
            elif role is None:
                undetermined.append("无法判定该图属于 1 号还是 2 号灯照角色，存活列判据不可用")
            elif alive is not None and alive < threshold["blur_strip_alive_var_min"]:
                if led_ratio is not None and led_ratio < threshold["blur_strip_alive_led_ratio_min"]:
                    notes.append(
                        f"存活列未检测到灯管反光（高亮像素占比 {led_ratio*100:.2f}% < "
                        f"{threshold['blur_strip_alive_led_ratio_min']*100:.2f}%），"
                        "存活列锐度判据不适用")
                else:
                    reasons.append(f"拼接存活列纹理锐度不足（{role} 号图存活列 Laplacian 方差 "
                                   f"{alive:.1f} < {threshold['blur_strip_alive_var_min']:.1f}）")
            elif surviving is not None and surviving < threshold["blur_strip_surviving_var_min"]:
                reasons.append(f"拼接无反光取片列纹理锐度不足（{role} 号图取片列 Laplacian 方差 "
                               f"{surviving:.1f} < {threshold['blur_strip_surviving_var_min']:.1f}）")

    if reasons:
        return _fail("blur", metric, threshold, "；".join(reasons))
    if undetermined:
        return _undetermined("blur", metric, threshold, "；".join(undetermined))
    return _ok("blur", metric, threshold, "；".join(notes))


# ---------------------------------------------------------------------------
# 3. 拍摄不全（亮像素占比上限 + 上下金属长边对称判据）
# ---------------------------------------------------------------------------
def _long_edge_metric(gray, width=512, grad_t=40.0, pos_thr=0.25,
                       bot_zone_start=0.85, top_pos_thr=None,
                       top_zone_end=0.30, bottom_zone_start=0.70,
                       bottom_band_end=0.96):
    """上下金属长边的位置、峰值强度与底部结构带挺直度。

    同时保留两套读数，语义不同：
    - y_top / y_bot：旧“第一条/最后一条强边”，只作诊断记录；
    - top_peak_* / bottom_peak_*：上 30% / 下 30% 各自最强的水平边，
      用来判断上、下金属长边是否存在、是否被推离画面边缘；
    - bot_zone_max / bottom_band_max：底部结构带 [bot_zone_start, 1.0] 与
      目标底梁带 [bot_zone_start, bottom_band_end] 的最强挺直度，
      用来判断底部金属长边是否弱到缺失。
    """
    h0, w0 = gray.shape
    if w0 > width:
        s = width / w0
        g = cv2.resize(gray, (width, max(1, int(h0 * s))), interpolation=cv2.INTER_AREA)
    else:
        g = gray
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    tall = np.convolve((np.abs(gy) > grad_t).mean(axis=1), np.ones(7) / 7.0, mode="same")
    h = len(tall)

    top_end = max(1, min(h, int(top_zone_end * h)))
    top_idx = int(np.argmax(tall[:top_end]))
    bottom_start = max(0, min(h - 1, int(bottom_zone_start * h)))
    bottom_idx = int(np.argmax(tall[bottom_start:])) + bottom_start

    start = max(0, min(h - 1, int(bot_zone_start * h)))
    band_end = max(start + 1, min(h, int(bottom_band_end * h)))
    bot_zone_max = round(float(tall[start:].max()), 3)
    band_max = round(float(tall[start:band_end].max()), 3)

    out = {
        "y_top": None,
        "y_bot": None,
        "bot_zone_max": bot_zone_max,
        "bottom_band_max": band_max,
        "top_peak_y": round(top_idx / h, 3),
        "top_peak_strength": round(float(tall[top_idx]), 3),
        "bottom_peak_y": round(bottom_idx / h, 3),
        "bottom_peak_strength": round(float(tall[bottom_idx]), 3),
    }

    strong = np.where(tall > pos_thr)[0]
    if not len(strong):
        return out
    if top_pos_thr is None:
        top_pos_thr = pos_thr
    strong_top = np.where(tall > top_pos_thr)[0]
    out["y_top"] = round(float(strong_top[0]) / h, 3) if len(strong_top) else None
    out["y_bot"] = round(float(strong[-1]) / h, 3)
    return out


def _top_cover_y(img_bgr, hue_min=5, hue_max=45, sat_min=45, val_min=25,
                 frac_min=0.45, width=512, hold=12) -> float | None:
    """成品图中板面（土壤色）第一次出现的归一化行位置。

    顶部金属横梁被裁时，褐色板面会更早出现（727 约 0.065，正常 508 约 0.079）。
    只对成品平图/拼接图使用；原图 2.png 的接缝侧允许没有完整顶梁。
    """
    if img_bgr is None or getattr(img_bgr, "size", 0) == 0:
        return None
    if getattr(img_bgr, "ndim", 0) == 2:
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
    elif img_bgr.shape[2] == 4:
        img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_BGRA2BGR)
    h, w = img_bgr.shape[:2]
    if w > width:
        img = cv2.resize(img_bgr, (width, max(1, int(h * width / w))),
                         interpolation=cv2.INTER_AREA)
    else:
        img = img_bgr
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    mask = ((H >= int(hue_min)) & (H <= int(hue_max))
            & (S >= int(sat_min)) & (V >= int(val_min)))
    frac = mask.mean(axis=1).astype(np.float32)
    frac = np.convolve(frac, np.ones(5) / 5, mode="same")
    n = len(frac)
    for i in range(max(0, n - hold)):
        if frac[i] > frac_min and frac[i:i + hold].mean() > frac_min:
            return round(float(i) / n, 4)
    return None

def _seam_step_misalign(gray, th: dict | None = None) -> list | None:
    """缝台阶错位：本图是否存在"同一根盒跨拼缝上下错开"的自洽缝组。

    缝台阶会把底部金属长边逐行打碎（强梯度列占比被稀释到 pos_thr 下），
    此时底部读数不可信，用于豁免 Rule A 误报。
    """
    th = _with_defaults(th)
    if gray is None:
        return None
    dy_min = float(th.get("alignment_seam_dy_min", 9.0))
    span = int(th.get("alignment_seam_dy_span", 120))
    corr_min = float(th.get("alignment_adaptive_corr_min", 0.30))
    hits = []
    for x, _r in _find_seam_candidates(gray, th):
        dy, corr = _seam_dy(gray, x, th)
        if dy_min <= abs(dy) < span and corr >= corr_min:
            hits.append({"x": x, "dy": dy, "corr": corr})
    if not hits:
        return None
    s = gray.shape[1] / _MOSAIC_W0
    chain, clen = _seam_chain(hits, th,
                              float(th.get("alignment_adaptive_min_gap", 200.0)) * s)
    if clen < int(th.get("alignment_adaptive_chain_min", 3)):
        return None
    return [{"x": c["x"], "dy": c["dy"]} for c in chain]


def check_completeness(img_bgr, gray, frame, th: dict | None = None) -> dict:
    th = _with_defaults(th)
    bf_max = float(th.get("completeness_bright_ratio_max", 0.30))
    reasons = []
    undetermined = []

    bright_ratio = float((frame or {}).get("bright_ratio", 0.0))
    metric = {"bright_ratio": round(bright_ratio, 4)}
    threshold = {"bright_ratio_max": bf_max}

    if bright_ratio > bf_max:
        reasons.append(
            f"画面中金属结构过多（亮像素 {bright_ratio*100:.1f}%，疑似相机偏离或拍到支架）")

    # 顶部覆盖：成品图中板面起点太靠上，说明顶部金属横梁被裁（727 类）。
    # 只对成品平图/拼接图使用；原图 1/2 的接缝侧不套用。
    if (bool(th.get("completeness_top_cover_check_enabled", True))
            and th.get("input_kind") == "mosaic"):
        cover_min = float(th.get("completeness_top_cover_y_min", 0.072))
        cover_y = _top_cover_y(
            img_bgr,
            hue_min=int(th.get("completeness_top_cover_hue_min", 5)),
            hue_max=int(th.get("completeness_top_cover_hue_max", 45)),
            sat_min=int(th.get("completeness_top_cover_sat_min", 45)),
            val_min=int(th.get("completeness_top_cover_val_min", 25)),
            frac_min=float(th.get("completeness_top_cover_frac_min", 0.45)))
        metric["top_cover_y"] = cover_y
        threshold["top_cover_y_min"] = cover_min
        if cover_y is not None and cover_y < cover_min:
            reasons.append(
                f"顶部金属横梁过窄/被裁（板面起点 y={cover_y:.3f} < {cover_min:.3f}），"
                "疑似未拍全根盒上侧")

    # 上下对称判据：上侧顶梁要贴顶且足够强；下侧底梁不能偏上，也不能弱到缺失。
    # 旧版“上侧缺失时看底部强度”会把 052/1 这类上侧缺失、底部仍强的图放行，
    # 已按 2026-10-29 的标注修正为上下各自独立判断。
    if bool(th.get("completeness_longedge_check_enabled", True)):
        pos_thr = float(th.get("completeness_longedge_pos_thr", 0.25))
        bot_zone_start = float(th.get("completeness_longedge_bot_zone_start", 0.85))
        top_enabled = bool(th.get("completeness_top_edge_check_enabled", True))
        bottom_enabled = bool(th.get("completeness_bottom_edge_check_enabled", True))
        top_pos_thr = (float(th.get("completeness_top_edge_pos_thr", 0.30))
                       if top_enabled else pos_thr)
        le = _long_edge_metric(
            gray,
            width=int(th.get("completeness_longedge_profile_width", 512)),
            grad_t=float(th.get("completeness_longedge_grad_thresh", 40.0)),
            pos_thr=pos_thr,
            bot_zone_start=bot_zone_start,
            top_pos_thr=top_pos_thr,
            top_zone_end=float(th.get("completeness_top_peak_zone_end", 0.30)),
            bottom_zone_start=float(th.get("completeness_bottom_peak_zone_start", 0.70)),
            bottom_band_end=float(th.get("completeness_bottom_band_end", 0.96)))
        metric["long_edge"] = le
        dark_mean_max = float(th.get("completeness_dark_mean_max", 65.0))
        threshold["longedge_bot_zone_start"] = bot_zone_start
        threshold["dark_mean_max"] = dark_mean_max
        mean_all = float(gray.mean())
        metric["long_edge"]["mean_gray"] = round(mean_all, 1)

        if top_enabled:
            top_min = float(th.get("completeness_top_peak_strength_min", 0.22))
            top_y_max = float(th.get("completeness_top_edge_y_max", 0.20))
            threshold["top_peak_strength_min"] = top_min
            threshold["top_edge_y_max"] = top_y_max
            ts, ty = le["top_peak_strength"], le["top_peak_y"]
            if ts < top_min:
                reasons.append(
                    f"上侧金属长边缺失/未拍到（顶梁挺直度 {ts:.3f} < {top_min:.2f}），"
                    "疑似相机偏到相邻根盒或未拍全根盒上侧")
            elif ty > top_y_max:
                reasons.append(
                    f"上侧金属长边被推离画面顶部（顶梁位置 {ty:.3f} > {top_y_max:.2f}），"
                    "疑似拍进相邻根盒")

        if bottom_enabled:
            bottom_floor = float(th.get("completeness_bottom_floor", 0.15))
            bottom_y_min = float(th.get("completeness_bottom_edge_y_min", 0.868))
            bottom_strength_max = float(th.get("completeness_bottom_strength_max", 0.35))
            band_min = float(th.get("completeness_bottom_band_strength_min", 0.28))
            dark_exempt_bot_max = float(th.get("completeness_dark_exempt_bot_max", 0.25))
            threshold["bottom_floor"] = bottom_floor
            threshold["bottom_edge_y_min"] = bottom_y_min
            threshold["bottom_strength_max"] = bottom_strength_max
            threshold["bottom_band_strength_min"] = band_min
            threshold["dark_exempt_bot_max"] = dark_exempt_bot_max

            bot, band = le["bot_zone_max"], le["bottom_band_max"]
            by = le["bottom_peak_y"]
            if by < bottom_y_min:
                reasons.append(
                    f"底部金属长边被推离画面底部/明显偏上（底梁位置 {by:.3f} < "
                    f"{bottom_y_min:.2f}），疑似未拍全根盒下侧")

            if bot < bottom_strength_max and band < band_min:
                if (mean_all < dark_mean_max and band >= bottom_floor
                        and bot < dark_exempt_bot_max):
                    metric["long_edge"]["dark_bottom_exempt"] = (
                        f"均值 {mean_all:.1f} < {dark_mean_max:.0f}，底部挺直度 {bot:.2f} "
                        f"与底部带 {band:.2f} 均偏低，但上侧长边正常且底部仍有弱结构")
                else:
                    reasons.append(
                        f"底部金属长边结构缺失/不明显（底部挺直度 {bot:.3f} < "
                        f"{bottom_strength_max:.2f} 且目标底梁带 {band:.3f} < {band_min:.2f}），"
                        "疑似未拍全根盒下侧")
            elif bot < bottom_floor:
                reasons.append(
                    f"底部金属长边结构完全缺失（底部挺直度 {bot:.3f} < "
                    f"{bottom_floor:.2f}），疑似未拍全根盒下侧")
            elif bot < pos_thr:
                if mean_all < dark_mean_max:
                    metric["long_edge"]["dark_bottom_exempt"] = (
                        f"均值 {mean_all:.1f} < {dark_mean_max:.0f}，"
                        f"底部挺直度 {bot:.2f} 偏低，但上侧长边位置正常")
                else:
                    step = _seam_step_misalign(gray, th)
                    if step is None:
                        reasons.append(
                            f"底部无金属长边结构（底部区挺直度峰值 {bot:.3f} < "
                            f"{pos_thr:.2f}），疑似未拍全根盒下侧")
                    else:
                        metric["long_edge"]["seam_step_exempt"] = step

    if reasons:
        return _fail("completeness", metric, threshold, "；".join(reasons))
    if undetermined:
        return _undetermined("completeness", metric, threshold, "；".join(undetermined))
    return _ok("completeness", metric, threshold)


# ---------------------------------------------------------------------------
# 4. 错位（成对：区域带相位为主 + ORB 特征匹配 + 质心兜底）
# ---------------------------------------------------------------------------
def _feature_similarity(img_a, img_b, th) -> dict | None:
    """降采样 → ORB → 比率测试匹配 → RANSAC 相似变换。"""
    down = max(1, int(th.get("align_orb_downscale", 3)))
    nfeat = int(th.get("align_orb_nfeatures", 4000))
    min_good = int(th.get("alignment_min_good_matches", 12))
    rois = []
    for img in (img_a, img_b):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        g = cv2.resize(gray, None, fx=1.0 / down, fy=1.0 / down,
                       interpolation=cv2.INTER_AREA)
        rois.append(g)
    ga, gb = rois

    orb = cv2.ORB_create(nfeatures=nfeat, edgeThreshold=19)
    kp_a, des_a = orb.detectAndCompute(ga, None)
    kp_b, des_b = orb.detectAndCompute(gb, None)
    min_kp = int(th.get("align_orb_min_keypoints", 50))
    if des_a is None or des_b is None or len(kp_a) < min_kp or len(kp_b) < min_kp:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    knn = bf.knnMatch(des_a, des_b, k=2)
    good = [m for m, n in knn if m.distance < 0.75 * n.distance]
    if len(good) < min_good:
        return None

    src = np.float32([kp_a[m.queryIdx].pt for m in good])
    dst = np.float32([kp_b[m.trainIdx].pt for m in good])
    M, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                             ransacReprojThreshold=3.0)
    if M is None:
        return None
    a, b = float(M[0, 0]), float(M[0, 1])
    tx, ty = float(M[0, 2]), float(M[1, 2])
    scale = float(np.hypot(a, b))
    theta = float(np.degrees(np.arctan2(b, a)))
    if theta <= -45:
        theta += 90
    elif theta > 45:
        theta -= 90
    n_inl = int(inliers.sum()) if inliers is not None else 0
    return {
        "shift_px": round(float(np.hypot(tx, ty) * down), 2),
        "theta_deg": round(theta, 3),
        "scale": round(scale, 4),
        "inlier_ratio": round(n_inl / len(good), 4) if good else 0.0,
        "n_good": len(good),
        "n_inliers": n_inl,
    }


def _illum_diff(img_a, img_b, width: int = 200, sigma: float = 2.5,
                change_thr: int = 25) -> dict:
    """灰度差双口径：原始 MAE + 低频照明差（照明状态一致性）。"""
    grays = []
    for img in (img_a, img_b):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        h = max(1, int(round(g.shape[0] * width / max(1, g.shape[1]))))
        grays.append(cv2.resize(g, (width, h), interpolation=cv2.INTER_AREA))
    a, b = grays
    raw = np.abs(a.astype(np.float32) - b.astype(np.float32))
    a_blur = cv2.GaussianBlur(a, (0, 0), sigma)
    b_blur = cv2.GaussianBlur(b, (0, 0), sigma)
    lo = np.abs(a_blur.astype(np.float32) - b_blur.astype(np.float32))
    return {"mae": round(float(raw.mean()), 3),
            "mae_lo": round(float(lo.mean()), 3),
            "frac_lo": round(float((lo > change_thr).mean()), 4)}


def _phase_shift_px(img_a, img_b, down: int) -> dict | None:
    """相位相关估计两图位移（全分辨率 px 尺度）；对重复纹理稳健，与 ORB 互相印证。"""
    gs = []
    for img in (img_a, img_b):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        gs.append(cv2.resize(g, None, fx=1.0 / down, fy=1.0 / down,
                             interpolation=cv2.INTER_AREA).astype(np.float32))
    try:
        (dx, dy), resp = cv2.phaseCorrelate(gs[0], gs[1])
    except cv2.error:
        return None
    if not np.isfinite(dx) or not np.isfinite(dy) or resp < 0.5:
        return None
    return {"dx_px": round(float(dx) * down, 2),
            "dy_px": round(float(dy) * down, 2),
            "shift_px": round(float(np.hypot(dx, dy)) * down, 2),
            "response": round(float(resp), 3)}


_BAND_PHASE_BANDS = ((0.02, 0.24), (0.38, 0.62), (0.78, 0.99))
_BAND_PHASE_X = (0.05, 0.95)


def _band_phase_shift(img_a, img_b, down: int = 2, sigma: float = 1.0) -> dict | None:
    """区域带相位位移：上/中/下三条带各跑 phaseCorrelate，取 |dy| 与 |dx| 中位（全长 px）。"""
    gs = []
    for img in (img_a, img_b):
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        r = cv2.resize(g, None, fx=1.0 / down, fy=1.0 / down,
                       interpolation=cv2.INTER_AREA)
        if sigma > 0:
            r = cv2.GaussianBlur(r, (0, 0), sigma)
        gs.append(r.astype(np.float32))
    a, b = gs
    h, w = a.shape
    x0, x1 = int(_BAND_PHASE_X[0] * w), int(_BAND_PHASE_X[1] * w)
    if x1 - x0 < 32:
        return None
    vals, bands = [], {}
    for y0f, y1f in _BAND_PHASE_BANDS:
        y0, y1 = int(y0f * h), int(y1f * h)
        pa, pb = a[y0:y1, x0:x1], b[y0:y1, x0:x1]
        if pa.shape[0] < 16 or pb.shape != pa.shape:
            continue
        win = cv2.createHanningWindow((pa.shape[1], pa.shape[0]), cv2.CV_32F)
        try:
            (dx, dy), resp = cv2.phaseCorrelate(pa * win, pb * win)
        except cv2.error:
            continue
        if not (np.isfinite(dx) and np.isfinite(dy)):
            continue
        bands[f"{y0f:.2f}-{y1f:.2f}"] = {"dx_px": round(float(dx) * down, 1),
                                         "dy_px": round(float(dy) * down, 1),
                                         "response": round(float(resp), 3)}
        vals.append(abs(float(dy) * down))
    if not vals:
        return None
    dx_vals = [abs(float(b["dx_px"])) for b in bands.values()]
    signs = {1 if b["dy_px"] >= 0 else -1 for b in bands.values()}
    dx_signs = {1 if b["dx_px"] >= 0 else -1 for b in bands.values()}
    return {"median_abs_dy_px": round(float(np.median(vals)), 1),
            "max_abs_dy_px": round(float(max(vals)), 1),
            "same_sign": len(signs) == 1,
            "median_abs_dx_px": round(float(np.median(dx_vals)), 1),
            "max_abs_dx_px": round(float(max(dx_vals)), 1),
            "min_abs_dx_px": round(float(min(dx_vals)), 1),
            "same_sign_dx": len(dx_signs) == 1 and len(dx_vals) >= 2,
            "bands": bands}


def pair_analyze(img_a, img_b, th) -> dict:
    """两图关系分析（特征匹配 + 相位相关 + 区域带相位），错位/重复拍摄共享，避免重复计算。"""
    return {"feature": _feature_similarity(img_a, img_b, th),
            "phase": _phase_shift_px(img_a, img_b,
                                     max(1, int((th or {}).get("align_orb_downscale", 3)))),
            "band": _band_phase_shift(img_a, img_b)}


def check_alignment_pair(img_a, img_b, frame_a: dict, frame_b: dict, th: dict | None = None,
                         *, pair: dict | None = None,
                         path_a: str | None = None, path_b: str | None = None) -> dict:
    """两张灯照：区域带相位走位 + 特征匹配相对位移/旋转/缩放 + 亮区质心兜底。"""
    th = _with_defaults(th)
    metric: dict = {"feature": None, "centroid_dist_px": None}
    threshold: dict = {}
    reasons = []

    # 类别互斥：若两张图文件字节完全相同（重复拷贝），归属重复拍摄，不再同时报错位
    if path_a and path_b and _file_identical(path_a, path_b).get("same_bytes"):
        metric["skipped"] = "file_identical"
        return _ok("alignment_pair", metric, threshold,
                   "两图文件字节完全相同（归属重复拍摄），跳过错位判定")

    # ---- 亮区质心（仅记录；特征不可用时兜底判定） ----
    ca = (frame_a or {}).get("centroid")
    cb = (frame_b or {}).get("centroid")
    dist_max = float(th.get("alignment_centroid_dist_max", 150.0))
    threshold["centroid_dist_max"] = dist_max
    if ca is not None and cb is not None:
        dx = cb[0] - ca[0]
        dy = cb[1] - ca[1]
        dist = float((dx ** 2 + dy ** 2) ** 0.5)
        metric["centroid_dist_px"] = round(dist, 2)
        metric["dx_px"] = round(float(dx), 2)
        metric["dy_px"] = round(float(dy), 2)
        if dist > dist_max:
            metric["centroid_over_max"] = True
    else:
        metric["dx_px"] = None
        metric["dy_px"] = None

    # ---- 特征匹配（主信号）+ 相位相关（独立印证） ----
    sim = (pair or {}).get("feature") if pair is not None \
        else _feature_similarity(img_a, img_b, th)
    ph = (pair or {}).get("phase") if pair is not None \
        else _phase_shift_px(img_a, img_b, max(1, int(th.get("align_orb_downscale", 3))))
    if ph is not None:
        metric["phase"] = ph

    # ---- 区域带相位（主判据）：直接量箱体内容的上下/水平走位 ----
    band = (pair or {}).get("band") if pair is not None else _band_phase_shift(img_a, img_b)
    if bool(th.get("alignment_band_check_enabled", True)):
        band_max = float(th.get("alignment_band_shift_max_px", 15.0))
        band_dx_max = float(th.get("alignment_band_dx_max_px", 25.0))
        band_dx_min = float(th.get("alignment_band_dx_min_band_px", 8.0))
        threshold["band_shift_max_px"] = band_max
        threshold["band_dx_max_px"] = band_dx_max
        if band is not None:
            metric["band"] = band
            if band["median_abs_dy_px"] > band_max:
                reasons.append(f"两图区域带上下位移 {band['median_abs_dy_px']:.1f}px"
                               f"（> {band_max:.0f}px），两张灯照错位")
            if (band.get("median_abs_dx_px", 0.0) > band_dx_max
                    and band.get("same_sign_dx")
                    and band.get("min_abs_dx_px", 0.0) >= band_dx_min):
                reasons.append(f"两图区域带水平位移 {band['median_abs_dx_px']:.1f}px"
                               f"（> {band_dx_max:.0f}px 且三带同向），两张灯照水平错位")

    if sim is not None:
        metric["feature"] = sim
        max_shift = float(th.get("alignment_max_shift_px", 400.0))
        dth_max = float(th.get("alignment_dtheta_max_deg", 2.0))
        scale_tol = float(th.get("alignment_scale_tol", 0.05))
        inlier_min = float(th.get("alignment_min_inlier_ratio", 0.10))
        threshold["feature_max_shift_px"] = max_shift
        threshold["feature_dtheta_max_deg"] = dth_max
        threshold["feature_scale_tol"] = scale_tol
        threshold["feature_inlier_ratio_min"] = inlier_min

        if sim["inlier_ratio"] < inlier_min:
            reasons.append(f"两图内容不一致（特征匹配内点率 "
                           f"{sim['inlier_ratio']:.1%} < {inlier_min:.0%}），"
                           "疑似不是同一根盒的两次拍摄")
        if abs(sim["scale"] - 1.0) > scale_tol:
            reasons.append(f"两图缩放差 {abs(sim['scale']-1)*100:.1f}%")

        # 位移采信条件：与质心位移一致（对称退化解防护）+ 整幅相位相关印证
        cd = metric["centroid_dist_px"] or 0.0
        sane_base = float(th.get("alignment_sane_shift_base", 300.0))
        sane_mult = float(th.get("alignment_sane_shift_mult", 3.0))
        sane_shift = max(sane_base, sane_mult * cd)
        reliable = sim["shift_px"] <= sane_shift
        agree = float(th.get("alignment_shift_agree_px", 150.0))
        threshold["feature_shift_agree_px"] = agree
        if ph is not None and abs(sim["shift_px"] - ph["shift_px"]) > agree:
            metric["feature_phase_conflict"] = True
            reliable = False
        metric["feature_unreliable"] = not reliable
        if reliable:
            if sim["shift_px"] > max_shift:
                reasons.append(f"两图相对位移 {sim['shift_px']:.0f}px（> {max_shift:.0f}px）")
            if abs(sim["theta_deg"]) > dth_max:
                reasons.append(f"两图旋转差 {abs(sim['theta_deg']):.2f}°")

    # 特征不可用/不可信时，亮区质心作为兜底判定
    if (sim is None or metric.get("feature_unreliable")) and metric.get("centroid_over_max"):
        dist = metric["centroid_dist_px"]
        reasons.append(f"亮区质心偏移 {dist:.1f}px（> {dist_max:.0f}px），"
                       "特征匹配不可用/不可信，以质心兜底判定错位")

    if reasons:
        return _fail("alignment_pair", metric, threshold,
                     "、".join(reasons) + "，拼接将错位")

    if sim is None and ca is None and band is None:
        return _ok("alignment_pair", metric, threshold,
                   "特征匹配、亮区质心与区域带相位均不可用，跳过两张间错位判定")
    return _ok("alignment_pair", metric, threshold)


# ---------------------------------------------------------------------------
# 5. 重复拍摄
# ---------------------------------------------------------------------------
def _file_identical(path_a: str, path_b: str) -> dict:
    """文件级"重复拷贝"强信号：字节数相同且首尾数据块相同 → 必为同一文件的副本。"""
    out = {"same_size": False, "same_bytes": False}
    try:
        sa, sb = os.path.getsize(path_a), os.path.getsize(path_b)
    except OSError:
        return out
    if sa != sb or sa == 0:
        return out
    out["same_size"] = True
    blk = min(65536, sa)
    try:
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            head_ok = fa.read(blk) == fb.read(blk)
            tail_ok = True
            if blk < sa:
                fa.seek(-blk, os.SEEK_END)
                fb.seek(-blk, os.SEEK_END)
                tail_ok = fa.read() == fb.read()
        out["same_bytes"] = head_ok and tail_ok
    except OSError:
        return out
    return out


def check_repeat_pair(img_a, img_b, th: dict | None = None, *,
                      path_a: str | None = None, path_b: str | None = None,
                      pair: dict | None = None) -> dict:
    """两张灯照完全相同（相机未移动 / 文件重复拷贝），独立类别。"""
    th = _with_defaults(th)
    metric: dict = {}
    threshold: dict = {}
    reasons = []
    hits = []

    illum = _illum_diff(img_a, img_b)
    metric["illum"] = illum
    metric["pair_mae"] = illum["mae"]
    illum_max = float(th.get("repeat_illum_mae_max", 10.0))
    same_inlier = float(th.get("repeat_inlier_ratio_min", 0.95))
    same_shift = float(th.get("repeat_shift_max_px", 5.0))
    threshold["repeat_illum_mae_max"] = illum_max
    threshold["repeat_inlier_ratio_min"] = same_inlier
    threshold["repeat_shift_max_px"] = same_shift

    # 1) 文件级强信号：字节完全相同 = 重复拷贝
    if path_a and path_b:
        fi = _file_identical(path_a, path_b)
        metric["file_identical"] = fi
        if fi["same_bytes"]:
            hits.append("文件字节完全相同（重复拷贝）")

    # 2) 像素级：低频照明差（始终参与，对箱体微移造成的边缘错位免疫）
    if illum["mae_lo"] <= illum_max:
        hits.append(f"低频照明差 {illum['mae_lo']:.1f} <= {illum_max:.0f}"
                    f"（两次拍摄照明状态相同；原始 MAE {illum['mae']:.1f}）")

    # 3) 特征级互补
    sim = (pair or {}).get("feature") if pair is not None \
        else _feature_similarity(img_a, img_b, th)
    if sim is not None:
        metric["feature"] = sim
        if sim["inlier_ratio"] >= same_inlier and sim["shift_px"] <= same_shift:
            hits.append(f"特征内点率 {sim['inlier_ratio']:.1%} 且位移 {sim['shift_px']:.0f}px")

    if hits:
        reasons.append("两张灯照相同（" + "；".join(hits) +
                       "），疑似重复拍摄/相机未移动，无法拼接")
    if reasons:
        return _fail("repeat", metric, threshold, "；".join(reasons))
    return _ok("repeat", metric, threshold, "两张灯照照明状态不同，非重复拍摄")


# ---------------------------------------------------------------------------
# 6. 单张内部错位（拼接成品图：自适应找缝 + 自洽缝组 + 固定缝窗兜底）
# ---------------------------------------------------------------------------
_MOSAIC_W0 = 5472.0   # 标定基准宽度：缝位按 W/5472 缩放定位（成品图 5472×3648 横置）
_MOSAIC_LAYOUT_NAMES = tuple(MOSAIC_LAYOUTS.keys())
_MOSAIC_LAYOUTS = tuple(MOSAIC_LAYOUTS.values())

_COLG_CACHE: list = []   # 最近处理的灰度图 |Sobel-x| 列均值缓存，避免同图算 3 次全幅卷积


def _col_abs_gx_mean(gray: np.ndarray) -> np.ndarray:
    """计算全幅 |Sobel-x| 列均值，同一 gray 对象复用结果。"""
    for g_ref, shape, colg in _COLG_CACHE:
        if g_ref is gray and shape == gray.shape:
            return colg
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    colg = np.abs(gx).mean(axis=0)
    if len(_COLG_CACHE) >= 2:
        _COLG_CACHE.pop(0)
    _COLG_CACHE.append((gray, gray.shape, colg))
    return colg


def _find_window_seams(gray, th: dict | None = None) -> list:
    """在两套产线刀口窗口内找贯穿全高的强垂直梯度峰（固定布局兜底）。"""
    th = _with_defaults(th)
    win = int(th.get("alignment_seam_window", 30))
    min_ratio = float(th.get("alignment_seam_min_peak_ratio", 2.0))
    colg = _col_abs_gx_mean(gray)
    W = len(colg)
    base = float(colg.mean())
    if base <= 0:
        return []
    s = W / _MOSAIC_W0
    nms = max(2, int(round(float(th.get("alignment_adaptive_nms", 150.0)) * s)))
    found = []
    for li, trio in enumerate(_MOSAIC_LAYOUTS):
        for x0 in trio:
            c = int(round(x0 * s))
            lo, hi = max(0, c - win), min(W, c + win + 1)
            if hi - lo < 5:
                continue
            seg = colg[lo:hi]
            pk = float(seg.max())
            if pk >= min_ratio * base:
                found.append((int(lo + int(np.argmax(seg))), round(pk / base, 2), li))
    found.sort(key=lambda t: -t[1])         # 峰比高者优先，去重保留最强的
    kept = []
    for x, r, li in found:
        if all(abs(x - y) >= nms for y, _r, _l in kept):
            kept.append((x, r, li))
    return sorted(kept)


def _layout_match_count(xs, W: int, th: dict | None = None) -> int:
    """自适应缝位与两套产线刀口的最大匹配条数。"""
    th = _with_defaults(th)
    if not xs:
        return 0
    s = W / _MOSAIC_W0
    tol = float(th.get("alignment_layout_match_tol", 60.0)) * s
    return max(sum(1 for y in trio if any(abs(x - y * s) <= tol for x in xs))
               for trio in _MOSAIC_LAYOUTS)


def _find_seam_candidates(gray, th: dict | None = None) -> list:
    """全幅自适应找缝：贯穿全高的垂直梯度峰，不依赖产线刀口位置。"""
    th = _with_defaults(th)
    min_ratio = float(th.get("alignment_adaptive_peak_min_ratio", 1.5))
    colg = _col_abs_gx_mean(gray)
    W = len(colg)
    if W < 8:
        return []
    base = float(np.median(colg))
    if base <= 0:
        return []
    ratio = colg / base
    s = W / _MOSAIC_W0
    nms = max(2, int(round(float(th.get("alignment_adaptive_nms", 150.0)) * s)))
    topk = int(th.get("alignment_adaptive_topk", 8))
    peaks = [x for x in range(2, W - 2)
             if ratio[x] >= min_ratio and ratio[x] >= ratio[x - 1] and ratio[x] >= ratio[x + 1]]
    peaks.sort(key=lambda x: -float(ratio[x]))
    kept: list = []
    for x in peaks:
        if all(abs(x - y) >= nms for y in kept):
            kept.append(x)
        if len(kept) >= topk:
            break
    return [(int(x), round(float(ratio[x]), 2)) for x in sorted(kept)]


def _seam_chain(hits: list, th: dict | None = None, gap: float = 200.0) -> tuple:
    """最长"自洽缝组"：符号交替 + 幅度相近 + 间距足够的命中序列（DP）。"""
    th = _with_defaults(th)
    tol = float(th.get("alignment_adaptive_ratio_tol", 1.7))
    n = len(hits)
    if n == 0:
        return [], 0
    dp = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            a, b = hits[j], hits[i]
            if (b["x"] - a["x"]) < gap or (a["dy"] > 0) == (b["dy"] > 0):
                continue
            lo, hi = sorted((abs(a["dy"]), abs(b["dy"])))
            if hi > tol * lo:
                continue
            if dp[j] + 1 > dp[i]:
                dp[i] = dp[j] + 1
                prev[i] = j
    k = max(range(n), key=lambda i: dp[i])
    best: list = []
    while k >= 0:
        best.append(hits[k])
        k = prev[k]
    best.reverse()
    return best, dp[max(range(n), key=lambda i: dp[i])]


def _seam_dy(gray, xb: int, th: dict | None = None) -> tuple:
    """缝两侧竖带行均值剖面的最佳纵向平移（dy px, 相关系数）。"""
    th = _with_defaults(th)
    b_in, b_out = th.get("alignment_seam_dy_bands", (30, 90))
    span = int(th.get("alignment_seam_dy_span", 120))
    min_ov = int(th.get("alignment_seam_dy_min_overlap", 500))
    W = gray.shape[1]
    la, ra = max(0, xb - b_out), max(0, xb - b_in)
    lb, rb = min(W, xb + b_in), min(W, xb + b_out)
    if ra - la < 2 or rb - lb < 2:      # 缝位贴边：取带放不下，视为不可测
        return 0, -2.0
    pa = gray[:, la:ra].mean(axis=1).astype(np.float64)
    pb = gray[:, lb:rb].mean(axis=1).astype(np.float64)
    n = min(len(pa), len(pb))
    pa, pb = pa[:n], pb[:n]
    best = (-2.0, 0)
    for dy in range(-span, span + 1):
        if dy >= 0:
            a, b = pa[:n - dy], pb[dy:]
        else:
            a, b = pa[-dy:], pb[:n + dy]
        if len(a) < min_ov:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if c > best[0]:
            best = (c, dy)
    return best[1], round(best[0], 4)


def _window_hits_consistent(hits: list, threshold: dict) -> bool:
    """固定缝窗兜底的自洽校验：>=3 条各自成立；恰好 2 条要求两条读数自洽。"""
    if len(hits) >= 3:
        return True
    if len(hits) < 2:
        return False
    a, b = hits[0], hits[1]
    if a.get("layout") != b.get("layout"):
        return True
    pos = _MOSAIC_LAYOUTS[_MOSAIC_LAYOUT_NAMES.index(a["layout"])]
    ia = min(range(len(pos)), key=lambda i: abs(pos[i] - a["x"]))
    ib = min(range(len(pos)), key=lambda i: abs(pos[i] - b["x"]))
    same_sign = (a["dy"] > 0) == (b["dy"] > 0)
    if same_sign != (abs(ia - ib) % 2 == 0):
        return False
    hi, lo = max(abs(a["dy"]), abs(b["dy"])), min(abs(a["dy"]), abs(b["dy"]))
    return hi <= lo * threshold["seam_pair_ratio_tol"]


def check_alignment_single(img_bgr, gray, frame, th: dict | None = None) -> dict:
    """单张内部错位（拼接成品图）：拼缝两侧同一根盒的纵向落差。"""
    th = _with_defaults(th)
    metric: dict = {"candidates": [], "hits": [], "chain": [], "window_seams": []}
    threshold: dict = {
        "seam_dy_min": float(th.get("alignment_seam_dy_min", 9.0)),
        "seam_dy_span": int(th.get("alignment_seam_dy_span", 120)),
        "chain_corr_min": float(th.get("alignment_adaptive_corr_min", 0.30)),
        "chain_min": int(th.get("alignment_adaptive_chain_min", 3)),
        "chain_ratio_tol": float(th.get("alignment_adaptive_ratio_tol", 1.7)),
        "seam_hits_min": int(th.get("alignment_seam_hits_min", 2)),
        "seam_pair_ratio_tol": float(th.get(
            "alignment_seam_pair_ratio_tol",
            th.get("alignment_adaptive_ratio_tol", 1.7))),
        "layout_min_matches": int(th.get("alignment_seam_layout_min_matches", 3)),
    }
    if not th.get("alignment_single_check_enabled", True):
        return _ok("alignment_single", metric, threshold, "单张错位判据已禁用")
    if th.get("input_kind") == "raw":
        metric["alignment_single_check"] = "skipped_raw"
        return _ok("alignment_single", metric, threshold,
                   "原图模式（input_kind=raw），跳过单张拼接缝错位判定")
    if gray is None:
        return _ok("alignment_single", metric, threshold, "无灰度图，跳过单张错位判定")
    dy_min = threshold["seam_dy_min"]
    span = threshold["seam_dy_span"]
    s = gray.shape[1] / _MOSAIC_W0
    for x, r in _find_seam_candidates(gray, th):
        dy, corr = _seam_dy(gray, x, th)
        metric["candidates"].append({"x": x, "peak_ratio": r, "dy": dy, "corr": corr})
    hits = [c for c in metric["candidates"]
            if dy_min <= abs(c["dy"]) < span and c["corr"] >= threshold["chain_corr_min"]]
    metric["hits"] = [{"x": c["x"], "dy": c["dy"], "corr": c["corr"]} for c in hits]
    chain, clen = _seam_chain(hits, th, float(th.get("alignment_adaptive_min_gap", 200.0)) * s)
    metric["chain"] = [{"x": c["x"], "dy": c["dy"], "corr": c["corr"]} for c in chain]
    metric["chain_len"] = clen
    if clen >= threshold["chain_min"]:
        return _fail("alignment_single", metric, threshold,
                     "自适应缝组 %s 落差 %s（符号交替、幅度相近，%d 条 >= %.0fpx），"
                     "同一根盒在缝两侧上下错位"
                     % ("/".join(str(c["x"]) for c in chain),
                        "/".join("%+d" % c["dy"] for c in chain), clen, dy_min))
    measured = {c["x"]: c for c in metric["candidates"]}
    whits = []
    for x, r, li in _find_window_seams(gray, th):
        c = measured.get(x)
        if c is None:
            dy, corr = _seam_dy(gray, x, th)
            c = {"x": x, "peak_ratio": r, "dy": dy, "corr": corr}
        c = dict(c, layout=_MOSAIC_LAYOUT_NAMES[li])
        metric["window_seams"].append(c)
        if abs(c["dy"]) >= dy_min:
            whits.append(c)
    metric["seam_hits"] = len(whits)
    layout_n = _layout_match_count([c["x"] for c in metric["candidates"]], gray.shape[1], th)
    metric["layout_matches"] = layout_n
    if (layout_n >= threshold["layout_min_matches"]
            and len(metric["window_seams"]) >= 2
            and len(whits) >= threshold["seam_hits_min"]
            and _window_hits_consistent(whits, threshold)):
        worst = max(whits, key=lambda d: abs(d["dy"]))
        return _fail("alignment_single", metric, threshold,
                     f"固定缝窗 {len(whits)}/{len(metric['window_seams'])} 条拼接缝两侧垂直落差"
                     f" >= {dy_min:.0f}px（{'/'.join('%+d' % c['dy'] for c in whits)}"
                     f" @ {'/'.join(str(c['x']) for c in whits)}，"
                     f"符号交替/幅度相近，最大 {abs(worst['dy']):.0f}px @x={worst['x']}），"
                     "同一根盒在缝两侧上下错位")
    max_dy = max((abs(c["dy"]) for c in metric["candidates"]), default=0)
    return _ok("alignment_single", metric, threshold,
               f"自适应缝组最长 {clen} 条（需 >= {threshold['chain_min']}）、"
               f"固定缝窗越线 {len(whits)}/{len(metric['window_seams'])} 条"
               f"（需 >= {threshold['seam_hits_min']} 条且自洽），候选最大落差 {max_dy:.0f}px")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _ok(name: str, metric, threshold, note: str = "") -> dict:
    return {"name": name, "passed": True, "status": "pass", "metric": metric,
            "threshold": threshold, "reason": note}


def _fail(name: str, metric, threshold, reason: str) -> dict:
    return {"name": name, "passed": False, "status": "fail", "metric": metric,
            "threshold": threshold, "reason": reason}


def _undetermined(name: str, metric, threshold, reason: str) -> dict:
    """三态中的"判不了"：不是合格，也不是确定不合格；默认按重拍处理。"""
    return {"name": name, "passed": False, "status": "undetermined", "metric": metric,
            "threshold": threshold, "reason": reason}
