"""全局配置：路径 / 阈值 / 配对规则 / 检测参数 / 通知方式。

生产使用说明
============
- 所有阈值集中在 THRESHOLDS，现场标定只改这里（或通过 run(*, thresholds=...) 临时覆盖）。
- 支持环境变量 ROOTBOX_QC_INPUT_DIR 覆盖拍照落盘目录（部署脚本用）。
- 任何 yaml / 环境变量均可选；不配置就用下面的默认值。
"""
from __future__ import annotations

import os

# ============================================================================
# 路径
# ============================================================================
PATHS = {
    # 待质检图片目录（拍照小车落盘的位置）
    # 支持四种结构（runner.collect_pairs 自动识别）：
    #   1) 平铺：<dir>/<num>_1.jpg + <num>_2.jpg
    #   2) 单层：<dir>/<编号>/1.png + <编号>/2.png
    #   3) 双层：<dir>/<类别>/<编号>/1.png + <编号>/2.png
    #   4) 根目录无图：自动汇总所有含图的子目录
    "input_dir": r"D:\yy1",          # ← 部署时改成实际目录（也可用环境变量 ROOTBOX_QC_INPUT_DIR）

    # 报告输出目录（每对一张 JSON）
    "output_dir": "./results",

    # 待重拍队列文件（外部系统/小车读取后触发重拍；jsonl 追加模式，自动轮转）
    "recapture_queue": "./results/recapture_queue.jsonl",

    # 运行日志目录
    "log_dir": "./logs",
    "stitch_output_dir": os.environ.get("ROOTBOX_QC_STITCH_DIR") or None,

    # 无缝拼接脚本路径（可选功能；启用 stitch_output_dir 时必须存在且可加载）
    # 脚本需提供 stitch_from_qc(img_a, img_b, out_path) 函数；部署时改成目标机路径
    "stitch_script": (os.environ.get("ROOTBOX_QC_STITCH_SCRIPT")
                      or r"d:\tupianpj\图片拼接2.py"),
}

# ============================================================================
# 图片配对规则
# ============================================================================
PAIRING = {
    # 平铺命名的正则（仅平铺结构时生效）
    # 默认匹配 <前缀>_<序号>.jpg，如 root_001_1.jpg + root_001_2.jpg
    "regex": r"^(?P<prefix>.+)_(?P<idx>[0-9]+)\.(?P<ext>jpg|jpeg|png|bmp)$",
}

# ============================================================================
# 产线拼接刀口配置（原图 px @5472 宽；图1 存活列=[c1,c2)∪[c3,W)，图2 存活列=[0,c1)∪[c2,c3)）
#   C 线（现行，2026-10-29 起）：2200 / 2780 / 3300
#   B 线（旧 dataed 平图）    ：1950 / 2500 / 3050
#   A 线（旧 data 池平图）    ：2000 / 2700 / 3300
#
# 注意：data 里混有两套相机角度，C 只对应现行角度；旧角度图用 layout="A"/"B"
# 显式切换整套刀口（模糊存活列、单张缝位、固定缝窗一起变），不要只改
# blur_strip_cuts，否则单张错位仍按 C 找缝。等现场角度统一后只保留 C。
# ============================================================================
MOSAIC_LAYOUTS = {
    "C": (2200, 2780, 3300),
    "B": (1950, 2500, 3050),
    "A": (2000, 2700, 3300),
}

