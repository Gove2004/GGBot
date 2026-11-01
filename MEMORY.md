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
  调用它就在**当轮立即收轮**：工具结果写完就 `return`，既不再追加一次请求，也不发同轮的正文（`tool/__init__.py` 里 `ctx["silence"]` 那一支）。
  改动时注意：新增"这轮不该说"的情形，加工具让它显式调用，不要靠沉默的缺席；也别在收轮后再补一次 LLM 调用"问个说法"。
  现状要说实话：本地 `temp/logs/ggbot.log` 里 `silence` 一次都没出现过（模型至今没用过这个动作），所以这条只验了代码路径，没验真实使用率。
- **写在正文里的"不说话"标志按沉默处理，不是按一句话处理**：模型常把不接话写成正文里的 `[SILENCE]` / `[沉默]`，
  运行时代它收轮（`ctx["silence"]`，与调 `silence` 同等），一个字符都不发、也不再追问一次。
  → 线上真的把 `[SILENCE]` 原样发进了群：群友跟着打这四个字，这两条又作为历史记录喂回下一轮，
  模型越发确信"这里的规矩就是这样"。所以标志词必须进那份唯一词表，拦截要发生在发送之前。
  改动时注意：同轮既有工具产物又有沉默标志时，工具照发、标志不发；夹在真话里的字样按占位符剔除，余文照发。
- **模型正文一律照发**，哪怕本轮已经发过东西（工具往返之后的补充话同样是要说的话）。
  改动时注意：不要重新引入 `sent_any` 之类的"已经发过就吞掉"判定，也不要加去重。

## 配置生效边界

- **面板改配置=热生效**，靠的是常驻对象"每次现读" `cfg`（消息流各模块把配置项挂成 property：作息与上限在
  `pm.inbound.p/r/backlog_limit/online_limit`、上下文在 `pm.context.history_limit/budget_tokens/system_prompt`、
  归档在 `pm.archiver.batch/overflow/enabled`，`VisionClient` 的 model/prompt/max_chars/max_tokens/enabled 现读）。→ 以前在 `__init__` 里抓快照，面板保存了也不动。
  改动时注意：新增配置项想热生效，就在读取处现读，别加 `refresh()`/缓存/监听这类机制。
- **只有建连参数需要重启**：`vision.timeout_sec`（以及 `api_key`/`base_url`、LLM 连接参数）。面板对需重启的字段挂「需重启」徽标，
  列表来自后端 `RESTART_SETTINGS`；`POST /api/config` 会把改不动的路径放进 `rejected` 回给前端，绝不静默丢弃。
- **识图可以在运行中打开**：启动时 `enabled=false` 不建连接，面板打开后第一次调用补建（失败就记一条 warning 并跳过）。

## 消息流（拆模块与状态模型）

- **一条链按职责切成七块，不再有一千行的 `presence.py`**：`ggbot/core/presence/` 包里 `manager`（组装 + 2 秒 tick + 面板读的 `status()`）、
  `state`（这个会话此刻的作息，只有作息）、`inbound`（权限闸门 → 入库 → 按此刻作息分流 → 在线抖动窗口聚一批）、
  `decision`（一批消息要不要说话：拼上下文 → 模型自己说或调 `silence`；回合外发言也在这里）、
  `context`（历史窗口 + 记忆尾注怎么拼）、`archiver`（满窗消化）、`defaults`（内置人设与记忆块文案）；出站单独一份 `ggbot/core/outbox.py`。
  → 之前那 887 行里四件事共用一堆局部变量：改作息要通读入站、改上下文要通读决策，回归测试只能造假对象整块喂，
    任何一段出错都得先搞清楚它在哪一段。切开后每块只认自己的输入，测试直接 `import ggbot.core.presence.decision` 替那一段。
  改动时注意：`PresenceManager` 只留"装配 + 心跳 + 面板口径"，别往它身上加业务方法；工具/日程/面板要发言一律走
  `pm.speak` → `TurnDecider.speak`，不要在工具里直接调 bot API 另开第二条出站（那条不过锚点、不过频控、不记回执）。

- **一条消息两条时间线，各一列**：`messages.read_state`（`unread`/`read`）与 `messages.reply_state`
  （`waiting`/`answered`/`failed`/`skipped`/`none`）。「到达」不占列——行存在 + `ts` 就是到达，`role` 就是方向。
  → v2 那阵只有一列 `status`，混了四件事：`pending` 同时说"没读"和"没回"，`seen` 把"读过了"和"不用回"糊在一起。
    于是"上线把积压只读不回"只能把 `pending` 改成 `seen`，改完就再也分不出这条是**看过了不打算回**还是**看过了还没回**，
    面板的「待处理」和下一轮的上下文过滤都只能猜；助手自己发出去的话也得靠"别把 assistant 当待处理"这种补丁绕。
  改动时注意：`status` 这一列现在只存在于 v2 的旧库里——v3 开库只 `executescript(SCHEMA)`，不再就地迁移，
  要把老数据带过来只能另写一次性脚本，别把迁移代码放回开库路径。`sessions.archived_upto` 认的是 `messages.id`（自增主键），
  任何改动都不能重排已有行的 id，那等于把所有会话的归档位置作废。
  `mark_backlog_read` / `finish_backlog` 的 `WHERE` 都带 `reply_state='waiting'`：已经记过结果的行不能被后到的"再读一遍"抹掉。
  组装上下文时排除 `waiting`（那条本来就在本轮的 `【本轮消息】` 里），面板与 CLI 的「待处理」数读的都是 `reply_state=waiting`。

- **出站两本账落库，不在进程里**：`reply_anchors(session_id, msg_id)` 记「这个锚点下次 `msg_seq` 排到几、窗口什么时候过」，
  `sends` 记每一次发送尝试（实际走的路 `passive`/`proactive`/`fallback`、送达与否、被拒原因）。
  → 序号原来放在消息流里的一个进程内 dict（`_anchor_seq`），**重启就归零**：发版必然重启，重启后第一条被动回复就撞上
    平台拒绝的重复 `msg_id`+`msg_seq`，只能靠降级主动发送兜住送达，白烧一份主动额度。回执原来只有日志，
    日志按行拼、会轮转、发版即丢，"这条到底发出去了没"在面板上读不到。
  改动时注意：`expires_at` 只在锚点第一次插入时写，过期那一格重开、序号回到 1，别拿它当"这条回复用了几次"的计数器往外暴露；
  `sends` 只留最近 2000 条（`commit_turn` 时剪），它是账本不是消息正文，别往 `messages` 里塞发送状态；
  面板会话详情「最近出站」与 `cli context` 末尾那几行读的都是 `sends` 表，**不是日志**；
  清空单个会话（`clear_session_context`）和 `--reset-chat` 连这两本账一起删——留着锚点，下一条新消息会接着旧序号往下排。

## 上下文、历史与记忆

- **历史里只存"看得见的消息"**：文本按段存、媒体存 `[表情]`/`[图片 …]` 这类标记；工具动作（搜索、写记忆、清上下文）不进历史。
  → 好处是下一回合读到的是聊天记录本身，不是执行日志。已知代价：模型看不到自己上回合搜过什么，可能重复检索。
- **窗口满才归档，归档只写记忆、不再产摘要**：历史只装 `sessions.archived_upto` 游标之后的最近
  `max_history_messages`（默认 1024）条；装满那一轮把**窗口里最旧的** `archive_overflow_messages`（默认 512）条按每批
  `archive_batch_messages`（默认 64）交给后台 LLM 提炼，逐批 `memory.commit()` 写入，游标一推进，窗口就从 1024/1024 回到 512/1024，
  再由新消息一条条攒满（512 ÷ 64 = 8 批）。某批写失败就停在这批，游标不推进，下轮重来。归档在后台任务里跑，不阻塞本轮回复。
  → 为什么缩窗而不是让窗口一直滚：顶格 1024 时每条新消息都把窗口往后挪一格，缓存前缀每轮全废，那些旧消息还从没进过记忆；
  锯齿留着最新那一半不动，前缀只在消化那一次整体失效，旧对话也真的沉淀成了记忆。
  → 以前的「会话摘要」层整个删掉了（连 `sessions.summary` 的读写一起）：128k 上下文装得下 1024 条历史，摘要不再省 token，
  反而每轮都在变，等于在缓存前缀里塞一个每次都不同的块；摘要说过的话和历史里的原话也会同时出现，模型分不清哪个算数。
  改动时注意：SCHEMA 里没有 `summary` 列，读写都别加回来；`archive_overflow_messages` 先夹 4096、再夹到「窗口减一批」，
  这样缩窗后至少还剩一批历史（一批就能装满窗口时等于不归档）；游标远落在窗口之前时（归档关过一阵，或连续几批没落地）
  先从库里最旧的一条批批往记忆补，追上窗口才恢复锯齿——这是有意的，别让归档跳过没进过记忆的历史。
- **提炼出的是结构化事实，不是一句话**：`extract_memories` 每条返回 `{content, kind, subject, importance, expires_in_days}`，
  模型退回旧形状（纯字符串数组）也收。→ 只有一句正文的记忆没法分组、没法取代、没法到期，"谁"和"什么时候作废"必须在写的时候问出来，
  事后靠正则猜人名只会猜错。
- **一次请求按缓存顺序拼**：全量 tools 数组 → 静态人设 → 最近历史 → 相关记忆（top-3）→ 本轮消息。
  → 前两段字节级跨会话不变，厂商前缀缓存才命中得了；记忆段和本轮消息每次都变，只能放尾巴上。
  改动时注意：想把任何"每次都不同"的东西（动态人设、按等级裁剪的 schema、摘要）往前挪，先想清楚它会不会劈开前缀。
- **人设在配置里，代码只留兜底**：整段 system prompt = `llm.system_prompt`，留空（或全空白）就用 `presence/defaults.py` 的 `SYSTEM_TEMPLATE`；
  面板侧栏「人设」页是一整块编辑器，读写走独立的 `GET/POST /api/system-prompt`，刻意不进 `POST /api/config` 的白名单。
  → 为什么能改：调语气是日常动作，以前每改一句都要动代码重启。为什么单独一条路由：这段是缓存前缀，存一次就失效一次，
  不该被设置页那套批量保存顺带碰到；一个键只留一条写入路径。
  → v3 的内置默认在最前面加了【你的底子】：GGBot 由 Gove 研发、当前 v3、开发测试阶段、成本由 鹿LwL 赞助、扮演一位女生。
    这几句写成"给你自己看的来历，不是台词"，人设里明确要求不主动交代——它是模型的自我口径，不是自我介绍的稿子；
    被追问是不是机器人时用玩笑岔开，不去编身世、也不论证自己多像人。说话方式/断句/语气/防注入那几段原样保留。
  → 改动时注意：`pm.context.system_prompt` 每轮现读，保存即生效不用重启；老配置没这个键要靠 `Config._migrate` 补空串
  （`set_by_path` 拒绝创建新键，不补就永远存不进去）；20000 字上限前端和后端各挡一道；这页不在 1s 轮询里，
  编辑器有未保存改动时切页回来也不被后端值覆盖（要丢弃得点「重载」），别把它接进 `refreshAll`/`poll.js`。
- **不做工具提示词注入**：`prompt_hint` 与 system 里的【工具提示】段删了。→ 好的工具描述写在 schema 的 `description` 里，
  下发列表本来就有；提示词里再复述一遍等于每个可能用到的工具都花一份 token，还和 `description` 有两处口径。
  代价是表情/语音这类靠暗示拉触发率的调用变少，实测由 `temp/test_tool_usage.py` 观察。
- **长期记忆只有两类：`profile`（人物档案）与 `event`（事件流水）**，各按各的活法清理。
  → 以前 `fact`/`episode`/`preference` 一堆词混着一张表，"小明喜欢美式"和"周五要交稿"用同一套规则处理，
  结果是档案越攒越厚、约定过期了还在提示，谁也分不清哪条该作废旧的哪条该留。profile 只认最新说法、不按年龄删；
  event 按时间追加，靠到期/淡出退场。v2 用过的 `fact`/`episode`/`preference` 这些旧词在 v3 不再归一，旧库直接不用（口径见「存储与进程资源」）。
  改动时注意：`kind` 只有这两个值，任何写入路径（工具/面板/CLI/归档）都归一到它，非法值按 profile 处理，别加第三类。
