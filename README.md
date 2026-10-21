# 云端视频总结助手

在 Codex Cloud 中发送 B站链接，由百炼直接读取音频直链转写，再由当前 Codex 任务依据原项目模板生成 HTML 阅读报告。电脑可以离线；不要求另一份报告模型 API Key。

本项目只准备云端运行所需的代码和指令。部署进度和当前验收见 `VALIDATION.md`；本地测试通过不等于已发布云端环境。

## 初始化

需要 Python 3.12+。运行 `bash setup.sh` 安装隔离依赖并执行离线测试。

在 Codex Cloud 的 Network secrets 中配置 `DASHSCOPE_API_KEY`，Allowed domains 仅填本次使用的百炼 API 域名。在 Environment variables 中设置 `BAILIAN_WORKSPACE_ID`（北京业务空间 ID）和 `ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans`。业务空间为空时使用官方默认北京域名。项目不自动加载本地 .env 文件。

云端环境创建、网络配置及验收步骤见 [DEPLOY.md](DEPLOY.md)。

## 使用

在选定本项目环境的 Codex 云端任务中发送：

> 总结 https://www.bilibili.com/video/BV1gmJX6SExT ，按 AGENTS.md 执行，standard 模式，交付 HTML、转写、字幕和下载包。

脚本负责准备来源，Codex 负责阅读、写作和检查：

```bash
# 只检查元数据与音轨：不调用百炼、不下载完整媒体
.venv/bin/python video_assistant.py probe 'https://www.bilibili.com/video/BV1gmJX6SExT'
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
