"""GGBot — QQ 群聊机器人。

四大模块：
1. 消息收发网关（bot.py）：qq-botpy 事件接收 + 归一化 + 富媒体落地
2. AI 请求装置（llm.py）：DeepSeek Chat Completions（function calling）
3. 上下文组织 / 持久化记忆 / 自我进化（storage.py + memory.py + tools.py）
4. Web 控制面板（web.py）
"""

__version__ = "0.1.0"
