# 多平台云端视频总结助手

本环境专用于视频总结。在已配置的 Codex Cloud 环境发送单条 B站、YouTube 或 Vimeo 链接（含 B站 `b23.tv` 分享短链接或 BV 号），即默认执行完整流程，无需再写“总结”或指定命令：

**单视频链接 → 复用已有成功来源 → 优先获取平台字幕 → 有字幕直接生成报告；字幕不可用或明确需登录时，提取独立音频直链 → 百炼 ASR → 报告；直链任务明确失败后，最多一次下载/校验音频 → 百炼临时上传 → ASR。**

用户明确要求只检查、只解释、只用字幕或不收费时遵守当次限制。不能用网页简介、搜索摘要或章节目录代替完整来源；未拿到来源时报告实际阻断，不能宣称已完成视频总结。

云环境安装脚本和启动技能见 [cloud/install.sh](cloud/install.sh)、[cloud/start-skill.md](cloud/start-skill.md)。云任务可能从 `/workspace` 开始，安装脚本会在该目录的 `AGENTS.md` 中保存项目入口，保留其他已有规则；仅把指令放在仓库子目录并不足以验证新会话实际读到了它。配置保存、发布和新任务验收见 [DEPLOY.md](DEPLOY.md)。

默认 source-strategy=auto，ASR 输入方式为 `direct-url`；standard 模式、最长 **300 分钟**、音频最大 **1024 MiB**。不下载视频画面，不需要自有 OSS 或额外报告模型 API Key。新直链任务明确 FAILED 后最多回退一次 `bailian-upload`；提交未知不重提，上传回退失败后停止。实际验收、授权直链尝试与免费检查见 [VALIDATION.md](VALIDATION.md)。

平台实际支持状态、样本和阻断见 [PLATFORMS.md](PLATFORMS.md)。B站已完成实际端到端；YouTube/Vimeo 已接入代码，但当前环境分别遇代理 CONNECT 403 和提取器登录要求，尚未通过线上音频与 ASR 验收。抖音、TikTok、西瓜仅预留入口，不开放处理。

字幕研究、样本与使用边界见 [CAPTIONS.md](CAPTIONS.md)。YouTube 支持人工/平台自动字幕，B站按分 P 的 cid 查询字幕目录，弹幕不当字幕；匿名访问可能拿不到已有字幕。仅字幕明确需登录时保留 restricted 证据，auto 按既有费用授权进入公开音频 ASR；subtitles-only 仍停止。请求被阻断或其他访问限制不自动回退。质量诊断记录覆盖、空档、重叠和重复，低覆盖不等于字幕缺失。参考对照见 [REFERENCE_COMPARISON.md](REFERENCE_COMPARISON.md)。

## 初始化

需要 Python 3.12+，在仓库根目录运行：

```bash
bash setup.sh
```

脚本检查 Python，必要时通过 apt 安装 FFmpeg/FFprobe 与 CA 证书（需 root 或免交互 sudo），创建 `.venv`、安装固定版本 Python 依赖、检查依赖并运行离线测试。YouTube 还安装 Deno 2.9.8 与 yt-dlp-ejs 0.8.0；HLS AES 使用 pycryptodomex 3.24.1，均写入 requirements。无需另装 Node，无需运行时拉取远程 JS 组件。不支持 apt 或没有安装权限时明确停止，须由环境管理员在镜像中预装系统包。

系统 CA 默认 `/etc/ssl/certs/ca-certificates.crt`。初始化和程序入口使用该信任库，设置 `SSL_CERT_FILE`、`REQUESTS_CA_BUNDLE`，保留显式配置的有效证书路径。yt-dlp 子进程使用官方 `--compat-options no-certifi` 并继承证书/代理设置，始终验证 TLS。代理 CA 须加入系统信任库，禁止关闭证书验证。

保留已有安全绑定：`DASHSCOPE_API_KEY` 放 Network secrets，仅允许百炼 API 域名；`BAILIAN_WORKSPACE_ID` 和 `ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans` 放环境变量。业务空间留空使用默认北京 API。程序不自动加载 `.env`，密钥值不写入聊天、源码或初始化脚本。部署与重新发布见 [DEPLOY.md](DEPLOY.md)。

## 使用

选中本项目环境，新会话只需发送：

> 总结这个视频：https://www.bilibili.com/video/BV1gmJX6SExT

[AGENTS.md](AGENTS.md) 规定自动完成转写、阅读、报告和交付；需要速览可加“brief 模式”。脚本准备来源，Codex 按 `vendor/video-report/SKILL.md` 写报告。

