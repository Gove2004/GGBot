# GGBot 项目记忆

README 讲「怎么用、怎么装、字段是什么意思」；这里只记**已经定过的设计与原因**——
为什么这样切、踩过什么坑、改这类东西时要守住什么。新决策往这里加一条，别把理由写进 README。

条目格式：结论 → 为什么 → 改动时注意。

## 说话与回合

- **模型只有一条说话通道：回复正文。** 发正文的不是工具，住在 `ggbot/core/outbound.py`：不进 registry、不进 schema、
  不进提示词、面板工具页也看不到，由运行时按行拆成一条条消息代发。→ 早期它叫 `send_text` 工具，模型既能调它又写正文，同回合发两遍；
  先改成 `llm_visible=False` 的内部工具，后来索性整个搬出工具层——工具基座里不再保留这个开关（没有用户的能力域不该留在基座上）。
  工具与正文同轮时，先执行工具、再把正文发出去（"发张表情再补一句"是一个动作）。
  改动时注意：别让任何新工具再承担"说话"职责；正文里的 `@昵称`、`[引用 关键词]` 由 `Outbound.send` 负责；
  面板「工具调用」计数与 `工具 …` 日志行都不含发正文这件事。
- **不接话是一个动作**：调 `silence`（可选 `reason`，只进日志）。→ 早期用"不调工具=不回复"，模型为了填回合硬凑一句。
  改动时注意：新增"这轮不该说"的情形，加工具让它显式调用，不要靠沉默的缺席。
- **模型正文一律照发**，哪怕本轮已经发过东西（工具往返之后的补充话同样是要说的话）。
  改动时注意：不要重新引入 `sent_any` 之类的"已经发过就吞掉"判定，也不要加去重。

## 配置生效边界

- **面板改配置=热生效**，靠的是常驻对象"每次现读" `cfg`（`PresenceManager` 的 `p/r/b/context/backlog` 是 property，
  `VisionClient` 的 model/prompt/max_chars/max_tokens/enabled 现读）。→ 以前在 `__init__` 里抓快照，面板保存了也不动。
  改动时注意：新增配置项想热生效，就在读取处现读，别加 `refresh()`/缓存/监听这类机制。
- **只有建连参数需要重启**：`vision.timeout_sec`（以及 `api_key`/`base_url`、LLM 连接参数）。面板对需重启的字段挂「需重启」徽标，
  列表来自后端 `RESTART_SETTINGS`；`POST /api/config` 会把改不动的路径放进 `rejected` 回给前端，绝不静默丢弃。
- **识图可以在运行中打开**：启动时 `enabled=false` 不建连接，面板打开后第一次调用补建（失败就记一条 warning 并跳过）。

## 上下文、历史与记忆

- **历史里只存"看得见的消息"**：文本按段存、媒体存 `[表情]`/`[图片 …]` 这类标记；工具动作（搜索、写记忆、读文件）不进历史。
  → 好处是下一回合读到的是聊天记录本身，不是执行日志。已知代价：模型看不到自己上回合搜过什么，可能重复检索。
- **窗口满才归档**：`llm.context.max_history_messages` 截条数，`summary_threshold_tokens` 截 tokens，超出的最旧消息交给后台 LLM 提炼进摘要，
  按 `archived_upto` 游标推进；写失败不推进，避免丢消息。归档不阻塞本轮回复。
- **记忆整理阈值随条数放宽**：0.95 起，每多 50 条降 0.01，最低 0.86（`ggbot/llm/memory.py`）。
  → 条数一多，同一件事的不同说法撞不上 0.95，固定阈值等于整理越来越无效；0.86 的下限防误合。CLI `--threshold` 显式给值时照用。
- **图片先描述再入库**：入站图片走识图，正文落成 `[图片]（描述）`；这个形状就是历史里的形状，模型学到的是"我看过了"。

## 富媒体与占位符

- **占位词只有一份词表**（`ggbot/util/text.py` 的 `_MEDIA_TOKEN`，入站标记、出站拦截、日志清洗共用 PLACEHOLDER_RE / PLACEHOLDER_INLINE_RE）。
  提示词里不举带括号的例子，否则模型照着写。纯占位符拦下不发，混排的剔除占位符后余文照发。
- **富媒体用 6–8 位短标识**（`[图片 6ce72460]`），和 `temp/` 落地文件对得上，能 `@`/引用回发。
- **表情靠向量挑图**，不建情绪标签表：索引 v3 + 描述向量缓存，Qwen3 嵌入要 Instruct/Query 前缀、`min_score≈0.50`。

## 生成媒体（media/generate）

- **生成完就走既有发送通道**：`generate_image` / `generate_video` 把产物写到 `temp/tool/media/generate/`，
  再交给 `ToolRuntime.send_media`（file_type 1/2），入库标记仍是 `[图片]` / `[视频]`，不新增占位词、不新增发送形态。
  → 生成与普通转发对模型是同一件事"这里出现了一张图"；再加一套标记就要动词表、日志和测试三处。
