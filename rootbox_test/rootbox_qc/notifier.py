"""通知适配器：质检结论对外出口（重拍触发通道）。

- FileNotifier   ：留档（JSON 报告 + 重拍队列 jsonl，自动轮转）。默认，生产保留。
- HttpNotifier   ：POST 到小车 REST 接口。标准库 urllib 实现，无额外依赖；失败只记日志，
                   绝不让通知异常中断质检主流程（生产原则：通知是旁路）。
- ConsoleNotifier：只打印。
- MultiNotifier  ：同时多种（如 file 留档 + http 触发小车）。

队列约定（recapture_queue.jsonl）每行一条：
    {"pair_id": "...", "reasons": [...], "targets": [...], "attempt": 1,
     "timestamp": "..."}
拍照控制端读取队列（或直接收 HTTP）后执行重拍，同 pair 的 attempt+1。

重拍后报告：FileNotifier 在 attempt>1 或已转人工时另写
<pair_id>_retry.json（report["retry"] 的汇总 + final_report 文件名）。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

logger = logging.getLogger("rootbox_qc")

_QUEUE_ROTATE_BYTES = 5 * 1024 * 1024  # 队列 >5MB 时轮转


class Notifier(ABC):
    @abstractmethod
    def notify(self, report: dict) -> None: ...


class FileNotifier(Notifier):
    """每对一份 JSON 报告 + 需重拍追加到队列文件（超限自动轮转 .1）。

    attempt>1 或已转人工时另写 <pair_id>_retry.json：首末状态、重拍次数、
    每次原因/故障类别；final_report 指向该对最终报告文件。
    """

    def __init__(self, output_dir: str, recapture_queue: str):
        self.output_dir = output_dir
        self.recapture_queue = recapture_queue
        os.makedirs(output_dir, exist_ok=True)
        qdir = os.path.dirname(recapture_queue)
        if qdir:
            os.makedirs(qdir, exist_ok=True)

    def notify(self, report: dict) -> None:
        pair_id = report.get("pair_id", "unknown")
        attempt = int(report.get("attempt", 1) or 1)
        try:
            report_path = os.path.join(self.output_dir, f"{pair_id}.json")
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            # 重拍尝试（attempt > 1）额外保留历史快照，避免覆盖首拍失败诊断信息
            if attempt > 1:
                hist_path = os.path.join(self.output_dir, f"{pair_id}_attempt{attempt}.json")
                with open(hist_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("写报告失败 %s: %s", pair_id, e)
            return
        # 重拍后报告（跨尝试汇总）：只汇总，不复制完整 checks。
        # 单独兜异常：汇总文件写失败不能挡住后面的重拍队列。
        retry = report.get("retry") or {}
        if retry and (attempt > 1 or report.get("escalate_manual")):
            try:
                retry_doc = dict(retry)
                retry_doc["final_report"] = f"{pair_id}.json"
                retry_doc["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                retry_path = os.path.join(self.output_dir, f"{pair_id}_retry.json")
                with open(retry_path, "w", encoding="utf-8") as f:
                    json.dump(retry_doc, f, ensure_ascii=False, indent=2)
            except OSError as e:
                logger.warning("写重拍汇总失败 %s: %s", pair_id, e)
        if report.get("recapture_needed"):
            item = {k: report.get(k) for k in
                    ("pair_id", "reasons", "targets", "attempt", "max_attempts",
                     "escalate_manual", "systemic_alert", "timestamp")}
            self._append(item)

    def _append(self, item: dict) -> None:
        try:
            if (os.path.isfile(self.recapture_queue)
                    and os.path.getsize(self.recapture_queue) > _QUEUE_ROTATE_BYTES):
                try:
                    os.replace(self.recapture_queue, self.recapture_queue + ".1")
                except OSError as e:
                    logger.warning("重拍队列轮转失败: %s", e)
            with open(self.recapture_queue, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("写重拍队列失败: %s", e)


class ConsoleNotifier(Notifier):
    def notify(self, report: dict) -> None:
        pass  # 由 runner 自己负责终端打印


class HttpNotifier(Notifier):
    """POST 报告到小车 REST 接口（标准库实现；失败重试 N 次后仅记日志）。"""

    def __init__(self, url: str, timeout: float = 5.0, retries: int = 2,
                 notify_on_pass: bool = False):
        self.url = url
        self.timeout = timeout
        self.retries = retries
        self.notify_on_pass = bool(notify_on_pass)

    def notify(self, report: dict) -> None:
        if not report.get("recapture_needed") and not self.notify_on_pass:
            return
        body = json.dumps(report, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        last_err = None
        for i in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if 200 <= resp.status < 300:
                        return
                    last_err = f"HTTP {resp.status}"
                    if 400 <= resp.status < 500:
                        break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                # 4xx 客户端错误重试也不会成功，立即结束；5xx 服务端错误照常重试
                if 400 <= e.code < 500:
                    break
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_err = e
            if i < self.retries:
                time.sleep(0.5 * (i + 1))
        logger.error("请求重拍接口失败 %s: %s", self.url, last_err)


class MultiNotifier(Notifier):
    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers

    def notify(self, report: dict) -> None:
        for n in self.notifiers:
            try:
                n.notify(report)
            except Exception as e:  # noqa: BLE001
                logger.error("通知器 %s 异常: %s", type(n).__name__, e)


def build_notifier(cfg: dict, paths: dict) -> Notifier:
    """按 NOTIFIER.type 构造：file | console | http | multi(列表)。

    示例（config.py）：
        NOTIFIER = {"type": "multi", "targets": [
            {"type": "file"},
            {"type": "http", "url": "http://robot:8080/qc/recapture"},
        ]}
    """
    ncfg = cfg.get("notifier", {}) or {}
    ntype = ncfg.get("type", "file")

    if ntype == "file":
        return FileNotifier(paths["output_dir"], paths["recapture_queue"])
    if ntype == "console":
        return ConsoleNotifier()
    if ntype == "http":
        h = ncfg.get("http") or {}
        return HttpNotifier(
            h.get("url") or ncfg.get("url", ""),
            h.get("timeout", ncfg.get("timeout", 5.0)),
            h.get("retries", ncfg.get("retries", 2)),
            notify_on_pass=h.get("notify_on_pass", ncfg.get("notify_on_pass", False)),
        )
    if ntype == "multi":
        notifiers = []
        for t in ncfg.get("targets", []):
            tcfg = dict(t)
            ttype = tcfg.pop("type", "file")
            sub_cfg = {"notifier": {"type": ttype, **tcfg}}
            notifiers.append(build_notifier(sub_cfg, paths))
        return MultiNotifier(notifiers)
    raise ValueError(f"未知 notifier 类型: {ntype}")
