# BV1gmJX6SExT probe 修复与检查记录

检查日期：2026-10-22。未提交转写、下载媒体或读取个人 Cookie；不保存原始网络日志、密钥及签名直链。

## 已落实的修复

- yt-dlp 从 `2026.08.19` 更新并固定为 PyPI 查询到的稳定版 `2026.10.21`。
- 解析子进程使用官方支持的 `--compat-options no-certifi`，让 yt-dlp 选择系统信任库。原先默认选择 certifi 的独立证书包，未使用环境的系统 CA。
- 保留 `SSL_CERT_FILE`、`SSL_CERT_DIR`、`REQUESTS_CA_BUNDLE`、`CURL_CA_BUNDLE` 及原有代理变量。子进程环境仍使用白名单，不包含百炼密钥或 Cookie 环境变量。
- TLS 校验始终开启。系统 CA 路径为 `/etc/ssl/certs/ca-certificates.crt`，Python 系统信任上下文载入 154 个 CA；未修改系统证书、代理或路由。
- 失败信息区分证书校验、HTTP 412 和页面初始数据解析错误；不会将可能包含敏感内容的 yt-dlp stderr 原样输出。

## 实测证据与边界

环境状态接口本次返回当前观测：网络策略 `unrestricted/enforced`，百炼密钥与模型变量均为 `ready`。这证明配置已应用，不代表 B站接受当前云端出口。

| 有限测试 | 结果 |
| --- | --- |
| 更新后执行程序 `probe BV1gmJX6SExT`，零重试 | 退出码 2：视频页面初始数据解析失败；没有证书错误，也没有得到可用音轨 |
| GET `api.bilibili.com/x/web-interface/view`，参数为本视频 BV 号，使用标准 User-Agent 和 Referer | HTTP 412，`Content-Type: text/html`，`Server: envoy` |
| 使用 yt-dlp 官方 `--user-agent Mozilla/5.0` 选项做一次诊断，系统 CA、零重试 | 请求跟踪显示视频页面、`/x/web-interface/nav`、`/x/web-interface/wbi/view/detail` 均返回 200；诊断未获得可用元数据，随后在处理空结果时抛出 AttributeError，此项不能算 probe 通过 |

412 响应的可见正文：

> 错误号: 412 由于触发哔哩哔哩安全风控策略，该次访问请求被拒绝。
> The request was rejected because of the bilibili security control policy.

正文带有哔哩哔哩品牌及明确风控提示，未见代理域名策略拒绝标记。证据更支持 B站风控响应，经云端代理转发；`Server: envoy` 单独不能确定响应来源，也不能排除代理缓存。该 412 出现在单独验证的 `/x/web-interface/view` 接口，不能将它等同于当前 yt-dlp 解析失败的直接原因：本次记录到的 yt-dlp 接口路径不同，且均返回 200。HTTP 200 也不保证包含解析器要求的数据。

本次在更新后仅执行一次程序 probe、一次带标准请求头的 metadata API 检查、一次官方 User-Agent 选项诊断，随后停止网络重试。没有关闭 TLS、切换出口、导入登录 Cookie、模拟浏览器指纹或提交百炼任务。没有验证 CDN 音轨读取或百炼远端抓取。

## 验证

- `.venv/bin/python -m unittest discover -s tests -v`：13 项通过。新增覆盖系统 CA 选项实际解析、TLS 校验开启、证书与代理变量传递、凭据隔离及失败消息脱敏。
- `.venv/bin/python -m pip check`：通过。
- `git diff --check`：通过。

## 可行替代方案

1. 在用户自己的正常网络上运行相同版本和修复后的 probe，仍不开登录 Cookie。若可解析，可在本地完成来源准备，并通过私有方式提供字幕或转写文本；当前云端不自动改用其他出口。
2. 使用 B站官方提供的字幕导出能力（如该视频及账号支持），或由用户提供有权使用的字幕、转写文本。项目目前没有字幕导入命令，需要增加离线导入流程后才能接入现有报告来源映射与校验。
3. 若需要继续使用此云端出口，将接口路径、HTTP 412、上述风控正文及发生时间提交给 B站支持核实；向云环境支持核实出口信誉及缓存情况。策略已经 enforced，单纯再放开域名没有解决风控的证据。未解决前不启动收费验收。

## 追加定位：HTTP 200 页面缺少初始数据

2026-10-22 应用户要求，增加一次真实提取流程跟踪（3 个响应），以及一次仅检查视频页面结构的请求；不重试、不下载媒体、不调用百炼。原始页面仅在进程内处理，未保存。结果如下：

