# GGBot — QQ 群聊机器人

基于官方 QQ 开放平台 api-v2（qq-botpy 1.2.1）+ DeepSeek（deepseek-flash）+ SiliconFlow 向量记忆的群聊机器人。

**四大模块：**
1. **消息收发网关**（`ggbot/bot.py`）— 群@/单聊事件接收、归一化、去重，富媒体附件落地 `temp/`
2. **AI 请求装置**（`ggbot/llm.py`）— DeepSeek Chat Completions，function calling 原生工具
3. **上下文组织 / 持久化记忆 / 自我进化**（`ggbot/storage.py` + `ggbot/memory.py` + `ggbot/tools.py`）
4. **Web 控制面板**（`ggbot/web.py`）— FastAPI，在线状态 / 消息流 / 记忆检索 / 规则 / 人设管理

## 目录结构

```
GGBot/
├── db/
│   ├── config.json      # 全部配置（QQ 凭据 / DeepSeek / Embedding / 行为 / 存储 / Web；含密钥，gitignored）
│   ├── persona_core.md  # 内核人设（owner 定义，AI 只读；首次启动自动导入）
│   └── ggbot.db         # SQLite 持久化：会话/消息/记忆/规则/人设版本（运行时生成，gitignored）
├── temp/                # 富媒体附件临时落地（图片/语音/视频/文件，gitignored）
├── requirements.txt
├── main.py              # 入口
├── start.bat            # Windows 前台启动（Ctrl+C 停止）
├── start.sh             # Linux 后台启动（nohup，kill <PID> 停止）
├── logs/                # 运行日志（5MB × 5 轮转，gitignored）
├── ggbot/
│   ├── config.py        # 配置加载（embedding 支持环境变量覆盖 + 热更新）
│   ├── logging_setup.py # 日志系统（多等级 / 文件轮转 / 面板 tail）
│   ├── storage.py       # SQLite（会话/消息/记忆/规则/人设版本）
│   ├── memory.py        # 向量记忆（SiliconFlow Embedding + 余弦检索 + 整理）
│   ├── llm.py           # DeepSeek 请求装置
│   ├── tools.py         # 8 个 function calling 工具 + agent 循环
│   ├── presence.py      # 在线/离线状态机（唤醒/积压分流/抖动/分段发送）
│   ├── bot.py           # QQ 网关
│   └── web.py           # Web 面板（状态/消息/记忆/设置/日志）
└── docs/实施方案.md
```

## 快速开始

```powershell
# 1. 虚拟环境（已建 .venv）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 配置 db/config.json（密钥已写入）：
#    appid / appsecret          → QQ 机器人凭据（qq 开放平台后台）
#    deepseek.api_key           → DeepSeek key
#    embedding.api_key          → SiliconFlow key（Qwen3-Embedding-4B）

# 3. 启动
start.bat                    # Windows 前台（Ctrl+C 停止）
# 或
.\.venv\Scripts\python.exe main.py [--no-web] [--log-level DEBUG]
# Linux 后台：
./start.sh
```

启动后：QQ gateway 连接成功 → 群@消息触发 → Presence 状态机（离线积压 / @唤醒 / 在线抖动聚合 / 选择性回复）。Web 面板：`http://127.0.0.1:8080`（需 `?token=ggbot-local` 或 `X-Token` 头）。

## 日志与 Web 运维

- **日志文件**：`logs/ggbot.log`（RotatingFileHandler，5MB × 5 轮转；stderr 同时输出）。格式 `时间 | 等级 | 模块 | 内容`；botpy/openai/httpx 等第三方库默认 WARNING 降噪
- **等级控制**：`config.json → web.log_level`（默认 INFO）、`--log-level DEBUG`、环境变量 `LOG_LEVEL`，三者优先级递增；面板保存设置即时热切换
- **Web 面板查看**：设置卡（LLM 参数 / Presence / 积压 / 日志等级，白名单热更新并写回 config.json）+ 日志卡（等级过滤，5s 自动刷新）
- **API**：`GET /api/config`（密钥脱敏）、`POST /api/config`（`[{"path": "a.b.c", "value": x}]` 批量，越权路径忽略）、`GET /api/logs?lines=&level=`
- **embedding 环境变量覆盖**：`EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` 优先于 config.json（当前默认 SiliconFlow Qwen/Qwen3-Embedding-4B）

