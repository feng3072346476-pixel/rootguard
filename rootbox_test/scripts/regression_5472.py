r"""5472×3648 真实画幅回归：分辨率守卫 / 竖拍旋转 / C 刀口。

用法：
    python scripts/regression_5472.py
    python scripts/regression_5472.py --data D:\rootguard\dataed

- 合成图只验证"画幅/旋转/三态"链路，不验证真实缺陷信号。
- 若本机存在 data 目录，额外跑两组真实锚点（正常 <1000 号、纯模糊 007~014），
  作为 C 刀口的回归，不作为上线验收（data 已参与过阈值标定）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from rootbox_qc import checks, verify_single, verify_pair  # noqa: E402
from rootbox_qc.runner import ensure_utf8_console  # noqa: E402

ensure_utf8_console()

W, H = 5472, 3648
OUT = os.environ.get("ROOTBOX_TEST_OUT") or tempfile.mkdtemp(prefix="rootbox_5472_")
rng = np.random.default_rng(20261029)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    RESULTS.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")


def make_landscape() -> np.ndarray:
    """带纹理的 5472×3648 横图；尺寸优先，不追求真实缺陷分布。"""
    img = rng.integers(25, 70, (H, W, 3), dtype=np.uint8)
    metal = rng.integers(125, 205, (H - 900, W - 1400, 3), dtype=np.uint8)
    img[450:H - 450, 700:W - 700] = metal
    # 上下金属长边（让不全判据有结构可看，不保证通过）
    cv2.rectangle(img, (700, 450), (W - 700, 470), (225, 225, 225), -1)
    cv2.rectangle(img, (700, H - 470), (W - 700, H - 450), (225, 225, 225), -1)
    return img


def save(name: str, img: np.ndarray) -> str:
    p = os.path.join(OUT, name)
    cv2.imwrite(p, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return p


def check_synthetic() -> None:
    print("== 1. 5472×3648 横图：分辨率守卫放行 ==")
    land = save("landscape.jpg", make_landscape())
    rep = verify_single(land, input_kind="raw")
    names = {c["name"] for c in rep.images[0].checks}
    check("横图尺寸被守卫接受", "resolution" not in names, sorted(names))
    check("横图不报文件截断", not any("文件头" in r for r in rep.recapture_reasons),
          rep.recapture_reasons[:2])
    print(f"        status={rep.status} recapture={rep.recapture_needed}")

    print("== 2. 3648×5472 竖图：自动旋转后不再误报文件截断 ==")
    portrait_path = save("portrait.jpg", cv2.rotate(make_landscape(), cv2.ROTATE_90_CLOCKWISE))
    rep_p = verify_single(portrait_path, input_kind="raw")
    names_p = {c["name"] for c in rep_p.images[0].checks}
    check("竖图旋转后尺寸被守卫接受", "resolution" not in names_p, sorted(names_p))
    check("竖图旋转后不报文件截断", not any("文件头" in r for r in rep_p.recapture_reasons),
          rep_p.recapture_reasons[:2])
    print(f"        status={rep_p.status} recapture={rep_p.recapture_needed}")


def check_real(data_root: str) -> None:
    print("== 3. 真实 5472 锚点（仅回归，不是盲测） ==")
    normal_root = os.path.join(data_root, "正常")
    blur_root = os.path.join(data_root, "模糊")
    normals = []
    for n in ("917", "915", "912"):
        d = os.path.join(normal_root, n)
        if os.path.isfile(os.path.join(d, "1.png")) and os.path.isfile(os.path.join(d, "2.png")):
            normals.append(d)
    if normals:
        for d in normals:
            rep = verify_pair(os.path.join(d, "1.png"), os.path.join(d, "2.png"), input_kind="raw")
            check(f"正常 {os.path.basename(d)} 不误报", not rep.recapture_needed,
                  rep.recapture_reasons[:2])
    else:
        print("        （跳过：data/正常 中未找到 917/915/912）")

    blurs = []
    for n in ("007", "008", "009", "010"):
        d = os.path.join(blur_root, n)
        if os.path.isfile(os.path.join(d, "1.png")) and os.path.isfile(os.path.join(d, "2.png")):
            blurs.append(d)
    if blurs:
        for d in blurs:
            rep = verify_pair(os.path.join(d, "1.png"), os.path.join(d, "2.png"), input_kind="raw")
            hit = any(c["name"] in ("blur", "blur_pair") and not c["passed"]
                      for c in rep.images[0].checks) or (
                rep.blur_pair is not None and not rep.blur_pair["passed"])
            check(f"模糊 {os.path.basename(d)} 命中模糊", rep.recapture_needed and hit,
                  rep.recapture_reasons[:2])
    else:
        print("        （跳过：data/模糊 中未找到 007~010）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.environ.get("ROOTBOX_QC_DATA", r"D:\rootguard\dataed"))
    args = ap.parse_args()
    try:
        check_synthetic()
        if args.data and os.path.isdir(args.data):
            check_real(args.data)
        else:
            print(f"== 3. 跳过真实锚点：{args.data!r} 不存在 ==")
        failed = [r for r in RESULTS if not r[1]]
        print()
        if not failed:
            print(f"5472 REGRESSION OK：{len(RESULTS)}/{len(RESULTS)} 项通过")
            return 0
        print(f"5472 REGRESSION FAIL：{len(failed)}/{len(RESULTS)} 项失败")
        return 1
    finally:
        shutil.rmtree(OUT, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
