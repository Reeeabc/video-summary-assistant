# 云端视频总结助手

在 Codex Cloud 中发送 B站链接，由百炼直接读取音频直链转写，再由当前 Codex 任务依据原项目模板生成 HTML 阅读报告。电脑可以离线；不要求另一份报告模型 API Key。

本项目只准备云端运行所需的代码和指令。部署进度和当前验收见 `VALIDATION.md`；本地测试通过不等于已发布云端环境。

## 初始化

需要 Python 3.12+。运行 `bash setup.sh` 安装隔离依赖并执行离线测试。

在 Codex Cloud 的 Network secrets 中配置 `DASHSCOPE_API_KEY`，Allowed domains 仅填本次使用的百炼 API 域名。在 Environment variables 中设置 `BAILIAN_WORKSPACE_ID`（北京业务空间 ID）和 `ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans`。业务空间为空时使用官方默认北京域名。项目不自动加载本地 .env 文件。

云端环境创建、网络配置及验收步骤见 [DEPLOY.md](DEPLOY.md)。

解析子进程使用 yt-dlp 官方支持的 `--compat-options no-certifi` 选择系统 CA，
继承环境的证书与代理配置，始终保持 TLS 证书校验。云端代理 CA 应加入系统信任库，
不要使用 `--no-check-certificates`。子进程不接收百炼密钥，也不加载个人 Cookie。
本次 probe 修复、HTTP 412 证据及尚未通过的项目见 [PROBE_DIAGNOSTICS.md](PROBE_DIAGNOSTICS.md)。

项目内的 `bilibili_extractor.py` 在页面缺少初始数据时使用详情 API 回退，
验证 BV 号及所选分 P 的 cid、时长，再复用固定版本 yt-dlp 的播放信息和格式解析。
无需修改虚拟环境依赖文件；API 返回错误时立即停止，不自动重试。
Range 检查会继承 yt-dlp 的公开媒体请求头（包括 User-Agent 和 Referer），
不会转发 Cookie 或 Authorization；这些请求头只在内存中传递，不写入任务元数据。
这些媒体请求头不会自动传给百炼。当前云端一次实际转写验收返回 `FILE_DOWNLOAD_FAILED`；
解析或 Range 成功不代表百炼能取流，尚未通过端到端验收，详情见 [VALIDATION.md](VALIDATION.md)。

## 使用

在选定本项目环境的 Codex 云端任务中发送：

> 总结 https://www.bilibili.com/video/BV1gmJX6SExT ，按 AGENTS.md 执行，standard 模式，交付 HTML、转写、字幕和下载包。

脚本负责准备来源，Codex 负责阅读、写作和检查：

```bash
# 只检查元数据与音轨：不调用百炼、不下载完整媒体
.venv/bin/python video_assistant.py probe 'https://www.bilibili.com/video/BV1gmJX6SExT'
# 可选：取得音轨后以 Range 最多读取 1 KiB；服务器不返回有效 206 时不读取响应体
.venv/bin/python video_assistant.py probe 'BV1gmJX6SExT' --check-audio-range
# 提交最多一次云端转写，成功后保存完整来源
.venv/bin/python video_assistant.py prepare 'https://www.bilibili.com/video/BV1gmJX6SExT'
# 中断后查询原任务
.venv/bin/python video_assistant.py resume runs/BV1gmJX6SExT-p1-standard
# Codex 生成报告后运行静态检查
.venv/bin/python video_assistant.py verify runs/BV1gmJX6SExT-p1-standard
```

运行 `status 任务目录` 查看非敏感检查点。提交结果未知时由用户从百炼控制台核实任务，再 `attach-task 任务目录 原任务ID`。恢复始终使用创建时冻结的模型和业务空间；密钥从当前环境读取，须能访问原账号任务。正常重试使用同一云端聊天及任务目录；新聊天有独立文件系统，须带入原任务目录才能恢复，不能承诺跨聊天自动去重。

## 输出

`runs/` 保存每次任务的 HTML、TXT、SRT、完整来源映射、脱敏原始结果和任务检查点。密钥及完整签名媒体地址不保存。产物不进入源代码仓库；结束前下载任务 ZIP 或保存到用户指定的私有存储，不能把临时任务文件当作永久资料库。

原始 ASR 可能识别错术语；报告只理解转写，不能保证覆盖只在画面里出现的信息。默认单次上限 60 分钟，支持明确指定分 P；暂不处理短链接、合集批量、登录受限视频或无独立 AAC 音轨的视频。直链在当前环境已实测成功，Codex 云端出口仍须单独验证。抓取失败时保留证据，不自动切换到下载/OSS 或另建计费任务。

## 来源

`vendor/video-report/` 完整保留 [imexlovery/video-report-agent](https://github.com/imexlovery/video-report-agent) 的报告 Skill、两种模板与参考文件，固定提交及文件校验见 `UPSTREAM.json`。未修改其视觉资源。上游仓库此次快照未发现 LICENSE；本包不额外授予上游内容的许可，先用于用户自己的私有云端环境，公开分发前核实上游许可。
