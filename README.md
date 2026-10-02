# Neko 生图

MaiBot 生图插件，只保留两条路：

- OpenAI 兼容接口。先请求 `/v1/images/generations`，失败再请求 `/v1/chat/completions`。很多中转的 `gpt-image` 只通其中一条。
- Gemini `generateContent`。需要图片模型，例如 `gemini-3.1-flash-image-preview`。

`general.provider` 失败后会试 `fallback_provider`。Gemini 官方地址在国内服务器上默认走 `http://127.0.0.1:7890`。

麦麦在用户要求画图时调用工具 `neko_draw`，问进度时调用 `neko_draw_status`。白名单里的人也可以发 `/draw 一只猫`，或者 `/draw 状态`。名单为空时这两条命令不生效。

画自己或自画像时，先用 `query_memory` 查长期记忆里的外貌，再写成一段完整的话。外貌不从人格设定里取，也不要编。画别人时按当前对话写。`masterpiece`、`1girl` 和一串逗号短词会被退回。

接口返回 HTTP 400 时不重试，也不改走另一个接口。下一轮规划会收到反馈：这一般是提示词违规，不要用同一句再试，改写后再调用 `neko_draw`。

同一个接口遇到超时、429 或 5xx 会按 `retry_times` 重试，等待时间从 `retry_delay_seconds` 起翻倍。OpenAI 的 `auto` 模式会记住上次成功的路径，下次先走那条。同一聊天里上一张没画完时不会再开新任务。

复制 `config.example.toml` 为 `config.toml` 后填写密钥。不要把 `config.toml` 提交进仓库。

## 安装

```bash
git clone https://github.com/znzsofficial/neko-draw.git plugins/neko-draw
```

MaiBot 1.2+，插件 SDK 2.x，Python 3.10+。许可证是 MIT。
