# bililive_danmu

简易 B 站直播弹幕实时展示。

## 运行

```bash
uv sync
uv run python app.py
```

打开浏览器访问 http://localhost:22333

## 配置

编辑 `config.json`：
- `room_id`：B 站直播间号（短号或长号均可）
- `port`：本地展示端口，默认 22333
