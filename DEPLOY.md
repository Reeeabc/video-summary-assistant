# Codex Cloud 部署

## 准备仓库

将本目录作为一个独立私有 GitHub 仓库的根目录。只上传部署 ZIP 内的文件，不上传课程播放器仓库、旧项目 .env、浏览器目录、媒体或 runs。GitHub 连接需要有权限访问该仓库。

## 创建环境

在网页或桌面应用中新建任务，选择 `Work in > Cloud > Select environment > Create environment`，选择此私有仓库。也可在设置的 `Codex Cloud > Environments` 中创建。

环境名称建议：`B站视频总结助手`，可见性选 `Only me`。准备环境时发送：

> 这是一个 Python 云端视频总结助手。阅读 README.md 和 AGENTS.md，执行 bash setup.sh。日常任务用百炼直接转写 B站音频，再由当前 Codex 生成 HTML，不安装 Pi、不申请额外 OpenAI API Key。只执行 probe 和离线测试，环境初始化不要提交收费转写。

## 网络与密钥

可选临时上传模式需要系统安装 FFmpeg（含 `ffmpeg`、`ffprobe`）。仅使用直链模式不需要这两个工具。
无需创建自有 OSS；上传目标由百炼上传凭证接口返回，需允许该实际官方临时存储域名。
百炼 API Key 仍只绑定百炼 API 域名，不应扩大到存储域名；存储上传使用接口返回的临时表单凭证。

开启 `Allow Codex to access internet`，允许依赖安装域名、B站页面/API/CDN、百炼 API 和阿里云转写结果存储域名。视频直链和结果域名是动态的；先在配置中选择适合的域名范围并通过 probe 实测，不能只允许 www.bilibili.com。

典型目的地包括 `pypi.org`、`files.pythonhosted.org`、`bilibili.com` 及其 API 子域、B站实际返回的 bilivideo.com / bilivideo.cn CDN 域名、`<业务空间ID>.cn-beijing.maas.aliyuncs.com` 和实际转写结果 OSS 域名。按界面支持的格式添加主域/子域或具体主机；网络被拒时只补充实际必要的目的地。账户或组织策略可能进一步限制网络。

Environment variables：

```text
BAILIAN_WORKSPACE_ID=你的北京业务空间ID
ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans
```

Network secrets：

```text
Key: DASHSCOPE_API_KEY
Value: 你的百炼API Key
Allowed domains: <业务空间ID>.cn-beijing.maas.aliyuncs.com
```

业务空间留空时，Network secret 的 Allowed domains 改为 `dashscope.aliyuncs.com`。使用代理替换的密钥占位值即可；脚本只把该值放入百炼请求的 Bearer 头。B站、CDN 和结果下载请求不携带百炼密钥。不要在聊天、源码、初始化脚本或任务提示词中填写密钥值。

## 发布与验收

1. 初始化与离线测试通过；运行 `probe` 确认云端能解析指定视频，尚不计费。
2. 保存并 Publish，等待界面确认 `Environment published`。
3. 新建任务，选该环境，发送 README 的完整总结指令；提交后应记录一个百炼 task_id。
4. 等待转写成功、生成报告及 verify 通过，打开 HTML 和字幕抽查关键数字、术语、开头与结尾。
5. 在手机 ChatGPT 的 Codex 中选择 Cloud 和该环境，再发链接，确认能查看并取得最终文件。入口开放取决于账号和工作区。

只有第 3–5 步实际通过，才确认“云端视频总结助手已部署并可在手机使用”。本地成功或环境初始化成功都不能替代该验收。

同一任务的复用不会自动新增转写。为测试请先复用此前已验证任务结果，再对新视频执行收费验收。云端模型使用消耗 Codex 额度，百炼调用按阿里云账号计费。

官方依据：[云端环境](https://learn.chatgpt.com/docs/environments/cloud-environments)、[手机端](https://learn.chatgpt.com/docs/mobile)、[千问长文件转写](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)。
