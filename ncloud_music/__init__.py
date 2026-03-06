"""
NCloud Music Provider for Music Assistant.

通过第三方 API 提供云音乐服务的 MA 原生插件。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from collections import deque
from collections.abc import AsyncGenerator, Sequence

import aiohttp

from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueType,
    ConfigValueOption,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest
    from music_assistant.mass import MusicAssistant

__version__ = "0.1.0"

# 配置项 Key
CONF_API_URL = "api_url"
CONF_COOKIE = "cookie"
CONF_ACTION_QR_LOGIN = "qr_login"
CONF_AUDIO_QUALITY = "audio_quality"
CONF_IMAGE_SIZE = "image_size"

PLAYLIST_ID_DAILY = "daily_recommend"

_LOGGER = logging.getLogger(__name__)

# 发现内容来源标签（用于补齐缺失的 creator 信息）
DISCOVERY_OWNER_RECOMMEND = "云音乐推荐"
DISCOVERY_OWNER_TOPLIST = "云音乐排行榜"
DISCOVERY_OWNER_HQ = "云音乐精品歌单"
BROWSE_CAT_MY = "my"
BROWSE_CAT_DAILY = "daily"
BROWSE_CAT_RECOMMEND = "recommend"
BROWSE_CAT_TOPLIST = "toplist"
BROWSE_CAT_HQ = "high_quality"


async def setup(
    mass: MusicAssistant,
    manifest: ProviderManifest,
    config: ProviderConfig,
) -> MusicProvider:
    """初始化 NCloud Music Provider 实例。"""
    return NCloudMusicProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    返回此 Provider 的配置项定义。
    
    参考 Spotify 的实现：在 ACTION 处理中直接 await 等待认证完成。
    """
    # 处理扫码登录 ACTION（参考 Spotify 的 _handle_auth_actions）
    if action == CONF_ACTION_QR_LOGIN and values:
        # 调用登录流程并等待（就像 Spotify 的 pkce_auth_flow）
        cookie = await _qr_code_login_flow(mass, values)
        if cookie:
            # 直接修改 values 字典（Spotify 的标准做法）
            values[CONF_COOKIE] = cookie
            _LOGGER.info("✅ 扫码登录成功！Cookie 已获取，MA 会在保存时持久化。")
        else:
            _LOGGER.warning("⚠️ 扫码登录失败或超时")
    
    # 判断登录状态（加密字段非空表示已设置）
    cookie = values.get(CONF_COOKIE, "") if values else ""
    has_cookie = cookie not in (None, "")
    
    if has_cookie:
        login_label = "✅ 已登录"
        login_desc = "如需更换账号，请点击重新扫码登录。"
    else:
        login_label = "⚠️ 未登录"
        login_desc = "点击按钮后将打开二维码页面，请使用云音乐 APP 扫码。"
    
    return (
        ConfigEntry(
            key=CONF_API_URL,
            type=ConfigEntryType.STRING,
            label="API 服务器地址",
            description="第三方 API 服务的完整 URL（例如：http://192.168.1.100:3000）",
            required=True,
            default_value="",
        ),
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
            key=CONF_COOKIE,
            type=ConfigEntryType.SECURE_STRING,
            label="登录凭据",
            description="自动保存，请勿手动修改。",
            required=False,
            hidden=True,
            default_value="",
        ),
        ConfigEntry(
            key="login_status",
            type=ConfigEntryType.LABEL,
            label=login_label,
            description=login_desc,
            required=False,
        ),
        ConfigEntry(
            key=CONF_ACTION_QR_LOGIN,
            type=ConfigEntryType.ACTION,
            label="扫码登录",
            description="点击后将打开二维码页面，扫码成功后会自动保存登录状态。",
            action=CONF_ACTION_QR_LOGIN,
            required=False,
        ),
    )


