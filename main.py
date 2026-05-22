from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

from .tools import build_llm_tools


BASE_URL = "https://sdxt.hainanu.edu.cn/scanQRWaterCtrl_redis_hndx1/service"
APP_ID = "sz@cgdz#2021$11"
APP_SECRET = "&szcgdz"
DEFAULT_TIMEOUT = 10.0

TEMPLATE_KEY_FIELD = "__template_key"
LEGACY_TEMPLATE_KEY_FIELD = "template"
DEFAULT_QUERY_DISPLAY_ITEMS = {
    "姓名",
    "学校",
    "楼栋",
    "房间",
    "热水余额",
    "照明",
    "空调",
    "水表",
    "检测提示",
}


@dataclass(frozen=True)
class Reminder:
    index: int
    fee_type: str
    fee_key: str
    threshold: float
    session_umo: str


class HNUUtilityBalancePlugin(Star):
    """海南大学水电费余额查询与低余额提醒。"""

    LLM_TOOL_NAMES = {"query_hnu_utility_balance"}

    FEE_TYPE_ALIASES = {
        "1": "light",
        "light": "light",
        "lighting": "light",
        "照明": "light",
        "插座": "light",
        "2": "ac",
        "ac": "ac",
        "air_conditioner": "ac",
        "空调": "ac",
        "water": "water",
        "水": "water",
        "水表": "water",
        "hot_water": "hot_water",
        "hotwater": "hot_water",
        "热水": "hot_water",
        "账户": "hot_water",
    }
    FEE_TYPE_LABELS = {
        "light": "照明",
        "ac": "空调",
        "water": "水表",
        "hot_water": "热水",
    }

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._monitor_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._schedule_event = asyncio.Event()
        self._next_auto_check_at: float | None = None
        self._active_alert_keys: set[str] = set()
        self._missing_openid_warned = False
        self._invalid_reminders_warned = False
        self._register_llm_tools()

    @filter.command("水电查询")
    async def query_balance(self, event: AstrMessageEvent):
        """从接口获取最新水电费余额，并刷新下一次自动检测时间。"""

        openid = self._extract_command_arg(event.message_str) or self._get_openid()
        if not openid:
            yield event.plain_result(
                "未配置 openid。请在插件配置中填写 openid，或使用：水电查询 <openid>"
            )
            return

        result, err = await self._query(openid)
        if err:
            yield event.plain_result(f"查询失败：{err}")
            return

        self._update_next_auto_check_time(notify=True)
        yield event.plain_result(self._format_query_reply(result))

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        self._register_llm_tools()
        self._ensure_monitor_task()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        if getattr(metadata, "module_path", None) == self.__module__:
            self._register_llm_tools()
            self._ensure_monitor_task()

    def _unregister_llm_tools(self) -> None:
        """移除本插件注册的函数工具，避免重复注册。"""

        tool_mgr = self.context.get_llm_tool_manager()
        tool_mgr.func_list = [
            tool
            for tool in tool_mgr.func_list
            if not (
                tool.name in self.LLM_TOOL_NAMES
                and getattr(tool, "handler_module_path", None) == self.__module__
            )
        ]

    def _register_llm_tools(self) -> None:
        """按 im_profile 插件风格注册函数工具。"""

        self._unregister_llm_tools()
        tools = build_llm_tools(self)
        if tools:
            self.context.add_llm_tools(*tools)
        logger.info(
            "[HNUUtilityBalance] 函数工具已注册：%s",
            sorted(tool.name for tool in tools),
        )

    async def query_hnu_utility_balance(self) -> str:
        """查询配置 openid 对应的水电费余额，供 LLM 工具调用。"""

        openid = self._get_openid()
        if not openid:
            return "未配置 openid，无法查询水电费余额。"

        result, err = await self._query(openid)
        if err:
            return f"查询水电费余额失败：{err}"

        self._update_next_auto_check_time(notify=True)
        return self._format_query_reply(result)

    def _ensure_monitor_task(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        if not self._auto_check_enabled():
            logger.info("[HNUUtilityBalance] 自动检测未启用。")
            return
        self._stop_event.clear()
        self._update_next_auto_check_time(notify=False)
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "[HNUUtilityBalance] 自动检测已启动，间隔 %s 分钟。",
            self._get_check_interval_minutes(),
        )

    async def _monitor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._next_auto_check_at is None:
                    self._update_next_auto_check_time(notify=False)
                    continue

                wait_seconds = self._next_auto_check_at - now
                if wait_seconds > 0:
                    await self._wait_for_schedule_change(wait_seconds)
                    continue

                await self._run_reminder_check(manual=False)
                self._update_next_auto_check_time(notify=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[HNUUtilityBalance] 自动检测任务异常退出。")

    async def _wait_for_schedule_change(self, timeout: float) -> None:
        wait_tasks = {
            asyncio.create_task(self._stop_event.wait()),
            asyncio.create_task(self._schedule_event.wait()),
        }
        try:
            done, pending = await asyncio.wait(
                wait_tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done and self._schedule_event.is_set():
                self._schedule_event.clear()
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()

    def _update_next_auto_check_time(self, *, notify: bool) -> None:
        self._next_auto_check_at = time.monotonic() + max(
            60,
            self._get_check_interval_minutes() * 60,
        )
        if notify:
            self._schedule_event.set()

    async def _run_reminder_check(
        self,
        *,
        manual: bool,
        preloaded_result: dict[str, Any] | None = None,
    ) -> str:
        openid = self._get_openid()
        if not openid:
            if not self._missing_openid_warned or manual:
                logger.warning("[HNUUtilityBalance] 未配置 openid，跳过检测。")
                self._missing_openid_warned = True
            return "未配置 openid，已跳过检测。"

        reminders = self._load_reminders()
        if not reminders:
            if not self._invalid_reminders_warned or manual:
                logger.warning("[HNUUtilityBalance] 未配置有效提醒，跳过检测。")
                self._invalid_reminders_warned = True
            return "未配置有效提醒，已跳过检测。"

        if preloaded_result is None:
            result, err = await self._query(openid)
            if err:
                logger.warning("[HNUUtilityBalance] 检测查询失败：%s", err)
                return f"检测查询失败：{err}"
        else:
            result = preloaded_result

        sent = 0
        triggered = 0
        skipped = 0
        for reminder in reminders:
            amount = self._get_fee_amount(result, reminder.fee_key)
            if amount is None:
                skipped += 1
                logger.warning(
                    "[HNUUtilityBalance] 无法读取费用类型 %s 的余额。",
                    reminder.fee_type,
                )
                continue

            alert_key = self._build_alert_key(reminder)
            if amount <= reminder.threshold:
                triggered += 1
                if alert_key in self._active_alert_keys and not manual:
                    continue

                message = self._format_alert_message(result, reminder, amount)
                if await self._send_to_umo(reminder.session_umo, message):
                    sent += 1
                    self._active_alert_keys.add(alert_key)
            else:
                self._active_alert_keys.discard(alert_key)

        if manual:
            return (
                f"检测完成：有效提醒 {len(reminders)} 个，触发 {triggered} 个，"
                f"已发送 {sent} 条，跳过 {skipped} 个。"
            )
        return ""

    async def _send_to_umo(self, umo: str, text: str) -> bool:
        try:
            return await self.context.send_message(
                umo,
                MessageChain([Plain(text)]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HNUUtilityBalance] 发送提醒到 UMO %s 失败：%s",
                umo,
                exc,
            )
            return False

    async def _query(self, openid: str) -> tuple[dict[str, Any], str | None]:
        try:
            async with httpx.AsyncClient(
                verify=False,
                timeout=DEFAULT_TIMEOUT,
            ) as client:
                return await self._query_with_client(client, openid)
        except httpx.HTTPError as exc:
            return {}, f"网络错误：{exc}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[HNUUtilityBalance] 查询异常。")
            return {}, f"查询异常：{exc}"

    async def _query_with_client(
        self,
        client: httpx.AsyncClient,
        openid: str,
    ) -> tuple[dict[str, Any], str | None]:
        timestamp = int(time.time() * 1000)
        user_data = await self._get_json(
            client,
            "/applet/getWxUser",
            {
                "openId": openid,
                "appid": APP_ID,
                "timestamp": timestamp,
                "sign": self._make_sign(timestamp),
            },
        )
        if user_data.get("statusCode") != "200":
            return {}, user_data.get(
                "message"
            ) or "用户信息查询失败，请确认 openid 有效。"

        user = (user_data.get("resultObject") or {}).get("user") or {}
        result: dict[str, Any] = {
            "name": user.get("realName") or user.get("nickName") or "",
            "school": user.get("schoolName") or "",
            "building": user.get("louDongName") or "",
            "room": user.get("roomName") or "",
            "balance": self._safe_float(user.get("amount"), default=0.0),
        }

        await self._fill_electric_info(client, openid, result, "1", "light")
        await self._fill_electric_info(client, openid, result, "2", "ac")
        await self._fill_water_info(client, openid, result)
        return result, None

    async def _fill_electric_info(
        self,
        client: httpx.AsyncClient,
        openid: str,
        result: dict[str, Any],
        ele_type: str,
        result_key: str,
    ) -> None:
        try:
            data = await self._get_json(
                client,
                "/weixinEle/getEleInfo",
                {"openId": openid, "type": ele_type},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[HNUUtilityBalance] 查询电费明细 type=%s 失败：%s",
                ele_type,
                exc,
            )
            return

        if data.get("statusCode") != "200":
            logger.warning(
                "[HNUUtilityBalance] 查询电费明细 type=%s 返回失败：%s",
                ele_type,
                data.get("message") or data,
            )
            return

        obj = data.get("resultObject") or {}
        result[result_key] = {
            "kwh": obj.get("leftEle", "0"),
            "money": obj.get("leftMoney", "0"),
            "time": obj.get("monTime", ""),
        }

    async def _fill_water_info(
        self,
        client: httpx.AsyncClient,
        openid: str,
        result: dict[str, Any],
    ) -> None:
        try:
            data = await self._get_json(
                client,
                "/weixinEle/getWaterInfo",
                {"openId": openid},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HNUUtilityBalance] 查询水表失败：%s", exc)
            return

        if data.get("statusCode") != "200":
            return

        obj = data.get("resultObject") or {}
        result["water"] = {
            "tons": obj.get("leftWater", "0"),
            "money": obj.get("leftMoney", "0"),
            "time": obj.get("monTime", ""),
        }

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.get(f"{BASE_URL}{path}", params=params)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data
        return {"statusCode": "500", "message": "接口返回格式异常"}

    def _load_reminders(self) -> list[Reminder]:
        raw_reminders = self.config.get("reminders", [])
        if not isinstance(raw_reminders, list):
            return []

        reminders: list[Reminder] = []
        for index, item in enumerate(raw_reminders):
            if not isinstance(item, dict):
                continue

            template_key = str(
                item.get(TEMPLATE_KEY_FIELD)
                or item.get(LEGACY_TEMPLATE_KEY_FIELD)
                or ""
            ).strip()
            if template_key and template_key != "balance_reminder":
                continue

            fee_type = str(item.get("fee_type") or "").strip()
            session_umo = str(item.get("session_umo") or "").strip()
            threshold = self._safe_float(item.get("threshold"), default=None)
            fee_key = self._normalize_fee_type(fee_type)

            if not fee_key or threshold is None or not session_umo:
                logger.warning(
                    "[HNUUtilityBalance] 第 %s 个提醒配置无效：fee_type=%r, threshold=%r, session_umo=%r",
                    index + 1,
                    fee_type,
                    item.get("threshold"),
                    session_umo,
                )
                continue

            reminders.append(
                Reminder(
                    index=index,
                    fee_type=fee_type,
                    fee_key=fee_key,
                    threshold=threshold,
                    session_umo=session_umo,
                )
            )
        return reminders

    def _format_query_reply(self, result: dict[str, Any]) -> str:
        lines = self._format_query_result(result)
        if self._should_display_query_item("检测提示"):
            lines.append(
                f"已从接口获取最新数据；下次自动检测将在 {self._get_check_interval_minutes()} 分钟后执行。"
            )
        return "\n".join(lines) if lines else "已从接口获取最新数据。"

    def _format_query_result(self, result: dict[str, Any]) -> list[str]:
        lines = []
        if self._should_display_query_item("姓名"):
            lines.append(f"姓名：{result.get('name') or 'N/A'}")
        if self._should_display_query_item("学校"):
            lines.append(f"学校：{result.get('school') or 'N/A'}")
        if self._should_display_query_item("楼栋"):
            lines.append(f"楼栋：{result.get('building') or '未绑定'}")
        if self._should_display_query_item("房间"):
            lines.append(f"房间：{result.get('room') or '未绑定'}")
        if self._should_display_query_item("热水余额"):
            lines.append(f"热水余额：{self._fmt(result.get('balance', 0))} 元")

        if self._should_display_query_item("照明") and result.get("light"):
            light = result["light"]
            lines.append(
                "照明："
                f"{self._fmt(light.get('kwh'))} 度 / "
                f"{self._fmt(light.get('money'))} 元"
                f" [{light.get('time') or 'N/A'}]"
            )
        if self._should_display_query_item("空调") and result.get("ac"):
            ac = result["ac"]
            lines.append(
                "空调："
                f"{self._fmt(ac.get('kwh'))} 度 / "
                f"{self._fmt(ac.get('money'))} 元"
                f" [{ac.get('time') or 'N/A'}]"
            )
        if self._should_display_query_item("水表") and result.get("water"):
            water = result["water"]
            lines.append(
                "水表："
                f"{self._fmt(water.get('tons'))} 吨 / "
                f"{self._fmt(water.get('money'))} 元"
                f" [{water.get('time') or 'N/A'}]"
            )

        return lines

    def _should_display_query_item(self, item_name: str) -> bool:
        raw_items = self.config.get("query_display_items", DEFAULT_QUERY_DISPLAY_ITEMS)
        if not isinstance(raw_items, list):
            return item_name in DEFAULT_QUERY_DISPLAY_ITEMS
        return item_name in {str(item).strip() for item in raw_items}

    def _format_alert_message(
        self,
        result: dict[str, Any],
        reminder: Reminder,
        amount: float,
    ) -> str:
        label = self.FEE_TYPE_LABELS.get(reminder.fee_key, reminder.fee_type)
        location = " ".join(
            part
            for part in [
                str(result.get("building") or ""),
                str(result.get("room") or ""),
            ]
            if part
        )
        location_line = f"位置：{location}\n" if location else ""
        return (
            "海南大学水电费余额提醒\n"
            f"{location_line}"
            f"费用种类：{label}\n"
            f"当前余额：{self._fmt(amount)} 元\n"
            f"提醒阈值：{self._fmt(reminder.threshold)} 元\n"
            "请及时充值。"
        )

    def _get_fee_amount(self, result: dict[str, Any], fee_key: str) -> float | None:
        if fee_key == "light":
            return self._safe_float((result.get("light") or {}).get("money"))
        if fee_key == "ac":
            return self._safe_float((result.get("ac") or {}).get("money"))
        if fee_key == "water":
            return self._safe_float((result.get("water") or {}).get("money"))
        if fee_key == "hot_water":
            return self._safe_float(result.get("balance"), default=0.0)
        return None

    def _normalize_fee_type(self, fee_type: str) -> str:
        normalized = fee_type.strip().lower().replace(" ", "_").replace("-", "_")
        return self.FEE_TYPE_ALIASES.get(fee_type.strip()) or self.FEE_TYPE_ALIASES.get(
            normalized,
            "",
        )

    def _build_alert_key(self, reminder: Reminder) -> str:
        return (
            f"{reminder.index}:{reminder.session_umo}:"
            f"{reminder.fee_key}:{reminder.threshold}"
        )

    def _auto_check_enabled(self) -> bool:
        return bool(self.config.get("enable_auto_check", True))

    def _get_openid(self) -> str:
        return str(self.config.get("openid", "") or "").strip()

    def _get_check_interval_minutes(self) -> int:
        raw = self.config.get("check_interval_minutes", 60)
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = 60
        return max(1, interval)

    @staticmethod
    def _make_sign(timestamp: int) -> str:
        raw = f"{APP_ID}{timestamp}{APP_SECRET}"
        return hashlib.md5(raw.encode()).hexdigest()

    @staticmethod
    def _safe_float(value: Any, default: float | None = None) -> float | None:
        if value is None or value == "":
            return default
        try:
            return float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt(value: Any) -> str:
        number = HNUUtilityBalancePlugin._safe_float(value, default=None)
        if number is None:
            return str(value)
        if number == int(number):
            return str(int(number))
        return f"{number:.2f}"

    @staticmethod
    def _extract_command_arg(message_str: str) -> str:
        parts = (message_str or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            return ""
        return parts[1].strip()

    async def terminate(self):
        self._stop_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