```bash
# 默认字幕优先，成功来源优先复用；字幕不可用或仅字幕需登录时进入音频后端
.venv/bin/python video_assistant.py prepare 'BV1gmJX6SExT' --mode standard
# 以下命令使用输出的 directory，不能假定任务路径
.venv/bin/python video_assistant.py resume '任务目录'
# Codex 写好工作稿后，按正文主题命名、生成完整 Markdown、检查并打包
.venv/bin/python video_assistant.py deliver '任务目录' --title '存储芯片的堆叠、键合与国产追赶路径'
# 重新检查完整交付（自动使用状态中保存的报告文件名）
.venv/bin/python video_assistant.py verify '任务目录'
```

默认新任务目录 `runs/<平台>-<视频ID>[-p分P]-<模式>/`（显式 bailian-upload 另加 -bailian-upload 后缀），B站 URL 的 p 参数选择分 P。旧任务目录原样保留，按规范链接复用，不因加平台前缀重复转写。YouTube 接受 watch、youtu.be、shorts/embed 单视频；Vimeo 接受公开数字 ID 或 player 链接。不展开合集、频道或直播。`--max-minutes` 接受 1–300，`--max-audio-mib` 接受 1–1024，可主动收紧。服务端临时上传策略若更小，程序在上传前检查实际文件大小，报告字节上下限并停止，不绕过限制。2026-10-22 只读查询当前模型策略上限为 1,073,741,824 字节（1024 MiB），不保证未来不变。

以下诊断不提交 ASR：

```bash
.venv/bin/python video_assistant.py probe 'BV1gmJX6SExT'
# 仅获取平台字幕，不下载音频，不调用百炼
.venv/bin/python video_assistant.py caption-check '视频链接'
# 严格只接受字幕，拿不到就停止
.venv/bin/python video_assistant.py prepare '视频链接' --source-strategy subtitles-only
# 本地音频下载、转换和完整解码；不上传、不需要百炼密钥
.venv/bin/python video_assistant.py audio-check 'https://www.youtube.com/watch?v=BaW_jenozKc'
# 有效 206 时最多读取 1 KiB，不下载完整媒体
.venv/bin/python video_assistant.py probe 'BV1gmJX6SExT' --check-audio-range
# 按完整 GET 检查公开文件响应，不携带站点请求头，不读取音频正文、不提交 ASR
.venv/bin/python video_assistant.py probe 'BV1LwHv6LEYz' --check-public-audio
# 验证完整独立音频下载、解码与官方上传回执，随后清理本地音频
.venv/bin/python video_assistant.py upload-check 'BV1gmJX6SExT'
```

audio-check 在 runs/<平台>-<视频ID>[-p分P]-audio-check/audio-check.json 保存脱敏阶段和失败证据，成功/失败均清理临时音频；已有证据不覆盖、不自动重复检查。

默认直链模式为 `prepare 视频链接`（等同 --input-mode direct-url）。直链仅支持单文件；分段音轨预检后直接进入上传模式，不提交无效直链任务；它把音频直链交给百炼，不假定云端 User-Agent/Referer 会传给百炼。此前一次实际直链任务 `FILE_DOWNLOAD_FAILED`，临时上传模式随后完成实际转写与报告；云端取流成功不能证明百炼也能取流。

直链模式省去本地媒体下载和临时上传，但要求百炼能在签名有效期内独立读取完整、受支持格式的音频。需 Referer/Cookie、出口受限或分段清单的地址不保证可用，平台网页 URL 不能替代音频文件 URL。新直链任务明确 FAILED 后自动回退一次下载上传 ASR；先归档原任务 ID/终态响应，再持久化回退检查点，重新解析新鲜音轨并下载校验。显式 --input-mode bailian-upload 可跳过直链；`--no-upload-fallback` 将本次限定为一个直链 ASR 任务，失败即停，不产生第二个上传回退任务。该新调度流程仅离线验收，历史直链失败与上传成功证据保持不变。

`probe --check-public-audio` 是可选的免费公开 GET 诊断，不作为默认直链收费提交的门槛；无 Range GET 被拒不能推断正常 Range 读取或百炼取流也失败。该检查不读取音频正文，也不能证明百炼出口可用，和 Range 检查互斥，默认 1024 MiB 上限可用 `--max-audio-mib` 收紧。默认流程仍为 direct-url → 百炼任务明确 FAILED 后最多一次下载上传。历史 `DIRECT_URL_BLOCKED` / `DIRECT_URL_CHECKING` 状态保留、不自动重试。

