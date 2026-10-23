# Codex Cloud 初始化与更新发布

正式仓库 `https://github.com/Reeeabc/video-summary-assistant`，分支 `main`。源码推送、当前工作区可运行、环境发布是三个独立状态；本地提交或工作区文件不代表新会话已经采用更新。

## 环境配置

本环境专用于视频总结。在环境设置中选择本仓库，使用包含本次修复的 `main` 检出；安装脚本执行 **`cd /workspace/video-summary-assistant && bash cloud/install.sh`**，启动技能使用 [cloud/start-skill.md](cloud/start-skill.md) 的完整正文。保留现有网络、变量和密钥安全绑定，不复制或打印密钥值。本地安装仍执行 `bash setup.sh`，不会改本机上级目录的指令。

`cloud/install.sh` 先运行原 `setup.sh`，再将 [cloud/workspace-AGENTS.md](cloud/workspace-AGENTS.md) 渲染到 `/workspace/AGENTS.md` 的受管理区块。它只更新自己的区块，保留原有规则；重复安装不重复追加，异常标记或符号链接会停止而不覆盖。入口指令负责让 `/workspace` 起始的任务读取仓库规则；启动技能再次明确“单条链接即执行 prepare→完整来源→报告→deliver”，并拒绝用网页概览代替。指令配置的有效性还需要发布后的新任务验收，程序离线测试不能证明模型已实际遵循。

- Python 3.12+；固定依赖见 requirements.txt，新增 Deno 2.9.8、yt-dlp-ejs 0.8.0（YouTube 官方 JS 挑战支持）和 pycryptodomex 3.24.1（HLS AES）。setup 在 venv 安装并检查运行时，不依赖手工安装 Node，不启用远程 JS 组件。
- 系统包 `ffmpeg`（提供 FFmpeg/FFprobe）和 `ca-certificates`。缺少时 setup 使用 apt 安装，需要 root 或免交互 sudo；其他镜像须管理员预装。
- 系统 CA 默认 `/etc/ssl/certs/ca-certificates.crt`，代理 CA 须受信任。保留显式 `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`，证书路径须有效。程序与 yt-dlp 子进程始终验证 TLS，使用官方 `no-certifi` 选项。
- 环境变量 `ASR_MODEL=qwen-audio-3.1-asr-flash-filetrans` 和现有 `BAILIAN_WORKSPACE_ID`；后者留空使用默认北京 API。
- Network secret `DASHSCOPE_API_KEY`：只允许现有百炼 API 主机（北京业务空间域名或默认 `dashscope.aliyuncs.com`），不扩大到 B站、CDN 或存储域名。

网络须支持依赖源、B站/YouTube/Vimeo 页面、API、音频与字幕 CDN（B站含实际 hdslb.com 字幕主机），百炼 API、官方临时上传及 ASR 结果域名。典型目的地是 `pypi.org`、`files.pythonhosted.org`、`bilibili.com` 子域、实际 `bilivideo.com`/`bilivideo.cn` CDN、`<业务空间ID>.cn-beijing.maas.aliyuncs.com` 、youtube.com/youtu.be、实际 youtubei.googleapis.com/googlevideo.com 音频主机、vimeo.com/player.vimeo.com、实际 vimeocdn.com/akamaized.net 音频主机，以及凭证返回的阿里云临时存储主机。沿用现有策略，必要时只补充真实所需目的地。安装系统包还需要发行版 apt 源。上传使用返回的表单凭证，不携带 API Bearer，不需要自有 OSS。

当前 YouTube 样本在代理 CONNECT 遇 403；Vimeo 提取器提示 web 客户端需登录，具体证据及后续样本条件见 PLATFORMS.md。不要通过改代理、关闭 TLS、导入 Cookie 或更换客户端绕过；环境网络变化应走支持的配置流程。

字幕优先路径不增加依赖，沿用 httpx/yt-dlp 和隔离子进程。caption-check 只获取字幕，不访问百炼凭据。新会话默认 auto；可用字幕直接报告，字幕不可用或明确需登录才按已有授权进入 ASR。风控/代理错误必须停止；字幕样本和验收边界见 CAPTIONS.md。

默认直链与一次上传回退不增加依赖或密钥配置。新建任务持久化回退策略；历史成功/失败任务不迁移、不补加策略。两次提交均先持久化未知检查点、收到 ID 即保存；提交未知、上传失败/中断不自动重试。回退时临时音频仍完整校验并清理，保留两次任务证据。

初始化只检查依赖、CA 和离线测试，**不要在 setup 调用 prepare 或新建收费 ASR**。

