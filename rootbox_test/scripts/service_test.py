"""监听服务（rootbox_qc.service）回归测试。

用真图（dataed 里的 067 正常对、1491 不全对）+ 真拼接脚本 + 真 HTTP 跑一遍，
覆盖上线前必须成立的 7 条：
  1. 落图目录识别（编号子目录 / 批次+编号 / 平铺 xxx_1.png）
  2. fail-closed：生产模式缺拼接配置时拒绝启动
  3. shadow：不合格对只写 would_*，不进真重拍队列
  4. live：不合格对进真重拍队列
  5. 重拍：同 pair 文件变化 → attempt+1，且重启后不丢
  6. 已处理不重复质检（指纹相同直接跳过）
  7. /health 200 + /metrics 有指标；HTTP 通知带 Idempotency-Key

用法：python scripts/service_test.py [--keep]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from rootbox_qc.config import load_config  # noqa: E402
from rootbox_qc.notifier import HttpNotifier  # noqa: E402
from rootbox_qc.service import (  # noqa: E402
    Settings, StartupError, WatchService, discover, setup_service_logging,
)

DATAED = os.environ.get("ROOTBOX_QC_DATA", "")
STITCH_SCRIPT = os.environ.get("ROOTBOX_QC_STITCH_SCRIPT", "")
WORK = os.path.join(PKG_ROOT, "_tmp", "service_test")
PASS_PAIR = ("正常", "067")
FAIL_PAIR = ("不全", "1491")

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")
    return cond


def link_pair(src_dir: str, dst_dir: str) -> None:
    """硬链接拷一对图（同盘零拷贝，跨盘退化为复制）。"""
    os.makedirs(dst_dir, exist_ok=True)
    for name in ("1.png", "2.png"):
        src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        if os.path.exists(dst):
            os.remove(dst)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_settings(cfg: dict, base: str, *, shadow: bool, require_stitch: bool = True,
                  health_port: int = 0, settle: float = 0.0) -> Settings:
    return Settings.from_env(
        cfg,
        watch_dir=os.path.join(base, "watch"),
        output_dir=os.path.join(base, "out"),
        feedback_dir=os.path.join(base, "feedback"),
        log_dir=os.path.join(base, "logs"),
        shadow=shadow,
        once=True,
        settle_seconds=settle,
        health_port=health_port,
        require_stitch=require_stitch,
    )


# --------------------------------------------------------------------------- 1
def test_discovery(cfg: dict, base: str) -> bool:
    print("\n[1] 落图目录识别")
    watch = os.path.join(base, "watch")
    link_pair(os.path.join(DATAED, *PASS_PAIR), os.path.join(watch, "067"))
    link_pair(os.path.join(DATAED, *FAIL_PAIR), os.path.join(watch, "1491"))
    link_pair(os.path.join(DATAED, *FAIL_PAIR), os.path.join(watch, "20261030", "1492"))
    link_pair(os.path.join(DATAED, *PASS_PAIR), os.path.join(watch, "flat"))
    for name in ("1.png", "2.png"):
        src = os.path.join(watch, "flat", name)
        os.replace(src, os.path.join(watch, f"ABC12_{name[0]}.png"))

    cands = discover(watch, cfg)
    ids = {c.pair_id for c in cands}
    ok = check("识别 4 对（编号/批次/平铺）", len(cands) == 4, f"得到 {sorted(ids)}")
    ok &= check("编号目录 pair_id=067", "067" in ids, str(ids))
    ok &= check("批次目录 pair_id=20261030__1492", "20261030__1492" in ids, str(ids))
    ok &= check("平铺 pair_id=ABC12", "ABC12" in ids, str(ids))
    for c in cands:
        if c.pair_id == "ABC12":
            names = [os.path.basename(c.path_a), os.path.basename(c.path_b)]
            ok &= check("平铺取 1/2 两张", names == ["ABC12_1.png", "ABC12_2.png"], str(names))

    # 输出目录不能反过来被当成落图
    cands2 = discover(watch, cfg, skip_paths=(os.path.join(watch, "20261030"),))
    ok &= check("skip_paths 生效", all(c.pair_id != "20261030__1492" for c in cands2),
                str({c.pair_id for c in cands2}))
    return ok


# --------------------------------------------------------------------------- 2
def test_fail_closed(cfg: dict, base: str) -> bool:
    print("\n[2] fail-closed 启动自检")
    s = make_settings(cfg, os.path.join(base, "fc"), shadow=False, require_stitch=True)
    cfg_no_stitch = json.loads(json.dumps(cfg))
    cfg_no_stitch["paths"]["stitch_output_dir"] = None
    svc = WatchService(s, cfg_no_stitch)
    problems = svc.validate()
    ok = check("缺 stitch_output_dir → 自检报错", any("拼接输出目录" in p for p in problems),
               str(problems))

    svc2 = WatchService(make_settings(cfg, os.path.join(base, "fc2"), shadow=False),
                        cfg_no_stitch)
    try:
        svc2.start()
    except StartupError:
        svc2.stop()
        ok &= check("缺拼接配置时启动被拒", True)
    else:
        svc2.stop()
        ok &= check("缺拼接配置时启动被拒", False, "竟然启动成功了")
    return ok


# --------------------------------------------------------------------------- 3
def test_shadow(cfg: dict, base: str) -> bool:
    print("\n[3] shadow 模式：只记录不拦车")
    b = os.path.join(base, "shadow")
    link_pair(os.path.join(DATAED, *PASS_PAIR), os.path.join(b, "watch", "067"))
    link_pair(os.path.join(DATAED, *FAIL_PAIR), os.path.join(b, "watch", "1491"))
    s = make_settings(cfg, b, shadow=True)
    svc = WatchService(s, cfg)
    svc.run_forever()

    ok = check("两对都处理了", svc.metrics.counters.get("pairs_total", 0) == 2,
               str(svc.metrics.counters))
    fb = read_json(os.path.join(b, "feedback", "1491.json"))
    ok &= check("shadow 反馈 mode=shadow", fb["mode"] == "shadow", str(fb["mode"]))
    ok &= check("shadow 不驱动重拍", fb["recapture_needed"] is False, str(fb))
    ok &= check("shadow 记录 would_recapture", fb.get("would_recapture") is True, str(fb))
    ok &= check("shadow would_reasons 有内容", bool(fb.get("would_reasons")), str(fb))
    ok &= check("shadow 不写 recapture_queue",
                not os.path.isfile(os.path.join(b, "out", "recapture_queue.jsonl")))
    shadow_q = read_jsonl(os.path.join(b, "out", "shadow_queue.jsonl"))
    ok &= check("shadow_queue 有这条", [q["pair_id"] for q in shadow_q] == ["1491"],
                str(shadow_q))
    ok &= check("完整档带 mode/enforced", read_json(
        os.path.join(b, "out", "1491.json"))["enforced"] is False)
    fb_pass = read_json(os.path.join(b, "feedback", "067.json"))
    ok &= check("合格对 status=pass 且拼接复检通过",
                fb_pass["status"] == "pass" and fb_pass["stitched_status"] == "pass",
                str(fb_pass))
    return ok


# --------------------------------------------------------------------------- 4–6
def test_live_and_retake(cfg: dict, base: str) -> bool:
    print("\n[4] live 模式：不合格对进真队列")
    b = os.path.join(base, "live")
    link_pair(os.path.join(DATAED, *FAIL_PAIR), os.path.join(b, "watch", "1491"))
    s = make_settings(cfg, b, shadow=False)
    svc = WatchService(s, cfg)
    svc.run_forever()

    fb = read_json(os.path.join(b, "feedback", "1491.json"))
    ok = check("live 反馈 recapture_needed=true", fb["recapture_needed"] is True, str(fb))
    ok &= check("live 反馈 targets 非空", bool(fb["targets"]), str(fb["targets"]))
    ok &= check("live 反馈 categories 含 不全", "不全" in fb["failure_categories"],
                str(fb["failure_categories"]))
    rq = read_jsonl(os.path.join(b, "out", "recapture_queue.jsonl"))
    ok &= check("recapture_queue 有这条", [r["pair_id"] for r in rq] == ["1491"], str(rq))
    ok &= check("没有 shadow 队列", not os.path.isfile(os.path.join(b, "out", "shadow_queue.jsonl")))

    print("\n[5] 重拍：文件变化 → attempt+1")
    time.sleep(1.1)
    p = os.path.join(b, "watch", "1491", "1.png")
    os.utime(p, None)
    svc2 = WatchService(s, cfg)  # 新实例：走状态文件恢复，模拟服务重启
    svc2.run_forever()
    fb2 = read_json(os.path.join(b, "feedback", "1491.json"))
    ok &= check("重拍 attempt=2", fb2["attempt"] == 2, str(fb2["attempt"]))
    ok &= check("重启后 tracker 恢复", svc2.tracker.pair_attempts.get("1491") == 2,
                str(svc2.tracker.pair_attempts))

    print("\n[6] 已处理不重复质检")
    svc3 = WatchService(s, cfg)
    svc3.run_forever()
    ok &= check("指纹未变 → 不再处理", svc3.metrics.counters.get("pairs_total", 0) == 0,
                str(svc3.metrics.counters))
    return ok


# --------------------------------------------------------------------------- 7
def test_health_and_idempotency(cfg: dict, base: str) -> bool:
    print("\n[7] /health、/metrics 与通知幂等键")
    b = os.path.join(base, "health")
    os.makedirs(os.path.join(b, "watch"), exist_ok=True)
    port = free_port()
    s = make_settings(cfg, b, shadow=True, health_port=port)
    svc = WatchService(s, cfg)
    svc.start()
    ok = True
    code, body = 0, None
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=2) as r:
                    code, body = r.status, r.read().decode("utf-8")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        health = json.loads(body) if body else {}
        ok &= check("/health 200 且 status=ok", code == 200 and health.get("status") == "ok",
                    f"code={code} body={body}")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as r:
            text = r.read().decode("utf-8")
        ok &= check("/metrics 含 pairs_total", "rootbox_qc_pairs_total" in text,
                    text[:200])
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=2)
            ok &= check("未知路径 404", False, "竟然返回 200")
        except urllib.error.HTTPError as e:
            ok &= check("未知路径 404", e.code == 404, str(e.code))
    finally:
        svc.stop()

    captured = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            captured["body"] = self.rfile.read(n)
            captured["headers"] = dict(self.headers)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):  # noqa: A003
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", free_port()), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/qc"
    try:
        n = HttpNotifier(url, notify_on_pass=True)
        n.notify({"pair_id": "P1", "attempt": 2, "recapture_needed": True})
        ok &= check("通知带 Idempotency-Key=P1:2",
                    captured.get("headers", {}).get("Idempotency-Key") == "P1:2",
                    str(captured.get("headers")))
        captured.clear()
        HttpNotifier(url, notify_on_pass=False).notify(
            {"pair_id": "P2", "attempt": 1, "recapture_needed": False})
        ok &= check("合格对默认不回调", "headers" not in captured, str(captured))
        captured.clear()
        HttpNotifier(url, notify_on_pass=True, idempotency=False).notify(
            {"pair_id": "P3", "attempt": 1, "recapture_needed": True})
        ok &= check("可关闭幂等键", "Idempotency-Key" not in captured.get("headers", {}),
                    str(captured.get("headers")))
    finally:
        srv.shutdown()
        srv.server_close()
    return ok


# ------------------------------------------------------------------------ 8
def test_production_layout(cfg: dict, base: str) -> bool:
    """产线真实结构：<watch>/data1030/0001/1.png → save/data1030/0001.png。"""
    print("\n[8] 产线目录结构：批次/编号 → save/<批次>/<编号>.png")
    b = base
    pair_dir = os.path.join(b, "watch", "data1030", "0001")
    link_pair(os.path.join(DATAED, *PASS_PAIR), pair_dir)

    cands = discover(os.path.join(b, "watch"), cfg)
    ok = check("识别 data1030__0001", [c.pair_id for c in cands] == ["data1030__0001"],
               str([c.pair_id for c in cands]))
    ok &= check("带上批次/编号", cands and cands[0].batch == "data1030"
                and cands[0].number == "0001",
                str((cands[0].batch, cands[0].number)) if cands else "无候选")

    svc = WatchService(make_settings(cfg, b, shadow=False), cfg)
    svc.run_forever()

    stitched = os.path.join((cfg.get("paths") or {})["stitch_output_dir"],
                            "data1030", "0001.png")
    ok &= check("成品落到 save/data1030/0001.png", os.path.isfile(stitched), stitched)
    ok &= check("成品不是 data1030__0001.png",
                not os.path.isfile(os.path.join(
                    (cfg.get("paths") or {})["stitch_output_dir"],
                    "data1030__0001.png")))
    rep = read_json(os.path.join(b, "out", "data1030__0001.json"))
    ok &= check("报告里 stitched_path 指向成品", os.path.normcase(
        rep.get("stitched_path") or "") == os.path.normcase(stitched),
        str(rep.get("stitched_path")))
    fb = read_json(os.path.join(b, "feedback", "data1030__0001.json"))
    ok &= check("扁平反馈带 batch/number", fb.get("batch") == "data1030"
                and fb.get("number") == "0001", str({k: fb.get(k) for k in ("batch", "number")}))
    mirror = os.path.join(b, "feedback", "data1030", "0001.json")
    ok &= check("镜像反馈 feedback/data1030/0001.json", os.path.isfile(mirror), mirror)
    if os.path.isfile(mirror):
        ok &= check("镜像内容与扁平一致", read_json(mirror)["pair_id"] == "data1030__0001")
    return ok


def main() -> int:
    global DATAED
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留测试目录")
    ap.add_argument("--data", default=DATAED,
                    help="真样本根目录；也可设环境变量 ROOTBOX_QC_DATA")
    ap.add_argument("--stitch-script", default=STITCH_SCRIPT,
                    help="拼接脚本路径；也可设环境变量 ROOTBOX_QC_STITCH_SCRIPT")
    args = ap.parse_args()

    DATAED = args.data

    cfg = load_config()
    stitch_dir = (cfg.get("paths") or {}).get("stitch_output_dir")
    if not stitch_dir:
        print("需要配置 ROOTBOX_QC_STITCH_DIR（生产同款拼接目录）后运行本测试")
        return 2
    if not args.stitch_script or not os.path.isfile(args.stitch_script):
        print("需要拼接脚本：--stitch-script <路径> 或环境变量 ROOTBOX_QC_STITCH_SCRIPT")
        return 2
    cfg.setdefault("paths", {})["stitch_script"] = args.stitch_script
    if not DATAED:
        print("需要真样本根目录：--data <路径> 或环境变量 ROOTBOX_QC_DATA\n"
              "  目录结构应为 <根>/正常/067/1.png + 2.png、<根>/不全/1491/1.png + 2.png")
        return 2
    if not os.path.isdir(os.path.join(DATAED, *PASS_PAIR)):
        print(f"缺测试样本 {os.path.join(DATAED, *PASS_PAIR)}")
        return 2

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    setup_service_logging(os.path.join(WORK, "logs"))

    t0 = time.perf_counter()
    ok = test_discovery(cfg, os.path.join(WORK, "discover"))
    ok &= test_fail_closed(cfg, os.path.join(WORK, "failclosed"))
    ok &= test_shadow(cfg, os.path.join(WORK, "shadow"))
    ok &= test_live_and_retake(cfg, os.path.join(WORK, "live"))
    ok &= test_health_and_idempotency(cfg, os.path.join(WORK, "health"))
    ok &= test_production_layout(cfg, os.path.join(WORK, "prod"))
    dt = time.perf_counter() - t0

    print(f"\n{'='*60}\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项，"
          f"耗时 {dt:.1f}s")
    for f in FAILED:
        print(f"  ✗ {f}")
    if not args.keep and not FAILED:
        # 日志句柄还开着，Windows 上删不掉目录，先摘掉 handler
        lg = logging.getLogger("rootbox_qc")
        for h in list(lg.handlers):
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(WORK, ignore_errors=True)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
