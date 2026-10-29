"""合成图像 smoke test：全链路验证 5 类生产检测判定（损坏/模糊/不全/错位/重复拍摄）的正确性。

用法：
    python scripts/smoke_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rootbox_qc.runner import ensure_utf8_console  # noqa: E402

ensure_utf8_console()

from rootbox_qc import verify_single, verify_pair  # noqa: E402

W, H = 1200, 1600          # 合成图尺寸（宽 x 高）
OUT = os.environ.get("ROOTBOX_TEST_OUT") or tempfile.mkdtemp(prefix="rootbox_smoke_")

# 合成图不是 5472×3648，关闭分辨率守卫（生产默认开启，非标尺寸判 undetermined）。
# 合成图没有"底部金属长边"结构，长边判据会判 undetermined；该判据由 opt_test 的
# full_box/cut_bot 用例覆盖，这里只测亮占比不全与其余四类。
SMOKE_CFG = {"thresholds": {"resolution_guard_enabled": False,
                            "completeness_longedge_check_enabled": False}}

rng = np.random.default_rng(42)


def background() -> np.ndarray:
    """暗土壤背景（噪声 sigma 8，均值 30）。"""
    return np.clip(rng.normal(30, 8, (H, W)), 0, 255).astype(np.uint8)


def draw_rootbox(img: np.ndarray, cx: int = W // 2, cy: int = H // 2,
                 bbox_w: int = 220, bbox_h: int = 170,
                 color: int = 160, angle: float = 0.0,
                 led_w: int = 10, led_h: int = 500,
                 led_val: int = 240, texture: float = 0.0) -> np.ndarray:
    """画一个合成根盒：中央亮金属块 + 左右两条亮条；支持纹理。"""
    pts = np.array([[-bbox_w // 2, -bbox_h // 2], [bbox_w // 2, -bbox_h // 2],
                    [bbox_w // 2, bbox_h // 2], [-bbox_w // 2, bbox_h // 2]],
                   dtype=np.float32)
    th = np.deg2rad(angle)
    rot = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    pts = pts @ rot.T + np.array([cx, cy])
    pts = np.round(pts).astype(np.int32)
    cv2.fillPoly(img, [pts], color)

    if texture > 0:
        t = np.clip(rng.normal(color, texture, (bbox_h, bbox_w)), 60, 235).astype(np.uint8)
        m = np.zeros(img.shape, np.uint8)
        cv2.fillPoly(m, [pts], 255)
        ys, xs = np.where(m > 0)
        if ys.size:
            ry = np.clip(ys - int(ys.min()), 0, bbox_h - 1)
            rx = np.clip(xs - int(xs.min()), 0, bbox_w - 1)
            img[ys, xs] = t[ry, rx]

    for off in (-int(bbox_w / 2 + 24), int(bbox_w / 2 + 24)):
        x = int(cx + off) - led_w // 2
        y0 = int(cy - led_h // 2)
        cv2.rectangle(img, (x, y0), (x + led_w, y0 + led_h), led_val, -1)

    return img


def save(prefix, img):
    p = os.path.join(OUT, f"{prefix}.png")
    cv2.imwrite(p, img)
    return p


def build_cases() -> dict:
    cases = {}

    # 1. 正常双张
    img_a = draw_rootbox(background(), texture=45, led_val=240)
    img_b = draw_rootbox(background(), cx=W // 2 + 45, texture=45, led_val=240)
    grad = np.linspace(-30, 30, W, dtype=np.float32)[None, :]
    img_b = np.clip(img_b.astype(np.float32) + grad, 30, 255).astype(np.uint8)
    cases["normal"] = (save("normal_a", img_a), save("normal_b", img_b))

    # 2. 模糊（ROI 方差信号）：整体失焦，亮区纹理摊平低于 blur_var_min
    st = rng.bit_generator.state
    soft = draw_rootbox(background(), texture=45, led_val=240)
    soft = cv2.GaussianBlur(soft, (0, 0), 6)
    rng.bit_generator.state = st
    cases["blur_var"] = (save("blur_var_a", soft), None)

    # 3. 不全：相机贴脸/偏到机架（亮占比 > 0.30 + 底部无长边结构）
    big = background()
    cv2.rectangle(big, (W // 2 - 600, H // 2 - 550), (W // 2 + 600, H // 2 + 550), 160, -1)
    for off in (-int(600 + 40), int(600 + 40)):
        x = int(W // 2 + off) - 8
        cv2.rectangle(big, (x, H // 2 - 700), (x + 16, H // 2 + 700), 235, -1)
    cases["incomplete_big"] = (save("incomplete_big", big), None)

    # 4. 损坏 A：纯黑图
    cases["corrupt_black"] = (save("corrupt_black", np.zeros((H, W), np.uint8)), None)

    # 5. 损坏 B：伪 jpg（不可解码 + 缺少 EOI + 文件过小）
    p_cut = os.path.join(OUT, "corrupt_truncated.jpg")
    with open(p_cut, "wb") as f:
        f.write(b"not an image data, just text " * 40)
    cases["corrupt_truncated"] = (p_cut, None)

    # 6. 错位 A：上下整体平移 600px
    shifted = cv2.warpAffine(draw_rootbox(background(), texture=45, led_val=240),
                             np.float32([[1, 0, 0], [0, 1, 600]]), (W, H),
                             borderMode=cv2.BORDER_REPLICATE)
    shifted = np.clip(shifted.astype(np.float32) + grad, 30, 255).astype(np.uint8)
    cases["misalign_shift"] = (save("misalign_shift_a",
                                    draw_rootbox(background(), texture=45, led_val=240)),
                               save("misalign_shift_b", shifted))

    # 7. 错位 B：两张内容不一致
    rng2 = np.random.default_rng(7)
    other = draw_rootbox(background(), bbox_w=260, bbox_h=210, texture=45, led_val=240)
    other_img = other.astype(np.int16)
    rng2.normal(0, 30, (H, W))
    other_img = np.clip(other_img + rng2.normal(0, 30, (H, W)).astype(np.int16), 0, 255)
    other_img = np.clip(other_img.astype(np.float32) + grad, 30, 255).astype(np.uint8)
    cases["misalign_diff"] = (
        save("misalign_diff_a", draw_rootbox(background(), texture=45, led_val=240)),
        save("misalign_diff_b", other_img))

    # 8. 错位 C：纯水平整幅滑移 45px（由三横带同向 dx>25px 捕获）
    base_h = draw_rootbox(background(), texture=45, led_val=240)
    shifted_h = cv2.warpAffine(base_h, np.float32([[1, 0, 45], [0, 1, 0]]), (W, H),
                               borderMode=cv2.BORDER_REPLICATE)
    shifted_h = np.clip(shifted_h.astype(np.float32) + grad, 30, 255).astype(np.uint8)
    cases["misalign_horiz"] = (save("misalign_horiz_a", base_h),
                               save("misalign_horiz_b", shifted_h))

    # 9. 重复拍摄：两张完全相同
    dup = draw_rootbox(background(), texture=45, led_val=240)
    cases["repeat"] = (save("repeat_a", dup), save("repeat_b", dup))

    return cases


def check_single(name: str, path: str | None, expect_fail: bool,
                 expect_types: set[str] | None = None, cfg=SMOKE_CFG) -> bool:
    if path is None:
        return True
    rep = verify_single(path, cfg)
    failed = {c["name"] for c in rep.images[0].checks if not c["passed"]}
    ok = (not rep.recapture_needed) == (not expect_fail)
    if expect_types is not None:
        ok = ok and failed == expect_types
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name:22s} fail={rep.recapture_needed} "
          f"reason={'; '.join(rep.recapture_reasons[:2])}")
    if not ok:
        print(f"        期望 fail={expect_fail} types={expect_types}, 实际 {failed}")
    return ok


def check_pair_case(name: str, pair_paths: tuple, expect_types: set[str],
                    cfg=SMOKE_CFG) -> bool:
    from rootbox_qc.verify import failed_checks
    rep = verify_pair(pair_paths[0], pair_paths[1], cfg)
    failed = {f["name"] for f in failed_checks(rep)}
    expect_fail = len(expect_types) > 0
    ok = (rep.recapture_needed == expect_fail) and (failed == expect_types)
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name:20s} fail={rep.recapture_needed} "
          f"types={sorted(failed)} reason={rep.recapture_reasons[:2]}")
    if not ok:
        print(f"        期望 types={expect_types}, 实际 {failed}")
    return ok


def main() -> int:
    try:
        print("=" * 70)
        print("根盒质检 smoke test（合成图）")
        print("=" * 70)
        cases = build_cases()

        results = []
        results.append(check_single("normal_a", cases["normal"][0], False, set()))
        results.append(check_single("blur_var", cases["blur_var"][0], True, {"blur"}))
        results.append(check_single("incomplete_big", cases["incomplete_big"][0],
                                    True, {"completeness"}))
        results.append(check_single("corrupt_black", cases["corrupt_black"][0],
                                    True, {"corruption", "blur"}))
        results.append(check_single("corrupt_truncated", cases["corrupt_truncated"][0],
                                    True, {"corruption"}))

        print("\n-- 成对（错位 / 重复拍摄） --")
        results.append(check_pair_case("normal_pair", cases["normal"], set()))
        results.append(check_pair_case("misalign_shift", cases["misalign_shift"], {"alignment_pair"}))
        results.append(check_pair_case("misalign_horiz", cases["misalign_horiz"], {"alignment_pair"}))
        results.append(check_pair_case("misalign_diff", cases["misalign_diff"], {"alignment_pair"}))
        results.append(check_pair_case("repeat_same", cases["repeat"], {"repeat"}))

        print("\n" + "=" * 70)
        n_pass = sum(results)
        if n_pass == len(results):
            print(f"SMOKE OK：{n_pass}/{len(results)} 项通过")
            return 0
        print(f"SMOKE FAIL：{n_pass}/{len(results)} 项通过，其余见上图")
        return 1
    finally:
        shutil.rmtree(OUT, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