- **取代发生在写入那一刻，不靠事后清理**：`MemoryStore.commit(facts)` 先给每条新事实找相关旧条目
  （同作用域向量近邻 ≥0.55 取前 5 ∪ 同 `subject` 的档案最多 8），**整批一次**交给 LLM 判
  `new`/`update`（改写那条）/`drop`（不写）+ `retire`（这条让哪些旧的作废）。
  → 只靠余弦去重要么合并不了（"住在杭州"和"最近搬到杭州了"距离挺远），要么误合（同一人两件不相干的事距离挺近）；
  "谁推翻了谁"是语义判断，只有模型判得了，而且写的时候判最便宜——那时候两条都还在眼前。
  → 一批只发一次请求：一次窗口满要写 8 批，每条一发就几十次调用，慢也贵。
  改动时注意：判定失败（没 LLM、`reconcile=false`、模型乱答、嵌入挂了）一律**退回直写 + 0.95 余弦去重**，
  绝不因为判不了就不记；`retire` 只在"状态已变/约定已完成/说法矛盾"时才给，不相干不许删——
  删错一条比多留一条近似记忆代价大得多。
- **遗忘扫描 `sweep` 按固定四步走：到期 → 事件淡出 → 超上限淘汰 → 近似重复合并**，一次只干一件事，日志一行按这四步报数。
  → 顺序是有意的：到期和淡出先把明显没用的清掉，剩下的才轮到按保留分腾上限，最后才做要过一遍全库向量的合并。
  改动时注意：**自动触发那次（累计写入够 `sweep_every_writes`）跳过第 4 步**——前三步纯本地筛选，合并是 O(n²) 的距离比较，
  不该出现在对话回合附近的写入路径里；面板「立即遗忘扫描」、CLI `memory sweep` 和启动后台那一次才做全四步。
- **保留分只有一个算法**（`retention = 0.4×重要度 + 0.4×新鲜度 + 0.2×命中`，半衰期 = `forget_after_days`）：
  上限淘汰、合并择优、面板「保留」列与 CLI 那一列读的都是它。→ 一条记忆该不该留是三件事叠出来的，
  单看年龄会把"天天被想起"的常识删掉，单看重要度会把标了 ★5 再没提过的事永久留着。
  改动时注意：低分不是"该删"，是"超出上限时先腾谁"；别为它另建一套分数或加缓存。
- **命中只由聊天侧记**：上下文注入的 top-3 与 `memory_fetch` 传 `touch=True`；面板的浏览和语义检索都不传。
  → "看一眼"不等于"想起来过"，否则运维把记忆页点一圈，那些条目就永远淡不掉了，遗忘等于没生效。
  改动时注意：新增任何读取路径先想清楚它算不算一次回忆，默认不记。
- **记忆合并阈值随条数放宽**：0.95 起，每多 50 条降 0.01，最低 0.86（`ggbot/llm/memory.py`）。
  → 条数一多，同一件事的不同说法撞不上 0.95，固定阈值等于合并越来越无效；0.86 的下限防误合。CLI `--threshold` 显式给值时照用。
- **面板的「记忆」页按人分组，每组末尾一个「清掉这一组」**，检索态按分数平铺不分组。
  → 一屏 200 条平铺没法看，人要的是"关于小林的档案"和"还挂着哪些约定"；分组的另一个理由是整组清理正好落在
  `delete_memories_by(subject/kind/session)` 这一个函数上，面板和 CLI `memory purge` 共用同一条护栏：**三个条件全空就一条不删**，
  清空整库只有 `main.py --reset-db`。
- **图片先描述再入库**：入站图片走识图，正文落成 `[图片]（描述）`；这个形状就是历史里的形状，模型学到的是"我看过了"。
- **给模型看的名字带一条稳定的 8 位尾巴**：历史和本轮消息的每一行都写成 `[昵称#openid前8位]`（`context.sender_label`），
  没有昵称时只剩这 8 位，不会写成 `id#id`。
  → QQ 昵称既会改又会撞车：只写昵称时，同一个人换了昵称被当成两个人、两个人撞了昵称被当成一个人，
  模型分不清这句话在回谁（「分不清人」）。这 8 位是唯一不会变的。
  → 入库的展示名**先认 `actors` 表**（面板/`manage_permission` 给这个人定的叫法），没登记过才退回这次事件带过来的昵称。
  改动时注意：归档提炼那一路必须用裸昵称（`sender_label_plain`）——那一侧产出的是记忆的 `subject`（"这条是关于谁的"），
  带上 `#id` 会把分组和检索弄脏；`@` 的解析仍走不带 `#` 的 `@昵称`，人设里也点明了这标记只给模型看、别照抄进正文；
  取名只在入站那一次查 `actors`，别在上下文拼装的热路径上加查库。

## 富媒体与占位符

- **占位词只有一份词表**（`ggbot/util/text.py` 的 `_PLACEHOLDER_WORDS`，由同一个 `_token()` 拼出形状，入站标记、出站拦截、日志清洗共用 PLACEHOLDER_RE / PLACEHOLDER_INLINE_RE；沉默类词也在这一份里，另配一个只认它的 SILENCE_RE 用来判"整条正文都是标志"）。
  提示词里不举带括号的例子，否则模型照着写。纯占位符拦下不发，混排的剔除占位符后余文照发。
  → 形状必须连着**括号里那一小段标识**一起吞：8 位短 id（`[语音 6ce72460]`）、冒号后的表情名（入站 `[表情:偷笑]`）、空格后的文件名（`[图片 cat]`）。
  入站标记长什么样，模型就照抄什么样——旧形状只吞 id，模型把 `[表情:偷笑]` 写进正文时那串脏括号会原样发进群，又作为历史记录喂回下一轮，越用越像"这里允许写"。
  改动时注意：这套正则只用于出站正文与日志清洗，**入站内容不剥**（`[表情:偷笑]` 是"有人发了个表情"的唯一记录，剥了就等于删数据）。
- **QQ 官方自带表情发不出去，是平台限制、不是实现缺口**：发送侧只有 `msg_type=0` 文本 / `2` Markdown / `7` 富媒体，
  内嵌表情语法 `<emoji:id>` 官方写明「仅频道可用」（且只支持 type=1 系统表情），表情表态也只有频道支持（口径见 2026-11 官方《消息类型》《文本交互》）。
  → 群里/私聊收得到小黄人（`<faceType=1,…>` 还原成 `[表情:偷笑]`），发回去只能是表情包里的图片（`emoji_*` 三件套）
  或正文里的 Unicode 表情字符（不走这套拦截，按普通文本发出）。
  改动时注意：**别为 `<emoji:id>` 加发送分支**——群/私聊里它只会原样显示成标签文字，等于把脏文本发给用户；
  要扩"表情"能力只有往表情包加图片这一条路（挑图靠向量，见下条）。
- **富媒体用 6–8 位短标识**（`[图片 6ce72460]`），和 `temp/` 落地文件对得上，能 `@`/引用回发。
- **表情挑图靠向量，不建情绪标签表**：Qwen3 嵌入要 Instruct/Query 前缀、`min_score≈0.50`；没配 embedding 时按字面（bigram）兜底，
  此时不分档也不给邻近项。→ 情绪表要人维护且永远漏，描述一句自然语言既给人读也给向量用。
- **表情库三层各管一段**：`emoji.py`（`EmojiLibrary` 编排）/ `_emoji_store.py`（`EmojiStore`：素材目录 + 三份 JSON 读写）/
  `_emoji_match.py`（`EmojiMatcher`：嵌入、排序、同档里选哪张）。→ 原来一个文件里混着"文件在哪、字节怎么写、图怎么挑"，改一处要读全篇。
  改动时注意：下划线开头 = 不是工具，基座扫模块时跳过；别把内部层写成第 4 个工具，也别把挑图逻辑塞回 `emoji.py`。
- **目录是唯一真相，三份 JSON 全是它的缓存**：`index.json` v4（`{file, desc, added_at}`，进版本库，描述是人写的）、
  `vectors.json`（描述向量，派生，gitignored，删了能重算）、`usage.json`（发过几次/上次何时，派生，gitignored）。
  → 分开是因为重写代价不同：整包重算向量要几秒，只改一句描述不该连带把别的描述嵌一遍（批量 64 条一次）。
  改动时注意：写入按脏标记只落改动的那一份；索引格式升版是"读时兼容"（`ensure()` 发现版本号落后就标脏，下次 flush 顺手升级），
  别加启动迁移脚本。
- **浏览是只读路径**：面板列表一行不写盘、一次不嵌入（3000 张实测冷读 141ms、热读 1.1ms）。→ 打开页面不等于改库，
  也不该因为有人看了一眼就重算向量。向量只在真的挑图那一轮补齐。
- **扫目录缓存 = 3 秒 TTL + 素材根目录 mtime**：目录动过（增删文件）立刻重扫；子目录里塞图仍可能晚 3 秒，所以面板「刷新」走 `?force=1`。
  改动时注意：任何写入都要把扫描缓存作废，否则"传完图列表里看不见"。
- **同档里先躲开刚发过的、再挑用得最少的**；整库没一张够像时把邻近几张（带分数）如实回给模型（`near`），由它决定退一步发还是改口说话。
  → 每次都给同一张最配的，群友看着像坏掉的点唱机；硬凑一个"最接近"发出去，等于替模型做了一个它本来能做的判断。
- **入库只有一条路**（`emoji_save` 与面板上传共用同一个函数）：按内容摘要（sha1 前 12 位）命名并去重——同一张图换扩展名再传也算重复；单张上限 8MB。
- **表情库是一个统一目录，不分「内置/收藏」**：图片放 `tool/media/emoji/` 就算入库，索引 v4 的条目只有 `{file, desc, added_at}`。
  → 旧 `source` 字段只是文件所在子目录的前缀（`stickers/`→builtin、`user/`→user），而内置表情早在清空素材那次就没了，
    「内置」变成一个永远为真的假类别；`emoji_save` 收的图和人手工放的图对模型是同一件事——都是"库里有这张"。
    两边的 `desc` 也都是同一套（识图写或文件名兜底），按来源筛选没有任何信息量。
  改动时注意：`_scan()` 是**整个素材目录递归**，老机器上 `stickers/`、`user/` 里的图会被一并收进库，不用搬文件；
  上限按整库算（`max_stickers`，默认 200）；素材图不进版本库（`.gitignore` 用 `tool/media/emoji/**` + 两条 `!` 白名单，
  只放行 `index.json` 与 `config.example.json`），部署侧更要整目录排除（`deploy/rsync-exclude.txt` 里的 `tool/media/emoji/`）——
  这个目录整个是服务器本地状态，仓库里那份空索引对它没有意义，而 `--delete` 清掉服务器上的图就是清掉用户素材。

## 生成媒体（media/generate）

- **生成完就走既有发送通道**：`generate_image` 把产物写到 `temp/tool/media/generate/`，
  再交给 `ToolRuntime.send_media`（file_type 1），入库标记仍是 `[图片]`，不新增占位词、不新增发送形态。
  → 生成与普通转发对模型是同一件事"这里出现了一张图"；再加一套标记就要动词表、日志和测试三处。
- **参数只留 prompt / refs / ratio**：`image_size` 放配置里由部署方定，
  模型不该在一次聊天里挑分辨率。→ 少一个参数就少一种失败与一段 token；生图已经慢，别把回合浪费在调参上。
- **生图 L4（心腹）**：一次要等几十秒，也是真花钱——"替群里做一件有成本的事"该跟 `clear_context`
  同档，而不是跟发一张现成图（`send_image`，L2）同档。`config.levels` 可按群放开。
- **生视频（`generate_video`）在 v3 下线**：异步任务、轮询到 `completed` 再下载，一段几十秒到一分多钟，
  失败面（队列满、超时、递归找 `.mp4` 地址）比生图宽一倍，群里几乎没人真等得起。
  → 代码、配置键、README/MEMORY 同批删；`send_video`（发一份现成视频）留着，那是另一件事。
