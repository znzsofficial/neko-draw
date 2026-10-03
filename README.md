# Neko 生图

## 图生图

发图后引用原图发送 `/draw edit 把背景换成海边，保留人物外貌和动作`（也支持 `/draw 图生图 …`）。
自然聊天修改图片时，麦麦调用 `neko_edit_image`，用当前聊天中的真实 `source_message_id` 读取原图或引用图。
找不到原图会提示补发，不会拿文字描述冒充图片，也不会自动选中无关的历史图片。

OpenAI 的 images 模式使用 multipart `/v1/images/edits`，chat 模式使用带 data URL 的多模态消息；
Gemini 原生模式使用 `inlineData`。auto 保留两条路线，但 HTTP 400 不切换端点。
最多 4 张 PNG/JPEG/WebP，每张 10 MiB；继续使用后台任务、状态查询和临时错误退避重试。
图生图直接依据原图，不要求先查长期记忆。旧绘图插件不需要启用。

MaiBot 生图插件，只保留两条路：

- OpenAI 兼容接口。先请求 `/v1/images/generations`，失败再请求 `/v1/chat/completions`。很多中转的 `gpt-image` 只通其中一条。
- Gemini `generateContent`。需要图片模型，例如 `gemini-3.1-flash-image-preview`。

`general.provider` 失败后会试 `fallback_provider`。Gemini 官方地址在国内服务器上默认走 `http://127.0.0.1:7890`。

麦麦在用户要求画图时调用工具 `neko_draw`，问进度时调用 `neko_draw_status`。白名单里的人也可以发 `/draw 一只猫`，或者 `/draw 状态`。名单为空时这两条命令不生效。

画自己或自画像时，先用 `query_memory` 查长期记忆里的外貌，再写成一段完整的话。外貌不从人格设定里取，也不要编。画别人时按当前对话写。`masterpiece`、`1girl` 和一串逗号短词会被退回。

接口返回 HTTP 400 时不重试，也不改走另一个接口。下一轮规划收到接口说明：区分内容审核与模型、尺寸、源图参数错误，不把所有 400 都当成违规。

同一个接口遇到超时、429 或 5xx 会按 `retry_times` 重试，等待时间从 `retry_delay_seconds` 起翻倍。OpenAI 的 `auto` 模式会记住上次成功的路径，下次先走那条。同一聊天里上一张没画完时不会再开新任务。

复制 `config.example.toml` 为 `config.toml` 后填写密钥。不要把 `config.toml` 提交进仓库。

## 安装

```bash
git clone https://github.com/znzsofficial/neko-draw.git plugins/neko-draw
```

MaiBot 1.2+，插件 SDK 2.x，Python 3.10+。许可证是 MIT。
