# Muse Chatroom

一个极简的多人聊天室，纯 Python 标准库，无第三方依赖。支持文字、图片、@提醒、引用回复、房间密码。

## 快速开始

```bash
docker compose up -d --build
```

打开浏览器访问 `http://localhost:3007/?room=lobby`，起个昵称就能聊。

## 功能

- 💬 文字聊天，消息持久化（JSONL）
- 🖼️ 图片发送（JPG/PNG/GIF/WebP，10MB 上限，支持 Ctrl+V 粘贴）
- 📎 图文同框：选图后先预览，打字按发送一起发出
- @ 提醒：`@昵称`、`@全体`/`@all`，被 @ 时标题闪烁
- ↩️ 引用回复：点消息旁的回复按钮
- 👥 成员列表：在线状态、最后在线时间
- 📝 本机备注：给成员加备注（只存自己浏览器）
- 🔄 换设备同步：6 位同步码同步昵称和备注
- 🔒 房间密码：每个房间可设独立密码
- 🤖 分身管理：`/admin` 页面可视化管理 AI 分身人设
- 📱 移动端适配

## 配置

环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | 3007 | 监听端口 |
| `DATA_DIR` | /data | 数据目录 |
| `PRESENCE_TTL` | 60 | 在线心跳超时（秒） |

数据存放在 `./data/`（已挂载为 volume，容器重建不丢）：
- `lobby.jsonl` — 聊天记录
- `passwords.json` — 房间密码
- `personas.json` — AI 分身人设
- `sync.json` — 换设备同步码
- `uploads/` — 上传的图片

## API

机器人接入很简单，纯 HTTP：

```bash
# 读消息
GET /api/messages?room=lobby&since=0&nick=你的昵称&pass=房间密码

# 发消息
POST /api/send
{"room":"lobby","nick":"你的昵称","text":"你好","pass":"房间密码"}

# 查是否需要密码
GET /api/needpass?room=lobby
```

## License

MIT
