"""影之刃零（PBZ）助手 —— 纯小黑盒版。

* ``pbz登录``  管理员扫码登录（小黑盒 App / 微信，两条渠道等价）
* ``pbz账号``  账号详情：昵称、小黑盒 ID、SteamID、影之刃零记录
* ``pbz战绩``  影之刃零个人数据（游玩时长、成就、在线）
* ``pbz退出`` / ``pbz状态``

数据全部来自小黑盒开放给你自己账号的接口，不依赖任何第三方。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as CompImage
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

from .heybox import (
    HeyboxClient,
    HeyboxError,
    HeyboxLoginRequired,
    HeyboxRateLimited,
    LoginState,
    create_login_client,
    find_steam_in_game_list,
    generate_device_id,
    pick_profile,
)

PLUGIN = "astrbot_plugin_pbz"
PBZ_APPID = "4115450"


@register(PLUGIN, "local", "影之刃零助手", "2.0.0")
class PbzPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = data_dir / "state.json"
        self._state = self._load_state()
        self._login_lock = asyncio.Lock()
        self._tpl = (Path(__file__).parent / "report.html").read_text(encoding="utf-8")

    # ==================== 配置 ====================

    def _number(self, key: str, default: float, low: float, high: float) -> float:
        try:
            return max(low, min(high, float(self.config.get(key, default))))
        except (TypeError, ValueError):
            return default

    @property
    def login_timeout(self) -> float:
        return self._number("login_timeout_seconds", 120.0, 30.0, 600.0)

    @property
    def allow_group_login(self) -> bool:
        return bool(self.config.get("allow_group_login", False))

    @property
    def login_mode(self) -> str:
        mode = str(self.config.get("login_mode", "heybox") or "heybox").strip().lower()
        return mode if mode in ("heybox", "wechat") else "heybox"

    @property
    def device_id_mode(self) -> str:
        mode = str(self.config.get("device_id_mode", "reuse") or "reuse").strip().lower()
        return mode if mode in ("reuse", "random", "off") else "reuse"

    def _resolve_device_id(self) -> str:
        if self.device_id_mode == "off":
            return ""
        existing = str(self._heybox.get("device_id") or "")
        if self.device_id_mode == "random" or not existing:
            return generate_device_id()
        return existing

    # ==================== 状态 ====================

    def _load_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_state(self) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._state_path)

    @property
    def _heybox(self) -> dict[str, Any]:
        value = self._state.get("heybox")
        return value if isinstance(value, dict) else {}

    @property
    def logged_in(self) -> bool:
        return bool(self._heybox.get("cookies"))

    def _client(self) -> HeyboxClient:
        return HeyboxClient(
            self._heybox.get("cookies") or {},
            str(self._heybox.get("device_id") or ""),
        )

    # ==================== 基础设施 ====================

    @staticmethod
    def _session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def _fetch_profile(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """账号详情 + Steam 绑定 + 影之刃零记录。"""
        client = self._client()
        profile = await client.profile(session)
        info = pick_profile(profile)
        uid = str(info.get("heybox_id") or self._heybox.get("uid") or "")

        try:
            accounts = await client.cloud_accounts(session)
            steam = accounts.get("cur_steam") or {}
            if steam.get("account_id"):
                info["steam_id"] = str(steam["account_id"])
                info["steam_name"] = str(steam.get("name") or "")
                info["steam_source"] = "cloud_archive"
        except HeyboxLoginRequired:
            raise
        except HeyboxError as exc:
            logger.info("[pbz] cloud accounts 读取失败：%s", exc)

        game: dict[str, Any] = {}
        try:
            rows = await client.game_list(session, uid) if uid else []
            for row in rows:
                appid = str(row.get("steam_appid") or row.get("appid") or "")
                if appid == PBZ_APPID:
                    game = row
                    break
        except HeyboxLoginRequired:
            raise
        except HeyboxError as exc:
            logger.info("[pbz] game_list 读取失败：%s", exc)

        info["has_pbz"] = bool(game)
        info["pbz"] = self._pbz_of(game) if game else {}
        logger.info(
            "[pbz] profile uid=%s steam=%s pbz=%s",
            uid,
            info.get("steam_id") or "-",
            bool(game),
        )
        return info

    async def _fetch_archive(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """Steam 账号 → 存档槽 → 档案概览。"""
        client = self._client()
        accounts = await client.cloud_accounts(session)
        steam = accounts.get("cur_steam") or {}
        archive_id = str(steam.get("account_id") or "")
        if not archive_id:
            raise HeyboxError("这个小黑盒账号还没绑定 Steam，无法读取存档")

        listing = await client.archive_list(session, archive_id)
        slot_id = str(listing.get("default_slot_id") or "")
        archive = str(listing.get("default_archive_id") or archive_id)
        if not slot_id:
            rows = listing.get("archives") or []
            if rows and isinstance(rows[0], dict):
                slot_id = str(rows[0].get("slot_id") or rows[0].get("id") or "")
        if not slot_id:
            raise HeyboxError("没有找到可用的存档槽，先在游戏里存一次档再试")

        overview = await client.archive_overview(session, archive, slot_id)
        report = overview.get("report") or {}
        if not report:
            raise HeyboxError("档案还没有解析完成，稍后再试")

        # overview 的 report.bosses 只给已击败的几条；
        # entries 才是完整的 BOSS 名单（含打了没打赢的），优先用它。
        try:
            entries = await client.archive_entries(session, archive, slot_id, "bosses", 100)
            rows = entries.get("list") or []
            if rows:
                report["bosses"] = rows
        except HeyboxError as exc:
            logger.info("[pbz] entries 读取失败，退回 overview 的 BOSS 列表：%s", exc)

        return {
            "steam": steam,
            "slots": listing.get("archives") or [],
            "report": report,
            "tasks": overview.get("task_overview") or {},
        }

    @staticmethod
    def _pbz_of(row: dict[str, Any]) -> dict[str, Any]:
        forever = int(row.get("playtime_forever") or 0)
        two_weeks = int(row.get("playtime_2weeks") or 0)
        total_ach = int(row.get("achivement_count") or 0)
        got_ach = int(row.get("achieved_count") or 0)
        return {
            "playtime_text": _duration(forever),
            "two_weeks_text": _duration(two_weeks),
            "achievements": f"{got_ach}/{total_ach}" if total_ach else "—",
            "achievement_percent": f"{got_ach / total_ach * 100:.0f}%" if total_ach else "—",
            "online": str(row.get("online_player") or ""),
            "cleared": bool(row.get("is_cleared")),
        }

    @staticmethod
    def _account_text(info: dict[str, Any]) -> str:
        lines = [
            f"昵称：{info.get('nickname') or '—'}",
            f"小黑盒ID：{info.get('heybox_id') or '—'}",
        ]
        if info.get("level"):
            lines.append(f"等级：{info['level']}")
        if info.get("steam_id"):
            name = info.get("steam_name") or ""
            suffix = f"（{name}）" if name else ""
            lines.append(f"SteamID：{info['steam_id']}{suffix}")
        else:
            lines.append("Steam：未读到（小黑盒账号还没绑定 Steam）")
        if info.get("has_pbz"):
            pbz = info.get("pbz") or {}
            lines.append(f"影之刃零：已入库，总时长 {pbz.get('playtime_text') or '—'}")
        else:
            lines.append("游戏记录：未在最近记录里看到《影之刃零》")
        return "\n".join(lines)

    @staticmethod
    def _err(exc: Exception) -> str:
        if isinstance(exc, HeyboxLoginRequired):
            return "小黑盒登录已失效，请管理员重新发送 pbz登录"
        if isinstance(exc, HeyboxRateLimited):
            return "小黑盒要求降低请求频率，请稍后再试"
        return str(exc) or type(exc).__name__

    # ==================== 登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pbz登录")
    async def login(self, event: AstrMessageEvent):
        if self._login_lock.locked():
            yield event.plain_result("已有登录流程在等待扫码，请先完成或等它超时。")
            return
        if not event.is_private_chat() and not self.allow_group_login:
            yield event.plain_result(
                "请私聊机器人发送 pbz登录。\n"
                "如需在群里扫码，请先到插件配置打开「允许在群里扫码登录」。"
            )
            return

        async with self._login_lock:
            device_id = self._resolve_device_id()
            mode = self.login_mode
            label = "微信" if mode == "wechat" else "小黑盒 App"
            client = create_login_client(mode, device_id)
            if client is None:
                yield event.plain_result(f"未知的登录渠道：{mode}")
                return
            try:
                qr = await client.request_qr()
                image = await client.qr_image(qr)
                tip = f"请用{label}扫码并确认，二维码约 2 分钟有效。"
                if mode != "wechat":
                    tip += "\n扫不出来可以到插件配置把「登录渠道」改成微信。"
                yield event.chain_result([Plain(tip), CompImage.fromBytes(image)])

                until = min(qr.expires_at, time.time() + self.login_timeout)
                scanned_notified = False
                while time.time() < until:
                    await asyncio.sleep(float(getattr(client, "poll_delay", 3.0) or 3.0))
                    result = await client.check_qr(qr)

                    if result.state is LoginState.SUCCESS:
                        if not result.cookies:
                            raise ValueError("扫码成功，但没有收到登录凭证")
                        self._state["heybox"] = {
                            "cookies": result.cookies,
                            "uid": result.uid,
                            "nickname": result.nickname,
                            "device_id": device_id,
                            "login_at": int(time.time()),
                        }
                        self._save_state()
                        who = result.nickname or result.uid or "未知账号"
                        text = f"小黑盒登录成功：{who}\n"
                        try:
                            async with self._session() as session:
                                info = await self._fetch_profile(session)
                            text += self._account_text(info) + "\n\n"
                            text += "发送 pbz战绩 查看你的影之刃零数据。"
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"[pbz] 登录后拉账号信息失败：{type(exc).__name__}")
                            text += "（账号详情读取失败，可稍后发送 pbz账号 重试）"
                        yield event.plain_result(text)
                        return

                    if result.state is LoginState.SCANNED_WAITING_CONFIRM and not scanned_notified:
                        scanned_notified = True
                        yield event.plain_result("已扫码，请在手机上点确认。")

                    if result.state in (LoginState.EXPIRED, LoginState.FAILED):
                        yield event.plain_result(
                            f"登录未完成：{result.message or '二维码已过期或失效'}，请重新发送 pbz登录"
                        )
                        return

                yield event.plain_result("二维码已过期，请重新发送 pbz登录")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[pbz] 登录失败：{type(exc).__name__}: {exc}")
                yield event.plain_result(f"登录失败：{exc}")

    @filter.command("pbz账号")
    async def account(self, event: AstrMessageEvent):
        if not self.logged_in:
            yield event.plain_result("还没有登录小黑盒。管理员发送 pbz登录 扫码即可。")
            return
        try:
            async with self._session() as session:
                info = await self._fetch_profile(session)
        except HeyboxLoginRequired:
            self._state.pop("heybox", None)
            self._save_state()
            yield event.plain_result("小黑盒登录已失效，请管理员重新发送 pbz登录。")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"读取账号失败：{self._err(exc)}")
            return
        yield event.plain_result(self._account_text(info))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("pbz退出")
    async def logout(self, event: AstrMessageEvent):
        if not self.logged_in:
            yield event.plain_result("当前没有登录小黑盒。")
            return
        self._state.pop("heybox", None)
        self._save_state()
        yield event.plain_result("已清除小黑盒登录信息。")

    @filter.command("pbz状态")
    async def status(self, event: AstrMessageEvent):
        if self.logged_in:
            hb = self._heybox
            yield event.plain_result(
                f"小黑盒：已登录（{hb.get('nickname') or hb.get('uid') or '未知'}）\n"
                f"登录渠道：{self.login_mode}"
            )
        else:
            yield event.plain_result("小黑盒：未登录")

    # ==================== 战绩 ====================

    @filter.command("pbz战绩")
    async def record(self, event: AstrMessageEvent):
        if not self.logged_in:
            yield event.plain_result("还没有登录小黑盒。管理员发送 pbz登录 扫码即可。")
            return
        try:
            async with self._session() as session:
                data = await self._fetch_archive(session)
        except HeyboxLoginRequired:
            self._state.pop("heybox", None)
            self._save_state()
            yield event.plain_result("小黑盒登录已失效，请管理员重新发送 pbz登录。")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"查询失败：{self._err(exc)}")
            return

        report = data.get("report") or {}
        steam = data.get("steam") or {}
        sender = str(event.get_sender_id() or "")
        avatar = f"https://q1.qlogo.cn/g?b=qq&nk={sender}&s=100" if sender.isdigit() else ""
        view = _build_view(
            report,
            steam,
            self._heybox.get("nickname") or "",
            avatar,
            data.get("tasks"),
        )
        try:
            url = await self.html_render(
                self._tpl,
                {"d": view},
                options={
                    "full_page": True,
                    "type": "png",
                    "device_scale_factor_level": "ultra",
                    "scale": "device",
                },
            )
            yield event.image_result(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[pbz] 渲染失败，退回文本：{type(exc).__name__}: {exc}")
            yield event.plain_result(_text_report(view))

    async def terminate(self):
        return


def _duration(seconds: int) -> str:
    if seconds <= 0:
        return "0 分钟"
    hours, remain = divmod(seconds, 3600)
    minutes = remain // 60
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分钟"


_DIFFICULTY = {"1": "入阵者", "2": "破局者", "3": "逆天者"}
_DIFFICULTY_ALT = {"0": "初入影境", "4": "六十六天"}


def _pct(done: Any, total: Any) -> int:
    try:
        d, t = int(done), int(total)
    except (TypeError, ValueError):
        return 0
    return round(d / t * 100) if t > 0 else 0


def _build_view(
    report: dict[str, Any],
    steam: dict[str, Any],
    nickname: str,
    avatar: str = "",
    tasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把小黑盒 archive/overview 的 report 压成模板需要的结构。"""
    raw_diff = str(report.get("difficulty") or "")
    difficulty = _DIFFICULTY.get(raw_diff) or _DIFFICULTY_ALT.get(raw_diff) or (f"难度{raw_diff}" if raw_diff else "—")
    try:
        cycle_no = int(report.get("cycle") or 0) + 1
    except (TypeError, ValueError):
        cycle_no = 1

    collections = []
    for row in report.get("collections") or []:
        if not isinstance(row, dict):
            continue
        collections.append({
            "name": str(row.get("name") or ""),
            "done": row.get("done") or 0,
            "total": row.get("total") or 0,
            "percent": _pct(row.get("done"), row.get("total")),
        })

    bosses = []
    for row in report.get("bosses") or []:
        if not isinstance(row, dict):
            continue
        attempt = int(row.get("attempt") or 0)
        killed = bool(row.get("killed"))
        # 打过（attempt > 0）就要展示，包括没通过的
        if not killed and attempt <= 0:
            continue
        enc = row.get("encyclopedia") or {}
        bosses.append({
            "name": str(row.get("name") or ""),
            "icon": str(enc.get("icon") or ""),
            "killed": killed,
            "kill": int(row.get("kill") or 0),
            "attempt": attempt,
            # 小黑盒徽章显示的是「死在这个 BOSS 手上的次数」
            "deaths": max(0, attempt - int(row.get("kill") or 0)),
            "best": _fmt_ms(row.get("best_time_ms")),
            "map": str(row.get("map_name") or ""),
        })
    bosses.sort(key=lambda b: (b["killed"], -b["deaths"], -b["attempt"]))

    maps = []
    for row in report.get("maps") or []:
        if not isinstance(row, dict):
            continue
        total = row.get("collect_total") or 0
        if not total:
            continue
        maps.append({
            "name": str(row.get("name") or ""),
            "done": row.get("collect_done") or 0,
            "total": total,
            "percent": _pct(row.get("collect_done"), total),
        })
    maps.sort(key=lambda m: m["percent"], reverse=True)

    task_info = (tasks or {}).get("summary") or {}
    kill_total = int(report.get("boss_killed_count") or len([b for b in bosses if b["killed"]]))
    boss_total = int(report.get("boss_total") or len(report.get("bosses") or []))

    # ---- 当前装备：按武器 / 影之武装 / 饰品 / 药瓶 分组 ----
    group_names = {
        "weapon": "武器",
        "shaqi": "影之武装",
        "accessory": "饰品",
        "medicine": "药瓶",
    }
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in group_names}
    for row in report.get("equipment") or []:
        if not isinstance(row, dict):
            continue
        cat = str(row.get("category") or "")
        if cat not in groups:
            continue
        enc = row.get("encyclopedia") or {}
        empty = bool(row.get("empty")) or str(row.get("name") or "") == "空槽"
        groups[cat].append({
            "name": "" if empty else str(row.get("name") or ""),
            "icon": "" if empty else str(enc.get("icon") or ""),
            "level": row.get("level"),
            "empty": empty,
        })
    equipment_groups = [
        {"name": group_names[k], "items": v} for k, v in groups.items() if v
    ]

    # ---- 任务 ----
    quests = []
    for row in (tasks or {}).get("list") or []:
        if not isinstance(row, dict):
            continue
        quests.append({
            "title": str(row.get("title") or ""),
            "subtitle": str(row.get("subtitle") or ""),
            "kind": str(row.get("kind_name") or ""),
            "icon": str(row.get("icon_url") or row.get("image_url") or ""),
            "status": str(row.get("status") or ""),
        })

    # ---- 地图探索 ----
    map_rows = []
    explore_done = explore_total = 0
    for row in report.get("maps") or []:
        if not isinstance(row, dict):
            continue
        total = int(row.get("collect_total") or 0)
        done = int(row.get("collect_done") or 0)
        if not total:
            continue
        explore_done += done
        explore_total += total
        map_rows.append({
            "name": str(row.get("name") or ""),
            "percent": _pct(done, total),
            "total": total,
            "done": done,
        })

    return {
        "nickname": str(steam.get("name") or nickname or "影境行者"),
        "avatar": avatar,
        "steam_id": str(report.get("archive_id") or steam.get("account_id") or ""),
        "friend_code": str(steam.get("friend_code") or ""),
        "slot": str(report.get("slot_label") or report.get("slot_id") or ""),
        "save_time": str(report.get("save_time") or ""),
        "area": str(report.get("area") or "—"),
        "difficulty": difficulty,
        "difficulty_raw": raw_diff,
        "cycle": f"{cycle_no} 周目",
        "remaining_days": report.get("remaining_days"),
        "play_time_h": f"{int(report.get('play_time_seconds') or 0) / 3600:.1f}h",
        "play_time": _duration(int(report.get("play_time_seconds") or 0)),
        "deaths": report.get("total_deaths") or 0,
        "cycle_deaths": report.get("cycle_deaths") or 0,
        "boss_killed": kill_total,
        "boss_total": boss_total,
        "collect_done": report.get("collect_done") or 0,
        "collect_total": report.get("collect_total") or 0,
        "achievement_done": report.get("achievement_done") or 0,
        "achievement_total": report.get("achievement_total") or 0,
        "collections": collections,
        "bosses": bosses,
        "maps": maps,
        "equipment_groups": equipment_groups,
        "quests": quests,
        "map_rows": map_rows,
        "explore_done": explore_done,
        "explore_total": explore_total,
        "task_total": task_info.get("total") or 0,
        "task_done": task_info.get("succeeded") or 0,
        "task_progress": task_info.get("started") or 0,
        "task_none": task_info.get("not_recorded") or 0,
        "updated_at": _fmt_time(report.get("updated_at")),
    }


