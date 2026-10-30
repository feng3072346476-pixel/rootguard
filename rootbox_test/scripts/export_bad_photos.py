"""按问题类型批量导出坏图，整夹剪切到指定目录（多类命中先复制后剪切）。

场景：从拍照落盘目录中筛出各分类问题图（损坏/模糊/倾斜/不全/错位/重复拍摄），
命中问题的那组（如 <某批次>\\001\\ 里的 1.png + 2.png）
以整个编号目录为单位剪切（移动）到分类目录，文件名不动，源目录不再保留。

用法
====
    python scripts/export_bad_photos.py -i <样本根目录> -o <坏图输出目录>
    python scripts/export_bad_photos.py -i <样本根目录> -o <坏图输出目录> --thresholds "{\\"tilt_angle_max_deg\\": 5.0}"
    python scripts/export_bad_photos.py -i <样本根目录> -o <坏图输出目录> --dry-run    # 只列计划，不剪切

注意：命中的来源目录/文件会从输入目录移走；不确定时先 --dry-run 看计划。

也可以不动命令行：设环境变量 ROOTBOX_QC_EXPORT_SRC / ROOTBOX_QC_EXPORT_DST，
保存后双击同目录的 export_bad_photos.bat 即可。

独立部署：把本脚本与 rootbox_qc/ 包一起拷到任意目录直接运行
（脚本自动查找同目录或上级目录的 rootbox_qc）；Windows 下可用
export_bad_photos.bat 双击运行或带参调用。

输出结构
========
    <坏图输出目录>\\
    ├── 损坏\\001\\1.png, 2.png      ← 整体剪切来源编号目录（文件原样，源目录移走）
    ├── 模糊\\...
    ├── 倾斜\\...
    ├── 不全\\...
    ├── 错位\\...
    ├── 重复拍摄\\...                 ← 两张灯照内容完全相同（相机未移动/文件重复拷贝）
    └── 明细.csv                    # 全部命中记录：来源/目录/类别/原因

分类说明
========
- 以"来源目录"为单位导出：某编号目录内任一图片命中，即把整个目录剪切到
  对应分类下（目录名不变，如 001\\；源目录随之移走）；左右/分批目录同名时自动
  加上级目录前缀（Date0525_left\\001 → Date0525_left_001）。
- 平铺在输入根目录里的单个文件（拼接成品等）直接按原文件名剪切。
- 一张图命中多类时按优先级归主类（损坏 > 模糊 > 倾斜 > 不全 > 错位 > 重复拍摄）;
  加 --multi-copy 则每个命中类都放一份：先复制到其余类，最后剪切一次
  （来源目录/文件只移走一次，各分类下内容完整）。
- 错位分两种：两张灯照间（整对判定）与单张内部偏移。
- 拼接成品图受限于单张信息，只能覆盖单张类检查；"两张间错位"只对原图成对生效。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys

# 独立部署：本脚本与 rootbox_qc 包放在同一目录（或 scripts/ 子目录）
# 即可运行，无需整个项目。
def _find_package_root() -> str | None:
    here = os.path.abspath(__file__)
    for r in (os.path.dirname(os.path.dirname(here)),      # 项目根（scripts/ 布局）
              os.path.dirname(here),                       # 同目录部署
              os.getcwd()):
        if os.path.isdir(os.path.join(r, "rootbox_qc")):
            return r
    return None


_pkg_root = _find_package_root()
if _pkg_root is None:
    print("  错误：未找到 rootbox_qc 包，请将本脚本与 rootbox_qc/ 目录放在一起")
    sys.exit(3)
sys.path.insert(0, _pkg_root)

from rootbox_qc.runner import collect_pairs, ensure_utf8_console, setup_logging
from rootbox_qc.verify import verify_pair, verify_single, failed_checks, CHECK_CATEGORY
from rootbox_qc.config import load_config

ensure_utf8_console()

try:  # 重定向/PyCharm 运行时时 stdout 默认全缓冲，不逐行刷会长时间"没有输出"
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass

# ============================================================================
# 输入 / 输出目录：默认从环境变量读，没有就必须用 -i / -o 传（不内置任何机器路径）
#   ROOTBOX_QC_EXPORT_SRC  拍照落盘目录
#   ROOTBOX_QC_EXPORT_DST  坏图导出目录
#   ROOTBOX_QC_PROTECTED_DIR  可选：受保护的真值基准库，禁止写入
# ============================================================================
INPUT_DIR = os.environ.get("ROOTBOX_QC_EXPORT_SRC") or ""
OUTPUT_DIR = os.environ.get("ROOTBOX_QC_EXPORT_DST") or ""
_PROTECTED_DATA_DIR = (os.path.normcase(os.path.normpath(os.environ["ROOTBOX_QC_PROTECTED_DIR"]))
                       if os.environ.get("ROOTBOX_QC_PROTECTED_DIR") else "")

# 主分类优先级（首个命中即主类）
# 2026-10-19：补上 illum_pair（单灯管照明，归损坏）与 blur_pair（成对模糊，归模糊）——
# 此前漏掉这两项，只命中它们的组线上会触发重拍、导出时却当作通过。
PRIORITY = ["corruption", "illum_pair", "blur", "blur_pair", "completeness",
            "alignment_pair", "repeat", "alignment_single"]
# 检查名 → 分类目录名：与汇总统计共用 verify.CHECK_CATEGORY，不再各维护一份
CLASS_NAMES = {name: CHECK_CATEGORY[name] for name in PRIORITY}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp")


def collect_all(input_dir: str, pairing_regex: str):
    """返回 (pairs, extras)。

    pairs 来自 collect_pairs（原图灯照对）；extras 是目录内未被任何 pair
    覆盖的其他图片文件（拼接成品图 / 落单图），递归收集。
    """
    pairs = collect_pairs(input_dir, pairing_regex)
    covered = set()
    for _pid, pa, pb in pairs:
        for p in (pa, pb):
            if p:
                covered.add(os.path.normpath(p))
    extras = []
    for dirpath, _dirs, files in os.walk(input_dir):
        for fn in sorted(files):
            if fn.lower().endswith(IMAGE_EXT):
                p = os.path.join(dirpath, fn)
                if os.path.normpath(p) not in covered:
                    extras.append(p)
    return pairs, extras


def collect_failures(report) -> dict:
    """返回 {path: {"hits": [检查名...], "reasons": [str...]}}。

    基于 verify.failed_checks：单图检查记入该图；整对检查（错位 / 重复拍摄 /
    单灯管照明）记入两张图；成对模糊只记入糊的那一张。
    """
    out = {img.path: {"hits": [], "reasons": []} for img in report.images}
    for f in failed_checks(report):
        if f["name"] not in CLASS_NAMES:   # 未登记的新检查：不导出，但提示一次
            print(f"  ! 未登记导出分类的检查 {f['name']}（{report.pair_id}），已跳过")
            continue
        tag = "[整对] " if f["scope"] == "pair" else ""
        for p in f["paths"]:
            info = out.setdefault(p, {"hits": [], "reasons": []})
            info["hits"].append(f["name"])
            info["reasons"].append(tag + f["reason"])
    return out


def safe_copy(src: str, dst: str) -> bool:
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except OSError as e:
        print(f"  ! 复制失败 {src} -> {dst}: {e}")
        return False


def safe_move(src: str, dst: str) -> bool:
    """剪切（跨盘时 shutil.move 自动退化为复制+删除）。"""
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return True
    except OSError as e:
        print(f"  ! 剪切失败 {src} -> {dst}: {e}")
        return False


def _print_hit(pair_id: str, new_rows: list) -> None:
    """某组/某张命中时打印一行（主分类去重），巡检进度可见。"""
    if not new_rows:
        return
    classes = dict.fromkeys(r["主分类"] for r in new_rows)
    print(f"  [命中] {pair_id}: {'、'.join(classes)}")


def main() -> int:
    # 模块级计划状态在每次入口重置，避免同进程多次调用残留上次计划（重复剪切/命名错乱）
    _units.clear()
    _claimed.clear()
    ap = argparse.ArgumentParser(description="按问题类型导出坏图")
    ap.add_argument("-i", "--input", default=INPUT_DIR,
                    help="拍照落盘目录（也可设 ROOTBOX_QC_EXPORT_SRC）")
    ap.add_argument("-o", "--output", default=OUTPUT_DIR,
                    help="导出目录（也可设 ROOTBOX_QC_EXPORT_DST）")
    ap.add_argument("--thresholds", default="{}",
                    help='JSON 阈值覆盖，如 {"blur_led_max_width": 60}')
    ap.add_argument("--multi-copy", action="store_true",
                    help="命中多类时每个分类都放一份（先复制后剪切；默认只导出到主分类）")
    ap.add_argument("--copy", action="store_true",
                    help="只复制不剪切（保留源目录/源文件不动）")
    ap.add_argument("--allow-data-dir", action="store_true",
                    help="显式允许向受保护的真值库写入（ROOTBOX_QC_PROTECTED_DIR 指定）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出计划，不执行复制/剪切")
    args = ap.parse_args()

    if not args.input:
        print("  错误：未指定输入目录，请用 -i <目录> 或设环境变量 ROOTBOX_QC_EXPORT_SRC")
        return 2
    if not args.output:
        print("  错误：未指定输出目录，请用 -o <目录> 或设环境变量 ROOTBOX_QC_EXPORT_DST")
        return 2

    out_norm = os.path.normcase(os.path.normpath(os.path.abspath(args.output)))
    in_norm = os.path.normcase(os.path.normpath(os.path.abspath(args.input)))
    if _PROTECTED_DATA_DIR and not args.dry_run and not args.allow_data_dir:
        if out_norm == _PROTECTED_DATA_DIR or in_norm.startswith(_PROTECTED_DATA_DIR + os.sep):
            print("  错误：检测到输入或输出涉及受保护的真值基准库。")
            print("        为防止误剪切/污染标定基准集，请改用独立目录，或显式加 --dry-run / --allow-data-dir。")
            return 2

    cfg = load_config()
    input_dir = args.input
    try:
        thresholds = json.loads(args.thresholds or "{}")
    except json.JSONDecodeError:
        print(f"  阈值 JSON 解析失败: {args.thresholds}")
        return 2
    cfg["thresholds"].update(thresholds)

    setup_logging(cfg["paths"].get("log_dir"))
    if not os.path.isdir(input_dir):
        print(f"  输入目录不存在: {input_dir}")
        return 1

    print(f"输入目录: {input_dir}")
    print(f"输出目录: {args.output}")
    pairs, extras = collect_all(input_dir, cfg["pairing"]["regex"])
    print(f"配对: {len(pairs)} 组（另 {len(extras)} 个非成对图片，按单张检）")

    rows = []
    n_pairs = len(pairs)
    for i, (pair_id, pa, pb) in enumerate(pairs, 1):
        try:
            rep = verify_pair(pa, pb, cfg, pair_id=pair_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {pair_id} 处理异常: {e}")
            continue

        n0 = len(rows)
        rows = _plan_report(rep, input_dir, args, rows)
        _print_hit(pair_id, rows[n0:])
        if i % 20 == 0 or i == n_pairs:
            print(f"  已检 {i}/{n_pairs} 组（命中 {len(rows)} 张）")

    n_extra = len(extras)
    for i, path in enumerate(extras, 1):
        pair_id = os.path.relpath(path, input_dir)
        try:
            rep = verify_single(path, cfg, pair_id=pair_id)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {pair_id} 处理异常: {e}")
            continue
        n0 = len(rows)
        rows = _plan_report(rep, input_dir, args, rows)
        _print_hit(pair_id, rows[n0:])
        if i % 20 == 0 or i == n_extra:
            print(f"  已检单张 {i}/{n_extra}")

    # 全部检查完再统一执行剪切：若边检边切，后续仍要读取的单张（如编号目录里
    # 的拼接成品）会因所在目录已被移走而读失败，误报"图片损坏"
    n_move = n_copy = 0
    if not args.dry_run and _units:
        n_move, n_copy = _execute_units(copy_only=args.copy)

    # 明细 CSV
    csv_path = os.path.join(args.output, "明细.csv")
    if not args.dry_run:
        os.makedirs(args.output, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else
                               ["来源路径", "导出目录", "主分类", "全部命中", "原因"])
            w.writeheader()
            for r in rows:
                w.writerow(r)

    print(f"\n完成：扫描 {len(pairs)} 组, 命中 {len(rows)} 张"
          + ("（计划模式，未执行）" if args.dry_run
             else f", 剪切 {n_move} 个、复制 {n_copy} 个目录/文件"))
    if not args.dry_run:
        print(f"明细: {csv_path}")
        print(f"目录: {args.output}\\<分类>\\")
    return 0


def safe_copytree(src_dir: str, dst_dir: str) -> bool:
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        return True
    except OSError as e:
        print(f"  ! 复制失败 {src_dir} -> {dst_dir}: {e}")
        return False


_units: dict[str, dict] = {}   # 导出单元：normpath(来源目录 或 平铺文件)
                               # -> {"flat": bool, "targets": {分类: (目标路径, 显示名)}}


def _plan_report(rep, input_dir: str, args, rows: list) -> list:
    """把一个报告的失败图登记到导出计划（不碰文件）；返回 rows。

    导出单位：来源图片所在目录（编号目录整夹）；图片平铺在输入根目录时
    （拼接成品、落单单拍）按单个文件。同一单位命中多类时全部登记，
    执行阶段先复制到其余类、最后剪切一次（见 _execute_units）。
    """
    failures = collect_failures(rep)
    input_root = os.path.normpath(os.path.abspath(input_dir))
    for path, info in failures.items():
        if not info["hits"]:
            continue
        # 主分类 = PRIORITY 中第一个命中；全命中按 CLASS_NAMES 去重
        ordered = [h for h in PRIORITY if h in info["hits"]]
        main_class = CLASS_NAMES[ordered[0]]
        all_classes = list(dict.fromkeys(CLASS_NAMES[h] for h in ordered))
        src = os.path.abspath(path)
        src_dir = os.path.dirname(src)
        flat = os.path.normpath(src_dir) == input_root
        unit_key = os.path.normpath(src if flat else src_dir)

        unit = _units.get(unit_key)
        if unit is None:
            unit = {"flat": flat, "targets": {}}
            _units[unit_key] = unit

        for cls in (all_classes if args.multi_copy else [main_class]):
            if cls in unit["targets"]:
                continue
            if flat:
                dst = _export_name(src, os.path.join(args.output, cls))
                shown = os.path.basename(dst)
            else:
                name = _export_dir_name(src_dir, os.path.join(args.output, cls))
                dst = os.path.join(args.output, cls, name)
                shown = f"{name}\\"
            _claimed.add(os.path.normpath(dst))
            unit["targets"][cls] = (dst, shown)
            if args.dry_run:
                print(f"  [计划] -> {cls}/{shown}")
        rows.append({
            "来源路径": path,
            "导出目录": os.path.join(main_class, unit["targets"][main_class][1]),
            "主分类": main_class,
            "全部命中": "、".join(all_classes),
            "原因": "；".join(info["reasons"]),
        })
    return rows


def _execute_units(copy_only: bool = False) -> tuple[int, int]:
    """执行导出计划，返回 (剪切数, 复制数)。

    copy_only=True 时全部目标均采用复制，保留源目录/文件；
    默认（copy_only=False）命中多类的单位先复制到其余各分类，最后剪切一次到最后一个分类；
    任一副本失败则保留源不剪切，避免丢数据。
    """
    n_move = n_copy = 0
    for key, unit in _units.items():
        targets = list(unit["targets"].values())
        if not targets:
            continue
        if copy_only:
            for dst, _ in targets:
                print(f"  [复制] {key} -> {dst}")
                ok = safe_copy(key, dst) if unit["flat"] else safe_copytree(key, dst)
                if ok:
                    n_copy += 1
            continue
        copy_ok = True
        for dst, _ in targets[:-1]:
            print(f"  [复制] {key} -> {dst}")
            ok = safe_copy(key, dst) if unit["flat"] else safe_copytree(key, dst)
            copy_ok = copy_ok and ok
            if ok:
                n_copy += 1
        if not copy_ok:
            print(f"  ! 复制未全部成功，保留源不剪切: {key}")
            continue
        dst = targets[-1][0]
        print(f"  [剪切] {key} -> {dst}")
        if safe_move(key, dst):
            n_move += 1
    return n_move, n_copy


def _export_dir_name(src_dir: str, dst_root: str) -> str:
    """导出文件夹名：保持来源目录原名；不同目录同名时加上级目录前缀
    （Date0525_left\\001 与 Date0525_right\\001 → 001 / Date0525_right_001）。"""
    name = os.path.basename(src_dir)
    cand = os.path.join(dst_root, name)
    if not _taken(cand):
        return name
    grand = os.path.basename(os.path.dirname(src_dir))
    for mid in (f"{grand}_{name}", f"{grand}_{name}_1"):
        if not _taken(os.path.join(dst_root, mid)):
            return mid
    n = 2
    while _taken(os.path.join(dst_root, f"{grand}_{name}_{n}")):
        n += 1
    return f"{grand}_{name}_{n}"


_claimed: set[str] = set()  # 本次运行已占用的目标路径（写入或计划，dry-run 也计入）


def _taken(dst: str) -> bool:
    return os.path.exists(dst) or os.path.normpath(dst) in _claimed


def _export_name(path: str, dst_dir: str) -> str:
    """导出文件名：原文件名直接保留；撞名时加来源目录前缀（001\\1.png → 001_1.png），
    再撞再加序号。不同编号目录的原图对互不混淆。"""
    base = os.path.basename(path)
    dst = os.path.join(dst_dir, base)
    if not _taken(dst):
        return dst
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    stem, ext = os.path.splitext(base)
    for mid in (f"{parent}_{stem}", f"{parent}_{stem}_1"):
        cand = os.path.join(dst_dir, f"{mid}{ext}")
        if not _taken(cand):
            return cand
    n = 2
    while _taken(os.path.join(dst_dir, f"{parent}_{stem}_{n}{ext}")):
        n += 1
    return os.path.join(dst_dir, f"{parent}_{stem}_{n}{ext}")


if __name__ == "__main__":
    sys.exit(main())