B站提取保留播放 API 的音轨备用地址。原主地址为 `upos-sz-mirrorcosov.bilivideo.com` 时，优先选择 API 为该音轨提供的 HTTPS Akamai 原始地址，要求相同资源路径、无凭据；不猜测主机、不编辑签名。主地址已为 Akamai 时原样保留，缺少有效备用地址时保留原地址。只记录 CDN 主机和选择来源，不记录签名 URL。

B站现在默认优先 AAC `30216`：已有成功来源 → 平台字幕 → 30216 音频直链 ASR → 明确 FAILED 后最多一次同音轨下载、完整校验、官方临时上传及 ASR。直链路径省去云端本地音频下载和上传；上传仅作为符合持久化策略的回退，不对两个后端同时提交。缺少 30216 时，在本次解析的音轨中依次选 30232、其他独立 AAC。

`prepare`、`probe`、`audio-check`、`upload-check` 也可显式传 `--bilibili-audio-format 30216` 或 `30232`；显式指定音轨不存在时停止，不自动替换。prepare 保存实际选中的 30216/30232，上传回退沿用；旧任务恢复沿用既有选择或原来源音轨，避免默认值更新导致换轨。该选项不用于其他平台，已有成功来源仍优先复用，不为改选轨重新计费。

依据是三个实测样本：109.6 分钟 Git/GitHub 视频的 30216 已完整下载、解码并官方上传（HTTP 200、校验和一致，未执行新的上传 ASR）；48.1 分钟 AI 概念视频和 24.9 分钟 AI 编程流程视频的 30216 已分别完成云端直链转写与报告。此前 30232 的失败原因没有被单独控制变量证实，不能推断所有 30216 都成功或仅文件大小决定结果。详细证据见 VALIDATION.md；未知提交不重提，单次直链授权用 `--no-upload-fallback`。

```bash
# 免费检查指定音轨；不提交 ASR
.venv/bin/python video_assistant.py audio-check 'BV1LwHv6LEYz' --bilibili-audio-format 30216 --output runs/cloud-30216-audio-check
# 官方临时上传检查；不提交 ASR，已有目录不重试
.venv/bin/python video_assistant.py upload-check 'BV1LwHv6LEYz' --bilibili-audio-format 30216 --output runs/cloud-30216-upload-check
```

单文件独立音轨使用连续 Range 下载：常规范围每段 1 MiB，初始探测与最后约 2 MiB 使用至多 32 KiB 小范围，同一下载过程不变更 URL。请求使用原媒体公开头、identity 编码、关闭持久连接并要求缓存重新验证。每段严格校验范围起止、总长度、Content-Length 和可用的 ETag，拒绝缺段、重复段、内容变更和中途退回整文件响应。首个请求若返回完整 HTTP 200 也可接受。仅传输超时/断连允许回滚并重试相同分块一次，整个文件最多三次网络重试；HTTP 拒绝、完整性错误、上传或 ASR 提交不适用。所有字节拼接完成后仍执行时长、SHA-256 与完整解码检查。

下载及公开 GET 诊断错误最多检查 8 KiB 文本，只保留错误分类、固定文本标记、SHA-256、媒体类型和经过格式校验的 EdgeOne 请求 ID。567 加 EdgeOne 响应证据标记为安全拦截；单凭 567 或 `Server: envoy` 不归因。原始 HTML、Cookie、签名直链不落盘。该诊断不会解除 CDN 侧限制，不会换出口或伪造签名；音轨只选择 API 已返回的上述地址，下载途中不自动切换 CDN。

## 恢复、复用与安全

`prepare` 在查询字幕、获取新直链、凭据和计费前寻找可访问的成功任务，检查来源完整性；不同阅读模式也复用原转写。始终使用返回目录，复用不会新建 ASR。`status 目录` 查看脱敏检查点。提交前持久化 `SUBMISSION_UNKNOWN`，拿到任务 ID 立即保存，正常恢复仅查询已保存的任务 ID；仅明确失败可按下述策略回退。

未知提交不得自动重提，由用户从百炼控制台核实原任务，再 `attach-task 目录 原任务ID`。仅带 upload_fallback_allowed 的新直链任务明确 FAILED 时允许一次上传回退，回退期间冻结模型/业务空间和本地限制；resume 可查询原直链后继续这一回退。取消、提交拒绝、查询/结果下载失败不触发回退。历史失败任务、上传失败或中断不自动另建收费任务；其他重试只有用户明确授权后，才用 `--new-attempt --output 新目录`，保留旧证据。该参数不能绕过未知提交。恢复使用创建时冻结的模型和业务空间，当前密钥须能访问原任务。