| 实际步骤 | 状态与脱敏结构 |
| --- | --- |
| 视频页面 `/video/BV1gmJX6SExT/` | HTTP 200，`text/html`；`<title>` 为空；含 html/head/body、22 个 script、2 个 div；没有视频标题的 Open Graph 元数据 |
| 搜索 `window.__INITIAL_STATE__` 的赋值 | 没有匹配。页面虽有这个名称，但实际只用于脚本条件判断及属性读取，没有点号、方括号、裸变量赋值或同名 JSON 属性形式的数据对象 |
| 页面风险数据搜索 | `_riskdata_` 搜索结果为空；未见可见的验证码、安全验证或明确风控拒绝提示 |
| `/x/web-interface/nav` | HTTP 200，JSON `code: -101`、`message: 账号未登录`；data 包含 isLogin、wbi_img 等字段；提取器继续执行，未在此终止 |
| `/x/web-interface/wbi/view/detail` | HTTP 200，JSON `code: 0`、`message: OK`；data 内存在 View，但不存在可用的 `View.redirect_url` |
| 最终错误 | yt-dlp `bilibili.py:752`，`BiliBiliIE._real_extract` 抛出 `Unable to extract initial state`，对外包装为 DownloadError |

直接失败链已确认：视频响应缺少提取器要求的初始数据赋值；进入回退分支后，当前 yt-dlp 只用详情接口的 `View.redirect_url` 判断是否跳转到番剧解析器，不会将普通 `data.View` 转换成视频初始状态。没有跳转地址时即抛错。因此普通详情接口成功并不会挽救这个失败分支。上一轮诊断器的 AttributeError 是它处理空结果造成的次生错误；本次已通过捕获真正的 DownloadError 和提取器调用位置消除了这项不确定性。

页面分类：这是没有注入视频详情/播放数据的 HTML 页面壳，不是已取得完整数据的正常视频详情页。当前证据不能将它确认为验证码或风控页面，也不能说明缺少数据是由站点渲染、响应版本还是中间环节造成。不能将另一个接口之前返回的 412 风控正文直接套用到这个 HTTP 200 页面。

公开页面提取音频的评估：没有 `__playinfo__` 或 `playurlSSRData` 赋值，没有 DASH、audio、baseUrl/base_url 或 durl 数据字段；`videoData` 也只出现于脚本引用，没有对应 JSON 字段。字符检查未发现 Unicode 替换字符或 NUL，暂不支持“明显解码损坏”的判断。该响应不具备直接提取 AAC 音频地址所需的数据；成功的 `data.View` 仅证明详情接口返回了元数据，不能证明播放接口或 CDN 可用。本轮不增加未验证的页面解析回退，不继续请求播放接口。

到此停止网络测试。本次仅增加诊断记录，没有修改程序逻辑。

## 详情 API 回退实现与最新验收

2026-10-22 按用户要求实施修复，代码保存在项目 `bilibili_extractor.py`，未修改虚拟环境中的 yt-dlp 文件。`video_assistant.py` 在原有隔离环境子进程中调用项目适配器，继续使用系统 CA、TLS 校验和零重试。打包清单也包含适配器与回归测试。

- 页面没有初始数据且未检测到验证凭据时，请求现有 WBI 详情接口。要求整数 `code=0`、有效 `data.View`、返回 BV 与请求完全一致。
- 根据 URL 的 p（默认 1）选择 page 字段对应的分 P；拒绝重复、缺失、不连续页码及无效 cid/时长。即使 API 返回的 pages 乱序，也先按 page 排序，避免取错音轨。
- 向上游提供经过校验的 videoData/upData/tags，并直接复用 yt-dlp 的 `_download_playinfo`、WBI 签名和 `extract_formats`。不另写播放签名或音轨解析实现。详情中的 pages 可供上游使用，避免再请求不必要的 pagelist；回退时不请求章节数据。
- 播放接口的 HTTP 错误、非零业务码或缺失数据会立即抛出带阶段、接口路径及安全错误信息的诊断，不进入其他画质重试。
- `probe --check-audio-range` 只请求 `bytes=0-1023`。必须先确认 206、有效 Content-Range、长度和媒体类型，才最多读取 1024 字节；否则关闭响应且不读取响应体。

实测只运行一次：

```text
.venv/bin/python video_assistant.py probe BV1gmJX6SExT --check-audio-range
音频 Range 检查返回 HTTP 403；未读取响应体，不重试。
```

程序只有在解析元数据、时长检查以及独立 AAC 音轨选择成功后才执行 Range 检查，因此此次已越过初始数据及播放信息阶段，取得符合要求的 HTTPS AAC 直链。直链读取验证失败：CDN 请求返回 403，程序读取响应体之前即关闭连接，随后停止所有网络测试。没有进行 CDN 响应来源诊断，也没有验证百炼是否能抓取该地址；不能据此声称可转写或端到端已通过。没有下载完整媒体，没有提交任何收费任务，没有输出或保存完整签名直链。

