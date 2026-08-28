"""Music Assistant 2.10 compatibility entrypoint for NCloud Music."""
from __future__ import annotations

import logging
from collections import deque
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from .legacy_impl import *  # noqa: F403
from .legacy_impl import (
    CONF_API_URL,
    CONF_AUDIO_QUALITY,
    CONF_COOKIE,
    CONF_IMAGE_SIZE,
    CONF_PLAY_FREE_TRIAL,
    NCloudMusicProvider as LegacyNCloudMusicProvider,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider

__version__ = "0.2.0-beta.1"
_LOGGER = logging.getLogger(__name__)


class NCloudMusicProvider(LegacyNCloudMusicProvider):
    """NCloud provider adapted to Music Assistant 2.10 setup/options APIs."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return runtime-editable options for an already configured provider."""
        has_cookie = bool(self.get_setup_value(CONF_COOKIE, ""))
        return (
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                label="期望音质",
                default_value="exhigh",
                options=(
                    ConfigValueOption(title="标准 (128k) ⚪", value="standard"),
                    ConfigValueOption(title="较高 (192k) ⚪", value="higher"),
                    ConfigValueOption(title="极高 (320k) ⚪", value="exhigh"),
                    ConfigValueOption(title="无损 (FLAC) 🔴", value="lossless"),
                    ConfigValueOption(title="Hi-Res 🔴", value="hires"),
                    ConfigValueOption(title="高清环绕声 👑", value="jyeffect"),
                    ConfigValueOption(title="沉浸环绕声 👑", value="sky"),
                    ConfigValueOption(title="杜比全景声 👑", value="dolby"),
                    ConfigValueOption(title="超清母带 👑", value="jymaster"),
                ),
                description="播放时尝试的最高音质。如果所选音质不可用，将自动尝试更低音质。",
            ),
            ConfigEntry(
                key=CONF_PLAY_FREE_TRIAL,
                type=ConfigEntryType.BOOLEAN,
                label="仅有试听片段时继续播放",
                description=(
                    "仅能获取试听片段时继续播放。关闭后将跳过该歌曲；"
                    "默认开启，以避免免费账号连续跳过大量歌曲。"
                ),
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_IMAGE_SIZE,
                type=ConfigEntryType.STRING,
                label="封面尺寸",
                default_value="300",
                options=(
                    ConfigValueOption(title="原图（最清晰，流量最大）", value="original"),
                    ConfigValueOption(title="120 x 120（最省流量）", value="120"),
                    ConfigValueOption(title="200 x 200（较省流量）", value="200"),
                    ConfigValueOption(title="300 x 300（默认）", value="300"),
                    ConfigValueOption(title="500 x 500（较清晰）", value="500"),
                    ConfigValueOption(title="800 x 800（高清）", value="800"),
                ),
                description="全局封面图片尺寸。尺寸越大越清晰，但加载越慢、流量越高。",
            ),
            ConfigEntry(
                key="login_status",
                type=ConfigEntryType.LABEL,
                label="✅ 已登录" if has_cookie else "⚠️ 未登录",
                description=(
                    "登录/API 地址请使用 Music Assistant 的“重新配置”流程修改。"
                    if has_cookie
                    else "当前未保存登录凭据；可通过“重新配置”流程扫码登录。"
                ),
                required=False,
            ),
        )

    async def handle_async_init(self) -> None:
        """Initialize with credentials collected by the MA 2.10 setup flow."""
        self._api_url = str(self.get_setup_value(CONF_API_URL, "") or "").rstrip("/")
        cookie_str = str(self.get_setup_value(CONF_COOKIE, "") or "")
        self._cookies = self._parse_cookie(cookie_str)
        self._image_size = str(self.get_config_value(CONF_IMAGE_SIZE, "300") or "300")

        _LOGGER.info(
            "NCloud Music Provider 初始化完成 (MA 2.10 compat, API: %s, 已登录: %s, 封面尺寸: %s)",
            self._api_url or "<未配置>",
            bool(self._cookies),
            self._image_size,
        )
        self._playlist_context_ids: deque[str] = deque(maxlen=1200)
        self._playlist_context_set: set[str] = set()


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> MusicProvider:
    """Initialize the MA 2.10 compatible provider instance."""
    return NCloudMusicProvider(mass, manifest, config)
