"""活跃度控制 · AstrBot 插件

群聊三态：
  /活跃  每两分钟读群并回复；被 @ 或唤醒词立即回复
  /潜水  不主动读群，不响应唤醒词；被 @ 时读取上下文回复
  /睡眠  只响应 owner 的 @ 和 owner 的指令

owner 默认读取 AstrBot 的 admins_id，也可以在插件配置的 owner_ids 中追加/覆盖。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.astrbot_message import MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.regex import RegexFilter

from .core import (
    ChatMessage,
    Mode,
    build_activity_block,
    decide_action,
    format_transcript,
    match_wake_word,
    mode_icon,
    mode_label,
    normalize_mode,
    normalize_user_ids,
    normalize_words,
    resolve_owner_ids,
)
from .store import StateStore

PLUGIN_NAME = "astrbot_plugin_activity_control"
DISPLAY_NAME = "活跃度控制"
PLUGIN_VERSION = "1.0.0"
MAX_GROUP_BUFFER = 200
BUSY_TIMEOUT_SECONDS = 300


def _text_part(text: str):
    """兼容不同 AstrBot 版本的临时文本 part。"""
    try:
        from astrbot.core.agent.message import TextPart

        part = TextPart(text=text)
        if hasattr(part, "mark_as_temp"):
            part.mark_as_temp()
        return part
    except Exception:
        return {"type": "text", "text": text, "_no_save": True}


class ActivityCronEvent(CronMessageEvent):
    """定时主动回复使用的合成事件。

    走 AstrBot 原生事件管线，因此会带上人格、技能、工具和发送链路。
    与普通 Cron 事件的区别是：发送前会检查本次回复是否已被模式切换/卸载取消。
    """

    def __init__(
        self,
        *,
        context: Context,
        umo: str,
        group_id: str,
        text: str,
        sender_id: str,
        sender_name: str,
        token: str,
        plugin: "ActivityControlPlugin",
    ) -> None:
        session = MessageSession.from_str(umo)
        super().__init__(
            context=context,
            session=session,
            message=text,
            sender_id=sender_id or "activity_control",
            sender_name=sender_name or DISPLAY_NAME,
            message_type=session.message_type,
            extras={
                "_activity_synthetic": True,
                "_activity_token": token,
                "_api_key_allow_admin_role": False,
                "enable_streaming": False,
            },
        )
        self.message_obj.group_id = str(group_id)
        # CronMessageEvent 默认把 self_id 设成 sender_id；这里必须改成真实机器人 ID，
        # 否则平台开启“忽略机器人自己消息”时会把这条主动回复当成自己发的而丢掉。
        self.message_obj.self_id = plugin._self_ids.get(str(group_id), "") or "0"
        self.message_obj.sender = MessageMember(
            user_id=str(sender_id or "activity_control"),
            nickname=sender_name or DISPLAY_NAME,
        )
        self._activity_plugin = plugin
        self.set_extra("_activity_synthetic", True)
        self.set_extra("_activity_token", token)
        self.set_extra("_activity_sending_started", False)

    async def send(self, message: MessageChain) -> None:
        # 只检查第一条消息：一次 LLM 结果可能被拆成多条发送，
        # 第一条发出后 token 会在 after_message_sent 里被结算掉，
        # 后续分条不应因此被丢弃。
        if not self.get_extra("_activity_sending_started"):
            if not self._activity_plugin._token_alive(
                str(self.get_group_id()), str(self.get_extra("_activity_token") or "")
            ):
                # 模式已切换或插件已卸载：安静丢弃，不再补发。
                return
            self.set_extra("_activity_sending_started", True)
        await super().send(message)


@register(
    PLUGIN_NAME,
    "DC",
    "群聊活跃/潜水/睡眠三态控制：定时读群、@/唤醒词即时回复、owner 识别",
    PLUGIN_VERSION,
)
class ActivityControlPlugin(Star):
    """活跃度控制

    在 QQ 群聊中使用 /活跃、/潜水、/睡眠 切换机器人的参与程度。
    owner 默认取 AstrBot 配置里的 admins_id，也可以在本插件配置 owner_ids 中覆盖或追加。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config: dict[str, Any] = config or {}

        self._closing = False
        self._loop_task: asyncio.Task | None = None
        self._buffers: dict[str, deque[ChatMessage]] = {}
        self._tokens: dict[str, str] = {}
        self._busy_until: dict[str, float] = {}
        self._synthetic: dict[str, ActivityCronEvent] = {}
        self._umos: dict[str, str] = {}
        self._self_ids: dict[str, str] = {}

        default_mode = normalize_mode(self._cfg("default_mode", Mode.DIVE.value), Mode.DIVE.value)
        data_dir = self._resolve_data_dir()
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("[activity_control] 数据目录不可写，回退到插件目录: %s", exc)
            data_dir = Path(__file__).resolve().parent / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
        self.store = StateStore(data_dir / "state.json", default_mode=default_mode)
        for err in self.store.load_errors:
            logger.warning("[activity_control] %s", err)

        if not normalize_user_ids(self._cfg("owner_ids", [])):
            logger.warning(
                "[activity_control] 插件 owner_ids 为空；将只使用 AstrBot 的 admins_id 识别 owner。"
                " 若 admins_id 也没有真实 QQ 号，/活跃、/潜水、/睡眠 将无法使用。"
            )
        logger.info(
            "[activity_control] 已加载：默认模式=%s 间隔=%ss 状态文件=%s",
            mode_label(default_mode),
            self._interval(),
            self.store.path,
        )

        self._loop_task = asyncio.create_task(self._periodic_loop())

    # ── 配置 ────────────────────────────────────────────────────────────

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self.config.get(key, default)
        if value is None:
            return default
        if isinstance(value, str) and not value.strip() and isinstance(default, str):
            return default
        return value

    def _interval(self) -> int:
        try:
            value = int(self._cfg("interval_seconds", 120))
        except (TypeError, ValueError):
            value = 120
        return max(10, min(3600, value))

    def _owner_source(self) -> str:
        return str(self._cfg("owner_source", "merge") or "merge").strip().lower()

    def _owner_ids_plugin(self) -> list[str]:
        return normalize_user_ids(self._cfg("owner_ids", []))

    def _astrbot_admins(self) -> list[str]:
        try:
            conf = self.context.get_config()
            return normalize_user_ids(conf.get("admins_id", []))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[activity_control] 读取 AstrBot admins_id 失败: %s", exc)
            return []

    def _owners_for_event(self, event: AstrMessageEvent) -> tuple[list[str], str]:
        source = self._owner_source()
        owners = resolve_owner_ids(
            self._astrbot_admins(),
            self._owner_ids_plugin(),
            source,
        )
        # AstrBot 已判定为 admin 的发送者也视为 owner（仅当来源允许 AstrBot 配置时）。
        if source not in {"plugin", "custom", "manual"}:
            try:
                if event.is_admin():
                    sender = str(event.get_sender_id() or "").strip()
                    if sender and sender not in owners:
                        owners.append(sender)
            except Exception:
                pass
        return owners, source

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "").strip()
        if not sender:
            return False
        owners, _ = self._owners_for_event(event)
        return sender in owners

    def _wake_words(self) -> list[str]:
        return normalize_words(self._cfg("wake_words", []))

    def _wake_match_mode(self) -> str:
        return str(self._cfg("wake_match", "contains") or "contains").strip().lower()

    def _member_commands_in_active(self) -> bool:
        return self._as_bool(self._cfg("member_commands_in_active", True), True)

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on", "y"}:
                return True
            if lowered in {"0", "false", "no", "off", "n", ""}:
                return False
        return bool(value)

    def _resolve_data_dir(self) -> Path:
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:
            return Path(__file__).resolve().parent / "data"

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def terminate(self) -> None:  # noqa: D401 - AstrBot 生命周期
        self._closing = True
        task = self._loop_task
        self._loop_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for event in list(self._synthetic.values()):
            try:
                event.stop_event()
            except Exception:
                pass
        self._synthetic.clear()
        self._tokens.clear()
        try:
            self.store.save(force=True)
        except OSError as exc:
            logger.warning("[activity_control] 退出时保存状态失败: %s", exc)
        logger.info("[activity_control] 已卸载，状态已保存")

    async def _periodic_loop(self) -> None:
        """按 interval_seconds 轮询 active 群，有新消息就触发一次主动回复。"""
        try:
            while not self._closing:
                await asyncio.sleep(self._interval())
                if self._closing:
                    break
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error("[activity_control] 定时任务异常: %s", exc, exc_info=True)
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        now = time.time()
        # 清理超时的 in-flight 标记，避免一次异常把群永久卡死。
        for gid, until in list(self._busy_until.items()):
            if until and until <= now:
                self._busy_until.pop(gid, None)
                self._tokens.pop(gid, None)
                self._synthetic.pop(gid, None)

        self.store.prune(max_groups=500, ttl_days=30)
        for gid in list(self._umos):
            if gid not in self.store.groups:
                self._umos.pop(gid, None)
                self._self_ids.pop(gid, None)
                self._buffers.pop(gid, None)
        for gid, buf in list(self._buffers.items()):
            if self._closing:
                return
            state = self.store.get(gid)
            if state.mode != Mode.ACTIVE.value:
                continue
            if not self._has_human(buf):
                continue
            await self._enqueue_periodic(gid)
        try:
            self.store.save()
        except OSError as exc:
            logger.warning("[activity_control] 保存状态失败: %s", exc)

    # ── 群消息主入口 ────────────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10000)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            await self._handle_group_message(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 绝不因本插件异常打断消息管线
            logger.error("[activity_control] 处理群消息异常: %s", exc, exc_info=True)

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        if self._closing:
            return
        # 自己排入的合成事件不再走三态拦截，否则会被当成普通群消息。
        if event.get_extra("_activity_synthetic"):
            return
        gid = str(event.get_group_id() or "").strip()
        if not gid:
            return
        sender_id = str(event.get_sender_id() or "").strip()
        self_id = str(event.get_self_id() or "").strip()
        if not sender_id or (self_id and sender_id == self_id):
            return

        self._umos[gid] = event.unified_msg_origin
        if self_id:
            self._self_ids[gid] = self_id

        now = time.time()
        state = self.store.get(gid)
        self.store.touch(gid, now)
        mode = normalize_mode(state.mode, self.store.default_mode)

        at_bot = self._is_at_bot(event)
        is_command = self._is_command(event)
        owner = self._is_owner(event)
        text = self._event_text(event, at_bot=at_bot)
        wake_word = (
            ""
            if is_command
            else match_wake_word(text, self._wake_words(), self._wake_match_mode())
        )

        # 聊天记录只存在内存里，用于之后 @ 时补上下文；睡眠模式下也不会
        # 对非 owner 做任何响应，只是保留上下文给 owner 的 @ 使用。
        if not is_command:
            self._record_message(gid, event, text)

        decision = decide_action(
            mode=mode,
            is_command=is_command,
            is_owner=owner,
            at_bot=at_bot,
            wake_word=wake_word,
            member_commands_in_active=self._member_commands_in_active(),
        )

        if decision.action == "allow_command":
            # 指令交给 AstrBot 的原生指令链路，避免再触发默认 LLM。
            event.should_call_llm(False)
            return

        if decision.action == "block_command":
            owners, _ = self._owners_for_event(event)
            if not owners:
                # 还没有任何 owner 时，放行到指令处理器，让它提示如何配置；
                # 指令处理器本身仍会拒绝非 owner，不会因此获得控制权。
                event.should_call_llm(False)
                return
            event.should_call_llm(False)
            event.stop_event()
            logger.debug(
                "[activity_control] 拦截非 owner 指令 gid=%s mode=%s", gid, mode
            )
            return

        if decision.action == "reply":
            await self._start_immediate_reply(event, gid, text, decision.trigger, owner)
            return

        # 活跃模式的普通消息、潜水/睡眠模式下所有不满足条件的消息：
        # 停止事件传播，确保不会被其他 LLM 链路误回复。
        event.should_call_llm(False)
        event.stop_event()

    async def _start_immediate_reply(
        self,
        event: AstrMessageEvent,
        gid: str,
        text: str,
        trigger: str,
        owner: bool,
    ) -> None:
        if self._is_busy(gid):
            # 已有一次回复在路上；当前消息已进缓存，交给下一轮。
            event.should_call_llm(False)
            event.stop_event()
            logger.debug("[activity_control] 群 %s 正忙，跳过本次即时触发", gid)
            return

        batch = self._snapshot(gid)
        if not batch:
            batch = [
                ChatMessage(
                    uid=uuid.uuid4().hex,
                    sender_id=str(event.get_sender_id() or ""),
                    sender_name=event.get_sender_name() or "群友",
                    text=text or "（@了你）",
                    ts=time.time(),
                )
            ]
        batch_ids = [msg.uid for msg in batch]
        token = self._begin_reply(gid)
        state = self.store.get(gid)
        block = build_activity_block(
            mode=state.mode,
            trigger=trigger,
            transcript=format_transcript(
                batch,
                limit=self._context_messages(),
                max_chars=self._context_chars(),
                max_message_chars=self._message_chars(),
            ),
            custom_instruction=str(self._cfg("reply_instruction", "") or ""),
            seconds_since_reply=int(time.time() - state.last_reply_at)
            if state.last_reply_at
            else 0,
        )
        event.set_extra(
            "_activity_ctx",
            {
                "gid": gid,
                "token": token,
                "batch_ids": batch_ids,
                "block": block,
                "trigger": trigger,
                "owner": owner,
            },
        )
        # 纯 @ 没有正文时给管线一个非空 message_str，否则会被当作空消息跳过。
        if not str(event.message_str or "").strip():
            marker = "（有人@你，但没有附带文字）"
            event.message_str = marker
            try:
                event.message_obj.message_str = marker
            except Exception:
                pass
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.should_call_llm(True)
        logger.info(
            "[activity_control] 即时回复 gid=%s trigger=%s owner=%s 缓存=%d 条",
            gid,
            trigger,
            owner,
            len(batch),
        )

    async def _enqueue_periodic(self, gid: str) -> None:
        if self._closing or self._is_busy(gid):
            return
        state = self.store.get(gid)
        if state.mode != Mode.ACTIVE.value:
            return
        buf = self._buffers.get(gid)
        if not buf:
            return
        batch = self._snapshot(gid)
        if not self._has_human(batch):
            return

        token = self._begin_reply(gid)
        block = build_activity_block(
            mode=state.mode,
            trigger="timer",
            transcript=format_transcript(
                batch,
                limit=self._context_messages(),
                max_chars=self._context_chars(),
                max_message_chars=self._message_chars(),
            ),
            custom_instruction=str(self._cfg("reply_instruction", "") or ""),
            seconds_since_reply=int(time.time() - state.last_reply_at)
            if state.last_reply_at
            else 0,
        )
        human = [m for m in batch if not m.is_bot]
        sender = human[-1] if human else batch[-1]
        event = ActivityCronEvent(
            context=self.context,
            umo=self._umo_for_group(gid),
            group_id=gid,
            text="（活跃度控制：定时读取群聊，请结合上下文自然回复）",
            sender_id=sender.sender_id,
            sender_name=sender.sender_name,
            token=token,
            plugin=self,
        )
        event.set_extra(
            "_activity_ctx",
            {
                "gid": gid,
                "token": token,
                "batch_ids": [m.uid for m in batch],
                "block": block,
                "trigger": "timer",
                "owner": False,
            },
        )
        self._synthetic[gid] = event
        try:
            queue = self.context.get_event_queue()
            await queue.put(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[activity_control] 投递定时回复事件失败 gid=%s: %s", gid, exc)
            self._end_reply(gid, token, consume=False)
            return
        logger.info(
            "[activity_control] 已投递定时回复 gid=%s 缓存=%d 条", gid, len(batch)
        )

    def _umo_for_group(self, gid: str) -> str:
        """使用该群最近一条真实消息的 unified_msg_origin。

        UMO 的第一段是平台实例 ID（不是适配器类型），所以不能硬编码
        aiocqhttp，否则多平台或 QQ 官方接口会发错会话。
        """
        umo = self._umos.get(gid, "")
        if umo:
            return umo
        # 仅在极端的“重启后尚未收到任何群消息”场景下兜底。
        return f"aiocqhttp:GroupMessage:{gid}"

    # ── LLM 请求注入与发送后结算 ────────────────────────────────────────

    @filter.on_llm_request(priority=1000)
    async def inject_activity_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        try:
            ctx = event.get_extra("_activity_ctx")
            if not isinstance(ctx, dict):
                return
            if not self._token_alive(str(ctx.get("gid") or ""), str(ctx.get("token") or "")):
                return
            block = str(ctx.get("block") or "").strip()
            if not block:
                return
            try:
                req.extra_user_content_parts.append(_text_part(block))
            except AttributeError:
                req.extra_user_content_parts = [_text_part(block)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("[activity_control] 注入上下文失败: %s", exc, exc_info=True)

    @filter.on_llm_response(priority=1000)
    async def remember_bot_reply(self, event: AstrMessageEvent, response: Any) -> None:
        try:
            ctx = event.get_extra("_activity_ctx")
            if not isinstance(ctx, dict):
                return
            gid = str(ctx.get("gid") or "")
            if not self._token_alive(gid, str(ctx.get("token") or "")):
                return
            if str(getattr(response, "role", "") or "") == "err":
                return
            if event.get_extra("_activity_response_recorded"):
                return
            text = str(getattr(response, "completion_text", "") or "").strip()
            if text:
                event.set_extra("_activity_response_text", text)
                event.set_extra("_activity_response_recorded", True)
                self._record_bot_message(gid, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[activity_control] 记录回复失败: %s", exc)

    @filter.on_decorating_result(priority=-1000)
    async def remember_decorated_reply(self, event: AstrMessageEvent) -> None:
        try:
            ctx = event.get_extra("_activity_ctx")
            if not isinstance(ctx, dict):
                return
            if event.get_extra("_activity_response_recorded"):
                return
            if event.get_extra("_activity_response_text"):
                return
            gid = str(ctx.get("gid") or "")
            if not self._token_alive(gid, str(ctx.get("token") or "")):
                return
            result = event.get_result()
            text = result.get_plain_text().strip() if result else ""
            if text:
                event.set_extra("_activity_response_text", text)
                event.set_extra("_activity_response_recorded", True)
                self._record_bot_message(gid, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[activity_control] 记录装饰后回复失败: %s", exc)

    @filter.after_message_sent(priority=1000)
    async def finish_activity_reply(self, event: AstrMessageEvent) -> None:
        try:
            ctx = event.get_extra("_activity_ctx")
            if not isinstance(ctx, dict):
                return
            gid = str(ctx.get("gid") or "")
            token = str(ctx.get("token") or "")
            batch_ids = [str(x) for x in (ctx.get("batch_ids") or [])]
            if not self._token_alive(gid, token):
                return
            self._end_reply(gid, token, batch_ids=batch_ids, consume=True)
            logger.info("[activity_control] 回复完成 gid=%s 消费=%d 条", gid, len(batch_ids))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[activity_control] 结算回复失败: %s", exc, exc_info=True)

    # ── 指令 ────────────────────────────────────────────────────────────

    @filter.command("活跃", alias={"开启活跃"}, priority=1000)
    async def cmd_active(self, event: AstrMessageEvent):
        async for item in self._switch_mode(event, Mode.ACTIVE.value):
            yield item

    @filter.command("潜水", alias={"下潜"}, priority=1000)
    async def cmd_dive(self, event: AstrMessageEvent):
        async for item in self._switch_mode(event, Mode.DIVE.value):
            yield item

    @filter.command("睡眠", alias={"休眠"}, priority=1000)
    async def cmd_sleep(self, event: AstrMessageEvent):
        async for item in self._switch_mode(event, Mode.SLEEP.value):
            yield item

    @filter.command("活跃度", alias={"活跃状态"}, priority=1000)
    async def cmd_status(self, event: AstrMessageEvent):
        async for item in self._status(event):
            yield item

    async def _switch_mode(self, event: AstrMessageEvent, mode: str):
        gid = str(event.get_group_id() or "").strip()
        if not gid:
            yield event.plain_result("这个指令只能在群聊里用。")
            return
        denied = self._owner_denied_text(event)
        if denied:
            yield event.plain_result(denied)
            return

        state = self.store.set_mode(gid, mode)
        self._invalidate(gid)
        try:
            self.store.save()
        except OSError as exc:
            logger.warning("[activity_control] 保存模式失败: %s", exc)

        label = mode_label(mode)
        icon = mode_icon(mode)
        owners, source = self._owners_for_event(event)
        owner_text = "、".join(owners) if owners else "未配置"
        if mode == Mode.ACTIVE.value:
            detail = (
                f"每 {self._interval()} 秒读取一次本群新消息并回复；\n"
                "有人 @ 或发送唤醒词时立即回复。"
            )
        elif mode == Mode.DIVE.value:
            detail = (
                "不主动读群，也不响应唤醒词；\n"
                "只有被 @ 时才读取最近消息并回复。\n"
                "只有 owner 的 / 指令会生效。"
            )
        else:
            detail = (
                "只响应 owner 的 @ 和 owner 的 / 指令；\n"
                "其他人的消息、@、唤醒词全部忽略。"
            )
        yield event.plain_result(
            f"{icon} 本群已切换到「{label}」模式\n"
            f"{detail}\n"
            f"owner（{self._source_label(source)}）：{owner_text}"
        )

    async def _status(self, event: AstrMessageEvent):
        gid = str(event.get_group_id() or "").strip()
        if not gid:
            yield event.plain_result("这个指令只能在群聊里用。")
            return
        denied = self._owner_denied_text(event)
        if denied:
            yield event.plain_result(denied)
            return

        state = self.store.get(gid)
        owners, source = self._owners_for_event(event)
        astrbot_ids = self._astrbot_admins()
        plugin_ids = self._owner_ids_plugin()
        wake_words = self._wake_words()
        buffer = self._buffers.get(gid)
        text = (
            f"📊 {DISPLAY_NAME} · 本群状态\n"
            f"模式：{mode_icon(state.mode)} {mode_label(state.mode)}\n"
            f"定时读取：{self._interval()} 秒（仅活跃模式）\n"
            f"唤醒词：{'、'.join(wake_words) if wake_words else '未配置（仅 @ 触发）'}\n"
            f"owner 来源：{self._source_label(source)}\n"
            f"AstrBot admins_id：{'、'.join(astrbot_ids) if astrbot_ids else '（空）'}\n"
            f"插件 owner_ids：{'、'.join(plugin_ids) if plugin_ids else '（空）'}\n"
            f"生效 owner：{'、'.join(owners) if owners else '未配置'}\n"
            f"本群内存缓存：{len(buffer) if buffer else 0} 条（不落盘）\n"
            f"可用指令：/活跃  /潜水  /睡眠  /活跃度"
        )
        yield event.plain_result(text)

    def _owner_denied_text(self, event: AstrMessageEvent) -> str:
        owners, _ = self._owners_for_event(event)
        if not owners:
            return (
                "还没有配置 owner，无法切换模式。\n"
                "请在 AstrBot 全局配置的 admins_id 或本插件的 owner_ids 里填写 QQ 号。"
            )
        if not self._is_owner(event):
            return "只有 owner 可以操作活跃度控制。"
        return ""

    def _source_label(self, source: str) -> str:
        return {
            "astrbot": "AstrBot admins_id",
            "global": "AstrBot admins_id",
            "admin": "AstrBot admins_id",
            "plugin": "插件 owner_ids",
            "custom": "插件 owner_ids",
            "manual": "插件 owner_ids",
        }.get(str(source or "").lower(), "AstrBot + 插件合并")

    # ── 事件解析工具 ────────────────────────────────────────────────────

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id() or "").strip()
        wake_on_at_all = self._as_bool(self._cfg("wake_on_at_all", False), False)
        treat_reply_as_at = self._as_bool(self._cfg("treat_reply_as_at", True), True)
        for comp in event.get_messages():
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq == "all":
                    if wake_on_at_all:
                        return True
                    continue
                if self_id and qq == self_id:
                    return True
            elif treat_reply_as_at and isinstance(comp, Reply):
                if self_id and str(getattr(comp, "sender_id", "") or "") == self_id:
                    return True
        return False

    def _is_command(self, event: AstrMessageEvent) -> bool:
        try:
            handlers = event.get_extra("activated_handlers") or []
        except Exception:
            handlers = []
        for handler in handlers:
            for item in getattr(handler, "event_filters", []) or []:
                if isinstance(item, (CommandFilter, CommandGroupFilter, RegexFilter)):
                    return True
        return False

    def _event_text(self, event: AstrMessageEvent, *, at_bot: bool) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        if at_bot:
            text = f"@你 {text}".strip() if text else "@你"
        if not text:
            try:
                text = str(event.get_message_outline() or "").strip()
            except Exception:
                text = ""
        if not text:
            text = "[非文本消息]"
        limit = self._message_chars()
        if len(text) > limit:
            text = text[: max(0, limit - 1)].rstrip() + "…"
        return text

    def _record_message(self, gid: str, event: AstrMessageEvent, text: str) -> None:
        if not text:
            return
        buf = self._buffers.get(gid)
        if buf is None:
            buf = deque(maxlen=MAX_GROUP_BUFFER)
            self._buffers[gid] = buf
        try:
            message_id = str(getattr(event.message_obj, "message_id", "") or "")
        except Exception:
            message_id = ""
        buf.append(
            ChatMessage(
                uid=uuid.uuid4().hex,
                sender_id=str(event.get_sender_id() or ""),
                sender_name=event.get_sender_name() or "群友",
                text=text,
                ts=time.time(),
                is_bot=False,
                message_id=message_id,
            )
        )

    def _record_bot_message(self, gid: str, text: str) -> None:
        if not gid or not text:
            return
        buf = self._buffers.get(gid)
        if buf is None:
            buf = deque(maxlen=MAX_GROUP_BUFFER)
            self._buffers[gid] = buf
        buf.append(
            ChatMessage(
                uid=uuid.uuid4().hex,
                sender_id=self._self_ids.get(gid, ""),
                sender_name=DISPLAY_NAME,
                text=text,
                ts=time.time(),
                is_bot=True,
            )
        )

    def _snapshot(self, gid: str) -> list[ChatMessage]:
        return list(self._buffers.get(gid) or [])

    @staticmethod
    def _has_human(messages) -> bool:
        return any(not msg.is_bot and str(msg.text or "").strip() for msg in messages)

    def _context_messages(self) -> int:
        try:
            return max(3, min(100, int(self._cfg("context_messages", 30))))
        except (TypeError, ValueError):
            return 30

    def _context_chars(self) -> int:
        try:
            return max(200, min(12000, int(self._cfg("context_chars", 4000))))
        except (TypeError, ValueError):
            return 4000

    def _message_chars(self) -> int:
        try:
            return max(50, min(2000, int(self._cfg("max_message_chars", 500))))
        except (TypeError, ValueError):
            return 500

    # ── in-flight / 取消 ────────────────────────────────────────────────

    def _begin_reply(self, gid: str) -> str:
        token = uuid.uuid4().hex
        self._tokens[gid] = token
        self._busy_until[gid] = time.time() + BUSY_TIMEOUT_SECONDS
        return token

    def _is_busy(self, gid: str) -> bool:
        until = self._busy_until.get(gid, 0.0)
        if not until:
            return False
        if until > time.time():
            return True
        self._busy_until.pop(gid, None)
        self._tokens.pop(gid, None)
        return False

    def _token_alive(self, gid: str, token: str) -> bool:
        if self._closing or not gid or not token:
            return False
        return self._tokens.get(gid) == token

    def _end_reply(
        self,
        gid: str,
        token: str,
        *,
        batch_ids: list[str] | None = None,
        consume: bool = True,
    ) -> None:
        if self._tokens.get(gid) != token:
            return
        self._tokens.pop(gid, None)
        self._busy_until.pop(gid, None)
        self._synthetic.pop(gid, None)
        if consume and batch_ids:
            ids = set(batch_ids)
            buf = self._buffers.get(gid)
            if buf is not None:
                kept = [m for m in buf if m.uid not in ids]
                self._buffers[gid] = deque(kept, maxlen=MAX_GROUP_BUFFER)
            self.store.mark_reply(gid)
            try:
                self.store.save()
            except OSError as exc:
                logger.warning("[activity_control] 保存回复时间失败: %s", exc)

    def _invalidate(self, gid: str) -> None:
        """模式切换时取消在途回复并清空上下文。"""
        self._tokens.pop(gid, None)
        self._busy_until.pop(gid, None)
        event = self._synthetic.pop(gid, None)
        if event is not None:
            try:
                event.stop_event()
            except Exception:
                pass
        self._buffers.pop(gid, None)
