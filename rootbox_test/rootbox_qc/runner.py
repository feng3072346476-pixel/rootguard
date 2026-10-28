"""端到端入口：扫描目录 + 配对 + 跑检测 + 终端汇总 + 结果文件。

核心 API：
    run(input_dir, **overrides)           # 扫描 + 检测 + 输出
    run_and_show(input_dir, **overrides)  # 同 run（兼容别名）

线上拍摄即检不需要本文件，直接用 verify 模块的 verify_single / verify_pair。
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import Counter

from .config import load_config
from .verify import (
    verify_pair, failed_checks, CATEGORY_ORDER,
    QCReport, ImageResult, RecaptureTracker,
)
from .notifier import build_notifier

logger = logging.getLogger("rootbox_qc")


def ensure_utf8_console(stdout=None) -> None:
    """Windows 下让 stdout 以 UTF-8 输出（否则终端中文乱码）。幂等。"""
    import sys
    stream = stdout or sys.stdout
    try:
        if stream is not None and stream.encoding and "utf" not in stream.encoding.lower():
            stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # noqa: BLE001
        pass


# ============================================================================
# 日志：控制台 + 按天文件（logging 自带 FileHandler，按天轮转由部署/轮转工具处理）
# ============================================================================
def setup_logging(log_dir: str | None = None, level: int = logging.INFO) -> None:
    """幂等日志初始化：控制台 + 文件（file 在 log_dir/rootbox_qc.log）。"""
    root = logging.getLogger("rootbox_qc")
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(os.path.join(log_dir, "rootbox_qc.log"),
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            logger.warning("创建日志文件失败: %s", e)


# ============================================================================
# ANSI 颜色（不支持时自动降级）
# ============================================================================
def _supports_color() -> bool:
    """是否输出 ANSI 颜色：重定向/管道下禁用，避免转义码污染落盘内容。"""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    if os.environ.get("PYCHARM_HOSTED") is not None:  # PyCharm 运行控制台支持 ANSI
        return True
    return bool(sys.stdout.isatty())


_COLOR = _supports_color()
if _COLOR:
    GRAY = "\033[90m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"
else:
    GRAY = RED = GREEN = YELLOW = BLUE = BOLD = RESET = ""


# ============================================================================
# 目录扫描 + 配对
# ============================================================================
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp")


def collect_pairs(input_dir: str, pairing_regex: str) -> list[tuple]:
    """根据目录结构自动配对。

    支持：
      A. 平铺：<prefix>_<idx>.<ext>（如 root_001_1.jpg + root_001_2.jpg）
      B. 单层子目录：每个子目录里 1.<ext> + 2.<ext>（支持 png/jpg/jpeg/bmp）
      C. 双层子目录：<类别>/<编号>/1.<ext> + 2.<ext>（如 D:\\data\\不全\\004）
      D. 根目录无图但子目录有图：自动汇总所有含图的子目录

    Returns: list of (pair_id, path_a, path_b)，path_b 可能为 None（单图）
    """
    if not os.path.isdir(input_dir):
        return []

    direct_pairs = _collect_pairs_here(input_dir, pairing_regex)
    if direct_pairs:
        return direct_pairs

    all_sub_pairs = []
    for entry in sorted(os.listdir(input_dir)):
        sub = os.path.join(input_dir, entry)
        if os.path.isdir(sub):
            sub_pairs = _collect_pairs_here(sub, pairing_regex)
            if sub_pairs:
                all_sub_pairs.extend(sub_pairs)

    return all_sub_pairs


def _collect_pairs_here(input_dir: str, pairing_regex: str) -> list[tuple]:
    """当层扫描（不递归）。"""

    def _find_idx_file(sub: str, idx: int) -> str | None:
        # 优先 .png（与历史行为一致），其次其它 IMAGE_EXT（大小写无关）
        for ext in (".png", ".jpg", ".jpeg", ".bmp"):
            cand = os.path.join(sub, f"{idx}{ext}")
            if os.path.isfile(cand):
                return cand
        try:
            for fn in sorted(os.listdir(sub)):
                stem, ext = os.path.splitext(fn)
                if stem == str(idx) and ext.lower() in IMAGE_EXT:
                    full = os.path.join(sub, fn)
                    if os.path.isfile(full):
                        return full
        except OSError:
            pass
        return None

    def pair_in(sub):
        f1 = _find_idx_file(sub, 1)
        f2 = _find_idx_file(sub, 2)
        if f1 and f2:
            return (f1, f2)
        if f1:
            return (f1, None)
        return None

    # 模式 C：双层嵌套（单次遍历：任一二级目录成对即按本模式收集全部）
    out = []
    for d1 in sorted(os.listdir(input_dir)):
        p1 = os.path.join(input_dir, d1)
        if not os.path.isdir(p1):
            continue
        for d2 in sorted(os.listdir(p1)):
            p2 = os.path.join(p1, d2)
            if not os.path.isdir(p2):
                continue
            pp = pair_in(p2)
            if pp is None:
                continue
            out.append((f"{d1}_{d2}", pp[0], pp[1]))
    if out:
        return out

    # 模式 B：单层子目录
    pair_b = []
    for d in sorted(os.listdir(input_dir)):
        sd = os.path.join(input_dir, d)
        if not os.path.isdir(sd):
            continue
        pp = pair_in(sd)
        if pp is not None:
            pair_b.append((d, pp[0], pp[1]))
    if pair_b:
        return pair_b

    # 模式 A：平铺（按正则分组配对）
    pat = re.compile(pairing_regex, re.IGNORECASE)
    groups: dict = {}
    for fn in sorted(os.listdir(input_dir)):
        p = os.path.join(input_dir, fn)
        if not os.path.isfile(p):
            continue
        if not fn.lower().endswith(IMAGE_EXT):
            continue
        m = pat.match(fn)
        if not m:
            groups.setdefault(fn, []).append((0, p))
            continue
        prefix = m.group("prefix")
        try:
            idx = int(m.group("idx"))
        except (ValueError, IndexError):
            idx = 0
        groups.setdefault(prefix, []).append((idx, p))
    out = []
    for prefix, lst in groups.items():
        lst.sort(key=lambda x: x[0])
        pretty = re.sub(r"\.(?:png|jpg|jpeg|bmp)$", "", prefix, flags=re.IGNORECASE)
        for i in range(0, len(lst) - (len(lst) % 2), 2):
            out.append((f"{pretty}_{lst[i][0]}-{lst[i + 1][0]}", lst[i][1], lst[i + 1][1]))
        if len(lst) % 2 == 1:
            out.append((pretty, lst[-1][1], None))
    return out


# ============================================================================
# 终端打印
# ============================================================================
def _print_pair_result(report: "QCReport", idx: int, total: int):
    pid = report.pair_id
    bar = f"[{idx:>4}/{total}]"
    if report.recapture_needed:
        head = f"{RED}{BOLD}✗ 需重拍{RESET}"
        if report.escalate_manual:
            head += f" {YELLOW}[转人工]{RESET}"
        reasons = report.recapture_reasons[:4]
        extra = (len(report.recapture_reasons) - 4)
        body = "\n             ".join(reasons)
        if extra > 0:
            body += f"\n             {YELLOW}...还有 {extra} 条{RESET}"
        print(f"{GRAY}{bar}{RESET} {head} {BOLD}{pid}{RESET}")
        print(f"             {body}")
        if report.systemic_alert:
            print(f"             {YELLOW}⚠ {report.systemic_alert}{RESET}")
    else:
        print(f"{GRAY}{bar}{RESET} {GREEN}✓ 通过{RESET}    {BOLD}{pid}{RESET}")
    if report.stitch_error:
        print(f"             {YELLOW}⚠ {report.stitch_error}{RESET}")


def _categories_of(report: "QCReport") -> set:
    """一组报告命中的类别集合（按检查名映射，见 verify.CHECK_CATEGORY）。

    2026-10-19：替代旧 _categorize(reason) 关键词匹配——旧版把 Rule A 不全归
    "其它"、重复拍摄（文案含"拼接"）归"错位"、单灯管照明归"其它"。
    """
    return {f["category"] for f in failed_checks(report)}


def _print_summary(reports: list, elapsed: float, output_dir: str | None):
    n = len(reports)
    n_fail = sum(1 for r in reports if r.recapture_needed)
    n_pass = n - n_fail
    n_escalate = sum(1 for r in reports if getattr(r, "escalate_manual", False))
    n_alert = sum(1 for r in reports if getattr(r, "systemic_alert", None))

    print()
    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  汇总{RESET}")
    print(f"{'=' * 60}")
    print(f"  总组数 : {BOLD}{n}{RESET}")
    print(f"  通过   : {GREEN}{n_pass}{RESET}")
    print(f"  需重拍 : {RED if n_fail else GREEN}{n_fail}{RESET}")
    if n_escalate:
        print(f"  转人工 : {YELLOW}{n_escalate}{RESET}")
    if n_alert:
        print(f"  系统告警: {YELLOW}{n_alert} 次触发连续同类熔断提示{RESET}")
    print(f"  用时   : {elapsed:.1f} s")
    if output_dir:
        print(f"  报告目录 : {output_dir}")
    print(f"{'=' * 60}")

    counter = Counter()
    for r in reports:
        for cat in _categories_of(r):
            counter[cat] += 1

    if counter:
        print(f"\n  {BOLD}失败原因分布（按组计，一组可同时命中多类）：{RESET}")
        max_w = max(len(k) for k in counter)
        for k in sorted(counter, key=lambda c: (-counter[c], CATEGORY_ORDER.index(c)
                                                if c in CATEGORY_ORDER else 99)):
            print(f"    {BLUE}{k:<{max_w}}{RESET} : {counter[k]}")


# ============================================================================
# 核心 API
# ============================================================================
def _merge_overrides(cfg: dict, overrides: dict) -> dict:
    """把用户传的 overrides 合并进 config（顶层键 / 点号路径均支持）。"""
    cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in cfg.items()}
    for k, v in (overrides or {}).items():
        if "." in k:
            top, sub = k.split(".", 1)
            cfg.setdefault(top, {})
            if isinstance(v, dict):
                cfg[top] = {**cfg.get(top, {}), **v}
            else:
                cfg[top][sub] = v
        else:
            cfg[k] = v
    return cfg


def _build_exception_report(pair_id: str, pa: str, pb: str | None,
                            err: Exception, max_attempts: int = 2) -> QCReport:
    """当某对处理抛出未预期异常时构造兜底失败报告（不静默丢弃，直接转人工）。"""
    reason = f"质检内部异常({type(err).__name__}: {err})"
    imgs = []
    targets = []
    for p in (pa, pb):
        if p:
            imgs.append(ImageResult(
                path=p,
                checks=[{"name": "corruption", "passed": False, "reason": reason,
                         "metric": {"exception": type(err).__name__}}],
                load_error=reason,
            ))
            targets.append(p)
    return QCReport(
        pair_id=pair_id,
        recapture_needed=True,
        recapture_reasons=[f"[异常] {reason}"],
        recapture_targets=targets,
        attempt=1,
        max_attempts=max_attempts,
        escalate_manual=True,
        images=imgs,
    )


def run(
    input_dir: str | None = None,
    *,
    output_dir: str | None = None,
    recapture_queue: str | None = None,
    stitch_output_dir: str | None = None,
    thresholds: dict | None = None,
    input_kind: str | None = None,
    write_files: bool = True,
    show: bool = True,
    **overrides,
) -> list:
    """扫描 + 配对 + 质检 + 终端汇总（可选质检通过后直接内存无缝拼接）。

    Args:
        input_dir:         待扫描目录（None 沿用 config 里的 paths.input_dir）
        output_dir:        报告输出目录（None 沿用 config）
        recapture_queue:   队列文件路径（None 沿用 config）
        stitch_output_dir: 质检通过后自动触发无缝拼接的输出目录（None 沿用 config）
        thresholds:        临时阈值覆盖 dict（不改源文件）
        input_kind:        "auto" | "raw" | "mosaic"（None 沿用 config.thresholds.input_kind）
        write_files:       是否写 JSON 报告 / 重拍队列（默认 True）
        show:              是否打印到终端（默认 True）
        **overrides:       任意顶层覆盖，例如 pairing={"regex": "..."}

    Returns:
        list[QCReport]，方便自行后处理（如调 HTTP 接口、上传数据库）。
    """
    cfg = load_config()
    cfg = _merge_overrides(cfg, overrides)

    paths = cfg["paths"]
    if input_dir is not None:
        paths["input_dir"] = input_dir
    if output_dir is not None:
        paths["output_dir"] = output_dir
    if recapture_queue is not None:
        paths["recapture_queue"] = recapture_queue
    if stitch_output_dir is not None:
        paths["stitch_output_dir"] = stitch_output_dir
    if thresholds:
        cfg["thresholds"].update(thresholds)
    if input_kind is not None:
        cfg["thresholds"]["input_kind"] = input_kind

    # 启用拼接时启动即校验脚本，避免跑完整批才在每对上报错
    stitch_dir = paths.get("stitch_output_dir")
    if stitch_dir:
        stitch_script = paths.get("stitch_script")
        if not stitch_script or not os.path.isfile(stitch_script):
            raise FileNotFoundError(
                f"启用了 stitch_output_dir={stitch_dir!r}，但无缝拼接脚本不存在: "
                f"{stitch_script!r}（配置 paths.stitch_script 或环境变量 "
                "ROOTBOX_QC_STITCH_SCRIPT）")

    ensure_utf8_console()
    setup_logging(paths.get("log_dir"))
    logger.info("根盒图像质检启动，input=%s", paths["input_dir"])

    if not os.path.isdir(paths["input_dir"]):
        raise FileNotFoundError(f"输入目录不存在: {paths['input_dir']}")

    if show:
        print(f"\n{BOLD}根盒图像质检{RESET}")
        print(f"  输入目录 : {paths['input_dir']}")
        if write_files:
            print(f"  输出目录 : {paths['output_dir']}")
        print()

    pairs = collect_pairs(paths["input_dir"], pairing_regex=cfg["pairing"]["regex"])
    if show:
        print(f"{GRAY}扫描到 {len(pairs)} 对（含单张）待质检图片组。{RESET}\n")

    if write_files and not paths["recapture_queue"].startswith("<"):
        qdir = os.path.dirname(paths["recapture_queue"])
        if qdir:
            os.makedirs(qdir, exist_ok=True)

    notifier = build_notifier(cfg, paths) if write_files else _NullNotifier()
    rcfg = cfg.get("recapture", {}) or {}
    tracker = RecaptureTracker(
        max_attempts=rcfg.get("max_attempts", 2),
        max_consecutive_same_category=rcfg.get("max_consecutive_same_category", 5),
    )

    t0 = time.time()
    reports = []
    failed_pairs = 0
    total = len(pairs)
    for i, (pair_id, pa, pb) in enumerate(pairs, 1):
        try:
            rpt = verify_pair(pa, pb, cfg, pair_id=pair_id, input_kind=input_kind,
                              stitch_output_dir=paths.get("stitch_output_dir"))
            tracker.observe(rpt)
        except Exception as e:  # noqa: BLE001 —— 异常隔离，不静默丢弃，写入兜底报告并转人工
            logger.exception("配对 %s 处理异常: %s", pair_id, e)
            failed_pairs += 1
            rpt = _build_exception_report(pair_id, pa, pb, e, max_attempts=tracker.max_attempts)
        if write_files:
            notifier.notify(rpt.to_dict())
        if show:
            _print_pair_result(rpt, i, total)
        reports.append(rpt)
        logger.info("pair=%s recapture=%s escalate=%s targets=%s",
                    pair_id, rpt.recapture_needed, rpt.escalate_manual, rpt.recapture_targets)

    elapsed = time.time() - t0
    if failed_pairs:
        logger.error("有 %d 对处理异常（已生成兜底失败报告并标记转人工）", failed_pairs)
    if show:
        _print_summary(reports, elapsed, paths["output_dir"] if write_files else None)
        if write_files:
            queue = paths["recapture_queue"]
            n_recap = sum(1 for r in reports if r.recapture_needed)
            print(f"\n  {YELLOW}→ 重拍队列已写入:{RESET} {queue}（{n_recap} 项）")
            print(f"  {YELLOW}→ 每对详细报告:{RESET} {paths['output_dir']}/<pair_id>.json")
    return reports


# 兼容旧名
run_and_show = run


class _NullNotifier:
    """不写任何文件。"""

    def notify(self, report: dict) -> None:
        pass