## 关键行为（config.json `behavior`）

| 机制 | 配置 | 说明 |
|---|---|---|
| 在线/离线作息 | `presence.online_duration_sec` / `offline_interval_sec` | 定时上线、在线一段时间后离线 |
| @唤醒 | `presence.wake_on_at_message` | 离线被@ → 立即上线并立即回复该条（不进积压） |
| 聊得火热 | `presence.renew_on_reply` | 在线每次回复重置在线计时器 |
| 抖动聚合 | `reply.debounce_sec` | 在线消息 10s 窗口聚合，LLM 选择性回复（非每条必回） |
| 积压分流 | `backlog.*` | 上线扫描 pending（≤1000），@/提问单独回，其余每 100 条合并一条 |
| 分段发送 | `reply.max_segments` / `max_chars_per_segment` / `segment_gap_sec` | 回复拆短句逐条发送（msg_seq 递增，官方支持同一消息多次回复） |

## 工具（DeepSeek function calling，`ggbot/tools.py`）

`send_text` / `send_image` / `send_voice`(TTS) / `send_video` / `send_emoji` / `memory_search` / `memory_write` / `persona_update`

> 富媒体发送限制：官方 `post_group_file` 上传需要**公网 URL**。转发群友刚发的图片/语音/视频直接用其附件 URL（代码已记录）；本地生成的文件（如 TTS 语音）需配置 `storage.public_base_url` 指向公网可访问的目录，否则该工具返回明确错误。

## 记忆向量化

- 嵌入：SiliconFlow `Qwen/Qwen3-Embedding-4B`（OpenAI 兼容 `/embeddings`，无本地模型）
- 写入：AI 调 `memory_write`（importance 1-5）
- 检索：`memory_search` 余弦相似度 top-k，命中记忆拼入 system prompt
- 整理：面板「整理」按钮合并近似重复（阈值 0.95）

## 验证记录（已通过）

```
[PASS] import_all        # 全部模块导入
[PASS] storage_crud      # SQLite 会话/消息/记忆/规则/人设 CRUD + 积压查询 + 回滚
[PASS] memory_vector     # SiliconFlow embedding 真实调用 + 语义检索 + tidy 合并
[PASS] llm_chat          # DeepSeek deepseek-flash 真实调用（thinking=disabled 生效）
[PASS] tools_schema      # 8 工具 schema 校验
[PASS] presence_split    # 分段逻辑（空行/按句/无标点硬切/限段）
[PASS] tool_loop         # function calling 端到端：AI 调用 memory_write 真实入库
[PASS] config_env_override   # embedding 环境变量优先于 config.json
[PASS] logging_system        # 多等级写文件 + tail 等级过滤 + set_level 热切换
[PASS] web_api_settings_logs # /api/config 白名单热更新写回、密钥脱敏、越权忽略；/api/logs
```

> 未覆盖：QQ 真实收发（需机器人上线 + 群内实测）；`send_image/video/voice` 富媒体实发（需真实群 + 公网 URL）。

## 已知限制

- qq-botpy 1.2.1（2024-03 停更）但内置新鉴权（appId+clientSecret → access_token 自动缓存刷新），实测可用
- 语音 TTS 产出 mp3，QQ 语音富媒体为 silk 格式，平台不接收时该工具返回错误（转发收到的语音不受影响）
- 表情消息类型官方未单列，`send_emoji` 以文本形式发送
- Web 面板无登录页，仅本地 token 鉴权；公网暴露请自行加反向代理
