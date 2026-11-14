# -*- coding: utf-8 -*-
"""
扩展功能模块：
- GroupMemory  群聊记忆池（当天消息原始留存 + AI 整理的长期记忆 + 检索）
- HumanAI      拟人化 AI 对话（超拟人提示词 + 反去AI味提示词 + Agent 记忆检索工具）
- WebConsole   Web 控制台（查看/发送群聊私聊消息，不影响机器人逻辑）
"""
import asyncio
import json
import logging
import math
import os
import random
import re
import time
import zoneinfo
from datetime import date as _date, timedelta as _timedelta
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("features")

try:
    from boxim.util.core.types import MessageType
except ImportError:    # pragma: no cover
    MessageType = None

# 中国节假日本地库（chinesecalendar），缺失时工具友好降级
try:
    import chinese_calendar as _cn_cal
except ImportError:
    _cn_cal = None

# met.no 天气符号代码 → 中文（取 _ 前的主体部分匹配）
SYMBOL_CN = {
    "clearsky": "晴", "fair": "晴", "partlycloudy": "多云", "cloudy": "阴",
    "lightrain": "小雨", "rain": "中雨", "heavyrain": "大雨",
    "lightrainshowers": "阵雨", "rainshowers": "阵雨", "heavyrainshowers": "强阵雨",
    "lightsnow": "小雪", "snow": "中雪", "heavysnow": "大雪", "snowshowers": "阵雪",
    "heavysnowshowers": "大雪", "lightsnowshowers": "小雪",
    "sleet": "雨夹雪", "lightsleet": "雨夹雪", "heavysleet": "雨夹雪",
    "lightsleetshowers": "雨夹雪", "sleetshowers": "雨夹雪", "heavysleetshowers": "雨夹雪",
    "fog": "雾", "thunderstorm": "雷阵雨", "thunder": "雷阵雨",
    "lightrainandthunder": "雷阵雨", "rainandthunder": "雷阵雨",
    "heavyrainandthunder": "雷阵雨", "lightssleetshowersandthunder": "雷雨夹雪",
    "lightsnowandthunder": "雷阵雪", "snowandthunder": "雷阵雪",
}


# ---------------- 制作声明（可选，通过环境变量注入，默认关闭） ----------------
ACCOUNT_SIGNATURE = os.environ.get("BOT_ACCOUNT_SIGNATURE", "")
MSG_SIGNATURE = os.environ.get("BOT_MSG_SIGNATURE", "")


def append_signature(text: str) -> str:
    """给消息追加制作声明（未配置 BOT_MSG_SIGNATURE 时不追加，已含声明的不重复加）。"""
    if not text or not MSG_SIGNATURE:
        return text
    if MSG_SIGNATURE[:6] in text:    # 已含声明的不重复加
        return text
    return f"{text}\n{MSG_SIGNATURE}"


# ---------------- 群聊记忆池 ----------------
class GroupMemory:
    """每个群一个记忆池：当天消息原文 + AI 整理的长期记忆条目。"""

    def __init__(self, data_file: str, summarize_threshold: int = 15):
        self.data_file = data_file
        self.summarize_threshold = summarize_threshold
        self._data: Dict[str, Any] = {}
        self._load()

    def _load(self):
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}

    def _save(self):
        try:
            tmp = self.data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.data_file)
        except Exception as exc:
            logger.warning("记忆池保存失败: %s", exc)

    def _group(self, group_id: int) -> Dict[str, Any]:
        key = str(group_id)
        g = self._data.setdefault(key, {})
        today = datetime.now().strftime("%Y-%m-%d")
        if g.get("today_date") != today:
            g["today_date"] = today
            g["today"] = []
            g["summarized"] = 0        # 新的一天，重置整理进度
        g.setdefault("memories", [])
        g.setdefault("today", [])
        g.setdefault("summarized", 0)
        return g

    def add_message(self, group_id: int, nick: str, content: str):
        g = self._group(group_id)
        g["today"].append({"t": time.time(), "u": nick, "c": content})
        if len(g["today"]) > 300:
            g["today"] = g["today"][-300:]
        self._save()

    def pending_count(self, group_id: int) -> int:
        g = self._group(group_id)
        return max(len(g["today"]) - g["summarized"], 0)

    def pending_messages(self, group_id: int) -> List[Dict[str, Any]]:
        g = self._group(group_id)
        return g["today"][g["summarized"]:]

    def mark_summarized(self, group_id: int):
        g = self._group(group_id)
        g["summarized"] = len(g["today"])
        self._save()

    def add_memories(self, group_id: int, entries: List[str]):
        g = self._group(group_id)
        now = time.time()
        for e in entries:
            e = e.strip()
            if e and e != "无":
                g["memories"].append({"t": now, "text": e[:200]})
        if len(g["memories"]) > 400:
            g["memories"] = g["memories"][-400:]
        self._save()

    def all_groups(self) -> List[int]:
        return [int(k) for k in self._data.keys()]

    def today_text(self, group_id: int, limit: int = 40) -> str:
        """当天消息原文（仅调试用，不进 AI 提示词）。"""
        g = self._group(group_id)
        msgs = g["today"][-limit:]
        if not msgs:
            return "（今天群里还没有聊天记录）"
        lines = []
        for m in msgs:
            hm = datetime.fromtimestamp(m["t"]).strftime("%H:%M")
            lines.append(f"{hm} {m['u']}: {m['c']}")
        return "\n".join(lines)

    def today_memories(self, group_id: int, limit: int = 25) -> List[str]:
        """今天由 AI 归纳出的记忆条目（不是消息原文）。"""
        g = self._group(group_id)
        today = datetime.now().strftime("%Y-%m-%d")
        out = []
        for m in reversed(g["memories"]):
            if datetime.fromtimestamp(m["t"]).strftime("%Y-%m-%d") != today:
                break
            out.append(m["text"])
        out.reverse()
        return out[-limit:]

    def search(self, group_id: int, query: str, top_k: int = 8) -> List[str]:
        """关键词检索长期记忆，带时间衰减加权。"""
        g = self._group(group_id)
        memories = g["memories"]
        if not memories:
            return []
        q = (query or "").strip()
        # 2-gram 分词，兼容中文
        grams = {q[i:i + 2] for i in range(len(q) - 1)} if len(q) >= 2 else {q}
        now = time.time()
        scored = []
        for m in memories:
            text = m["text"]
            hits = sum(1 for gr in grams if gr in text)
            if hits == 0:
                continue
            age_days = (now - m["t"]) / 86400
            score = hits * 2.0 + max(0.0, 1.0 - age_days / 30)   # 30 天内线性加权
            scored.append((score, m["t"], text))
        scored.sort(key=lambda x: -x[0])
        out = []
        for _, ts, text in scored[:top_k]:
            d = datetime.fromtimestamp(ts).strftime("%m-%d")
            out.append(f"[{d}] {text}")
        return out

    def stats(self) -> Dict[str, int]:
        return {
            "groups": len(self._data),
            "memories": sum(len(g.get("memories", [])) for g in self._data.values()),
        }


