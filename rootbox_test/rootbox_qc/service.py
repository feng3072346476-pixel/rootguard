"""目录监听型线上壳（watch service）：落图 → 质检 → 反馈。

为什么需要它
============
`verify_pair()` 只是判据库；生产线要的是"一有图就检、结果能拿到、缺配置不许带病上线"。
本模块补这一层（对应上线评估里缺的 4 件事）：

1. **线上入口**：监听落图目录，自动配对 → `verify_pair` → 写反馈；不依赖调用方写循环。
2. **fail-closed 启动自检**：缺拼接目录/拼接脚本、目录不可写、画幅配置缺失、cv2 不可用
   → 直接拒绝启动（`StartupError`），不会"静默只做单图检查"。
3. **可观测**：`/health`、`/metrics`、轮转日志、逐对耗时。
4. **幂等与重拍节流**：attempt 递增、`max_attempts` 转人工、HTTP 通知带 `Idempotency-Key`。

另有 **shadow 模式**：照常判定并留档，但绝不驱动重拍（反馈里 `recapture_needed=false`，
只给 `would_recapture`），用来在真产线上量"如果真拦会误停几次"。

落图约定（自动识别，任选其一）
============================
    <watch>/<pair_id>/1.png + 2.png                 # 最常见：一对一个目录
    <watch>/<pair_id>/<任意两张图>                   # 目录里恰好两张图，按文件名排序
    <watch>/<批次>/<pair_id>/1.png + 2.png          # 外面多一层日期/批次目录
    <watch>/<num>_1.png + <num>_2.png               # 平铺，按 PAIRING.regex 配对

`pair_id` = 目录名（双层时 `批次/编号`），路径分隔符在文件名里替换为 `__`。
同一 `pair_id` 再次落图（文件 size/mtime 变了）视为重拍：attempt+1，历史由
`RecaptureTracker` 汇总进报告的 `retry` 块。

反馈约定（小车/产线读这里）
==========================
    <feedback_dir>/<pair_id>.json   # 精简结论，tmp→replace 原子写入，读不到半截文件
    <feedback_dir>/feedback.jsonl   # 追加行，便于 tail / 采集（按处理顺序）
    <output_dir>/<pair_id>.json     # 完整报告（含每项检查 metric，人看/归档）
    <output_dir>/recapture_queue.jsonl  # 需重拍时追加（小车可直接读，jsonl 轮转）
    可选 HTTP POST 到 ROOTBOX_QC_CALLBACK_URL（带 Idempotency-Key: <pair_id>:<attempt>）

精简结论字段见 `WatchService._feedback_doc()`；对外契约只有
`pair_id / status / passed / recapture_needed / targets / reasons / attempt`，
其余是运维辅助信息。
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import MOSAIC_LAYOUTS, get_thresholds, load_config
from .notifier import FileNotifier, HttpNotifier
from .verify import RecaptureTracker, failed_checks, verify_pair

logger = logging.getLogger("rootbox_qc.service")

IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
# 监听目录里不该被当成产线落图目录的名字（含本服务自己的输出）
_SKIP_NAMES = {"results", "feedback", "done", "failed", "logs", "__pycache__",
               "node_modules", "rejected"}
_PREFERRED_A = ("1.png", "1.jpg", "1.jpeg", "1.bmp")
_PREFERRED_B = ("2.png", "2.jpg", "2.jpeg", "2.bmp")
_ERROR_RETRY_LIMIT = 3


class StartupError(RuntimeError):
    """启动自检未通过：列出全部问题，不做任何"降级启动"。"""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("启动自检未通过：\n  - " + "\n  - ".join(self.problems))


# ============================================================================
# 小工具
# ============================================================================
def _env_str(name: str, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _env_int(name: str, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


def _env_float(name: str, default):
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _pid_alive(pid: int) -> bool:
    """判断进程是否存活（Windows 用 OpenProcess，避免 os.kill(pid,0) 误杀）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:  # noqa: BLE001 —— 探测失败按"活着"处理，宁可拒绝启动
        return True


