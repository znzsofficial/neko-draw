# Neko 生图

1.3.0：新增默认关闭的签名Life配图执行端，双方独立授权、会话隔离和持久防重放；详见文末。

1.2.11：移除文生图工具及命令对标签、逗号短语和solo/highres等字样的硬拒绝，接受自然语言/标签/混合描述，不要求列全外貌和场景。**无原图自画像仍要求先query_memory查询外貌**，查到的特征作基础，用户指定新造型作为本次创作设定，不冒称记忆。缩短Planner路由提示及工具/参数重复说明；真实源图、权限、大小、防并发任务和供应商审核不变。不自动重写用户prompt、不降低供应商限制，不保证模型每次查记忆。

1.2.10：Planner自动状态提示改为有限生命周期：排队/执行/重试每次简短刷新；成功/失败自进入终态起15分钟内最多准备一次简短纠正，随后停止。每次先移除本插件标记开头的系统状态项，终态过期、重载/淘汰后也清理，不碰用户消息/工具回执/普通历史。没有记录不再因历史工具名/排队字样反复注入，状态工具明确不可核实；保留最新任务及详细失败原因供neko_draw_status查询，淘汰时清理悬空索引。双items/messages同一次Hook只消耗一次提示，无有效请求列表不消耗。一份提示准备完成不代表RPC成功、模型已看到或用户收到回复，16MiB Hook限制不变；不主动触发新回合、不改图像生成/发送/取图/重试。不保证模型不会从普通历史主动再提旧任务。52项测试通过，覆盖终态一次/900秒边界/清理范围/双字段/跨聊天/新任务/淘汰/发送确认；未付费生成或发QQ验收。

1.2.9：适配MaiBot1.3.6/SDK2.12的`maisaka.context.resolve_image`。模型可把当前上下文里的短消息编号或工具媒体索引传给`source_message_id`，数据库按ID取图没有可读图片时，从**当前聊天**现存上下文读取第0张普通图片；可选`source_image_index`从0开始，只取指定普通图片，不用于emoji表情包。显式索引失败不会改用另一张/缓存/引用。已有真实消息ID多图、表情包、引用与聊天隔离缓存成功时不调用新API；命令仍按当前消息/引用工作。解码、尺寸/像素超限、RPC异常不借新路径绕过原限制，取消传播；新API返回仍需10MiB/4000万像素/格式及动图首帧校验。不会把文字描述或URL当源图，不自动采用无关历史图。该接口提供上下文图片字节，不保证搜索图片是未压缩原图；高清原始下载仍由neko-web_send_image管理。

1.2.9验证：48项插件回归、Ruff F/B通过；另在隔离MaiBot1.3.6真实宿主capability实现上跑4项假数据集成，覆盖工具媒体/短编号、第二张选择、跨聊天拒绝、错误索引不被缓存替代。未调用付费生成接口或主动发QQ，真实聊天最终投递仍需使用验收。

1.2.8复查补齐边界：普通image标签的GIF也走首帧转换，带独立封面的APNG跳过封面取首个动画帧；所有image/emoji源图统一像素解码＋文件校验（JPEG不能只靠verify）。静态PNG/JPEG/WebP校验后仍保留原字节。按消息ID读取时以宿主新字节为准，不把压缩前缓存和压缩后图片重复当两张；RPC读取失败时仅回退当前聊天同消息ID的缓存，不跨聊天猜图、不吞取消。引用最多读取16条消息，避免过度展开；已有引用深度/4张上限保留。38项测试通过（含改图工具接收GIF源图、未调用付费生成）。

1.2.7：改图同时读取普通图片和`emoji`表情包原始图片，支持直接消息ID、引用以及聊天隔离缓存。静态PNG/JPEG/WebP保留原字节；GIF及动图WebP/APNG只取首帧转静态PNG作为源图，不修改整段动画。仅使用宿主提供的原始图片字节，不把表情描述、Unicode表情或QQ face编号当原图。表情包解码校验、最多4000万像素，输入/首帧输出各≤10MiB；Pillow解码在线程中执行。

1.2.6：通用 400 只提示“有时是提示词被拦截”，并允许修改提示词后重新生成。不限制用户要求的重试次数，也不把提示写死。

1.2.3：提交回执明确标为历史快照。每轮规划按当前聊天注入最新任务状态，成功发送后不再沿用“排队中”，普通失败和 HTTP 400 也持续可见；热重载丢失记录时明确标为不可核实。HTTP 400 只报告实际错误证据，`openai_error` 等通用错误明确标为原因未知，不推断源图 ID 或参数错误。状态同步不额外触发聊天或重复发送图片。

1.2.2：配置热重载会等待后台任务取消，并终结尚未启动的排队任务，释放源图。生成结果只接受经过 Pillow 校验的 PNG/JPEG/WebP（单张 16 MiB、4000 万像素以内），API 响应上限 32 MiB。结果 URL 只允许公网 80/443，请求前固定 IP，逐跳验证重定向；直连和 HTTP/HTTPS 代理均保留原域名 TLS 校验。`public_http.py` 与独立的 `neko-web` 仓库保持相同副本。依赖 aiohttp >= 3.12、httpx >= 0.26、Pillow >= 10。