- **参考图（`refs`）只收 `temp/` 与 `tool/media/emoji/` 下的本地路径，工具自己不抓外部 URL**
  → 群友发的图本来就落地在 temp/，接口对两种输入都收（公网 URL 或 base64），去拉网图等于白加一处 SSRF 面。
  改动时注意：白名单口径与 `send_image` 本地媒体一致（temp 排除 logs、拒 `config.json`/`.key`/`.pem`），两处要同进同退。
- **凭据与固定素材缺失 = 未启用**（`api_key` 为空、或语音克隆缺 `audio/voice_ref.wav`，配置里 `enabled=true` 也不进下发列表）
  → 这些都是部署时就定好、运行中基本不变的东西，判定一次就够，没凭据/没素材就别让模型调，省一轮必然失败的往返。
  **会随聊天长大的素材不套这条口径**：表情库为空时 `send_emoji` 照样下发，调用时才如实失败（详见「工具与权限」的空表情库那条）。
- **接口的字段口径以当期文档为准，别照旧脚本抄**：Agnes 视频 7 月那版吃 `width`/`height`/`num_frames`/`frame_rate`，
  2.5 版把这些列为禁止字段（传了 400），改成 `mode` + `seconds`（字符串 4–12 秒）+ `size` 档位 + `aspect_ratio`，
  轮询也要多带 `model_name`；`agnes-video-v2.0` 直接从分组下架（404）。→ 这类第三方生成接口会整版换参数，
  所以只锁"能跑通的那一种形状"，参数收在配置里，实测报错就写进注释。
- 视频只走 `text` / `keyframe`（首帧、首尾帧），不做 `reference`：参考模式要求素材公网可达，本地图没法直链。
- 提交任务遇 `503 video_queue_full` 是队列满，不是参数错：按 `max_retries` 退避重试，别把 503 甩给模型。

## 日志口径

- 只写 `temp/logs/ggbot.log`（轮转）+ `stdout.log`（兜底），不再有 `botpy.log`，根目录不落日志。
- **日志跨重启追加，不是每次启动清空**：`RotatingFileHandler` 只要 `maxBytes > 0` 就把 `mode="w"` 换成 `"a"`，
  所以以前那个 `mode="w"` 是个从未生效的写法。→ 保留历史是有用的：线上出问题往往是几小时后反馈、中间可能已经推过一次版，
  上次进程的报错行还在文件里才查得到（`[SILENCE]` 那次就是靠重启前的日志定位的）；体积不用怕，轮转封顶 5MB × 6 份。
  改动时注意：`setup_logging` 里显式写 `mode="a"`，别改回 `"w"`——它不改行为，只会让代码和文档又开始说谎。
- **一条信息只打一行**：入站 `收到 [小明/54019C24] @=1: …`；模型调用的工具唯一形态 `工具 X（admin=昵称/8位id(L2), k=v）`；
  代发正文不是工具，记 `发送: <正文>`（每条消息一行、INFO，不带 admin）；@ 成员、图片描述、语音转写都只出现一次。
- **发正文不暴露给模型，日志里就不该长得像工具调用**——它是"系统替模型说了一句话"，
  所以按发出结果记，失败只说 `发送未成功: …`，不再出现 `工具 send_text（admin=…, content=…）`。
- 每行都带调用方会话列（工具/LLM/识图/作息都是），特权工具（L3+）INFO、空转上下线降 DEBUG。
- **默认等级是 DEBUG**（代码兜底、`config.example.json`、本地与线上的 `db/config.json` 四处一致），要 quieter 就在面板「日志等级」里切，保存即热生效。
  → 用户两次报「看不到 DEBUG」，原因有叠加的两层，改一层没用：**文件里没写**（以前默认 INFO，且 `deploy/ggbot.service` 曾钉死 `--log-level INFO`——启动参数盖掉配置，unit 里现在不带这个参数）；**界面又默认筛掉**（日志页下拉和 `GET /api/logs` 的 `level` 都曾默认 `INFO 及以上`）。现在两处默认都是 DEBUG。
  改动时注意：手工起 `start_*.sh` 才用 `--log-level DEBUG` 这个参数，别把它抄进 unit；DEBUG 只放业务行，第三方库固定 WARNING，体积靠 5MB × 5 轮转封顶。
- 改动时注意：措辞被 `temp/test_log_format.py` 锁住，改文案要同步改测试。

## 工具与权限

- **一切皆 Tool**，按能力域放在根目录 `tool/{core,schedule,media,memory,web,mail,admin}/`，每个目录自包含代码 + `config.json` + 数据；
  拷走文件夹就能迁移。`silence`/`time` 属于回合控制所以进 `core`，`schedule` 单独成类（"排一件事等会儿做"跟回合控制不是一回事，还要配巡检间隔与宽限），
  富媒体（表情/图/视频/语音/生成）合在 `media`，记忆与上下文合在 `memory`，联网单独成类，`mail` 保持独立不并进别的分类。
- **文件读写分类（`tool/fs`）已整体删除**：机器人只做"在群里说话 + 联网查 + 发媒体"，不再替人操作服务器上的文件，Agent 方向暂时不做。
  → 留着就等于一份带密钥读取面、一套沙箱拒绝清单和两个特权工具位（L3/L4），而这些代码长期没人用；删的时候连 `fs` 的
  schema、`config.example.json`、等级说明与本地测试一起删净，`temp/test_tool_layout.py` 断言仓库里不再残留 `tool/fs` 目录。
  改动时注意：面板分类映射（`static/js/tools.js` 的 `TOOL_CATS`/`TOOL_CAT_ORDER`）、`manage_permission` 的六级能力文案、
  README 的等级表都要跟着分类表走；线上那台 `tool/fs/config.json` 被 rsync 的 `config.json` 排除项护着，`--delete` 清不掉，
  目录壳要手动删（没有 `__init__.py` 就不会被注册表发现，只是留个空壳）。
- **目录名、数据目录名、工具名是三套外部契约**：分类目录决定 `category`（面板分组与排序），`data_name` 决定配置与素材落点
  （`.gitignore`、deploy 的 rsync 排除项都按它写），工具名出现在人设提示词、`config.levels` 键与测试断言里。
  → 本轮 `chat/knowledge` 拆成 `core/media/memory/web`，数据目录 `chat/gen`→`media/generate`、`knowledge/web`→`web`，
  `memory_write`/`memory_search` 改名 `memory_save`/`memory_fetch`，表情三件套跟着所在数据目录改名
  `send_sticker`/`sticker_list`/`sticker_save` → `send_emoji`/`emoji_list`/`emoji_save`（模块 `tool/media/sticker.py`→`emoji.py`）。
  改动时注意：换目录要在**每台机器各搬一次**已有数据（素材、参考音频、含密钥的 `config.json`），搬漏一次等于工具"未启用"；
  换工具名会让 `config.levels` 里的旧键静默失效（回到代码默认门槛），迁移脚本 `deploy/migrate_tool_dirs.sh` 一起做这两件事。
  只换工具名时数据契约别顺手改：`data_name` 与素材目录留着——它们对应磁盘上真实存在的东西和服务器上已写好的配置，
  改名等于要搬文件；`emoji.py` 与数据目录 `emoji/` 同名是安全的
  （同包内模块文件优先于无 `__init__.py` 的目录，实测 `import tool.media.emoji` 拿到的是 `.py`）。
  真要改契约时走"读时兼容、不搬文件"这条路：表情库并成一个统一目录后，扫描改成递归整目录，
  老机器上的 `stickers/`、`user/` 里的图照样进库、只是不再当分类；配置键 `max_user_stickers`→`max_stickers`
  不用迁移，`load_config` 按 defaults 补齐新键并回写，老键留在文件里变成死字段。
- **门槛只在执行期生效**：发送者等级 0–5（`permissions`），工具 `min_level` 1–6（6=永久锁）。启用的工具**一律全量下发**，
  不按等级裁剪 schema，按 (门槛, 分类, 名称) 固定排序，好让所有会话拿到同一份列表；越权调用由执行器直接失败返回
  「权限不足：{name} 需要 L4，当前 L2」，未知工具名返回「未知工具 {name}」。
  → 以前按等级裁剪，等于把缓存前缀按人群切成几份，等级不同的会话互相挤不掉前缀；而"低等级看不到"也没真挡住什么——
  模型照样能凭记忆编出工具名，最后仍要靠执行期那一次校验。6 级是唯一的列表例外：谁都不下发，面板也当它是锁。
  改动时注意：`config.levels` 覆盖的是门槛值而不是可见性，别把它当开关用（`enabled=false` 才是不进列表的理由）。
- **管理动作要能被口语调用**：`manage_permission` 支持昵称/8 位 id/openid、等级词、重名给候选、越级与同级保护，
  并有 `list`/`help`——不要让管理员猜参数。
- **空表情库照样下发 `send_emoji`**：调用时才如实返回「表情库为空（图片放 `tool/media/emoji/`…）」，模型改用说话就行。
  → 早期是"没素材就不给看 schema"，但那要每次请求扫一遍目录才能让列表稳定；素材会长会删，把它当下发条件等于每轮重算列表、
    正好劈开缓存前缀。凭据（Agnes/MiMo key）不一样，它只在部署时变，所以仍按 `enabled` 决定进不进列表。

## 日程（schedule：一次性 + 周期）

- **真相在 `reminders` 表，进程里只有一个巡检循环**：`main.py` 起 `Reminders.run()`，每 `scan_interval_sec`（默认 15s）扫一次到期行；启动时另跑一次历史清理。
  → 排好的日程必须活过重启，所以不能放内存堆；工具实例与巡检实例各持一个 `Reminders`，状态全在表里，两边看到的是同一张表。
  改动时注意：不要加"下次触发时间"堆、定时器句柄缓存之类第二套状态，扫表 + `status='pending'` 就是唯一真相。
- **到点发的是当初写好的原文，不再问一次模型**：省一次请求，也保证发出去的就是那句话。
  → 到点重新组织语言等于把"提醒你交材料"变成模型自由发挥，还会在深夜多烧一次调用。
- **发送复用回合外发言 `PresenceManager.speak`，不新建第二条发送链路**：`speak` → `TurnDecider.speak` 走的是和回合内一样的通道——先找还能用的被动锚点，
  找不到才主动发送；主动被本地频控拦住就返回失败、这一条留在 `pending` 下一轮重试，重试到超过宽限自然转 `expired`。
  → 日程的送达约束（@ 转换、引用、入库成 assistant 行、失败降级）和模型说话完全一样，另写一个发送函数就会有两套口径要维护。
  改动时注意：`speak` 会 `st.renew()`（替它说了话接下来得在场），但会话正离线时**只把上线倒计时压到 30 秒内、不抢读积压**——
  抢读会把还没回的消息一起置成「已读 + 不需要回复」，那些话就永远没人答了。
- **周期项的下一场从「原本那一格」往后推，不从"现在"推**（`_rule.advance(due_ts, rule)`），发完一场只 `fired_count +1` 并把到期推到下一格，状态仍是 `pending`，
  排满 `times` 场（或算不出下一格）才置 `fired`；`times=0` 是不限场数，靠取消退场。
  → 从"现在"推的话，8 点的打卡迟到两分钟发出就永久变成 8:02，一路漂下去；`monthly` 在解析时把几号钉进规则串（`monthly:31`），遇到没 31 号的月份 clamp 到当月最后一天、
    下个月又回到 31 号——不钉号就会一路漂到月末再也回不去。规则串本身自解释（`''`/`daily`/`weekdays`/`weekly`/`monthly:D`），面板和日志都用 `rule_label` 说回人话。
- **错过宽限（`miss_grace_sec`，默认 300s）分两种处置**：一次性项直接 `expired` 不发（`提醒过期未发（错过 …s，宽限 …s）#id: …`），
  周期项是 `_catch_up` 跳过没赶上的那几场继续挂着，**不消耗已发场数**（`提醒跳过 N 场未发…`）。
  → 进程关了一小时才冒出来的「五分钟后记得开会」只会让人莫名其妙，宁可作废；但停一周不该把一整年的打卡清零，也不该在重启瞬间把欠下的场次一次灌完。
