"""按现行生产判据重分类 D:\\pic\\data 主池（dataed 已并入 data）。

`--include-total` 把 data\\total 中不在主池的单元一并检测（total 只留源件）。

全部判定均为规则驱动，不含任何单元名单：
- 生产五检（损坏/模糊/倾斜/不全/单张错位）+ 成对检查
  （整对错位/重复拍摄/成对模糊/单灯管照明，后者 2026-10-17 新增）。
- 黑帧过滤：任一帧亮区 Laplacian 方差 < 2 时，成对模糊/错位读数不可信，
  剔除该单元的模糊与错位（单元本身已由 corruption 判损坏）。
- 黑块平图过滤：损坏原因为"局部纯黑区"且自适应缝链长 < 3 时，固定缝窗
  兜底的错位读数来自黑区噪声，剔除错位（仅影响单张平图）。

用法：
  python scripts/reclass_data.py scan [--include-total]   # 跑全池检查，写缓存
  python scripts/reclass_data.py plan [--include-total]   # 生成目标标签计划
  python scripts/reclass_data.py apply --dry-run [--include-total]
  python scripts/reclass_data.py apply [--include-total]  # 落地副本 + 明细.csv
  python scripts/reclass_data.py verify               # 核验
扫描/计划/落地/核验的产物都在 results/_probe/ 下，带时间戳。
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import shutil
import sys
import time

ROOT = r"D:\pic\data"
TOTAL = os.path.join(ROOT, "total")
TABLE = os.path.join(ROOT, "明细.csv")
PROBE = r"D:\pic\rootbox_test\results\_probe"
CLASSES = ["正常", "不全", "损坏", "模糊", "重复拍摄", "错位"]
DEFECTS = ["损坏", "模糊", "不全", "错位", "重复拍摄"]   # 主分类优先级
IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp")
IMG_CLASS = {"corruption": "损坏", "blur": "模糊",
             "completeness": "不全", "alignment_single": "错位"}
PAIR_CLASS = {"alignment_pair": "错位", "repeat": "重复拍摄",
              "blur_pair": "模糊", "illum_pair": "损坏"}


# ---------------------------------------------------------------------------
# 枚举 / 扫描
# ---------------------------------------------------------------------------
def enumerate_units(root=ROOT, include_total=False):
    units = {}
    for cls in CLASSES:
        d = os.path.join(root, cls)
        if not os.path.isdir(d):
            continue
        for e in sorted(os.listdir(d)):
            p = os.path.join(d, e)
            if os.path.isdir(p):
                kind = "dir"
            elif e.lower().endswith(IMG_EXT):
                kind = "file"
            else:
                continue
            u = units.setdefault(e, {"unit": e, "kind": kind, "copies": {}})
            u["copies"][cls] = p
    if include_total:
        tdir = os.path.join(root, "total")
        if os.path.isdir(tdir):
            for e in sorted(os.listdir(tdir)):
                p = os.path.join(tdir, e)
                if e in units:
                    continue
                if os.path.isdir(p):
                    kind = "dir"
                elif e.lower().endswith(IMG_EXT):
                    kind = "file"
                else:
                    continue
                units[e] = {"unit": e, "kind": kind, "copies": {},
                            "source": p}
    return units


def _scan_one(item):
    unit, kind, src = item
    sys.path.insert(0, r"D:\pic\rootbox_test")
    from rootbox_qc import verify
    cfg = verify._as_cfg(None)
    rec = {"unit": unit, "kind": kind, "src": src,
           "images": [], "pair": {}, "error": None}
    try:
        if kind == "dir":
            p1 = os.path.join(src, "1.png")
            p2 = os.path.join(src, "2.png")
            rep = verify.verify_pair(p1, p2 if os.path.exists(p2) else None,
                                     cfg=cfg, pair_id=unit)
        else:
            rep = verify.verify_single(src, cfg=cfg, pair_id=unit, input_kind="mosaic")
        for res in rep.images:
            im = {"img": os.path.basename(res.path),
                  "load_error": res.load_error, "checks": []}
            for c in res.checks:
                m = c.get("metric") or {}
                keep = {}
                if c["name"] == "blur":
                    keep = {k: m.get(k) for k in
                            ("roi_laplacian_var", "blur_strip_alive_var",
                             "blur_strip_surviving_var", "blur_strip_role")}
                elif c["name"] == "corruption":
                    keep = {k: m.get(k) for k in
                            ("dark_blocks", "dim_blocks", "bright_block",
                             "gray_std")}
                elif c["name"] == "alignment_single":
                    keep = {"chain_len": m.get("chain_len"),
                            "seam_hits": m.get("seam_hits")}
                im["checks"].append({"name": c["name"], "passed": c["passed"],
                                     "reason": c["reason"], "metric": keep})
            rec["images"].append(im)
        for field, name in (("alignment_pair", "alignment_pair"),
                            ("repeat_pair", "repeat"),
                            ("blur_pair", "blur_pair"),
                            ("illumination_pair", "illum_pair")):
            r = getattr(rep, field, None)
            if r is None:
                continue
            m = r.get("metric") or {}
            keep = {}
            if name == "blur_pair":
                keep = {k: m.get(k) for k in
                        ("roi_var", "soft_roi_var", "sharp_roi_var",
                         "roi_var_ratio", "soft_role")}
            elif name == "alignment_pair":
                keep = {"band_median":
                        (m.get("band") or {}).get("median_abs_dy_px"),
                        "band_dx":
                        (m.get("band") or {}).get("median_abs_dx_px")}
            elif name == "illum_pair":
                keep = {k: m.get(k) for k in
                        ("bright_ratio", "dark_ratio", "bright_ratio_max",
                         "dark_role")}
            rec["pair"][name] = {"passed": r["passed"],
                                 "reason": r["reason"], "metric": keep}
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


def cmd_scan(args):
    units = enumerate_units(include_total=args.include_total)
    items = []
    for u in sorted(units.values(), key=lambda x: x["unit"]):
        if args.only_class and args.only_class not in u["copies"]:
            continue
        src = (u["copies"].get(args.only_class)
               if (args.only_class and args.only_class in u["copies"])
               else (sorted(u["copies"].items())[0][1] if u["copies"] else u.get("source")))
        if not src:
            continue
        items.append((u["unit"], u["kind"], src))
    print(f"units {len(items)}", flush=True)
    import multiprocessing as mp
    t0 = time.time()
    rows = []
    with mp.Pool(args.jobs) as pool:
        for i, r in enumerate(pool.imap_unordered(_scan_one, items, chunksize=1), 1):
            rows.append(r)
            if i % 25 == 0:
                print(f"{i}/{len(items)} {time.time()-t0:.0f}s", flush=True)
    rows.sort(key=lambda x: x["unit"])
    out = os.path.join(PROBE, f"reclass_data_scan_{args.stamp}.json")
    json.dump({"root": ROOT, "only_class": args.only_class,
               "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "units": rows}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False)
    print("saved", out, f"{time.time()-t0:.0f}s")


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------
def derive(rec):
    """返回 (labels set, reasons dict, flags list)。"""
    hits, reasons, flags = set(), {}, []

    def add(cls, reason):
        hits.add(cls)
        reasons.setdefault(cls, []).append(reason)

    for im in rec["images"]:
        if im["load_error"]:
            add("损坏", f"[{im['img']}] 图片损坏({im['load_error']})")
        for c in im["checks"]:
            if c["passed"]:
                continue
            cls = IMG_CLASS.get(c["name"])
            if cls:
                add(cls, f"[{im['img']}] {c['reason']}")
    for name, cls in PAIR_CLASS.items():
        r = rec["pair"].get(name)
        if r and not r["passed"]:
            add(cls, "[整对] " + r["reason"])

    # 黑帧：任一帧亮区/存活列方差 <2，成对读数不可信
    black = False
    for im in rec["images"]:
        for c in im["checks"]:
            if c["name"] != "blur":
                continue
            m = c["metric"]
            v, alive = m.get("roi_laplacian_var"), m.get("blur_strip_alive_var")
            if (v is not None and v < 2) or (alive is not None and alive < 2):
                black = True
    if black:
        for cls in ("模糊", "错位"):
            if cls in hits:
                hits.discard(cls)
                reasons.pop(cls, None)
                flags.append(f"黑帧剔除{cls}")

    # 黑块平图：局部纯黑区 + 自适应缝链 <3，固定缝窗错位是黑区噪声
    if "局部纯黑区" in " ".join(reasons.get("损坏", [])):
        chain = None
        for im in rec["images"]:
            for c in im["checks"]:
                if c["name"] == "alignment_single" and not c["passed"]:
                    chain = c["metric"].get("chain_len")
        if chain is not None and chain < 3 and "错位" in hits:
            hits.discard("错位")
            reasons.pop("错位", None)
            flags.append("黑块剔除错位")
    return hits, reasons, flags


def cmd_plan(args):
    scan = json.load(open(os.path.join(PROBE, f"reclass_data_scan_{args.stamp}.json"),
                          encoding="utf-8"))
    current = enumerate_units(include_total=args.include_total)
    plan = []
    for rec in scan["units"]:
        u = current.get(rec["unit"])
        if u is None:
            print("! 当前池里找不到", rec["unit"])
            continue
        hits, reasons, flags = derive(rec)
        labels = [c for c in DEFECTS if c in hits]
        main = labels[0] if labels else "正常"
        plan.append({
            "unit": rec["unit"], "kind": rec["kind"],
            "current": sorted(u["copies"]), "copies": {k: v for k, v in u["copies"].items()},
            "labels": labels or ["正常"], "main": main,
            "reasons": {k: v for k, v in reasons.items() if k in labels},
            "flags": flags, "error": rec["error"],
        })
    out = os.path.join(PROBE, f"reclass_data_plan_{args.stamp}.json")
    json.dump({"root": ROOT, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "units": plan}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False)
    changed = [p for p in plan if set(p["current"]) != set(p["labels"])]
    cnt = collections.Counter(p["main"] for p in plan)
    print("plan units", len(plan), "changed", len(changed))
    print("main", dict(cnt))
    add = collections.Counter()
    drop = collections.Counter()
    for p in changed:
        for c in set(p["labels"]) - set(p["current"]):
            add[c] += 1
        for c in set(p["current"]) - set(p["labels"]):
            drop[c] += 1
    print("add", dict(add), "drop", dict(drop))
    for p in changed:
        print(" ", p["unit"], p["current"], "->", p["labels"],
              ("flags=" + ",".join(p["flags"])) if p["flags"] else "")
    print("saved", out)


# ---------------------------------------------------------------------------
# 落地
# ---------------------------------------------------------------------------
def _copy_unit(src, dst, kind):
    if kind == "dir":
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def _remove_unit(path, kind):
    if kind == "dir":
        shutil.rmtree(path)
    else:
        os.remove(path)


def cmd_apply(args):
    plan_path = os.path.join(PROBE, f"reclass_data_plan_{args.stamp}.json")
    plan = json.load(open(plan_path, encoding="utf-8"))["units"]
    current = enumerate_units(include_total=args.include_total)
    log = []

    def out(s):
        print(s, flush=True)
        log.append(s)

    add_n = del_n = 0
    for p in plan:
        u = current.get(p["unit"])
        if u is None:
            out(f"! {p['unit']} 当前不存在，跳过")
            continue
        cur = set(u["copies"])
        tgt = set(p["labels"])
        src = (sorted(u["copies"].items())[0][1] if u["copies"]
               else u.get("source"))
        if not src:
            out(f"! {p['unit']} 无源件，跳过")
            continue
        adds = sorted(tgt - cur)
        dels = sorted(cur - tgt)
        if not adds and not dels:
            continue
        out(f"{p['unit']}: {sorted(cur)} -> {sorted(tgt)}"
            + (f"  (+{adds})" if adds else "") + (f"  (-{dels})" if dels else ""))
        if args.dry_run:
            add_n += len(adds)
            del_n += len(dels)
            continue
        for cls in adds:
            dst = os.path.join(ROOT, cls, p["unit"])
            _copy_unit(src, dst, p["kind"])
            add_n += 1
        # 先确认目标副本存在，再删多余副本
        for cls in tgt:
            if not os.path.exists(os.path.join(ROOT, cls, p["unit"])):
                raise RuntimeError(f"{p['unit']} 目标副本 {cls} 缺失，停止删除")
        for cls in dels:
            _remove_unit(os.path.join(ROOT, cls, p["unit"]), p["kind"])
            del_n += 1

    # 明细表：只更新本次计划的单元行（按来源路径末段匹配）
    rows = list(csv.reader(open(TABLE, encoding="utf-8-sig", newline="")))
    hdr, body = rows[0], rows[1:]
    by_unit = {p["unit"]: p for p in plan}
    tchg = 0
    for i, row in enumerate(body):
        unit = row[0].split("\\")[-1]
        p = by_unit.get(unit)
        if p is None:
            continue
        # 标签未变的单元不动：表已与实体一致，避免原因文本无意义 churn
        if set(p["current"]) == set(p["labels"]):
            continue
        labels = p["labels"]
        hits = "、".join(c for c in DEFECTS if c in labels) or "正常"
        main = p["main"]
        if labels == ["正常"]:
            reason = "五项检测全部通过"
        else:
            reason = "；".join(r for c in DEFECTS if c in labels
                              for r in p["reasons"].get(c, []))
        new = [row[0], f"{main}\\{unit}", main, hits, reason]
        if new != row:
            tchg += 1
            if not args.dry_run:
                body[i] = new
    out(f"落地{'预览' if args.dry_run else '完成'}：新增副本 {add_n}，删除副本 {del_n}，"
        f"改表 {tchg} 行")

    if not args.dry_run:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        bak = f"{TABLE}.bak_{stamp}"
        shutil.copy2(TABLE, bak)
        out(f"备份 {bak}")
        with open(TABLE, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            w.writerows(body)
        out("已写入 " + TABLE)
    log_path = os.path.join(PROBE, f"reclass_data_apply_{args.stamp}"
                            f"{'_dry' if args.dry_run else ''}.txt")
    open(log_path, "w", encoding="utf-8").write("\n".join(log))
    print("log", log_path)


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------
def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_verify(args):
    plan = json.load(open(os.path.join(PROBE, f"reclass_data_plan_{args.stamp}.json"),
                          encoding="utf-8"))["units"]
    current = enumerate_units()
    mism, conflicts, missing, md5_bad = [], [], [], []
    for p in plan:
        cur = set(current.get(p["unit"], {"copies": {}})["copies"])
        if cur != set(p["labels"]):
            mism.append((p["unit"], sorted(cur), p["labels"]))
        if "正常" in p["labels"] and len(p["labels"]) > 1:
            conflicts.append(p["unit"])
    for unit, u in current.items():
        if not u["copies"]:
            missing.append(unit)
    # MD5：每个单元所有副本内容一致（抽样 40 个单元）
    for p in plan[:40]:
        u = current.get(p["unit"])
        if not u:
            continue
        src = sorted(u["copies"].items())[0][1]
        if p["kind"] == "dir":
            base = {fn: _md5(os.path.join(src, fn)) for fn in ("1.png", "2.png")
                    if os.path.exists(os.path.join(src, fn))}
            for cls, d in u["copies"].items():
                for fn, h in base.items():
                    q = os.path.join(d, fn)
                    if not os.path.exists(q) or _md5(q) != h:
                        md5_bad.append((p["unit"], cls, fn))
        else:
            h = _md5(src)
            for cls, f in u["copies"].items():
                if _md5(f) != h:
                    md5_bad.append((p["unit"], cls))
    # 明细表
    rows = list(csv.reader(open(TABLE, encoding="utf-8-sig", newline="")))
    body = rows[1:]
    by_unit = {p["unit"]: p for p in plan}
    tbad = []
    t_rows = 0
    for row in body:
        unit = row[0].split("\\")[-1]
        p = by_unit.get(unit)
        if p is None:
            continue
        t_rows += 1
        hits = "、".join(c for c in DEFECTS if c in p["labels"]) or "正常"
        if row[2] != p["main"] or row[3] != hits:
            tbad.append((unit, row[2], row[3], p["main"], hits))
    n_total = len(os.listdir(TOTAL)) if os.path.isdir(TOTAL) else 0
    lines = [
        f"plan units {len(plan)}",
        f"label mismatches {len(mism)} {mism[:10]}",
        f"正常+缺陷 conflicts {len(conflicts)} {conflicts[:10]}",
        f"units with no copies {len(missing)} {missing[:10]}",
        f"data table rows matched {t_rows} mismatches {len(tbad)} {tbad[:10]}",
        f"MD5 sample 40 inconsistent {len(md5_bad)} {md5_bad[:10]}",
        f"total entries remaining {n_total}",
    ]
    txt = "\n".join(lines)
    print(txt)
    out = os.path.join(PROBE, f"reclass_data_verify_{args.stamp}.txt")
    open(out, "w", encoding="utf-8").write(txt + "\n")
    print("saved", out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "plan", "apply", "verify"])
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d_%H%M%S"))
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-total", action="store_true")
    ap.add_argument("--only-class", default=None,
                    help="仅扫描/处理指定分类目录下的单元（如 正常）")
    args = ap.parse_args()
    {"scan": cmd_scan, "plan": cmd_plan,
     "apply": cmd_apply, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