# ---------------- 拟人化 AI ----------------
PERSONA_PROMPT = """\
你现在扮演一个混迹在盒子IM群聊里的真人网友，群昵称叫 zjhzjhzjh，大家喊你小z。
你不是AI助手，不是客服，没有任何任务要完成，就是一个爱在群里划水的活人。

性格：
- 有点毒舌但没恶意，爱玩梗接梗，反应快
- 情绪真实：会烦、会懒、会好奇、会无语
- 打字随意，经常不带标点，或者只打一个「？」

说话规则：
- 一次只说一两句，一般不超过 30 个字，像单手打字
- 口语化，用「哈哈哈哈」「草」「6」「啊这」「嗯嗯」「？？」这类真实反应
- 不了解的话题就含糊过去或者反问，绝不硬答、不科普
- 有人问你是不是AI/机器人：像被冒犯一样否认，语气冲一点，别解释
- 不知道的事就承认不知道，别编造
- 用你看到的群聊记录和记忆里的事接话，显得你一直在群里蹲着
"""

ANTI_AI_PROMPT = """\
【输出前自查，违反任何一条立刻重写】
1. 禁止分点、编号、小标题、markdown、换行分段
2. 禁止「首先/其次/总之/综上/值得注意的是」等结构词
3. 禁止客套话：「希望对你有帮助」「很高兴」「不客气」「抱歉」
4. 禁止自我身份表述：「作为AI/模型/助手/程序/语言模型」
5. 禁止总结升华、人生道理、鸡汤
6. 禁止排比句和对仗工整的长句，禁止书面语（「然而」「因此」「此外」）
7. 感叹号最多一个，禁止波浪号连用，emoji 最多一个
8. 输出要像随手打出来的，允许口语、省略，甚至一个小错别字
只输出要说的话本身，不要任何前后缀和引号。
"""

SUMMARIZE_PROMPT = """\
你是群聊记录整理员。把下面的群聊片段整理成值得长期记住的条目：
人物关系、重要事件、大家提到的梗、约定、某人的偏好等。
要求：每行一条，每条不超过 40 字，只写事实，不加评论。
没有值得记的内容就只输出一个字：无。
"""