- **清理只碰终态行**（`fired`/`cancelled`/`expired`）：`delete_terminal_before`、`delete_oldest_terminal`、`purge_reminders` 的 SQL 里都写死 `status<>'pending'`，
  `purge_reminders` 还拒绝"没有任何筛选条件"的删除（面板/CLI 同理）；`max_rows` 比的是**整表行数**不是待触发的条数。
  → 还在排的日程被一条 `DELETE` 悄悄删掉等于吞掉一次约定，比事故更糟的是没人知道发生过；上限若只看 pending，巡检一删历史就会让"是否超限"跟着抖动，删多删少说不清。
- **`when` 与 `repeat` 的解析全项目只有 `tool/schedule/_rule.py` 一份**：`when` 只认固定写法（`+30m` / `+2h` / `+1d` / `18:30` / `10-30 18:30` / `2026-10-30 18:30`），
  中文自然语言由模型先调 `time` 换算；`repeat` 只认 `daily`/`weekdays`/`weekly`/`monthly`（含中文说法）。报错文案都带上合法写法（`WHEN_HELP` / `REPEAT_HELP`），模型看一次就能改对。
  → 自己养一套中文时间解析（"下周三下午"、"傍晚"）是无限期的边界修补，而模型本来就会查日期；面板要是再写一套 `when`，两边对"明天 8 点"的理解迟早分叉——所以面板改时间送的是日期选择器算出的**绝对 unix 秒**。
- **面板「日程」只改不建**：分组视图 + 统计 + 筛选 + 逐行改时间/正文、取消、删除、批量清理历史与按保留期清理；**没有新建入口**，也没有第 4 个对话工具。
  → 改日程在对话里靠「取消 + 重排」两步说清楚，再开一个 `remind_update` 只得多养一套校验和一套措辞；面板能改字段但不能替用户理解 `repeat`，那就别让它在网页上排周期。
  改动时注意：「改」只把用户**真动过的那一格**发给后端（逾期项预填的就是已经过去的时间，改个错别字不该被「必须是将来」挡回来）。
- **三件套共享 `tool/schedule/config.json`**：`remind_list`/`remind_cancel` 标 `config_owner=false, toggleable=false`（表情三件套同口径），
  门槛 `remind_add` = **L2**——约定一句话不花钱、不越权，跟 `send_image` 同档就够；取消只能撤自己设的或提醒自己的那条（L5 例外），取消周期项等于整条停下。
- **提醒第三人**：`who` 走 `storage.find_sender_openid`（本会话发言者 → 别处的同名发言者 → `actors`），查不到就如实报错不猜人；
  群里提醒第三人时 `_compose` 自动在正文前补 `@昵称`，由正文通道换成真 @，私聊不补。


## 联网搜索

- 默认 Bing RSS（无 key），可切 SearXNG / Tavily。**RSS 的 `pubDate` 是抓取时间不是发布时间，所以 Bing 结果不返回 `published`**
  （返回过会让模型把旧页面认成"今天的新消息"）。
- **没收录的词 Bing 会退回单字匹配**（搜「瑟克斯遗纪」出"瑟"的百科）。工具对这种结果加 `note` 提示模型换词或直说不确定，
  并且不再为它抓正文。查询词写 2–6 个关键词、专有名词配类型词，比整句好。
- 抓正文受 SSRF 约束：只允许公网 http(s)，拒回环/内网/保留地址与 `file://`，限跳转/体积/超时/字符数。

## 发送与平台

- **一律先试可回，回满才主动**：被动回复不占主动额度，所以每条消息都先找一个"还能用的锚点"——本轮批次里有可回的消息用它，批次里一条都没有就回看库里窗口内最近一条（`storage.latest_replyable_message`，按 `messages.ts` 字符串比较＝时间序），同一个锚点的 `msg_seq` 由 `reply_anchors.next_seq` 接着上次排，回满次数或过窗口才降级主动发送。
  → 之前按事件来源一刀切：只有 @事件/私聊事件被动回复，群全量消息一律主动，白白烧主动额度。官方文档写明 `GROUP_MESSAGE_CREATE` 的 `id` 可用于被动回复，
  实测被平台拒（40034027）的是**跟着一起传的 `event_id`**，所以这条路径现在 `passive_ok=1` 而 `event_id` 为空（`use_event_id=False`）。
  改动时注意：窗口与次数是 `ggbot/core/outbox.py` 的 `PASSIVE_WINDOWS`（群 300s/5 次、私聊 3600s/4 次，2026-10 文档口径），别和平台真实配额脱节；
  这本序号账现在**在库里**（理由见上面「出站两本账落库」），别退回进程内的 dict；也不要把它当"已回复"计数往外暴露。
- **@ 只在 markdown 消息生效**：wire 走 markdown，被平台拒就退回纯文本原文；库里始终存 `@昵称`。
- **发送目标反查**：`sessions.key` 里才是完整 openid，内部主键 `session_id` 不等于它。
- **在线状态机是拟人的**：上线时长/离线间隔带抖动，每轮续期固定秒数（不随轮数递增，有封顶）；
  上线积压只读不回（`waiting → none`、置已读），自然上线时把最后一条积压投进窗口。
- **入站去重两级：内存 `_seen` + 查库兜底**（`_route` 里 `_seen` 未命中再 `storage.message_exists(msg_id)`）。
  → 内存那层拦得住@事件与群全量事件的重复推送，但扛不过重启——每次发版都会重启一次进程，`_seen` 清空，
  平台在重启窗口重推同一条就会回第二遍。查库这层补的正是这个缝；`messages.msg_id` 单独建索引，
  这段索引随 SCHEMA 每次启动 `executescript` 自动补。命中重复只打 DEBUG，不刷「收到」行。
  → **但 @ 事件晚到不是重复推送，是一条消息的第二种身份**：全量事件先到时认不出是@本机器人，那行按 `is_at=0` 入库；
  旧口径首写者赢，随后的 `GROUP_AT_MESSAGE_CREATE` 被当重复直接丢掉，那条@于是永远躺在积压里——离线时 @ 唤不醒，
  群里表现为「@了没反应」。现在晚到的 @ 只把标记补回那一行（`storage.mark_message_at`：`is_at/passive_ok` 置 1，
  `event_id` 为空才填、已有的不覆盖，`WHERE role='user'` 绝不动助手行），再拿这条去唤醒并回一轮；
  库里仍只有一行，会话已在线时它本来就在抖动窗口里，`already_stored` 直接放过、不回第二遍。
  改动时注意：**别把晚到的 @ 当新消息再 `add_message`**，那会让同一句话在历史里出现两遍、还多消耗一次回复；
  措辞是 DEBUG 的 `@ 事件晚到，补 @ 标记 id=…`，测试在 `temp/test_late_at.py`。
- **`bot_openid` 只从 `GROUP_AT_MESSAGE_CREATE` 学**（`_learn_bot_openid`，还要 `robot` 开头才算），全量消息只做精确比对。
  → 以前 `bot_openid` 为空时把第一条 `<@>` 的 id 当成自己：一条 @ 别人的群消息带的也是 openid，猜错一次就把别人的 id 写进 config，
  之后真的 @ 本机器人反而永远比对不上，比没学还糟。没订阅 @ 事件又没手工填时，只是认不出 @，普通消息照收。
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
- **v3 起表结构只有 `SCHEMA` 一份，开库不迁移旧库**：`Storage.__init__` 就一行 `executescript(SCHEMA)`——建缺失的表/列/索引，
  仅此而已。以前那段「PRAGMA table_info 找缺列 → ALTER 补 → 把 `status` 折成两列重建表 → `UPDATE` 换 `fact`/`episode` 词」整体删掉。
  → 为什么删：这次升 v3 明确「不兼容线上数据」，服务器那份 v2 库本来就要清空重来；留着迁移分支等于为一份注定要删的数据
    维护两套口径，每次改表还得回答「老库这列叫什么」。迁移逻辑只在换版那一次有机会生效，之后再没人走它。
  改动时注意：改表就改 `SCHEMA`（`CREATE INDEX IF NOT EXISTS` 这类幂等语句可以随便加），别在开库路径上补 `ALTER`；
  旧数据要保就写一次性脚本手动搬，不要长驻在启动路径里。`main.py --reset-db` 才是"换版重来"的正路（先备份再清空）。
- **`LimitNOFILE=65535` 是兜底，不是修复**：unit 归仓库管，`deploy_server.sh` 每次部署把**按本机项目目录渲染后的** unit
  与 `/etc/systemd/system/ggbot.service` 比对，不一致才覆盖 + `daemon-reload`，所以改完 unit push 即生效，不必重跑 `install_service.sh`。

## 安全与部署边界

- `db/config.json` 与 `tool/**/config.json` 有密钥（AppSecret、LLM/Embedding/MiMo/SMTP/搜索 key、`web.token`），
  `.gitignore` 已忽略；日志、面板、提示词都不出现明文密钥（`GET /api/config` 脱敏）。
- 发送本地媒体只允许 `temp/` 与 `tool/media/emoji/`；邮件收件人白名单、附件只走 temp 运行时文件。
- 面板：Cookie 登录（HttpOnly+SameSite）、每 IP 限流、CSP 无内联脚本、WS 鉴权、`trust_proxy` 默认关（防伪造 XFF 绕过限流）。
  `web.host=0.0.0.0` 时面板对整个公网可见，上云要么配 `web.token`，要么只绑 127.0.0.1 走反代。
- **部署走 GitHub Actions：push master → rsync → 远端 `deploy/deploy_server.sh` 重启 + 健康检查**（细节见 README「部署到云服务器」）。
  同步排除 `config.json`/`*.db`/`temp/`/素材目录，服务器上的密钥和对话库不会被覆盖。
  → 同一条排除规则有个反直觉的后果：**旧工具目录只要还剩一个 `config.json` 就删不掉**，
  `rsync --delete` 不会碰被排除的文件，代码早就没有的目录壳会一年年留在服务器上（本次就查到 `tool/chat`/`tool/knowledge`/`tool/send_text`）。
  改名/换分类目录后要顺手 SSH 核对 `ls tool/` 与代码是否一致，多出来的目录先确认配置与新路径逐键相同、再移进 `temp/_cleanup_<日期>/` 隔离（别直接 `rm`，temp 有 TTL 会自己清）。
  服务器进程统一交给 systemd（`deploy/install_service.sh`），装完要删掉 crontab 的 `@reboot start.sh` 行，否则抢单实例锁。
- **换大版本要"从零起"时，清数据这一步永远是手工的**：排除清单保住的正是库、配置与素材，所以 push 不会替你清空。
  → v3 的数据模型（`messages.read_state/reply_state`、`reply_anchors`、`sends`）只由 `SCHEMA` 新建，代码里没有任何 v2 迁移，
  旧库顶上新代码只会缺列报错，所以线上 v2 的库必须删掉重起。
  顺序：`systemctl stop` 再动库（SQLite 边写边打包得到一份坏快照）→ 整目录 tar 到**项目目录之外**（`--delete` 与 temp 的 TTL 都碰不到它，
  `chmod 600`：里面有密钥）→ 删 `db/*.db*`、`db/config.json`、`temp/`、素材目录与被删工具集的 `config.json` →
  只把初始化配置推回去（`db/config.json` + 各 `tool/*/config.json`；表情素材、记忆、日志这些运行产物不推）→
  `systemctl enable` 恢复开机自启 → push 走 CI。核对起版看 `GGBot v3.0 启动`、`工具就绪` 的分类计数、空表的新库。
