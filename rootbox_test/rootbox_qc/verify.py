"""质检编排：加载图片、跑检测、生成结构化报告。兼容中文路径。

线上（拍摄即检）接口：
    verify_single(path, config_or_thresholds)  # 拍完一张立即校验，不合格当场重拍该张
    verify_pair(path_a, path_b, ...)           # 两张单图 -> 成对 -> 拼接 -> 成品复检

生产流程（2026-10-29 确认）：两张单图任一未过立即返回，不拼接；两张都过后才做
成对比对，再按 paths.stitch_script 拼接，并对成品按 mosaic 重跑一遍单图检查。
配置 paths.stitch_output_dir（或环境变量 ROOTBOX_QC_STITCH_DIR）才启用成品复检；
verify_stitched=True 可强制要求，缺少目录/成品按 undetermined 阻塞。

离线（批量）接口：
    run(input_dir, ...)                        # 扫描目录，见 runner.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import time
from dataclasses import dataclass, field, asdict

import cv2
import numpy as np

from . import checks, frame as frame_mod
from .config import load_config, get_thresholds, get_frame_params

logger = logging.getLogger("rootbox_qc")


@dataclass
class ImageResult:
    path: str
    checks: list = field(default_factory=list)
    load_error: str | None = None
    input_kind: str | None = None   # 本结果按哪种模态算出来的，复用前必须核对

    @property
    def status(self) -> str:
        """三态：pass / fail / undetermined（任一确定失败优先于判不了）。"""
        if self.load_error is not None:
            return "fail"
        states = [c.get("status", "pass" if c["passed"] else "fail")
                  for c in self.checks]
        if "fail" in states:
            return "fail"
        if "undetermined" in states:
            return "undetermined"
        return "pass"

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass
class QCReport:
    pair_id: str
    images: list = field(default_factory=list)
    alignment_pair: dict | None = None
    repeat_pair: dict | None = None   # 两张灯照完全相同（重复拍摄），独立类别
    blur_pair: dict | None = None     # 两张里一张锐一张糊（灯照拖影/失焦），独立类别
    illumination_pair: dict | None = None  # 单灯管/单侧照明缺失（损坏）
    recapture_needed: bool = False
    recapture_reasons: list = field(default_factory=list)
    recapture_targets: list = field(default_factory=list)
    status: str = "pass"              # pass / fail / undetermined（判不了默认重拍）
    attempt: int = 1
    max_attempts: int = 2
    escalate_manual: bool = False     # attempt >= max_attempts 且仍不合格时转人工/停机
    systemic_alert: str | None = None # 连续多组命中同类故障时的系统性熔断告警
    stitched_path: str | None = None  # 质检通过后内存直传无缝拼接的输出路径（若启用）
    stitch_error: str | None = None   # 拼接脚本缺失/执行失败时的明确错误
    stitched_status: str | None = None  # 拼接成品复检的三态；None=未启用/未执行
    stitched_checks: list = field(default_factory=list)  # 拼接成品上的单图检查明细
    # 同一 pair 的历次重拍摘要；由 RecaptureTracker.observe() 合并。
    # 不直接进报告顶层（to_dict 会取出并整理成 retry 块），避免与 retry 重复。
    attempt_history: list = field(default_factory=list, repr=False)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def retry_report(self) -> dict:
        """重拍后报告：重拍次数、首末状态、每次原因与故障类别。

        以 attempt=1 为首次拍摄，retake_count = attempt-1。即使调用方没有使用
        RecaptureTracker，也会把当前 attempt 作为唯一一条历史，字段仍可用；
        但只有复用同一个 tracker 并逐次 observe，历史才是完整的
        （history_complete=True）。历史不完整时 first_* 是最早一条已记录，
        不一定是真正的首拍。
        """
        attempts = [dict(a) for a in self.attempt_history
                    if int(a.get("attempt", 0) or 0) != int(self.attempt or 1)]
        attempts.append(_attempt_summary(self))
        attempts.sort(key=lambda a: int(a.get("attempt", 0) or 0))
        first = attempts[0]
        attempt = int(self.attempt or 1)
        ordered = [n for n in CATEGORY_ORDER
                   if any(n in a.get("categories", []) for a in attempts)]
        extra = sorted({c for a in attempts for c in a.get("categories", [])
                        if c not in CATEGORY_ORDER})
        retake_count = max(0, attempt - 1)
        return {
            "pair_id": self.pair_id,
            "retake_count": retake_count,
            "attempts_total": len(attempts),
            "max_attempts": self.max_attempts,
            "history_complete": (
                len(attempts) == attempt
                and int(first.get("attempt", 0) or 0) == 1
            ),
            "first_status": first.get("status"),
            "final_status": self.status,
            "resolved": self.status == "pass",
            "retake_succeeded": retake_count > 0 and self.status == "pass",
            "escalated": bool(self.escalate_manual),
            "first_reasons": list(first.get("reasons", [])),
            "final_reasons": list(self.recapture_reasons),
            "failure_categories": ordered + extra,
            "attempts": attempts,
            "systemic_alert": self.systemic_alert,
            "first_timestamp": first.get("timestamp"),
            "final_timestamp": self.timestamp,
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("attempt_history", None)   # 只通过 retry.attempts 暴露，避免重复
        d["status"] = self.status
        d["passed"] = self.status == "pass"
        # 对外文档契约（README）：重拍队列 / HTTP 消费方读 reasons / targets，
        # 与内部字段名 recapture_reasons / recapture_targets 保持一致别名
        d["reasons"] = self.recapture_reasons
        d["targets"] = self.recapture_targets
        d["retry"] = self.retry_report()
        return d


def load_image(path: str, *, auto_rotate: bool = True):
    """读取图像（兼容中文路径）。返回 (img_bgr, gray)；失败返回 (None, None)。

    当 auto_rotate=True 且输入为实拍大图竖构图（H > W 且短边 >= 2000px）时，
    自动顺时针旋转 90° 统一为横置构图（与无缝拼接模块保持一致）。
    """
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取失败 %s: %s", path, e)
        return None, None
    if img is None:
        logger.warning("解码失败（文件损坏/格式错误）: %s", path)
        return None, None
    if auto_rotate and img.shape[0] > img.shape[1] and min(img.shape[0], img.shape[1]) >= 2000:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if img.ndim == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img
    return img, gray


def _resolve_single_input_kind(path: str | None, gray: np.ndarray | None, th: dict) -> str:
    """当 input_kind='auto' 时，为单张图片自动推断模态（'raw' | 'mosaic' | 'auto'）。

    判据：
      1) 文件名主干为 1 / 2 或以 _1 / _2 结尾 → 原图 'raw'；
      2) 包含已知产线拼接缝特征（_layout_match_count >= 2）→ 成品平图 'mosaic'；
      3) 实拍大图（宽 >= 2000）按双光源条带反光不对称性识别：
         单灯原图在 stripA 与 stripB 之间的高光(>220)占比高度不对称（一侧 >=1.5% 且 >= 另一侧 4 倍）；
         而拼接成品图已去除单侧反光带且文件名非 1/2 → 'mosaic'。
    """
    kind = th.get("input_kind", "auto")
    if kind in ("raw", "mosaic"):
        return kind
    if path is not None:
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        if stem in ("1", "2") or stem.endswith("_1") or stem.endswith("_2"):
            return "raw"
    if gray is not None:
        try:
            cands = [c[0] for c in checks._find_seam_candidates(gray, th)]
            if checks._layout_match_count(cands, gray.shape[1], th) >= 2:
                return "mosaic"
        except Exception:  # noqa: BLE001
            pass
        if min(gray.shape[:2]) >= 2000:
            g = gray if gray.shape[0] <= gray.shape[1] else cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE)
            w = g.shape[1]
            cuts = checks._active_cuts(th)
            if len(cuts) >= 3 and 0 < cuts[0] < cuts[1] < cuts[2] < w:
                c1, c2, c3 = int(cuts[0]), int(cuts[1]), int(cuts[2])
                ga = float((np.concatenate([g[:, 0:c1], g[:, c2:c3]], axis=1) > 220).mean())
                gb = float((np.concatenate([g[:, c1:c2], g[:, c3:w]], axis=1) > 220).mean())
                hi, lo = max(ga, gb), min(ga, gb)
                if hi >= 0.015 and hi >= 4.0 * max(lo, 1e-4):
                    return "raw"
                return "mosaic"
    return "auto"


_STITCH_MOD = None
_STITCH_MOD_PATH = None


def _load_stitch_module(stitch_script: str):
    """按路径加载无缝拼接脚本；脚本路径变化时重新加载。"""
    global _STITCH_MOD, _STITCH_MOD_PATH
    script_abs = os.path.abspath(stitch_script)
    if _STITCH_MOD is not None and _STITCH_MOD_PATH == script_abs:
        return _STITCH_MOD
    spec = importlib.util.spec_from_file_location("tupian_stitch", script_abs)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载无缝拼接脚本: {script_abs}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _STITCH_MOD = mod
    _STITCH_MOD_PATH = script_abs
    return mod


def _stitch_in_memory(img_a: np.ndarray, img_b: np.ndarray, pair_id: str,
                      stitch_output_dir: str, stitch_script: str,
                      out_name: str | None = None) -> str:
    """质检通过后直接将内存中已解码的 img_a, img_b 送入无缝拼接模块。

    out_name 指定成品文件名（不含扩展名）；默认用 pair_id。落图目录是
    <watch>/<批次>/<编号>/ 时，线上约定成品是 save/<批次>/<编号>.png。
    """
    if not stitch_script or not os.path.isfile(stitch_script):
        raise FileNotFoundError(
            f"无缝拼接脚本不存在: {stitch_script!r}"
            "（配置 paths.stitch_script 或环境变量 ROOTBOX_QC_STITCH_SCRIPT）")
    mod = _load_stitch_module(stitch_script)
    os.makedirs(stitch_output_dir, exist_ok=True)
    out_path = os.path.join(stitch_output_dir, f"{out_name or pair_id}.png")
    res_path = mod.stitch_from_qc(img_a, img_b, out_path)
    return res_path if res_path else out_path


def _run_single_checks(path, img, gray, frame, th) -> ImageResult:
    res = ImageResult(path=path, input_kind=th.get("input_kind"))
    res.checks.append(checks.check_corruption(path=path, img_bgr=img, th=th))
    if img is None or gray is None:
        res.load_error = "decode_failed"
        return res

    # 分辨率守卫：现行相机原图只有 5472×3648（竖拍旋转后同尺寸）。
    # 非该尺寸不缩放刀口、不硬猜 input_kind，直接判"无法判定"（默认重拍）。
    if bool(th.get("resolution_guard_enabled", True)):
        h, w = gray.shape
        ew = int(th.get("expected_width", 5472))
        eh = int(th.get("expected_height", 3648))
        if sorted((w, h)) != sorted((ew, eh)):
            res.checks.append(checks._undetermined(
                "resolution",
                {"width": int(w), "height": int(h)},
                {"expected_width": ew, "expected_height": eh},
                f"分辨率 {w}x{h} 不是 {ew}x{eh}，刀口/判据未按该尺寸标定"))
            return res

    res.checks.append(checks.check_blur(img, gray, frame, th, path=path))
    res.checks.append(checks.check_completeness(img, gray, frame, th))
    res.checks.append(checks.check_alignment_single(img, gray, frame, th))
    return res


def _reuse_matches_kind(prev: ImageResult, expected_kind: str) -> bool:
    """复用 verify_single 结果前核对模态；猜成 mosaic 就重算，避免永久漏 blur。"""
    return prev is not None and prev.input_kind == expected_kind


# 特征检测缓存：只缓存 frame 结果（小 dict，不缓存大图矩阵，避免内存爆炸）
# 键 = (规范化路径, mtime, size, frame_detect 参数哈希)，容量上限防止常驻进程无限增长
_FRAME_CACHE: dict = {}
_FRAME_CACHE_MAX = 512


def _prepare(path, cfg) -> tuple:
    """加载单图 + 特征检测；任何异常都隔离为 load_error，不让单张拖垮整批。

    特征检测结果按 (路径, mtime, size, frame 参数) 缓存（LRU 上限 512）——
    线上 verify_single 后紧接着调 verify_pair、或阈值搜索/重复导出时，
    同一张图不重复做形态学+连通域分析（仅缓存 frame，大图矩阵随用随释放）。
    """
    key = None
    try:
        st = os.stat(path)
        fp = get_frame_params(cfg)
        key = (os.path.normpath(path), st.st_mtime_ns, st.st_size,
               hash(tuple(sorted((k, str(v)) for k, v in fp.items()))))
    except OSError:
        key = None
    auto_rot = bool(get_thresholds(cfg).get("auto_rotate_landscape", True))
    img, gray = load_image(path, auto_rotate=auto_rot)
    frame = None
    if key is not None and key in _FRAME_CACHE:
        frame = _FRAME_CACHE.pop(key)
        _FRAME_CACHE[key] = frame  # 移到队尾（LRU）
    if frame is None:
        try:
            frame = frame_mod.detect_frame(img, get_frame_params(cfg)) \
                if img is not None else None
        except Exception as e:  # noqa: BLE001
            logger.error("特征检测异常 %s: %s", path, e)
            frame = None
        if key is not None and frame is not None:
            if len(_FRAME_CACHE) >= _FRAME_CACHE_MAX:
                _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
            _FRAME_CACHE[key] = frame
    return img, gray, (frame or {})


def _extract_image_result(prev, expected_path: str) -> ImageResult | None:
    """从 verify_single 返回的 QCReport 或 ImageResult 中提取可复用的单图结果。"""
    if isinstance(prev, ImageResult):
        return prev
    if isinstance(prev, QCReport) and prev.images:
        norm_exp = os.path.normpath(expected_path)
        for im in prev.images:
            if os.path.normpath(im.path) == norm_exp:
                return im
        return prev.images[0]
    return None


# ============================================================================
# 线上（单张即检）
# ============================================================================
def verify_single(path: str, cfg: dict | None = None, *, pair_id: str | None = None,
                  attempt: int = 1, input_kind: str | None = None) -> QCReport:
    """拍摄完一张立即校验（损坏/模糊/倾斜/不全/单张错位）。

    用法：机器拍完 1 张 → verify_single → recapture_needed=True 则重拍第 1 张；
         拍完第 2 张 → verify_single → 通过后调用 verify_pair 补错位检查。
    input_kind: "auto"（默认）| "raw"（原图：跳过单张拼缝检查）| "mosaic"（成品图：跳过存活列判定）。
    """
    cfg = _as_cfg(cfg)
    th = dict(get_thresholds(cfg))
    if input_kind is not None:
        th["input_kind"] = input_kind
    max_att = int((cfg.get("recapture") or {}).get("max_attempts", 3))
    pair_id = pair_id or os.path.splitext(os.path.basename(path))[0]
    report = QCReport(pair_id=pair_id, attempt=attempt, max_attempts=max_att)

    img, gray, frame = _prepare(path, cfg)
    if th.get("input_kind", "auto") == "auto":
        th["input_kind"] = _resolve_single_input_kind(path, gray, th)
    res = _run_single_checks(path, img, gray, frame, th)
    report.images.append(res)
    _collect_report(report, path_b=None)
    return report


# ============================================================================
# 双张（含错位比对）
# ============================================================================
def verify_pair(path_a, path_b=None, cfg: dict | None = None, *,
                pair_id: str | None = None, attempt: int = 1,
                input_kind: str | None = None,
                stitch_output_dir: str | None = None,
                verify_stitched: bool | None = None,
                stitch_name: str | None = None,
                res_a: ImageResult | QCReport | None = None,
                res_b: ImageResult | QCReport | None = None) -> QCReport:
    """两张灯照成对校验：单图先过 -> 成对比对 -> 拼接 -> 拼接成品复检。

    线上已调过 verify_single(path_a) / verify_single(path_b) 时，可将返回报告传入
    res_a / res_b 直接复用单图检查结果，避免重复计算单图卷积与连通域。

    生产流程（2026-10-29 确认）：两张单图任一未通过时立即返回，不再做成对比对/拼接；
    两张都通过后才比对，随后将内存中的 img_a/img_b 送入 cfg.paths.stitch_script 拼接，
    并对 <pair_id>.png 按 mosaic 再跑一遍单图检查（模糊/不全/单图错位/损坏）。
    拼接成品未通过时，report.status 置为 fail/undetermined，重拍目标指向两张原图。

    stitch_name: 成品文件名（不含扩展名），默认取 pair_id；落图是
    <watch>/<批次>/<编号>/ 时用来落成 save/<批次>/<编号>.png。

    verify_stitched: True/False 强制开关；None（默认）时，只要配置了 stitch_output_dir
    就复检。拼接脚本缺失/执行失败时记入 stitch_error；要求复检时按 undetermined 阻塞，
    不要求复检时保持旧行为（只暴露错误，不改变质检结论）。path_b=None（单张）不拼接，
    stitched_status 恒为 None，避免离线扫散图时误判“未产出成品”。
    """
    cfg = _as_cfg(cfg)
    th = dict(get_thresholds(cfg))
    if input_kind is not None:
        th["input_kind"] = input_kind
    max_att = int((cfg.get("recapture") or {}).get("max_attempts", 3))
    pair_id = pair_id or os.path.splitext(os.path.basename(path_a))[0]
    report = QCReport(pair_id=pair_id, attempt=attempt, max_attempts=max_att)

    img_a, gray_a, frame_a = _prepare(path_a, cfg)
    if path_b is not None:
        # 成对原图模式：即使配置是 auto 或调用方传了 mosaic，也强制按 raw 重算/复用，
        # 否则单图阶段猜成 mosaic 会把存活列模糊判据永久跳过。
        th["input_kind"] = "raw"
    elif th.get("input_kind", "auto") == "auto":
        th["input_kind"] = _resolve_single_input_kind(path_a, gray_a, th)
    prev_a = _extract_image_result(res_a, path_a)
    if prev_a is not None and not _reuse_matches_kind(prev_a, th["input_kind"]):
        prev_a = None
    report.images.append(prev_a if prev_a is not None
                         else _run_single_checks(path_a, img_a, gray_a, frame_a, th))

    if path_b is not None:
        img_b, gray_b, frame_b = _prepare(path_b, cfg)
        prev_b = _extract_image_result(res_b, path_b)
        if prev_b is not None and not _reuse_matches_kind(prev_b, "raw"):
            prev_b = None
        report.images.append(prev_b if prev_b is not None
                             else _run_single_checks(path_b, img_b, gray_b, frame_b, th))
        if any(r.status != "pass" for r in report.images):
            _collect_report(report, path_b)
            return report
        if img_a is not None and img_b is not None:
            pair = None
            try:
                pair = checks.pair_analyze(img_a, img_b, th)
            except Exception as e:  # noqa: BLE001
                logger.error("成对预处理异常 %s/%s: %s", path_a, path_b, e)

            for attr, check_name, label, fn in (
                ("alignment_pair", "alignment_pair", "错位比对",
                 lambda: checks.check_alignment_pair(
                     img_a, img_b, frame_a, frame_b, th, pair=pair,
                     path_a=path_a, path_b=path_b)),
                ("repeat_pair", "repeat", "重复拍摄比对",
                 lambda: checks.check_repeat_pair(
                     img_a, img_b, th, path_a=path_a, path_b=path_b, pair=pair)),
                ("blur_pair", "blur_pair", "成对模糊比对",
                 lambda: checks.check_blur_pair(
                     img_a, img_b, frame_a, frame_b, th, path_a=path_a, path_b=path_b)),
                ("illumination_pair", "illum_pair", "照明比对",
                 lambda: checks.check_illumination_pair(img_a, img_b, th)),
            ):
                try:
                    setattr(report, attr, fn())
                except Exception as e:  # noqa: BLE001
                    logger.error("%s异常 %s/%s: %s", label, path_a, path_b, e)
                    setattr(report, attr,
                            checks._fail(check_name, {}, {}, f"{label}异常: {e}"))

    _collect_report(report, path_b)
    eff_stitch_dir = stitch_output_dir or (cfg.get("paths") or {}).get("stitch_output_dir")
    eff_verify = bool(eff_stitch_dir) if verify_stitched is None else bool(verify_stitched)
    if path_b is None:
        # 单张没有可拼接的另一半；离线扫描散图时不能因此判“未产出拼接成品”。
        eff_verify = False
    stitch_script = (cfg.get("paths") or {}).get("stitch_script")

    if (eff_stitch_dir and not report.recapture_needed
            and path_b is not None and img_a is not None and img_b is not None):
        try:
            report.stitched_path = _stitch_in_memory(
                img_a, img_b, report.pair_id, eff_stitch_dir, stitch_script,
                out_name=stitch_name)
        except Exception as e:  # noqa: BLE001 —— 显式暴露，是否阻塞由 verify_stitched 决定
            report.stitch_error = f"无缝拼接失败: {e}"
            logger.error("%s pair=%s", report.stitch_error, report.pair_id)

    if eff_verify:
        if report.stitched_path:
            _check_stitched(report, cfg, th)
        elif report.stitch_error:
            # 要求“拼接后复检”但拼接没产出：不能当通过放行，按判不了阻塞。
            report.stitched_status = "undetermined"
            report.stitched_checks = [checks._undetermined(
                "stitch", {}, {}, report.stitch_error)]
        elif not report.recapture_needed:
            # 成对检查已通过却没有产出拼接图：缺目录或缺少 path_b 都不能静默放行。
            reason = ("要求复检拼接图，但未配置 stitch_output_dir"
                      if eff_stitch_dir is None
                      else "要求复检拼接图，但未产出拼接成品")
            report.stitched_status = "undetermined"
            report.stitched_checks = [checks._undetermined("stitch", {}, {}, reason)]
        _collect_report(report, path_b)
    return report


def _check_stitched(report: QCReport, cfg: dict, th: dict) -> None:
    """对拼接成品按 mosaic 模态跑一遍单图检查，结果写回 report。"""
    try:
        img, gray, frame = _prepare(report.stitched_path, cfg)
    except Exception as e:  # noqa: BLE001
        report.stitched_status = "fail"
        report.stitched_checks = [checks._fail(
            "stitch", {}, {}, f"拼接成品读取异常: {e}")]
        return
    sth = dict(th)
    sth["input_kind"] = "mosaic"
    res = _run_single_checks(report.stitched_path, img, gray, frame, sth)
    report.stitched_checks = list(res.checks)
    report.stitched_status = res.status
    if res.load_error:
        report.stitched_checks.insert(0, checks._fail(
            "corruption", {}, {}, f"拼接成品损坏({res.load_error})"))


def _collect_report(report: QCReport, path_b: str | None) -> None:
    """汇总结论：status / recapture_needed / reasons / targets / escalate_manual。

    三态：任一项确定失败 -> fail；否则任一项判不了 -> undetermined；
    两者都按需重拍处理（undetermined 到 max_attempts 后同样转人工）。
    """
    reasons = []
    targets = []
    states: list[str] = []

    def add_reason(state: str, text: str, paths: list) -> None:
        states.append(state)
        prefix = "无法判定：" if state == "undetermined" else ""
        reasons.append(prefix + text)
        for p in paths:
            if p and p not in targets:
                targets.append(p)

    for r in report.images:
        label = os.path.basename(r.path)
        failed = []
        if r.load_error:
            states.append("fail")
            failed.append(f"图片损坏({r.load_error})")
        for c in r.checks:
            st = c.get("status", "pass" if c["passed"] else "fail")
            if st == "pass":
                continue
            states.append(st)
            prefix = "无法判定：" if st == "undetermined" else ""
            failed.append(prefix + c["reason"])
        if failed:
            reasons.append(f"[{label}] " + "；".join(failed))
            targets.append(r.path)

    def add_pair(res: dict | None, paths: list) -> None:
        if res is None or res["passed"]:
            return
        st = res.get("status", "fail")
        add_reason(st, "[整对] " + res["reason"], paths)

    pair_paths = [report.images[0].path, path_b] if report.images else [path_b]
    add_pair(report.alignment_pair, pair_paths)
    add_pair(report.repeat_pair, pair_paths)

    if report.blur_pair is not None and not report.blur_pair["passed"]:
        # 只有糊的那一张需要重拍（另一张清晰），软路径由 metric 直接给出；无软路径时退回整对
        soft = (report.blur_pair.get("metric") or {}).get("soft_path")
        add_pair(report.blur_pair, [soft] if soft else pair_paths)

    # 照明异常需整对重拍（灯管/曝光问题，两张都不可用）
    add_pair(report.illumination_pair, pair_paths)

    # 拼接成品复检：成品不过同样阻塞放行，重拍目标指向两张原图。
    if report.stitched_status and report.stitched_status != "pass":
        failed = []
        for c in report.stitched_checks:
            st = c.get("status", "pass" if c["passed"] else "fail")
            if st == "pass":
                continue
            # 整条拼接结论是 undetermined 时由 add_reason 统一加前缀；
            # 只有整体已 fail 的混合场景才需要给单条 undetermined 加前缀。
            prefix = ("无法判定：" if st == "undetermined"
                      and report.stitched_status == "fail" else "")
            text = c["reason"]
            if prefix and text.startswith(prefix):
                text = text[len(prefix):]
            failed.append(prefix + text)
        if failed:
            add_reason(report.stitched_status, "[拼接图] " + "；".join(failed),
                       pair_paths)
        else:
            add_reason(report.stitched_status, "[拼接图] 拼接成品复检未通过",
                       pair_paths)

    if "fail" in states:
        report.status = "fail"
    elif "undetermined" in states:
        report.status = "undetermined"
    else:
        report.status = "pass"
    report.recapture_needed = len(reasons) > 0
    report.recapture_reasons = reasons
    report.recapture_targets = targets
    report.escalate_manual = bool(report.recapture_needed and report.attempt >= report.max_attempts)


# ============================================================================
# 检查名 → 问题类别（汇总统计 / 导出分类统一用这张表）
# 2026-10-19：此前 runner 从中文原因文案里按关键词猜类别，Rule A 不全被归"其它"、
# 重复拍摄因文案含"拼接"被归"错位"、单灯管照明被归"其它"；导出脚本另有一份
# 名单且漏了 blur_pair / illum_pair。新增检查只需在这里登记一次。
# ============================================================================
CHECK_CATEGORY = {
    "corruption": "损坏",
    "resolution": "其它",          # 画幅尺寸与标定不符：不是图片内容缺陷，需按环境处理
    "illum_pair": "损坏",          # 单灯管/单侧照明缺失（README：归损坏）
    "blur": "模糊",
    "blur_pair": "模糊",
    "completeness": "不全",
    "alignment_pair": "错位",
    "alignment_single": "错位",
    "repeat": "重复拍摄",
}
CATEGORY_ORDER = ("损坏", "模糊", "不全", "错位", "重复拍摄", "其它")

# QCReport 上的成对检查字段（字段名, 检查名）
_PAIR_FIELDS = (("alignment_pair", "alignment_pair"),
                ("repeat_pair", "repeat"),
                ("blur_pair", "blur_pair"),
                ("illumination_pair", "illum_pair"))


def category_of(check_name: str) -> str:
    """检查名 → 类别；未登记的检查归"其它"。"""
    return CHECK_CATEGORY.get(check_name, "其它")


def failed_checks(report: QCReport) -> list:
    """列出报告中全部未通过的检查（结构化，不依赖原因文案）。

    Returns: list of dict
        name     : 检查名（corruption / blur / ... / illum_pair）
        category : 类别（见 CHECK_CATEGORY）
        scope    : "image"（单图检查）或 "pair"（整对检查）
        paths    : 该失败涉及的图片路径列表
        status   : "fail" 或 "undetermined"（判不了，按重拍处理）
        reason   : 原因文案
    """
    out = []
    img_paths = [r.path for r in report.images]
    for r in report.images:
        if r.load_error:
            out.append({"name": "corruption", "category": category_of("corruption"),
                        "scope": "image", "paths": [r.path], "status": "fail",
                        "reason": f"图片损坏({r.load_error})"})
        for c in r.checks:
            if not c["passed"]:
                out.append({"name": c["name"], "category": category_of(c["name"]),
                            "scope": "image", "paths": [r.path],
                            "status": c.get("status", "fail"), "reason": c["reason"]})
    for field_name, default_name in _PAIR_FIELDS:
        res = getattr(report, field_name, None)
        if res is None or res["passed"]:
            continue
        name = res.get("name") or default_name
        paths = list(img_paths)
        if default_name == "blur_pair":
            # 成对模糊只有糊的那张有问题（另一张清晰）
            soft = (res.get("metric") or {}).get("soft_path")
            if soft in img_paths:
                paths = [soft]
        out.append({"name": name, "category": category_of(name), "scope": "pair",
                    "paths": paths, "status": res.get("status", "fail"),
                    "reason": res["reason"]})

    # 拼接成品上的问题：单独 scope；paths 留空，避免离线导出把原图误判为坏图。
    for c in getattr(report, "stitched_checks", []) or []:
        if c["passed"]:
            continue
        out.append({"name": c["name"], "category": category_of(c["name"]),
                    "scope": "stitched", "paths": [],
                    "status": c.get("status", "fail"), "reason": c["reason"]})
    return out


def _attempt_summary(report: QCReport) -> dict:
    """一次 attempt 的可留档摘要（供 retry.attempts 与文件留档使用）。"""
    fails = failed_checks(report)
    cats = {f["category"] for f in fails}
    ordered = [n for n in CATEGORY_ORDER if n in cats]
    extra = sorted(cats - set(CATEGORY_ORDER))
    return {
        "attempt": int(report.attempt or 1),
        "retake_index": max(0, int(report.attempt or 1) - 1),
        "scope": "pair" if len(report.images) > 1 else "single",
        "status": report.status,
        "recapture_needed": bool(report.recapture_needed),
        "reasons": list(report.recapture_reasons),
        "targets": list(report.recapture_targets),
        "categories": ordered + extra,
        "images": [
            {
                "path": r.path,
                "status": r.status,
                "input_kind": getattr(r, "input_kind", None),
            }
            for r in report.images
        ],
        "stitched_status": report.stitched_status,
        "escalate_manual": bool(report.escalate_manual),
        "systemic_alert": report.systemic_alert,
        "timestamp": report.timestamp,
    }


def _as_cfg(cfg: dict | None) -> dict:
    """接受完整 config（含缺字段时补齐）或纯阈值 dict（旧调用兼容）。"""
    base = load_config()
    if cfg is None:
        return base
    if isinstance(cfg, dict) and ("paths" in cfg or "pairing" in cfg or "thresholds" in cfg):
        # 完整/部分 config：字段级合并到默认值
        for k in ("paths", "pairing", "thresholds", "frame_detect", "recapture", "notifier"):
            if k in cfg and isinstance(cfg[k], dict):
                base[k] = {**base.get(k, {}), **cfg[k]}
        return base
    if isinstance(cfg, dict):  # 纯阈值 dict：合入默认配置
        base["thresholds"].update(cfg)
        return base
    return base


class RecaptureTracker:
    """重拍熔断与逐对尝试次数追踪器（线上巡检循环 & 批量 runner 共用）。

    解决问题：
      1. 同一 pair_id 重复重拍达 max_attempts 后标记 escalate_manual=True（转人工）；
      2. 连续 N 个不同根盒因同类原因（如灯管熄灭、镜头脏污、导轨卡死）失败时，
         设置 report.systemic_alert 提示系统性硬件故障，避免整排盲目重拍。
    """

    def __init__(self, max_attempts: int | None = None,
                 max_consecutive_same_category: int | None = None):
        rcfg = load_config().get("recapture", {})
        self.max_attempts = int(
            max_attempts if max_attempts is not None else rcfg.get("max_attempts", 2)
        )
        self.max_consecutive = int(
            max_consecutive_same_category
            if max_consecutive_same_category is not None
            else rcfg.get("max_consecutive_same_category", 5)
        )
        self.pair_attempts: dict[str, int] = {}
        self.consecutive_by_cat: dict[str, int] = {}
        # pair_id -> 历次 attempt 摘要；同一 tracker 跨重拍复用才能得到完整历史
        self.attempt_history: dict[str, list[dict]] = {}

    def next_attempt(self, pair_id: str) -> int:
        """返回某个 pair_id 下一次质检对应的 attempt 序号（从 1 起）。"""
        return self.pair_attempts.get(pair_id, 0) + 1

    def observe(self, report: QCReport) -> QCReport:
        """登记一次质检结果，更新 report 的熔断状态与重拍历史。

        注意：report.attempt 必须由调用方在重拍后递增（同 pair 复用本 tracker）。
        """
        pid = report.pair_id
        self.pair_attempts[pid] = max(self.pair_attempts.get(pid, 0), report.attempt)
        report.max_attempts = self.max_attempts
        report.escalate_manual = bool(
            report.recapture_needed and report.attempt >= report.max_attempts
        )

        if report.recapture_needed:
            cats = {f["category"] for f in failed_checks(report)}
            # 仅保留本次仍出现的类别计数，其余清零
            for c in list(self.consecutive_by_cat.keys()):
                if c not in cats:
                    del self.consecutive_by_cat[c]
            triggered = []
            for c in sorted(cats):
                cnt = self.consecutive_by_cat.get(c, 0) + 1
                self.consecutive_by_cat[c] = cnt
                if self.max_consecutive > 0 and cnt >= self.max_consecutive:
                    triggered.append(f"{c}连续{cnt}组")
            if triggered and not report.systemic_alert:
                report.systemic_alert = (
                    f"疑似系统性故障（{'、'.join(triggered)}失败），建议暂停重拍并检查灯光/相机/导轨"
                )
        else:
            self.consecutive_by_cat.clear()

        self._remember(report)
        return report

    def _remember(self, report: QCReport) -> None:
        """把本次 attempt 摘要并入 report 与 tracker 的逐对历史（同一 attempt 去重）。"""
        pid = report.pair_id
        # tracker 是权威来源；新 tracker 遇到已带 history 的 report（如从磁盘恢复）时沿用
        prior = list(self.attempt_history.get(pid, [])) or list(report.attempt_history)
        merged = [dict(a) for a in prior
                  if int(a.get("attempt", 0) or 0) != int(report.attempt or 1)]
        merged.append(_attempt_summary(report))
        merged.sort(key=lambda a: int(a.get("attempt", 0) or 0))
        report.attempt_history = merged
        self.attempt_history[pid] = [dict(a) for a in merged]