def _atomic_json(path: str, obj) -> None:
    """原子写 JSON：同目录 tmp → fsync → os.replace，读者永远看不到半截文件。"""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _append_jsonl(path: str, item: dict) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _header_ok(path: str) -> bool:
    """只看文件头：挡住 0 字节 / 半截写入的占位文件（完整解码留给 verify）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    if len(head) < 4:
        return False
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if head[:3] == b"\xff\xd8\xff":
        return True
    if head[:2] == b"BM":
        return True
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return True
    return False


def _safe_id(text: str) -> str:
    """把相对路径变成可做文件名的 pair_id（保留中文与 -_.，分隔符换 __）。"""
    return re.sub(r"[\\/]+", "__", text).strip("_") or "pair"


def _fingerprint(paths) -> tuple:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((os.path.basename(p), int(st.st_size), int(st.st_mtime)))
        except OSError:
            out.append((os.path.basename(p), -1, -1))
    return tuple(sorted(out))


@dataclass
class Candidate:
    pair_id: str
    path_a: str
    path_b: str
    fingerprint: tuple = field(default_factory=tuple)
    batch: str | None = None      # 落图 <watch>/<批次>/<编号>/ 时的批次名
    number: str | None = None     # 同上时的编号，用来落 save/<批次>/<编号>.png


# ============================================================================
# 配置
# ============================================================================
@dataclass
class Settings:
    watch_dir: str
    output_dir: str
    feedback_dir: str
    state_file: str
    recapture_queue: str
    shadow_queue: str
    log_dir: str | None = None
    shadow: bool = False
    settle_seconds: float = 2.0
    poll_seconds: float = 1.0
    health_host: str = "127.0.0.1"
    health_port: int = 0
    callback_url: str | None = None
    require_stitch: bool = True
    max_attempts: int | None = None
    once: bool = False
    dry_run: bool = False

    @classmethod
    def from_env(cls, cfg: dict, **overrides) -> "Settings":
        paths = cfg.get("paths") or {}
        out_dir = overrides.get("output_dir") or (_env_str("ROOTBOX_QC_OUTPUT_DIR")
                                                  or paths.get("output_dir") or "./results")
        fb_dir = overrides.get("feedback_dir") or (_env_str("ROOTBOX_QC_FEEDBACK_DIR")
                                                   or paths.get("feedback_dir")
                                                   or os.path.join(out_dir, "feedback"))
        s = cls(
            watch_dir=(overrides.get("watch_dir") or _env_str("ROOTBOX_QC_WATCH_DIR")
                       or paths.get("input_dir") or ""),
            output_dir=out_dir,
            feedback_dir=fb_dir,
            state_file=(overrides.get("state_file") or _env_str("ROOTBOX_QC_STATE_FILE")
                        or paths.get("state_file")
                        or os.path.join(out_dir, "service_state.json")),
            # 服务写的东西默认都落在 output_dir 下，避免和 runner 的旧队列串台
            recapture_queue=(overrides.get("recapture_queue")
                             or _env_str("ROOTBOX_QC_RECAPTURE_QUEUE")
                             or os.path.join(out_dir, "recapture_queue.jsonl")),
            shadow_queue=(overrides.get("shadow_queue")
                          or os.path.join(out_dir, "shadow_queue.jsonl")),
            log_dir=(overrides.get("log_dir") or _env_str("ROOTBOX_QC_LOG_DIR")
                     or paths.get("log_dir") or "./logs"),
            shadow=bool(overrides.get("shadow")
                        if overrides.get("shadow") is not None
                        else _env_bool("ROOTBOX_QC_SHADOW", False)),
            settle_seconds=float(overrides.get("settle_seconds")
                                 if overrides.get("settle_seconds") is not None
                                 else _env_float("ROOTBOX_QC_SETTLE_SECONDS", 2.0)),
            poll_seconds=float(overrides.get("poll_seconds")
                               if overrides.get("poll_seconds") is not None
                               else _env_float("ROOTBOX_QC_POLL_SECONDS", 1.0)),
            health_host=(overrides.get("health_host")
                         or _env_str("ROOTBOX_QC_HEALTH_HOST", "127.0.0.1")),
            health_port=int(overrides.get("health_port")
                            if overrides.get("health_port") is not None
                            else _env_int("ROOTBOX_QC_HEALTH_PORT", 0)),
            callback_url=(overrides.get("callback_url")
                          or _env_str("ROOTBOX_QC_CALLBACK_URL")),
            require_stitch=bool(overrides.get("require_stitch")
                                if overrides.get("require_stitch") is not None
                                else _env_bool("ROOTBOX_QC_REQUIRE_STITCH", True)),
            max_attempts=(overrides.get("max_attempts")
                          if overrides.get("max_attempts") is not None
                          else _env_int("ROOTBOX_QC_MAX_ATTEMPTS", None)),
            once=bool(overrides.get("once", False)),
            dry_run=bool(overrides.get("dry_run", False)),
        )
        if s.settle_seconds < 0:
            s.settle_seconds = 0.0
        if s.poll_seconds <= 0:
            s.poll_seconds = 0.05
        return s


# ============================================================================
# 状态持久化（重启不重复处理、重拍次数不丢）
# ============================================================================
class ServiceState:
    VERSION = 1

    def __init__(self, path: str):
        self.path = path
        self.data = {"version": self.VERSION, "pairs": {}, "tracker": {}}

    def load(self) -> None:
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("状态文件格式错误")
            data.setdefault("pairs", {})
            data.setdefault("tracker", {})
            self.data = data
        except (OSError, ValueError) as e:
            backup = f"{self.path}.corrupt.{int(time.time())}"
            logger.error("状态文件损坏（%s），已备份到 %s，按空状态继续；"
                         "重拍次数将从 1 重新计。", e, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        _atomic_json(self.path, self.data)

    def processed_fingerprint(self, pair_id: str):
        """已成功处理过的指纹；异常重试中的记录不算（否则一次瞬时错误会永久跳过）。"""
        rec = (self.data.get("pairs") or {}).get(pair_id)
        if not rec or rec.get("processed") is False:
            return None
        fp = rec.get("fingerprint")
        return tuple(tuple(x) for x in fp) if fp else None

    def record(self, pair_id: str, fingerprint: tuple, attempt: int,
               status: str, elapsed_ms: float, error: bool = False) -> None:
        pairs = self.data.setdefault("pairs", {})
        prev = pairs.get(pair_id) or {}
        pairs[pair_id] = {
            "fingerprint": [list(x) for x in fingerprint],
            "attempt": attempt,
            "status": status,
            "elapsed_ms": round(elapsed_ms, 1),
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "error_retries": int(prev.get("error_retries", 0)) + (1 if error else 0),
            "processed": not error,
        }

    def error_retries(self, pair_id: str) -> int:
        rec = (self.data.get("pairs") or {}).get(pair_id) or {}
        return int(rec.get("error_retries", 0) or 0)

    def dump_tracker(self, tracker: RecaptureTracker) -> None:
        self.data["tracker"] = {
            "pair_attempts": dict(tracker.pair_attempts),
            "consecutive_by_cat": dict(tracker.consecutive_by_cat),
            "attempt_history": {k: list(v) for k, v in tracker.attempt_history.items()},
        }

    def restore_tracker(self, tracker: RecaptureTracker) -> None:
        t = self.data.get("tracker") or {}
        tracker.pair_attempts = {str(k): int(v) for k, v in
                                 (t.get("pair_attempts") or {}).items()}
        tracker.consecutive_by_cat = {str(k): int(v) for k, v in
                                      (t.get("consecutive_by_cat") or {}).items()}
        tracker.attempt_history = {str(k): list(v) for k, v in
                                   (t.get("attempt_history") or {}).items()}


# ============================================================================
# 指标
# ============================================================================
class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.counters = {
            "pairs_total": 0, "pass_total": 0, "fail_total": 0,
            "undetermined_total": 0, "errors_total": 0,
            "shadow_would_recapture_total": 0,
            "escalations_total": 0, "systemic_alerts_total": 0,
            "resolution_mismatch_total": 0, "notify_fail_total": 0,
        }
        self.gauges = {"pending_pairs": 0, "processed_cache": 0}
        self.last = {}
        self._elapsed = []

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def set_gauge(self, name: str, value) -> None:
        with self._lock:
            self.gauges[name] = value

    def observe(self, doc: dict) -> None:
        with self._lock:
            self.counters["pairs_total"] += 1
            st = doc.get("status")
            key = {"pass": "pass_total", "fail": "fail_total",
                   "undetermined": "undetermined_total"}.get(st)
            if key:
                self.counters[key] += 1
            if doc.get("escalate_manual"):
                self.counters["escalations_total"] += 1
            if doc.get("systemic_alert"):
                self.counters["systemic_alerts_total"] += 1
            if doc.get("resolution_mismatch"):
                self.counters["resolution_mismatch_total"] += 1
            if doc.get("mode") == "shadow" and doc.get("would_recapture"):
                self.counters["shadow_would_recapture_total"] += 1
            self._elapsed.append(float(doc.get("elapsed_ms") or 0.0))
            del self._elapsed[:-200]
            self.last = {
                "pair_id": doc.get("pair_id"),
                "status": st,
                "elapsed_ms": doc.get("elapsed_ms"),
                "at": doc.get("processed_at"),
            }

    def elapsed_stats(self) -> dict:
        with self._lock:
            vals = sorted(self._elapsed)
        if not vals:
            return {"count": 0}
        def pct(p):
            idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
            return round(vals[idx], 1)
        return {"count": len(vals), "p50_ms": pct(0.50), "p95_ms": pct(0.95),
                "max_ms": round(vals[-1], 1)}

    def snapshot(self) -> dict:
        with self._lock:
            return {"counters": dict(self.counters), "gauges": dict(self.gauges),
                    "last": dict(self.last)}

    def prometheus(self) -> str:
        snap = self.snapshot()
        lines = []
        for k, v in sorted(snap["counters"].items()):
            lines.append(f"# TYPE rootbox_qc_{k} counter")
            lines.append(f"rootbox_qc_{k} {v}")
        for k, v in sorted(snap["gauges"].items()):
            lines.append(f"# TYPE rootbox_qc_{k} gauge")
            lines.append(f"rootbox_qc_{k} {v}")
        st = self.elapsed_stats()
        if st.get("count"):
            lines.append("# TYPE rootbox_qc_elapsed_p95_ms gauge")
            lines.append(f"rootbox_qc_elapsed_p95_ms {st['p95_ms']}")
        return "\n".join(lines) + "\n"


# ============================================================================
# 发现候选对
# ============================================================================
def _images_in(dir_path: str) -> list[str]:
    out = []
    try:
        for e in sorted(os.scandir(dir_path), key=lambda x: x.name.lower()):
            if not e.is_file(follow_symlinks=False):
                continue
            if os.path.splitext(e.name)[1].lower() not in IMG_EXTS:
                continue
            try:
                if e.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            out.append(e.path)
    except OSError as e:
        logger.warning("读取目录失败 %s: %s", dir_path, e)
    return out


def _pick_two(images: list[str]) -> list[str] | None:
    """两张图直接返回；多于两张时优先认 1/2 命名，否则判为歧义（返回 None）。"""
    if len(images) == 2:
        return images
    names = {os.path.basename(p).lower(): p for p in images}
    for a in _PREFERRED_A:
        for b in _PREFERRED_B:
            if a in names and b in names:
                return [names[a], names[b]]
    return None


def discover(watch_dir: str, cfg: dict, skip_paths=()) -> list[Candidate]:
    """扫一遍监听目录，返回所有"看起来已成对"的候选（还没判断完整性/稳定性）。"""
    pairing = (cfg.get("pairing") or {})
    regex = None
    try:
        regex = re.compile(pairing.get("regex") or r"^(?P<prefix>.+)_(?P<idx>[0-9]+)\.(?P<ext>jpg|jpeg|png|bmp)$",
                           re.IGNORECASE)
    except re.error as e:
        logger.warning("PAIRING.regex 无效（%s），平铺配对不可用", e)

    skip_real = {os.path.normcase(os.path.abspath(p)) for p in skip_paths if p}
    cands: list[Candidate] = []
    flat: dict[str, dict[str, str]] = {}

    def skipped_dir(path: str) -> bool:
        name = os.path.basename(path)
        if name.startswith(".") or name in ("__pycache__",):
            return True
        if name.lower() in _SKIP_NAMES:
            return True
        return os.path.normcase(os.path.abspath(path)) in skip_real

    try:
        entries = sorted(os.scandir(watch_dir), key=lambda x: x.name.lower())
    except OSError as e:
        logger.error("扫描监听目录失败 %s: %s", watch_dir, e)
        return []

    for e in entries:
        if e.is_dir(follow_symlinks=False):
            if skipped_dir(e.path):
                continue
            imgs = _images_in(e.path)
            picked = _pick_two(imgs) if imgs else None
            if picked:
                pid = _safe_id(e.name)
                cands.append(Candidate(pid, picked[0], picked[1], _fingerprint(picked)))
                continue
            if len(imgs) == 0:
                # 双层：<watch>/<批次>/<编号>/1.png
                try:
                    subs = sorted(os.scandir(e.path), key=lambda x: x.name.lower())
                except OSError:
                    subs = []
                for sub in subs:
                    if not sub.is_dir(follow_symlinks=False) or skipped_dir(sub.path):
                        continue
                    imgs2 = _images_in(sub.path)
                    picked2 = _pick_two(imgs2) if imgs2 else None
                    if picked2:
                        pid = _safe_id(f"{e.name}/{sub.name}")
                        cands.append(Candidate(pid, picked2[0], picked2[1],
                                               _fingerprint(picked2),
                                               batch=e.name, number=sub.name))
            elif len(imgs) > 2:
                logger.warning("目录内图片数=%d 且认不出 1/2 命名，跳过：%s",
                               len(imgs), e.path)
            continue
        # 平铺文件
        if regex is None:
            continue
        m = regex.match(e.name)
        if not m:
            continue
        try:
            prefix, idx = m.group("prefix"), m.group("idx")
        except (IndexError, KeyError):
            continue
        flat.setdefault(prefix, {})[idx] = e.path

    for prefix, idxs in flat.items():
        if "1" in idxs and "2" in idxs:
            pair = [idxs["1"], idxs["2"]]
            cands.append(Candidate(_safe_id(prefix), pair[0], pair[1], _fingerprint(pair)))
        else:
            logger.debug("平铺前缀 %s 未成对（有 %s），跳过", prefix, sorted(idxs))
    return cands


# ============================================================================
# 健康检查 HTTP
# ============================================================================
class _HealthHandler(BaseHTTPRequestHandler):
    svc = None

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            body, code = self.svc.health(), 200 if self.svc.is_healthy() else 503
            self._send_json(body, code)
        elif path == "/metrics":
            self._send(self.svc.metrics.prometheus(), "text/plain; version=0.0.4")
        elif path == "/version":
            self._send_json({"service": "rootbox_qc.watch", "version": 1}, 200)
        else:
            self._send_json({"error": "not found", "paths": ["/health", "/metrics"]}, 404)

    def _send_json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False, indent=2),
                   "application/json; charset=utf-8", code)

    def _send(self, text, ctype, code=200):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, fmt, *args):  # 不要每 5 秒往 stderr 刷一行
        logger.debug("health: " + fmt, *args)


# ============================================================================
# 服务主体
# ============================================================================
class WatchService:
    def __init__(self, settings: Settings, cfg: dict | None = None):
        self.settings = settings
        self.cfg = dict(cfg or load_config())
        paths = dict(self.cfg.get("paths") or {})
        paths["output_dir"] = settings.output_dir
        paths["feedback_dir"] = settings.feedback_dir
        paths["recapture_queue"] = settings.recapture_queue
        self.cfg["paths"] = paths

        self.state = ServiceState(settings.state_file)
        self.tracker = RecaptureTracker(max_attempts=settings.max_attempts)
        self.metrics = Metrics()
        self._stop = threading.Event()
        self._seen: dict[str, tuple] = {}
        self._warned: set[str] = set()
        self._httpd = None
        self._http_thread = None
        self._lock_path = os.path.join(settings.output_dir, ".watch_service.lock")
        self._owned_lock = False
        self._started_at = time.time()
        self._heartbeat = self._started_at
        self._running = False
        self.degraded: list[str] = []

    # ---------------------------------------------------------------- 自检
    def validate(self) -> list[str]:
        """fail-closed 启动自检：返回全部问题（空列表才允许启动）。"""
        s = self.settings
        problems: list[str] = []
        self.degraded = []

        if not s.watch_dir:
            problems.append("未配置监听目录（--watch 或 ROOTBOX_QC_WATCH_DIR）")
        else:
            try:
                os.makedirs(s.watch_dir, exist_ok=True)
            except OSError as e:
                problems.append(f"监听目录不可用 {s.watch_dir}: {e}")
            if os.path.isdir(s.watch_dir):
                if os.path.normcase(os.path.abspath(s.watch_dir)) == \
                        os.path.normcase(os.path.abspath(s.output_dir)):
                    problems.append("监听目录不能等于输出目录（会把自己的输出当落图）")
            elif not any("监听目录" in p for p in problems):
                problems.append(f"监听路径不是目录: {s.watch_dir}")

        for label, path in (("输出目录", s.output_dir), ("反馈目录", s.feedback_dir),
                            ("日志目录", s.log_dir)):
            if not path:
                problems.append(f"{label}未配置")
                continue
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as e:
                problems.append(f"{label}不可用 {path}: {e}")
                continue
            probe = os.path.join(path, f".write_probe.{os.getpid()}")
            try:
                with open(probe, "w", encoding="utf-8") as f:
                    f.write("ok")
                os.remove(probe)
            except OSError as e:
                problems.append(f"{label}不可写 {path}: {e}")

        # 拼接：生产必须配齐，否则 verify 会静默降级成"只做单图+成对"
        paths = self.cfg.get("paths") or {}
        stitch_dir = paths.get("stitch_output_dir")
        stitch_script = paths.get("stitch_script")
        if s.require_stitch:
            if not stitch_dir:
                problems.append(
                    "未配置拼接输出目录（paths.stitch_output_dir / "
                    "ROOTBOX_QC_STITCH_DIR）：生产要求拼接后复检，"
                    "未配置会静默跳过该步骤")
            else:
                try:
                    os.makedirs(stitch_dir, exist_ok=True)
                except OSError as e:
                    problems.append(f"拼接输出目录不可用 {stitch_dir}: {e}")
            if not stitch_script or not os.path.isfile(stitch_script):
                problems.append(f"拼接脚本不存在: {stitch_script!r}"
                                f"（paths.stitch_script / ROOTBOX_QC_STITCH_SCRIPT）")
            else:
                try:
                    fn = _load_stitch_fn(stitch_script)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"拼接脚本无法加载 {stitch_script}: {e}")
                else:
                    if not callable(fn):
                        problems.append(
                            f"拼接脚本缺少 stitch_from_qc(img_a, img_b, out_path): "
                            f"{stitch_script}")
        else:
            self.degraded.append("stitch-disabled")
            logger.warning("require_stitch=False：只做单图+成对检查，"
                           "不执行拼接后复检（仅限调试/旁路评估）")

        # 阈值/画幅
        try:
            th = get_thresholds(self.cfg)
        except Exception as e:  # noqa: BLE001
            problems.append(f"阈值加载失败: {e}")
            th = {}
        ew = int(th.get("expected_width", 0) or 0)
        eh = int(th.get("expected_height", 0) or 0)
        if ew <= 0 or eh <= 0:
            problems.append(f"画幅配置无效 expected={ew}x{eh}")
        layout = th.get("layout")
        if layout is not None and layout not in MOSAIC_LAYOUTS:
            problems.append(f"阈值 layout={layout!r} 不在 {sorted(MOSAIC_LAYOUTS)} 内")
        cuts = th.get("blur_strip_cuts") or []
        if len(cuts) != 3 or not all(isinstance(c, int) and 0 < c < max(ew, 1)
                                     for c in cuts):
            problems.append(f"blur_strip_cuts 无效: {cuts!r}（应 0<c1<c2<c3<{ew}）")

        # OpenCV 可用性（真跑一次）
        try:
            import cv2
            import numpy as np
            tiny = np.zeros((16, 16), dtype=np.uint8)
            cv2.Laplacian(tiny, cv2.CV_64F)
        except Exception as e:  # noqa: BLE001
            problems.append(f"OpenCV/numpy 不可用: {e}")

        # 回调地址
        if s.callback_url:
            u = urllib.parse.urlparse(s.callback_url)
            if u.scheme not in ("http", "https") or not u.netloc:
                problems.append(f"回调地址不合法: {s.callback_url}")

        return problems

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        problems = self.validate()
        if problems:
            raise StartupError(problems)
        self._acquire_lock()
        try:
            if self.settings.health_port:
                self._start_http()
        except OSError as e:
            self._release_lock()
            raise StartupError([f"健康检查端口 {self.settings.health_port} 启动失败: {e}"])
        self.state.load()
        self.state.restore_tracker(self.tracker)
        self._running = True
        self._heartbeat = time.time()
        s = self.settings
        logger.info("监听服务启动 dir=%s mode=%s settle=%.1fs poll=%.1fs "
                    "stitch=%s health=%s callback=%s",
                    s.watch_dir, "shadow" if s.shadow else "live",
                    s.settle_seconds, s.poll_seconds,
                    (self.cfg.get("paths") or {}).get("stitch_output_dir"),
                    f"{s.health_host}:{s.health_port}" if s.health_port else "off",
                    s.callback_url or "off")

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None
        try:
            self.state.save()
        except OSError as e:
            logger.warning("退出时保存状态失败: %s", e)
        self._release_lock()
        logger.info("监听服务已停止（处理 %d 对）", self.metrics.counters.get("pairs_total", 0))

    def _acquire_lock(self) -> None:
        os.makedirs(os.path.dirname(self._lock_path) or ".", exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"{os.getpid()}\n{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
                self._owned_lock = True
                return
            except FileExistsError:
                pid = 0
                try:
                    with open(self._lock_path, "r", encoding="utf-8") as f:
                        pid = int((f.readline() or "0").strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and pid != os.getpid() and _pid_alive(pid):
                    raise StartupError([
                        f"另一个实例正在运行（pid={pid}，锁 {self._lock_path}）；"
                        f"同一输出目录只允许一个监听进程"])
                logger.warning("发现残留锁文件（pid=%s 已不存在），接管", pid)
                try:
                    os.remove(self._lock_path)
                except OSError as e:
                    raise StartupError([f"无法清理残留锁 {self._lock_path}: {e}"])

    def _release_lock(self) -> None:
        if not self._owned_lock:
            return
        try:
            os.remove(self._lock_path)
        except OSError:
            pass
        self._owned_lock = False

    def _start_http(self) -> None:
        handler = type("HealthHandler", (_HealthHandler,), {"svc": self})
        self._httpd = ThreadingHTTPServer(
            (self.settings.health_host, int(self.settings.health_port)), handler)
        self._httpd.daemon_threads = True
        self._http_thread = threading.Thread(target=self._httpd.serve_forever,
                                             name="qc-health", daemon=True)
        self._http_thread.start()

    # ---------------------------------------------------------------- 健康
    def is_healthy(self) -> bool:
        if not self._running:
            return False
        return (time.time() - self._heartbeat) < max(60.0, self.settings.poll_seconds * 30)

    def health(self) -> dict:
        snap = self.metrics.snapshot()
        return {
            "status": "ok" if self.is_healthy() else "stale",
            "service": "rootbox_qc.watch",
            "pid": os.getpid(),
            "uptime_s": round(time.time() - self._started_at, 1),
            "mode": "shadow" if self.settings.shadow else "live",
            "degraded": list(self.degraded),
            "watch_dir": self.settings.watch_dir,
            "output_dir": self.settings.output_dir,
            "feedback_dir": self.settings.feedback_dir,
            "last_heartbeat_age_s": round(time.time() - self._heartbeat, 1),
            "counters": snap["counters"],
            "gauges": snap["gauges"],
            "last": snap["last"],
            "elapsed": self.metrics.elapsed_stats(),
        }

    # ---------------------------------------------------------------- 主循环
    def run_forever(self) -> int:
        self.start()
        try:
            while not self._stop.is_set():
                self.tick()
                if self.settings.once:
                    break
                self._stop.wait(self.settings.poll_seconds)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，准备退出")
        finally:
            self.stop()
        return 0

    def tick(self) -> None:
        """扫一次目录，处理所有"稳定且未处理"的候选对。"""
        self._heartbeat = time.time()
        s = self.settings
        cands = discover(s.watch_dir, self.cfg,
                         skip_paths=(s.output_dir, s.feedback_dir, s.log_dir,
                                     (self.cfg.get("paths") or {}).get("stitch_output_dir")))
        now = time.time()
        ready = []
        for c in cands:
            if self._should_process(c, now):
                ready.append(c)
        self.metrics.set_gauge("pending_pairs", len(cands))
        if s.dry_run:
            for c in cands:
                logger.info("[dry-run] 候选 pair_id=%s a=%s b=%s", c.pair_id,
                            c.path_a, c.path_b)
            return
        for c in ready:
            if self._stop.is_set():
                break
            self.process(c)
        self._heartbeat = time.time()

    def _should_process(self, c: Candidate, now: float) -> bool:
        prev_fp = self.state.processed_fingerprint(c.pair_id)
        if prev_fp is not None and prev_fp == c.fingerprint:
            return False
        if not (_header_ok(c.path_a) and _header_ok(c.path_b)):
            if c.pair_id not in self._warned:
                logger.info("%s 文件头未就绪（可能还在写），等待", c.pair_id)
                self._warned.add(c.pair_id)
            return False
        seen = self._seen.get(c.pair_id)
        if seen is None or seen[0] != c.fingerprint:
            self._seen[c.pair_id] = (c.fingerprint, now)
            # --once 用于人工补跑已落好的目录，不必再等稳定期
            return bool(self.settings.once)
        return (now - seen[1]) >= self.settings.settle_seconds

    # ---------------------------------------------------------------- 处理
    def _stitch_target(self, c: Candidate, paths: dict) -> tuple[str | None, str | None]:
        """成品落点：<stitch>/<批次>/<编号>.png，与产线 save/data1030/0001.png 对齐。

        平铺或单层落图没有批次信息，保持 <stitch>/<pair_id>.png。
        """
        base = paths.get("stitch_output_dir")
        if not base or not c.batch or not c.number:
            return base, None
        return os.path.join(base, c.batch), c.number

    def process(self, c: Candidate) -> None:
        s = self.settings
        paths = self.cfg.get("paths") or {}
        attempt = max(1, self.tracker.next_attempt(c.pair_id))
        t0 = time.perf_counter()
        stitch_dir, stitch_name = self._stitch_target(c, paths)
        logger.info("开始质检 %s attempt=%d a=%s b=%s", c.pair_id, attempt,
                    c.path_a, c.path_b)
        try:
            report = verify_pair(
                c.path_a, c.path_b, cfg=self.cfg, pair_id=c.pair_id, attempt=attempt,
                stitch_output_dir=stitch_dir,
                verify_stitched=bool(s.require_stitch),
                stitch_name=stitch_name,
            )
            self.tracker.observe(report)
        except Exception as e:  # noqa: BLE001 —— 单对异常绝不能带崩服务
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.metrics.inc("errors_total")
            retries = self.state.error_retries(c.pair_id) + 1
            logger.exception("质检异常 %s（第 %d/%d 次）: %s",
                             c.pair_id, retries, _ERROR_RETRY_LIMIT, e)
            give_up = retries >= _ERROR_RETRY_LIMIT
            self.state.record(c.pair_id, c.fingerprint, attempt, "error",
                              elapsed_ms, error=not give_up)
            if give_up:
                doc = self._error_doc(c, attempt, elapsed_ms, e, given_up=True)
                self._write_feedback(doc)
                self.metrics.observe(doc)
                logger.error("%s 连续异常 %d 次，放弃自动处理，转人工", c.pair_id, retries)
            self.state.dump_tracker(self.tracker)
            self.state.save()
            self._seen.pop(c.pair_id, None)
            return

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        doc = self._feedback_doc(c, report, elapsed_ms)
        self._write_feedback(doc)
        self._notify(report, doc)
        self.state.record(c.pair_id, c.fingerprint, attempt, report.status, elapsed_ms)
        self.state.dump_tracker(self.tracker)
        self.state.save()
        self.metrics.observe(doc)
        self._seen.pop(c.pair_id, None)
        _retake_label = "would_recapture" if s.shadow else "recapture"
        _retake_value = (doc.get("would_recapture") if s.shadow
                         else report.recapture_needed)
        logger.info("完成 %s status=%s %s=%s stitched=%s %.0fms",
                    c.pair_id, report.status, _retake_label, _retake_value,
                    report.stitched_status, elapsed_ms)
        if report.escalate_manual:
            logger.error("%s 已达 max_attempts=%d 仍不合格，转人工：%s",
                         c.pair_id, report.max_attempts, "; ".join(report.recapture_reasons))
        if report.systemic_alert:
            logger.error("系统性告警 %s: %s", c.pair_id, report.systemic_alert)

    def _feedback_doc(self, c: Candidate, report, elapsed_ms: float) -> dict:
        s = self.settings
        d = report.to_dict()
        cats = sorted({f["category"] for f in failed_checks(report)})
        resolution_mismatch = any(
            (chk.get("name") == "resolution") for r in report.images
            for chk in (r.checks or []))
        doc = {
            "pair_id": report.pair_id,
            "mode": "shadow" if s.shadow else "live",
            "status": report.status,
            "passed": report.status == "pass",
            "recapture_needed": bool(report.recapture_needed) and not s.shadow,
            "targets": list(report.recapture_targets) if not s.shadow else [],
            "reasons": list(report.recapture_reasons) if not s.shadow else [],
            "attempt": report.attempt,
            "max_attempts": report.max_attempts,
            "escalate_manual": bool(report.escalate_manual),
            "systemic_alert": report.systemic_alert,
            "failure_categories": cats,
            "stitched_status": report.stitched_status,
            "stitch_error": report.stitch_error,
            "resolution_mismatch": resolution_mismatch,
            "images": {"a": c.path_a, "b": c.path_b},
            "batch": c.batch,
            "number": c.number,
            "elapsed_ms": round(elapsed_ms, 1),
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "report_file": f"{report.pair_id}.json",
            "degraded": list(self.degraded),
        }
        if s.shadow:
            doc["would_recapture"] = bool(report.recapture_needed)
            doc["would_targets"] = list(report.recapture_targets)
            doc["would_reasons"] = list(report.recapture_reasons)
        # 给 FileNotifier 用的完整档（含 shadow 标记，避免下游误读 recapture_needed）
        d["mode"] = doc["mode"]
        d["enforced"] = not s.shadow
        d["elapsed_ms"] = doc["elapsed_ms"]
        if s.shadow:
            d["would_recapture"] = bool(report.recapture_needed)
            d["would_reasons"] = list(report.recapture_reasons)
            d["would_targets"] = list(report.recapture_targets)
            d["recapture_needed"] = False
            d["reasons"] = []
            d["targets"] = []
        doc["_report"] = d
        return doc

    def _error_doc(self, c: Candidate, attempt: int, elapsed_ms: float,
                   error: Exception, given_up: bool = False) -> dict:
        return {
            "pair_id": c.pair_id,
            "mode": "shadow" if self.settings.shadow else "live",
            "status": "error",
            "passed": False,
            "recapture_needed": False,
            "targets": [],
            "reasons": [f"质检异常: {error}"],
            "attempt": attempt,
            "max_attempts": self.tracker.max_attempts,
            "escalate_manual": bool(given_up),
            "systemic_alert": None,
            "failure_categories": ["error"],
            "stitched_status": None,
            "stitch_error": None,
            "resolution_mismatch": False,
            "images": {"a": c.path_a, "b": c.path_b},
            "batch": c.batch,
            "number": c.number,
            "elapsed_ms": round(elapsed_ms, 1),
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "report_file": None,
            "degraded": list(self.degraded),
            "error": str(error),
        }

    def _write_feedback(self, doc: dict) -> None:
        s = self.settings
        public = {k: v for k, v in doc.items() if not k.startswith("_")}
        try:
            os.makedirs(s.feedback_dir, exist_ok=True)
            _atomic_json(os.path.join(s.feedback_dir, f"{doc['pair_id']}.json"), public)
            # 镜像一份到 feedback/<批次>/<编号>.json，跟落图目录结构对得上
            if doc.get("batch") and doc.get("number"):
                mirror = os.path.join(s.feedback_dir, doc["batch"],
                                      f"{doc['number']}.json")
                os.makedirs(os.path.dirname(mirror), exist_ok=True)
                _atomic_json(mirror, public)
        except OSError as e:
            logger.error("写反馈文件失败 %s: %s", doc["pair_id"], e)
            self.metrics.inc("errors_total")
        try:
            _append_jsonl(os.path.join(s.feedback_dir, "feedback.jsonl"), public)
        except OSError as e:
            logger.error("写 feedback.jsonl 失败: %s", e)

    def _notify(self, report, doc: dict) -> None:
        s = self.settings
        full = doc.get("_report")
        queue = s.shadow_queue if s.shadow else s.recapture_queue
        notifier = FileNotifier(s.output_dir, queue)
        try:
            notifier.notify(full if full is not None else report.to_dict())
        except Exception as e:  # noqa: BLE001
            logger.error("留档/队列写入失败 %s: %s", doc["pair_id"], e)
            self.metrics.inc("notify_fail_total")

        if s.shadow and doc.get("would_recapture"):
            # shadow 下 FileNotifier 看到 recapture_needed=False 不会入队，这里手工补记录
            try:
                _append_jsonl(s.shadow_queue, {
                    "pair_id": doc["pair_id"], "attempt": doc["attempt"],
                    "reasons": doc["would_reasons"], "targets": doc["would_targets"],
                    "timestamp": doc["processed_at"], "mode": "shadow",
                })
            except OSError as e:
                logger.warning("写 shadow 队列失败: %s", e)

        if s.callback_url and not s.shadow and full is not None:
            h = HttpNotifier(s.callback_url, notify_on_pass=True)
            try:
                h.notify(full)
            except Exception as e:  # noqa: BLE001
                logger.error("回调通知异常 %s: %s", doc["pair_id"], e)
                self.metrics.inc("notify_fail_total")


def _load_stitch_fn(script: str):
    """加载拼接脚本并取 stitch_from_qc（走 verify 的模块缓存，启动时不重复执行脚本）。"""
    try:
        from .verify import _load_stitch_module
        mod = _load_stitch_module(script)
    except (ImportError, AttributeError):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rootbox_stitch_impl", script)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {script} 创建模块 spec")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return getattr(mod, "stitch_from_qc", None)


# ============================================================================
# 日志
# ============================================================================
def setup_service_logging(log_dir: str | None, level: int = logging.INFO) -> None:
    root = logging.getLogger("rootbox_qc")
    if getattr(root, "_watch_configured", False):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "watch_service.log"),
                maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            logger.warning("创建日志文件失败 %s: %s", log_dir, e)
    root._watch_configured = True  # type: ignore[attr-defined]


# ============================================================================
# CLI
# ============================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_service.py",
        description="根盒质检 · 目录监听服务（落图 → verify_pair → 反馈）")
    p.add_argument("--watch", dest="watch_dir",
                   help="监听目录（小车落图的地方）；也可用 ROOTBOX_QC_WATCH_DIR")
    p.add_argument("--out", dest="output_dir", help="报告/队列/状态输出目录")
    p.add_argument("--feedback", dest="feedback_dir", help="精简反馈目录（小车读这里）")
    p.add_argument("--log-dir", dest="log_dir", help="日志目录（轮转）")
    p.add_argument("--shadow", action="store_true",
                   help="旁路模式：只记录 would_recapture，绝不驱动重拍")
    p.add_argument("--settle", dest="settle_seconds", type=float,
                   help="文件稳定多少秒后才处理（默认 2）")
    p.add_argument("--poll", dest="poll_seconds", type=float, help="扫描间隔秒（默认 1）")
    p.add_argument("--health-host", default=None, help="健康检查绑定地址（默认 127.0.0.1）")
    p.add_argument("--health-port", type=int, default=None,
                   help="健康检查端口；0=关闭（默认），建议 8090")
    p.add_argument("--callback", dest="callback_url", help="结果 HTTP 回调地址（可选）")
    p.add_argument("--stitch-dir", dest="stitch_dir",
                   help="拼接成品输出目录（生产必配）；也可用 ROOTBOX_QC_STITCH_DIR")
    p.add_argument("--stitch-script", dest="stitch_script",
                   help="拼接脚本路径；也可用 ROOTBOX_QC_STITCH_SCRIPT")
    p.add_argument("--max-attempts", type=int, default=None, help="超过则转人工（默认取配置 3）")
    p.add_argument("--no-require-stitch", dest="require_stitch", action="store_false",
                   default=None, help="调试用：允许未配拼接目录启动（生产勿用）")
    p.add_argument("--once", action="store_true", help="扫一轮就退出（自检/CI）")
    p.add_argument("--dry-run", action="store_true", help="只列候选，不质检")
    p.add_argument("--print-config", action="store_true", help="打印生效配置后退出")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config()
    overrides = {k: getattr(args, k) for k in
                 ("watch_dir", "output_dir", "feedback_dir", "log_dir", "shadow",
                  "settle_seconds", "poll_seconds", "health_host", "health_port",
                  "callback_url", "require_stitch", "max_attempts", "once", "dry_run")
                 if getattr(args, k, None) not in (None, False)}
    if getattr(args, "shadow", False):
        overrides["shadow"] = True
    if getattr(args, "once", False):
        overrides["once"] = True
    if getattr(args, "dry_run", False):
        overrides["dry_run"] = True
    if getattr(args, "require_stitch", None) is False:
        overrides["require_stitch"] = False
    # 拼接目录/脚本走配置树，命令行优先于环境变量（Settings 读的是 paths.*）
    if getattr(args, "stitch_dir", None):
        cfg.setdefault("paths", {})["stitch_output_dir"] = args.stitch_dir
    if getattr(args, "stitch_script", None):
        cfg.setdefault("paths", {})["stitch_script"] = args.stitch_script

    settings = Settings.from_env(cfg, **overrides)
    if not settings.watch_dir:
        build_parser().print_usage(sys.stderr)
        print("错误：必须指定 --watch 或设置 ROOTBOX_QC_WATCH_DIR", file=sys.stderr)
        return 2

    setup_service_logging(settings.log_dir)
    if args.print_config:
        print(json.dumps(settings.__dict__, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in (cfg.get("paths") or {}).items()},
                         ensure_ascii=False, indent=2))
        return 0

    svc = WatchService(settings, cfg)

    def _on_signal(signum, frame):  # noqa: ARG001
        logger.info("收到信号 %s，准备退出", signum)
        svc._stop.set()

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass  # 非主线程/不支持时忽略

    try:
        return svc.run_forever()
    except StartupError as e:
        logger.error("%s", e)
        print(str(e), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