- **部署脚本按"一件事只有一份"重排过**：unit 渲染、面板探活口径、"哪个进程才是本项目的机器人"三件事以前在
  `install_service.sh` 与 `deploy_server.sh` 里各写一遍，改一处就漏一处，现在集中在 `deploy/service.sh`（被两边 source，不单独执行）。
  → `ggbot.service` 从此是**模板**：里面写死的 `/root/GGBot` 装机/部署时换成脚本自己 `cd` 出来的项目根。
    以前是拿仓库原文直接覆盖 `/etc` 那份，项目不在 `/root/GGBot` 的机器一次部署就把服务指到不存在的路径上，
    还要人手改 unit 两行、并和 `DEPLOY_SSH_PATH` 对齐——路径有两个口径。现在比对的是渲染结果，
    项目在 `/root/GGBot` 的机器上渲染结果与模板逐字节一致，所以现网部署行为没变（`cmp` 仍判"已是最新"）。
- **rsync 排除清单也只有**一份**：`deploy/rsync-exclude.txt`，CI 从内联 heredoc 改成 `--exclude-from` 引用它**（手工同步同一份）。
  → 清单是"保命"用的：CI 的 checkout 里没有这些文件（`.gitignore` 挡的），而 `--delete` 删的正是"目标有、源没有"的东西。
    这次补上 `*.db.bak-*`——`--reset-db` 生成的库备份就躺在 `db/` 下，之前每次部署都会被 `--delete` 清掉。
    rsync 会忽略 exclude 文件里的空行和 `#` / `;` 开头的行（清单里写注释是安全的），但我们这份保持纯模式、
    条目的理由写在 README 与 workflow 注释里；`.gitattributes` 单独给它定了 `eol=lf`——这份是给服务器上的 rsync 读的，
    不该跟着本机 `core.autocrlf` 变。
- **部署成功的判定看配置，不看固定端口**：`web.enable=false` 是用户故意关面板，以前一律探端口，
  这种机器的每次部署都被判成失败（CI 红叉但服务其实好好的）。现在面板开着才 `curl` 首页，关着就只看服务进程活着。
  失败时的现场也跟着改：systemd 下控制台输出在 journal 里，`temp/logs/stdout.log` 是 nohup 时代留下的旧文件，
  再 tail 它等于给用户看一份过期的"现场"（只有退回 `start_linux.sh` 的分支才继续 tail 它）。
- **杀进程只杀本项目的**：以前 `pgrep -f 'main\.py'` 会把同机任何同名进程一起 kill（`install_service.sh` 真这么干过），
  现在按 `/proc/<pid>/cwd` 是不是本项目根来认，且先 `systemctl stop` 再兜底 kill。
- **依赖探测与 `requirements.txt` 由测试对齐**（`temp/test_deploy_scripts.py`）：`aiohttp` 被 `ggbot/qq/bot.py` 直接 import
  却没写进 requirements，一直靠 `qq-botpy` 的传递依赖带着——botpy 哪天换 HTTP 客户端，服务器 pip 缓存一失效就起不来。
  现在它进 requirements，三个启动脚本的探测也一起补上；`pydantic` 是唯一例外（由 fastapi 带装，不进探测）。
  `start_linux.sh` 同时补上了版本库里的可执行位——README 一直写着 `./start_linux.sh`，但文件是 644，新克隆直接 Permission denied。

## 面板（web）

- **日志筛选：等级是门槛，不是等值匹配；先筛后截断。** `tail_log` 按 `LEVEL_ORDER` 取「该级及以上」，
  无等级列的堆栈续行继承上一条的等级（`Traceback`/`  File ...` 得跟自己的 ERROR 待在一起），
  会话/关键词筛选扫全文、只在最后一步截到 N 行，且忽略大小写。
  → 用户报「筛选失效」的四个原因全在这一条里：以前是先读尾部 400 行再筛（老日志根本进不了视野）、
  等级按字面相等（选 INFO 就看不到 ERROR）、解析不出格式的行直接绕过筛选（堆栈被单列为 INFO 之外）、
  会话 id 大小写敏感。改动时注意：`/api/logs` 的 `total` 是命中总数、`logs` 是截断后的显示行，
  面板副标题靠这两个数说「命中 N 条，显示最近 M 条」，别只回一个数组让用户自己猜被截了多少。
- **版本号只有一处：`ggbot.__version__`。** 页面标题、登录卡、侧栏徽标写的是 `{{VERSION}}`，`GET /` 返回前一次性替换；
  启动日志第一行也打它。→ 前端是可独立热改的静态文件，硬编码第二份版本号一定会过期，
  而面板显示的版本对不上部署版本，等于排查时多一个假线索。改动时注意：发版只改 `ggbot/__init__.py`。
- **前端按页拆在 `static/js/`（19 个）与 `static/css/`（6 个），没有打包步骤，靠 `index.html` 的引用顺序拼回来。**
  脚本仍是经典脚本（不是 module），全局 `const/function` 跨文件可见，所以顺序 = 原来单个 `app.js` 的行序，
  `events.js` 必须最后（它挂 DOMContentLoaded 并 `boot()`）；样式表 `responsive.css` 必须最后（层叠覆盖）。
  → 用户要求「不要全堆在三个文件」，而拆分的唯一风险就是漏引/错序——`temp/test_web_schedule.py` 里那半截
  就是守这个的：引用的文件必须存在、`js/` 与 `css/` 里不许有没被引用的文件、首尾顺序钉死（`schedule.js` 排在 `refresh.js` 之前，页面自己也要在 nav 里登记）。
  改动时注意：**新增一页要同时动五处**（`index.html` 的 section + nav、`util.js` 的 `PAGE_META`、
  `nav.js` 的 `switchPage` 分支、`refresh.js` 的 `refreshAll`、`poll.js`/`events.js` 的入口），少一处就是点了没反应。
  例外是"编辑器页"：「人设」刻意不进 `refreshAll`/`poll.js`，只在 `switchPage` 进那一次拉数据，
  否则 1s 轮询会把用户正在敲的字冲掉——`temp/test_web_persona.py` 把"poll/refresh 里不许出现 persona"钉成了断言。
- **面板「日程」页能看、能改、能删，但刻意没有新建入口。** 分组视图（已经过点 / 今天要发 / 这周之内 / 以后 / 历史）+ 全局统计 + 三个筛选 + 逐行改时间正文/取消/删 + 批量清理历史与按保留期清理；排日程仍然只有对话里那三个工具。
  → 时间语法只在 `_rule.py` 解析一次：面板上真要做"明天 8 点、每个工作日"就得多养一套输入与校验，两边对同一句话的理解迟早分叉。用户要的"看看挂着什么、点掉某条、改个时间"不需要理解规则。
  改动时注意三件事：① 列表是卡片行不是表格，手机端只需放开 `.sch-body` 的 `max-height`；② `stats` 走**全表口径**、`total` 跟着筛选条件走，别拿其中一个当另一个用；
  ③ 这一页在 1s 轮询里会被整片重画，所以渲染要保住 `#sch-body` 的滚动位置，写完请求再用 `guarded("schedule", refreshSchedule)` 顶一下——
  `guarded` 会丢掉并发调用，因此"点完没立刻变"是既有 house 行为，下一轮轮询自然追上，不要为它加乐观改本地状态。
- **会话详情多了一块「最近出站」，读的是 `sends` 表不是日志。** `GET /api/sessions/{sid}/messages` 一次回
  `{session, messages, sends}`（出站只给最近 10 条），前端把 `via` 翻成「被动回复 / 主动发送 / 降级主动」、把 `ok` 翻成
  「已送达 / 被拒：<原因>」，消息气泡上的「待处理 / 回复失败 / 没接话」来自 `reply_state`。
  → 以前判断"这条到底发出去了没"要开日志页搜关键词：日志会轮转、发版即丢、还混着别的会话，而这是运维最想看的一列。
  改动时注意：`cli context` 末尾那 5 行读的是同一张表，别在面板里再拼一套口径；「清空上下文」
  （`DELETE /api/sessions/{sid}/context`）与 CLI `clear` 都走 `storage.clear_session_context`，连该会话的锚点与出站记录一起删。
- **「消息流」页已经换成「表情包」浏览页，但 `/api/messages` 留着。** 消息流和会话详情读的是同一张 `messages` 表，
  跨会话那份没人翻（要看历史就进「会话与上下文」按会话看），仪表盘的「最近消息」还在调这个接口
  → 删的是页面不是接口，`refreshDashRecent()` 因此从 `js/messages.js` 搬进了 `js/dash.js`；
  仪表盘那张「查看全部」按钮也改成跳「会话与上下文」，别留一个指向不存在页面的 `data-page`。
- **表情包页能浏览也能管素材，图片路由刻意不在 `/api` 下。** 列表复用 `emoji_list` 那份 `EmojiLibrary.rows()`（目录仍是唯一真相），
  缩略图走 `GET /emoji/<rel>`，越界判定放在 `EmojiLibrary.image_path()` 里（限定素材根目录 + 只认图片扩展名），
  鉴权复用 `_auth`——Cookie 也能过，所以 `<img src>` 不需要带令牌。
  → 不放 `/api` 是因为中间件按 IP 限 240 次/10s，一屏几十张图会被自己的限流打回。
- **网页上的增删改只复用工具层那三个函数**（改描述 / 删除 / 上传），面板不另写一套落盘逻辑。
  → 以前定过"网页只浏览"，理由是写入只有"往目录放文件"和 `emoji_save` 两条路；实际管素材时改一句描述要跑终端，
    那条老规矩把便捷性换成了"少一个真相源"的执念。现在真相源仍然是目录，网页只是 `emoji_save` 之外的第二个入口，
    走的是同一个入库函数（摘要命名 + 去重 + 上限）。
  改动时注意：这页不在 1 秒轮询里（纯手动刷新），删/传之后要把扫目录缓存打掉，否则列表还留着刚删掉的那张；
  前端对 `/static/*` 用 `no-cache`（带 ETag 回源，没改照样 304），因为这套前端是"改了刷新一下就生效"的设计，
  抱着旧 JS 跑新页面比多一次 304 难查得多。

## 验证约定

- 离线回归：`sh temp/run_all.sh`（`PYTHONPATH=.` + venv python），每个 `temp/test_*.py` 自己打 `[PASS]/[FAIL]`，
  以退出码为准；需要联网/真实 LLM 的测试单独跑，别塞进套件。
- 部署脚本也在套件里（`temp/test_deploy_scripts.py`）：用 `bash -n` 验语法、用临时目录 + `UNIT_DEST` 验渲染与比对、
  用 `systemctl` 桩函数挡住真实系统调用——`systemctl`/`journalctl`/`crontab` 的真行为测不到，只能真机跑一次。
- UI 改动用 `temp/serve_preview.py` + 浏览器实测（快照/DOM 尺寸/控制台报错），不要凭代码断言"应该能渲染"。
- 消息流的回归只碰各模块的读取面（`pm.inbound.*` / `pm.context.*` / `pm.archiver.*` / `storage.*`），假对象只假在两处：
  网关（`FakeBot` 记账）和模型循环——后者靠 `presence.decision` 模块级的 `agent_loop` 名字换掉，跑的是真的入站 → 决策 → 出站；
  `TurnDecider._agent_loop` 就是为这个接缝留的（拼上下文 + 归档触发都在这儿，不经模型也能单测）。
  → 这次拆分的收益就在这儿：内部实现改名不该让测试报废，而测试一旦开始伸进别的私有步骤，说明那段职责又混回去了。
- 每次改动同步 README；**文档不写代码里没有的行为**。
- **用例清单**：README 只留「跑 `sh temp/run_all.sh`」那一句，这里记每一条锁住了什么行为。标签是当时从输出抓下来的，和今天的 43 个脚本不是一对一——有的已并进别的脚本（`messages_state` 就在 `temp/test_sender_quote.py` 第 5 节），有的是一次性的真实调用或浏览器实测（`llm_chat`、`voice_clone`、`email_tool`、`web_ui*`，脚本早删了，结论还值得留着）。改行为时按说明去找脚本，别按标签猜文件名。