新会话可能有独立文件系统，不能承诺跨会话自动发现旧任务。结束前下载完整 ZIP；复用时解压回 `runs/`，或用 `prepare 视频链接 --reuse-from 已解压的任务目录`。导入错误、成功来源损坏时停止，不以重新计费替代修复。

B站继续核对 Content-Length、SHA-256、AAC 音轨、时长及完整解码。新平台使用独立音轨格式（包括 Opus、HLS/DASH），由 yt-dlp 原生下载器组装分段，缺片即停止，再按需 remux/transcode 为经过完整校验的单轨 M4A/AAC；无独立音轨则停止，不下载画面。音源和最终文件分别受 1024 MiB 限制。全部校验通过后向当前模型申请官方临时凭证；检查上传 HTTP 200 与 ETag，提交添加 `X-DashScope-OssResourceResolve: enable`。凭证、源签名直链和 `oss://` URI 只在内存使用，API Authorization 仅发往百炼 API。本地临时音频在正常或错误退出时清理。官方临时文件约 48 小时清理，不提供独立查询/下载；`upload-check` 不持久化临时地址，正式转写需重新上传。

页面缺少初始数据时，项目内 `bilibili_extractor.py` 使用详情 API 回退，校验 BV/分 P，再复用固定版本 yt-dlp 播放信息与格式提取，不修改虚拟环境依赖。失败保存脱敏证据后停止，不反复请求。历史网络诊断见 [PROBE_DIAGNOSTICS.md](PROBE_DIAGNOSTICS.md)。

## 默认交付

| 文件 | 用途 |
| --- | --- |
| `内容主题_精读.html` | 项目模板生成的阅读报告 |
| `内容主题_精读.md` | 对应 HTML 的完整阅读报告，含章节、必要表格、时间范围、图示文字和来源标记 |
| `内容主题_精读_字幕.srt` | 完整字幕 |
| `内容主题_精读_完整资料.zip` | 全部报告与原始资料下载包 |

`deliver --title` 接受根据正文提炼的简洁主题；未指定时优先使用报告 H1，再使用视频标题，拒绝 report 等通用标题。文件名清理路径字符并限制长度，brief 使用 `_速览`；TXT 命名为 `内容主题_精读_原始转写.txt`。迁移旧 report 工作稿后，后续 verify/deliver 从 state 中读取实际名称，ZIP 解压后仍可恢复任务。

字幕任务的 TXT 文件后缀为“原始字幕”；ASR 任务为“原始转写”。source_kind=platform_subtitles 时，ZIP 使用真实 caption-raw.json 与 caption-check.json，不生成虚构的 ASR 任务文件。auto 回退 ASR 时保留字幕不可用或需登录的证据。

ASR 任务 ZIP 还包含原始 `transcript.txt`、`transcript.md`、`input.json`、`source-units.json`、`state.json`、`asr-task.json`、脱敏 `asr-raw.json`、上传回退时的 `direct-url-state.json` / `direct-url-asr-task.json`、`report-check.json` 和可用的 `review.json`。**主题命名的 Markdown 是阅读报告，`transcript.md` 是原始转写**，不能互相代替。

`deliver` 检查 Markdown 实际存在且对应 HTML、来源 ID 有效，再逐字节检查归档内容。内容仍须人工核对来源；浏览器视觉验收无法完成时写入 `review.json`，继续静态检查和交付，不声称视觉验收通过。

`runs/` 和临时音频不进入 Git。保留报告、原始资料和任务证据供下载，处理后清理本地音频；临时工作区不是永久资料库。ASR 可能误识别术语，流程无法覆盖只在画面中出现的信息。暂不处理 B站短链接、批量合集、直播或需登录的视频。音频路线无独立音轨时停止；字幕路线不要求存在音轨。YouTube/Vimeo 各自独立适配，不套用 B站仅 AAC 的选择规则。

## 来源

`vendor/video-report/` 保留 [imexlovery/video-report-agent](https://github.com/imexlovery/video-report-agent) 的 Skill、模板和参考文件，提交及校验见 `UPSTREAM.json`。上游快照未发现 LICENSE，本包不额外授予上游许可，公开分发前须核实。

百炼依据：[官方临时存储](https://help.aliyun.com/zh/model-studio/get-temporary-file-url/)、[ASR HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)。
