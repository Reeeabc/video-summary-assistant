# 多平台云端视频总结助手

在已配置的 Codex Cloud 环境发送单条 B站、YouTube 或 Vimeo 链接并要求总结，默认执行：

**单视频链接 → 按平台解析（B站支持所选分 P）→ 云端下载独立音频 → 按需规范化为 M4A/AAC → 完整性/时长/完整解码校验 → 百炼官方临时上传 → ASR → Codex 生成 HTML 与完整 Markdown 阅读报告。**

默认 `bailian-upload`、standard 模式、最长 **300 分钟**、音频最大 **1024 MiB**。不下载视频画面，不需要自有 OSS 或额外报告模型 API Key。`direct-url` 是手动选项，失败不自动切换模式或另建收费任务。实际验收及本轮非计费检查见 [VALIDATION.md](VALIDATION.md)。

平台实际支持状态、样本和阻断见 [PLATFORMS.md](PLATFORMS.md)。B站已完成实际端到端；YouTube/Vimeo 已接入代码，但当前环境分别遇代理 CONNECT 403 和提取器登录要求，尚未通过线上音频与 ASR 验收。抖音、TikTok、西瓜仅预留入口，不开放处理。

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
# 默认上传模式，成功任务优先复用
.venv/bin/python video_assistant.py prepare 'BV1gmJX6SExT' --mode standard
# 以下命令使用输出的 directory，不能假定任务路径
.venv/bin/python video_assistant.py resume '任务目录'
# Codex 写好工作稿后，按正文主题命名、生成完整 Markdown、检查并打包
.venv/bin/python video_assistant.py deliver '任务目录' --title '存储芯片的堆叠、键合与国产追赶路径'
# 重新检查完整交付（自动使用状态中保存的报告文件名）
.venv/bin/python video_assistant.py verify '任务目录'
```

默认新任务目录 `runs/<平台>-<视频ID>[-p分P]-<模式>-bailian-upload/`，B站 URL 的 p 参数选择分 P。旧任务目录原样保留，按规范链接复用，不因加平台前缀重复转写。YouTube 接受 watch、youtu.be、shorts/embed 单视频；Vimeo 接受公开数字 ID 或 player 链接。不展开合集、频道或直播。`--max-minutes` 接受 1–300，`--max-audio-mib` 接受 1–1024，可主动收紧。服务端临时上传策略若更小，程序在上传前检查实际文件大小，报告字节上下限并停止，不绕过限制。2026-10-22 只读查询当前模型策略上限为 1,073,741,824 字节（1024 MiB），不保证未来不变。

以下诊断不提交 ASR：

```bash
.venv/bin/python video_assistant.py probe 'BV1gmJX6SExT'
# 本地音频下载、转换和完整解码；不上传、不需要百炼密钥
.venv/bin/python video_assistant.py audio-check 'https://www.youtube.com/watch?v=BaW_jenozKc'
# 有效 206 时最多读取 1 KiB，不下载完整媒体
.venv/bin/python video_assistant.py probe 'BV1gmJX6SExT' --check-audio-range
# 验证完整独立音频下载、解码与官方上传回执，随后清理本地音频
.venv/bin/python video_assistant.py upload-check 'BV1gmJX6SExT'
```

audio-check 在 runs/<平台>-<视频ID>[-p分P]-audio-check/audio-check.json 保存脱敏阶段和失败证据，成功/失败均清理临时音频；已有证据不覆盖、不自动重复检查。

手动直链模式为 `prepare 视频链接 --input-mode direct-url`。仅支持单文件直链，分段音轨明确拒绝并提示使用上传模式；它把音频直链交给百炼，不假定云端 User-Agent/Referer 会传给百炼。此前一次实际直链任务 `FILE_DOWNLOAD_FAILED`，临时上传模式随后完成实际转写与报告；云端取流成功不能证明百炼也能取流。

## 恢复、复用与安全

`prepare` 在获取新直链、凭据和计费前寻找可访问的成功任务，检查来源完整性；不同阅读模式也复用原转写。始终使用返回目录，复用不会新建 ASR。`status 目录` 查看脱敏检查点。提交前持久化 `SUBMISSION_UNKNOWN`，拿到任务 ID 立即保存，后续仅查询原任务。

未知提交不得自动重提，由用户从百炼控制台核实原任务，再 `attach-task 目录 原任务ID`。失败任务不自动另建收费任务；只有用户明确授权新尝试后，才用 `--new-attempt --output 新目录`，保留旧证据。该参数不能绕过未知提交。恢复使用创建时冻结的模型和业务空间，当前密钥须能访问原任务。

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

ZIP 还包含原始 `transcript.txt`、`transcript.md`、`input.json`、`source-units.json`、`state.json`、`asr-task.json`、脱敏 `asr-raw.json`、`report-check.json` 和可用的 `review.json`。**主题命名的 Markdown 是阅读报告，`transcript.md` 是原始转写**，不能互相代替。

`deliver` 检查 Markdown 实际存在且对应 HTML、来源 ID 有效，再逐字节检查归档内容。内容仍须人工核对来源；浏览器视觉验收无法完成时写入 `review.json`，继续静态检查和交付，不声称视觉验收通过。

`runs/` 和临时音频不进入 Git。保留报告、原始资料和任务证据供下载，处理后清理本地音频；临时工作区不是永久资料库。ASR 可能误识别术语，流程无法覆盖只在画面中出现的信息。暂不处理 B站短链接、批量合集、直播、需登录或无独立音轨的视频。YouTube/Vimeo 各自独立适配，不套用 B站仅 AAC 的选择规则。

## 来源

`vendor/video-report/` 保留 [imexlovery/video-report-agent](https://github.com/imexlovery/video-report-agent) 的 Skill、模板和参考文件，提交及校验见 `UPSTREAM.json`。上游快照未发现 LICENSE，本包不额外授予上游许可，公开分发前须核实。

百炼依据：[官方临时存储](https://help.aliyun.com/zh/model-studio/get-temporary-file-url/)、[ASR HTTP API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)。
