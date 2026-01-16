"""
NCloud Music Provider for Music Assistant.

通过第三方 API 提供云音乐服务的 MA 原生插件。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp

from music_assistant_models.config_entries import ConfigEntry, ConfigValueType
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
    ItemMapping,
    MediaItemImage,
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

_LOGGER = logging.getLogger(__name__)


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
    
    当用户点击 ACTION 按钮时，action 参数会传入对应的 key。
    """
    # 处理扫码登录 ACTION
    if action == CONF_ACTION_QR_LOGIN:
        await _handle_qr_login(mass, instance_id, values)
    
    # 判断登录状态
    cookie = values.get(CONF_COOKIE, "") if values else ""
    if cookie:
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
            description="第三方 API 服务的完整 URL（例如：http://192.168.1.100:4001）",
            required=True,
            default_value="",
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
        }
    
    async def handle_async_init(self) -> None:
        """Provider 初始化（在 setup 之后调用）。"""
        self._api_url = str(self.config.get_value(CONF_API_URL)).rstrip("/")
        cookie_str = str(self.config.get_value(CONF_COOKIE) or "")
        self._cookies = self._parse_cookie(cookie_str)
        
        _LOGGER.info(
            "NCloud Music Provider 初始化完成 (API: %s, 已登录: %s)",
            self._api_url,
            bool(self._cookies),
        )
    
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
        
        # 解析艺术家
        artists = []
        for ar in data.get("ar", []):
            artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=str(ar["id"]),
                    provider=self.instance_id,
                    name=ar.get("name", "未知艺术家"),
                )
            )
        
        # 解析专辑
        al = data.get("al", {})
        album = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=str(al.get("id", 0)),
            provider=self.instance_id,
            name=al.get("name", "未知专辑"),
        )
        
        # 封面图片
        images = []
        if pic_url := al.get("picUrl"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{pic_url}?param=300y300",
                    provider=self.instance_id,
                )
            )
        
        return Track(
            item_id=track_id,
            provider=self.instance_id,
            name=data.get("name", "未知歌曲"),
            provider_mappings={
                ProviderMapping(
                    item_id=track_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            artists=artists,
            album=album,
            duration=data.get("dt", 0) // 1000,  # 毫秒转秒
            metadata={},
            images=images,
        )
    
    def _parse_album(self, data: dict) -> Album:
        """解析 API 返回的专辑数据为 Album 对象。"""
        album_id = str(data["id"])
        
        # 解析艺术家
        artists = []
        for ar in data.get("artists", []):
            artists.append(
                ItemMapping(
                    media_type=MediaType.ARTIST,
                    item_id=str(ar["id"]),
                    provider=self.instance_id,
                    name=ar.get("name", "未知艺术家"),
                )
            )
        
        # 封面图片
        images = []
        if pic_url := data.get("picUrl"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{pic_url}?param=300y300",
                    provider=self.instance_id,
                )
            )
        
        return Album(
            item_id=album_id,
            provider=self.instance_id,
            name=data.get("name", "未知专辑"),
            provider_mappings={
                ProviderMapping(
                    item_id=album_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            artists=artists,
            images=images,
        )
    
    def _parse_artist(self, data: dict) -> Artist:
        """解析 API 返回的歌手数据为 Artist 对象。"""
        artist_id = str(data["id"])
        
        # 封面图片
        images = []
        if pic_url := data.get("picUrl") or data.get("img1v1Url"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{pic_url}?param=300y300",
                    provider=self.instance_id,
                )
            )
        
        return Artist(
            item_id=artist_id,
            provider=self.instance_id,
            name=data.get("name", "未知艺术家"),
            provider_mappings={
                ProviderMapping(
                    item_id=artist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            images=images,
        )
    
    def _parse_playlist(self, data: dict) -> Playlist:
        """解析 API 返回的歌单数据为 Playlist 对象。"""
        playlist_id = str(data["id"])
        
        # 封面图片
        images = []
        if pic_url := data.get("coverImgUrl"):
            images.append(
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=f"{pic_url}?param=300y300",
                    provider=self.instance_id,
                )
            )
        
        # 创建者
        owner = ""
        if creator := data.get("creator"):
            owner = creator.get("nickname", "")
        
        return Playlist(
            item_id=playlist_id,
            provider=self.instance_id,
            name=data.get("name", "未知歌单"),
            provider_mappings={
                ProviderMapping(
                    item_id=playlist_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
            owner=owner,
            images=images,
        )
    
    # ========== 获取详情 ==========
    
    async def get_track(self, prov_track_id: str) -> Track:
        """获取单曲详情。"""
        data = await self._api_request("/song/detail", {"ids": prov_track_id})
        if data.get("code") == 200 and data.get("songs"):
            return self._parse_track(data["songs"][0])
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
    
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """获取歌单详情。"""
        data = await self._api_request(f"/playlist/detail?id={prov_playlist_id}")
        if data.get("code") == 200 and data.get("playlist"):
            return self._parse_playlist(data["playlist"])
        raise ValueError(f"歌单不存在: {prov_playlist_id}")
    
    # ========== 播放流 ==========
    
    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """
        获取音频流详情。
        
        调用 /song/url/v1 获取播放链接。
        """
        # 获取播放 URL
        data = await self._api_request(
            "/song/url/v1",
            {"id": item_id, "level": "exhigh"},
        )
        
        if data.get("code") != 200 or not data.get("data"):
            raise ValueError(f"获取播放链接失败: {item_id}")
        
        song_data = data["data"][0]
        url = song_data.get("url")
        
        if not url:
            raise ValueError(f"歌曲无可用播放链接: {item_id}")
        
        # 解析音频格式
        content_type = ContentType.UNKNOWN
        file_type = song_data.get("type", "").lower()
        if file_type == "mp3":
            content_type = ContentType.MP3
        elif file_type == "flac":
            content_type = ContentType.FLAC
        elif file_type == "m4a":
            content_type = ContentType.AAC
        
        return StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                sample_rate=song_data.get("sr", 44100),
                bit_depth=song_data.get("br", 320000) // 1000 if song_data.get("br") else 16,
            ),
            stream_type=StreamType.HTTP,
            path=url,
        )
    
    # ========== 用户库 ==========
    
    async def get_library_playlists(self) -> list[Playlist]:
        """获取用户收藏的歌单列表。"""
        # 获取用户信息
        user_data = await self._api_request("/user/account")
        if user_data.get("code") != 200 or not user_data.get("account"):
            _LOGGER.warning("未登录或获取用户信息失败")
            return []
        
        uid = user_data["account"]["id"]
        
        # 获取用户歌单
        data = await self._api_request(f"/user/playlist?uid={uid}")
        if data.get("code") != 200 or not data.get("playlist"):
            return []
        
        return [self._parse_playlist(pl) for pl in data["playlist"]]
    
    async def get_playlist_tracks(self, prov_playlist_id: str) -> list[Track]:
        """获取歌单中的所有歌曲。"""
        data = await self._api_request(f"/playlist/track/all?id={prov_playlist_id}")
        if data.get("code") != 200 or not data.get("songs"):
            return []
        
        return [self._parse_track(song) for song in data["songs"]]
