"""Report subscriptions — deliver a scheduled run's report to its subscribers.

「定时分析 + 订阅」的订阅半边：一次运行产出一份**报告记录**（NL 路径由
runner 把答案/主因/行数写进 ``run.result_json``，决策路径写判定摘要），本
模块把这份报告投递给该任务的每个订阅者（webhook / console），并把每次投递
落成 ``deliveries`` 行。

设计纪律（与 alerting / verdict / action 同款）：
- **best-effort**：投递失败只落一条 failed 记录 + 日志，绝不把一次跑得好
  好的运行变成 error（runner 侧还有一层兜底）。
- **幂等**：``deliveries`` 表 ``(subscription_id, run_id)`` 唯一 —— 同一
  run 对同一订阅者最多一条投递记录；真正的重发是新的 run。
- **确定性**：谁该收到（mode 闸门 / enabled）、走什么通道（订阅通道 →
  任务通道 → console）都是纯判定，零 LLM。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from trove.core.logging import get_logger
from trove.services.jobs.notify import build_notifier
from trove.services.jobs.store import Delivery, Job, JobStore, Subscription

logger = get_logger(__name__)

MODES = ("always", "alert_only")
MAX_EXCERPT = 512


class SubscriptionService:
    def __init__(self, store: JobStore):
        self.store = store

    # ── subscription lifecycle ───────────────────────────

    async def create(
        self, job_id: str, subscriber: str, *, channel: str = "",
        mode: str = "always", created_by: str = "",
    ) -> Subscription | None:
        subscriber = (subscriber or "").strip()
        if not subscriber or mode not in MODES:
            return None
        sub = Subscription(
            job_id=job_id, subscriber=subscriber,
            channel=(channel or "").strip(), mode=mode, created_by=created_by,
        )
        await self.store.save_subscription(sub)
        return sub

    async def find(self, job_id: str, subscriber: str) -> Subscription | None:
        """The (job, subscriber) pair's subscription; None when absent."""
        for sub in await self.store.load_subscriptions():
            if sub.job_id == job_id and sub.subscriber == subscriber:
                return sub
        return None

    async def list_subs(
        self, *, job_id: str | None = None, subscriber: str | None = None,
    ) -> list[Subscription]:
        subs = await self.store.load_subscriptions()
        if job_id:
            subs = [s for s in subs if s.job_id == job_id]
        if subscriber:
            subs = [s for s in subs if s.subscriber == subscriber]
        return subs

    async def get(self, sub_id: str) -> Subscription | None:
        return await self.store.get_subscription(sub_id)

    async def update(
        self, sub_id: str, *, channel: str | None = None,
        mode: str | None = None, enabled: bool | None = None,
    ) -> Subscription | None:
        """Update mutable fields (None = unchanged); None on unknown id/bad mode."""
        sub = await self.store.get_subscription(sub_id)
        if sub is None:
            return None
        if mode is not None:
            if mode not in MODES:
                return None
            sub.mode = mode
        if channel is not None:
            sub.channel = channel.strip()
        if enabled is not None:
            sub.enabled = bool(enabled)
        await self.store.save_subscription(sub)
        return sub

    async def delete(self, sub_id: str) -> bool:
        return await self.store.delete_subscription(sub_id)

    async def list_deliveries(
        self, *, subscription_id: str | None = None, job_id: str | None = None,
        subscriber: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self.store.list_deliveries(
            subscription_id=subscription_id, job_id=job_id,
            subscriber=subscriber, limit=limit,
        )

    # ── delivery ─────────────────────────────────────────

    async def deliver_for_run(
        self, job: Job, *, run_id: int, status: str, verdict: str,
        alert_triggered: bool, alert_message: str,
        report: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Deliver one run's report to every enabled subscriber of ``job``.

        Returns one result dict per attempted delivery. A channel failure is
        recorded as ``failed`` and never raises; a caller-side bug would —
        the runner's wrapper is the last line of defence for that.
        """
        subs = [
            s for s in await self.store.load_subscriptions()
            if s.job_id == job.id and s.enabled
        ]
        results: list[dict[str, Any]] = []
        for sub in subs:
            if sub.mode == "alert_only" and not alert_triggered:
                continue
            channel = sub.channel or job.alert_channel or "console"
            payload = self._payload(
                job, sub, run_id=run_id, status=status, verdict=verdict,
                alert_message=alert_message, report=report or {},
            )
            excerpt = str(payload.get("message") or "")[:MAX_EXCERPT]
            error = ""
            delivery_status = "sent"
            notifier = build_notifier(channel)
            if notifier is None:
                # 通道写坏（任务/订阅上残留了非法串）也要留痕：静默跳过会让
                # 「订了却从没收到」看起来像订阅没生效，而不是一条坏配置。
                delivery_status = "failed"
                error = f"unsupported channel: {channel}"
            else:
                try:
                    ok = await notifier.send(payload)
                    if not ok:
                        delivery_status = "failed"
                        error = "channel reported failure"
                except Exception as e:  # 通道实现自有兜底；这是最后一道
                    delivery_status = "failed"
                    error = str(e)[:200]
            await self.store.add_delivery(Delivery(
                subscription_id=sub.id, job_id=job.id, run_id=run_id,
                subscriber=sub.subscriber, channel=channel,
                status=delivery_status, error=error, excerpt=excerpt,
            ))
            results.append({
                "subscription_id": sub.id, "subscriber": sub.subscriber,
                "channel": channel, "status": delivery_status, "error": error,
            })
        return results

    def _payload(
        self, job: Job, sub: Subscription, *, run_id: int, status: str,
        verdict: str, alert_message: str, report: dict[str, Any],
    ) -> dict[str, Any]:
        """The delivered report body (webhook JSON / console line source).

        ``message`` 是一行人类可读摘要：有告警用告警文本，否则用答案开头
        （无答案的运行退到错误文本）；有主因行就缀上 —— console notifier
        直接打印它。``report`` 是完整报告记录（有界），webhook 收件方拿得到。
        """
        answer = str(report.get("answer") or "")
        driver = str(report.get("driver") or "")
        message = (
            alert_message or answer[:160]
            # 决策路径没有 answer，report["message"]（规则消息+主因）即正文。
            or str(report.get("message") or "")[:160]
            or str(report.get("error") or "")
        )
        if not alert_message and driver:
            message = f"{message}\n主因：{driver}" if message else f"主因：{driver}"
        return {
            "kind": "REPORT",
            "type": "trove.report",
            "job_id": job.id,
            "job_name": job.name,
            "question": job.question,
            "datasource": job.datasource,
            "run_id": run_id,
            "status": status,
            "verdict": verdict,
            "alert": alert_message,
            "subscriber": sub.subscriber,
            "delivered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "message": message,
            "report": report,
        }
