# Codex Cloud 初始化与更新发布

正式仓库 `https://github.com/Reeeabc/video-summary-assistant`，分支 `main`。源码推送、当前工作区可运行、环境发布是三个独立状态；本地提交或工作区文件不代表新会话已经采用更新。

## 环境配置

在环境设置中选择本仓库与 `main`，Setup command 填 **`bash setup.sh`**。保留现有网络、变量和密钥安全绑定，不复制或打印密钥值。

- Python 3.12+；固定 Python 依赖见 `requirements.txt`。
- 系统包 `ffmpeg`（提供 FFmpeg/FFprobe）和 `ca-certificates`。缺少时 setup 使用 apt 安装，需要 root 或免交互 sudo；其他镜像须管理员预装。
- 系统 CA 默认 `/etc/ssl/certs/ca-certificates.crt`，代理 CA 须受信任。保留显式 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`，证书路径须有效。程序与 yt-dlp 子进程始终验证 TLS，使用官方 `no-certifi` 选项。
- 环境变量 `ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans` 和现有 `BAILIAN_WORKSPACE_ID`；后者留空使用默认北京 API。
- Network secret `DASHSCOPE_API_KEY`：只允许现有百炼 API 主机（北京业务空间域名或默认 `dashscope.aliyuncs.com`），不扩大到 B站、CDN 或存储域名。

网络须支持依赖源、B站页面/API/音频 CDN、百炼 API、官方临时上传及 ASR 结果域名。典型目的地是 `pypi.org`、`files.pythonhosted.org`、`bilibili.com` 子域、实际 `bilivideo.com`/`bilivideo.cn` CDN、`<业务空间ID>.cn-beijing.maas.aliyuncs.com` 和凭证返回的阿里云临时存储主机。沿用现有策略，必要时只补充真实所需目的地。安装系统包还需要发行版 apt 源。上传使用返回的表单凭证，不携带 API Bearer，不需要自有 OSS。

初始化只检查依赖、CA 和离线测试，**不要在 setup 调用 prepare 或新建收费 ASR**。

## 可执行的发布前检查

在仓库根目录运行：

```bash
bash setup.sh
.venv/bin/python runtime_config.py
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
bash -n setup.sh
git diff --check
```

有原成功任务时可额外执行，不新建转写：

```bash
.venv/bin/python video_assistant.py prepare '原视频链接' --reuse-from '原成功任务目录'
.venv/bin/python video_assistant.py deliver '命令返回的任务目录'
```

确认沿用原任务 ID；Markdown 实际存在、对应 HTML、来源有效且已加入 ZIP。无旧任务时只做离线测试，不为发布自行付费转写。浏览器受限如实记录，不阻止静态检查和源码发布。

## 仍需在环境界面点击

当前若只有只读环境工具，没有编辑/Publish 接口，以下步骤须用户完成，不能宣称已自动发布：

1. 打开现有云环境 **Edit/设置**，确认仓库 `Reeeabc/video-summary-assistant`、分支 `main`、初始化命令 `bash setup.sh`，保留现有安全配置。
2. **Save → Rebuild/更新环境 → Publish**（以当前界面为准），等待初始化成功及 `Environment published`。无单独重建按钮则按界面提供的更新/发布流程完成。
3. 新建会话并选择该环境。需要复用旧结果时导入原完整 ZIP，不为环境更新重新转写成功视频。

新会话最简提示词：`总结这个视频：https://www.bilibili.com/video/BV……`。默认官方临时上传，300 分钟/1024 MiB，交付 HTML、完整 Markdown、SRT 和 ZIP；实际上传策略更小时报告限制并停止。

源码包使用白名单，密钥、上传凭证、签名地址、音频、runs、虚拟环境和诊断脚本不提交。报告须下载留存，新会话不保证继承运行资料。未知/失败任务恢复规则见 README，禁止清空状态或换目录自动新增收费任务。

## 验收边界

当前单次成功不保证新环境快照已发布，也不证明所有视频或手机端均可交付。需要手机端时，由用户选择该环境并检查文件能否打开/下载，入口受账号和工作区限制。实际检查与发布边界见 [VALIDATION.md](VALIDATION.md)。
