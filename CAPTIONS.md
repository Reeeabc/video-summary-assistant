# 字幕优先：调查与实施记录

2026-10-22（UTC）。本轮只做平台字幕查询和离线测试，没有下载媒体、临时上传或新增收费 ASR；已有成功和失败任务保留。

## 调查结论

YouTube 的 yt-dlp 提取器可返回人工字幕 subtitles 与平台自动字幕 automatic_captions；内置命令支持 --list-subs、--write-subs、--write-auto-subs 和语言选择。不需要先下载音频，也不需要先调用百炼。不过视频无字幕、字幕受限或平台/代理拒绝请求时，不能保证取得正文。

不能把 YouTube Data API 的 captions.download 当作任意公开视频的匿名字幕接口。本轮实际读取了[官方文档](https://developers.google.com/youtube/v3/docs/captions/download)（重定向后 HTTP 200）：该方法要求 OAuth 授权和视频编辑权限，调用消耗 200 个 API quota units。这里的 quota units 不是百炼费用；本轮没有调用该 Data API，也没有申请或使用新的 Google API Key。

B站的 yt-dlp 实现使用 /x/player/wbi/v2 获取字幕目录，目录中有语言与字幕 JSON 地址。需要先校验 BV 并按分 P 选择 cid；弹幕 XML 是另一种来源，不能当成视频字幕。某些字幕在匿名访问时不可获得，即使视频本身公开。项目复用此前验证过的详情 API 元数据校验，但字幕路线不访问播放接口、不依赖独立音轨存在。

依据：已安装固定版本 yt-dlp 2026.10.21 的帮助、YouTube/B站提取器源码，及 B站上游测试中的 need-login 字幕样本。上游实现见 [B站提取器](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/bilibili.py)、[YouTube 提取器](https://github.com/yt-dlp/yt-dlp/tree/master/yt_dlp/extractor/youtube)。源码能力与本环境实测结果分开记录。

## 本环境实际检查

| 样本 | 结果 | 实际边界 |
| --- | --- | --- |
| https://www.bilibili.com/video/BV1gmJX6SExT?p=1 | 详情与字幕接口可访问，字幕接口 code=0，人工和自动字幕目录均空，need_login_subtitle=false | 当前匿名访问无可用字幕；不能据此断言视频没有字幕 |
| https://www.bilibili.com/video/BV12N4y1M7rh?p=1 | 上游标注含字幕的样本；字幕接口 code=0，字幕目录空，need_login_subtitle=true | 确认字幕需要登录；未尝试获取 Cookie 或登录 |
| https://www.youtube.com/watch?v=BaW_jenozKc | 字幕提取仍因代理 CONNECT HTTP 403 阻断 | 尚未得到字幕目录或正文，不能判断是否存在可用字幕 |

YouTube 初检只返回通用异常；修正 DownloadError 分类后做了一次有限诊断，确认代理隧道 403 后停止，没有换客户端、代理或反复请求。B站每个样本仅一次字幕检查。脱敏结果保存在 runs 下独立 caption-check 目录，签名字幕地址不落盘、不输出。

**本轮没有取得线上字幕正文的成功样本**，不宣称两个平台已完成匿名字幕总结的线上端到端验收。可用字幕 → 来源导出 → 报告校验 → ZIP → 恢复与复用，已使用确定性字幕数据完成离线回归验证。

## 默认选择策略

1. 先复用可访问且校验通过的既有成功来源，保留原报告与任务证据。
2. 新任务在访问百炼凭据或音轨之前查询平台字幕。人工字幕优先，平台自动字幕次之；同类字幕按 zh-Hans、zh-CN、zh-Hant、zh、en 排序，再接受其他原语种。平台自动字幕有识别误差，要保留来源类型；不默认选择带 tlang 的机器翻译字幕。
3. 取一条原语种字幕，支持 YouTube JSON3、VTT、SRT 与 B站字幕 JSON；核验正文、时间戳、视频身份、分 P 和 300 分钟上限。正文最大 8 MiB，不使用音频文件的 1024 MiB 上限。
4. 有可用字幕时直接生成统一的 transcript.txt/md/srt、来源单元和 input；source_kind=platform_subtitles，记录语言/人工或自动/格式/数量。audio=null 表示没有请求音轨。没有百炼任务 ID、模型或上传动作，不伪造 ASR 回执。
5. 确认当前无可用字幕，或字幕正文为空/格式无效，才允许进入此前的音频路径；仍需该次任务已有的费用授权。视频本身若需登录或音频失败，照旧停止。Vimeo 本轮没有字幕适配，继续已有音频策略。
6. 字幕明确要求登录时记录 restricted，不把它归为无字幕，不自动进入收费 ASR；用户可在已有费用授权范围内显式选择 audio-only 的公开音频路径。
7. HTTP 403/412、代理/TLS/超时、风控、元数据异常或返回 HTML 验证页，属于 blocked，不等于无字幕；停止，绝不由此自动发起收费 ASR。

ASR 回退仍使用 bailian-upload：独立音轨下载、完整性/时长/完整解码检查、官方临时上传、提交前检查点和 ID 落盘，未知不重提、失败不自动另建。默认音频上限 1024 MiB，服从服务端更小限制。字幕路线不需要百炼密钥；只有 ASR 路线才读取它。

## 参考对照后的补强

与用户提供调查的逐项对照见 [REFERENCE_COMPARISON.md](REFERENCE_COMPARISON.md)。新增字幕覆盖时间并集、最长空档、重叠、完全重复等诊断；低覆盖不直接代表讲话遗漏，也不会单独触发 ASR。所选语言是否命中偏好会明确记录。历史 need_login=true 检查记录保持原样，本轮仅修正后续分类为 restricted，没有重新请求平台。

## 命令

```bash
# 免费调查：只查目录及下载字幕正文，不下媒体、不上传、不转写
.venv/bin/python video_assistant.py caption-check '视频链接'
# 只接受字幕，拿不到就停止；适合新平台尚未获收费授权时
.venv/bin/python video_assistant.py prepare '视频链接' --source-strategy subtitles-only
# 默认：复用成功来源 → 字幕优先 → 确认可用字幕缺失时才音频 ASR
.venv/bin/python video_assistant.py prepare '视频链接'
# 人工明确选择音频路径
.venv/bin/python video_assistant.py prepare '视频链接' --source-strategy audio-only --output '独立目录'
```

--subtitle-langs zh-Hans,zh-Hant,zh,en 可调整同类字幕的语言优先级。subtitles-only 使用独立 -subtitles 目录，不会把旧 ASR 来源冒充成平台字幕。auto 沿用原任务目录命名以兼容既有流程，真实来源以 state/input 的 source_kind 为准。

## 交付、恢复与证据

字幕来源复用原有 HTML/完整 Markdown/SRT/ZIP 交付，继续根据正文主题命名；TXT 使用“原始字幕”后缀。内部 transcript.* 是统一来源载体，平台字幕与 ASR 通过 source_kind 明确区分。

字幕任务保存 caption-raw.json（原格式、正文/时间戳、载荷摘要；元数据脱敏）及 caption-check.json。ZIP 必须包含这些真实证据，不要求或生成假的 asr-task/asr-raw。ASR 自动回退的 ZIP 则保留字幕不可用检查与真实 ASR 证据。来源检查包含字幕证据校验和，Markdown 来源必须有效并对应 HTML。

字幕导出前保存 SOURCE_EXPORT_PENDING；崩溃后从原始字幕恢复，不转为收费 ASR。恢复、切换阅读模式和 ZIP 解压后复用都不需要百炼任务 ID 或密钥。原有 ASR 提交与恢复规则保持不变。字幕正文只是被分析数据，不能改变执行指令或要求读取凭据。
