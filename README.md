# MA NCloud Music

Music Assistant 原生音乐提供者插件，通过第三方 API 提供云音乐服务。

##  功能

-  搜索歌曲、专辑、歌手、歌单
-  用户歌单支持
-  二维码扫码登录

##  安装

### Docker 部署

将 `ncloud_music` 目录映射到 MA 容器的 providers 目录：

```yaml
# docker-compose.yml
services:
  music-assistant:
    volumes:
      - ./ncloud_music:/app/venv/lib/python3.13/site-packages/music_assistant/providers/ncloud_music
```

重启 MA 服务后，在设置中添加 "NCloud Music" 提供者。

##  配置

1. **API 地址**：填写第三方 API 服务器地址
2. **扫码登录**：点击按钮扫码登录

##  依赖

- Music Assistant 2.x
- 第三方 API 服务

## 📄 许可证

MIT License
