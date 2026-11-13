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
import os
import random
import re
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("features")

try:
    from boxim.util.core.types import MessageType
except ImportError:    # pragma: no cover
    MessageType = None


# ---------------- 制作声明 ----------------
ACCOUNT_SIGNATURE = os.environ.get(
    "BOT_ACCOUNT_SIGNATURE",
    "",
)
MSG_SIGNATURE = os.environ.get(
    "BOT_MSG_SIGNATURE",
    "",
)


def append_signature(text: str) -> str:
    """给消息追加制作声明（已含声明的不重复加）。"""
    if not text or not MSG_SIGNATURE:
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
        g = self._group(group_id)
        msgs = g["today"][-limit:]
        if not msgs:
            return "（今天群里还没有聊天记录）"
        lines = []
        for m in msgs:
            hm = datetime.fromtimestamp(m["t"]).strftime("%H:%M")
            lines.append(f"{hm} {m['u']}: {m['c']}")
        return "\n".join(lines)

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
    """拟人化群聊 AI，带记忆检索 Agent 工具。"""

    TOOLS = [{
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
    }]

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
        today = self.memory.today_text(group_id)
        hits = self.memory.search(group_id, question)
        parts = [PERSONA_PROMPT, ANTI_AI_PROMPT]
        parts.append(f"【今天的群聊记录（最新在最后）】\n{today}")
        if hits:
            parts.append("【长期记忆里相关的回忆】\n" + "\n".join(hits))
        parts.append(
            "你可以调用工具 search_memory 查更早的记忆；"
            "但大部分时候直接接话就行，不必每次都查。"
        )
        return "\n\n".join(parts)

    def _build_messages(self, group_id: int, asker: str, question: str,
                        extra_memory: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        system = self._build_system(group_id, question)
        if extra_memory:
            system += "\n【补充记忆检索结果】\n" + "\n".join(extra_memory)
        msgs = [{"role": "system", "content": system}]
        for user, text in self._history.get(group_id, deque()):
            role = "assistant" if user == "我" else "user"
            content = text if role == "assistant" else f"{user} 说：{text}"
            msgs.append({"role": role, "content": content})
        msgs.append({"role": "user", "content": f"{asker} 对你说：{question}"})
        return msgs

    async def _chat_request(self, messages: List[Dict[str, Any]], use_tools: bool) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.config.ai_model_id,
            "messages": messages,
            "stream": False,
            "max_tokens": 200,
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

    async def chat(self, group_id: int, asker: str, question: str) -> Optional[str]:
        """生成拟人化回复，带 Agent 记忆检索。返回最终文本。"""
        await self._ensure_session()
        now = time.time()
        if now - self._cooldown.get(group_id, 0) < 5:
            return None
        self._cooldown[group_id] = now

        messages = self._build_messages(group_id, asker, question)
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
                if fn.get("name") == "search_memory":
                    args = {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        pass
                    query = str(args.get("query") or question)
                    hits = self.memory.search(group_id, query)
                    result = "\n".join(hits) if hits else "没有查到相关记忆"
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
            text = str((((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or "").strip()
        except Exception:
            text = ""
        if not text:
            return None

        # 兜底去 AI 味：剥掉 markdown 符号与开头自称
        text = re.sub(r"[*#`>|]", "", text)
        text = re.sub(r"^(?:作为(?:一个)?(?:AI|人工智能|模型|助手)[，,]?)", "", text)
        text = text.strip().strip("“”\"'")[:200]
        self.chat_count += 1
        self.remember(group_id, asker, question)
        self.remember(group_id, "我", text, is_bot=True)
        return text

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
            "max_tokens": 400,
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
<header><b>🤖 机器人控制台</b><span id="st"></span><div class="spacer"></div><span><a style="color:var(--acc)" href="https://github.com/bbGJ-email/boxim-group-bot" target="_blank">源码开源</a></span></header>
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
(async()=>{try{const d=await api('/api/status');document.getElementById('st').textContent='运行 '+d.uptime+' · 已检查 '+d.checked+' · AI对话 '+d.chats}catch(e){}})();
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
        return web.Response(text=WEB_HTML, content_type="text/html")

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
            out.append({
                "id": d.get("id"),
                "seqNo": d.get("seqNo") or d.get("id"),
                "nick": d.get("sendNickName") or str(sender),
                "text": str(d.get("content") or "")[:2000],
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

    async def _status(self, request):
        from aiohttp import web
        s = self.bot.stats
        uptime = int(time.time() - self.bot.start_time)
        h, m = uptime // 3600, uptime % 3600 // 60
        return web.json_response({
            "uptime": f"{h}小时{m}分",
            "checked": s.get("checked_messages", 0),
            "chats": getattr(self.bot.human_ai, "chat_count", 0) if getattr(self.bot, "human_ai", None) else 0,
            "memory": self.bot.memory.stats() if getattr(self.bot, "memory", None) else {},
        })