## 图生图

发图片或表情包后引用它发送 `/draw edit 把背景换成海边，保留人物外貌和动作`（也支持 `/draw 图生图 …`）。
自然聊天修改图片时，麦麦调用 `neko_edit_image`，用当前聊天中的真实 `source_message_id` 读取原图或引用图；也接受模型实际可见的短消息编号/工具媒体索引，不得编造编号。需要指定某张普通图片时填可选 `source_image_index`（0为第一张）；上下文回退默认只取第0张，普通完整消息ID未指定索引时保留原多图/引用逻辑。
找不到原图会提示补发，不会拿文字描述冒充图片，也不会自动选中无关的历史图片。

OpenAI 的 images 模式使用 multipart `/v1/images/edits`，chat 模式使用带 data URL 的多模态消息；
Gemini 原生模式使用 `inlineData`。auto 保留两条路线，但 HTTP 400 不切换端点。
最多 4 张 PNG/JPEG/WebP源图（表情包GIF/动图先转首帧PNG），每张10MiB；继续使用后台任务、状态查询和临时错误退避重试。宿主丢失原始文件或未提供二进制时仍须补发；不自行按描述、表情编号或任意URL猜图。

多图取回仍受宿主RPC整帧16MiB限制，单图10MiB/4张上限不代表任何组合都能通过传输；若超限且同聊天/消息ID缓存也不存在，会明确取图失败，需减少图片或重新发送，不擅自修改宿主上限。实际供应商改图与QQ最终发送仍需使用验收，读取成功不代表已生成成功。
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

MaiBot 1.3.6+，插件 SDK 2.12+。许可证是 MIT。
## Life自主场景插画（1.3.0）

可与Neko Life≥0.4配对，默认关闭。Life的`scripts/configure_bridge.py --draw-data-dir <本插件data_dir>`
先预览，再加`--approve`生成独立能力密钥，不复制供应商Key。
双方开关和允许会话均须开启：本插件`life.enabled`/`life.sessions`，Life的`drawing.enabled`/`drawing.sessions`。
`neko.draw.life_v1@1`验证HMAC、目标、时间、nonce及持久请求ID后才启动原有后台生图/发送。
调用者自报ID、未配对、禁用、越权会话、重放或损坏账本均拒绝；重载后已接纳请求不会再次生图。
`life-admission.db`存于私有data_dir，需随密钥备份，不能删除来重试未知工作。
普通`neko_draw`/改图和供应商配置保持不变；Life只生成无人生活环境插画，自画像仍先查询外貌记忆。
无新增每日生图成本限额，Life另保留同场景去重及可配置防刷屏冷却；submitted不是QQ客户端送达证明。
与Life0.4的签名执行、重载防重放及真实供应商生图已验证（私有图片捕获，无额外QQ发送）；
Linux/Windows×Python3.12/3.14完整59项回归通过。提醒终态边界改用绝对deadline比较，避免浮点相减的平台精度偏差。

## CI、离线回归与源码包

GitHub Actions 在 Ubuntu/Windows × Python 3.12/3.14 × SDK `==2.12.0`/`>=2.12,<3` 共8档运行。
SDK 的 PyPI 包名是 `maibot-plugin-sdk`，导入名为 `maibot_sdk`；必须安装真实 SDK，不以 stub 替代。

```sh
python -m pip install 'maibot-plugin-sdk==2.12.0' 'aiohttp>=3.12' 'httpx>=0.26' 'Pillow>=10' tzdata
python scripts/check.py
python scripts/package.py
# 可选：输出到尚不存在的路径，不创建 GitHub Release
python scripts/package.py --output neko-draw.zip
```

门禁要求至少71项测试、0 skip、0失败，防止缺SDK后关键用例被跳过或发现0项仍成功。
`tzdata` 显式安装，避免Windows与Linux时区数据库差异。新增公网下载回归全部使用MockTransport/假会话：
混合公网/内网DNS、空解析、IPv4映射、当前机器地址、逐跳验证、DNS重绑定、重定向上限、固定IP及Host/TLS SNI、
代理传输参数、分块响应限额；不访问测试域名/真实供应商、不付费生成、不发送QQ。共享`public_http.py`未修改。

打包只读取`scripts/package.py`列出的9个固定文件（源码、manifest、MIT许可证、README及公开`config.example.toml`）；
拒绝符号链接，不递归扫描。实际`config.toml`、`.env`、能力密钥、数据库、回执、缓存、音频/图片和其他运行数据不进入ZIP。
测试检查私密哨兵不入包/多余归档成员拒绝；CI仅在临时目录解压并用真实SDK导入、构造插件，不启用/加载宿主服务。
默认包随临时目录删除，不上传artifact、不创建Release、不部署；仅显式`--output`保留本地ZIP。