```
[PASS] import_all        # 全部模块导入
[PASS] storage_crud      # SQLite 会话/消息/记忆 CRUD + 待处理查询（`reply_state=waiting`）
[PASS] messages_state    # 消息两条时间线（`temp/test_sender_quote.py` 第 5 节）：`read_state`/`reply_state` 两列各自独立，新写入的用户消息默认「未读 + 待处理」、助手消息默认「已读 + 不需要回复」；只读不回只动还挂着 `waiting` 的行——已回过的「读一遍」抹不掉结果，未回过的才转成「已读 + 不需要回复」；`sender_name` 与状态列同一条读写
[PASS] fmt_tagged        # 给模型看的发送者标识（`temp/test_sender_quote.py` 第 6/7 节）：历史与本轮每行 `[小明#AAAAAAAA] 你好`、无昵称只剩 8 位 `[CCCCCCCC] 无昵称`（不写成 `id#id`）、撞昵称的另一个人标签不同（`fmt_same_name_diff_id`）、归档那一路裸昵称不带 #（`archive_plain_no_tag`）；入库展示名 actors 表优先（`actors_name_wins` 登记过「阿星」就不再用事件里的「小星」）、没登记过才沿用 QQ 昵称（`unregistered_keeps_qq_name`）
[PASS] storage_fd        # 连接随操作关闭：450 轮混合读写后零长驻连接、fd（Windows 取进程句柄数）不随调用数增长；with 块里抛异常时未提交的内容丢弃（原 rollback 语义保留）；close() 把 WAL 归并回主库
[PASS] dedup             # 入站去重：msg_id 索引在位（随 SCHEMA 每次开库补）、重启后平台重推不再打「收到」行/不再入库/只留 DEBUG（库中命中带「库中已有」，内存命中不带），新消息与同进程重推口径不变，没接 storage 时退回纯内存去重不报错
[PASS] late_at           # @ 事件晚到不再被当重复丢掉：`mark_message_at` 只挪标记（一行不增、助手行不动、本来就是 @ 返回 False、已有 event_id 不被覆盖）；离线路径补标记后唤醒并只回一轮（被动锚点用的就是补上的 `event_id`）；在线路径那条不重复进窗口、批次里只出现一次、「收到」行只有全量那一条 @=0，@ 事件只留一行 `@ 事件晚到，补 @ 标记 id=…`
[PASS] memory_storage    # 记忆存储层口径（v3 起不含旧库迁移）：`subject_key` 折叠空白、掐长度、空串归 None；按会话/人/类别筛与计数同一套条件；空白 subject 落库就是 None；`delete_memories_by` 三条件全空时一条不删；`update_memory(expires_at="")` 是取消到期不是写空；`search(touch=True)` 才加命中且可累加，面板式检索（不传 touch）计数不动
[PASS] memory_sweep      # 遗忘扫描四步与顺序：合并阈值仍随条数放宽（0/49→0.95、50→0.94、120→0.93、450 与 1000 条封底 0.86），CLI/面板显式 `--threshold` 覆盖自动值；`retention` = 0.4 重要度 + 0.4 新鲜度（半衰期 forget_after_days）+ 0.2 命中（10 次封顶）；到期行删；event 安静过 90 天淡出，但 ★4 以上不淡、被命中过的按 3 倍（270 天）放宽、profile 永不按年龄淡；每人档案超 max_per_subject、整库超 max_memories 时按保留分从低往高腾；近似重复只留保留分高的一条（同分留新的）；`auto=True`（写入攒够顺手跑）跳过合并；库里没有向量时不做合并也不报错；日志行锁「记忆遗忘 到期 1 · 淡出 0 · 超限 0 · 合并 0（共 0 条）」
[PASS] memory_commit     # 写入前的取代判定：没有 LLM / `reconcile=false` / 判定抛异常三种情况都退回直写、照样落库；一批多条只发一次判定请求；`update` 改写旧条目不新增、`drop` 一个字不写、`retire` 作废被推翻的旧条目；模型给乱编号（target 越界、retire 非数字）按「新增」处理不崩；同归属向量 ≥0.95 的重复在没走判定时并成一条；`expires_in_days` 落成 `expires_at` 且到期条目检索取不到；累计写入够 `sweep_every_writes` 顺手跑一次遗忘扫描
[PASS] web_memories      # 记忆页接口：`GET /api/memories` 按 session/subject/kind 组合筛、回执带 `limits`（整库上限 / 每人档案 / 淡出天数）与每条的保留分、安静天数；`GET /api/memories/search` 看一眼不写命中；`POST /api/memories` 走 commit（假 LLM 判 update/retire 生效，回执 written/updated/dropped/retired）、空内容 400、只填日期 `2026-11-20` 补成当天 23:59:59；`PUT` 改 kind 非法 400、未知 id 404、空 body 400、`expires_at` 传空串取消到期；`POST /api/memories/sweep` 回四类计数并把该删的删干净；`DELETE /api/memories` 无条件 400、给条件时整组删到零；全部端点未登录 401
[PASS] llm_chat          # LLM 真实调用（实测 DeepSeek deepseek-flash，thinking=disabled 生效）
[PASS] tools_schema      # 20 个 Tool 插件自动发现 + schema 校验 + 启用状态过滤
[PASS] tool_layout       # 根目录 tool/ 按分类包自动发现（core/schedule/media/memory/web/mail/admin）+ 嵌套数据目录（media/emoji、media/audio、media/generate、schedule、web、mail）+ config-less 工具 toggle 生效 + 正文通道不在 registry 里 + remind 三件套共享 schedule 配置（附属工具不可单独启停）+ fs 分类已下线不残留目录
[PASS] gen_tools         # Agnes 生图：无 api_key 时不进下发列表（L4 心腹，填上 key 才下发，不按发送者等级裁剪）、参数与比例校验、参考图只允许 temp/ 与 emoji/ 素材（配置/密钥/越界路径拒绝）、纯文生图不带 image 字段与单张参考图写成字符串两种载荷、只给 URL 的形态补一次下载、503 退避重试、产物落 temp/tool/media/generate 并按 file_type=1 发出、`生图 2K/4:3 …（…s）` 日志形状
[PASS] permissions       # 发送者等级解析（未登记→默认 2、空 id→0）、owners 引导不覆盖面板设置、全量下发（每个等级拿到同一份列表、6 级永久锁不进列表、没有工具提示词注入）、门槛覆盖、执行期拒绝/放行/未知工具/永久锁；正文通道不是工具（不在 registry 里）
[PASS] media_paths       # 本地媒体只允许 temp/ 与 tool/media/emoji/，日志、其他工具目录与配置/密钥拒绝发送
[PASS] web_permissions   # /api/actors 与 /api/tools/{name}/level 鉴权、成员 6/-1/空值校验、工具门槛写回 config、6 级后不再下发、未知工具 404
[PASS] assistant_persist # 发出去的话进得了历史：`Outbound` 每发一条落一行 assistant 消息（已读 + 不需要回复）加一行 `sends` 回执（被动/主动各记自己的 `via`）；回合结束把这批用户消息的 `reply_state` 记成已回，之后它只作为历史出现、不再算待办；本轮消息独立标注（防逐条重答旧消息）
[PASS] tool_plugin       # 插件配置隔离（send_email→tool/mail）、共享配置同组不可单独启停、/api/tools 密钥脱敏
[PASS] media_signed      # 签名媒体 URL：有效链接可读、篡改/过期/目录穿越/config.json 均拒绝
[PASS] emoji_library     # 表情库（v4，一个统一目录）：向量挑图（含 min_score 拒发并把邻近三张的说法回给模型 / 同档内先躲刚发过的再挑用得最少的 / temp 投喂识图入库、换扩展名也按摘要去重）→ 未配 embedding 降级二元组关键词匹配 → v2 moods 自动迁移、v3 的 source 字段读时丢弃、手改 desc 后旧向量作废只重算那一条；浏览用的 rows() 不写盘不嵌向量、只有聊天动作才补索引；外加空库提示、max_per_turn 与 min_interval 限流、max_stickers 整库上限、usage.json 落盘后重启仍认、重启后复用 vectors 缓存不重复计费
[PASS] voice_clone       # MiMo 真实合成（mimo-v2.5-tts-voiceclone）产出 wav
[PASS] email_tool        # SMTP 真实自测发送 + 收件人白名单 + 附件仅限 temp/并拦截敏感文件
[PASS] text_split        # 正文通道把带换行的内容拆成多条依次发送（msg_seq 递增、每条单独入库）
[PASS] media_upload      # 本地媒体 base64 file_data 直传（群/私聊路由、file_name、msg_type=7）+ 公网 URL 上传
[PASS] voice_silk        # MiMo wav 24kHz/单声道 → pysilk 编码出 #!SILK_V3 头（207KB→9.5KB）
[PASS] qq_face_clean     # QQ 表情富文本还原为 [表情:文字] + @还原为 @昵称/8 位 id、自己的 @ 剔除
[PASS] tool_loop         # function calling 端到端：AI 调用 memory_save 真实入库
[PASS] tool_choice       # 真实 LLM 触发率（新提示词 + 全量下发，只观察不执行）：自我介绍→memory_save、回忆→memory_fetch 照旧命中；去掉工具提示词注入后，「哈哈哈哈哈」不再顺手调 send_emoji、「好难受」不再调 send_voice_clone——本轮只发正文，靠 description 里的触发场景兜
[PASS] agent_fallback    # 回合只有一条说话通道：只有正文→运行时走正文通道发出；正文+工具同轮→两条都发且不重复（工具在前、正文在后）；模型自己调 send_text（注册表里根本没有）→ 按「未知工具 send_text」回一条、一次都不发；空正文/纯占位符→静默结束（占位符同回合纠正一次）；silence 调用后立即收轮，全程只有一次 LLM 请求；正文整条是沉默标志（`[SILENCE]`/`【沉默】`/`(silence)`/带标点/带短标识/连写两个）→ 同等按 silence 收轮，一次都不发、也不再追问；工具+沉默标志同轮→只发工具产物；真话里夹着沉默字样→那句人话照发
[PASS] config_read       # 纯配置文件驱动（无环境变量）+ temp/db 路径
[PASS] config_generic    # deepseek→llm 自动迁移、缺失配置按模板生成、embedding 可选（关键词降级）、LLM 厂商无关且缺项明确报错、39 个 Web 设置路径均存在且在白名单（白名单不含 api_key/base_url/token 类凭据路径）
[PASS] config_hotapply   # 面板改配置免重启：同一个 PresenceManager 实例现读作息/回复/积压/上下文（作息与积压上限读在 `pm.inbound`、上下文读在 `pm.context`、归档读在 `pm.archiver`；archive_batch 现读夹 256、archive_overflow 现读先夹 4096 再夹到「窗口减一批」，无记忆或可消化量为 0 时归档恒关；落盘存的是面板填的原值，钳位只在读取时生效）与识图参数（含启动关着、面板打开后补建连接），set_by_path+save 落盘后重开一致；POST /api/config 回执 updated/rejected（凭据路径、类型不符、config.json 缺键各算一条拒绝），GET 带 restart 列表；旧键 summary_threshold_tokens 已从白名单去掉
[PASS] logging_system    # 多等级写文件 + tail 等级过滤 + set_level 热切换；根目录不产生 botpy.log
[PASS] placeholder_guard # 占位符不进正文：词表覆盖 表情/表情包/语音/语音克隆/语音条/图片/视频/文件/邮件 与 emoji/sticker/voice/audio/image/photo/video/file/mail，以及沉默类（沉默/不说话/不接话/无话可说/silence/silent）（方括号、中文括号、圆括号都拦），同回合纠正重试；正文通道拦截纯占位符、剔除混排占位符（`[SILENCE]` 单独一条发不出去，`行吧[SILENCE]` 只剩「行吧」）；入站富媒体标记的整套形状（`[图片]（描述）` / `[语音 6ce72460]`，括号里 6–8 位短标识 + 紧跟的（…））照抄进正文也一起吞掉——历史里存的就是这个形状，模型学的就是它
[PASS] vision_runtime    # 识图配置继承/覆盖/停用、data URL 请求与描述清洗、失败重试回退、[图片]（描述）入库、启动统计累加、正文通道 @昵称与 [引用 关键词]
[PASS] time_tool         # 当前日期/时间/星期/时区
[PASS] clear_context     # 只清本会话（消息删净 + 归档游标归零、记忆与别的会话不动）、工具照常下发但 L3 调用被拒、门槛下调后工具内部仍按 L4 兜底、上调到 5 后 L4 被执行器拦下
[PASS] admin_tool        # manage_permission：口语等级映射、昵称/8 位 id/openid 定位、重名候选、越级与同级保护、list/help、默认 L2 也拿得到这个 schema（L5 才调得动）、6 级锁
[PASS] presence_backlog  # 离线攒下的积压上线只读：`waiting → none`（置已读并结束回复需求）、零 LLM 调用/零发送；唤醒先读旧积压再回本条；读入上限 256、在线批次上限 16
[PASS] context_limits    # 请求拼装顺序=缓存顺序：首段是逐字节静态人设（无占位符、无工具提示段、无摘要段）→ 历史按条数截到窗口 → 相关记忆作为独立一条放尾部（无命中就整段不注入，命中的 top-3 各记一次命中）→ 本轮消息；默认 1024 条 / 128k 预算 / 满窗消化 512 / 每批 64；历史只取归档游标之后的最近那么多条；`base_len` = 人设 + 历史；会话摘要与 `storage.get_summary/save_summary` 已不存在；积压读入 256、在线批次 16
[PASS] llm_usage         # 请求行报 usage.prompt_tokens + 工具/历史/新增三段（缩放后三段之和=总数）、响应行报 completion_tokens + 缓存命中率/分时 pricing 费用（缺字段显示 -）；不再有「估算拆分」字样与第 N 次；服务商无 usage 时请求行整行标 ≈；会话列跟随调用方传入的标签
[PASS] log_format        # 日志整洁度：入站唯一一行 `收到 [小明/54019C24] @=1: …`（@ 用 1/0，不再有 被动= 字段，通道由发送侧决定）；模型调用的工具唯一形态 `工具 X（admin=昵称/8位id(L2), k=v）`（长值 120 字、整行 200 字截断）；内部通道不套工具格式，每条发出的消息一行 `发送: 正文`（INFO，不带 admin，换行拆条时一条一行，@ 与引用行存的是可读原文）；正文只出现一次，只有拦截/未成功/降级/平台拒收才另留痕（纯占位符 → `发送未成功:` 且没有发送行）；夹带占位符时发送行是剔除后的干净正文，标记只出现在剔除行；[语音克隆]/【表情包】/(image) 等变体占位词一律剔除、余文照发；入站标记的整套形状一起吞（`[图片]（描述）`、`[语音 6ce72460]`、`[表情:偷笑]`、`[图片 cat]`）；图片描述只在「图片已识别」出现一次、识图行只有 tokens/命中/费用；语音平台转写只在「语音已转写」出现一次（正文替换为 `[语音]（…）`，无转写时不打行）；@ 成员 wire 走 markdown、被拒退回纯文本原文，入库始终是 `@昵称`；一轮内无重复行（旧 agent 工具调用行已删）；工具/LLM/识图/作息每行都带调用方会话列；特权工具 INFO、空转上下线降 DEBUG；拒绝行 `需要 L4，当前 小明/54019C24(L2)`、编造工具名与未成功结果各留一条；日志文件跨重启**追加**（`setup_logging` 重开同一个轮转 handler，旧行仍在、新行接在后面，`handler.mode` 恒为 `a`）
[PASS] log_filter        # 面板日志筛选口径（`temp/test_log_filter.py`：3005 行合成日志 + TestClient 打 `/api/logs`）：等级是门槛（INFO 及以上含 WARNING/ERROR/CRITICAL，DEBUG 为全部）、无等级列的堆栈续行继承上一条等级；先按等级筛、后截断，会话/内容筛选扫全量（3000 行 DEBUG 噪声之前的老 WARNING 也搜得到）且忽略大小写；回执 `logs`（最近 N 行）+ `total`（命中总数），解析不出格式的行同样走筛选、不再绕过
[PASS] startup_online    # 重启后本会话首条消息直接上线（reason=startup、日志「启动上线」）、两条都进在线窗口不重复触发；start_online=false 时恢复离线积压语义（只入库等窗口/@ 唤醒）
[PASS] backlog_reply     # 自然上线带上积压（`temp/test_backlog_reply.py`）：到点上线时除最后一条全部置「已读 + 不需要回复」，最后一条（storage 行，字段够出站用）仍挂待处理并投进在线窗口；回复那段跑真链路（决策层 → 上下文组装 → Outbox/Outbound → 假网关），续期、`reply_state=answered`、助手消息入库、`sends` 回执一次验齐；非@ 且窗口里没有可回的消息就走主动发送；空积压不往窗口塞东西
[PASS] memory_archive    # 满窗后消化的是窗口最旧的一段、窗口当场减半：默认 1024 满窗一次 8 批 × 64 条写完后窗口回到 512/1024（1024 条库实测）、128 窗口配 16 批 64 消化量走完整锯齿（满 128→剩 64→攒满→再消化，缩窗后留下的正是原先最新的一半，逐字节不动）、消化量钳在「窗口-一批」以内（128/16→112，一批就装满窗口时视为不归档）、游标远落后于窗口时先从最旧的补进记忆追上才算；每批提炼出的是结构化事实（kind=profile/event、subject、importance、expires_in_days），逐批走 `memory.commit`（取代判定），日志一行 `历史归档 64 条消息 → 记忆 新增 a · 改写 b · 作废 c`；写入失败停在首批不推进游标、下一轮同一批重试补完；开关关闭时不消化也不缩窗；清空上下文连游标一起归零；后台任务不阻塞回复
[PASS] web_ui_memory     # 记忆页浏览器实测（本地预览服务 + 无头窗口截图核对，1440×960 与 390×844，控制台无 JS 报错、无横向溢出）：标题一行给出「共 N 条 · 整库上限 800 · 每人档案 12 · 事件 90 天不用就淡出」；档案按人分组（关于 小林 / 未归属的档案）后接事件流水，每组末尾「清掉这一组」二次确认后整组删到零并 toast 条数；写入一条带 subject/类别/到期日 → 当场落到对应分组；改类别为 event 后条目挪到事件段；到期日已过的那条挂「已过」徽标；检索态按分数平铺不分组（行首多一列相关度）；「立即遗忘扫描」返回「到期 a · 淡出 b · 超限 c · 合并 d（剩 N 条）」；手机端表头隐藏、类别/命中/到期列折进行内，操作按钮仍点得到
[PASS] web_tools         # Bing RSS/SearXNG/Tavily 结果解析、HTML 正文提取与编码识别、超时/体积/字符限制、SSRF 拒绝回环/内网/保留地址与 file://；published 归一函数各格式都认（含 Bing 中英文 RSS），但 Bing 结果不再带 published（pubDate 是抓取时间，会误导"最新"）；关键词字面重合度 <0.3 判为不沾边→返回 note 且不去抓正文（`……` 这类拆不出词的不判、空结果不误标），相干结果不带 note；web_search read=true 并发抓前 3 条正文（单页失败只标 content_error）；web_fetch urls 批量最多 5 个并回 N/M 成功
[PASS] system_prompt     # 默认提示词仅保留感觉与说话方式（无固定名字/经历），工具行动与主动记忆引导生效；整段全静态、不含任何运行时占位符，跨会话逐字节一致
[PASS] presence_proactive # 发送通道一律先试可回：本轮批次有可回消息用它、没有就回看窗口内最近一条，同一个锚点的 msg_seq 接着上次排；锚点这本账落在 `reply_anchors` 表里，**进程重启也从上次接着排**、不会又从 1 开始撞上平台拒绝的重复 msg_id+msg_seq（`temp/test_proactive.py` 的跨重启场景直接查表核对）；锚点回满 5 次（群）/过期 → 无锚点，这一轮改主动发送（不带 msg_id/event_id）并受 `proactive_enabled` 与每分钟/每日闸门约束；每条发送的实际路径（被动/主动/降级）都落在 `sends` 里；回合外发言 `speak` 同样先找锚点、过同一个主动闸门，主动被拦时返回失败不硬发
[PASS] schedule_rule     # 日程时间规则（`temp/test_schedule_rule.py`，全离线不碰库）：`parse_when` 只认固定写法（+30m/+2h/+1d/18:30/10-30 18:30/2026-10-30 18:30），非法与看不懂的都把合法写法回给用户，只说 `18:30` 而今天已过就滚明天、`MM-DD` 滚下一年；`parse_rule` 认中英两种说法并把 `monthly` 归一成 `monthly:<几号>`；`advance` 从**原本那一格**往后推（迟到两分钟发出的 8 点打卡，下一场仍是明天 8:00，钟点不漂）、`weekdays` 跳过周六日、`weekly` 固定同一天、`monthly:31` 在没 31 号的月份 clamp 到当月最后一天而下个月又回到 31 号（不锯齿式漂移）；`rule_label` 与 `fmt_due`/`span_text` 的中文口径
[PASS] schedule_remind   # 日程工具与巡检循环（`temp/test_schedule_remind.py`）：排与校验（空会话/空内容/超长/过去时间/超 `max_days`/超 `max_pending`、`repeat` 看不懂报错、`times` 只在填了 `repeat` 时生效且负数与超 `max_repeat_times` 被拒），周期字段落库（`rule`/`times`，`times=0`=一直排到被取消，回执 note 分别说「排 3 场」与「一直排到被取消」）、`who` 反查（查得到用别人、查不到报错、不填就是本人）、列表口径（待触发按到期时间从近到远、周期项带规则与「已发/总场数」，不限场数只显示已发几场，一次性不带场次）、取消（按编号或唯一关键词，多条命中给候选，别人的日程除 L5 外撤不掉，**取消周期项一次停整条**）、到期就发（群聊自动补 @昵称、私聊不带）、发完一场 `fired_count` +1 且状态仍 `pending`、排满 `times` 场才置 `fired`、超宽限的一次性置 `expired` 不发、周期项错过宽限只跳过没赶上的场次（`fired_count` 不变、继续挂着）、发送失败留 `pending` 下一轮重试、统计与筛选/分页口径
[PASS] schedule_sweep    # 日程历史退场（`temp/test_schedule_sweep.py`，真库离线）：各状态与逾期/今天/周期的统计；过了 `keep_history_days` 的终态行退场；整表超 `max_rows` 从最旧的历史行删起、不够删也绝不碰 `pending`；单条删除与批量清理只认 `fired`/`cancelled`/`expired`，条件全空（怕一句 `DELETE` 清了全表）拒绝执行；列表与计数共用同一份过滤条件（翻页数对不上就是这里）
[PASS] remind_send       # 日程端到端（`temp/test_remind_send.py`，真 Storage + 真正文通道 + 假 bot 记账）：触发第一条走被动（带锚点 msg_id、msg_seq=1）、同锚点第二条 seq 递增、锚点回满后转主动发送（msg_id 为空）、宽限外的 stale 一次性行只置 expired 一次都不发、提醒第三人时用窗口内最新锚点并在 wire 上带真 @；周期项到点同样只发一场然后把自己的到期推到下一格（从原定格子推，不从"现在"推）、排满最后一场记满场数再退场、错过宽限的周期项一场都不发只跳过
[PASS] renew_flat        # 每轮续期固定 30s（带抖动）、不随轮数递增、renew_max_sec 封顶
[PASS] presence_fail     # 发送失败隔离（失效会话把这批记成 `reply_state=failed`，不中断其余会话积压）
[PASS] target_derive     # 发送目标反查（session_id 内部主键 ≠ openid，从 sessions.key 取完整 openid；c2c/group 双场景）
[PASS] msg_seq           # 同一锚点的回复序号递增且不复用（下一格记在 `reply_anchors.next_seq` 里，回合内每发一段 +1），被动失败自动降级主动发送
[PASS] at_persist        # @标志/事件ID 持久化入库（积压读回恢复 is_at/event_id/msg_seq）
[PASS] audit            # 订阅 message_audit intent；审核通过/拦截事件均记日志（排查"平台受理但未展示"）
[PASS] singleton_lock    # 单实例锁（僵尸锁清理 / 存活 pid 探测，防双进程双写 db）
[PASS] web_security_api  # Cookie 登录/401/限流（登录失败 + 每 IP API 窗口）、HttpOnly+SameSite、密钥脱敏、配置类型校验、会话/记忆 API、跨站拦截、WS 鉴权、安全响应头
[PASS] web_ui            # 浏览器实测：登录/错误提示、bento 仪表盘、会话左右分栏（自动选中/切换/清空）、消息时间线、日志控制台（自动滚动到底部）、1s 刷新、侧栏折叠与 localStorage 记忆、记忆增改检索、设置保存、退出；1440px 与 390px 均无横向溢出。本轮外观/版本实测（1440×900 与 390×844，无 JS 报错、无横向溢出）：标签页标题、登录卡与侧栏的版本徽标都来自后端注入的 `{{VERSION}}`（与 `ggbot.__version__` 同值，改一处三处同步）；日志控制台表头吸顶（滚动时不穿透，≤620px 隐藏表头改由行内字段承担）、奇偶行斑马纹且 hover/等级底色仍压得过它；日志页输入「占位符」实搜命中 12 条、时间跨度 12:29→18:35，全在旧 400 行窗口之外，卡片副标题同时显示「命中 N 条，显示最近 M 条」；仪表盘末个指标补满整行、不再留下半宽孤儿卡；设置页同一行的卡片等高
[PASS] web_ui_env        # 本轮布局实测（533px 窄窗，命中 ≤620 移动端规则）：仪表盘「调用/开销」两组（识图独立、输入/输出/缓存命中率/费用）与「运行环境」卡渲染、会话表「待处理」列 + 上线事件折进状态格（空闲列隐藏）、会话详情只看消息 / 待处理 / 最近发言 / 作息（摘要段已随会话摘要一起去掉）、工具页等级徽标（L2 普通…）、设置页 6 张卡 39 个字段读写（含「满窗一次归档（条）」）、侧栏显示对话模型；无 JS 报错、无横向溢出；`POST /api/config` 拒绝 llm.api_key/vision.api_key/web.token，接受 vision.model、llm.pricing.* 与 llm.context.archive_overflow_messages，负数单价被拒；「需重启」徽标只挂在 `vision.timeout_sec` 上（19px 高、不越出字段列宽，标签首个文本节点仍是「超时 (s)」，非法数字提示不带徽标字样）
[PASS] web_static        # web/ 前端拆分托管（/ + /static）、无内联脚本/样式（CSP）、手机端日志行重排与筛选控件自适应；`GET /` 返回前只把 `{{VERSION}}` 占位换成 `ggbot.__version__`，前端不留第二份版本号
[PASS] version_badge     # 发版口径：升 `__version__` 后 `GET /` 的 HTML 里三处版本位（标签页标题 / 登录卡 / 侧栏徽标）全是新版本号且不留 `{{VERSION}}` 占位；`index.html` 源文件仍是 3 个占位符、且不含 `v<版本号>` 之类写死的版本号（发版只改 `ggbot/__init__.py` 一处）
[PASS] web_schedule      # 日程页接口（`temp/test_web_schedule.py`）：`GET /api/reminders` 默认只列 pending 且按到期时间从近到远（逾期那条排最前）、`limit` 夹在 1..500、`offset` 翻页时 `total` 与 `items` 用同一份筛选条件（`session_id`/`status`/`q` 三样都算进 total）、`status=all` 出全表、每行带 `rule_label`、`stats` 是**全表口径**（不跟着筛选走，免得顶上的数字和列表对不上）且「今天要发」从"现在"起算不重复计逾期、`limits` 回显 6 个配置项；`POST /api/reminders/{id}/cancel` 只把 pending 置 cancelled（非 pending 400、无此 id 404）；`PUT /api/reminders/{id}` 改时间/正文（正文压掉连续空白、超 `max_text_chars` 与空正文 400、两个字段都不给 400、过去时间与超 `max_days` 400、非 pending 400、无此 id 404、行在别人手里刚被巡检推走时 409），改时间只收浏览器算出的绝对 unix 秒；`DELETE /api/reminders/{id}` 删单条（含终态行，无此 id 404）；`DELETE /api/reminders` 批量清理必须带条件（无条件 400 且提示写清「条件」、`status=pending` 直接拒绝、按会话/状态/`before_days` 各删对号）；`POST /api/reminders/sweep` 回 `aged/over/total/keep_days` 且不碰 pending；工具未挂载时 `GET` 仍出数据（`limits` 全为 null）而 `PUT`/`sweep` 返回 503（没有配置可读就不给改）；未登录 401、令牌错 403；前端拆分守护——`index.html` 引用的每个 `/static/` 文件都存在、`js/` 19 个与 `css/` 6 个一个不漏不多、顺序为 util 最先 / events 最后 / responsive 最后、`schedule.js` 在 `refresh.js` 之前、侧栏 10 项且不再有 `reminders.js` / `app.js` / `style.css` 引用；页面接线：`data-page="schedule"` + `id="page-schedule"` + 三个筛选控件 + 列表容器 + 「载入更多」+ 计数 + 提示位都在，8 个 `schedule-*` 动作在 HTML 里有出处、在 `events.js` 里有分支
[PASS] web_ui_schedule   # 日程页浏览器实测（本地预览服务 + 无头窗口截图核对，1440×900 / 1280×800 / 390×844，控制台无 JS 报错、无横向溢出）：待触发按「已经过点 / 今天要发 / 这周之内 / 以后」四堆 + 「历史」一堆，每堆自己按到期时间排序（「全部」视图里后端按落定时间排的历史混进 pending 就没有方向了）；顶栏一行统计与筛选（状态/会话/关键词，搜「登山」当场只剩 1 行）；周期行挂「每天 已发 0」「每周 0/8」徽标，终态行挂状态徽标与「落定 <时间>」；行内「改 / 取消 / 删」——改弹窗预填 datetime-local 与正文且**只发人真动过的那一格**（逾期项的时间本来就是过去的，改个错别字不该被「必须是将来」挡回来），保存后 toast 报新时间；清理历史弹窗按状态实时给出「已触发 / 已取消 / 已过期」条数再确认删除；「按保留期清理」确认后 toast「到期删 0 · 超限删 0（剩 8 条）」；66 条 pending 时首屏拉 50，点「载入更多」把窗口撑到 66、按钮消失、提示改「共 66 条」，随后两轮 1s 轮询不重复插行；1s 轮询重画列表时保持滚动位置（读到一半不被弹回顶部）；手机端列折进行内、操作按钮仍点得到

[PASS] web_emojis        # 表情包页（`temp/test_web_emojis.py`）：`GET /api/emojis` 列的就是目录里的图（库目录与旧 stickers//user/ 子目录并列、手动丢进去的当场出现、删掉的条目失效），回 file/desc/added_at/url/use_count/last_used_at/has_vec 与 mode/上限，既不给向量也没有来源字段；浏览只读——不写索引、不建向量与使用统计文件；`PUT /emoji/<rel>` 改描述（清空退回文件名、超长 400、不在库里 404）、`DELETE /emoji/<rel>` 连图片文件一起删、`POST /api/emojis/import?name=` 收原始字节上传并按摘要去重（非图片 / 空文件 400），三个写动作都要登录；`GET /emoji/<rel>` 出图且 `Cache-Control: private`，非图片 / 不存在 / `%2e%2e%2f` 越界一律 404；扫目录缓存：库目录一变立刻重扫，没变时 TTL 内复用同一次结果，`?force=1` 强制重扫；面板不再有来源筛选与内置/收藏徽标（「内置默认」这个词现在人设页也在用，所以这条只查表情页那一段 markup），表情页不进 1s 轮询；消息流整页下线（`js/messages.js` 删除、侧栏 10 项、util/nav/poll/refresh/events/sessions 里不再残留 `refreshMessages` 与 `#msg-` 接线），`/api/messages` 保留给仪表盘「最近消息」
[PASS] web_persona       # 人设页（`temp/test_web_persona.py`）：老 config 没有 `llm.system_prompt` 时加载即补空串（`set_by_path` 才写得进去）；留空/全空白 = 用代码里的内置默认，写进去后常驻的 PresenceManager 下一轮就换新人设（无需重启），`cfg.save()` 落盘重开仍在；`GET/POST /api/system-prompt` 未登录 401、回执含 `custom/default/effective/is_custom/max_chars`、清空保存回到内置默认、超过 20000 字 400；`llm.system_prompt` 不在设置白名单里，走 `POST /api/config` 会被记 `rejected`（人设只有一条写入路径）；前端接线：导航项 / `page-persona` / `persona.js` 引用 / `PAGE_META` / 三个按钮都在，编辑框不带 `data-path`（通用设置保存碰不到它），`poll.js` 与 `refreshAll` 都不碰它（正在打字不会被 1s 轮询冲掉），有未保存改动时切页回来仍是原样；浏览器实测（1440×900 与 390×844，无 JS 报错、无横向溢出）：进页面显示内置默认那 1551 字 + 徽标「内置默认」→ 改成一句话保存 → 徽标变「自定义」、toast「人设已保存，下一轮生效」、重载后编辑框仍是新内容 → 清空保存回到「内置默认」；「填入内置默认」把 1551 字灌回编辑框；粘 20001 字页脚变红并报「系统提示词最长 20000 字，现在 20001 字」，服务端旧值不动，绕过前端直接 POST 也拿到同一句 400
[PASS] temp_video_policy # 视频附件不下载 + temp 过期文件清理
[PASS] deploy_scripts    # 部署脚本离线回归（`temp/test_deploy_scripts.py`，只用 bash 与临时目录，不碰服务器）：`deploy/*.sh` 与两个 unix 启动脚本 `bash -n` 通过且都是 LF；`render_unit` 把模板里写死的 `/root/GGBot` 换成传入的/当前项目根（`WorkingDirectory`/`ExecStart` 两处都换、不留旧路径、`LimitNOFILE` 不动），`apply_unit` 对同一目标第二次判定「已是最新」且不再 `daemon-reload`；`web_probe` 五种配置（缺失/关面板/自定义端口/无 web 段/坏 JSON）都按代码默认口径给出 `on|off 端口`；`bot_pids` 对认不出的项目根返回空且退出 0；`deploy/rsync-exclude.txt` 逐条盯住 14 个排除项（含 `*.db.bak-*`，`--reset-db` 的库备份不能被 `--delete` 顺手清了）、是纯模式不含 `#`/`;` 注释行、不碰 `config.example.json`，workflow 改成 `--exclude-from` 引用这份、不再有内联 heredoc、没有 `--delete-excluded`；三个启动脚本的探测模块名一致、与 `requirements.txt` 一一对应，并覆盖代码里所有直接 import 的第三方（`pydantic` 由 fastapi 带装，是唯一例外）；unit 的 `ExecStart` 不带 `--log-level`；`deploy_server.sh` systemd 分支的现场是 `journalctl` 而 `stdout.log` 只在 nohup 分支 tail
```

> 未覆盖：QQ 真实收发（需机器人上线 + 群内实测）；`send_image/video/voice` 与表情/克隆语音的富媒体实发（需真实群；本地文件走 base64 直传）；**群全量事件只用 `msg_id`（不带 `event_id`）的被动回复是否真被平台受理**——官方文档写明该事件的 `id` 可用于被动回复，本机没有实测条件，判断依据是之前带上 `event_id` 时被拒（40034027）。若线上仍被拒，日志会有 `被动回复失败，降级主动发送: …`，消息照发不丢，只是白耗一次请求；真出现这种情况就看那条警告的返回码，把 `on_group_message_create` 的 `passive_ok` 改回 False 即可退回旧行为。到点日程的实发（需真实群 + 等一个到期时间）同样没测，周期项「发一场滚下一场」在线上跨天滚动、以及停机一晚后那行 `提醒跳过 N 场未发` 也只做了离线验证。记忆的取代判定用假 LLM 固定了四种回执（new/update/drop/retire）与各种畸形回执，**真模型判得准不准、embedding 服务这一轮是否还通**都要看线上那行 `历史归档 … → 记忆 新增 a · 改写 b · 作废 c`——本轮没有再打真实的 `/embeddings` 调用（会实际计费），向量路径由注入假向量的本地用例覆盖。

