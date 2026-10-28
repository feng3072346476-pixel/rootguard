"""五类生产检测与架构特性的专项功能验证（合成图）。"""
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from rootbox_qc import checks, frame as frame_mod
from rootbox_qc.config import load_config

OUT = tempfile.mkdtemp(prefix="rootbox_opt_")
rng = np.random.default_rng(1)
W, H = 1200, 1600


def bg(sig=8, mu=30):
    return np.clip(rng.normal(mu, sig, (H, W)), 0, 255).astype(np.uint8)


def save(name, img):
    p = os.path.join(OUT, name)
    ok, buf = cv2.imencode(".png", img)
    buf.tofile(p)
    return p


def draw_led(img, x, w=10, val=240):
    cv2.rectangle(img, (x, 200), (x + w, 1400), val, -1)
    return img


ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    st = "PASS" if cond else "FAIL"
    print(f"  [{st}] {name} {extra}")


try:
    cfg = load_config()
    th = dict(cfg["thresholds"])
    fp = dict(cfg["frame_detect"])

    print("== 1. 损坏：截断检测 + 体积/分辨率归一 ==")
    good = save("good.png", draw_led(bg(), 400))
    img_g = cv2.imdecode(np.fromfile(good, np.uint8), cv2.IMREAD_COLOR)
    r = checks.check_corruption(path=good, img_bgr=img_g, th=th)
    check("正常图通过", r["passed"], r["reason"])
    check("记录 bytes_per_mpx", "bytes_per_mpx" in r["metric"], r["metric"].get("bytes_per_mpx"))
    data = open(good, "rb").read()
    trunc = os.path.join(OUT, "trunc.png")
    open(trunc, "wb").write(data[: len(data) // 2])
    img_t = cv2.imdecode(np.fromfile(trunc, np.uint8), cv2.IMREAD_COLOR)
    r = checks.check_corruption(path=trunc, img_bgr=img_t, th=th)
    check("截断 PNG 被检出", not r["passed"], r["reason"][:80])

    print("== 2. 模糊：ROI 方差 + 角色感知存活列 ==")
    _tex = np.clip(np.random.default_rng(99).normal(160, 45, (500, 400)), 60, 235).astype(np.uint8)

    def scene(xoff=0):
        img = bg()
        cx, cy = 600 + xoff, 800
        img[cy - 250 : cy + 250, cx - 200 : cx + 200] = _tex
        cv2.rectangle(img, (400 + xoff, 200), (410 + xoff, 1400), 240, -1)
        return img

    sharp_s = scene(0)
    soft_s = cv2.GaussianBlur(sharp_s, (0, 0), 6)
    rs = checks.check_blur(cv2.cvtColor(sharp_s, cv2.COLOR_GRAY2BGR), sharp_s, frame_mod.detect_frame(sharp_s, fp), th)
    ro = checks.check_blur(cv2.cvtColor(soft_s, cv2.COLOR_GRAY2BGR), soft_s, frame_mod.detect_frame(soft_s, fp), th)
    check("清晰图通过、失焦图被 ROI 方差拦截", rs["passed"] and not ro["passed"], ro["reason"])

    print("== 3. 不全：Rule A 底部长边结构缺失 ==")
    full_box = bg()
    cv2.rectangle(full_box, (100, 30), (1100, 1540), 200, 8)
    fr_full = frame_mod.detect_frame(full_box, fp)
    r_full = checks.check_completeness(cv2.cvtColor(full_box, cv2.COLOR_GRAY2BGR), full_box, fr_full, th)
    check("上下长边完整通过", r_full["passed"], r_full["metric"].get("long_edge"))
    cut_bot = np.full((H, W), 80, dtype=np.uint8)
    cv2.rectangle(cut_bot, (100, 30), (1100, 900), 220, 8)  # 底部长边在 y=900 (0.56 < 0.85)
    fr_cut = frame_mod.detect_frame(cut_bot, fp)
    r_cut = checks.check_completeness(cv2.cvtColor(cut_bot, cv2.COLOR_GRAY2BGR), cut_bot, fr_cut, th)
    check("底部无金属长边检出不全", not r_cut["passed"], r_cut["reason"])

    print("== 4. 错位：相位相关 + 特征匹配印证 ==")
    a = scene(0)
    b = scene(10)
    pair = checks.pair_analyze(cv2.cvtColor(a, cv2.COLOR_GRAY2BGR), cv2.cvtColor(b, cv2.COLOR_GRAY2BGR), th)
    ff, ph = pair["feature"], pair["phase"]
    check("特征与相位互相印证", ff is not None and ph is not None and abs(ff["shift_px"] - ph["shift_px"]) < 20,
          f"feature={ff['shift_px']} phase={ph['shift_px']}")
    fa = frame_mod.detect_frame(a, fp)
    fb = frame_mod.detect_frame(b, fp)
    r = checks.check_alignment_pair(cv2.cvtColor(a, cv2.COLOR_GRAY2BGR), cv2.cvtColor(b, cv2.COLOR_GRAY2BGR), fa, fb, th, pair=pair)
    check("小位移正常对通过", r["passed"], r["reason"][:60])
    b2 = scene(350)
    pair2 = checks.pair_analyze(cv2.cvtColor(a, cv2.COLOR_GRAY2BGR), cv2.cvtColor(b2, cv2.COLOR_GRAY2BGR), th)
    fb2 = frame_mod.detect_frame(b2, fp)
    r6 = checks.check_alignment_pair(cv2.cvtColor(a, cv2.COLOR_GRAY2BGR), cv2.cvtColor(b2, cv2.COLOR_GRAY2BGR), fa, fb2, th, pair=pair2)
    check("大位移错位检出", not r6["passed"], r6["reason"][:80])

    print("== 5. 重复拍摄：文件级强信号 + 低频照明差主信号 ==")
    orig = draw_led(bg(), 400)
    p1 = save("r1.png", orig)
    p2 = save("r2.png", orig)
    r = checks.check_repeat_pair(orig, orig, th, path_a=p1, path_b=p2)
    fi = r["metric"]["file_identical"]
    check("重复拷贝文件级信号命中", fi["same_bytes"], fi)
    check("重复拷贝被判定", not r["passed"])
    s1 = scene(0)
    s2 = scene(5)
    ps1 = save("s1.png", s1)
    ps2 = save("s2.png", s2)
    rs_rep = checks.check_repeat_pair(s1, s2, th, path_a=ps1, path_b=ps2)
    check("同光照微移仍判重复", not rs_rep["passed"],
          f"mae_lo={rs_rep['metric']['illum']['mae_lo']} mae={rs_rep['metric']['pair_mae']}")
    grad = np.linspace(-30, 30, W, dtype=np.float32)[None, :]
    c1 = np.clip(draw_led(bg(), 400).astype(np.float32) - grad, 0, 255).astype(np.uint8)
    c2 = np.clip(draw_led(bg(), 700).astype(np.float32) + grad, 0, 255).astype(np.uint8)
    pc1 = save("c1.png", c1)
    pc2 = save("c2.png", c2)
    r7 = checks.check_repeat_pair(c1, c2, th, path_a=pc1, path_b=pc2)
    check("不同照明状态不被误判", r7["passed"],
          f"mae_lo={r7['metric']['illum']['mae_lo']} mae={r7['metric']['pair_mae']}")

    print("== 6. 回归：结构化分类映射 / 成对模糊与单灯管导出 / 队列二次轮转 ==")
    from rootbox_qc.verify import verify_pair, verify_single, failed_checks, QCReport, ImageResult
    from rootbox_qc.runner import _categories_of
    from rootbox_qc.notifier import FileNotifier
    import rootbox_qc.notifier as notifier_mod
    from export_bad_photos import collect_failures

    sharp_p = save("bp_1.png", scene(0))
    grad_bp = np.linspace(-60, 60, W, dtype=np.float32)[None, :]
    soft_img = cv2.GaussianBlur(
        np.clip(scene(0).astype(np.float32) + grad_bp, 30, 255).astype(np.uint8), (0, 0), 3)
    soft_p = save("bp_2.png", soft_img)
    th_bp = dict(th)
    th_bp["blur_strip_check_enabled"] = False
    th_bp["blur_var_min"] = 0.0
    rep_bp = verify_pair(sharp_p, soft_p, th_bp)
    check("成对模糊独立命中 blur_pair（单张检查全过）",
          all(img.passed for img in rep_bp.images)
          and rep_bp.blur_pair is not None and not rep_bp.blur_pair["passed"],
          rep_bp.blur_pair["reason"] if rep_bp.blur_pair else "")
    check("成对模糊只重拍糊图(2号)",
          rep_bp.recapture_targets == [soft_p], rep_bp.recapture_targets)
    check("成对模糊汇总分类归模糊",
          _categories_of(rep_bp) == {"模糊"}, _categories_of(rep_bp))
    exp_bp = collect_failures(rep_bp)
    check("导出脚本识别 blur_pair 且只标糊图",
          exp_bp[sharp_p]["hits"] == [] and exp_bp[soft_p]["hits"] == ["blur_pair"], exp_bp)

    fake_rule_a = QCReport(
        pair_id="ra",
        images=[ImageResult(path="1.png", checks=[
            checks._fail("completeness", {}, {},
                         "底部无金属长边结构（底部区挺直度峰值 0.12 < 0.25），疑似未拍全根盒下侧")
        ])],
        repeat_pair=checks._fail("repeat", {}, {}, "两张灯照相同，疑似重复拍摄/相机未移动，无法拼接"),
        illumination_pair=checks._fail("illum_pair", {}, {}, "1 号图亮像素占比远低于另一张，疑似只有一根灯管亮（照明异常）"),
    )
    check("Rule A/重复/单灯管分类不落入其它",
          _categories_of(fake_rule_a) == {"不全", "重复拍摄", "损坏"}, _categories_of(fake_rule_a))
    exp_ra = collect_failures(fake_rule_a)
    check("导出脚本包含 completeness/repeat/illum_pair",
          set(exp_ra["1.png"]["hits"]) == {"completeness", "repeat", "illum_pair"}, exp_ra["1.png"]["hits"])

    q_dir = os.path.join(OUT, "qtest")
    q_path = os.path.join(q_dir, "recapture_queue.jsonl")
    fn = FileNotifier(q_dir, q_path)
    old_rot = notifier_mod._QUEUE_ROTATE_BYTES
    try:
        notifier_mod._QUEUE_ROTATE_BYTES = 50
        for idx in range(4):
            fn.notify({"pair_id": f"p{idx}", "recapture_needed": True,
                       "reasons": ["r"], "targets": ["t"], "attempt": 1, "timestamp": "now"})
    finally:
        notifier_mod._QUEUE_ROTATE_BYTES = old_rot
    check("队列二次轮转后仍可写入",
          os.path.isfile(q_path) and os.path.isfile(q_path + ".1") and os.path.getsize(q_path) > 0)

    print("== 7. P1/P2 回归：JPEG截断 / 缺省阈值对齐 / input_kind / 预检复用 / 熔断追踪 / 多扩展名配对 ==")
    from rootbox_qc.verify import RecaptureTracker
    from rootbox_qc.runner import collect_pairs

    _, jpg_buf = cv2.imencode(".jpg", scene(0))
    raw_bytes = jpg_buf.tobytes()
    jpg_trunc = os.path.join(OUT, "trunc_half.jpg")
    with open(jpg_trunc, "wb") as _jf:
        _jf.write(raw_bytes[:-2])
    dec_img = cv2.imdecode(np.fromfile(jpg_trunc, np.uint8), cv2.IMREAD_COLOR)
    rc_trunc = checks.check_corruption(path=jpg_trunc, img_bgr=dec_img, th=th)
    check("JPEG 末尾截断缺 EOI 判损坏",
          not rc_trunc["passed"] and rc_trunc["metric"].get("jpeg_eoi_ok") is False,
          rc_trunc["reason"])

    rt_partial = checks.check_completeness(None, full_box, {"bright_ratio": 0.05}, {})
    check("空阈值 dict 自动对齐 config.THRESHOLDS",
          rt_partial["passed"] and rt_partial["threshold"]["bright_ratio_max"] == th["completeness_bright_ratio_max"],
          rt_partial["threshold"])

    raw_As = checks.check_alignment_single(None, scene(0), fa, {"input_kind": "raw"})
    check("input_kind=raw 跳过 alignment_single",
          raw_As["passed"] and raw_As["metric"].get("alignment_single_check") == "skipped_raw",
          raw_As["metric"])

    pre_a = verify_single(pc1, th)
    pre_b = verify_single(pc2, th)
    rep_reuse = verify_pair(pc1, pc2, th, res_a=pre_a, res_b=pre_b)
    check("verify_pair 支持复用 verify_single 结果",
          rep_reuse.images[0] is pre_a.images[0] and rep_reuse.images[1] is pre_b.images[0])

    tracker = RecaptureTracker(max_attempts=2, max_consecutive_same_category=3)
    for idx in range(1, 4):
        r_trk = QCReport(
            pair_id=f"box_{idx:03d}",
            recapture_needed=True,
            recapture_reasons=["[1.png] 模糊"],
            recapture_targets=["1.png"],
            attempt=2 if idx == 2 else 1,
            images=[ImageResult(path="1.png",
                                checks=[checks._fail("blur", {}, {}, "模糊")])],
        )
        tracker.observe(r_trk)
        if idx == 2:
            check("attempt>=max_attempts 标记 escalate_manual", r_trk.escalate_manual)
            fn.notify(r_trk.to_dict())
    check("attempt>1 额外保留 _attempt2.json 历史快照",
          os.path.isfile(os.path.join(q_dir, "box_002_attempt2.json")))
    check("连续 3 组同类失败触发 systemic_alert",
          bool(r_trk.systemic_alert and "模糊连续3组" in r_trk.systemic_alert), r_trk.systemic_alert)

    ra_same = checks.check_alignment_pair(orig, orig, fa, fa, th, path_a=p1, path_b=p2)
    check("字节相同重复图在 alignment_pair 中互斥跳过",
          ra_same["passed"] and ra_same["metric"].get("skipped") == "file_identical", ra_same["metric"])

    sub_root = os.path.join(OUT, "scan_mode_d")
    for batch in ("batchA", "batchB"):
        bdir = os.path.join(sub_root, batch, "001")
        os.makedirs(bdir, exist_ok=True)
        cv2.imwrite(os.path.join(bdir, "1.jpg"), orig)
        cv2.imwrite(os.path.join(bdir, "2.jpg"), orig)
    scanned = collect_pairs(sub_root, r"^(?P<prefix>.+)[_-](?P<idx>[12])\.(?:jpg|jpeg|png|bmp)$")
    check("子目录 1.jpg+2.jpg 配对 & 模式 D 多子目录汇总", len(scanned) == 2, scanned)

    print()
    print("ALL PASS" if ok_all else "SOME FAILED")
    sys.exit(0 if ok_all else 1)
finally:
    shutil.rmtree(OUT, ignore_errors=True)