本次回归测试共 24 项通过，新增覆盖缺少初始数据、乱序分 P 与默认分 P、BV 不匹配、无效详情/页码/cid/时长、正常页面不触发回退、验证页停止、播放业务错误/HTTP 412 停止，以及 Range 小响应读取、忽略 Range 时零读取、失败后仍输出安全元数据。依赖检查与 diff 空白检查通过。

## 403 的一次有限诊断：携带 yt-dlp 请求头后返回 206

2026-10-22 保留上述解析修复，按用户要求做一次媒体请求诊断。旧签名直链未保存，因此本次重新解析一次，仍使用系统 CA、TLS 校验及零重试；只对所选 AAC 直链发出一次 `Range: bytes=0-1023` 请求，没有切换备用 CDN 或尝试其他出口。

请求头核对结果：

- yt-dlp 的有效公开头包括 User-Agent、Referer、Accept、Accept-Language、Sec-Fetch-Mode；本次返回的数据没有要求 Origin。
- 旧 Range 代码仅显式设置 Referer、Range、Accept-Encoding，实际使用 httpx 默认 User-Agent。其 Referer 与 yt-dlp 一致，但没有传递 yt-dlp 的 User-Agent 和其他公开头。
- 本次按 yt-dlp 的全局、视频、格式优先级合并头，确认发送的 User-Agent 与 Referer 和 yt-dlp 一致；另加 Range 和 `Accept-Encoding: identity`。不发送 Cookie 或 Authorization。

单次媒体响应的脱敏摘要：

| 字段 | 值 |
| --- | --- |
| HTTP 状态 | 206 |
| Server | envoy |
| Content-Type | video/mp4 |
| Content-Length | 1024 |
| X-Cache-Lookup | Cache Miss |
| X-Envoy-Upstream-Service-Time | 33 |
| 应用读取媒体正文 | 0 字节，检查响应头后关闭 |

本次没有错误响应，因此没有可提取的文本错误正文。403 未复现，不能可靠判断上一次 403 是云端代理拒绝还是 B站 CDN 拒绝；`Server: envoy` 和缓存头不足以独立归因。本次重新取得了签名链接，除请求头外，签名时效和 CDN 调度也可能不同，不能将变化严格归因于 User-Agent 单项。当前 206 表明这次请求的 Range 已被接受，不代表完整音频下载或百炼抓取验证通过。

已将公开请求头传递修复保存到项目源码：提取子进程将所选格式的安全头通过私有 JSON 交给父进程，probe 再传给 Range 请求。白名单排除 Cookie、Authorization 及任意其他认证头；头和直链不写入任务元数据。原有详情回退逻辑保持不变。25 项离线测试通过，新增及扩展覆盖头优先级、敏感头过滤、正确 User-Agent/Referer 传递及固定的小 Range。没有再次进行网络验收，以保持一次媒体请求的限制。

**云端单次 403 不足以判断百炼也无法取流。** 百炼使用自己的网络出口、User-Agent、Referer 和抓取时机；签名链接还可能有过期或访问限制。云端与百炼的结果不能相互替代。本项目当前提交逻辑只传 `file_urls`，不会把 probe 的媒体请求头转交给百炼，所以此次带头 206 同样不能证明百炼一定成功。本轮未调用百炼、未提交收费转写，也未下载完整音频或输出签名参数。

## 后续一次实际转写验收

用户另行明确授权后，使用新鲜 AAC 直链与已配置模型提交一次。提交前及收到任务 ID 后均已保存检查点，随后仅查询同一个任务。提供商最终返回 FAILED，顶层错误码与消息均为 `FILE_DOWNLOAD_FAILED`，没有子任务结果及更具体的 HTTP 状态。已停止，没有自动重提，也没有生成没有来源正文支撑的报告。任务证据保存在被 Git 忽略的运行目录中；正式源码文档不记录任务 ID 或签名地址。当前验收结论见 VALIDATION.md。

## BV1LwHv6LEYz：567 下载阻断与直链预检修复

2026-10-23（Asia/Shanghai）按用户要求分析两条失败路径。原直链任务的 `FILE_DOWNLOAD_FAILED` 仅能确认百炼未能取到媒体，没有上游 HTTP 详情；不能将云端的 567 直接认定为百炼同一根因。核对 [百炼当前 API](https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api)：输入为 `file_urls`，未提供媒体下载自定义 Referer/User-Agent 参数。向百炼 API 的请求头添加 Referer 不能据此保证其远端取流携带该头。

本轮非计费有限诊断仅解析两次、各发一个所选音轨的完整 GET，没有改出口或切备用 CDN：

| 检查 | 结果 |
| --- | --- |
| 提取器公开请求头（含 User-Agent、Referer） | HTTP 567，text/html，Content-Length 7352；检查前 4096 字节，未下载媒体 |
| 不带站点请求头，使用新预检函数 | HTTP 567，text/html，Content-Length 7351；只检查 7351 字节错误文本，命中 Restricted Access / security policy / access blocked 固定标记 |

