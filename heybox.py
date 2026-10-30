"""小黑盒客户端：请求签名 + App 扫码登录 + 登录态查询。

签名与登录流程参考 `as124da1231/astrbot_plugin_naraka`（MIT），
接口契约与实测结论见同仓库文档。

实测（2026-10）：

* ``hkey`` 只取决于（路径, 秒级时间戳, nonce），与 query、cookie 无关。
* ``/account/*`` 是 GET-only，会校验 ``_time`` / ``hkey`` / ``nonce`` / ``os_type`` / ``version`` / ``x_app``。
* 未登录访问 ``/account/restore_login`` 返回 ``status=login``；
  其余 ``/account/user_info``、``/account/steam*`` 等猜测路径全部 404，
  账号与 Steam 绑定信息只能从 ``restore_login`` 的返回里取。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit

import aiohttp
import qrcode

API_BASE_URL = "https://api.xiaoheihe.cn"
PBZ_APPID = "4115450"

WEB_CLIENT_PARAMS = {
    "os_type": "web",
    "app": "web",
    "client_type": "web",
    "version": "999.0.4",
    "web_version": "2.5",
    "x_client_type": "web",
    "x_app": "heybox_website",
    "x_os_type": "Windows",
    "device_info": "Chrome",
    "_notip": "true",
}

HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.xiaoheihe.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_HKEY_ALPHABET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


# ==================== 异常 ====================


class HeyboxError(Exception):
    """接口返回了业务错误。"""


class HeyboxLoginRequired(HeyboxError):
    """登录态缺失或失效。"""


class HeyboxRateLimited(HeyboxError):
    """被上游限流。"""


# ==================== 签名 ====================


def _xtime(v: int) -> int:
    return (255 & ((v << 1) ^ 27)) if v & 128 else v << 1


def _mul3(v: int) -> int:
    return _xtime(v) ^ v


def _mul4(v: int) -> int:
    return _mul3(_xtime(v))


def _mul8(v: int) -> int:
    return _mul4(_mul3(_xtime(v)))


def _mul14(v: int) -> int:
    return _mul8(v) ^ _mul4(v) ^ _mul3(v)


def _mix_tail(values: list[int]) -> list[int]:
    a, b, c, d = values[:4]
    return [
        _mul14(a) ^ _mul8(b) ^ _mul4(c) ^ _mul3(d),
        _mul3(a) ^ _mul14(b) ^ _mul8(c) ^ _mul4(d),
        _mul4(a) ^ _mul3(b) ^ _mul14(c) ^ _mul8(d),
        _mul8(a) ^ _mul4(b) ^ _mul3(c) ^ _mul14(d),
        *values[4:],
    ]


def _map_to_alphabet(value: str, alphabet: str) -> str:
    return "".join(alphabet[ord(c) % len(alphabet)] for c in value)


def generate_hkey(path: str, timestamp: int, nonce: str) -> str:
    normalized_path = f"/{'/'.join(p for p in str(path).split('/') if p)}/"
    parts = (
        _map_to_alphabet(str(timestamp), _HKEY_ALPHABET[:-2]),
        _map_to_alphabet(normalized_path, _HKEY_ALPHABET),
        _map_to_alphabet(str(nonce), _HKEY_ALPHABET),
    )
    interleaved = "".join(
        part[i]
        for i in range(max(len(p) for p in parts))
        for part in parts
        if i < len(part)
    )[:20]
    digest = hashlib.md5(interleaved.encode()).hexdigest()
    mixed = _mix_tail([ord(c) for c in digest[-6:]])
    suffix = str(sum(mixed) % 100).zfill(2)
    prefix = _map_to_alphabet(digest[:5], _HKEY_ALPHABET[:-4])
    return f"{prefix}{suffix}"


def sign_params(path: str, base: dict[str, str] | None = None, device_id: str = "") -> dict[str, str]:
    params = dict(base or {})
    ts = int(time.time())
    nonce = hashlib.md5(f"{ts}{secrets.token_hex(16)}".encode()).hexdigest().upper()
    params["_time"] = str(ts)
    params["nonce"] = nonce
    params["hkey"] = generate_hkey(path, ts, nonce)
    if device_id:
        params["device_id"] = device_id
    return params


def generate_device_id() -> str:
    return secrets.token_hex(16)


# ==================== 扫码登录 ====================


class LoginState(str, Enum):
    WAITING_SCAN = "waiting_scan"
    SCANNED_WAITING_CONFIRM = "scanned_waiting_confirm"
    SUCCESS = "success"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class QRSession:
    qr_content: str
    poll_params: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0


@dataclass
class LoginResult:
    state: LoginState
    message: str
    cookies: dict[str, str] = field(default_factory=dict)
    uid: str = ""
    nickname: str = ""


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("result", payload.get("data", payload))
    return candidate if isinstance(candidate, dict) else {}


def parse_qr_response(payload: dict[str, Any]) -> QRSession:
    body = _data(payload)
    qr_content = str(
        body.get("qrcode") or body.get("qr_url") or body.get("url") or body.get("qr_content") or ""
    )
    if not qr_content:
        raise ValueError("二维码响应缺少 qrcode 字段")
    poll_params = {
        str(k): str(v)
        for k, v in parse_qsl(urlsplit(qr_content).query, keep_blank_values=True)
    }
    started = time.time()
    raw_expiry = body.get("expires_in", body.get("ttl", body.get("expire", 180)))
    try:
        expiry = float(raw_expiry)
    except (TypeError, ValueError):
        expiry = 180
    if expiry > 10_000_000_000:
        expiry /= 1000
    ttl = expiry - started if expiry > 1_000_000_000 else expiry
    return QRSession(
        qr_content=qr_content,
        poll_params=poll_params,
        expires_at=started + max(10, min(ttl, 600)),
    )


def parse_login_state(payload: dict[str, Any]) -> tuple[LoginState, str]:
    body = _data(payload)
    message = str(
        body.get("message") or body.get("msg") or body.get("error_msg") or body.get("err_msg") or ""
    )
    marker = str(body.get("error") or body.get("err") or "").strip().lower()
    raw_state = str(body.get("state") or body.get("status") or body.get("qr_state") or "").strip().lower()

    state_map = {
        "0": LoginState.WAITING_SCAN,
        "waiting": LoginState.WAITING_SCAN,
        "waiting_scan": LoginState.WAITING_SCAN,
        "1": LoginState.SCANNED_WAITING_CONFIRM,
        "scanned": LoginState.SCANNED_WAITING_CONFIRM,
        "confirm": LoginState.SCANNED_WAITING_CONFIRM,
        "2": LoginState.SUCCESS,
        "success": LoginState.SUCCESS,
        "confirmed": LoginState.SUCCESS,
        "3": LoginState.EXPIRED,
        "expired": LoginState.EXPIRED,
        "-1": LoginState.FAILED,
        "failed": LoginState.FAILED,
    }
    if raw_state in state_map:
        return state_map[raw_state], message
    if marker in {"ok", "success", "confirmed"}:
        return LoginState.SUCCESS, message
    if marker in {"ready", "scanned"}:
        return LoginState.SCANNED_WAITING_CONFIRM, message
    if marker in {"wait", "waiting"}:
        return LoginState.WAITING_SCAN, message

    hint = f"{marker} {message}".casefold()
    if any(t in hint for t in ("expired", "timeout", "过期", "失效", "超时")):
        return LoginState.EXPIRED, message
    if any(t in hint for t in ("scanned", "confirm", "已扫码", "已扫描", "待确认", "请确认", "确认")):
        return LoginState.SCANNED_WAITING_CONFIRM, message
    return LoginState.WAITING_SCAN, message


def parse_login_credentials(
    payload: dict[str, Any], response_cookies: dict[str, str]
) -> tuple[str, str, dict[str, str]]:
    body = _data(payload)
    containers: list[dict[str, Any]] = [body]
    for key in ("user", "account", "profile", "account_detail"):
        candidate = body.get(key)
        if isinstance(candidate, dict):
            containers.append(candidate)

    def _pick(*keys: str) -> str:
        for container in containers:
            for key in keys:
                value = container.get(key)
                if value is not None and str(value) != "":
                    return str(value)
        return ""

    uid = _pick("uid", "heybox_id", "user_heybox_id", "heyboxid", "user_id", "userid", "id")
    nickname = _pick("nickname", "username", "name")

    cookies = {str(k): str(v) for k, v in response_cookies.items()}
    embedded = body.get("cookies")
    if isinstance(embedded, dict):
        cookies.update({str(k): str(v) for k, v in embedded.items()})

    for cookie_name, aliases in [
        ("pkey", ("pkey", "user_pkey", "key")),
        ("heybox_id", ("heybox_id", "user_heybox_id", "heyboxid", "user_id", "userid")),
        ("x_xhh_tokenid", ("x_xhh_tokenid",)),
    ]:
        if not cookies.get(cookie_name):
            value = _pick(*aliases)
            if value:
                cookies[cookie_name] = value
    return uid, nickname, cookies


def generate_qr_png(content: str, box_size: int = 7, border: int = 4) -> bytes:
    """生成聊天气泡里大小合适的二维码。

    ``border`` 必须 >= 4（QR 标准静默区），白边不够时小黑盒这类
    识别较弱的扫码器会直接失败，微信反而能扫。
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class HeyboxLoginClient:
    """小黑盒 App 扫码登录。"""

    poll_delay = 3.0

    def __init__(self, device_id: str = ""):
        self.device_id = device_id

    async def request_qr(self) -> QRSession:
        params = sign_params("/account/get_qrcode_url/", dict(WEB_CLIENT_PARAMS), self.device_id)
        jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            async with session.get(
                f"{API_BASE_URL}/account/get_qrcode_url/", params=params, headers=HEADERS
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise ValueError("二维码响应格式无效")
        return parse_qr_response(payload)

    async def qr_image(self, qr_session: QRSession) -> bytes:
        return generate_qr_png(qr_session.qr_content)

    async def check_qr(self, qr_session: QRSession) -> LoginResult:
        params = sign_params(
            "/account/qr_state/",
            {**WEB_CLIENT_PARAMS, **qr_session.poll_params},
            self.device_id,
        )
        jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=20)
        cookies: dict[str, str] = {}
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            async with session.get(
                f"{API_BASE_URL}/account/qr_state/", params=params, headers=HEADERS
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
            for cookie in session.cookie_jar:
                cookies[str(cookie.key)] = str(cookie.value)

        if not isinstance(payload, dict):
            raise ValueError("登录状态响应格式无效")

        state, message = parse_login_state(payload)
        uid = ""
        nickname = ""
        if state is LoginState.SUCCESS:
            uid, nickname, cookies = parse_login_credentials(payload, cookies)
        return LoginResult(state=state, message=message, cookies=cookies, uid=uid, nickname=nickname)


# ==================== 登录态客户端 ====================


class HeyboxClient:
    """带登录态的只读客户端。"""

    APP_UA = (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/41.0.2272.118 Safari/537.36 ApiMaxJia/1.0"
    )

    def __init__(self, cookies: dict[str, str] | None = None, device_id: str = "", timeout: float = 25.0):
        self.cookies = dict(cookies or {})
        self.device_id = device_id
        self.timeout = timeout

    async def call(
        self,
        session: aiohttp.ClientSession,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        with_cookies: bool = True,
        app_ua: bool = False,
    ) -> dict[str, Any]:
        query: dict[str, Any] = dict(WEB_CLIENT_PARAMS)
        for key, value in (params or {}).items():
            if value is not None and str(value) != "":
                query[key] = value
        query = sign_params(path, query, self.device_id)

        headers = dict(HEADERS)
        if app_ua:
            headers["User-Agent"] = self.APP_UA
            headers["Referer"] = "https://web.xiaoheihe.cn/"

        try:
            async with session.get(
                f"{API_BASE_URL}{path}",
                params=query,
                headers=headers,
                cookies=(self.cookies or None) if with_cookies else None,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                text = await resp.text()
                status_code = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HeyboxError(f"网络请求失败：{type(exc).__name__}") from exc

        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise HeyboxError(f"响应不是 JSON（HTTP {status_code}）") from exc

        status = str(payload.get("status") or "").strip().lower()
        message = str(payload.get("msg") or payload.get("message") or "")
        if status in ("login", "relogin"):
            raise HeyboxLoginRequired(message or "小黑盒登录已失效")
        if status not in ("", "ok", "success"):
            lowered = message.lower()
            if "频繁" in message or "429" in lowered:
                raise HeyboxRateLimited(message)
            raise HeyboxError(message or f"接口返回错误（{status}）")

        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def profile(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        """当前登录账号（未登录会抛 HeyboxLoginRequired）。"""
        return await self.call(session, "/account/restore_login")

    async def game_list(
        self, session: aiohttp.ClientSession, userid: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """个人游戏记录（必须带 userid，否则上游返回「请求失败了」）。"""
        body = await self.call(
            session,
            "/account/game_list/",
            {"userid": userid, "offset": 0, "limit": limit, "sort": "play_time"},
        )
        for key in ("game_list", "list", "games", "data_list"):
            rows = body.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        return []

    # ---------- 影之刃零档案（App 端 H5 接口，web cookie 同样可用） ----------

    async def cloud_accounts(self, session: aiohttp.ClientSession, game_id: str = PBZ_APPID) -> dict[str, Any]:
        """已绑定的 Steam 账号；``cur_steam.account_id`` 就是 SteamID。"""
        return await self.call(
            session,
            "/game/cloud_archive/user/accounts",
            {"game_id": game_id},
            app_ua=True,
        )

    async def archive_list(
        self, session: aiohttp.ClientSession, archive_id: str, game_id: str = PBZ_APPID
    ) -> dict[str, Any]:
        """存档槽列表，返回 archives / default_archive_id / default_slot_id。"""
        return await self.call(
            session,
            "/game/phantom_blade_zero/archive/list",
            {"archive_id": archive_id, "game_id": game_id},
            app_ua=True,
        )

    async def archive_overview(
        self, session: aiohttp.ClientSession, archive_id: str, slot_id: str
    ) -> dict[str, Any]:
        """档案概览：report 里含身陨、BOSS、收集、装备、地图、成就、任务。"""
        return await self.call(
            session,
            "/game/phantom_blade_zero/archive/overview",
            {"archive_id": archive_id, "slot_id": slot_id},
            app_ua=True,
        )

    async def archive_entries(
        self,
        session: aiohttp.ClientSession,
        archive_id: str,
        slot_id: str,
        section: str = "bosses",
        limit: int = 100,
    ) -> dict[str, Any]:
        """分节明细。

        ``section=bosses`` 返回**全部** BOSS（含没打赢的），
        比 ``archive/overview`` 里的 ``report.bosses`` 完整
        —— 后者只给已击败的那几条。
        """
        return await self.call(
            session,
            "/game/phantom_blade_zero/archive/entries",
            {
                "archive_id": archive_id,
                "slot_id": slot_id,
                "section": section,
                "limit": limit,
            },
            app_ua=True,
        )


_STEAM_AVATAR_RE = re.compile(r"/avatar/steam/(\d{5,})/")
_STEAM64_BASE = 76561197960265728


def steam_id_from_avatar(url: str) -> str:
    """小黑盒把 Steam 头像放在 ``/avatar/steam/<account_id>/`` 下。

    ``account_id`` 是 Steam 的 32 位账号 ID，加上固定基数就是 Steam64。
    这是目前唯一能从 web 接口拿到的 Steam 身份线索（restore_login 与
    game_list 都不直接返回 steamid）。
    """
    match = _STEAM_AVATAR_RE.search(str(url or ""))
    if not match:
        return ""
    try:
        return str(_STEAM64_BASE + int(match.group(1)))
    except ValueError:
        return ""


def pick_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """从 restore_login 的返回里挑出展示用的字段。"""
    candidates: list[dict[str, Any]] = [profile]
    for key in ("profile", "account_detail", "account", "user", "user_info"):
        value = profile.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    def _pick(*keys: str) -> str:
        for c in candidates:
            for k in keys:
                v = c.get(k)
                if v is not None and str(v) != "":
                    return str(v)
        return ""

    avatar = _pick("avatar", "avatar_url", "user_avatar")
    steam = _pick("steam_id", "steamid", "steam_account")
    steam_source = "profile" if steam else ""

    return {
        "heybox_id": _pick("heybox_id", "user_heybox_id", "heyboxid", "userid", "user_id", "id", "uid"),
        "nickname": _pick("nickname", "username", "name", "user_nickname"),
        "level": _pick("level", "user_level", "exp_level"),
        "avatar": avatar,
        "steam_id": steam,
        "steam_source": steam_source,
    }


def find_steam_in_game_list(rows: list[dict[str, Any]]) -> str:
    """游戏记录里偶尔带 steam 字段，兜底捞一次。"""
    for row in rows:
        for key in ("steam_id", "steamid", "steamId"):
            value = row.get(key)
            if value and str(value).isdigit() and len(str(value)) >= 17:
                return str(value)
    return ""


def encode_state(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()


def decode_state(value: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(value.encode()).decode())
    except Exception:  # noqa: BLE001
        return {}


# ==================== 微信扫码登录 ====================


def _read_set_cookies(response: aiohttp.ClientResponse) -> dict[str, str]:
    result: dict[str, str] = {}
    headers = response.headers
    values = headers.getall("Set-Cookie", []) if hasattr(headers, "getall") else []
    for raw in values:
        first = raw.split(";")[0]
        key, sep, value = first.partition("=")
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


class WechatLoginClient:
    """微信扫码登录小黑盒（SSO 换 cookie），与 App 扫码等价。"""

    WEIXIN_APP_ID = "wxced0cbce486f737e"
    LOGIN_REDIRECT = "https://api.xiaoheihe.cn/account/wechat/login_redirect/v2/web_sso/"
    CALLBACK_URL = "http://127.0.0.1/heybox-login-callback"
    poll_delay = 1.0

    _UUID_RE = re.compile(r"connect/qrcode/([A-Za-z0-9_\-]{8,})")
    _ERRCODE_RE = re.compile(r"wx_errcode=(\d+)")
    _WXCODE_RE = re.compile(r"window\.wx_code='([^']*)'")

    def __init__(self, device_id: str = ""):
        self.device_id = device_id
        self._headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://open.weixin.qq.com/",
        }

    def _redirect_uri(self) -> str:
        return f"{self.LOGIN_REDIRECT}?redirect_url={quote(self.CALLBACK_URL, safe='')}"

    async def request_qr(self) -> QRSession:
        params = {
            "appid": self.WEIXIN_APP_ID,
            "redirect_uri": self._redirect_uri(),
            "response_type": "code",
            "scope": "snsapi_login",
            "state": "xiaoheihe",
        }
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://open.weixin.qq.com/connect/qrconnect",
                params=params,
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        match = self._UUID_RE.search(html)
        if match is None:
            raise ValueError("微信登录：未能从页面取到二维码 uuid")
        uuid = match.group(1)
        return QRSession(
            qr_content=f"https://open.weixin.qq.com/connect/qrcode/{uuid}",
            poll_params={"uuid": uuid},
            expires_at=time.time() + 120,
        )

    async def qr_image(self, qr_session: QRSession) -> bytes:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(qr_session.qr_content, headers=self._headers) as resp:
                resp.raise_for_status()
                return await resp.read()

    async def check_qr(self, qr_session: QRSession) -> LoginResult:
        uuid = str(qr_session.poll_params.get("uuid") or "")
        if not uuid:
            return LoginResult(LoginState.FAILED, "缺少微信二维码 uuid")
        params = {"uuid": uuid, "_": str(int(time.time() * 1000))}
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://long.open.weixin.qq.com/connect/l/qrconnect",
                    params=params,
                    headers=self._headers,
                ) as resp:
                    body = await resp.text()
        except (aiohttp.ClientError, TimeoutError):
            return LoginResult(LoginState.WAITING_SCAN, "")

        errcode_match = self._ERRCODE_RE.search(body)
        errcode = errcode_match.group(1) if errcode_match else ""
        if errcode == "404":
            return LoginResult(LoginState.SCANNED_WAITING_CONFIRM, "已扫码，请在微信中确认")
        if errcode in ("402", "403"):
            return LoginResult(LoginState.EXPIRED, "二维码已过期")
        if errcode != "405":
            return LoginResult(LoginState.WAITING_SCAN, "")

        wxcode_match = self._WXCODE_RE.search(body)
        if wxcode_match is None or not wxcode_match.group(1):
            return LoginResult(LoginState.FAILED, "微信已确认，但未取到 code")
        return await self._exchange(wxcode_match.group(1))

    async def _exchange(self, wx_code: str) -> LoginResult:
        url = (
            f"{self.LOGIN_REDIRECT}?redirect_url={quote(self.CALLBACK_URL, safe='')}"
            f"&code={quote(wx_code, safe='')}&state=xiaoheihe"
        )
        timeout = aiohttp.ClientTimeout(total=25)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=self._headers, allow_redirects=False) as resp:
                    cookies = _read_set_cookies(resp)
                    location = str(resp.headers.get("Location") or "")
        except (aiohttp.ClientError, TimeoutError) as exc:
            return LoginResult(LoginState.FAILED, f"登录交换失败：{type(exc).__name__}")

        query = dict(parse_qsl(urlsplit(location).query, keep_blank_values=True))
        uid = cookies.get("user_heybox_id") or cookies.get("heybox_id") or query.get("heybox_id") or ""
        pkey = cookies.get("user_pkey") or cookies.get("pkey") or query.get("pkey") or ""
        if not uid or not pkey:
            return LoginResult(LoginState.FAILED, "登录交换未返回 pkey / heybox_id")

        cookies.setdefault("pkey", pkey)
        cookies.setdefault("user_pkey", pkey)
        cookies.setdefault("heybox_id", uid)
        cookies.setdefault("user_heybox_id", uid)
        return LoginResult(LoginState.SUCCESS, "登录成功", cookies=cookies, uid=uid)


LOGIN_CLIENTS = {
    "heybox": HeyboxLoginClient,
    "wechat": WechatLoginClient,
}


def create_login_client(mode: str, device_id: str = ""):
    factory = LOGIN_CLIENTS.get(str(mode or "").strip().lower())
    return factory(device_id) if factory else None