async def _qr_code_login_flow(
    mass: MusicAssistant,
    values: dict[str, ConfigValueType],
) -> str | None:
    """
    二维码登录流程（模仿 Spotify 的 pkce_auth_flow）。
    
    这个函数会 await 等待登录完成或超时，但不会阻塞 UI。
    返回 Cookie 字符串，失败返回 None。
    """
    api_url = str(values.get(CONF_API_URL, "")).rstrip("/")
    session_id = values.get("session_id")
    
    if not api_url:
        _LOGGER.error("扫码登录失败：未配置 API 地址")
        return None
    
    if not session_id:
        _LOGGER.error("扫码登录失败：缺少 session_id")
        return None
    
    try:
        async with aiohttp.ClientSession() as session:
            # 步骤 1: 获取二维码 key
            _LOGGER.info("[1/4] 获取二维码 key...")
            async with session.get(f"{api_url}/login/qr/key?timestamp={_timestamp()}") as resp:
                key_data = await resp.json()
                if key_data.get("code") != 200:
                    _LOGGER.error("获取二维码 key 失败: %s", key_data)
                    return None
                qr_key = key_data["data"]["unikey"]
                _LOGGER.debug("获取到 key: %s", qr_key)
            
            # 步骤 2: 生成二维码 URL
            _LOGGER.info("[2/4] 生成二维码 URL...")
            async with session.get(
                f"{api_url}/login/qr/create?key={qr_key}&qrimg=true&timestamp={_timestamp()}"
            ) as resp:
                qr_data = await resp.json()
                if qr_data.get("code") != 200:
                    _LOGGER.error("生成二维码失败: %s", qr_data)
                    return None
                qr_url = qr_data["data"].get("qrurl")
        
        if not qr_url:
            _LOGGER.error("二维码 URL 为空")
            return None
        
        # 步骤 3: 使用 AuthenticationHelper 打开浏览器
        from urllib.parse import quote
        from music_assistant.helpers.auth import AuthenticationHelper
        
        qr_image_url = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote(qr_url)}"
        
        _LOGGER.info("[3/4] 打开浏览器显示二维码...")
        _LOGGER.info("二维码链接: %s", qr_image_url)
        
        # 打开浏览器（不需要等待回调，因为扫码在手机上）
        try:
            async with AuthenticationHelper(mass, str(session_id)) as auth_helper:
                # 启动认证流程但不等待回调
                asyncio.create_task(auth_helper.authenticate(qr_image_url))
                await asyncio.sleep(0.5)  # 短暂等待浏览器打开
        except Exception as e:
            _LOGGER.warning("打开浏览器失败: %s", e)
        
        # 步骤 4: 轮询登录状态（等待用户扫码）
        _LOGGER.info("[4/4] 等待扫码（最多 120 秒）...")
        _LOGGER.info("提示：请使用云音乐 APP 扫描二维码")
        
        async with aiohttp.ClientSession() as session:
            for i in range(60):  # 最多轮询 120 秒（60 次 x 2 秒）
                await asyncio.sleep(2)  # 每 2 秒轮询一次
                
                try:
                    async with session.get(
                        f"{api_url}/login/qr/check?key={qr_key}&timestamp={_timestamp()}"
                    ) as resp:
                        check_data = await resp.json()
                        code = check_data.get("code")
                        
                        if code == 803:  # 登录成功
                            cookie = check_data.get("cookie", "")
                            if cookie:
                                _LOGGER.info("🎉 扫码登录成功！Cookie 长度: %d", len(cookie))
                                return cookie
                            else:
                                _LOGGER.error("登录成功但 Cookie 为空")
                                return None
                        
                        elif code == 800:  # 二维码过期
                            _LOGGER.warning("二维码已过期，请重新扫码")
                            return None
                        
                        elif code == 802:  # 等待用户确认
                            _LOGGER.info("已扫码，等待您在手机上确认...")
                        
                        # code == 801: 等待扫码（继续轮询）
                        
                except Exception as e:
                    _LOGGER.warning("轮询登录状态异常: %s", e)
            
            # 超时
            _LOGGER.warning("扫码登录超时（120 秒），请重新尝试")
            return None
    
    except Exception as e:
        _LOGGER.exception("扫码登录流程异常: %s", e)
        return None


async def _handle_qr_login(
    mass: MusicAssistant,
    instance_id: str | None,
    values: dict[str, ConfigValueType] | None,
) -> None:
    """
    处理二维码登录流程。
    
    流程：
    1. 调用 /login/qr/key 获取二维码 key
    2. 调用 /login/qr/create 生成二维码 URL
    3. 通过外部服务生成二维码图片并打开
    4. 后台轮询 /login/qr/check 检测登录状态
    5. 成功后保存 cookie 到配置
    """
    if not values:
        _LOGGER.warning("扫码登录：缺少配置值")
        return
    
    api_url = values.get(CONF_API_URL, "")
    if not api_url:
        _LOGGER.warning("扫码登录：未配置 API 地址")
        return
    
    api_url = str(api_url).rstrip("/")
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. 生成二维码 key
            async with session.get(f"{api_url}/login/qr/key?timestamp={_timestamp()}") as resp:
                key_data = await resp.json()
                if key_data.get("code") != 200:
                    _LOGGER.error("获取二维码 key 失败: %s", key_data)
                    return
                qr_key = key_data["data"]["unikey"]
            
            # 2. 生成二维码 URL
            async with session.get(
                f"{api_url}/login/qr/create?key={qr_key}&qrimg=true&timestamp={_timestamp()}"
            ) as resp:
                qr_data = await resp.json()
                if qr_data.get("code") != 200:
                    _LOGGER.error("生成二维码失败: %s", qr_data)
                    return
                # 使用 API 返回的内嵌二维码图片（base64）或 URL
                qr_img = qr_data["data"].get("qrimg")
                qr_url = qr_data["data"].get("qrurl")
            
            # 3. 打开二维码页面
            # 使用第三方二维码生成服务，因为 MA 无法直接显示 base64 图片
            if qr_url:
                qr_page_url = f"https://cdn.dotmaui.com/qrc/?t={qr_url}"
                # TODO: 调用 mass.open_url() 打开二维码页面
                # 目前 MA API 可能需要进一步确认
                _LOGGER.info("请打开以下链接扫码登录: %s", qr_page_url)
            
            # 4. 后台轮询登录状态（最多 60 次，每次 2 秒）
            for i in range(60):
                await asyncio.sleep(2)
                async with session.get(
                    f"{api_url}/login/qr/check?key={qr_key}&timestamp={_timestamp()}"
                ) as resp:
                    check_data = await resp.json()
                    code = check_data.get("code")
                    
                    if code == 803:  # 登录成功
                        cookie = check_data.get("cookie", "")
                        if cookie and instance_id:
                            # 5. 保存 cookie 到配置
                            await mass.config.set_provider_config_value(
                                instance_id, CONF_COOKIE, cookie
                            )
                            _LOGGER.info("扫码登录成功！")
                        return
                    elif code == 800:  # 二维码过期
                        _LOGGER.warning("二维码已过期，请重新扫码")
                        return
                    # 801: 等待扫码, 802: 等待确认
            
            _LOGGER.warning("扫码登录超时")
    
    except Exception as e:
        _LOGGER.exception("扫码登录异常: %s", e)