两次响应均带 EO-LOG-UUID。结合 [EdgeOne 官方状态码说明](https://edgeone.ai/zh/document/58009)，证据支持 CDN 安全拦截；567 对应自定义防护、限速或 Bot 管理，具体规则须站点侧用请求 ID 查询。`Server: envoy` 本身不能确定响应来源。本轮不能证明单靠补充请求头能恢复访问，也未尝试绕过该策略。

修复：新直链提交前检查无站点头的公开 GET，明确拒绝或非文件时停止；预检不读取音频正文、不用 Range/HEAD 推断完整下载成功，也不保证百炼可访问。预检和真实下载错误保存有界脱敏诊断；历史状态和两次任务证据不修改、失败回退不重试。新增 `probe --check-public-audio` 非计费入口。

没有新建 ASR、申请上传凭证、上传音频或下载完整媒体；原任务 5 个证据文件逐字节校验和保持不变。此视频仍未取得转写，不宣称已恢复端到端总结。


## 后续云端实现：官方音轨地址与可靠完整读取

本轮继续按用户要求在原 Codex Cloud 环境定位；没有改代理、出口、TLS 或登录态。新鲜音轨签名剩余约 7199 秒，播放 API 响应为当前时间且 no-cache，未发现签名过期的证据。

- 原 cosov 地址无 Range GET 返回 567，但同一类独立音轨的正常 Range 请求返回 206；不能将无 Range 失败认定为云端完全无法取流。
- 播放 API 同时提供同音轨 Akamai 地址，主/备用角色可能变化；上游格式转换未保留 backupUrl，项目此前只使用一个地址。现在保留备用列表，仅当原主地址为已观察到问题的 cosov、备用为 HTTPS Akamai 且资源路径完全一致、无嵌入凭据时，选择 API 原样返回的地址。其他情况保留主地址。不制造域名或编辑签名。
- Akamai 无站点头 GET 可返回 403，正常公开媒体头 GET 返回 200、声明 58,338,224 字节；百炼能否抓取仍应看其真实任务，不用云端公开 GET 结果代替。
- 大响应在文件尾部出现 RemoteProtocolError，整文件及多种分块方式在约 57,540,608 字节处中断；同一区域及文件最后 1024 字节的精确 Range 请求可完整返回，文件尾部存在。未把原因武断归为某个代理厂商或源站文件损坏。
- 下载改为常规 1 MiB、首次和尾部至多 32 KiB 的顺序 Range；关闭持久连接、要求缓存重新验证，验证每段范围、总大小、长度和可用 ETag。完整性或 HTTP 拒绝不重试；只对传输异常回滚当前未完成范围与哈希后重试一次，全文件最多三次，不重开 ASR、不换 URL 或 CDN。
- 撤回第一版新增的强制公开 GET 门槛，保留 `probe --check-public-audio` 手动诊断和历史预检状态的停止语义。默认仍为 direct-url、明确 FAILED 后最多一次上传回退；分块只是同一完整文件的下载方式，不拆分上传或绕过大小上限。

完整下载、解码和官方上传的最终结果以运行目录回执及 VALIDATION.md 最新记录为准；旧失败任务不迁移、不重置回退额度。本机当时使用的 CDN 未记录，不能断言本地成功必然来自特定节点。

## 单次新直链终态与另一条 AAC 音轨

2026-10-22 UTC 用户另行明确授权一次新的 109.6 分钟收费直链试验，禁用上传回退。所选 30232 为 API 返回的同路径 Akamai 备用地址；任务最终失败，错误改为 `CONTENT_LENGTH_CHECK_FAILED`，没有更细的字节数或 HTTP 信息。只有这一个新增 ASR，失败后停止计费操作；保留旧任务和本次终态，不能宣称换 CDN 已修复直链。

随后免费检查同视频公开提供的 AAC 30216：53,899,003 字节，139 个 Range、零传输重试，长度/范围/ETag 一致；完整解码通过，44.1 kHz 双声道、6575.891156 秒，无视频轨。这个结果证明当前云端可以完整获取该音轨，不能把问题概括为云端一概不能下载 B站音频。与 30232 的不同结果涉及具体资源及其传输路径，未证明仅由码率或文件大小造成。

加入 `--bilibili-audio-format` 显式选择 30216 或 30232，默认仍优先 30232。缺少指定音轨时停止，回退沿用任务保存的选择；不自动更换多个音轨、出口或提交更多任务。正式 upload-check 随后也通过：重新下载得到相同文件、完整解码后官方临时上传 HTTP 200，服务端校验和一致，本地音频已清理。`transcription_submitted=false`，没有创建新的上传 ASR；完整记录见 VALIDATION.md。
