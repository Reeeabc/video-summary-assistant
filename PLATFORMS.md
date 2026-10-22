# 多平台适配与非计费验收

日期：2026-10-22（UTC）。本轮没有提交新 ASR，没有调用官方临时上传接口。新增平台的完整转写尚未验收。

| 平台 | 样本链接 | 本轮实际阶段 | 状态与停止原因 |
| --- | --- | --- | --- |
| B站 | https://www.bilibili.com/video/BV1gmJX6SExT?p=1 | 解析 → 独立音轨下载 → 完整解码 → 清理 | 通过；AAC 5,943,687 字节、474.410667 秒、48 kHz 双声道，无视频。此前已完成一次临时上传/ASR/报告，本轮只回归、不重新转写 |
| YouTube | https://www.youtube.com/watch?v=BaW_jenozKc | 提取器 API 请求 | 未取得视频元数据或音轨；代理 CONNECT 返回 403，停止。不能据此认定 YouTube 本身风控或该样本不可用 |
| Vimeo | https://vimeo.com/76979871 | Vimeo 提取器 | 明确提示 `The web client only works when logged-in`，停止；没有尝试登录、Cookie 或改客户端绕过 |
| 抖音 | 未请求 | 仅适配入口 | 未实现、未实测、不开放 |
| TikTok | 未请求 | 仅适配入口 | 未实现、未实测、不开放 |
| 西瓜视频 | 未请求 | 仅适配入口 | 未实现、未实测、不开放 |

YouTube/Vimeo 各做了一次初检，初始诊断过于笼统；补充错误脱敏后各做一次有限诊断，确认上述原因即停止，未更换客户端、出口或反复尝试其他样本。任务内分别保留初检与诊断的 audio-check.json，不覆盖旧记录。证据只进 runs，不提交 Git。

## 脱敏错误依据

YouTube：`Unable to download API page: <urlopen error Tunnel connection failed: 403 Forbidden>`，异常为 `ProxyError`。隧道建立阶段失败，未拿到可归因于 YouTube 的业务响应，需通过环境支持的网络配置排查实际目标主机；不修改代理、不绕过网络策略、不关闭 TLS。

Vimeo：`The web client only works when logged-in.`，这是当前固定版本 yt-dlp 的 web 客户端要求。它不能证明所有 Vimeo 视频都必须登录，也不能宣称本项目已完成 Vimeo 公开视频取流验收。本轮按要求停止，不读取浏览器或登录态。

字幕优先策略与新增实测见 [CAPTIONS.md](CAPTIONS.md)。YouTube 字幕请求仍受代理 CONNECT 403 阻断；B站目标视频匿名目录为空，另一个上游字幕样本明确需登录。以上不改变此前音频验收结论。

## 实现与离线验证范围

- platforms.py 显式校验单视频 URL、平台和 ID，输出含平台的目录标识；合集、频道、直播入口和未开放平台拒绝处理。旧 B站任务按规范链接识别复用，无需迁移或改写来源。
- bilibili_media.py 与 bilibili_extractor.py 保留 B站专用规则和详情 API 修复；generic_media.py 使用 upstream YouTube/Vimeo 提取器，按独立音轨/协议挑选，不沿用 30232 或只支持 AAC 的条件。
- media_download.py 将选定的单一音轨交给 yt-dlp 原生 HTTP/HLS/DASH 下载器，缺片即失败，重试为零；不合并画面，不自动回退到视频格式。私有媒体元数据仅经内存/stdin 传入隔离子进程。
- audio_acquisition.py 对音频 remux/transcode 为 M4A/AAC，校验仅一条音轨、无视频、大小、时长和完整解码；音源与输出均限制 1024 MiB，上传还受实际服务端策略约束。
- 离线使用 FFmpeg 生成三秒 Opus 音频，实际转为 M4A/AAC 并完整解码成功；另外生成真实分段音频，通过离线内存 HTTP 传输调用真实 yt-dlp HLS/DASH 下载器完成组装、M4A 规范化与完整解码。这些验证转换/分段能力，不代替平台线上取流验收。缺片停止、体积限制和凭据隔离有回归测试。
- 初始化安装固定版本 Deno 2.9.8、yt-dlp-ejs 0.8.0 和 pycryptodomex 3.24.1。使用 yt-dlp 官方 JS 支持，不在任务中下载远程组件；继续使用系统 CA。

## 后续完整转写验收需要的样本与授权

| 平台 | 非收费前提 | 之后另需的费用授权 |
| --- | --- | --- |
| YouTube | 当前测试链接可作为候选；先由环境配置解决代理 CONNECT 403，再验证其可用性及完整音频准备。若不可用，由用户提供另一个短、公开、允许处理的单视频 | 前置免费检查通过后，单独授权该样本一次百炼 ASR；未知提交不重提 |
| Vimeo | 需一个无需登录、能由官方 yt-dlp 提取器访问的公开视频样本；先完成独立音轨下载与解码。如果仍要求登录则继续停止 | 前置免费检查通过后，单独授权该样本一次百炼 ASR；未知提交不重提 |

本轮未取得两个新平台的可靠时长，不能给出准确收费金额。后续先报告实际时长与当前账号/模型计费规则，再由用户授权单次费用；不拿测试通过或“总结”请求推定本轮新增费用授权。

## 扩展入口

ADAPTERS 注册在 platforms.py；抖音/TikTok/西瓜的值为 None，URL 命中即明确拒绝。后续新增时须分别补充 URL/ID 校验、单视频与直播边界、对应提取器、音频选择/分段支持、无凭据实测样本及阶段证据，然后才能开放。不要把所有未知站点交给 generic 提取器。