# ============================================================================
# 检测阈值（5 类：损坏 / 模糊 / 不全 / 错位 / 重复拍摄；现场标定只改这里！）
# ============================================================================
THRESHOLDS = {
    # 输入图类型："auto"（默认，兼容原图与成品图混合扫描）| "raw"（相机原图：跳过单张拼缝检查）
    #           | "mosaic"（拼接成品平图：跳过原图存活列模糊判定）
    # 线上拍摄即检必须显式传 input_kind="raw"；auto 只用于离线混合目录。
    "input_kind": "auto",
    "auto_rotate_landscape": True,

    # 分辨率守卫：现行相机原图 5472×3648（竖拍旋转后同样落到这个尺寸）。
    # 非该尺寸不缩放刀口、不硬猜 input_kind，直接判"无法判定"并默认重拍。
    "resolution_guard_enabled": True,
    "expected_width": 5472,
    "expected_height": 3648,

    # ----- 1. 图片损坏（单张 + 成对单灯管照明） -----
    "corruption_min_filesize": 100_000,      # 字节下限；过小视为未完整写入/损坏
    "corruption_min_bytes_per_mpx": 5000.0,  # 每百万像素字节数下限（按分辨率自适应）
    "corruption_gray_std_min": 15.0,         # 灰度标准差下限；过低视为近纯色（全黑/全白）
    "corruption_block_rows": 6,              # 局部纯黑/极暗分块行数
    "corruption_block_cols": 8,              # 局部纯黑/极暗分块列数
    "corruption_dark_blocks_min": 4,         # 局部纯黑块下限（mean<12 且 std<8）
    "corruption_dim_blocks_min": 4,          # 极暗无纹理块下限（mean<25 且 std<12）
    "corruption_dim_bright_min": 60.0,       # 极暗与亮区并存：最亮块 mean 下限

    # 单灯管/单侧照明缺失（成对，归损坏）：同一次拍摄两帧照明严重不对称
    "illum_pair_check_enabled": True,
    "illum_pair_bright_dark_max": 0.10,      # 暗图亮像素(>90)占比上限
    "illum_pair_bright_ratio_min": 0.25,     # 正常图亮像素(>90)占比下限

    # ----- 2. 模糊（单张 ROI 方差 + 角色感知拼接存活列 + 成对锐度不对称） -----
    "blur_var_min": 20.0,                    # 亮区 ROI Laplacian 方差下限（兜底全糊与散装成品图）
    "blur_strip_metric_enabled": True,
    "blur_strip_check_enabled": True,
    "blur_strip_alive_var_min": 46.0,        # 角色对应的反光侧存活列 Laplacian 方差下限
    "blur_strip_surviving_var_min": 26.5,    # 角色对应的无反光取片列 Laplacian 方差下限
    # layout=None 表示按现行 C 线；切旧角度时设 layout="A"/"B"，整套刀口一起切。
    # blur_strip_cuts 仅作为自定义覆盖：layout 非空时以 layout 为准。
    "layout": None,
    "blur_strip_cuts": [2200, 2780, 3300],

    # 成对模糊（一张锐一张糊：灯照拖影/轻微失焦）
    "blur_pair_check_enabled": True,
    "blur_pair_roi_var_max": 25.5,           # 糊图亮区 Laplacian 方差上限
    "blur_pair_ratio_min": 2.2,              # 两张方差比下限

    # ----- 3. 拍摄不全（亮像素占比上限 + 底部金属长边结构缺失 Rule A） -----
    "completeness_bright_ratio_max": 0.30,         # 亮像素占比上限（超过视为拍到支架/贴脸）
    "completeness_longedge_check_enabled": True,
    "completeness_longedge_profile_width": 512,    # 剖面下采样宽度（px）
    "completeness_longedge_grad_thresh": 40.0,     # Sobel 垂直梯度绝对值门限
    "completeness_longedge_pos_thr": 0.25,         # 强长边挺直度门限（该行强梯度列占比）
    "completeness_longedge_bot_zone_start": 0.85,  # Rule A 底部结构带检查区间起点 [0.85, 1.0]
    "completeness_dark_mean_max": 65.0,            # 暗图守卫：全图灰度均值低于此值视为暗帧，豁免 Rule A

    # ----- 4. 错位（成对：区域带相位为主 + ORB 特征匹配 + 质心兜底） -----
    "alignment_band_check_enabled": True,
    "alignment_band_shift_max_px": 15.0,     # 三条横带相位上下位移 |dy| 中位数上限（px）
    "alignment_band_dx_max_px": 25.0,        # 三条横带相位水平位移 |dx| 中位数上限（px，需三带同向）
    "alignment_band_dx_min_band_px": 8.0,    # 水平错位触发时单带最小 |dx| 下限（px）
    "alignment_centroid_dist_max": 150.0,    # 两图亮区质心距离上限（px，特征不可靠时兜底）
    "alignment_dtheta_max_deg": 2.0,         # 两图旋转差上限（度）
    "alignment_scale_tol": 0.05,             # 两图缩放相对偏差上限（5.0% 内视作正常抖动）
    "alignment_max_shift_px": 400.0,         # ORB 特征相对位移上限（px）
    "alignment_min_inlier_ratio": 0.10,      # ORB 特征匹配内点率下限
    "alignment_min_good_matches": 12,        # 有效匹配对数下限
    "align_orb_nfeatures": 4000,             # ORB 特征点数量上限
    "align_orb_downscale": 3,                # ORB 匹配前的降采样倍数
    "align_orb_min_keypoints": 50,           # ORB 特征点数下限
    "alignment_sane_shift_base": 300.0,      # 位移采信门限基线（px）
    "alignment_sane_shift_mult": 3.0,        # 位移采信门限 = max(基线, 倍数 * 质心位移)
    "alignment_shift_agree_px": 150.0,       # 特征位移与整幅相位位移的最大允许偏差（px）

    # ----- 5. 错位（单张内部，拼接成品图：自适应找缝 + 自洽缝组 + 固定缝窗兜底） -----
    "alignment_single_check_enabled": True,
    "alignment_adaptive_peak_min_ratio": 1.5,    # 候选峰比下限（列梯度 / 中位数）
    "alignment_adaptive_nms": 150.0,             # 候选最小间距（px @5472 宽，按宽缩放）
    "alignment_adaptive_topk": 8,                # 每图保留候选数上限
    "alignment_adaptive_corr_min": 0.30,         # 入链最低相关系数
    "alignment_adaptive_chain_min": 3,           # 自洽缝组最短链长
    "alignment_adaptive_ratio_tol": 1.7,         # 链内相邻幅度比上限
    "alignment_adaptive_min_gap": 200.0,         # 链内相邻缝最小间距（px @5472 宽）
    "alignment_layout_match_tol": 60.0,          # 缝位→产线刀口匹配容差（px @5472 宽）
    "alignment_seam_window": 30,                 # 固定缝窗搜索半窗（px）
    "alignment_seam_min_peak_ratio": 2.0,        # 固定缝窗列梯度峰 / 画面平均列梯度 下限
    "alignment_seam_dy_min": 9.0,                # 单缝纵向落差阈值（px）
    "alignment_seam_hits_min": 2,                # 固定缝窗至少几条缝越线判错位
    "alignment_seam_pair_ratio_tol": 1.7,        # 固定缝窗恰好 2 条越线时的幅度比上限
    "alignment_seam_layout_min_matches": 3,      # 固定缝窗生效前提：自适应缝位匹配该产线刀口数
    "alignment_seam_dy_span": 120,               # 缝两侧平移相关搜索半径（px）
    "alignment_seam_dy_bands": (30, 90),         # 缝左/右取带：内边界 30px、外边界 90px
    "alignment_seam_dy_min_overlap": 500,        # 平移后重叠行数下限

    # ----- 6. 重复拍摄（文件级字节一致 + 低频照明差 + 特征高内点零位移） -----
    "repeat_illum_mae_max": 10.0,            # 低频照明差上限（同一次照明状态下的重拍）
    "repeat_inlier_ratio_min": 0.95,         # 特征内点率下限（与 repeat_shift_max_px 联合判定）
    "repeat_shift_max_px": 5.0,              # 位移上限（px）
}