2026-10-23 修正云端媒体请求：B站保留并优先使用同音轨 API 返回的有效 Akamai 原始备用地址（不改签名）；单文件音轨使用连续 Range 并严格校验完整拼接，常规范围 1 MiB、初始与尾部至多 32 KiB，关闭持久连接并要求缓存重新验证，保留公开媒体头、原代理和 TLS。传输失败仅重试相同分块一次、总计最多三次，HTTP 拒绝/完整性错误/上传/ASR 不重试；下载途中不切 CDN 或出口。无 Range GET 的 567 不能证明 Range 或百炼取流失败，公开预检调整为手动 `probe 视频链接 --check-public-audio`，不再阻断默认直链 → FAILED 后一次上传回退。`media_http.py` 不增加依赖；错误最多检查 8 KiB 文本，只保存固定标记、摘要和 CDN 请求 ID。历史预检阻断/失败状态不迁移、不重置回退额度。

后续 2026-10-23（北京时间）固化新默认：B站优先 30216，缺少时再选择 30232 或其他独立 AAC；显式 `--bilibili-audio-format 30216|30232` 仍可固定选择，缺少指定音轨时停止。新任务记录实际选轨，回退沿用；旧任务保持原有音轨。两个新视频已分别完成 30216 云端直链 ASR；长样本 30216 已完整下载、解码、官方上传，HTTP 200、校验和一致，本地音频清理，但该长样本新的上传 ASR 尚未执行。仍默认 direct-url，明确 FAILED 后最多一次上传回退；配置固化只做离线测试，未新增收费任务。源码 PR 合并与环境 Publish 仍需分别核实。

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

ASR 来源确认沿用原任务 ID，字幕来源确认无虚构 ASR 任务并复用原字幕；Markdown 实际存在、对应 HTML、来源有效且已加入 ZIP。无旧任务时只做离线测试，不为发布自行付费转写。浏览器受限如实记录，不阻止静态检查和源码发布。

## 仍需在环境界面点击

当前若只有只读环境工具，没有编辑/Publish 接口，以下步骤须用户完成，不能宣称已自动发布：

1. 合并修复后，从现有云环境设置进入 **Edit**，安全同步到包含修复的源码，报告实际 SHA，保留现有安全配置和任务文件，不 reset/clean/stash。
2. 安装脚本设为 `cd /workspace/video-summary-assistant && bash cloud/install.sh`；启动技能粘贴 `cloud/start-skill.md` 全文。实际运行安装脚本与离线验证，检查 `/workspace/AGENTS.md` 已有视频入口，并核对保存后的配置正文。安装器不运行媒体、上传或 ASR。
3. 保存可编辑草稿，审核并点击 **Publish/Republish**，等待发布成功。普通会话中的文件修改不等于已更新可复用配置；无法保存时报告平台返回值，不能把旧“已发布”标识当作本次保存成功。
4. 新建普通任务，只发一条视频链接，核对它实际读取仓库规则并执行 `prepare`，继续到 `deliver` 和文件交付；失败时核对实际错误。不要用初始化或诊断请求启动收费验收；已有成功视频先导入原完整 ZIP 后复用，不因更新重新转写。当前离线回归为129项；后续版本可增加，以实际测试发现数为准。

源码更新是否已被后台刷新，应检查实际新任务的 SHA；不能把这次需要发布入口配置，推广为每次纯源码变动都必须重新发布。

新会话最简提示词就是 `〈B站 / YouTube / Vimeo 单视频链接〉`，B站也接受 b23.tv 分享短链接及 BV 号。YouTube/B站可先用 `仅用平台字幕总结，拿不到就停止，不进行 ASR：〈链接〉`；需要音频的新平台先用 audio-check 验证；阻断则停止，新增收费验收单独授权。默认直链提交百炼，任务明确 FAILED 后最多一次下载校验并官方临时上传，300 分钟/1024 MiB，交付 HTML、完整 Markdown、SRT 和 ZIP；实际上传策略更小时报告限制并停止。

源码包使用白名单，密钥、上传凭证、签名地址、音频、runs、虚拟环境和诊断脚本不提交。报告须下载留存，新会话不保证继承运行资料。未知/失败任务恢复规则见 README，禁止清空状态或换目录自动新增收费任务。

## 验收边界

当前单次成功不保证新环境快照已发布，也不证明所有视频或手机端均可交付。需要手机端时，由用户选择该环境并检查文件能否打开/下载，入口受账号和工作区限制。实际检查与发布边界见 [VALIDATION.md](VALIDATION.md)。