def _timestamp() -> int:
    """生成时间戳（毫秒）。"""
    import time
    return int(time.time() * 1000)


class NCloudMusicProvider(MusicProvider):
    """NCloud Music 音乐提供者。"""
    
    # 支持的功能
    @property
    def supported_features(self) -> set[ProviderFeature]:
        """返回此 Provider 支持的功能列表。"""
        return {
            ProviderFeature.SEARCH,
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_PLAYLISTS,
            ProviderFeature.ARTIST_ALBUMS,
            ProviderFeature.ARTIST_TOPTRACKS,
            ProviderFeature.SIMILAR_TRACKS,
        }
    
    async def handle_async_init(self) -> None:
        """Provider 初始化（在 setup 之后调用）。"""
        self._api_url = str(self.config.get_value(CONF_API_URL)).rstrip("/")
        cookie_str = str(self.config.get_value(CONF_COOKIE) or "")
        self._cookies = self._parse_cookie(cookie_str)
        self._image_size = str(self.config.get_value(CONF_IMAGE_SIZE) or "300")
        
        _LOGGER.info(
            "NCloud Music Provider 初始化完成 (API: %s, 已登录: %s, 封面尺寸: %s)",
            self._api_url,
            bool(self._cookies),
            self._image_size,
        )
        # 轻量上下文标记：仅用于判断“最近是否来自歌单场景”。
        self._playlist_context_ids: deque[str] = deque(maxlen=1200)
        self._playlist_context_set: set[str] = set()

    def _remember_playlist_context(self, tracks: list[Track]) -> None:
        """记录最近从歌单路径返回的歌曲 ID。"""
        for track in tracks:
            item_id = str(track.item_id)
            if item_id in self._playlist_context_set:
                continue
            if len(self._playlist_context_ids) >= self._playlist_context_ids.maxlen:
                dropped = self._playlist_context_ids.popleft()
                self._playlist_context_set.discard(dropped)
            self._playlist_context_ids.append(item_id)
            self._playlist_context_set.add(item_id)

    async def browse(self, path: str) -> Sequence[Playlist | ItemMapping | BrowseFolder]:
        """自定义浏览层级：在 playlists 下提供可见分类入口。"""
        if "://" not in path:
            return await super().browse(path)

        # 只拦截 playlists 路径，其余沿用 MA 默认逻辑
        path_parts = [part for part in path.split("://", 1)[1].split("/") if part]
        if not path_parts or path_parts[0] != "playlists":
            return await super().browse(path)

        base_path = path if path.endswith("/") else f"{path}/"

        # 一级：展示分类目录，而不是把所有歌单直接平铺
        if len(path_parts) == 1:
            return [
                BrowseFolder(
                    item_id=BROWSE_CAT_MY,
                    provider=self.instance_id,
                    path=f"{base_path}{BROWSE_CAT_MY}",
                    name="我的歌单",
                ),
                BrowseFolder(
                    item_id=BROWSE_CAT_DAILY,
                    provider=self.instance_id,
                    path=f"{base_path}{BROWSE_CAT_DAILY}",
                    name="每日推荐",
                ),
                BrowseFolder(
                    item_id=BROWSE_CAT_RECOMMEND,
                    provider=self.instance_id,
                    path=f"{base_path}{BROWSE_CAT_RECOMMEND}",
                    name="推荐歌单",
                ),
                BrowseFolder(
                    item_id=BROWSE_CAT_TOPLIST,
                    provider=self.instance_id,
                    path=f"{base_path}{BROWSE_CAT_TOPLIST}",
                    name="排行榜",
                ),
                BrowseFolder(
                    item_id=BROWSE_CAT_HQ,
                    provider=self.instance_id,
                    path=f"{base_path}{BROWSE_CAT_HQ}",
                    name="精品歌单",
                ),
            ]

        category = path_parts[1]
        if category == BROWSE_CAT_MY:
            return await self._get_user_playlists("my")
        if category == BROWSE_CAT_DAILY:
            daily_items: list[Playlist] = [await self._build_daily_playlist()]
            daily_items.extend(await self._get_user_playlists("daily"))
            return daily_items
        if category == BROWSE_CAT_RECOMMEND:
            return await self._get_discovery_playlists("recommend")
        if category == BROWSE_CAT_TOPLIST:
            return await self._get_discovery_playlists("toplist")
        if category == BROWSE_CAT_HQ:
            return await self._get_discovery_playlists("high_quality")

        raise KeyError(f"Invalid playlists subpath: {category}")
    
    def _parse_cookie(self, cookie_str: str) -> dict[str, str]:
        """
        解析 Cookie 字符串为字典。
        
        参考 HA 集成代码 cloud_music.py 第 88-109 行。
        """
        if not cookie_str:
            return {}
        
        cookies = {}
        for item in cookie_str.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            # 跳过无用的 cookie 属性
            if item.startswith(("Max-Age=", "Expires=", "Path=", "HTTPOnly", "Domain=")):
                continue
            key, _, value = item.partition("=")
            if value:
                cookies[key] = value
        
        return cookies

    def _safe_text(self, value: Any, default: str) -> str:
        """Normalize text to a non-empty string to avoid None leaks."""
        if value is None:
            return default
        value_str = str(value).strip()
        return value_str or default
    
    async def _api_request(self, endpoint: str, params: dict | None = None) -> dict:
        """
        调用 API 并返回 JSON 响应。
        
        自动附加 Cookie 和时间戳参数。
        """
        url = f"{self._api_url}{endpoint}"
        
        # 添加时间戳防止缓存
        if params is None:
            params = {}
        params["timestamp"] = _timestamp()
        
        try:
            async with aiohttp.ClientSession(cookies=self._cookies) as session:
                async with session.get(url, params=params) as resp:
                    data = await resp.json()
                    
                    # 检查响应状态
                    code = data.get("code")
                    if code not in (200, 801):
                        _LOGGER.warning("API 请求失败: %s -> %s", endpoint, data)
                    
                    return data
        except Exception as e:
            _LOGGER.exception("API 请求异常 (%s): %s", endpoint, e)
            return {"code": -1, "error": str(e)}
    
    # ========== 搜索功能 ==========
    
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """
        搜索歌曲、专辑、歌手、歌单。
        
        API 端点：/cloudsearch
        type 参数：1=单曲, 10=专辑, 100=歌手, 1000=歌单
        """
        results = SearchResults()
        
        # 搜索单曲
        if MediaType.TRACK in media_types:
            data = await self._api_request(
                "/cloudsearch",
                {"keywords": search_query, "type": 1, "limit": limit},
            )
            if data.get("code") == 200 and data.get("result"):
                songs = data["result"].get("songs", [])
                results.tracks = [self._parse_track(song) for song in songs]
        
        # 搜索专辑
        if MediaType.ALBUM in media_types:
            data = await self._api_request(
                "/cloudsearch",
                {"keywords": search_query, "type": 10, "limit": limit},
            )
            if data.get("code") == 200 and data.get("result"):
                albums = data["result"].get("albums", [])
                results.albums = [self._parse_album(album) for album in albums]
        
        # 搜索歌手
        if MediaType.ARTIST in media_types:
            data = await self._api_request(
                "/cloudsearch",
                {"keywords": search_query, "type": 100, "limit": limit},
            )
            if data.get("code") == 200 and data.get("result"):
                artists = data["result"].get("artists", [])
                results.artists = [self._parse_artist(artist) for artist in artists]
        
        # 搜索歌单
        if MediaType.PLAYLIST in media_types:
            data = await self._api_request(
                "/cloudsearch",
                {"keywords": search_query, "type": 1000, "limit": limit},
            )
            if data.get("code") == 200 and data.get("result"):
                playlists = data["result"].get("playlists", [])
                results.playlists = [self._parse_playlist(pl) for pl in playlists]
        
        return results
    
    # ========== 解析辅助方法 ==========
    
    def _parse_track(self, data: dict) -> Track:
        """解析 API 返回的歌曲数据为 Track 对象。"""
        track_id = str(data["id"])
        
        track = Track(
            item_id=track_id,
            provider=self.instance_id,
            name=data.get("name") or "未知歌曲",
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        
        # 后置设置属性
        duration_ms = data.get("dt")
        track.duration = int(duration_ms / 1000) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else 0
        
        # 解析艺术家
        for ar in data.get("ar", []):
            if not isinstance(ar, dict):
                continue
            track.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=str(ar.get("id", 0)),
                    provider=self.instance_id,
                    name=ar.get("name") or "未知艺术家",
                )
            )
        
        # 解析专辑
        al = data.get("al", {})
        if isinstance(al, dict) and al:
            track.album = ItemMapping(
                media_type=MediaType.ALBUM,
                item_id=str(al.get("id", 0)),
                provider=self.instance_id,
                name=al.get("name") or "未知专辑",
            )
            # 使用专辑封面作为歌曲封面
            if pic_url := al.get("picUrl"):
                track.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=self._build_image_url(pic_url),
                        provider=self.instance_id,
                    )
                ]
        
        return track
    
    def _parse_album(self, data: dict) -> Album:
        """解析 API 返回的专辑数据为 Album 对象。"""
        album_id = str(data["id"])
        
        album = Album(
            item_id=album_id,
            provider=self.instance_id,
            name=data.get("name") or "未知专辑",
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        
        # 解析艺术家
        for ar in data.get("artists", []):
            if not isinstance(ar, dict):
                continue
            album.artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=str(ar.get("id", 0)),
                    provider=self.instance_id,
                    name=ar.get("name") or "未知艺术家",
                )
            )
        
        # 封面图片
        if pic_url := data.get("picUrl"):
            album.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._build_image_url(pic_url),
                    provider=self.instance_id,
                )
            ]
        
        return album
    
    def _parse_artist(self, data: dict) -> Artist:
        """解析 API 返回的歌手数据为 Artist 对象。"""
        artist_id = str(data["id"])
        
        artist = Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=data.get("name") or "未知艺术家",
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        
        # 封面图片
        if pic_url := data.get("picUrl") or data.get("img1v1Url"):
            artist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._build_image_url(pic_url),
                    provider=self.instance_id,
                )
            ]
        
        return artist
    
    def _parse_playlist(self, data: dict) -> Playlist:
        """解析 API 返回的歌单数据为 Playlist 对象。"""
        playlist_id = str(data["id"])
        
        playlist = Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=data.get("name") or "未知歌单",
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        
        # 创建者
        if creator := data.get("creator"):
            playlist.owner = self._safe_text(creator.get("nickname"), "")
        
        # 封面图片
        if pic_url := data.get("coverImgUrl"):
            playlist.metadata.images = [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=self._build_image_url(pic_url),
                    provider=self.instance_id,
                )
            ]
        
        return playlist

    def _normalize_playlist_data(self, data: dict, default_owner: str) -> dict:
        """标准化不同接口返回的歌单字段。"""
        normalized = dict(data)
        if not normalized.get("coverImgUrl") and normalized.get("picUrl"):
            normalized["coverImgUrl"] = normalized["picUrl"]
        if not normalized.get("creator"):
            normalized["creator"] = {"nickname": default_owner}
        return normalized
    
    # ========== 获取详情 ==========
    
    async def _get_real_audio_quality(self, item_id: str) -> AudioFormat:
        """获取歌曲的真实音质信息。"""
        quality_config = self.config.get_value(CONF_AUDIO_QUALITY)
        # 默认值
        content_type = ContentType.UNKNOWN
        sample_rate = 44100
        bit_depth = 16
        bit_rate = 128000
        
        try:
            # 调用 /song/url/v1 获取详细信息
            # 注意：这里只尝试一次用户配置的音质，不进行复杂的降级/解灰逻辑
            # 因为这只是为了显示元数据，如果播放时不可用，get_stream_details 会处理
            data = await self._api_request(
                "/song/url/v1",
                {"id": item_id, "level": quality_config},
            )
            
            if data.get("code") == 200 and data.get("data"):
                song_data = data["data"][0]
                
                # 解析格式
                file_type = str(song_data.get("type", "")).lower()
                br = song_data.get("br", 0)
                sr = song_data.get("sr", 0)
                
                if file_type == "mp3":
                    content_type = ContentType.MP3
                elif file_type == "flac":
                    content_type = ContentType.FLAC
                    # 简单的位深度推断
                    if br > 1000000:
                        bit_depth = 24
                elif file_type == "m4a":
                    content_type = ContentType.AAC
                
                if br:
                    bit_rate = br
                if sr:
                    sample_rate = sr
                    
        except Exception as e:
            _LOGGER.warning("获取真实音质失败: %s", e)
            
        return AudioFormat(
            content_type=content_type,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
            bit_rate=bit_rate // 1000 if bit_rate else None, # kbps
        )

    async def get_track(self, prov_track_id: str) -> Track:
        """获取单曲详情。"""
        data = await self._api_request("/song/detail", {"ids": prov_track_id})
        if data.get("code") == 200 and data.get("songs"):
            track = self._parse_track(data["songs"][0])
            # 获取并更新真实音质
            if track.provider_mappings:
                audio_format = await self._get_real_audio_quality(prov_track_id)
                for mapping in track.provider_mappings:
                    mapping.audio_format = audio_format
            return track
        raise ValueError(f"歌曲不存在: {prov_track_id}")
    
    async def get_album(self, prov_album_id: str) -> Album:
        """获取专辑详情。"""
        data = await self._api_request(f"/album?id={prov_album_id}")
        if data.get("code") == 200 and data.get("album"):
            return self._parse_album(data["album"])
        raise ValueError(f"专辑不存在: {prov_album_id}")
    
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """获取歌手详情。"""
        data = await self._api_request(f"/artists?id={prov_artist_id}")
        if data.get("code") == 200 and data.get("artist"):
            return self._parse_artist(data["artist"])
        raise ValueError(f"歌手不存在: {prov_artist_id}")
    
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """获取专辑中的所有歌曲。"""
        data = await self._api_request(f"/album?id={prov_album_id}")
        if data.get("code") == 200 and data.get("songs"):
            return [self._parse_track(song) for song in data["songs"]]
        return []
    
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """获取歌手的专辑列表。"""
        data = await self._api_request(f"/artist/album?id={prov_artist_id}&limit=50")
        if data.get("code") == 200 and data.get("hotAlbums"):
            return [self._parse_album(album) for album in data["hotAlbums"]]
        return []
    
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """获取歌单详情。"""
        if prov_playlist_id == PLAYLIST_ID_DAILY:
            playlist = Playlist(
                item_id=PLAYLIST_ID_DAILY,
                provider=self.instance_id,
                name="📅 每日推荐",
                owner="云音乐",
                provider_mappings={
                    ProviderMapping(
                        item_id=PLAYLIST_ID_DAILY,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                    )
                },
            )
            # 动态获取封面（使用第一首歌的封面）
            try:
                songs = await self._get_daily_recommend_songs()
                if songs:
                    # 找到第一首有封面的歌
                    for song in songs:
                        if song.metadata.images:
                            if not playlist.metadata:
                                playlist.metadata = MediaItemMetadata()
                            playlist.metadata.images = song.metadata.images
                            _LOGGER.debug("每日推荐封面已设置为: %s", song.name)
                            break
            except Exception as e:
                _LOGGER.warning("获取每日推荐封面失败: %s", e)
            return playlist

        data = await self._api_request(f"/playlist/detail?id={prov_playlist_id}")
        if data.get("code") == 200 and data.get("playlist"):
            return self._parse_playlist(data["playlist"])
        raise ValueError(f"歌单不存在: {prov_playlist_id}")
    
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """获取歌手热门 50 首歌曲。"""
        data = await self._api_request(f"/artist/top/song?id={prov_artist_id}")
        if data.get("code") != 200:
            return []
            
        # 兼容不同的字段名
        songs = data.get("songs") or data.get("hotSongs") or []
        if songs:
            return [self._parse_track(song) for song in songs]
            
        _LOGGER.warning("歌手热门歌曲为空或字段解析失败: %s", data.keys())
        return []

    def _normalize_track_payload(self, song: dict[str, Any]) -> dict[str, Any]:
        """兼容不同接口的歌曲字段，统一为 _parse_track 可解析格式。"""
        normalized = dict(song)
        if "ar" not in normalized and "artists" in normalized:
            normalized["ar"] = normalized["artists"]
        if "al" not in normalized and "album" in normalized:
            normalized["al"] = normalized["album"]
        if "dt" not in normalized and "duration" in normalized:
            normalized["dt"] = normalized["duration"]
        return normalized

    def _build_image_url(self, image_url: str) -> str:
        """根据全局配置构建封面图片 URL。"""
        size = self._image_size
        if not image_url or size == "original":
            return image_url
        separator = "&" if "?" in image_url else "?"
        return f"{image_url}{separator}param={size}y{size}"

    async def _get_similar_song_tracks(self, prov_track_id: str) -> list[Track]:
        """主路径：通过当前歌曲获取相似歌曲。"""
        data = await self._api_request("/simi/song", {"id": prov_track_id})
        if data.get("code") != 200:
            _LOGGER.warning("Failed to get similar songs: code=%s", data.get("code"))
            return []

        songs = data.get("songs") or []
        result: list[Track] = []
        for song in songs:
            if not isinstance(song, dict) or "id" not in song:
                continue
            try:
                result.append(self._parse_track(self._normalize_track_payload(song)))
            except Exception as err:
                _LOGGER.debug("Failed to parse similar song payload: %s", err)
        return result

    async def _get_similar_playlist_tracks(self, prov_track_id: str, limit: int) -> list[Track]:
        """补量路径：只取一个相似歌单并抽取歌曲。"""
        data = await self._api_request("/simi/playlist", {"id": prov_track_id})
        if data.get("code") != 200:
            return []

        playlists = data.get("playlists") or []
        playlist_id = ""
        for playlist in playlists:
            temp_id = str(playlist.get("id", ""))
            if temp_id:
                playlist_id = temp_id
                break
        if not playlist_id:
            return []

        target = max(1, min(limit, 50))
        result: list[Track] = []
        seen_ids: set[str] = set()

        tracks_data = await self._api_request(
            "/playlist/track/all",
            {"id": playlist_id, "limit": target, "offset": 0},
        )
        songs = tracks_data.get("songs") or []
        for song in songs:
            if not isinstance(song, dict) or "id" not in song:
                continue
            item_id = str(song["id"])
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            try:
                result.append(self._parse_track(self._normalize_track_payload(song)))
            except Exception as err:
                _LOGGER.debug("Failed to parse track from similar playlist: %s", err)
                continue
            if len(result) >= target:
                return result

        return result

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """
        为 MA 的 DSTM/Radio 提供下一批歌曲。

        策略：
        1) 如果命中最近歌单上下文：优先 /simi/playlist
        2) 否则优先 /simi/song
        3) 另一条路径用于补量
        4) 不使用 /personal_fm，避免语义偏差
        """
        target = max(1, min(limit, 100))
        seed_id = str(prov_track_id)
        result: list[Track] = []
        seen_ids: set[str] = {seed_id}

        prefer_playlist = seed_id in self._playlist_context_set
        if prefer_playlist:
            primary_tracks = await self._get_similar_playlist_tracks(seed_id, target)
            fallback_tracks = await self._get_similar_song_tracks(seed_id)
        else:
            primary_tracks = await self._get_similar_song_tracks(seed_id)
            fallback_tracks = await self._get_similar_playlist_tracks(seed_id, target)

        for track in primary_tracks:
            item_id = str(track.item_id)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            result.append(track)
            if len(result) >= target:
                return result

        for track in fallback_tracks:
            item_id = str(track.item_id)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            result.append(track)
            if len(result) >= target:
                break

        return result
    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        获取音频流详情。
        
        策略：官方优先 + 解灰兜底
        1. 尝试官方源 (支持音质降级)
        2. 如果是试听片段或无 URL，尝试解灰 (source=pyncmd,bodian,kuwo)
        """
        # 1. 尝试官方源
        quality_config = self.config.get_value(CONF_AUDIO_QUALITY)
        # 音质从高到低排序
        all_levels = [
            "jymaster", "dolby", "sky", "jyeffect", 
            "hires", "lossless", "exhigh", "higher", "standard"
        ]
        try:
            start_index = all_levels.index(quality_config)
            levels = all_levels[start_index:]
        except ValueError:
            # 默认或无效值处理，默认从 exhigh 开始
            levels = ["exhigh", "higher", "standard"]
            
        _LOGGER.debug("尝试音质列表 (config=%s): %s", quality_config, levels)
        
        song_data = None
        url = None
        is_free_trial = False
        
        for level in levels:
            data = await self._api_request(
                "/song/url/v1",
                {"id": item_id, "level": level},
            )
            
            if data.get("code") == 200 and data.get("data"):
                temp_data = data["data"][0]
                temp_url = temp_data.get("url")
                
                # 检查是否为试听片段
                if free_trial := temp_data.get("freeTrialInfo"):
                    _LOGGER.warning("检测到试听片段 (level=%s): %s", level, free_trial)
                    is_free_trial = True
                    # 保存试听版数据作为兜底
                    if not song_data:
                        song_data = temp_data
                        url = temp_url
                    # 继续尝试更低音质，看是否有完整版
                    continue
                
                # 获取到完整版 URL
                if temp_url:
                    _LOGGER.debug("获取官方完整版链接成功 (level=%s): %s", level, temp_url)
                    song_data = temp_data
                    url = temp_url
                    is_free_trial = False
                    break
        
        # 2. 如果无 URL 或为试听片段，尝试解灰
        if not url or is_free_trial:
            _LOGGER.info("歌曲 %s 需要解灰（试听限制或无URL），尝试解灰源...", item_id)
            try:
                # 调用解灰接口
                unblock_data = await self._api_request(
                    "/song/url/match",
                    {"id": item_id, "source": "pyncmd,bodian,kuwo"}
                )
                
                if unblock_data.get("code") == 200 and unblock_data.get("data"):
                    match_data = unblock_data["data"]
                    match_url = match_data.get("url")
                    
                    if match_url:
                        _LOGGER.info("🎉 解灰成功！使用解灰源 URL: %s", match_url)
                        # 更新数据
                        url = match_url
                        # 构造一个模拟的 song_data，因为 match 接口返回结构可能不同
                        # 优先使用 match 接口返回的元数据，缺失的用官方试听版的数据补全
                        if not song_data:
                            song_data = {}
                        
                        song_data["url"] = match_url
                        song_data["br"] = match_data.get("br", song_data.get("br", 128000))
                        song_data["type"] = match_data.get("type", song_data.get("type", "mp3"))
                        song_data["size"] = match_data.get("size", song_data.get("size", 0))
                        song_data["md5"] = match_data.get("md5", song_data.get("md5", ""))
                        # 解灰成功后，不再视为试听
                        is_free_trial = False
                    else:
                        _LOGGER.warning("解灰接口返回成功但 URL 为空")
                else:
                    _LOGGER.warning("解灰失败: %s", unblock_data)
            except Exception as e:
                _LOGGER.exception("解灰过程发生异常: %s", e)
        
        # 3. 最终检查
        if not url:
            _LOGGER.warning("歌曲无可用播放链接 (解灰也失败): %s", item_id)
            raise ValueError(f"歌曲无可用播放链接: {item_id}")
        
        if is_free_trial:
            _LOGGER.warning("最终只能播放试听片段: %s", item_id)
        
        # 解析音频格式
        content_type = ContentType.UNKNOWN
        file_type = str(song_data.get("type", "")).lower()
        bit_depth = 16
        bit_rate = song_data.get("br", 0)  # bps
        
        if file_type == "mp3":
            content_type = ContentType.MP3
        elif file_type == "flac":
            content_type = ContentType.FLAC
            if bit_rate > 1000000:
                bit_depth = 24
        elif file_type == "m4a":
            content_type = ContentType.AAC
        
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                sample_rate=song_data.get("sr", 44100),
                bit_depth=bit_depth,
                bit_rate=bit_rate // 1000 if bit_rate else None,  # kbps
            ),
            stream_type=StreamType.HTTP,
            path=url,
            # 注意：解灰接口可能不返回 time，如果 song_data 是解灰构造的，可能缺 time
            # 如果之前获取过官方试听版，song_data 中会有 time (试听版时长?)
            # 最好还是用 Track 对象的 duration，但这里拿不到 Track 对象
            # 暂时信任 song_data 中的 time，如果没有则为 None (MA 会自己处理)
            duration=song_data.get("time", 0) // 1000 if song_data.get("time") else None,
        )
    
    # ========== 用户库 ==========
    
    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """获取歌单列表（用于库/列表视图，保持平铺聚合）。"""
        # 获取用户信息
        user_data = await self._api_request("/user/account")
        if user_data.get("code") != 200 or not user_data.get("account"):
            _LOGGER.warning("未登录或获取用户信息失败")
            return
        
        uid = user_data["account"]["id"]
        
        # 获取用户歌单
        data = await self._api_request(f"/user/playlist?uid={uid}")
        user_playlists = data.get("playlist", []) if data.get("code") == 200 else []
        if data.get("code") != 200:
            _LOGGER.warning("获取用户歌单失败: code=%s", data.get("code"))
        
        emitted_ids: set[str] = set()

        # 1. 插入虚拟的“每日推荐”歌单
        daily_playlist = await self._build_daily_playlist()
        emitted_ids.add(daily_playlist.item_id)
        yield daily_playlist

        # 2. 插入平台发现歌单（推荐/排行榜/精品）
        discovery_playlists = await self._get_discovery_playlists("all")
        for playlist in discovery_playlists:
            if playlist.item_id in emitted_ids:
                continue
            emitted_ids.add(playlist.item_id)
            yield playlist

        # 3. 用户歌单
        for pl in user_playlists:
            playlist = self._parse_playlist(pl)
            if playlist.item_id in emitted_ids:
                continue
            emitted_ids.add(playlist.item_id)
            yield playlist

    async def _build_daily_playlist(self) -> Playlist:
        """构建虚拟每日推荐歌单。"""
        daily_playlist = Playlist(
            item_id=PLAYLIST_ID_DAILY,
            provider=self.instance_id,
            name="📅 每日推荐",
            owner="云音乐",
            provider_mappings={
                ProviderMapping(
                    item_id=PLAYLIST_ID_DAILY,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        try:
            songs = await self._get_daily_recommend_songs()
            if songs:
                for song in songs:
                    if song.metadata.images:
                        if not daily_playlist.metadata:
                            daily_playlist.metadata = MediaItemMetadata()
                        daily_playlist.metadata.images = song.metadata.images
                        break
        except Exception as e:
            _LOGGER.warning("获取每日推荐封面失败: %s", e)
        return daily_playlist

    def _is_private_radar_playlist(self, playlist: Playlist) -> bool:
        """判断是否为每日更新类的私人雷达歌单。"""
        return "私人雷达" in (playlist.name or "")

    async def _get_user_playlists(self, mode: str = "all") -> list[Playlist]:
        """获取当前账号用户歌单，并按场景过滤。"""
        user_data = await self._api_request("/user/account")
        if user_data.get("code") != 200 or not user_data.get("account"):
            _LOGGER.warning("未登录或获取用户信息失败")
            return []
        uid = user_data["account"]["id"]
        data = await self._api_request(f"/user/playlist?uid={uid}")
        if data.get("code") != 200:
            _LOGGER.warning("获取用户歌单失败: code=%s", data.get("code"))
            return []
        playlists = data.get("playlist", [])
        parsed_playlists = [self._parse_playlist(pl) for pl in playlists]

        if mode == "daily":
            return [pl for pl in parsed_playlists if self._is_private_radar_playlist(pl)]
        if mode == "my":
            return [pl for pl in parsed_playlists if not self._is_private_radar_playlist(pl)]
        return parsed_playlists

    async def _get_discovery_playlists(self, category: str = "all") -> list[Playlist]:
        """获取平台发现内容中的歌单列表。"""
        result: list[Playlist] = []
        seen_ids: set[str] = set()

        async def _append_playlists(items: list[dict], owner: str, limit: int) -> None:
            count = 0
            for item in items:
                if count >= limit:
                    break
                try:
                    playlist = self._parse_playlist(self._normalize_playlist_data(item, owner))
                except Exception as err:
                    _LOGGER.debug("解析发现歌单失败: %s", err)
                    continue
                if playlist.item_id in seen_ids:
                    continue
                seen_ids.add(playlist.item_id)
                result.append(playlist)
                count += 1

        if category in ("all", "recommend"):
            recommend_data = await self._api_request("/personalized", {"limit": 12})
            recommend_items = (
                recommend_data.get("result", []) if recommend_data.get("code") == 200 else []
            )
            _LOGGER.debug(
                "发现歌单接口 /personalized: code=%s, count=%s",
                recommend_data.get("code"),
                len(recommend_items),
            )
            await _append_playlists(recommend_items, DISCOVERY_OWNER_RECOMMEND, 12)

        if category in ("all", "toplist"):
            toplist_data = await self._api_request("/toplist/detail")
            toplist_items = toplist_data.get("list", []) if toplist_data.get("code") == 200 else []
            _LOGGER.debug(
                "发现歌单接口 /toplist/detail: code=%s, count=%s",
                toplist_data.get("code"),
                len(toplist_items),
            )
            await _append_playlists(toplist_items, DISCOVERY_OWNER_TOPLIST, 12)

        if category in ("all", "high_quality"):
            hq_data = await self._api_request("/top/playlist/highquality", {"limit": 12})
            hq_items = hq_data.get("playlists", []) if hq_data.get("code") == 200 else []
            _LOGGER.debug(
                "发现歌单接口 /top/playlist/highquality: code=%s, count=%s",
                hq_data.get("code"),
                len(hq_items),
            )
            await _append_playlists(hq_items, DISCOVERY_OWNER_HQ, 12)

        _LOGGER.debug("发现歌单最终合并数量: category=%s, count=%s", category, len(result))

        return result
    
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """获取歌单中的所有歌曲（支持分页）。"""
        if prov_playlist_id == PLAYLIST_ID_DAILY:
            return await self._get_daily_recommend_songs(page)

        # 限制每次获取的数量，模拟分页
        limit = 50
        offset = page * limit
        
        # 注意：/playlist/track/all 接口实际上是一次性返回所有歌曲
        # 为了符合 MA 的分页逻辑，我们需要在内存中切片
        # 或者使用 /playlist/track/all?id={id}&limit={limit}&offset={offset} (如果支持)
        # 经查，/playlist/track/all 支持 limit 和 offset
        
        data = await self._api_request(
            "/playlist/track/all",
            {"id": prov_playlist_id, "limit": limit, "offset": offset}
        )
        
        if data.get("code") != 200 or not data.get("songs"):
            # 尝试不带分页参数请求（兼容旧版或特定接口行为）
            if page == 0:
                data = await self._api_request(f"/playlist/track/all?id={prov_playlist_id}")
            else:
                return []
        
        songs = data.get("songs", [])
        if not songs:
            return []
            
        # 如果接口不支持分页返回了所有数据，我们需要手动切片
        if len(songs) > limit:
            start = page * limit
            end = start + limit
            songs = songs[start:end]

        tracks: list[Track] = []
        skipped_count = 0
        skipped_samples: list[dict[str, Any]] = []
        for song in songs:
            if not isinstance(song, dict):
                skipped_count += 1
                if len(skipped_samples) < 3:
                    skipped_samples.append({"song_id": None, "error": "song is not a dict"})
                continue
            try:
                tracks.append(self._parse_track(self._normalize_track_payload(song)))
            except Exception as err:
                skipped_count += 1
                if len(skipped_samples) < 3:
                    artists = song.get("ar") or song.get("artists") or []
                    artist_names = []
                    for artist in artists:
                        if isinstance(artist, dict):
                            artist_names.append(artist.get("name"))
                    skipped_samples.append(
                        {
                            "song_id": song.get("id"),
                            "name": song.get("name"),
                            "artists": artist_names,
                            "error": type(err).__name__,
                        }
                    )

        if skipped_count:
            _LOGGER.warning(
                "Playlist parse skipped invalid songs: playlist_id=%s, page=%s, skipped=%s, returned=%s, samples=%s",
                prov_playlist_id,
                page,
                skipped_count,
                len(tracks),
                skipped_samples,
            )

        self._remember_playlist_context(tracks)
        return tracks

    async def _get_daily_recommend_songs(self, page: int = 0) -> list[Track]:
        """获取每日推荐歌曲。"""
        # 每日推荐通常只有 30 首左右，不支持分页，一次性返回
        if page > 0:
            return []
            
        data = await self._api_request("/recommend/songs")
        if data.get("code") != 200:
            _LOGGER.warning("获取每日推荐失败: %s", data)
            return []
            
        songs = data.get("data", {}).get("dailySongs", [])
        return [self._parse_track(song) for song in songs]