def _fmt_time(value: Any) -> str:
    """上游有时给秒级时间戳，有时给已格式化的字符串。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit() and len(raw) >= 10:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(raw)))
        except (ValueError, OSError):
            return raw
    return raw


def _fmt_ms(value: Any) -> str:
    """毫秒 → ``分:秒.百分秒``，例如 0:46.32。"""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    seconds, msec = divmod(ms, 1000)
    return f"{seconds // 60}:{seconds % 60:02d}.{msec // 10:02d}"


def _text_report(view: dict[str, Any]) -> str:
    lines = [
        f"影之刃零 · {view['nickname']}",
        f"SteamID {view['steam_id']} · {view['cycle']} · {view['difficulty']}",
        f"当前 {view['area']}｜剩余 {view['remaining_days']} 天｜{view['play_time']}",
        "",
        f"身陨 {view['deaths']} 次｜BOSS {view['boss_killed']}/{view['boss_total']}｜"
        f"收集 {view['collect_done']}/{view['collect_total']}（{view['collect_percent']}%）",
    ]
    for c in view["collections"]:
        lines.append(f"{c['name']} {c['done']}/{c['total']}（{c['percent']}%）")
    if view["bosses"]:
        lines.append("")
        lines.append("已击败 BOSS：")
        for b in view["bosses"][:10]:
            extra = f"，最佳 {b['best']}" if b["best"] else ""
            lines.append(f"· {b['name']} ×{b['kill']}{extra}")
    return "\n".join(lines)