# ============================================================================
# 亮金属结构检测参数
# ============================================================================
FRAME_DETECT = {
    "bright_threshold": 130,        # 灰度大于此值视为金属（亮区）
}

# ============================================================================
# 重拍管制（线上拍摄即检与批量运行共用）
# ============================================================================
RECAPTURE = {
    "max_attempts": 3,
    "max_consecutive_same_category": 5,
}

# ============================================================================
# 通知适配器
# ============================================================================
NOTIFIER = {
    "type": "file",
    "http": {
        "url": "http://127.0.0.1:8080/qc/recapture",  # 小车地址（占位）
        "timeout": 5.0,
        "retries": 2,
        "notify_on_pass": False,
    },
}


# ============================================================================
# 加载逻辑
# ============================================================================
def _env_overrides() -> dict:
    """环境变量覆盖（部署用，优先级高于默认值、低于显式参数）。"""
    out = {}
    if os.environ.get("ROOTBOX_QC_INPUT_DIR"):
        out.setdefault("paths", {})["input_dir"] = os.environ["ROOTBOX_QC_INPUT_DIR"]
    return out


def load_config(path: str | None = None) -> dict:
    """返回完整配置字典（默认值 + 可选 yaml + 可选环境变量覆盖）。"""
    cfg = {
        "paths": dict(PATHS),
        "pairing": dict(PAIRING),
        "thresholds": dict(THRESHOLDS),
        "frame_detect": dict(FRAME_DETECT),
        "recapture": dict(RECAPTURE),
        "notifier": dict(NOTIFIER),
    }

    if path is not None:
        try:
            import yaml
        except ImportError:
            yaml = None
        if yaml is not None and os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            cfg = _merge(cfg, user_cfg)

    return _merge(cfg, _env_overrides())


def _merge(defaults: dict, override: dict) -> dict:
    out = dict(defaults)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def get_thresholds(cfg: dict) -> dict:
    return cfg["thresholds"]


def get_frame_params(cfg: dict) -> dict:
    return cfg["frame_detect"]
