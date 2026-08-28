"""Music Assistant 2.10 guided setup flow for NCloud Music."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import AbortFlow, StepExpiredError

from . import CONF_API_URL, CONF_COOKIE

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


class QrExpiredError(Exception):
    """Raised when the NetEase login QR code expires."""


async def run_setup(session: SetupSession) -> None:
    """Collect API URL, optionally perform QR login, and persist setup_data."""
    existing_setup = session.context.setup_data or {}
    existing_values = session.context.values or {}
    existing_api = str(existing_setup.get(CONF_API_URL) or existing_values.get(CONF_API_URL) or "")
    existing_cookie = str(existing_setup.get(CONF_COOKIE) or existing_values.get(CONF_COOKIE) or "")

    values = await session.form(
        [
            ConfigEntry(
                key=CONF_API_URL,
                type=ConfigEntryType.STRING,
                label="API 服务器地址",
                description="第三方 API 服务的完整 URL（例如：http://192.168.1.100:3000）",
                required=True,
                default_value=existing_api,
            ),
            ConfigEntry(
                key="login_now",
                type=ConfigEntryType.BOOLEAN,
                label="使用云音乐 APP 扫码登录",
                description="关闭后可匿名使用；已有登录凭据在重新配置时会被保留。",
                required=False,
                default_value=not bool(existing_cookie),
            ),
        ],
        step_id="connection",
        last_step=False,
    )

    api_url = str(values.get(CONF_API_URL) or "").strip().rstrip("/")
    if not api_url:
        raise AbortFlow("invalid_api_url")

    login_now = bool(values.get("login_now", False))
    cookie = existing_cookie

    if login_now:
        # Give the user a fresh QR once if the first one expires.
        for _attempt in range(2):
            try:
                qr_key, qr_image = await _create_qr(session, api_url)
                cookie = await session.progress_until(
                    _poll_qr_login(session, api_url, qr_key),
                    step_id="qr_login",
                    text="请使用云音乐 APP 扫码并确认登录",
                    image=qr_image,
                    expires_in=120,
                )
                break
            except (StepExpiredError, QrExpiredError):
                cookie = ""
                continue
        if not cookie:
            raise AbortFlow("login_timeout")

    setup_data = {CONF_API_URL: api_url}
    if cookie:
        setup_data[CONF_COOKIE] = cookie
    await session.finish(setup_data)


async def _create_qr(session: SetupSession, api_url: str) -> tuple[str, str | None]:
    """Create a login QR code and return (key, data-uri image)."""
    http = session.mass.http_session
    try:
        async with http.get(f"{api_url}/login/qr/key?timestamp={_timestamp()}") as resp:
            key_data = await resp.json()
        if key_data.get("code") != 200:
            raise RuntimeError(f"qr key failed: {key_data}")
        qr_key = str(key_data["data"]["unikey"])

        async with http.get(
            f"{api_url}/login/qr/create?key={qr_key}&qrimg=true&timestamp={_timestamp()}"
        ) as resp:
            qr_data = await resp.json()
        if qr_data.get("code") != 200:
            raise RuntimeError(f"qr create failed: {qr_data}")
        qr_image = qr_data.get("data", {}).get("qrimg")
        if qr_image and not str(qr_image).startswith("data:image"):
            qr_image = None
        return qr_key, qr_image
    except Exception as err:
        raise AbortFlow("api_unavailable") from err


async def _poll_qr_login(session: SetupSession, api_url: str, qr_key: str) -> str:
    """Poll the third-party API until the QR login succeeds."""
    http = session.mass.http_session
    while True:
        await asyncio.sleep(2)
        async with http.get(
            f"{api_url}/login/qr/check?key={qr_key}&timestamp={_timestamp()}"
        ) as resp:
            check_data = await resp.json()
        code = check_data.get("code")
        if code == 803:
            cookie = str(check_data.get("cookie") or "")
            if not cookie:
                raise AbortFlow("empty_cookie")
            return cookie
        if code == 800:
            raise QrExpiredError
        # 801 = waiting for scan, 802 = scanned and waiting for phone confirmation.


def _timestamp() -> int:
    """Return a millisecond Unix timestamp."""
    import time

    return int(time.time() * 1000)