- **参数只留 prompt / refs / ratio / seconds**：`image_size`、`video_size` 这些放配置里由部署方定，
  模型不该在一次聊天里挑分辨率。→ 少一个参数就少一种失败与一段 token；生图/生视频已经慢，别把回合浪费在调参上。
- **生图 L2、生视频 L3**：视频慢一个数量级、成本也高，门槛先收紧，`config.levels` 可放开。
- **参考图（`refs`）只收 `temp/` 与 `tool/media/emoji/` 下的本地路径，工具自己不抓外部 URL**
  → 群友发的图本来就落地在 temp/，接口对两种输入都收（公网 URL 或 base64），去拉网图等于白加一处 SSRF 面。
  改动时注意：白名单口径与 `send_image` 本地媒体一致（temp 排除 logs、拒 `config.json`/`.key`/`.pem`），两处要同进同退。
- **`api_key` 为空 = 未启用**（配置里 `enabled=true` 也不下发 schema），与语音克隆缺 key、表情库缺素材同一条口径：
  没素材/没凭据就别让模型调，省一轮必然失败的往返。
- **接口的字段口径以当期文档为准，别照旧脚本抄**：Agnes 视频 7 月那版吃 `width`/`height`/`num_frames`/`frame_rate`，
  2.5 版把这些列为禁止字段（传了 400），改成 `mode` + `seconds`（字符串 4–12 秒）+ `size` 档位 + `aspect_ratio`，
  轮询也要多带 `model_name`；`agnes-video-v2.0` 直接从分组下架（404）。→ 这类第三方生成接口会整版换参数，
  所以只锁"能跑通的那一种形状"，参数收在配置里，实测报错就写进注释。
- 视频只走 `text` / `keyframe`（首帧、首尾帧），不做 `reference`：参考模式要求素材公网可达，本地图没法直链。
- 提交任务遇 `503 video_queue_full` 是队列满，不是参数错：按 `max_retries` 退避重试，别把 503 甩给模型。

## 日志口径

- 只写 `temp/logs/ggbot.log`（轮转）+ `stdout.log`（兜底），不再有 `botpy.log`，根目录不落日志。
- **一条信息只打一行**：入站 `收到 [小明/54019C24] @=1: …`；模型调用的工具唯一形态 `工具 X（admin=昵称/8位id(L2), k=v）`；
  代发正文不是工具，记 `发送: <正文>`（每条消息一行、INFO，不带 admin）；@ 成员、图片描述、语音转写都只出现一次。
- **发正文不暴露给模型，日志里就不该长得像工具调用**——它是"系统替模型说了一句话"，
  所以按发出结果记，失败只说 `发送未成功: …`，不再出现 `工具 send_text（admin=…, content=…）`。
- 每行都带调用方会话列（工具/LLM/识图/作息都是），特权工具（L3+）INFO、空转上下线降 DEBUG。
- 改动时注意：措辞被 `temp/test_log_format.py` 锁住，改文案要同步改测试。

## 工具与权限

- **一切皆 Tool**，按能力域放在根目录 `tool/{core,media,memory,web,fs,mail,admin}/`，每个目录自包含代码 + `config.json` + 数据；
  拷走文件夹就能迁移。`silence`/`time` 属于回合控制所以进 `core`，富媒体（表情/图/视频/语音/生成）合在 `media`，
  记忆与上下文合在 `memory`，联网单独成类，`mail` 保持独立不并进别的分类。
- **目录名、数据目录名、工具名是三套外部契约**：分类目录决定 `category`（面板分组与排序），`data_name` 决定配置与素材落点
  （`.gitignore`、deploy 的 rsync 排除项都按它写），工具名出现在人设提示词、`config.levels` 键与测试断言里。
  → 本轮 `chat/knowledge` 拆成 `core/media/memory/web`，数据目录 `chat/gen`→`media/generate`、`knowledge/web`→`web`，
  `memory_write`/`memory_search` 改名 `memory_save`/`memory_fetch`。
  改动时注意：换目录要在**每台机器各搬一次**已有数据（素材、参考音频、含密钥的 `config.json`），搬漏一次等于工具"未启用"；
  换工具名会让 `config.levels` 里的旧键静默失效（回到代码默认门槛），迁移脚本 `deploy/migrate_tool_dirs.sh` 一起做这两件事。
- **门槛由服务端算**：发送者等级 0–5（`permissions`），工具 `min_level` 1–6（6=永久锁），执行期再校验一次；
  请求只下发当前等级能用的 schema，并按等级稳定排序以复用厂商前缀缓存。
- **管理动作要能被口语调用**：`manage_permission` 支持昵称/8 位 id/openid、等级词、重名给候选、越级与同级保护，
  并有 `list`/`help`——不要让管理员猜参数。
- **表情库为空时不暴露 `send_sticker`**：没素材就别让模型调，省一轮失败。

## 联网搜索

- 默认 Bing RSS（无 key），可切 SearXNG / Tavily。**RSS 的 `pubDate` 是抓取时间不是发布时间，所以 Bing 结果不返回 `published`**
  （返回过会让模型把旧页面认成"今天的新消息"）。
