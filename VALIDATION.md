# 部署与验收进度

核对日期：2026-10-22（北京时间）。

## 已验证

- 原视频 BV1gmJX6SExT 在本地环境解析成功，页面元数据时长 474.41 秒，有独立 AAC 音轨；新独立脚本 probe 同样成功，不下载完整媒体。
- 此前使用百炼 qwen-audio-3.1-asr-flash-filetrans 直接抓取该音频直链，主任务和子任务成功；实际结果有 3226 字符、41 个句段。本次复用原任务输出，没有新提交收费转写。
- 从此前原始结果重新导出助手所需的完整 TXT、Markdown、SRT 和来源 ID；已完成同一任务的样例 HTML、来源对照及静态校验。未安装 Chromium，未声称浏览器视觉验收通过。
- 独立项目 11 项离线测试通过：URL 边界、脱敏、提交未知不重提、任务 ID 持久化、子任务失败、无时间戳原文保留、结果下载不携带 Authorization、并发锁、完成任务复用、虚构来源与外部资源拦截、原文改动拦截。
- 原课程播放器仓库基础检查：pip check 通过，66 项测试中 59 项通过、7 项按 Windows/真实媒体条件跳过。课程播放器源代码没有调整。
- vendor 的 10 个文件逐个通过 Git blob SHA-1 核对，与 UPSTREAM.json 指定提交完全一致。
- 已经由用户提供的 SSH 地址上传独立项目至 https://github.com/Reeeabc/video-summary-assistant 的 main 分支，初始提交 49d7af5；只含 22 个部署源文件，没有父项目 Git 历史、密钥、媒体或转写产物。

## 尚未完成

- GitHub 插件对新仓库仍返回 404，按账号查询也未列出它；SSH 可读写，代码上传已通过该路径完成。Codex 网页中若看不到新仓库，需要在其 GitHub 连接里授权该仓库；SSH 写入权限不代表 Codex 云端已有仓库访问权。
- Codex Cloud CLI 可读取任务列表，但当前任务列表为空；环境选择器仅显示 All Environments，未显示可选环境。没有证据说明账户已建好本助手环境。
- 当前工具不提供云端环境创建/密钥配置 API。Computer Use 初始化返回 sandboxCwd is not a local file URI，无法在这次 WSL 会话中操作配置页面。
- 因此未完成云端环境发布、云端出口访问 B站、云端百炼调用和手机端交付验收。不能将本包描述成已上线服务。

## 下一步

1. 在 Codex 网页中选择 Reeeabc/video-summary-assistant；如列表缺失，先在 GitHub 连接里授权新仓库。
2. 按 DEPLOY.md 创建并发布 Codex 云端环境；在 Network secrets 中保存百炼密钥。
3. 提供环境 ID/环境链接后，在真正云端执行 probe 和完整总结，下载并检查最终报告。

本说明记录实际证据，不记录密钥、带签名媒体地址、个人浏览器数据或私人课程内容。
