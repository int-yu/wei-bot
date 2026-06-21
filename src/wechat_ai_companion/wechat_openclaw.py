from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp

from .config import WeChatSettings


@dataclass(slots=True)
class WeChatInboundMessage:
    from_user_id: str
    context_token: str
    text: str
    raw: dict[str, Any]


@dataclass(slots=True)
class LoginResult:
    bot_token: str
    base_url: str
    bot_id: str | None = None
    user_id: str | None = None


class OpenClawWeChatClient:
    def __init__(self, settings: WeChatSettings) -> None:
        self.settings = settings
        self.base_url = settings.base_url
        self.bot_token: str | None = None
        self.get_updates_buf = ""
        self.typing_tickets: dict[str, str] = {}

    def _headers(self, token: str | None = None) -> dict[str, str]:
        uin = str(random.randint(0, 0xFFFFFFFF))
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": self.settings.app_client_version,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _base_info(self) -> dict[str, str]:
        return {
            "channel_version": self.settings.channel_version,
            "bot_agent": self.settings.bot_agent,
        }

    async def _get(self, path: str, *, token: str | None = None, base_url: str | None = None) -> dict[str, Any]:
        url = f"{base_url or self.base_url}/{path}"
        async with _client_session() as session:
            async with session.get(url, headers=self._headers(token)) as response:
                return await _json_response(response)

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        token: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        url = f"{base_url or self.base_url}/{path}"
        async with _client_session() as session:
            async with session.post(url, json=body, headers=self._headers(token)) as response:
                return await _json_response(response)

    async def login(self) -> LoginResult:
        qrcode_data = await self.fetch_qrcode()
        qrcode = qrcode_data["qrcode"]
        qrcode_image = str(qrcode_data.get("qrcode_img_content") or qrcode)
        print("扫码地址:", qrcode_image)
        render_terminal_qr(qrcode_image)
        result = await self.wait_login_confirmation(qrcode)
        self.bot_token = result.bot_token
        self.base_url = result.base_url or self.base_url
        return result

    async def fetch_qrcode(self, local_token_list: list[str] | None = None) -> dict[str, Any]:
        data = await self._post(
            "ilink/bot/get_bot_qrcode?bot_type=3",
            {"local_token_list": local_token_list or []},
            base_url=self.base_url,
        )
        if data.get("qrcode"):
            return data
        return await self._get("ilink/bot/get_bot_qrcode?bot_type=3", base_url=self.base_url)

    async def wait_login_confirmation(self, qrcode: str) -> LoginResult:
        deadline = asyncio.get_event_loop().time() + self.settings.qrcode_scan_timeout_seconds
        current_base_url = self.base_url
        pending_verify_code: str | None = None
        scanned_printed = False
        while True:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("QR code login timed out.")
            path = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
            if pending_verify_code:
                path += f"&verify_code={quote(pending_verify_code, safe='')}"
            status = await self._get(path, base_url=current_base_url)
            state = status.get("status")
            if state == "confirmed" or status.get("bot_token"):
                return LoginResult(
                    bot_token=status["bot_token"],
                    base_url=status.get("baseurl") or status.get("base_url") or current_base_url,
                    bot_id=status.get("ilink_bot_id"),
                    user_id=status.get("ilink_user_id"),
                )
            if state == "scaned" and not scanned_printed:
                print("已扫码，等待手机端确认。")
                scanned_printed = True
            elif state == "need_verifycode" or status.get("need_verifycode"):
                pending_verify_code = input("请输入手机微信显示的数字配对码: ").strip()
                continue
            elif state == "verify_code_blocked":
                raise RuntimeError("数字配对码多次错误，请重新运行后刷新二维码。")
            elif state == "scaned_but_redirect" and status.get("redirect_host"):
                current_base_url = f"https://{status['redirect_host']}"
                print(f"扫码轮询切换到节点: {current_base_url}")
                continue
            elif state == "expired":
                raise RuntimeError("二维码已过期，请重新运行。")
            elif state and state != "wait":
                print(f"登录状态: {state}，原始响应: {status}")
            await asyncio.sleep(1)

    async def get_updates(self) -> list[WeChatInboundMessage]:
        if not self.bot_token:
            raise RuntimeError("WeChat client is not logged in.")
        result = await self._post(
            "ilink/bot/getupdates",
            {"get_updates_buf": self.get_updates_buf, "base_info": self._base_info()},
            token=self.bot_token,
            base_url=self.base_url,
        )
        self.get_updates_buf = result.get("get_updates_buf") or self.get_updates_buf
        messages: list[WeChatInboundMessage] = []
        for raw in result.get("msgs") or []:
            if raw.get("message_type") != 1:
                continue
            if raw.get("group_id"):
                print(f"跳过疑似群聊消息: group_id={raw.get('group_id')}")
                continue
            text = _extract_text(raw)
            if not text:
                continue
            messages.append(
                WeChatInboundMessage(
                    from_user_id=raw["from_user_id"],
                    context_token=raw["context_token"],
                    text=text,
                    raw=raw,
                )
            )
        return messages

    async def send_text(self, to_user_id: str, context_token: str, text: str) -> None:
        if not self.bot_token:
            raise RuntimeError("WeChat client is not logged in.")
        client_id = f"openclaw-weixin-{random.randint(0, 0xFFFFFFFF):08x}"
        await self._post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": self._base_info(),
            },
            token=self.bot_token,
            base_url=self.base_url,
        )

    async def set_typing(self, user_id: str, context_token: str, status: int) -> None:
        if not self.settings.show_typing or not self.bot_token:
            return
        ticket = self.typing_tickets.get(user_id)
        if not ticket:
            config = await self._post(
                "ilink/bot/getconfig",
                {"ilink_user_id": user_id, "context_token": context_token, "base_info": self._base_info()},
                token=self.bot_token,
                base_url=self.base_url,
            )
            ticket = config.get("typing_ticket") or ""
            self.typing_tickets[user_id] = ticket
        if not ticket:
            return
        await self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": user_id,
                "typing_ticket": ticket,
                "status": status,
                "base_info": self._base_info(),
            },
            token=self.bot_token,
            base_url=self.base_url,
        )


async def _json_response(response: aiohttp.ClientResponse) -> dict[str, Any]:
    text = await response.text()
    if response.status >= 400:
        raise RuntimeError(f"OpenClaw HTTP {response.status}: {text[:500]}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _client_session(**kwargs: Any) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector, trust_env=True, **kwargs)


def _extract_text(raw: dict[str, Any]) -> str:
    for item in raw.get("item_list") or []:
        if item.get("type") == 1:
            return str((item.get("text_item") or {}).get("text") or "").strip()
    return ""


def render_terminal_qr(content: str) -> None:
    if not content:
        return
    if content.startswith("http") and _render_terminal_image_from_url(content):
        return
    try:
        import qrcode
    except ImportError:
        print("未安装 qrcode/Pillow，无法在终端渲染二维码；可直接打开上面的扫码地址。")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(content)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    print()
    for row in matrix:
        print("".join("██" if cell else "  " for cell in row))
    print()


def _render_terminal_image_from_url(url: str) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        image = Image.open(io.BytesIO(data)).convert("L")
        max_width = 72
        scale = max(1, int(image.width / max_width))
        width = max(1, int(image.width / scale))
        height = max(1, int(image.height / scale))
        image = image.resize((width, height))
        print()
        for y in range(height):
            print("".join("██" if image.getpixel((x, y)) < 128 else "  " for x in range(width)))
        print()
        return True
    except Exception:
        return False