- **没收录的词 Bing 会退回单字匹配**（搜「瑟克斯遗纪」出"瑟"的百科）。工具对这种结果加 `note` 提示模型换词或直说不确定，
  并且不再为它抓正文。查询词写 2–6 个关键词、专有名词配类型词，比整句好。
- 抓正文受 SSRF 约束：只允许公网 http(s)，拒回环/内网/保留地址与 `file://`，限跳转/体积/超时/字符数。

## 发送与平台

- **被动优先、主动兜底**：被动回复用本轮 `msg_id`（seq 每轮从 1 递增），失败自动降级主动发送（主动不带引用）。
  群主动发言要群主开「机器人主动在群聊内发言」，被拒时日志会说清原因。
- **@ 只在 markdown 消息生效**：wire 走 markdown，被平台拒就退回纯文本原文；库里始终存 `@昵称`。
- **发送目标反查**：`sessions.key` 里才是完整 openid，内部主键 `session_id` 不等于它。
- **在线状态机是拟人的**：上线时长/离线间隔带抖动，每轮续期固定秒数（不随轮数递增，有封顶）；
  上线积压只读不回，自然上线时把最后一条积压投进窗口。
- **入站去重两级：内存 `_seen` + 查库兜底**（`_route` 里 `_seen` 未命中再 `storage.message_exists(msg_id)`）。
  → 内存那层拦得住@事件与群全量事件的重复推送，但扛不过重启——每次发版都会重启一次进程，`_seen` 清空，
  平台在重启窗口重推同一条就会回第二遍。查库这层补的正是这个缝；`messages.msg_id` 单独建索引，
  老库靠 SCHEMA 每次启动 `executescript` 自动补。命中重复只打 DEBUG，不刷「收到」行。
- 平台配额/规则会变，写这类注释前先查当期官方文档，别照旧笔记下结论。

## 存储与进程资源

- **SQLite 一次操作一个连接，用完必须 close**（`ggbot/core/storage.py` 的 `_conn()` 是 contextmanager，`finally` 里关）。
  → 云服务器崩过一次：旧写法把每个连接塞进 `_open_conns` 长期持有、从不关闭，一个连接吃掉 `ggbot.db` + `-wal` 两个 fd，
  跑到 505 个连接就顶满 systemd 默认的 soft nofile(1024)，面板 `accept()` 报 Errno 24，asyncio 失败即重试把 2 核 CPU 吃满；
  重启只是清空一轮 fd，几分钟后复发。
  改动时注意：`with conn:` 只管提交/回滚、**不关连接**，别把它当关闭用；也不要为"省一次 open"把连接缓存回一个长驻列表——
  真要复用就按线程缓存并给出明确的生命周期。这条由 `temp/test_storage_fd.py` 锁住（450 轮读写后零长驻连接、fd/句柄不涨）。
- **`Storage.close()` 不释放连接**（连接已随操作关闭），只做 `PRAGMA wal_checkpoint(TRUNCATE)` 把 WAL 归并回主库，
  让退出与测试清理后的库目录干净。
- **`LimitNOFILE=65535` 是兜底，不是修复**：unit 归仓库管，`deploy_server.sh` 每次部署比对 `/etc/systemd/system/ggbot.service`，
  不一致就覆盖 + `daemon-reload`，所以改完 unit push 即生效，不必重跑 `install_service.sh`。

## 安全与部署边界

- `db/config.json` 与 `tool/**/config.json` 有密钥（AppSecret、LLM/Embedding/MiMo/SMTP/搜索 key、`web.token`），
  `.gitignore` 已忽略；日志、面板、提示词都不出现明文密钥（`GET /api/config` 脱敏）。
- `tool/fs` 不能读配置、密钥与数据库文件；发送本地媒体只允许 `temp/` 与 `tool/media/emoji/`；邮件收件人白名单、附件只走 temp 运行时文件。
- 面板：Cookie 登录（HttpOnly+SameSite）、每 IP 限流、CSP 无内联脚本、WS 鉴权、`trust_proxy` 默认关（防伪造 XFF 绕过限流）。
  `web.host=0.0.0.0` 时面板对整个公网可见，上云要么配 `web.token`，要么只绑 127.0.0.1 走反代。
- **部署走 GitHub Actions：push master → rsync → 远端 `deploy/deploy_server.sh` 重启 + 健康检查**（细节见 README「部署到云服务器」）。
  同步排除 `config.json`/`*.db`/`temp/`/素材目录，服务器上的密钥和对话库不会被覆盖。
  服务器进程统一交给 systemd（`deploy/install_service.sh`），装完要删掉 crontab 的 `@reboot start.sh` 行，否则抢单实例锁。

## 验证约定

- 离线回归：`sh temp/run_all.sh`（`PYTHONPATH=.` + venv python），每个 `temp/test_*.py` 自己打 `[PASS]/[FAIL]`，
  以退出码为准；需要联网/真实 LLM 的测试单独跑，别塞进套件。
- UI 改动用 `temp/serve_preview.py` + 浏览器实测（快照/DOM 尺寸/控制台报错），不要凭代码断言"应该能渲染"。
- 每次改动同步 README；**文档不写代码里没有的行为**。
