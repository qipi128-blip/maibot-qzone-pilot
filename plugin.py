"""QQ 空间动态管家插件。

两组后台任务：
1. 发说说：按间隔收集最近聊天、历史说说和好友动态灵感，让模型决定“现在要不要发、
   发什么”，确认后通过 QQ 空间 Web 接口发布。
2. 好友动态：定时读取好友的说说列表，让模型决定是否评论、评论什么；默认只读，
   需要显式开启 `browse.comment_enabled` 才会真的发评论。

Cookie 与 g_tk 通过 NapCat 的 ``get_cookies`` 动作获取，不依赖扫码登录。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maibot_sdk import Command, MaiBotPlugin, Tool

from .config import QZonePilotConfig
from .decision import (
    CommentDecision,
    PublishDecision,
    build_comment_messages,
    build_publish_messages,
    is_too_similar,
    parse_comment_decision,
    parse_publish_decision,
)
from .napcat import NapCatClient, NapCatEndpoint, NapCatError, discover_endpoint, ensure_action_ok
from .qzone import Feed, QZoneAuth, QZoneClient, QZoneError

_STATE_VERSION = 1
_AUTH_ERROR_CODES = {-3000, -10000, -100, -1000}
_AD_KEYWORDS = (
    "广告",
    "推广",
    "代购",
    "招代理",
    "兼职",
    "刷单",
    "返利",
    "加微信",
    "扫码",
    "点击链接",
    "抽奖",
    "领红包",
)
_GENERIC_COMMENTS = {
    "好看",
    "不错",
    "哈哈哈",
    "支持",
    "厉害了",
    "真棒",
    "羡慕",
    "加油",
    "可以",
    "牛",
    "6",
}
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_MAX_HISTORY = 60
_SEEN_RETENTION_SECONDS = 7 * 24 * 3600


class QZonePilotPlugin(MaiBotPlugin):
    """QQ 空间动态管家。"""

    config_model = QZonePilotConfig

    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        qzone_urls: dict[str, str] | None = None,
    ) -> None:
        """初始化插件。

        Args:
            state_path: 可选的状态文件路径，主要用于测试。
            qzone_urls: 可选的 QQ 空间接口地址覆盖，主要用于测试。
        """

        super().__init__()
        self._state_path: Path | None = Path(state_path) if state_path is not None else None
        self._qzone_urls = dict(qzone_urls or {})
        self._state: dict[str, Any] = {}
        self._client: NapCatClient | None = None
        self._endpoint: NapCatEndpoint | None = None
        self._auth: QZoneAuth | None = None
        self._auth_at = 0.0
        self._inspiration_feeds: list[Feed] = []
        self._browse_cursor = 0
        self._publish_task: asyncio.Task[None] | None = None
        self._browse_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopped = False
        self._last_error_notify_at = 0.0
        self._last_publish_attempt_at = 0.0

    # ---------------------------------------------------------------- 生命周期

    async def on_load(self) -> None:
        """加载状态并启动后台任务。"""

        self._ensure_state_path()
        self._load_state()
        self._endpoint = self._resolve_endpoint()

        if not self.config.plugin.enabled:
            self.ctx.logger.info("QQ 空间动态管家已加载，但插件当前未启用")
            return

        if self._endpoint is None:
            self.ctx.logger.warning(
                "QQ 空间动态管家未找到 NapCat 连接信息：请在插件配置中填写 ws_url/access_token，"
                "或确保 Napcat 适配器已安装并生成 config.toml"
            )
            return

        self._start_client()
        self._start_schedulers()
        self.ctx.logger.info("QQ 空间动态管家已加载，正在连接 NapCat: %s", self._endpoint.safe_url)

    async def on_unload(self) -> None:
        """停止任务、连接并保存状态。"""

        self._stopped = True
        for task in (self._publish_task, self._browse_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._publish_task, self._browse_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._publish_task = None
        self._browse_task = None

        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        await self._stop_client()
        self._save_state()
        self.ctx.logger.info("QQ 空间动态管家已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """处理配置热更新。"""

        del config_data
        if scope != "self":
            return
        self._save_state()

        old_endpoint = self._endpoint
        self._endpoint = self._resolve_endpoint()

        if not self.config.plugin.enabled:
            await self._stop_schedulers()
            await self._stop_client()
            self.ctx.logger.info("QQ 空间动态管家配置已更新：插件已停用（version=%s）", version)
            return

        if self._endpoint is None:
            await self._stop_schedulers()
            await self._stop_client()
            self.ctx.logger.warning("QQ 空间动态管家配置已更新，但未找到有效的 NapCat 连接信息")
            return

        if old_endpoint != self._endpoint or self._client is None:
            await self._stop_client()
            self._start_client()

        await self._stop_schedulers()
        self._start_schedulers()
        self.ctx.logger.info("QQ 空间动态管家配置已更新（version=%s）", version)

    def _ensure_state_path(self) -> None:
        """初始化状态文件路径。"""

        if self._state_path is not None:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            return
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "state.json"

    def _resolve_endpoint(self) -> NapCatEndpoint | None:
        """解析配置或自动发现 NapCat 连接。"""

        ws_url = str(self.config.connection.ws_url or "").strip()
        token = str(self.config.connection.access_token or "").strip()
        if ws_url:
            if not ws_url.startswith(("ws://", "wss://")):
                ws_url = f"ws://{ws_url}"
            return NapCatEndpoint(ws_url=ws_url, access_token=token)
        if not self.config.connection.auto_discover:
            return None
        try:
            mai_root = Path(__file__).resolve().parents[2]
        except IndexError:
            return None
        return discover_endpoint(mai_root)

    def _start_client(self) -> None:
        """创建并启动 NapCat 客户端。"""

        if self._client is not None or self._endpoint is None:
            return
        self._client = NapCatClient(
            self._endpoint,
            logger=self.ctx.logger,
            action_timeout=self.config.connection.action_timeout_seconds,
            reconnect_initial=self.config.connection.reconnect_initial_seconds,
            reconnect_max=self.config.connection.reconnect_max_seconds,
            on_connected=self._on_napcat_connected,
        )
        self._client.start()

    async def _stop_client(self) -> None:
        """停止 NapCat 客户端。"""

        client = self._client
        self._client = None
        self._auth = None
        self._auth_at = 0.0
        if client is not None:
            await client.stop()

    async def _on_napcat_connected(self, client: NapCatClient) -> None:
        """NapCat 连接成功后的回调。"""

        self._auth = None
        self._auth_at = 0.0
        self.ctx.logger.info("QQ 空间动态管家已连上 NapCat，账号=%s", client.self_id or "未知")

    # ---------------------------------------------------------------- 后台调度

    def _start_schedulers(self) -> None:
        """按配置启动发说说 / 浏览动态任务。"""

        self._stopped = False
        if self.config.publish.enabled and self._publish_task is None:
            self._publish_task = asyncio.create_task(self._publish_loop())
        if self.config.browse.enabled and self._browse_task is None:
            self._browse_task = asyncio.create_task(self._browse_loop())

    async def _stop_schedulers(self) -> None:
        """停止后台任务。"""

        tasks = [task for task in (self._publish_task, self._browse_task) if task is not None]
        self._publish_task = None
        self._browse_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _publish_loop(self) -> None:
        """发说说后台循环。"""

        initial_delay = max(0, int(self.config.publish.initial_delay_seconds))
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while not self._stopped:
            try:
                await self._maybe_publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("发说说任务异常: %s", exc, exc_info=True)
                await self._notify_error(f"发说说任务异常：{exc}")
            await asyncio.sleep(self._next_sleep(self.config.publish.check_interval_minutes, self.config.publish.jitter_seconds))

    async def _browse_loop(self) -> None:
        """浏览好友动态后台循环。"""

        initial_delay = max(0, int(self.config.browse.initial_delay_seconds))
        if initial_delay:
            await asyncio.sleep(initial_delay)
        while not self._stopped:
            try:
                await self._maybe_browse()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ctx.logger.error("浏览好友动态任务异常: %s", exc, exc_info=True)
                await self._notify_error(f"浏览好友动态异常：{exc}")
            await asyncio.sleep(self._next_sleep(self.config.browse.check_interval_minutes, self.config.browse.jitter_seconds))

    @staticmethod
    def _next_sleep(interval_minutes: int, jitter_seconds: int) -> float:
        """计算下一轮等待时间。"""

        base = max(60, int(interval_minutes) * 60)
        jitter = max(0, int(jitter_seconds))
        return base + random.uniform(0, jitter)

    # ---------------------------------------------------------------- 发说说

    async def _maybe_publish(self, *, force: bool = False, topic: str = "") -> tuple[bool, str]:
        """执行一轮发说说决策。

        Returns:
            tuple[bool, str]: 是否发布成功，以及面向命令调用者的提示。
        """

        publish = self.config.publish
        if not publish.enabled and not force:
            return False, "自动发说说未启用。"

        now = time.time()
        if self._daily_count("posts") >= publish.max_posts_per_day:
            return False, "今天发说说次数已达上限。"
        if not force:
            if not self._in_active_hours(publish.active_hours_start, publish.active_hours_end):
                return False, "当前不在发说说的活动时间。"
            last_publish_at = float(self._state.get("last_publish_at") or 0)
            if last_publish_at and now - last_publish_at < publish.min_interval_minutes * 60:
                return False, "距离上次发说说还没到最小间隔。"
            if self._last_publish_attempt_at and now - self._last_publish_attempt_at < 90:
                return False, "刚刚检查过，稍后再试。"

        self._last_publish_attempt_at = now
        bot_name, persona = await self._get_persona_and_name()
        recent_chat = await self._collect_recent_chat()
        messages = build_publish_messages(
            bot_name=bot_name,
            persona=persona,
            now_text=self._now_text(),
            recent_chat=recent_chat,
            recent_posts=self._format_recent_posts(),
            inspiration=self._format_inspiration(),
            max_chars=publish.content_max_chars,
            style=self.config.prompt.publish_style,
            topic_hint=topic,
        )

        raw = await self._call_llm(messages, temperature=self.config.llm.publish_temperature)
        if raw is None:
            return False, "模型调用失败，本轮不发说说。"

        decision = parse_publish_decision(raw, max_chars=publish.content_max_chars)
        if decision is None:
            self.ctx.logger.warning("模型发说说输出无法解析: %s", raw[:300])
            return False, "模型输出无法解析，本轮不发说说。"

        if not decision.should_post:
            self.ctx.logger.info("模型决定此刻不发说说：%s", decision.reason)
            return False, f"模型决定不发：{decision.reason}"

        if decision.confidence < publish.min_confidence:
            self.ctx.logger.info(
                "模型想发说说但置信度不足: %.2f < %.2f", decision.confidence, publish.min_confidence
            )
            return False, "模型置信度不足，本轮不发说说。"

        content = decision.content.strip()
        if not content:
            return False, "模型没有生成有效内容。"
        recent_contents = [
            str(item.get("content") or "")
            for item in self._state.get("publish_history", [])
            if isinstance(item, dict)
        ]
        if is_too_similar(content, recent_contents):
            self.ctx.logger.info("模型生成的说说与最近内容过于相似，已跳过")
            return False, "内容与最近说说过于相似，本轮不发。"

        if self.config.plugin.dry_run:
            if self.config.notify.notify_on_post:
                await self._notify_admin(
                    f"🧪 [演练] 本来要发说说：\n{content}\n\n理由：{decision.reason}\n置信度：{decision.confidence:.2f}"
                )
            self._set_last_publish_at(now)
            self._save_state()
            return False, "[演练] 已生成说说但未发布。"

        try:
            tid = await self._qzone_publish(content, ugc_right=publish.ugc_right)
        except Exception as exc:
            self.ctx.logger.warning("发表说说失败: %s", exc)
            await self._notify_error(f"发表说说失败：{exc}")
            return False, f"发表说说失败：{exc}"

        self._record_publish(content=content, tid=tid, decision=decision, now=now)
        if self.config.notify.notify_on_post:
            await self._notify_admin(
                f"📝 已发说说（tid={tid}）\n{content}\n\n理由：{decision.reason}\n置信度：{decision.confidence:.2f}"
            )
        return True, f"已发说说，tid={tid}。"

    def _set_last_publish_at(self, timestamp: float) -> None:
        """记录最后一次发说说（含演练）时间。"""

        self._state["last_publish_at"] = float(timestamp)

    def _record_publish(
        self,
        *,
        content: str,
        tid: str,
        decision: PublishDecision,
        now: float,
    ) -> None:
        """记录一条发布历史。"""

        history = self._state.get("publish_history")
        if not isinstance(history, list):
            history = []
            self._state["publish_history"] = history
        history.append(
            {
                "time": now,
                "tid": tid,
                "content": content,
                "topic": decision.topic,
                "reason": decision.reason,
                "confidence": decision.confidence,
            }
        )
        self._state["publish_history"] = history[-_MAX_HISTORY:]
        self._set_last_publish_at(now)
        self._increment_daily("posts")
        self._increment_stat("posts")
        self._save_state()

    # ---------------------------------------------------------------- 好友动态

    async def _maybe_browse(self, *, force: bool = False, target_qq: str = "") -> tuple[bool, str]:
        """执行一轮好友动态浏览 / 评论。

        Returns:
            tuple[bool, str]: 是否完整执行，以及提示文本。
        """

        browse = self.config.browse
        if not browse.enabled and not force:
            return False, "浏览好友动态未启用。"

        now = time.time()
        if not force and not self._in_active_hours(browse.active_hours_start, browse.active_hours_end):
            return False, "当前不在浏览动态的活动时间。"

        targets = await self._resolve_browse_targets(target_qq)
        if not targets:
            self._state["last_browse_at"] = now
            self._save_state()
            return False, "没有可浏览的好友，请配置 watch_friends 或开启 auto_friends。"

        comments_allowed = (
            browse.comment_enabled
            and (self._daily_count("comments") < browse.max_comments_per_day)
            and (
                not self._state.get("last_comment_at")
                or now - float(self._state.get("last_comment_at") or 0) >= browse.min_comment_interval_minutes * 60
            )
        )

        candidate_count = 0
        commented_count = 0
        seen_count = 0
        for target in targets:
            try:
                feeds = await self._qzone_get_feeds(target, num=browse.feeds_per_friend)
            except Exception as exc:
                self.ctx.logger.warning("读取 QQ %s 的说说失败: %s", target, exc)
                continue

            self._remember_inspiration(feeds)
            for feed in feeds:
                if self._feed_seen(feed):
                    continue
                if not self._feed_eligible(feed, now=now):
                    self._mark_feed_seen(feed)
                    continue

                seen_count += 1
                if comments_allowed and candidate_count < browse.max_candidates_per_cycle:
                    candidate_count += 1
                    if await self._maybe_comment_feed(feed):
                        commented_count += 1
                        self._mark_feed_seen(feed)
                        break
                self._mark_feed_seen(feed)

        self._state["last_browse_at"] = now
        self._save_state()
        if commented_count:
            return True, f"浏览了 {len(targets)} 位好友，评论了 {commented_count} 条动态。"
        return True, f"浏览了 {len(targets)} 位好友，共 {seen_count} 条新动态，没有评论。"

    async def _maybe_comment_feed(self, feed: Feed) -> bool:
        """让模型决定并执行一条动态的评论。"""

        if feed.secret != 0 or feed.ugc_right not in {1, 4}:
            return False
        if not feed.content and not feed.images:
            return False
        if self._looks_like_ad(feed.content):
            self.ctx.logger.info("动态疑似广告，跳过评论: %s", feed.tid)
            return False
        if feed.self_commented(self._auth.uin if self._auth else ""):
            return False

        bot_name, persona = await self._get_persona_and_name()
        messages = build_comment_messages(
            bot_name=bot_name,
            persona=persona,
            feed_text=feed.to_prompt_text(),
            max_chars=self.config.browse.comment_max_chars,
            style=self.config.prompt.comment_style,
        )
        raw = await self._call_llm(messages, temperature=self.config.llm.comment_temperature)
        if raw is None:
            return False

        decision = parse_comment_decision(raw, max_chars=self.config.browse.comment_max_chars)
        if decision is None:
            self.ctx.logger.warning("模型评论输出无法解析: %s", raw[:300])
            return False
        if not decision.should_comment:
            self.ctx.logger.info("模型决定不评论 %s：%s", feed.tid, decision.reason)
            return False
        if decision.confidence < self.config.browse.min_confidence:
            self.ctx.logger.info("评论置信度不足: %.2f", decision.confidence)
            return False
        comment = self._clean_comment(decision.comment)
        if not comment or self._is_generic_comment(comment):
            return False

        if self.config.plugin.dry_run:
            if self.config.notify.notify_on_comment:
                await self._notify_admin(
                    f"🧪 [演练] 本来要评论 {feed.author or feed.target_qq} 的动态：\n"
                    f"{feed.content[:200]}\n\n评论：{comment}\n理由：{decision.reason}"
                )
            return False

        try:
            await self._qzone_comment(feed.tid, feed.target_qq, comment)
        except Exception as exc:
            self.ctx.logger.warning("评论动态失败: %s", exc)
            await self._notify_error(f"评论动态失败：{exc}")
            return False

        self._record_comment(feed=feed, comment=comment, decision=decision)
        if self.config.notify.notify_on_comment:
            await self._notify_admin(
                f"💬 已评论 {feed.author or feed.target_qq} 的动态（tid={feed.tid}）\n"
                f"动态：{feed.content[:160]}\n评论：{comment}\n理由：{decision.reason}"
            )
        return True

    def _record_comment(self, *, feed: Feed, comment: str, decision: CommentDecision) -> None:
        """记录一条评论历史。"""

        now = time.time()
        history = self._state.get("comment_history")
        if not isinstance(history, list):
            history = []
            self._state["comment_history"] = history
        history.append(
            {
                "time": now,
                "target_qq": feed.target_qq,
                "author": feed.author,
                "tid": feed.tid,
                "content": feed.content[:300],
                "comment": comment,
                "reason": decision.reason,
                "confidence": decision.confidence,
            }
        )
        self._state["comment_history"] = history[-_MAX_HISTORY:]
        self._state["last_comment_at"] = now
        self._increment_daily("comments")
        self._increment_stat("comments")
        self._save_state()

    async def _resolve_browse_targets(self, target_qq: str = "") -> list[str]:
        """确定本轮要浏览的好友列表。"""

        if target_qq:
            normalized = _normalize_qq(target_qq)
            return [normalized] if normalized else []

        configured = [_normalize_qq(item) for item in self.config.browse.watch_friends]
        targets = [item for item in configured if item]
        if not targets and self.config.browse.auto_friends:
            targets = await self._fetch_friend_ids()

        if self._client is not None and self._client.self_id:
            targets = [item for item in targets if item != self._client.self_id]
        # 去重并保持顺序。
        deduped: list[str] = []
        seen: set[str] = set()
        for item in targets:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)

        limit = max(1, int(self.config.browse.max_friends_per_cycle))
        if len(deduped) <= limit:
            return deduped
        start = self._browse_cursor % len(deduped)
        selected = [deduped[(start + index) % len(deduped)] for index in range(limit)]
        self._browse_cursor = (start + limit) % len(deduped)
        return selected

    async def _fetch_friend_ids(self) -> list[str]:
        """从 NapCat 获取好友 QQ 列表。"""

        client = self._client
        if client is None or not client.connected:
            return []
        try:
            data = await client.call_data("get_friend_list", {"no_cache": False})
        except Exception as exc:
            self.ctx.logger.warning("获取好友列表失败: %s", exc)
            return []
        result: list[str] = []
        items = data if isinstance(data, list) else data.get("data") if isinstance(data, dict) else []
        for item in items or []:
            if isinstance(item, dict):
                qq = _normalize_qq(item.get("user_id"))
                if qq:
                    result.append(qq)
        return result

    def _feed_eligible(self, feed: Feed, *, now: float) -> bool:
        """判断一条动态是否值得让模型看。"""

        if not feed.tid:
            return False
        if not feed.content and not feed.images:
            return False
        if feed.secret != 0:
            return False
        if feed.ugc_right not in {1, 4}:
            return False
        if feed.created_at <= 0:
            return False
        if now - feed.created_at > self.config.browse.max_feed_age_hours * 3600:
            return False
        if self.config.browse.skip_if_commented:
            self_uin = self._auth.uin if self._auth else ""
            if feed.self_commented(self_uin):
                return False
        return True

    def _feed_seen(self, feed: Feed) -> bool:
        """判断动态是否已经看过。"""

        seen = self._state.get("seen_feeds")
        return isinstance(seen, dict) and feed.tid in seen

    def _mark_feed_seen(self, feed: Feed) -> None:
        """标记动态已看过。"""

        seen = self._state.setdefault("seen_feeds", {})
        if isinstance(seen, dict):
            seen[feed.tid] = time.time()

    def _remember_inspiration(self, feeds: list[Feed]) -> None:
        """保存最近动态，供发说说时作为灵感。"""

        self._inspiration_feeds = (feeds + self._inspiration_feeds)[:10]

    def _format_inspiration(self) -> str:
        """格式化好友动态灵感。"""

        lines: list[str] = []
        for feed in self._inspiration_feeds[:3]:
            content = feed.content or feed.rt_con
            if content:
                lines.append(f"- {feed.author or feed.target_qq}：{content[:160]}")
        return "\n".join(lines)

    def _looks_like_ad(self, content: str) -> bool:
        """判断内容是否像广告。"""

        text = str(content or "").casefold()
        hits = sum(1 for keyword in _AD_KEYWORDS if keyword.casefold() in text)
        return hits >= 2 or (hits >= 1 and ("http://" in text or "https://" in text))

    def _clean_comment(self, comment: str) -> str:
        """清理评论正文。"""

        text = str(comment or "").strip()
        text = re.sub(r"^@\S+\s*", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        max_chars = max(1, int(self.config.browse.comment_max_chars))
        return text[:max_chars].strip()

    @staticmethod
    def _is_generic_comment(comment: str) -> bool:
        """过滤过于空泛的评论。"""

        normalized = re.sub(r"[\s\W_]+", "", comment).casefold()
        return normalized in {item.casefold() for item in _GENERIC_COMMENTS}

    # ---------------------------------------------------------------- QQ 空间调用

    async def _get_auth(self, *, force: bool = False) -> QZoneAuth:
        """获取 QQ 空间凭据（带缓存）。"""

        now = time.time()
        ttl = max(60, int(self.config.connection.cookie_ttl_minutes) * 60)
        if not force and self._auth is not None and now - self._auth_at < ttl:
            return self._auth

        client = self._client
        if client is None or not client.connected:
            raise QZoneError("NapCat 未连接，无法获取 QQ 空间 Cookie")

        data = await client.call_data("get_cookies", {"domain": "qzone.qq.com"})
        if not isinstance(data, dict):
            raise QZoneError("NapCat get_cookies 返回格式异常")
        cookies_text = str(data.get("cookies") or "").strip()
        bkn = str(data.get("bkn") or "").strip()
        if not cookies_text:
            raise QZoneError("NapCat 未返回 QQ 空间 Cookie，请确认机器人已登录")

        auth = QZoneAuth.from_cookies(cookies_text, bkn)
        self._auth = auth
        self._auth_at = now
        self.ctx.logger.debug("已刷新 QQ 空间凭据，uin=%s", auth.uin)
        return auth

    def _build_qzone_client(self, auth: QZoneAuth) -> QZoneClient:
        """构造 QQ 空间 HTTP 客户端。"""

        return QZoneClient(
            auth,
            logger=self.ctx.logger,
            timeout=self.config.qzone.request_timeout_seconds,
            urls=self._qzone_urls or None,
        )

    async def _with_qzone(self, operation: Any, *, retry_on_any: bool = False) -> Any:
        """执行 QQ 空间操作，必要时刷新 Cookie 重试。"""

        attempts = max(0, int(self.config.qzone.retry_times)) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                auth = await self._get_auth(force=attempt > 0)
                client = self._build_qzone_client(auth)
                return await operation(client)
            except QZoneError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                if retry_on_any or exc.code in _AUTH_ERROR_CODES:
                    self.ctx.logger.warning("QQ 空间请求失败，刷新 Cookie 后重试: %s", exc)
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
        if isinstance(last_error, QZoneError):
            raise last_error
        raise QZoneError(f"QQ 空间请求失败: {last_error}") from last_error

    async def _qzone_get_feeds(self, target_qq: str, *, num: int) -> list[Feed]:
        """读取说说列表。"""

        async def operation(client: QZoneClient) -> list[Feed]:
            return await client.get_feeds(target_qq, num=num, filter_commented=False)

        result = await self._with_qzone(operation, retry_on_any=True)
        return result if isinstance(result, list) else []

    async def _qzone_publish(self, content: str, *, ugc_right: int) -> str:
        """发表说说。"""

        async def operation(client: QZoneClient) -> str:
            return await client.publish(content, ugc_right=ugc_right)

        result = await self._with_qzone(operation)
        return str(result)

    async def _qzone_comment(self, fid: str, target_qq: str, content: str) -> None:
        """评论说说。"""

        async def operation(client: QZoneClient) -> None:
            await client.comment(fid, target_qq, content)

        await self._with_qzone(operation)

    # ---------------------------------------------------------------- 上下文与模型

    async def _call_llm(self, messages: list[dict[str, str]], *, temperature: float) -> str | None:
        """调用模型并返回文本。"""

        try:
            result = await self.ctx.llm.generate(
                messages,
                task_name=str(self.config.llm.task_name or "utils"),
                model_name=str(self.config.llm.model_name or ""),
                temperature=float(temperature),
                max_tokens=int(self.config.llm.max_tokens),
            )
        except Exception as exc:
            self.ctx.logger.warning("模型调用异常: %s", exc)
            await self._notify_error(f"模型调用异常：{exc}")
            return None

        if not isinstance(result, dict) or not bool(result.get("success")):
            error = str(result.get("error") or "未知错误") if isinstance(result, dict) else "返回格式异常"
            self.ctx.logger.warning("模型调用失败: %s", error)
            await self._notify_error(f"模型调用失败：{error}")
            return None
        return str(result.get("response") or "").strip()

    async def _collect_recent_chat(self) -> str:
        """收集最近的聊天消息，作为发说说的素材。"""

        publish = self.config.publish
        if publish.context_message_limit <= 0:
            return ""
        now = time.time()
        start = now - max(0.5, publish.context_hours) * 3600
        try:
            result = await self.ctx.message.get_by_time(
                str(int(start)),
                str(int(now)),
                limit=int(publish.context_message_limit),
                limit_mode="latest",
                filter_mai=False,
            )
        except Exception as exc:
            self.ctx.logger.debug("读取最近聊天失败: %s", exc)
            return ""

        messages = result if isinstance(result, list) else result.get("messages") if isinstance(result, dict) else []
        if not isinstance(messages, list):
            return ""

        self_id = self._client.self_id if self._client is not None else ""
        lines: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if bool(message.get("is_notify")) or bool(message.get("is_command")):
                continue
            text = str(message.get("processed_plain_text") or "").strip()
            if not text:
                continue
            info = message.get("message_info")
            if not isinstance(info, dict):
                continue
            user_info = info.get("user_info")
            if not isinstance(user_info, dict):
                continue
            user_id = str(user_info.get("user_id") or "").strip()
            if self_id and user_id == self_id:
                continue
            nickname = str(user_info.get("user_cardname") or user_info.get("user_nickname") or user_id).strip()
            group_info = info.get("group_info")
            group_name = ""
            if isinstance(group_info, dict):
                group_name = str(group_info.get("group_name") or "").strip()
            timestamp = _safe_float(message.get("timestamp"), default=0.0)
            time_text = time.strftime("%H:%M", time.localtime(timestamp)) if timestamp > 0 else "--:--"
            prefix = f"{group_name}/{nickname}" if group_name else nickname
            lines.append(f"[{time_text}] {prefix}: {text[:120]}")

        if not lines:
            return ""
        return "\n".join(lines[-int(publish.context_message_limit) :])[:6000]

    def _format_recent_posts(self) -> str:
        """格式化最近发过的说说。"""

        history = self._state.get("publish_history")
        if not isinstance(history, list):
            return ""
        lines: list[str] = []
        for item in history[-5:]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if content:
                created = _safe_float(item.get("time"), default=0.0)
                when = time.strftime("%m-%d %H:%M", time.localtime(created)) if created > 0 else "未知时间"
                lines.append(f"- {when}：{content[:120]}")
        return "\n".join(lines)

    async def _get_persona_and_name(self) -> tuple[str, str]:
        """读取机器人昵称与人设。"""

        bot_name = "麦麦"
        persona = str(self.config.prompt.persona_override or "").strip()
        try:
            bot_config = await self.ctx.config.get("bot", {})
            if isinstance(bot_config, dict):
                bot_name = str(bot_config.get("nickname") or bot_name).strip() or bot_name
        except Exception:
            pass
        if not persona:
            try:
                personality_config = await self.ctx.config.get("personality", {})
                if isinstance(personality_config, dict):
                    persona = str(personality_config.get("personality") or "").strip()
            except Exception:
                pass
        return bot_name, persona

    def _now_text(self) -> str:
        """返回当前时间文本。"""

        now = self._local_now()
        return f"{now.strftime('%Y-%m-%d %H:%M')} {_WEEKDAYS[now.weekday()]}"

    # ---------------------------------------------------------------- 时间与统计

    def _local_now(self) -> datetime:
        """返回配置时区的当前时间。"""

        timezone = str(self.config.plugin.timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
        try:
            return datetime.now(tz=ZoneInfo(timezone))
        except (ZoneInfoNotFoundError, ValueError):
            return datetime.now()

    def _in_active_hours(self, start: str, end: str) -> bool:
        """判断当前是否在活动时间内。"""

        current = self._local_now().hour * 60 + self._local_now().minute
        start_minutes = _parse_hhmm(start)
        end_minutes = _parse_hhmm(end)
        if start_minutes is None or end_minutes is None:
            return True
        if start_minutes == end_minutes:
            return True
        if start_minutes < end_minutes:
            return start_minutes <= current < end_minutes
        return current >= start_minutes or current < end_minutes

    def _today_key(self) -> str:
        """返回今天的日期键。"""

        return self._local_now().strftime("%Y-%m-%d")

    def _daily_counts(self) -> dict[str, int]:
        """返回今天的计数。"""

        daily = self._state.setdefault("daily", {})
        if not isinstance(daily, dict):
            daily = {}
            self._state["daily"] = daily
        today = self._today_key()
        counts = daily.get(today)
        if not isinstance(counts, dict):
            counts = {"posts": 0, "comments": 0}
            daily[today] = counts
        counts.setdefault("posts", 0)
        counts.setdefault("comments", 0)
        # 只保留最近 7 天。
        for key in list(daily.keys()):
            if key != today and str(key) < today:
                daily.pop(key, None)
        return counts

    def _daily_count(self, field: str) -> int:
        """读取今天某个计数。"""

        try:
            return int(self._daily_counts().get(field, 0))
        except (TypeError, ValueError):
            return 0

    def _increment_daily(self, field: str) -> None:
        """增加今天的计数。"""

        counts = self._daily_counts()
        counts[field] = int(counts.get(field, 0)) + 1

    def _increment_stat(self, field: str) -> None:
        """增加累计统计。"""

        stats = self._state.setdefault("stats", {})
        if not isinstance(stats, dict):
            stats = {}
            self._state["stats"] = stats
        stats[field] = int(stats.get(field, 0)) + 1

    # ---------------------------------------------------------------- 通知

    async def _notify_admin(self, text: str) -> None:
        """向管理员发送私聊通知。"""

        if not self.config.notify.enabled or not str(text or "").strip():
            return
        admins = [_normalize_qq(item) for item in self.config.notify.admin_qqs]
        admins = [item for item in admins if item]
        if not admins:
            self.ctx.logger.debug("未配置管理员 QQ，跳过通知: %s", str(text)[:80])
            return

        client = self._client
        if client is None or not client.connected:
            return
        message = str(text)[:1500]
        for admin in admins:
            try:
                response = await client.call(
                    "send_private_msg",
                    {
                        "user_id": int(admin),
                        "message": [{"type": "text", "data": {"text": message}}],
                    },
                )
                ensure_action_ok(response, "send_private_msg")
            except Exception as exc:
                self.ctx.logger.warning("发送管理员通知失败: qq=%s error=%s", admin, exc)

    async def _notify_error(self, text: str) -> None:
        """节流后的错误通知。"""

        if not self.config.notify.notify_on_error:
            return
        now = time.time()
        if now - self._last_error_notify_at < 300:
            return
        self._last_error_notify_at = now
        await self._notify_admin(f"❌ {text}")

    # ---------------------------------------------------------------- 命令

    @Command(
        "qzone_status",
        description="查看 QQ 空间动态管家状态",
        pattern=r"^/(?:空间状态|动态状态|qzone状态)\s*$",
    )
    async def handle_status_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """状态查询。"""

        allowed, _ = self._command_allowed(kwargs)
        if not allowed:
            return False, None, False

        connection = "已连接" if self._client is not None and self._client.connected else "未连接"
        last_publish = _safe_float(self._state.get("last_publish_at"), default=0.0)
        last_comment = _safe_float(self._state.get("last_comment_at"), default=0.0)
        daily = self._daily_counts()
        stats = self._state.get("stats") if isinstance(self._state.get("stats"), dict) else {}
        lines = [
            "QQ 空间动态管家状态",
            f"插件：{'启用' if self.config.plugin.enabled else '停用'}"
            f"{'（演练模式）' if self.config.plugin.dry_run else ''}",
            f"NapCat：{connection}",
            f"发说说：{'开启' if self.config.publish.enabled else '关闭'}，"
            f"今日 {daily.get('posts', 0)}/{self.config.publish.max_posts_per_day}",
            f"浏览动态：{'开启' if self.config.browse.enabled else '关闭'}，"
            f"评论 {'开启' if self.config.browse.comment_enabled else '关闭'}，"
            f"今日 {daily.get('comments', 0)}/{self.config.browse.max_comments_per_day}",
            f"累计：说说 {stats.get('posts', 0)}，评论 {stats.get('comments', 0)}",
            f"上次发说说：{_format_timestamp(last_publish)}",
            f"上次评论：{_format_timestamp(last_comment)}",
        ]
        return True, "\n".join(lines), True

    @Command(
        "qzone_publish_now",
        description="让模型立即生成并发布一条说说",
        pattern=r"^/(?:发说说|发动态|发空间)(?:\s+(?P<topic>.+))?$",
    )
    async def handle_publish_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """手动触发发说说。"""

        allowed, _ = self._command_allowed(kwargs)
        if not allowed:
            return False, None, False
        matched = kwargs.get("matched_groups") or {}
        topic = str(matched.get("topic") or "").strip()
        success, message = await self._maybe_publish(force=True, topic=topic)
        return True, message, True

    @Command(
        "qzone_read_now",
        description="读取好友的 QQ 空间动态",
        pattern=r"^/(?:看动态|看说说|看空间)(?:\s+(?P<target_qq>\d+))?(?:\s+(?P<limit>\d+))?$",
    )
    async def handle_read_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """手动读取动态。"""

        allowed, _ = self._command_allowed(kwargs)
        if not allowed:
            return False, None, False

        matched = kwargs.get("matched_groups") or {}
        target_qq = _normalize_qq(matched.get("target_qq"))
        limit = 3
        try:
            limit = max(1, min(10, int(matched.get("limit") or 3)))
        except (TypeError, ValueError):
            limit = 3

        targets = [target_qq] if target_qq else await self._resolve_browse_targets()
        if not targets:
            return True, "没有可读取的好友，请先配置 watch_friends 或开启 auto_friends。", True

        lines: list[str] = []
        for target in targets[:3]:
            try:
                feeds = await self._qzone_get_feeds(target, num=limit)
            except Exception as exc:
                lines.append(f"QQ {target}：读取失败（{exc}）")
                continue
            if not feeds:
                lines.append(f"QQ {target}：最近没有说说")
                continue
            lines.append(f"QQ {target} 的最近说说：")
            for feed in feeds[:limit]:
                content = _truncate_text(feed.content or feed.rt_con, 120)
                lines.append(
                    f"- [{feed.created_text}] {feed.author or target}：{content or '（无文字）'}"
                    f"（tid={feed.tid}，评论 {len(feed.comments)}）"
                )
        return True, "\n".join(lines)[:3000], True

    @Command(
        "qzone_comment_now",
        description="手动评论一条说说",
        pattern=r"^/评论动态\s+(?P<target_qq>\d+)\s+(?P<fid>\S+)\s+(?P<content>.+)$",
    )
    async def handle_comment_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """手动评论。"""

        allowed, _ = self._command_allowed(kwargs)
        if not allowed:
            return False, None, False

        matched = kwargs.get("matched_groups") or {}
        target_qq = _normalize_qq(matched.get("target_qq"))
        fid = str(matched.get("fid") or "").strip()
        content = str(matched.get("content") or "").strip()
        if not target_qq or not fid or not content:
            return True, "用法：/评论动态 <QQ> <tid> <评论内容>", True

        if self._daily_count("comments") >= self.config.browse.max_comments_per_day:
            return True, "今天评论次数已达上限。", True

        if self.config.plugin.dry_run:
            return True, f"[演练] 本来要评论 {target_qq} 的动态 {fid}：{content}", True

        try:
            await self._qzone_comment(fid, target_qq, content)
        except Exception as exc:
            return True, f"评论失败：{exc}", True

        now = time.time()
        history = self._state.get("comment_history")
        if not isinstance(history, list):
            history = []
            self._state["comment_history"] = history
        history.append(
            {
                "time": now,
                "target_qq": target_qq,
                "author": "",
                "tid": fid,
                "content": "",
                "comment": content,
                "reason": "管理员手动评论",
                "confidence": 1.0,
            }
        )
        self._state["comment_history"] = history[-_MAX_HISTORY:]
        self._state["last_comment_at"] = now
        self._increment_daily("comments")
        self._increment_stat("comments")
        self._save_state()
        return True, "已发送评论。", True

    def _command_allowed(self, kwargs: dict[str, Any]) -> tuple[bool, str | None]:
        """判断命令调用者是否允许操作。"""

        if bool(kwargs.get("is_local_operator")):
            return True, None
        if str(kwargs.get("group_id") or "").strip():
            return False, None
        sender = _normalize_qq(kwargs.get("user_id"))
        admins = {_normalize_qq(item) for item in self.config.notify.admin_qqs}
        if sender and sender in admins:
            return True, None
        return False, None

    # ---------------------------------------------------------------- 工具

    @Tool(
        "qzone_publish_moment",
        description="让模型生成并发布一条 QQ 空间说说；可选 topic 指定主题",
        parameters={
            "topic": {
                "type": "string",
                "description": "可选的主题提示，例如“今天的晚饭”“吐槽早八”",
                "required": False,
            }
        },
    )
    async def tool_publish_moment(self, topic: str = "", **kwargs: Any) -> dict[str, Any]:
        """工具：发说说。"""

        del kwargs
        success, message = await self._maybe_publish(force=True, topic=str(topic or "").strip())
        return {"content": message, "success": success}

    @Tool(
        "qzone_read_moments",
        description="读取好友最近的 QQ 空间说说，返回文字摘要；可选 target_qq 只看某个人",
        parameters={
            "target_qq": {
                "type": "string",
                "description": "可选的目标 QQ；留空则读取配置中的好友",
                "required": False,
            },
            "limit": {
                "type": "integer",
                "description": "每个人读取几条，默认 3",
                "required": False,
            },
        },
    )
    async def tool_read_moments(self, target_qq: str = "", limit: int = 3, **kwargs: Any) -> dict[str, Any]:
        """工具：读取动态。"""

        del kwargs
        try:
            count = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            count = 3
        target = _normalize_qq(target_qq)
        targets = [target] if target else await self._resolve_browse_targets()
        if not targets:
            return {"content": "没有可读取的好友动态。"}
        lines: list[str] = []
        for item in targets[:3]:
            try:
                feeds = await self._qzone_get_feeds(item, num=count)
            except Exception as exc:
                lines.append(f"QQ {item}：读取失败（{exc}）")
                continue
            for feed in feeds[:count]:
                lines.append(
                    f"[{feed.created_text}] {feed.author or item}："
                    f"{_truncate_text(feed.content or feed.rt_con, 120)}（tid={feed.tid}）"
                )
        return {"content": "\n".join(lines)[:3000] or "最近没有说说。"}

    # ---------------------------------------------------------------- 持久化

    def _load_state(self) -> None:
        """读取状态文件。"""

        path = self._state_path
        if path is None or not path.is_file():
            self._state = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.ctx.logger.warning("读取 QQ 空间动态管家状态失败: %s", exc)
            self._state = {}
            return
        self._state = data if isinstance(data, dict) else {}
        for key in ("publish_history", "comment_history"):
            if not isinstance(self._state.get(key), list):
                self._state[key] = []
        if not isinstance(self._state.get("seen_feeds"), dict):
            self._state["seen_feeds"] = {}
        if not isinstance(self._state.get("daily"), dict):
            self._state["daily"] = {}
        if not isinstance(self._state.get("stats"), dict):
            self._state["stats"] = {}
        self._prune_state()

    def _prune_state(self) -> None:
        """清理过期状态。"""

        now = time.time()
        seen = self._state.get("seen_feeds")
        if isinstance(seen, dict):
            for key, value in list(seen.items()):
                if now - _safe_float(value, default=0.0) > _SEEN_RETENTION_SECONDS:
                    seen.pop(key, None)

        daily = self._state.get("daily")
        if isinstance(daily, dict):
            today = self._today_key()
            for key in list(daily.keys()):
                if str(key) < today:
                    daily.pop(key, None)

        for key in ("publish_history", "comment_history"):
            history = self._state.get(key)
            if isinstance(history, list):
                self._state[key] = history[-_MAX_HISTORY:]

    def _save_state(self) -> None:
        """原子写入状态文件。"""

        path = self._state_path
        if path is None:
            return
        self._state["version"] = _STATE_VERSION
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, path)
        except Exception as exc:
            self.ctx.logger.warning("保存 QQ 空间动态管家状态失败: %s", exc)


def create_plugin() -> QZonePilotPlugin:
    """创建插件实例。"""

    return QZonePilotPlugin()


def _normalize_qq(value: Any) -> str:
    """规范化 QQ 号。"""

    text = str(value or "").strip()
    if text.lower().startswith("qq:"):
        text = text[3:].strip()
    if text.startswith("o") or text.startswith("O"):
        text = text[1:]
    text = text.lstrip("0")
    return text if text.isdigit() else ""


def _safe_float(value: Any, *, default: float) -> float:
    """安全转换为浮点数。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_hhmm(value: str) -> int | None:
    """解析 HH:MM 为分钟。"""

    match = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", str(value or ""))
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _format_timestamp(timestamp: float) -> str:
    """格式化时间戳。"""

    if timestamp <= 0:
        return "从未"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OverflowError, OSError, ValueError):
        return "未知"


def _truncate_text(value: str, limit: int) -> str:
    """截断文本。"""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]