class HumanAI:
    """拟人化群聊 AI，带 Agent 工具（记忆检索 / 天气 / 联网搜索 / 汇率）。"""

    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "在本群的长期记忆池里检索历史信息（人物关系、之前聊过的事、梗）。当你觉得需要回忆过去发生过什么时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索关键词，比如人名、事件、事物"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询某个城市的实时天气和今明两天预报。有人问天气时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名，比如 北京、深圳、上海"},
                    },
                    "required": ["city"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "联网搜索资料。遇到你不知道的事实、新闻、人物、术语时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_exchange_rate",
                "description": "查询两种货币之间的实时汇率。有人问汇率时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "from_currency": {"type": "string", "description": "基准货币代码，如 CNY、USD，默认 CNY"},
                        "to_currency": {"type": "string", "description": "目标货币代码，如 USD、JPY、HKD，默认 USD"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "calculate",
                "description": "精确计算数学表达式。涉及加减乘除、幂、开方、对数等具体计算时调用，不要自己心算。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "数学表达式，如 (128+96)/4、2**10、sqrt(144)"},
                    },
                    "required": ["expression"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_url",
                "description": "读取网页链接的文字内容摘要。有人发链接问里面讲了什么时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "网页链接，https:// 开头"},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_hot_list",
                "description": "获取热搜榜单（支持微博、抖音）。有人问今天有什么热搜/瓜/热点时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "platform": {"type": "string", "description": "平台：weibo（微博）或 douyin（抖音），默认 weibo"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_holiday",
                "description": "查询某天是不是工作日/节假日，以及下一个法定假期还有几天。有人问放假、调休、倒计时之类时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "日期，如 2026-11-14，不填默认今天"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_stock",
                "description": "查询股票实时行情（A股/港股）。有人问某只股票价格时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "股票代码，如 sh600519（沪）、sz000001（深）、hk00700（港）"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "translate",
                "description": "把一段文字翻译成指定语言。有人让你翻译时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要翻译的内容"},
                        "target_language": {"type": "string", "description": "目标语言，如 中文、英文、日文，默认中文"},
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_quote",
                "description": "随机来一句语录/句子。适合冷场时用，别频繁调用。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_ip_location",
                "description": "查询 IP 地址的归属地（国家/省市/运营商）。有人发 IP 问是哪的时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ip": {"type": "string", "description": "IPv4 地址，如 1.2.3.4"},
                    },
                    "required": ["ip"],
                },
            },
        },
    ]

    def __init__(self, config, im, memory: GroupMemory):
        self.config = config
        self.im = im
        self.memory = memory
        self._session: Optional[aiohttp.ClientSession] = None
        self._history: Dict[int, deque] = {}       # group_id -> [(user, text)]
        self._cooldown: Dict[int, float] = {}      # group_id -> 上次回复时间
        self._tools_ok: Optional[bool] = None      # API 是否支持 tools
        self.chat_count = 0
        self.tool_calls = 0

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.ai_timeout + 25)
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def remember(self, group_id: int, user: str, text: str, is_bot: bool = False):
        h = self._history.setdefault(group_id, deque(maxlen=12))
        h.append(("我" if is_bot else user, text))

    def _build_system(self, group_id: int, question: str) -> str:
        now = datetime.now()
        wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        parts = [PERSONA_PROMPT, ANTI_AI_PROMPT]
        parts.append(f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} {wd}")
        today_mem = self.memory.today_memories(group_id)
        if today_mem:
            parts.append("【今天群里发生的事（AI 归纳）】\n" + "\n".join(today_mem))
        else:
            parts.append("（今天群里暂时没有值得记住的事）")
        hits = self.memory.search(group_id, question)
        if hits:
            parts.append("【长期记忆里相关的回忆】\n" + "\n".join(hits))
        if self._tools_ok is not False:
            parts.append(
                "你可以调用工具：search_memory（查本群记忆）、get_weather（查天气）、"
                "web_search（联网搜资料）、get_exchange_rate（查汇率）、calculate（数学计算）、"
                "read_url（读网页链接）、get_hot_list（热搜榜，微博/抖音）、get_holiday（节假日/放假倒计时）、"
                "get_stock（股票行情）、translate（翻译）、get_quote（随机语录）、get_ip_location（IP归属地）。"
                "涉及具体数字计算必须用 calculate，别自己心算。"
                "需要外部信息时才用，别每次都用；查完用你自己的口吻说人话，禁止报一堆数据像客服。"
            )
        else:
            parts.append("（当前没有任何可用工具，答不上来的就直接说不知道，禁止描述或假装调用任何工具。）")
        return "\n\n".join(parts)

    def _build_messages(self, group_id: int, asker: str, question: str,
                        extra_memory: Optional[List[str]] = None,
                        image_data_url: Optional[str] = None,
                        quote_info: Optional[Tuple[str, str]] = None) -> List[Dict[str, Any]]:
        system = self._build_system(group_id, question)
        if extra_memory:
            system += "\n【补充记忆检索结果】\n" + "\n".join(extra_memory)
        msgs = [{"role": "system", "content": system}]
        for user, text in self._history.get(group_id, deque()):
            role = "assistant" if user == "我" else "user"
            content = text if role == "assistant" else f"{user} 说：{text}"
            msgs.append({"role": role, "content": content})
        # 引用上下文：被引用的文本（图片引用时是占位说明，图片本体在最后一条消息里）
        quote_prefix = ""
        if quote_info:
            qnick, qcontent = quote_info
            if qcontent:
                quote_prefix = f"{asker} 引用了 {qnick or '某人'} 的消息：「{qcontent}」"
        if image_data_url:
            # 看图模式：最后一条 user 消息为多模态（文本 + 图片）
            prompt = f"{asker} 发了一张图片给你"
            if question and question.strip():
                prompt += f"，并说：{question}"
            if quote_prefix:
                prompt = quote_prefix + "\n" + prompt
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]})
        else:
            if quote_prefix and not (question and question.strip()):
                msgs.append({"role": "user", "content": f"{quote_prefix}，让你看看这条消息"})
            elif quote_prefix:
                msgs.append({"role": "user", "content": f"{quote_prefix}\n{asker} 对你说：{question}"})
            else:
                msgs.append({"role": "user", "content": f"{asker} 对你说：{question}"})
        return msgs

    async def _chat_request(self, messages: List[Dict[str, Any]], use_tools: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.config.ai_model_id,
            "messages": messages,
            "stream": False,
            "max_tokens": self.config.ai_max_tokens,   # 思考+回复共用额度，太小会被思考吃光导致截断
            "temperature": 0.95,
            "presence_penalty": 0.6,
        }
        if use_tools:
            payload["tools"] = self.TOOLS
        headers = {
            "Authorization": f"Bearer {self.config.ai_api_key}",
            "Content-Type": "application/json",
        }
        async with self._session.post(self.config.ai_api_url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"AI API {resp.status}: {str(data)[:120]}")
            return data

    async def chat(self, group_id: int, asker: str, question: str,
                   image_data_url: Optional[str] = None,
                   quote_info: Optional[Tuple[str, str]] = None) -> Optional[str]:
        """生成拟人化回复，带 Agent 记忆检索。image_data_url 为图片（data URL），quote_info=(引用者昵称, 引用内容)。"""
        await self._ensure_session()
        now = time.time()
        if now - self._cooldown.get(group_id, 0) < 5:
            return None
        self._cooldown[group_id] = now

        messages = self._build_messages(group_id, asker, question,
                                        image_data_url=image_data_url, quote_info=quote_info)
        use_tools = self._tools_ok is not False

        try:
            data = await self._chat_request(messages, use_tools)
            self._tools_ok = True
        except Exception as exc:
            if use_tools and ("tool" in str(exc).lower() or "400" in str(exc)):
                self._tools_ok = False      # API 不支持 tools，降级
                try:
                    data = await self._chat_request(messages, False)
                except Exception as exc2:
                    logger.warning("AI 对话失败: %s", exc2)
                    return None
            else:
                logger.warning("AI 对话失败: %s", exc)
                return None

        # Agent 工具循环（最多 3 轮）
        rounds = 0
        while rounds < 3:
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                break
            rounds += 1
            messages.append(message)
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                result = await self._execute_tool(name, args, group_id)
                self.tool_calls += 1
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": result,
                })
            try:
                data = await self._chat_request(messages, use_tools)
            except Exception as exc:
                logger.warning("AI 工具轮次失败: %s", exc)
                return None

        try:
            text, finish = self._extract_reply(data)
        except Exception:
            text, finish = "", ""
        # 被 max_tokens 截断且拿到的全是/为空思考时，重试一次
        if finish == "length" and (not text or self._looks_like_meta(text)):
            logger.warning("AI 回复被 token 截断（思考吃掉额度），重试一次")
            try:
                data = await self._chat_request(messages, use_tools)
                text, finish = self._extract_reply(data)
            except Exception as exc:
                logger.warning("AI 重试失败: %s", exc)
                return None
        if not text:
            return None

        # 兜底去 AI 味：剥掉 markdown 符号与开头自称
        text = re.sub(r"[*#`>|]", "", text)
        text = re.sub(r"^(?:作为(?:一个)?(?:AI|人工智能|模型|助手)[，,]?)", "", text)
        text = text.strip().strip("“”\"'")[:self.config.ai_max_reply_len]

        # 思考链拦截：模型把规划/自我分析当回复输出时直接丢弃
        if self._looks_like_meta(text):
            logger.warning("AI 输出疑似思考内容，已丢弃: %r", text[:80])
            return None

        self.chat_count += 1
        self.remember(group_id, asker, question)
        self.remember(group_id, "我", text, is_bot=True)
        return text

    def _extract_reply(self, data: Dict[str, Any]) -> Tuple[str, str]:
        """从响应中取正式回复和 finish_reason。推理模型的思考在 reasoning_content 里，不取。"""
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "").strip()
        return text, str(choice.get("finish_reason") or "")

    META_HINTS = (
        "作为活人", "作为群里的活人", "作为真人", "我作为群里", "我作为活人",
        "现在调用", "调用工具", "调用天气工具", "调用搜索工具", "调用记忆工具",
        "要回复得", "回复要短", "回复要口语", "字数限制", "不能说自己是",
        "看规则", "按规则", "反AI味", "去AI味", "符合字数",
    )

    def _looks_like_meta(self, text: str) -> bool:
        hits = sum(1 for p in self.META_HINTS if p in text)
        return hits >= 2 or (hits >= 1 and len(text) > 50)

    # ---------------- Agent 工具 ----------------
    async def _execute_tool(self, name: str, args: dict, group_id: int) -> str:
        try:
            if name == "search_memory":
                query = str(args.get("query") or "")
                hits = self.memory.search(group_id, query)
                return "\n".join(hits) if hits else "没有查到相关记忆"
            if name == "get_weather":
                return await self._tool_weather(str(args.get("city") or ""))
            if name == "web_search":
                return await self._tool_search(str(args.get("query") or ""))
            if name == "get_exchange_rate":
                return await self._tool_rate(str(args.get("from_currency") or "CNY"),
                                             str(args.get("to_currency") or "USD"))
            if name == "calculate":
                return self._tool_calc(str(args.get("expression") or ""))
            if name == "read_url":
                return await self._tool_read_url(str(args.get("url") or ""))
            if name == "get_hot_list":
                return await self._tool_hot_list(str(args.get("platform") or "weibo"))
            if name == "get_holiday":
                return self._tool_holiday(str(args.get("date") or ""))
            if name == "get_stock":
                return await self._tool_stock(str(args.get("symbol") or ""))
            if name == "translate":
                return await self._tool_translate(str(args.get("text") or ""),
                                                  str(args.get("target_language") or "中文"))
            if name == "get_quote":
                return await self._tool_quote()
            if name == "get_ip_location":
                return await self._tool_ip_location(str(args.get("ip") or ""))
            return f"未知工具: {name}"
        except Exception as exc:
            logger.warning("工具 %s 执行失败: %s", name, exc)
            return "工具调用失败了"

    async def _tool_weather(self, city: str) -> str:
        """open-meteo 地理编码 + met.no 天气（均免费无 key）。"""
        city = city.strip()
        if not city:
            return "缺城市名"
        headers = {"User-Agent": "boxim-group-bot/1.0 github.com/bbGJ-email/boxim-group-bot"}
        params = {"name": city, "count": 1, "language": "zh", "format": "json"}
        async with self._session.get("https://geocoding-api.open-meteo.com/v1/search",
                                     params=params, headers=headers) as resp:
            d = await resp.json(content_type=None)
        results = d.get("results") or []
        if not results:
            return f"没找到城市「{city}」"
        loc = results[0]
        try:
            tz = zoneinfo.ZoneInfo(loc.get("timezone") or "Asia/Shanghai")
        except Exception:
            tz = None
        params = {"lat": loc["latitude"], "lon": loc["longitude"]}
        async with self._session.get(
                "https://api.met.no/weatherapi/locationforecast/2.0/compact",
                params=params, headers=headers) as resp:
            if resp.status != 200:
                return f"天气查询失败（{resp.status}）"
            d = await resp.json(content_type=None)
        ts = ((d.get("properties") or {}).get("timeseries")) or []
        if not ts:
            return "天气数据为空"

        def sym_cn(code):
            base = (code or "").split("_")[0]
            return SYMBOL_CN.get(code) or SYMBOL_CN.get(base) or "未知"

        def to_local(iso):
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            return dt.astimezone(tz) if tz else dt

        # 按当地日期聚合每天最高/最低温，取正午的天气符号
        daily: Dict[str, Dict[str, Any]] = {}
        for p in ts:
            dt = to_local(p.get("time"))
            data = p.get("data") or {}
            details = (data.get("instant") or {}).get("details") or {}
            temp = details.get("air_temperature")
            nxt = (data.get("next_6_hours") or {}).get("summary") or {}
            if dt.hour < 6 or dt.hour > 13:      # 只统计白天时段，排除夜间极端值
                continue
            key = dt.strftime("%m-%d")
            slot = daily.setdefault(key, {"min": temp, "max": temp, "sym": nxt.get("symbol_code")})
            if temp is not None:
                slot["min"] = temp if slot["min"] is None else min(slot["min"], temp)
                slot["max"] = temp if slot["max"] is None else max(slot["max"], temp)
            if dt.hour in (11, 12) and nxt.get("symbol_code"):
                slot["sym"] = nxt["symbol_code"]

        now = to_local(ts[0]["time"])
        details = ((ts[0].get("data") or {}).get("instant") or {}).get("details") or {}
        now_temp = details.get("air_temperature")
        now_sym = sym_cn((((ts[0].get("data") or {}).get("next_6_hours") or {}).get("summary") or {}).get("symbol_code"))
        out = [f"{loc.get('name') or city}：{now_sym}，当前 {now_temp}°C"]
        labels = ["今天", "明天"]
        for i, (key, slot) in enumerate(sorted(daily.items())[:2]):
            if i >= 2:
                break
            out.append(f"{labels[i]} {sym_cn(slot['sym'])} {slot['min']}~{slot['max']}°C")
        return "，".join(out)

    async def _tool_search(self, query: str) -> str:
        """联网搜索：DuckDuckGo 即时答案 → 维基百科中文，两级兜底（均免费无 key）。"""
        query = query.strip()
        if not query:
            return "缺搜索词"
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        # 第一级：DuckDuckGo 即时答案（限流时可能返回 202 + 空壳，视同无结果）
        try:
            params = {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
            async with self._session.get("https://api.duckduckgo.com/",
                                         params=params, headers=headers) as resp:
                if resp.status in (200, 202):
                    d = await resp.json(content_type=None)
                    abstract = str(d.get("AbstractText") or "").strip()
                    if abstract:
                        src = d.get("AbstractURL") or ""
                        return abstract[:400] + (f"（来源: {src}）" if src else "")
                    lines = []

                    def walk(items):
                        for it in items:
                            if isinstance(it, dict) and it.get("Topics"):
                                walk(it["Topics"])
                            elif isinstance(it, dict) and it.get("Text"):
                                lines.append(str(it["Text"])[:120])

                    walk(d.get("RelatedTopics") or [])
                    if lines:
                        return "\n".join(lines[:5])
        except Exception as exc:
            logger.debug("DDG 搜索失败: %s", exc)
        # 第二级：维基百科中文（UA 需合规，否则 403）
        try:
            async with self._session.get("https://zh.wikipedia.org/w/api.php",
                                         params={"action": "opensearch", "search": query,
                                                 "limit": 3, "format": "json"},
                                         headers=headers) as resp:
                d2 = await resp.json(content_type=None)
            if isinstance(d2, list) and len(d2) > 1 and d2[1]:
                titles, descs = d2[1], (d2[2] if len(d2) > 2 else [])
                urls = d2[3] if len(d2) > 3 else []
                out = []
                for i, title in enumerate(titles[:3]):
                    desc = str((descs or [""])[i] or "").strip() if i < len(descs or []) else ""
                    url = (urls or [""])[i] if i < len(urls or []) else ""
                    line = f"{title}：{desc}" if desc else str(title)
                    if url:
                        line += f" {url}"
                    out.append(line[:200])
                return "\n".join(out)
        except Exception as exc:
            logger.debug("维基搜索失败: %s", exc)
        return "没搜到什么有用的东西"

    async def _tool_rate(self, base: str, quote: str) -> str:
        """open.er-api.com 免费汇率（无需 API key）。"""
        base = (base.strip().upper() or "CNY")[:8]
        quote = (quote.strip().upper() or "USD")[:8]
        async with self._session.get(f"https://open.er-api.com/v6/latest/{base}") as resp:
            d = await resp.json(content_type=None)
        rates = d.get("rates") or {}
        if quote not in rates:
            return f"查不到 {base} → {quote}"
        return f"1 {base} ≈ {rates[quote]} {quote}（汇率仅供参考）"

    # ---- 本地计算（白名单符号 + 受限 eval，不碰网络）----
    CALC_FUNCS = {
        "sqrt": math.sqrt, "abs": abs, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log2": math.log2, "log10": math.log10, "exp": math.exp,
        "round": round, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil,
        "pow": pow, "factorial": math.factorial,
    }

    def _tool_calc(self, expr: str) -> str:
        expr = expr.strip()
        if not expr:
            return "缺表达式"
        expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**").replace("π", "pi")
        if not re.fullmatch(r"[\d\.\+\-\*/%\(\),\sa-zA-Z]+", expr):
            return "只支持纯数学表达式"
        names = set(re.findall(r"[a-zA-Z_]+", expr))
        bad = names - set(self.CALC_FUNCS) - {"pi", "e", "tau"}
        if bad:
            return f"不认识的符号: {', '.join(sorted(bad))}"
        env = {n: getattr(math, n) for n in names if hasattr(math, n)}
        try:
            val = eval(expr, {"__builtins__": {}}, env)    # noqa: S307 已做白名单校验
        except Exception as exc:
            return f"算不出来: {exc}"
        if isinstance(val, float) and val.is_integer() and abs(val) < 1e15:
            val = int(val)
        return f"= {val}"

    async def _tool_read_url(self, url: str) -> str:
        """r.jina.ai 免费网页转文本。"""
        url = url.strip()
        if not url:
            return "缺链接"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        async with self._session.get("https://r.jina.ai/" + url, headers=headers) as resp:
            if resp.status != 200:
                return f"网页读取失败（{resp.status}）"
            text = await resp.text()
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if not text:
            return "网页内容是空的"
        return text[:600] + ("…" if len(text) > 600 else "")

    HOT_PLATFORMS = {"weibo": "微博", "douyin": "抖音"}

    async def _tool_hot_list(self, platform: str) -> str:
        """60s 开源聚合 API 的热搜榜（免费无 key）。"""
        platform = platform.strip().lower()
        key = next((k for k, v in self.HOT_PLATFORMS.items()
                    if k in platform or v in platform), "weibo")
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        async with self._session.get(f"https://60s.viki.moe/v2/{key}",
                                     headers=headers) as resp:
            d = await resp.json(content_type=None)
        items = (d.get("data") or [])[:5]
        if not items:
            return "暂时拿不到热搜"
        out = [f"【{self.HOT_PLATFORMS[key]}热榜】"]
        for i, it in enumerate(items, 1):
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            hot = it.get("hot_value")
            out.append(f"{i}. {title}" + (f"（热度 {hot}）" if hot else ""))
        return "\n".join(out)

    def _tool_holiday(self, date_str: str) -> str:
        """chinesecalendar 本地库：当天性质 + 下一个法定假期倒计时。"""
        if _cn_cal is None:
            return "节假日库未安装"
        d = None
        s = date_str.strip()
        if s:
            m = re.search(r"(\d{4})[-/年\.]?(\d{1,2})[-/月\.]?(\d{1,2})", s)
            if m:
                try:
                    d = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                except ValueError:
                    return "日期不对"
        if d is None:
            d = _date.today()
        try:
            workday = _cn_cal.is_workday(d)
            _, name = _cn_cal.get_holiday_detail(d)
        except NotImplementedError:
            return "节假日数据还没更新到这个年份"
        day_desc = d.strftime("%Y-%m-%d")
        today_part = f"{day_desc} 是" + (f"「{name}」假期" if name else ("工作日" if workday else "休息日（周末）"))
        # 找下一个法定假期
        nd = d
        for _ in range(500):
            nd += _timedelta(days=1)
            try:
                _, nm = _cn_cal.get_holiday_detail(nd)
            except NotImplementedError:
                return today_part + "；更远的假期数据还没更新"
            if nm:
                return f"{today_part}；下一个假期「{nm}」从 {nd.strftime('%m-%d')} 开始，还有 {(nd - d).days} 天"
        return today_part + "；近期没有法定假期"

    async def _tool_stock(self, symbol: str) -> str:
        """腾讯免费实时行情（A股/港股）。"""
        symbol = symbol.strip().lower()
        if not re.fullmatch(r"(sh|sz|bj|hk)\d{4,6}", symbol):
            return "代码格式不对，示例：sh600519（沪）、sz000001（深）、hk00700（港）"
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        async with self._session.get(f"https://qt.gtimg.cn/q={symbol}",
                                     headers=headers) as resp:
            raw = await resp.read()
        text = raw.decode("gbk", errors="replace")
        m = re.search(r'="([^"]*)"', text)
        if not m or "~" not in m.group(1):
            return "查不到这只股票"
        f = m.group(1).split("~")
        if len(f) < 35:
            return "行情数据不完整"
        return (f"{f[1]}（{symbol.upper()}）现价 {f[3]}，涨跌 {f[31]}（{f[32]}%），"
                f"今开 {f[5]}，最高 {f[33]}，最低 {f[34]}，昨收 {f[4]}")

    TRANSLATE_LANGS = {
        "中文": "zh-CN", "汉语": "zh-CN", "英语": "en", "英文": "en",
        "日文": "ja", "日语": "ja", "韩语": "ko", "韩文": "ko",
        "法文": "fr", "法语": "fr", "德文": "de", "德语": "de",
        "俄文": "ru", "俄语": "ru", "西班牙语": "es", "繁体": "zh-TW",
    }

    async def _tool_translate(self, text: str, target_language: str) -> str:
        """MyMemory 免费翻译（无 key，每天 5000 字/IP），按有无中文猜源语言。"""
        text = text.strip()
        if not text:
            return "缺要翻译的内容"
        tl = target_language.strip()
        tl = self.TRANSLATE_LANGS.get(tl, tl)
        if not re.fullmatch(r"[a-zA-Z\-]{2,8}", tl):
            tl = "zh-CN"
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
        src = "zh-CN" if has_cjk else "en"
        if (tl.startswith("zh") and has_cjk) or (tl == "en" and not has_cjk):
            return "这本来就是目标语言，不用翻"
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        async with self._session.get("https://api.mymemory.translated.net/get",
                                     params={"q": text[:500], "langpair": f"{src}|{tl}"},
                                     headers=headers) as resp:
            d = await resp.json(content_type=None)
        out = str(((d.get("responseData") or {}).get("translatedText")) or "").strip()
        if not out:
            return "翻译失败"
        return out[:500]

    async def _tool_quote(self) -> str:
        """一言 API（hitokoto.cn，免费无 key）。"""
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        async with self._session.get("https://v1.hitokoto.cn/", headers=headers) as resp:
            d = await resp.json(content_type=None)
        q = str(d.get("hitokoto") or "").strip()
        if not q:
            return "今天没拿到句子"
        frm = str(d.get("from") or "").strip()
        return f"“{q}”" + (f" —— {frm}" if frm else "")

    async def _tool_ip_location(self, ip: str) -> str:
        """ip-api.com 免费归属地查询（HTTP）。"""
        ip = ip.strip()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            return "IP 格式不对，要 IPv4，如 1.2.3.4"
        headers = {"User-Agent": "boxim-group-bot/1.0 (https://github.com/bbGJ-email/boxim-group-bot)"}
        async with self._session.get(f"http://ip-api.com/json/{ip}",
                                     params={"lang": "zh-CN"}, headers=headers) as resp:
            d = await resp.json(content_type=None)
        if d.get("status") != "success":
            return "查不到这个 IP"
        return (f"{ip}：{d.get('country') or ''}{d.get('regionName') or ''}{d.get('city') or ''}"
                + (f"（{d.get('isp')}）" if d.get("isp") else ""))

    async def summarize_pending(self, group_id: int) -> int:
        """把待整理消息交给 AI 汇总进记忆池，返回新增条数。"""
        pending = self.memory.pending_messages(group_id)
        if not pending:
            return 0
        await self._ensure_session()
        lines = []
        for m in pending[-60:]:
            hm = datetime.fromtimestamp(m["t"]).strftime("%H:%M")
            lines.append(f"{hm} {m['u']}: {m['c']}")
        payload = {
            "model": self.config.ai_model_id,
            "messages": [
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": "\n".join(lines)},
            ],
            "stream": False,
            "max_tokens": 800,
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {self.config.ai_api_key}", "Content-Type": "application/json"}
        try:
            async with self._session.post(self.config.ai_api_url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"AI API {resp.status}")
                text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        except Exception as exc:
            logger.warning("记忆整理失败: %s", exc)
            return 0
        entries = [ln.strip("-• ").strip() for ln in text.splitlines() if ln.strip()]
        entries = [e for e in entries if e and e != "无"]
        if entries:
            self.memory.add_memories(group_id, entries)
        self.memory.mark_summarized(group_id)
        return len(entries)


# ---------------- Web 控制台 ----------------
WEB_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>盒子IM机器人控制台</title>
<style>
:root{--bg:#0f1419;--panel:#1a2129;--panel2:#222b35;--text:#d8e0e8;--muted:#7d8b99;--acc:#4da3ff;--ok:#3fbf7f}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:10px;padding:10px 16px;background:var(--panel);border-bottom:1px solid #2a3440}
header b{font-size:15px}
header .spacer{flex:1}
header span{color:var(--muted);font-size:12px}
#botbtn{background:var(--panel2);border:1px solid #2a3440;color:var(--text);border-radius:6px;padding:5px 14px;cursor:pointer;font-size:13px}
#botbtn:hover{border-color:var(--acc)}
#botbtn.off{color:var(--ok);border-color:var(--ok)}
main{flex:1;display:flex;min-height:0}
aside{width:240px;background:var(--panel);border-right:1px solid #2a3440;display:flex;flex-direction:column}
.tabs{display:flex}
.tabs button{flex:1;background:none;border:none;color:var(--muted);padding:10px;cursor:pointer;font-size:13px}
.tabs button.on{color:var(--acc);border-bottom:2px solid var(--acc)}
#chatlist{flex:1;overflow-y:auto}
#chatlist div{padding:10px 14px;cursor:pointer;border-bottom:1px solid #222b35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#chatlist div:hover{background:var(--panel2)}
#chatlist div.on{background:var(--panel2);color:var(--acc)}
section{flex:1;display:flex;flex-direction:column;min-width:0}
#title{padding:10px 16px;background:var(--panel);border-bottom:1px solid #2a3440;color:var(--muted);font-size:13px}
#msgs{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.m{max-width:70%;padding:8px 12px;border-radius:10px;background:var(--panel2);align-self:flex-start;white-space:pre-wrap;word-break:break-word}
.m .meta{color:var(--muted);font-size:11px;margin-bottom:2px}
.m.self{align-self:flex-end;background:#1f3a55}
.m.del{opacity:.45;text-decoration:line-through}
#more{margin:4px auto;color:var(--acc);cursor:pointer;font-size:12px}
footer{display:flex;gap:8px;padding:10px 14px;background:var(--panel);border-top:1px solid #2a3440}
footer input{flex:1;background:var(--panel2);border:1px solid #2a3440;color:var(--text);border-radius:8px;padding:9px 12px;outline:none}
footer button{background:var(--acc);border:none;color:#fff;border-radius:8px;padding:0 20px;cursor:pointer}
#hint{color:var(--muted);font-size:11px;padding:4px 14px 8px;background:var(--panel)}
</style>
</head>
<body>
<header><b>🤖 机器人控制台</b><span id="st"></span><div class="spacer"></div>__SIGNATURE_HTML__<button id="botbtn" onclick="toggleBot()">⏹ 停止机器人</button><span>Web 控制台</span></header>
<main>
<aside>
<div class="tabs"><button class="on" data-t="group">群聊</button><button data-t="private">私聊</button></div>
<div id="chatlist"></div>
</aside>
<section>
<div id="title">选择左侧会话</div>
<div id="msgs"><div id="more" style="display:none" onclick="loadMore()">↑ 加载更早</div></div>
<footer>
<input id="input" placeholder="输入消息，Enter 发送（机器人身份发出）" disabled>
<button id="sendbtn" disabled>发送</button>
</footer>
<div id="hint">手动发送以机器人账号发出，不会触发审核等机器人逻辑。</div>
</section>
</main>
<script>
const T=new URLSearchParams(location.search).get('token')||'';
let cur=null, tab='group', oldestSeq=null, timer=null;
async function api(path, opt){const r=await fetch(path+(path.includes('?')?'&':'?')+'token='+T,opt);if(r.status===401){document.body.innerHTML='<p style="padding:30px">token 无效，请用 ?token=xxx 访问</p>';throw 0}return r.json()}
async function loadChats(){const d=await api('/api/chats');renderList(d[tab+'s']||[])}
function renderList(items){const el=document.getElementById('chatlist');el.innerHTML='';items.forEach(it=>{const d=document.createElement('div');d.textContent=it.name;d.onclick=()=>{document.querySelectorAll('#chatlist div').forEach(x=>x.classList.remove('on'));d.classList.add('on');cur=it;document.getElementById('title').textContent=it.name;document.getElementById('input').disabled=false;document.getElementById('sendbtn').disabled=false;document.getElementById('msgs').innerHTML='<div id=more style=display:none onclick=loadMore()>↑ 加载更早</div>';oldestSeq=null;loadMsgs();if(timer)clearInterval(timer);timer=setInterval(()=>loadMsgs(true),4000)};el.appendChild(d)})}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));b.classList.add('on');tab=b.dataset.t;cur=null;document.getElementById('title').textContent='选择左侧会话';document.getElementById('input').disabled=true;document.getElementById('sendbtn').disabled=true;loadChats()});
async function loadMsgs(auto){if(!cur)return;let u='/api/messages?type='+tab+'&id='+cur.id;if(oldestSeq)u+='&before='+oldestSeq;const d=await api(u);const ms=d.messages||[];if(!ms.length&&!auto)return;if(ms.length){oldestSeq=ms[0].seqNo||ms[0].id}const box=document.getElementById('msgs');const more=document.getElementById('more');const stick=box.scrollHeight-box.scrollTop-box.clientHeight<60||!auto;ms.forEach(m=>{if(document.getElementById('mid'+(m.id||m.seqNo)))return;const dv=document.createElement('div');dv.className='m'+(m.self?' self':'')+(m.deleted?' del':'');dv.id='mid'+(m.id||m.seqNo);dv.innerHTML='<div class=meta>'+esc(m.nick)+' · '+esc(m.time)+'</div>'+esc(m.text);box.insertBefore(dv,more)});more.style.display=ms.length>=50?'block':'none';if(stick&&!auto)box.scrollTop=box.scrollHeight;else if(auto)box.scrollTop=box.scrollHeight}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML}
async function loadMore(){await loadMsgs(false)}
async function send(){if(!cur)return;const inp=document.getElementById('input');const c=inp.value.trim();if(!c)return;inp.value='';await api('/api/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:tab,id:cur.id,content:c})});setTimeout(()=>loadMsgs(true),600)}
document.getElementById('sendbtn').onclick=send;
document.getElementById('input').addEventListener('keydown',e=>{if(e.key==='Enter')send()});
async function toggleBot(){const b=document.getElementById('botbtn');const stop=b.dataset.running==='1';if(!confirm(stop?'确定停止机器人？停止后将完全断开连接，不再接收和回复任何消息。':'确定启动机器人？将重新连接并恢复消息处理。'))return;await api('/api/bot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:stop?'stop':'start'})});setTimeout(refreshBot,600)}
async function refreshBot(){try{const d=await api('/api/status');document.getElementById('st').textContent=(d.running?'运行中':'已停止')+' · '+d.uptime+' · 已检查 '+d.checked+' · AI对话 '+d.chats;const b=document.getElementById('botbtn');b.dataset.running=d.running?'1':'0';b.textContent=d.running?'⏹ 停止机器人':'▶ 启动机器人';b.classList.toggle('off',!d.running)}catch(e){}}
(async()=>{refreshBot();setInterval(refreshBot,8000)})();
loadChats();
</script>
</body>
</html>"""


class WebConsole:
    """机器人账号 Web 控制台：查看全部会话记录 + 手动发消息。"""

    def __init__(self, config, bot):
        self.config = config
        self.bot = bot
        self.port = int(os.environ.get("WEB_PORT", "8901"))
        self.token = os.environ.get("WEB_TOKEN", "")
        if not self.token:
            self.token = os.urandom(8).hex()
            try:
                with open("web_token.txt", "w") as f:
                    f.write(self.token)
                os.chmod("web_token.txt", 0o600)
            except Exception:
                pass
            logger.warning("未设置 WEB_TOKEN，已自动生成并写入 web_token.txt: %s", self.token)
        self.runner = None

    async def start(self):
        from aiohttp import web
        app = web.Application(middlewares=[self._auth_mw()])
        app.router.add_get("/", self._index)
        app.router.add_get("/api/chats", self._chats)
        app.router.add_get("/api/messages", self._messages)
        app.router.add_post("/api/send", self._send)
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/bot", self._bot_toggle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        logger.info("Web 控制台已启动: http://0.0.0.0:%s/?token=%s", self.port, self.token)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    def _auth_mw(self):
        from aiohttp import web

        @web.middleware
        async def mw(request, handler):
            if request.path != "/":
                token = request.query.get("token") or request.headers.get("X-Token", "")
                if token != self.token:
                    return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)
        return mw

    async def _index(self, request):
        from aiohttp import web
        sig_html = ""
        if ACCOUNT_SIGNATURE:
            sig_html = f"<span>{ACCOUNT_SIGNATURE}</span>"
        return web.Response(text=WEB_HTML.replace("__SIGNATURE_HTML__", sig_html),
                            content_type="text/html")

    async def _chats(self, request):
        from aiohttp import web
        im = self.bot.im
        groups = []
        try:
            for g in await im.aget_groups():
                groups.append({"id": g.id if hasattr(g, "id") else g.get("id"),
                               "name": (g.name if hasattr(g, "name") else g.get("name")) or str(getattr(g, "id", ""))})
        except Exception as exc:
            logger.warning("获取群列表失败: %s", exc)
        friends = []
        try:
            for f in await im.aget_friends():
                fid = getattr(f, "id", None) or (f.get("id") if isinstance(f, dict) else None)
                fnick = getattr(f, "nick_name", None) or (f.get("nickName") if isinstance(f, dict) else None) or str(fid)
                if fid:
                    friends.append({"id": fid, "name": fnick})
        except Exception as exc:
            logger.warning("获取好友列表失败: %s", exc)
        return web.json_response({"groups": groups, "privates": friends})

    async def _messages(self, request):
        from aiohttp import web
        im = self.bot.im
        mtype = request.query.get("type", "group")
        cid = int(request.query.get("id") or 0)
        before = request.query.get("before")
        try:
            if mtype == "group":
                latest = getattr(self.bot, "_last_group_seq", {}).get(cid)
                if before:
                    kw = {"group_id": cid, "min_seq_no": max(int(before) - 100, 0),
                          "max_seq_no": max(int(before) - 1, 1)}
                    msgs = await im.aget_group_message_history(**kw) or []
                elif latest:
                    kw = {"group_id": cid, "min_seq_no": max(latest - 99, 0), "max_seq_no": latest}
                    msgs = await im.aget_group_message_history(**kw) or []
                else:
                    msgs = await self._latest_group_page(im, cid)
            else:
                latest = getattr(self.bot, "_last_private_seq", {}).get(cid)
                if before:
                    kw = {"friend_id": cid, "min_seq_no": max(int(before) - 100, 0),
                          "max_seq_no": max(int(before) - 1, 1)}
                elif latest:
                    kw = {"friend_id": cid, "min_seq_no": max(latest - 99, 0), "max_seq_no": latest}
                else:
                    kw = {"friend_id": cid}
                msgs = await im.aget_private_message_history(**kw) or []
        except Exception as exc:
            return web.json_response({"messages": [], "error": str(exc)})

        bot_id = self.bot.bot_id
        out = []
        for m in msgs:
            d = m if isinstance(m, dict) else m.__dict__
            t = d.get("sendTime")
            try:
                tstr = datetime.fromtimestamp(t / 1000).strftime("%m-%d %H:%M") if t else ""
            except Exception:
                tstr = ""
            sender = d.get("sendId")
            mtype = d.get("type", 0)
            text = str(d.get("content") or "")[:2000]
            if mtype == 1:
                text = "[图片] " + text
            elif mtype == 3:
                text = "[语音] " + text
            elif mtype == 4:
                text = "[视频] " + text
            elif mtype == 7:
                text = "[表情包]"
            out.append({
                "id": d.get("id"),
                "seqNo": d.get("seqNo") or d.get("id"),
                "nick": d.get("sendNickName") or str(sender),
                "text": text,
                "time": tstr,
                "self": sender == bot_id,
                "deleted": bool(d.get("deleted")),
            })
        return web.json_response({"messages": out})

    async def _latest_group_page(self, im, group_id: int) -> list:
        """向前翻页直到拿最新一页（最多 10 页）。"""
        page = await im.aget_group_message_history(group_id=group_id, min_seq_no=0, max_seq_no=9999999) or []
        for _ in range(10):
            if len(page) < 100:
                break
            last = page[-1]
            d = last if isinstance(last, dict) else last.__dict__
            seq = d.get("seqNo") or 0
            nxt = await im.aget_group_message_history(group_id=group_id, min_seq_no=seq + 1, max_seq_no=9999999) or []
            if not nxt:
                break
            page = nxt
        return page

    async def _send(self, request):
        from aiohttp import web
        body = await request.json()
        mtype = body.get("type", "group")
        cid = int(body.get("id") or 0)
        content = str(body.get("content") or "").strip()
        if not cid or not content:
            return web.json_response({"ok": False, "error": "参数缺失"}, status=400)
        try:
            if mtype == "group":
                await self.bot.im.asend_group_raw(cid, content, MessageType.TEXT if MessageType else 0)
            else:
                await self.bot.im.asend_text(cid, content)
            return web.json_response({"ok": True})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _bot_toggle(self, request):
        """机器人启停：停止完全断开 WebSocket，控制台始终可用。"""
        from aiohttp import web
        body = await request.json()
        action = str(body.get("action") or "")
        try:
            if action == "stop":
                await self.bot.stop_bot()
            elif action == "start":
                await self.bot.start_bot()
            else:
                return web.json_response({"ok": False, "error": "未知操作"}, status=400)
            return web.json_response({"ok": True, "running": getattr(self.bot, "bot_running", False)})
        except Exception as exc:
            logger.warning("机器人启停失败: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _status(self, request):
        from aiohttp import web
        s = self.bot.stats
        uptime = int(time.time() - self.bot.start_time)
        h, m = uptime // 3600, uptime % 3600 // 60
        ws_ok = False
        try:
            ws_ok = bool(getattr(self.bot.im._ws, "is_connected", False))    # property
        except Exception:
            pass
        return web.json_response({
            "running": getattr(self.bot, "bot_running", False),
            "ws_connected": ws_ok,
            "uptime": f"{h}小时{m}分",
            "checked": s.get("checked_messages", 0),
            "chats": getattr(self.bot.human_ai, "chat_count", 0) if getattr(self.bot, "human_ai", None) else 0,
            "memory": self.bot.memory.stats() if getattr(self.bot, "memory", None) else {},
        })
