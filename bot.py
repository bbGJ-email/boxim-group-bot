# -*- coding: utf-8 -*-
"""
盒子 IM 群管理机器人
- 敏感内容审核（敏感词 API + AI 复检）
- 违规记录与跟踪
- 自动撤回 / 禁言 / 定时解禁
- 自动举报
- 管理员命令
- /punch 每日签到
"""
import asyncio
import json
import logging
import os
import random
import re
import signal
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from config import BotConfig
from features import (GroupMemory, HumanAI, WebConsole, append_signature,
                      ACCOUNT_SIGNATURE)

try:
    import boxim
    from boxim import BoxIM
    from boxim.util.core.types import MessageType
except ImportError:
    print("缺少 boxim-sdk，请先: pip install boxim-sdk")
    raise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("bot")
logging.getLogger("boxim").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# 敏感词检测器
# --------------------------------------------------------------------------
class SensitiveWordDetector:
    """调远程 API 检测文本，根据返回的 * 定位敏感词。"""

    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self.config.sensitive_api_token:
                headers["Authorization"] = f"Bearer {self.config.sensitive_api_token}"
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10), headers=headers
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _extract_masked(self, data: Any) -> Optional[str]:
        """从 API 响应里挖出掩码文本，兼容几种常见返回格式。"""
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("result", "text", "masked", "data", "content", "censored"):
                val = data.get(key)
                if isinstance(val, str):
                    return val
                if isinstance(val, dict):
                    return self._extract_masked(val)
        if isinstance(data, list) and data and isinstance(data[0], str):
            return data[0]
        return None

    @staticmethod
    def find_sensitive_positions(original: str, masked: str) -> List[Tuple[int, int]]:
        """对比原文和掩码文本，返回敏感区间列表 [(start, end)]。

        前提是掩码是等长替换（一个字符换一个 *）。长度不一致时退化为
        直接找掩码里的 * 连续段，词内容拿不到就记为掩码段本身。
        """
        positions: List[Tuple[int, int]] = []
        if len(original) == len(masked):
            start = None
            for i, ch in enumerate(masked):
                if ch == "*" and original[i] != "*":
                    if start is None:
                        start = i
                else:
                    if start is not None:
                        positions.append((start, i))
                        start = None
            if start is not None:
                positions.append((start, len(masked)))
        else:
            # 等长假设不成立，只能按 * 的连续段记录，词内容未知
            for m in re.finditer(r"\*+", masked):
                positions.append((m.start(), m.end()))
        return positions

    async def check(self, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "is_sensitive": False,
            "sensitive_words": [],
            "positions": [],
            "masked_text": text,
            "hit_types": [],
            "error": None,
        }
        await self._ensure_session()
        try:
            # 云智 API：GET token+msg，format=json 时返回 {text: 掩码文本, hits: 类型列表}
            params = {"token": self.config.sensitive_api_token, "msg": text, "format": "json"}
            async with self._session.get(self.config.sensitive_api_url, params=params) as resp:
                if resp.status != 200:
                    result["error"] = f"API 返回 {resp.status}"
                    return result
                raw = await resp.text()
        except Exception as exc:
            result["error"] = f"API 请求失败: {exc}"
            return result

        masked = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            masked = self._extract_masked(data)
            hits = data.get("hits") or []
            result["hit_types"] = sorted({h.get("label", h.get("type", "")) for h in hits if isinstance(h, dict)} - {""})
        else:
            masked = raw.strip() if raw.strip() else None
        if masked is None:
            result["error"] = "无法从响应中解析掩码文本"
            return result
        result["masked_text"] = masked
        positions = self.find_sensitive_positions(text, masked)
        result["positions"] = positions
        if positions:
            words = []
            for start, end in positions:
                word = text[start:end]
                words.append(word if word and set(word) != {"*"} else masked[start:end])
            result["sensitive_words"] = words
            result["is_sensitive"] = True
        return result


# --------------------------------------------------------------------------
# AI 复检器（OpenAI 协议，流式）
# --------------------------------------------------------------------------
class AIContentChecker:
    """OpenAI 协议流式复检，让模型只回答 是/否。"""

    SYSTEM_PROMPT = (
        "你是内容安全审核员。判断用户消息是否包含敏感、违规、色情、辱骂、"
        "广告刷屏等不良内容。只回答一个字：敏感回答「是」，安全回答「否」，"
        "不要输出任何其他内容。"
    )

    def __init__(self, config: BotConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._consecutive_errors = 0
        self._disabled_until = 0.0
        # 运行统计
        self.check_count = 0
        self.confirmed_count = 0      # AI 确认敏感
        self.overturned_count = 0     # AI 推翻（判安全）
        self.error_count = 0

    @property
    def available(self) -> bool:
        return time.time() >= self._disabled_until

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.ai_timeout)
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @staticmethod
    def parse_answer(text: str) -> Tuple[bool, float]:
        """解析 AI 回答 → (是否敏感, 置信度)。"""
        t = text.strip()
        if not t:
            return False, 0.0
        # 干净回答「是/否」算高置信，含糊回答算低置信
        if t in ("是", "否"):
            return t == "是", 0.9
        lowered = t.lower()
        if "是" in t or "yes" in lowered or "敏感" in t:
            if "否" in t or "no" in lowered:
                return False, 0.4   # 又是否又是，含糊
            return True, 0.7
        if "否" in t or "no" in lowered or "安全" in t:
            return False, 0.7
        return False, 0.3           # 完全没答上来，按安全放行但低置信

    async def check(self, text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "is_sensitive": False,
            "confidence": 0.0,
            "reason": "",
            "raw_response": "",
            "success": False,
        }
        if not self.available:
            result["reason"] = "AI 检查器暂时禁用中（连续错误）"
            return result

        await self._ensure_session()
        payload = {
            "model": self.config.ai_model_id,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "stream": True,
            "temperature": 0,
            "max_tokens": 8,
        }
        headers = {
            "Authorization": f"Bearer {self.config.ai_api_key}",
            "Content-Type": "application/json",
        }
        self.check_count += 1
        buffer = ""
        try:
            async with self._session.post(
                self.config.ai_api_url, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {body[:120]}")
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        buffer += delta
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
            self._consecutive_errors = 0
            self.error_count = self.error_count  # 错误数只在失败时累计
            result["raw_response"] = buffer
            is_sensitive, confidence = self.parse_answer(buffer)
            result["is_sensitive"] = is_sensitive
            result["confidence"] = confidence
            result["success"] = True
            if is_sensitive:
                self.confirmed_count += 1
                result["reason"] = f"AI 判定敏感（置信度 {confidence:.0%}）"
            else:
                self.overturned_count += 1
                result["reason"] = f"AI 判定安全（置信度 {confidence:.0%}）"
        except Exception as exc:
            self.error_count += 1
            self._consecutive_errors += 1
            result["reason"] = f"AI 请求失败: {exc}"
            if self._consecutive_errors >= self.config.ai_max_consecutive_errors:
                self._disabled_until = time.time() + self.config.ai_error_disable_seconds
                self._consecutive_errors = 0
                logger.warning("AI 连续出错，禁用 %s 秒", self.config.ai_error_disable_seconds)
        return result


# --------------------------------------------------------------------------
# 违规记录
# --------------------------------------------------------------------------
class ViolationTracker:
    """按 用户+群 记录违规，过期自动清理。"""

    def __init__(self, config: BotConfig):
        self.config = config
        # {(user_id, group_id): [violation, ...]}
        self._records: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
        self._file = config.violation_data_file
        self._load()

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, encoding="utf-8") as f:
                raw = json.load(f)
            for key, records in raw.items():
                uid, gid = key.split(":")
                self._records[(int(uid), int(gid))] = records
            self.cleanup()   # 顺手清掉已过期的
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning("违规记录读取失败，使用空数据: %s", exc)

    def _save(self):
        try:
            data = {f"{uid}:{gid}": records for (uid, gid), records in self._records.items()}
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as exc:
            logger.error("违规记录保存失败: %s", exc)

    def add(
        self,
        user_id: int,
        group_id: int,
        original_message: str,
        detected_content: str,
        positions: List,
        ai_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        record = {
            "user_id": user_id,
            "group_id": group_id,
            "original_message": original_message,
            "detected_content": detected_content,
            "positions": positions,
            "time": time.time(),
            "ai_result": ai_result,
        }
        self._records[(user_id, group_id)].append(record)
        self._save()
        return record

    def count(self, user_id: int, group_id: int, hours: Optional[float] = None) -> int:
        window = hours * 3600 if hours else self.config.violation_expire_hours * 3600
        cutoff = time.time() - window
        return sum(
            1 for r in self._records.get((user_id, group_id), []) if r["time"] >= cutoff
        )

    def get_user_records(self, user_id: int, group_id: int, limit: int = 5) -> List[Dict[str, Any]]:
        records = self._records.get((user_id, group_id), [])
        return sorted(records, key=lambda r: r["time"], reverse=True)[:limit]

    def group_stats(self, group_id: int) -> Dict[str, Any]:
        cutoff = time.time() - self.config.violation_expire_hours * 3600
        user_counts: Dict[int, int] = defaultdict(int)
        total = 0
        for (uid, gid), records in self._records.items():
            if gid != group_id:
                continue
            recent = [r for r in records if r["time"] >= cutoff]
            if recent:
                user_counts[uid] += len(recent)
                total += len(recent)
        return {"total": total, "user_counts": dict(user_counts)}

    def cleanup(self, group_id: Optional[int] = None) -> int:
        """清理过期记录，返回清掉的条数。group_id 为 None 时清全部。"""
        cutoff = time.time() - self.config.violation_expire_hours * 3600
        removed = 0
        keys = (
            [k for k in self._records if k[1] == group_id]
            if group_id is not None
            else list(self._records.keys())
        )
        for key in keys:
            fresh = [r for r in self._records[key] if r["time"] >= cutoff]
            removed += len(self._records[key]) - len(fresh)
            if fresh:
                self._records[key] = fresh
            else:
                del self._records[key]
        if removed:
            self._save()
        return removed

    def clear_group(self, group_id: int) -> int:
        removed = 0
        for key in [k for k in self._records if k[1] == group_id]:
            removed += len(self._records[key])
            del self._records[key]
        if removed:
            self._save()
        return removed

    @staticmethod
    def mask_content(content: str, positions: List) -> str:
        """展示记录时把敏感位置打码。"""
        chars = list(content)
        for pos in positions:
            if isinstance(pos, (tuple, list)) and len(pos) == 2:
                start, end = int(pos[0]), int(pos[1])
                for i in range(start, min(end, len(chars))):
                    chars[i] = "*"
        return "".join(chars)


# --------------------------------------------------------------------------
# 自动举报
# --------------------------------------------------------------------------
class ComplaintManager:
    """达到阈值自动举报，带 1 小时去重和统计。"""

    def __init__(self, config: BotConfig):
        self.config = config
        # {(user_id, group_id): last_complaint_ts}
        self._history: Dict[Tuple[int, int], float] = {}
        self.total = 0
        self.success = 0
        self.failed = 0
        self._events: List[Dict[str, Any]] = []   # (ts, user_id, group_id, ok)

    def should_complaint(self, user_id: int, group_id: int, violation_count: int) -> Tuple[bool, str]:
        if not self.config.auto_complaint_enabled:
            return False, "自动举报未开启"
        if violation_count < self.config.max_violations_before_complaint:
            return False, "未达举报阈值"
        last = self._history.get((user_id, group_id))
        if last and time.time() - last < self.config.complaint_interval_hours * 3600:
            return False, "1 小时内已举报过"
        return True, ""

    def build_content(
        self,
        user_id: int,
        user_name: str,
        violations: List[Dict[str, Any]],
        violation_count: int,
    ) -> str:
        lines = [
            f"举报类型: {self.config.complaint_reason}",
            f"目标用户: {user_name} (ID: {user_id})",
            f"累计违规: {violation_count} 次",
        ]
        if violations:
            last_time = datetime.fromtimestamp(violations[0]["time"]).strftime("%m-%d %H:%M")
            lines.append(f"最近违规时间: {last_time}")
            lines.append("违规样本:")
            for v in violations[: self.config.complaint_max_samples]:
                masked = ViolationTracker.mask_content(v["detected_content"], v["positions"])
                ts = datetime.fromtimestamp(v["time"]).strftime("%m-%d %H:%M")
                lines.append(f"- [{ts}] {masked[:60]}")
        content = "\n".join(lines)
        if len(content) > self.config.complaint_content_limit:
            content = content[: self.config.complaint_content_limit - 3] + "..."
        return content

    def record_event(self, user_id: int, group_id: int, ok: bool, content: str):
        self.total += 1
        if ok:
            self.success += 1
        else:
            self.failed += 1
        if ok:
            self._history[(user_id, group_id)] = time.time()
        self._events.append(
            {"time": time.time(), "user_id": user_id, "group_id": group_id, "ok": ok, "content": content}
        )
        # 只保留最近 200 条，防内存膨胀
        if len(self._events) > 200:
            self._events = self._events[-200:]

    def has_complained(self, user_id: int, group_id: int) -> bool:
        return (user_id, group_id) in self._history

    def stats(self) -> Dict[str, Any]:
        now = time.time()
        day_count = sum(1 for e in self._events if now - e["time"] < 86400)
        rate = self.success / self.total * 100 if self.total else 0.0
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "rate": rate,
            "day_count": day_count,
        }

    def recent_records(self, group_id: Optional[int] = None, user_id: Optional[int] = None, limit: int = 10):
        records = self._events
        if group_id is not None:
            records = [e for e in records if e["group_id"] == group_id]
        if user_id is not None:
            records = [e for e in records if e["user_id"] == user_id]
        return sorted(records, key=lambda e: e["time"], reverse=True)[:limit]

    def complained_users(self) -> int:
        return len({uid for uid, _ in self._history})


# --------------------------------------------------------------------------
# 每日签到
# --------------------------------------------------------------------------
class PunchManager:
    """/punch 每日签到，JSON 文件持久化。"""

    def __init__(self, config: BotConfig):
        self.config = config
        self.enabled = config.punch_enabled
        self.file = config.punch_data_file
        self.data: Dict[str, Any] = {"users": {}, "daily": {}}
        self._load()

    def _load(self):
        if os.path.exists(self.file):
            try:
                with open(self.file, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("签到数据读取失败，使用空数据: %s", exc)

    def _save(self):
        try:
            with open(self.file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
        except OSError as exc:
            logger.error("签到数据保存失败: %s", exc)

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _yesterday() -> str:
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    def punch(self, user_id: int, user_name: str) -> Dict[str, Any]:
        today = self._today()
        users = self.data["users"]
        uid = str(user_id)
        info = users.setdefault(uid, {"streak": 0, "total": 0, "points": 0, "last": "", "name": user_name})
        info["name"] = user_name or info.get("name", str(user_id))

        if info.get("last") == today:
            return {"ok": False, "already": True, "info": dict(info)}

        # 连续签到判断
        if info.get("last") == self._yesterday():
            info["streak"] = info.get("streak", 0) + 1
        else:
            info["streak"] = 1
        info["total"] = info.get("total", 0) + 1
        info["last"] = today

        base = random.randint(self.config.punch_min_points, self.config.punch_max_points)
        bonus = min((info["streak"] - 1) * self.config.punch_streak_bonus, self.config.punch_streak_bonus_cap)
        points = base + bonus
        info["points"] = info.get("points", 0) + points

        daily = self.data["daily"].setdefault(today, [])
        if user_id not in daily:
            daily.append(user_id)
        order = daily.index(user_id) + 1
        self._save()
        return {"ok": True, "already": False, "info": dict(info), "points": points, "order": order}

    def leaderboard(self, limit: int = 10) -> List[Tuple[int, Dict[str, Any]]]:
        items = [(int(uid), info) for uid, info in self.data["users"].items()]
        items.sort(key=lambda x: (x[1].get("points", 0), x[1].get("streak", 0)), reverse=True)
        return items[:limit]

    def today_count(self) -> int:
        return len(self.data["daily"].get(self._today(), []))


# --------------------------------------------------------------------------
# 机器人主体
# --------------------------------------------------------------------------
class GroupModerationBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.im: Optional[BoxIM] = None
        self.bot_id = 0
        self.detector = SensitiveWordDetector(config)
        self.ai = AIContentChecker(config)
        self.violations = ViolationTracker(config)
        self.complaints = ComplaintManager(config)
        self.punch = PunchManager(config)

        self.unmute_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._admin_cache: Dict[int, Tuple[bool, float]] = {}   # group_id -> (is_admin, ts)
        self._runtime_punch_enabled = config.punch_enabled
        self._name_cache: Dict[str, Tuple[int, str]] = {}       # 名称解析缓存 u:/g: 前缀
        self._nick_cache: Dict[int, str] = {}                   # user_id -> 昵称
        self._flood: Dict[Tuple[int, int], deque] = defaultdict(deque)          # (group, user) -> [(ts, content)]
        self._flood_warn_ts: Dict[Tuple[int, int], float] = {}  # 上次刷屏警告时间
        self._last_group_seq: Dict[int, int] = {}               # 各群最新 seqNo（控制台翻页用）
        self._last_private_seq: Dict[int, int] = {}             # 各私聊最新 seqNo

        # 运行统计
        self.stats = {
            "checked_messages": 0,
            "group_messages": 0,
            "sensitive_detected": 0,
            "recalled": 0,
            "muted_users": 0,
            "unmuted_users": 0,
            "mute_commands": 0,
            "unmute_commands": 0,
            "flood_triggered": 0,
        }
        self.start_time = time.time()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._shutdown = asyncio.Event()

    # ---------------- 启动 ----------------
    async def start(self):
        self._print_config()
        self.im = await boxim.ainit(self.config.username, self.config.password, base_url=self.config.base_url)
        me = await self.im.aget_me()
        self.bot_id = me.get("id", 0) if isinstance(me, dict) else 0
        logger.info("登录成功，机器人 ID: %s", self.bot_id)

        # 制作声明：账号签名
        try:
            if (me.get("signature") or "") != ACCOUNT_SIGNATURE:
                await self.im.aupdate_profile(signature=ACCOUNT_SIGNATURE)
                logger.info("已更新账号签名为制作声明")
        except Exception as exc:
            logger.warning("设置账号签名失败: %s", exc)

        # 扩展功能：记忆池 / 拟人 AI / Web 控制台
        self.memory = GroupMemory(self.config.memory_data_file,
                                  self.config.memory_summarize_threshold)
        self.human_ai = HumanAI(self.config, self.im, self.memory)
        self.web = None
        if self.config.web_enabled:
            self.web = WebConsole(self.config, self)
            asyncio.create_task(self.web.start())

        # 消息中间件：统计群消息
        def stats_middleware(msg: dict, is_group: bool):
            if is_group and self.config.stats_group_messages:
                self.stats["group_messages"] += 1

        self.im.on_message(stats_middleware)
        self.im.on_message(self.handle_message)

        await self.im.start_listening()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._token_refresh_loop())
        asyncio.create_task(self._memory_loop())

        await self._shutdown.wait()
        await self.shutdown()

    def _print_config(self):
        cfg = self.config
        print("=" * 50)
        print("盒子 IM 群管理机器人 启动")
        print(f"  敏感词 API: {cfg.sensitive_api_url}")
        print(f"  AI API: {cfg.ai_api_url} (模型: {cfg.ai_model_id}, 复检: {'开' if cfg.ai_recheck_enabled else '关'})")
        print(f"  触发字符: {cfg.trigger_char}")
        print(f"  敏感处理方式: {cfg.sensitive_action}")
        print(f"  最大违规次数: {cfg.max_violations} / 举报阈值: {cfg.max_violations_before_complaint}")
        print(f"  违规记录过期: {cfg.violation_expire_hours} 小时")
        print(f"  禁言时长梯度: {cfg.mute_durations} 秒")
        print(f"  自动解禁: {'开' if cfg.auto_unmute_enabled else '关'}")
        print(f"  自动举报: {'开' if cfg.auto_complaint_enabled else '关'}")
        print(f"  刷屏检测: {'开' if cfg.flood_enabled else '关'}（{cfg.flood_window} 秒 {cfg.flood_max_messages} 条 / 重复 {cfg.flood_max_repeat} 次 → {cfg.flood_action}）")
        print(f"  每日签到: {'开' if cfg.punch_enabled else '关'}")
        print(f"  管理员: {cfg.admin_ids}")
        print("=" * 50)

    # ---------------- 消息入口 ----------------
    async def handle_message(self, msg: dict, is_group: bool):
        try:
            if not is_group:
                seq_p = int(msg.get("seqNo") or 0)
                peer = int(msg.get("sendId") or 0)
                if seq_p and peer:
                    self._last_private_seq[peer] = seq_p
                await self.handle_command(msg, is_group)
                return

            msg_type = msg.get("type", 0)
            sender_id = int(msg.get("sendId") or 0)
            group_id = int(msg.get("groupId") or 0)
            content = msg.get("content", "") or ""

            # 消息序号与记忆池（供控制台翻页 / AI 记忆）
            seq = int(msg.get("seqNo") or 0)
            if seq:
                self._last_group_seq[group_id] = seq
            if msg_type == 0 and content.strip() and sender_id != self.bot_id \
                    and not content.startswith(self.config.trigger_char):
                nick = str(msg.get("sendNickName") or sender_id)
                self.memory.add_message(group_id, nick, content)
                self.human_ai.remember(group_id, nick, content)

            # AI 拟人对话：@机器人 或 回复机器人（群内）
            if msg_type == 0 and content.strip() and sender_id != self.bot_id \
                    and self.config.ai_chat_enabled and group_id not in self.config.whitelist_groups \
                    and sender_id not in self.config.whitelist_users:
                quote = msg.get("quoteMessage") or {}
                at_me = isinstance(msg.get("atUserIds"), list) and self.bot_id in msg["atUserIds"]
                reply_me = isinstance(quote, dict) and quote.get("sendId") == self.bot_id
                lower = content.lower()
                name_hit = any(n and n.lower() in lower for n in self.config.ai_chat_names)
                if (at_me or reply_me or name_hit) and not re.search(r"\*{3,}", content):
                    await self._handle_ai_chat(msg, sender_id, group_id, content)
                    return

            # 命令优先（文本消息才解析）
            if msg_type == 0 and content.startswith(self.config.trigger_char):
                await self.handle_command(msg, is_group)
                return

            await self.moderate_group_message(msg, msg_type, sender_id, group_id, content)
        except Exception:
            logger.exception("处理消息出错")

    async def _handle_ai_chat(self, msg: dict, sender_id: int, group_id: int, content: str):
        """@机器人 / 回复机器人 → 拟人化 AI 对话。"""
        try:
            question = re.sub(r"^@\S+\s*", "", content, count=1).strip() or content
            nick = str(msg.get("sendNickName") or sender_id)
            reply = await self.human_ai.chat(group_id, nick, question)
            logger.info("AI 对话触发: 群=%s 问=%r → %r", group_id, question[:30], (reply or "")[:50])
            if not reply:
                return
            # 拟人打字延迟
            await asyncio.sleep(min(3.5, 0.8 + len(reply) * 0.04 + random.random()))
            await self.send_group(group_id, reply, at_users=[sender_id],
                                  quote_message_id=int(msg.get("id") or 0) or None)
        except Exception:
            logger.exception("AI 对话出错")

    async def _memory_loop(self):
        """定期把各群未整理消息交给 AI 归纳进记忆池。"""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                return
            try:
                for gid in self.memory.all_groups():
                    if self.memory.pending_count(gid) >= self.memory.summarize_threshold:
                        n = await self.human_ai.summarize_pending(gid)
                        if n:
                            logger.info("群 %s 记忆池新增 %s 条", gid, n)
            except Exception:
                logger.exception("记忆整理任务出错")

    # ---------------- 审核 ----------------
    def _should_skip(self, sender_id: int, group_id: int) -> bool:
        cfg = self.config
        if sender_id == self.bot_id:
            return True
        if group_id in cfg.whitelist_groups:
            return True
        if sender_id in cfg.whitelist_users:
            return True
        if sender_id in cfg.ignore_user_ids:
            return True
        if sender_id in cfg.admin_ids:
            return True
        return False

    async def _check_flood(self, msg: dict, sender_id: int, group_id: int, content: str) -> bool:
        """刷屏检测：窗口内消息过多或相同内容连发。返回 True 表示已处理。"""
        cfg = self.config
        key = (group_id, sender_id)
        now = time.time()
        dq = self._flood[key]
        dq.append((now, content))
        while dq and now - dq[0][0] > cfg.flood_window:
            dq.popleft()

        recent = [c for t, c in dq if now - t <= cfg.flood_window]
        repeat = sum(1 for c in recent if c == content)
        if len(recent) < cfg.flood_max_messages and repeat < cfg.flood_max_repeat:
            return False

        # 警告冷却，避免每次消息都告警
        last = self._flood_warn_ts.get(key, 0)
        if now - last < cfg.flood_warn_cooldown:
            return True
        self._flood_warn_ts[key] = now
        dq.clear()
        self.stats["flood_triggered"] += 1

        nick = await self.get_nickname(sender_id, str(msg.get("sendNickName") or ""))
        if len(recent) >= cfg.flood_max_messages:
            reason = f"{len(recent)} 条消息 / {cfg.flood_window} 秒"
        else:
            reason = f"相同内容连续发送 {repeat} 次"
        lines = [
            "📢 检测到刷屏行为，请慢一点发言！",
            f"👤 用户: @{nick}（ID: {sender_id}）",
            f"📈 刷屏特征: {reason}",
        ]

        is_bot_admin = await self.is_bot_group_admin(group_id)
        if is_bot_admin and cfg.flood_action == "mute":
            lines.append(f"🔇 已禁言 {self._fmt_duration(cfg.flood_mute_seconds)}")
            await self.mute_user(group_id, sender_id, duration=cfg.flood_mute_seconds,
                                 reason=f"刷屏（{reason}）", user_name=nick)
            await self.send_group(group_id, "\n".join(lines), at_users=[sender_id])
        else:
            if not is_bot_admin:
                lines.append("ℹ️ 机器人非本群管理员，仅警告")
            await self.send_group(group_id, "\n".join(lines),
                                  at_users=[sender_id],
                                  quote_message_id=int(msg.get("id") or 0) or None)
        return True

    async def moderate_group_message(self, msg: dict, msg_type: int, sender_id: int, group_id: int, content: str):
        if msg_type != 0:          # 只审文本
            return
        if not content or not content.strip():
            return
        if self._should_skip(sender_id, group_id):
            return
        self.stats["checked_messages"] += 1

        # 刷屏检测（触发即处理，不再进入敏感词链路）
        if self.config.flood_enabled and await self._check_flood(msg, sender_id, group_id, content):
            return

        # BoxIM 服务端会先把敏感词打码成 *** 再下发，机器人收到的是掩码文本。
        # 含连续 *** 说明服务端已判定违规，直接处理，不必再过 AI/词库
        if re.search(r"\*{3,}", content):
            positions = [(m.start(), m.end()) for m in re.finditer(r"\*{3,}", content)]
            detection = {
                "is_sensitive": True,
                "sensitive_words": [content[s:e] for s, e in positions],
                "positions": positions,
                "masked_text": content,
                "hit_types": ["服务端拦截"],
                "error": None,
            }
            self.stats["sensitive_detected"] += 1
            await self.handle_violation(msg, sender_id, group_id, content, detection, None)
            return

        # AI 复检：先 AI 判断，判敏感再走敏感词检测
        ai_result: Optional[Dict[str, Any]] = None
        if self.config.ai_recheck_enabled and self.ai.available:
            ai_result = await self.ai.check(content)
            if ai_result["success"] and not ai_result["is_sensitive"]:
                return    # AI 判安全，不处理
            # AI 出错则退回敏感词检测
        elif self.config.ai_recheck_enabled:
            ai_result = {"success": False, "is_sensitive": None, "reason": "AI 禁用中", "confidence": 0, "raw_response": ""}

        detection = await self.detector.check(content)
        if detection["error"]:
            logger.warning("敏感词检测失败: %s", detection["error"])
            return
        if not detection["is_sensitive"]:
            return

        self.stats["sensitive_detected"] += 1
        await self.handle_violation(msg, sender_id, group_id, content, detection, ai_result)

    async def handle_violation(self, msg: dict, sender_id: int, group_id: int, content: str, detection: dict, ai_result: Optional[dict]):
        self.violations.add(
            sender_id, group_id,
            original_message=content,
            detected_content=detection["masked_text"],
            positions=detection["positions"],
            ai_result=ai_result,
        )
        count = self.violations.count(sender_id, group_id)
        cfg = self.config

        # 组装警告文本
        words = detection["sensitive_words"]
        if words and all(set(w) == {"*"} for w in words):
            masked_words = "已被服务端打码（原词不可见）"
        else:
            # 按原规格展示掩码词，避免在群里二次传播违规内容
            masked_words = "、".join(self.mask_word(w) for w in words) or "***"
        lines = [cfg.warning_message]
        nick = await self.get_nickname(sender_id, str(msg.get("sendNickName") or ""))
        lines.append(f"👤 用户: @{nick}（ID: {sender_id}）")
        lines.append(f"🔇 敏感词: {masked_words}")
        if detection.get("hit_types"):
            lines.append(f"🏷️ 违规类型: {'、'.join(detection['hit_types'])}")
        if ai_result and ai_result.get("success"):
            lines.append(f"🤖 AI 审核: {ai_result['reason']}")
        lines.append(f"📊 当前违规次数: {count}")

        remain = cfg.max_violations - count
        if 0 < remain <= 2:
            lines.append(f"⚠️ 再违规 {remain} 次将被禁言！")

        is_bot_admin = await self.is_bot_group_admin(group_id)
        recalled = False
        if is_bot_admin:
            # 撤回（sensitive_action=warn 时只警告不动消息）
            if cfg.sensitive_action != "warn":
                try:
                    await self.im.arecall_group_message(int(msg.get("id")))
                    recalled = True
                    self.stats["recalled"] += 1
                    lines.append("🗑️ 消息已撤回")
                except Exception as exc:
                    logger.warning("撤回失败: %s", exc)
            # 禁言
            if count >= cfg.max_violations:
                await self.mute_user(group_id, sender_id, count=count, auto=True, user_name=nick)
        else:
            lines.append("ℹ️ 机器人非本群管理员，无法撤回或禁言")

        # 引用原敏感消息（撤回后原消息已不存在，不再引用）
        quote_id = None if recalled else int(msg.get("id") or 0) or None
        await self.send_group(group_id, "\n".join(lines),
                              at_users=[sender_id] if not recalled else None,
                              quote_message_id=quote_id)

        # 自动举报检查
        should, why = self.complaints.should_complaint(sender_id, group_id, count)
        if should:
            await self.do_complaint(group_id, sender_id, count, auto=True)
        elif count >= cfg.max_violations_before_complaint and why == "1 小时内已举报过":
            pass    # 正常去重

    # ---------------- 禁言 / 解禁 ----------------
    async def is_bot_group_admin(self, group_id: int) -> bool:
        cached = self._admin_cache.get(group_id)
        if cached and time.time() - cached[1] < 300:
            return cached[0]
        is_admin = False
        try:
            members = await self.im._http.arequest("GET", f"/api/group/members/{group_id}")
            for m in members or []:
                if int(m.get("userId") or 0) == self.bot_id and m.get("isManager"):
                    is_admin = True
                    break
        except Exception as exc:
            logger.warning("查询群管理员失败: %s", exc)
        self._admin_cache[group_id] = (is_admin, time.time())
        return is_admin

    async def get_group_managers(self, group_id: int) -> List[int]:
        managers = []
        try:
            members = await self.im._http.arequest("GET", f"/api/group/members/{group_id}")
            for m in members or []:
                if m.get("isManager"):
                    managers.append(int(m.get("userId") or 0))
        except Exception as exc:
            logger.warning("获取群管理员列表失败: %s", exc)
        return managers

    # ---------------- 名称解析 ----------------
    async def resolve_user(self, group_id: int, token: str) -> Tuple[Optional[int], str, str]:
        """用户名/昵称/ID 文本 → (用户ID, 显示名, 错误信息)。纯数字按 ID 处理。"""
        token = token.strip().lstrip("@").strip()
        if not token:
            return None, "", "参数为空"
        if token.isdigit():
            return int(token), token, ""
        cached = self._name_cache.get(f"u:{token.lower()}")
        if cached:
            return cached[0], cached[1], ""

        exact: List[Tuple[int, str]] = []
        fuzzy: List[Tuple[int, str]] = []

        def consider(uid, uname, nick):
            if not uid:
                return
            uname, nick = str(uname or ""), str(nick or "")
            disp = nick or uname or str(uid)
            if token.lower() in (uname.lower(), nick.lower()):
                exact.append((uid, disp))
            elif token.lower() in uname.lower() or token.lower() in nick.lower():
                fuzzy.append((uid, disp))

        # 群成员优先（不用额外搜索，昵称也全）
        if group_id:
            try:
                members = await self.im._http.arequest("GET", f"/api/group/members/{group_id}")
                for m in members or []:
                    consider(
                        int(m.get("userId") or 0),
                        m.get("userName"),
                        m.get("showNickName") or m.get("remarkNickName"),
                    )
            except Exception:
                pass

        if not exact:
            try:
                users = await self.im.asearch_users(token)
                for u in users or []:
                    if isinstance(u, dict):
                        consider(u.get("id"), u.get("userName"), u.get("nickName"))
                    else:
                        consider(getattr(u, "id", 0), getattr(u, "user_name", ""), getattr(u, "nick_name", ""))
            except Exception:
                pass

        cands = exact or fuzzy
        if not cands:
            return None, "", f"未找到用户「{token}」"
        if len(cands) > 1:
            listing = "、".join(f"{d}({i})" for i, d in cands[:5])
            return None, "", f"「{token}」匹配到多个用户，请用更完整的名称: {listing}"
        uid, disp = cands[0]
        self._name_cache[f"u:{token.lower()}"] = (uid, disp)
        return uid, disp, ""

    async def resolve_user_from_args(self, group_id: int, args: List[str]) -> Tuple[Optional[int], str, List[str], str]:
        """把前几个参数当用户名整体尝试（名称可能带空格），返回 (uid, 显示名, 剩余参数, 错误)。

        从长到短逐个尝试，第一个能唯一定位的就是名称，剩下的参数还给调用方。
        """
        last_err = "参数为空"
        for k in range(len(args), 0, -1):
            token = " ".join(args[:k])
            uid, disp, err = await self.resolve_user(group_id, token)
            if uid is not None:
                return uid, disp, args[k:], ""
            last_err = err
        return None, "", args, last_err or f"未找到用户「{' '.join(args)}」"

    async def resolve_group(self, token: str) -> Tuple[Optional[int], str, str]:
        """群名/ID 文本 → (群ID, 显示名, 错误信息)。纯数字按 ID 处理。"""
        token = token.strip()
        if not token:
            return None, "", "参数为空"
        if token.isdigit():
            return int(token), token, ""
        cached = self._name_cache.get(f"g:{token.lower()}")
        if cached:
            return cached[0], cached[1], ""
        try:
            groups = await self.im.aget_groups()
        except Exception as exc:
            return None, "", f"获取群列表失败: {exc}"

        exact, fuzzy = [], []
        for g in groups or []:
            if isinstance(g, dict):
                gid, names = g.get("id"), [g.get("name"), g.get("showGroupName"), g.get("remarkGroupName")]
            else:
                gid, names = getattr(g, "id", None), [getattr(g, "name", None),
                                                     getattr(g, "show_group_name", None),
                                                     getattr(g, "remark_group_name", None)]
            if not gid:
                continue
            disp = next((n for n in names if n), str(gid))
            names = [str(n) for n in names if n]
            if any(n.lower() == token.lower() for n in names):
                exact.append((gid, disp))
            elif any(token.lower() in n.lower() for n in names):
                fuzzy.append((gid, disp))

        cands = exact or fuzzy
        if not cands:
            return None, "", f"未找到群「{token}」"
        if len(cands) > 1:
            listing = "、".join(f"{d}({i})" for i, d in cands[:5])
            return None, "", f"「{token}」匹配到多个群，请用更完整的名称: {listing}"
        gid, disp = cands[0]
        self._name_cache[f"g:{token.lower()}"] = (gid, disp)
        return gid, disp, ""

    async def get_nickname(self, user_id: int, known: str = "") -> str:
        """取用户昵称：已知昵称 > 缓存 > get_user > ID 兜底。"""
        if known and known.strip():
            self._nick_cache[user_id] = known.strip()
            return known.strip()
        if user_id in self._nick_cache:
            return self._nick_cache[user_id]
        try:
            user = await self.im.aget_user(user_id)
            if isinstance(user, dict):
                name = user.get("nickName") or user.get("showNickName") or user.get("userName")
                if name:
                    self._nick_cache[user_id] = str(name)
                    return str(name)
            elif user is not None:
                name = getattr(user, "nick_name", None) or getattr(user, "user_name", None)
                if name:
                    self._nick_cache[user_id] = str(name)
                    return str(name)
        except Exception:
            pass
        return str(user_id)

    async def mute_user(self, group_id: int, user_id: int, duration: Optional[int] = None,
                        count: int = 0, auto: bool = False, reason: str = "",
                        user_name: str = "") -> bool:
        """duration=None 表示自动禁言按梯度；duration<=0 或 None+永久标记为永久。"""
        cfg = self.config
        try:
            await self.im.aset_group_member_muted(group_id, [user_id], True)
        except Exception as exc:
            logger.warning("禁言 API 失败: %s", exc)
            return False

        # 时长计算
        if duration is None:
            idx = max(count - cfg.max_violations, 0)
            if cfg.mute_durations:
                duration = cfg.mute_durations[min(idx, len(cfg.mute_durations) - 1)]
            else:
                duration = 60
        permanent = duration <= 0

        self.stats["muted_users"] += 1
        if auto:
            self.stats["mute_commands"] += 0   # 自动禁言不计命令

        # 定时解禁任务
        key = (group_id, user_id)
        old_task = self.unmute_tasks.pop(key, None)
        if old_task and not old_task.done():
            old_task.cancel()
        if not permanent and cfg.auto_unmute_enabled:
            self.unmute_tasks[key] = asyncio.create_task(
                self._delayed_unmute(group_id, user_id, duration)
            )

        duration_text = "永久" if permanent else self._fmt_duration(duration)
        nick = await self.get_nickname(user_id, user_name)
        notice = cfg.mute_notice_template.format(
            user=nick, duration=duration_text, count=count or "-"
        )
        if reason:
            notice += f"\n📌 原因: {reason}"
        if permanent:
            notice += "\nℹ️ 永久禁言需管理员手动解禁"
        elif cfg.auto_unmute_enabled:
            notice += f"\n⏰ 将在 {duration_text} 后自动解禁"
        await self.send_group(group_id, notice, at_users=[user_id])
        return True

    async def _delayed_unmute(self, group_id: int, user_id: int, duration: int):
        try:
            await asyncio.sleep(duration)
        except asyncio.CancelledError:
            return
        self.unmute_tasks.pop((group_id, user_id), None)
        ok = await self.unmute_user(group_id, user_id, auto=True)
        if ok:
            try:
                nick = await self.get_nickname(user_id)
                await self.im.asend_group_text(
                    group_id,
                    f"✅ @{nick} 禁言时间已到，已自动解禁",
                    at_users=[user_id],
                )
            except Exception:
                pass

    async def unmute_user(self, group_id: int, user_id: int, auto: bool = False) -> bool:
        try:
            await self.im.aset_group_member_muted(group_id, [user_id], False)
        except Exception as exc:
            logger.warning("解禁 API 失败: %s", exc)
            return False
        task = self.unmute_tasks.pop((group_id, user_id), None)
        if task and not task.done():
            task.cancel()    # 手动解禁要取消对应定时任务
        if not auto:
            self.stats["unmute_commands"] += 1
        self.stats["unmuted_users"] += 1
        return True

    @staticmethod
    def mask_word(word: str) -> str:
        """敏感词展示掩码：保留首字，其余打码。"""
        if not word:
            return "*"
        if set(word) == {"*"}:
            return word
        if len(word) <= 1:
            return "*"
        return word[0] + "*" * (len(word) - 1)

    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        if seconds % 3600 == 0 and seconds >= 3600:
            return f"{seconds // 3600} 小时"
        if seconds % 60 == 0 and seconds >= 60:
            return f"{seconds // 60} 分钟"
        return f"{seconds} 秒"

    # ---------------- 举报 ----------------
    async def do_complaint(self, group_id: int, user_id: int, violation_count: int,
                           reason: str = "", auto: bool = True) -> bool:
        records = self.violations.get_user_records(user_id, group_id, limit=5)
        if not records:
            if not auto:
                await self.send_group(group_id, f"❌ 用户 {user_id} 没有违规记录，无法举报")
            return False
        user_name = str(user_id)
        # 尽量拿昵称
        try:
            user = await self.im.aget_user(user_id)
            name = getattr(user, "nick_name", None) or getattr(user, "user_name", None)
            if name:
                user_name = name
        except Exception:
            pass

        content = self.complaints.build_content(user_id, user_name, records, violation_count)
        if reason:
            content = f"{reason}\n{content}"[: self.config.complaint_content_limit]

        try:
            await self.im.asubmit_complaint(
                target_type=self.config.complaint_target_type,
                target_id=user_id,
                target_name=user_name,
                complaint_type=self.config.complaint_type,
                content=content,
            )
            ok = True
        except Exception as exc:
            logger.warning("举报失败: %s", exc)
            ok = False
            if not auto:
                await self.send_group(group_id, f"❌ 举报用户 @{user_name} 失败: {exc}")
        self.complaints.record_event(user_id, group_id, ok, content)
        if not ok:
            return False

        await self.send_group(
            group_id,
            f"📨 已{'自动' if auto else ''}举报用户 @{user_name}（违规 {violation_count} 次）\n"
            f"类型: {self.config.complaint_reason}",
            at_users=[user_id],
        )
        return True

    # ---------------- 命令分发 ----------------
    COMMANDS: Dict[str, List[str]] = {
        "mute": ["禁言", "mute"],
        "mute_all": ["禁言所有人", "muteall", "全员禁言"],
        "unmute": ["解禁", "unmute"],
        "unmute_all": ["解禁群所有人", "unmuteall"],
        "violations": ["违规记录", "violations"],
        "cleanup": ["清理记录", "clean"],
        "whitelist": ["白名单", "whitelist"],
        "report": ["举报用户", "report"],
        "reports": ["举报记录", "reports"],
        "report_stats": ["举报统计", "reportstats"],
        "test_detect": ["检测测试", "testdetect"],
        "ai_check": ["ai检查", "aitest", "AI检查"],
        "ai_stats": ["ai统计", "aistats"],
        "status": ["状态", "status"],
        "help": ["管理帮助", "help", "菜单"],
        "punch": ["punch", "签到"],
        "punch_rank": ["签到排行", "punchrank"],
        "punch_toggle": ["签到开关", "punchtoggle"],
    }

    def _match_command(self, text: str) -> Optional[Tuple[str, List[str]]]:
        if not text.startswith(self.config.trigger_char):
            return None
        parts = text[len(self.config.trigger_char):].split()
        if not parts:
            return None
        cmd = parts[0].strip().lower()
        for name, aliases in self.COMMANDS.items():
            if cmd in [a.lower() for a in aliases]:
                return name, parts[1:]
        return None

    async def handle_command(self, msg: dict, is_group: bool):
        content = msg.get("content", "") or ""
        matched = self._match_command(content)
        if not matched:
            return
        name, args = matched
        sender_id = int(msg.get("sendId") or 0)
        group_id = int(msg.get("groupId") or 0) if is_group else 0

        # 签到对所有人开放，其余命令仅管理员
        if name not in ("punch", "punch_rank"):
            if sender_id not in self.config.admin_ids:
                await self.reply(msg, is_group, "⛔ 该命令仅管理员可用", group_id)
                return

        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return
        try:
            await handler(msg, is_group, group_id, sender_id, args)
        except Exception as exc:
            logger.exception("命令 %s 执行出错", name)
            await self.reply(msg, is_group, f"❌ 命令执行出错: {exc}", group_id)

    # ---------------- 命令实现 ----------------
    async def cmd_mute(self, msg, is_group, group_id, sender_id, args):
        """禁言 用户名/昵称 [时长秒] [原因]，不填时长永久。"""
        if not group_id:
            await self.reply(msg, is_group, "请在群聊中使用该命令", group_id)
            return
        if not args:
            await self.reply(msg, is_group, "用法: /禁言 用户名 [时长秒] [原因]（不填时长则永久禁言）", group_id)
            return
        target, disp, rest, err = await self.resolve_user_from_args(group_id, args)
        if err:
            await self.reply(msg, is_group, f"❌ {err}", group_id)
            return
        duration = -1     # 永久
        reason = ""
        if rest and rest[0].isdigit():
            duration = int(rest[0])
            reason = " ".join(rest[1:])
        else:
            reason = " ".join(rest)
        ok = await self.mute_user(group_id, target, duration=duration, reason=reason, user_name=disp)
        if ok:
            self.stats["mute_commands"] += 1
            await self.reply(msg, is_group,
                             f"✅ 已禁言 {disp}（{'永久' if duration <= 0 else self._fmt_duration(duration)}）",
                             group_id)

    async def cmd_mute_all(self, msg, is_group, group_id, sender_id, args):
        if not group_id:
            await self.reply(msg, is_group, "请在群聊中使用该命令", group_id)
            return
        duration = int(args[0]) if args and args[0].isdigit() else 3600
        reason = " ".join(args[1:]) if len(args) > 1 else ""
        try:
            members = await self.im._http.arequest("GET", f"/api/group/members/{group_id}")
        except Exception as exc:
            await self.reply(msg, is_group, f"❌ 获取群成员失败: {exc}", group_id)
            return
        targets = []
        for m in members or []:
            uid = int(m.get("userId") or 0)
            if uid == self.bot_id or m.get("isManager"):
                continue    # 跳过自己和群管理员
            targets.append(uid)
        if not targets:
            await self.reply(msg, is_group, "没有可禁言的普通成员", group_id)
            return
        ok = True
        for i in range(0, len(targets), 50):
            try:
                await self.im.aset_group_member_muted(group_id, targets[i:i + 50], True)
            except Exception:
                ok = False
        if ok:
            self.stats["mute_commands"] += 1
            # 定时解禁：群全员禁言也挂任务
            if self.config.auto_unmute_enabled and duration > 0:
                for uid in targets:
                    key = (group_id, uid)
                    old = self.unmute_tasks.pop(key, None)
                    if old and not old.done():
                        old.cancel()
                    self.unmute_tasks[key] = asyncio.create_task(
                        self._delayed_unmute(group_id, uid, duration)
                    )
            await self.reply(msg, is_group,
                             f"✅ 已禁言 {len(targets)} 名普通成员（{self._fmt_duration(duration)}）"
                             + (f"，原因: {reason}" if reason else ""), group_id)
        else:
            await self.reply(msg, is_group, "❌ 部分成员禁言失败，请检查权限", group_id)

    async def cmd_unmute(self, msg, is_group, group_id, sender_id, args):
        if not group_id:
            await self.reply(msg, is_group, "请在群聊中使用该命令", group_id)
            return
        if not args:
            await self.reply(msg, is_group, "用法: /解禁 用户名", group_id)
            return
        target, disp, rest, err = await self.resolve_user_from_args(group_id, args)
        if err:
            await self.reply(msg, is_group, f"❌ {err}", group_id)
            return
        ok = await self.unmute_user(group_id, target)
        if ok:
            await self.reply(msg, is_group, f"✅ 已解禁 {disp}", group_id)
            await self.send_group(group_id, f"🎉 用户 @{disp} 已被管理员解禁", at_users=[target])

    async def cmd_unmute_all(self, msg, is_group, group_id, sender_id, args):
        """群聊可省略群名，私聊必须给群名。"""
        target_group, gdisp = group_id, str(group_id)
        if args:
            name = " ".join(args)
            target_group, gdisp, err = await self.resolve_group(name)
            if err:
                await self.reply(msg, is_group, f"❌ {err}", group_id)
                return
        if not target_group:
            await self.reply(msg, is_group, "私聊使用请提供群名: /解禁群所有人 群名", group_id)
            return
        try:
            members = await self.im._http.arequest("GET", f"/api/group/members/{target_group}")
        except Exception as exc:
            await self.reply(msg, is_group, f"❌ 获取群成员失败: {exc}", group_id)
            return
        ids = [int(m.get("userId") or 0) for m in members or []]
        ids = [i for i in ids if i]
        try:
            for i in range(0, len(ids), 50):
                await self.im.aset_group_member_muted(target_group, ids[i:i + 50], False)
        except Exception as exc:
            await self.reply(msg, is_group, f"❌ 解禁失败: {exc}", group_id)
            return
        # 取消该群所有定时解禁任务
        cancelled = 0
        for key in [k for k in self.unmute_tasks if k[0] == target_group]:
            task = self.unmute_tasks.pop(key)
            if task and not task.done():
                task.cancel()
            cancelled += 1
        self.stats["unmute_commands"] += 1
        await self.reply(msg, is_group,
                         f"✅ 已解禁群「{gdisp}」全部 {len(ids)} 名成员，取消 {cancelled} 个定时任务",
                         group_id)
        if not is_group or target_group != group_id:
            await self.send_group(target_group, "🎉 管理员已解除全员禁言")

    async def _show_user_violations(self, msg, is_group, group_id, uid, disp):
        records = self.violations.get_user_records(uid, group_id or 0, limit=5)
        if not records:
            await self.reply(msg, is_group, f"{disp} 暂无违规记录", group_id)
            return
        lines = [f"📋 {disp} 最近违规记录:"]
        for i, r in enumerate(records, 1):
            ts = datetime.fromtimestamp(r["time"]).strftime("%m-%d %H:%M")
            masked = ViolationTracker.mask_content(r["detected_content"], r["positions"])
            lines.append(f"{i}. [{ts}] {masked[:80]}")
            ai = r.get("ai_result")
            if ai and ai.get("success"):
                lines.append(f"   🤖 AI: {ai['reason']}")
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def _show_group_violations(self, msg, is_group, group_id):
        stats = self.violations.group_stats(group_id)
        lines = [f"📊 24 小时违规统计: 共 {stats['total']} 次"]
        for uid, cnt in sorted(stats["user_counts"].items(), key=lambda x: -x[1]):
            lines.append(f"  用户 {uid}: {cnt} 次")
        if not stats["user_counts"]:
            lines.append("  暂无违规记录 👍")
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_violations(self, msg, is_group, group_id, sender_id, args):
        if is_group:
            if args:
                uid, disp, rest, err = await self.resolve_user_from_args(group_id, list(args))
                if err:
                    await self.reply(msg, is_group, f"❌ {err}", group_id)
                    return
                await self._show_user_violations(msg, is_group, group_id, uid, disp)
            else:
                await self._show_group_violations(msg, is_group, group_id)
            return
        # 私聊: /违规记录 群名 [用户名]
        if not args:
            await self.reply(msg, is_group, "私聊使用请提供: /违规记录 群名 用户名", group_id)
            return
        if len(args) == 1:
            gid, gdisp, err = await self.resolve_group(args[0])
            if err:
                await self.reply(msg, is_group, f"❌ {err}", group_id)
                return
            await self._show_group_violations(msg, is_group, gid)
            return
        # 群名和用户名都可能带空格，从长到短找分割点
        last_err = ""
        for k in range(len(args) - 1, 0, -1):
            gid, gdisp, gerr = await self.resolve_group(" ".join(args[:k]))
            if gid is None:
                last_err = gerr
                continue
            uid, disp, _, uerr = await self.resolve_user_from_args(gid, list(args[k:]))
            if uid is not None:
                await self._show_user_violations(msg, is_group, gid, uid, disp)
                return
            last_err = uerr
        await self.reply(msg, is_group, f"❌ {last_err or '未找到匹配的群和用户'}", group_id)

    async def cmd_cleanup(self, msg, is_group, group_id, sender_id, args):
        if args:
            gid, gdisp, err = await self.resolve_group(" ".join(args))
            if err:
                await self.reply(msg, is_group, f"❌ {err}", group_id)
                return
            removed = self.violations.cleanup(gid)
            await self.reply(msg, is_group, f"✅ 已清理群「{gdisp}」过期违规记录 {removed} 条", group_id)
        else:
            removed = self.violations.cleanup()
            await self.reply(msg, is_group, f"✅ 已清理全部过期违规记录 {removed} 条", group_id)

    async def cmd_whitelist(self, msg, is_group, group_id, sender_id, args):
        if len(args) < 2:
            await self.reply(msg, is_group,
                             "用法:\n/白名单 添加 群组 群名\n/白名单 删除 用户 用户名\n/白名单 列表 群组", group_id)
            return
        action, kind = args[0], args[1]
        if action == "列表":
            lines = ["📋 白名单:"]
            lines.append("  群组: " + (", ".join(map(str, self.config.whitelist_groups)) or "无"))
            lines.append("  用户: " + (", ".join(map(str, self.config.whitelist_users)) or "无"))
            await self.reply(msg, is_group, "\n".join(lines), group_id)
            return
        if len(args) < 3:
            await self.reply(msg, is_group, f"用法: /白名单 {action} {kind} 名称", group_id)
            return
        name = " ".join(args[2:])
        if kind.startswith("群"):
            gid, gdisp, err = await self.resolve_group(name)
            if err:
                await self.reply(msg, is_group, f"❌ {err}", group_id)
                return
            target_id, disp, lst, kind_name = gid, gdisp, self.config.whitelist_groups, "群组"
        elif kind.startswith("用"):
            uid, udisp, err = await self.resolve_user(group_id or 0, name)
            if err:
                await self.reply(msg, is_group, f"❌ {err}", group_id)
                return
            target_id, disp, lst, kind_name = uid, udisp, self.config.whitelist_users, "用户"
        else:
            await self.reply(msg, is_group, "类型只支持 群组 或 用户", group_id)
            return
        if action == "添加":
            if target_id not in lst:
                lst.append(target_id)
            await self.reply(msg, is_group, f"✅ 已添加{kind_name}「{disp}」({target_id}) 到白名单", group_id)
        elif action == "删除":
            if target_id in lst:
                lst.remove(target_id)
                await self.reply(msg, is_group, f"✅ 已从白名单删除{kind_name}「{disp}」({target_id})", group_id)
            else:
                await self.reply(msg, is_group, f"ℹ️ {kind_name}「{disp}」({target_id}) 不在白名单中", group_id)
        else:
            await self.reply(msg, is_group, "操作只支持 添加 或 删除", group_id)

    async def cmd_report(self, msg, is_group, group_id, sender_id, args):
        """/举报用户 用户名 [原因]"""
        if not args:
            await self.reply(msg, is_group, "用法: /举报用户 用户名 [原因]", group_id)
            return
        uid, disp, rest, err = await self.resolve_user_from_args(group_id or 0, args)
        if err:
            await self.reply(msg, is_group, f"❌ {err}", group_id)
            return
        reason = " ".join(rest)
        count = self.violations.count(uid, group_id or 0)
        if count == 0:
            await self.reply(msg, is_group, f"❌ {disp} 没有违规记录，无法举报", group_id)
            return
        ok = await self.do_complaint(group_id, uid, count, reason=reason, auto=False)
        if ok:
            await self.reply(msg, is_group, f"✅ 已提交对 {disp} 的举报", group_id)

    async def cmd_reports(self, msg, is_group, group_id, sender_id, args):
        user_disp = None
        user_id = None
        if args:
            user_id, user_disp, rest, err = await self.resolve_user_from_args(group_id or 0, args)
            if err:
                await self.reply(msg, is_group, f"❌ {err}", group_id)
                return
            records = self.complaints.recent_records(user_id=user_id, limit=5)
            title = f"📋 {user_disp} 举报记录:"
        else:
            records = self.complaints.recent_records(group_id=group_id or None, limit=5)
            title = "📋 本群最近举报记录:" if group_id else "📋 最近举报记录:"
        if not records:
            await self.reply(msg, is_group, title + "\n暂无记录", group_id)
            return
        lines = [title]
        for r in records:
            ts = datetime.fromtimestamp(r["time"]).strftime("%m-%d %H:%M")
            status = "✅" if r["ok"] else "❌"
            lines.append(f"{status} [{ts}] 用户 {r['user_id']} (群 {r['group_id']})")
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_report_stats(self, msg, is_group, group_id, sender_id, args):
        s = self.complaints.stats()
        cfg = self.config
        lines = [
            "📊 举报统计:",
            f"  总举报: {s['total']} | 成功: {s['success']} | 失败: {s['failed']}",
            f"  成功率: {s['rate']:.0f}% | 24 小时举报: {s['day_count']}",
            f"  被举报用户数: {self.complaints.complained_users()}",
            f"  配置: 自动举报 {'开' if cfg.auto_complaint_enabled else '关'}，"
            f"阈值 {cfg.max_violations_before_complaint} 次，去重 {cfg.complaint_interval_hours} 小时",
        ]
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_test_detect(self, msg, is_group, group_id, sender_id, args):
        if not args:
            await self.reply(msg, is_group, "用法: /检测测试 文本", group_id)
            return
        text = " ".join(args)
        result = await self.detector.check(text)
        lines = ["🔬 敏感词检测测试:", f"  原始文本: {text[:80]}"]
        if result["error"]:
            lines.append(f"  ❌ {result['error']}")
        else:
            lines.append(f"  检测结果: {'敏感' if result['is_sensitive'] else '安全'}")
            lines.append(f"  掩码文本: {result['masked_text'][:80]}")
            lines.append(f"  敏感位置: {result['positions'] or '无'}")
            lines.append(f"  敏感词: {'、'.join(result['sensitive_words']) or '无'}")
            if result.get("hit_types"):
                lines.append(f"  违规类型: {'、'.join(result['hit_types'])}")
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_ai_check(self, msg, is_group, group_id, sender_id, args):
        if not args:
            await self.reply(msg, is_group, "用法: /ai检查 文本", group_id)
            return
        text = " ".join(args)
        if not self.config.ai_recheck_enabled:
            await self.reply(msg, is_group, "ℹ️ AI 复检当前未开启（config.ai_recheck_enabled）", group_id)
            return
        result = await self.ai.check(text)
        lines = ["🤖 AI 内容检查测试:", f"  文本: {text[:60]}"]
        if not result["success"]:
            lines.append(f"  ❌ {result['reason']}")
        else:
            lines.append(f"  是否敏感: {'是' if result['is_sensitive'] else '否'}")
            lines.append(f"  置信度: {result['confidence']:.0%}")
            lines.append(f"  理由: {result['reason']}")
            lines.append(f"  建议: {'拦截处理' if result['is_sensitive'] else '放行'}")
            raw = result["raw_response"] or "(空)"
            lines.append(f"  原始响应: {raw[:40]}")
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_ai_stats(self, msg, is_group, group_id, sender_id, args):
        ai = self.ai
        total_judged = ai.confirmed_count + ai.overturned_count
        check_rate = ai.check_count / max(self.stats["checked_messages"], 1) * 100
        overturn_rate = ai.overturned_count / max(total_judged, 1) * 100
        lines = [
            "🤖 AI 检查统计:",
            f"  检查次数: {ai.check_count}",
            f"  确认敏感: {ai.confirmed_count} | 推翻敏感: {ai.overturned_count}",
            f"  检查率: {check_rate:.1f}% | 推翻率: {overturn_rate:.1f}%",
            f"  错误次数: {ai.error_count} | 状态: {'可用' if ai.available else '临时禁用中'}",
            f"  API: {self.config.ai_api_url}",
            f"  模型: {self.config.ai_model_id} | 复检: {'开' if self.config.ai_recheck_enabled else '关'}",
        ]
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_status(self, msg, is_group, group_id, sender_id, args):
        s = self.stats
        cs = self.complaints.stats()
        ms = self.memory.stats()
        uptime = int(time.time() - self.start_time)
        h, m = uptime // 3600, uptime % 3600 // 60
        ai_total = self.ai.confirmed_count + self.ai.overturned_count
        lines = [
            "📊 机器人状态:",
            f"  已检查消息: {s['checked_messages']} | 群消息: {s['group_messages']}",
            f"  敏感检测: {s['sensitive_detected']} | 撤回: {s['recalled']} | 刷屏处置: {s['flood_triggered']}",
            f"  AI: 检查 {self.ai.check_count} 次，确认 {self.ai.confirmed_count}，推翻 {self.ai.overturned_count}",
            f"  禁言: {s['muted_users']} 人 | 解禁: {s['unmuted_users']} 人",
            f"  命令: 禁言 {s['mute_commands']} 次 / 解禁 {s['unmute_commands']} 次",
            f"  举报: 总 {cs['total']}，成功 {cs['success']}（{cs['rate']:.0f}%），24h {cs['day_count']}",
            f"  活跃定时解禁任务: {len(self.unmute_tasks)}",
            f"  记忆池: {ms['groups']} 群 {ms['memories']} 条 | AI 对话: {self.human_ai.chat_count} 次",
            f"  签到: {'开启' if self._runtime_punch_enabled else '关闭'}，今日 {self.punch.today_count()} 人",
            f"  运行时间: {h} 小时 {m} 分",
        ]
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_help(self, msg, is_group, group_id, sender_id, args):
        lines = [
            "📖 管理命令（仅管理员）:",
            "  /禁言 用户名 [时长秒] [原因] — 不填时长永久禁言",
            "  /禁言所有人 [时长] [原因] — 禁言普通成员",
            "  /解禁 用户名",
            "  /解禁群所有人 [群名]",
            "  /违规记录 [用户名] — 查群统计或个人记录",
            "  /清理记录 [群名] — 清理过期违规记录",
            "  /白名单 添加|删除|列表 群组|用户 [名称]",
            "  /举报用户 用户名 [原因]",
            "  /举报记录 [用户名]",
            "  /举报统计",
            "  /检测测试 文本 — 敏感词检测测试",
            "  /ai检查 文本 — AI 审核测试",
            "  /ai统计",
            "  /状态 — 运行统计",
            "  /签到开关 on|off",
            "",
            "🎮 所有人可用:",
            "  /punch 或 /签到 — 每日签到",
            "  /签到排行 — 积分排行",
            "",
            "⚙️ 自动功能:",
            "  敏感内容审核 → 警告 → 撤回 → 累计禁言 → 自动举报",
            "  刷屏检测 → 警告 → 自动禁言（10 秒 5 条或同内容连发 3 次）",
            "  禁言到期自动解禁（永久禁言除外）",
            "  提示: 用户参数支持 用户名/昵称（带@也行），群参数支持群名",
        ]
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    # ---------------- 签到 ----------------
    async def cmd_punch(self, msg, is_group, group_id, sender_id, args):
        if not self._runtime_punch_enabled:
            await self.reply(msg, is_group, "ℹ️ 签到功能当前已关闭", group_id)
            return
        nick = msg.get("sendNickName") or str(sender_id)
        result = self.punch.punch(sender_id, nick)
        info = result["info"]
        if result.get("already"):
            await self.reply(msg, is_group,
                             f"📅 {nick} 今天已经签到过啦～\n"
                             f"🔥 连续签到 {info['streak']} 天 | 累计 {info['total']} 天 | 积分 {info['points']}",
                             group_id)
            return
        order = result.get("order", 1)
        medal = "🥇" if order == 1 else "🥈" if order == 2 else "🥉" if order == 3 else "custom"
        lines = [
            f"✅ {nick} 签到成功！",
            f"🎯 今日第 {order} 位签到",
            f"🔥 连续签到 {info['streak']} 天 | 累计 {info['total']} 天",
            f"💰 获得积分 +{result['points']}，当前 {info['points']}",
        ]
        if info["streak"] >= 7:
            lines.append("🌟 坚持一周了，太强了！")
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_punch_rank(self, msg, is_group, group_id, sender_id, args):
        board = self.punch.leaderboard(10)
        if not board:
            await self.reply(msg, is_group, "暂无签到数据，快来做第一个签到的人！", group_id)
            return
        lines = ["🏆 签到排行榜:"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, info) in enumerate(board):
            m = medals[i] if i < 3 else f"{i + 1}."
            lines.append(
                f"{m} {info.get('name', uid)} — 积分 {info.get('points', 0)}，"
                f"连续 {info.get('streak', 0)} 天，累计 {info.get('total', 0)} 天"
            )
        await self.reply(msg, is_group, "\n".join(lines), group_id)

    async def cmd_punch_toggle(self, msg, is_group, group_id, sender_id, args):
        if not args or args[0].lower() not in ("on", "off", "开", "关"):
            await self.reply(msg, is_group,
                             f"用法: /签到开关 on|off（当前: {'开启' if self._runtime_punch_enabled else '关闭'}）",
                             group_id)
            return
        turn_on = args[0].lower() in ("on", "开")
        self._runtime_punch_enabled = turn_on
        self.punch.enabled = turn_on
        await self.reply(msg, is_group, f"✅ 签到功能已{'开启' if turn_on else '关闭'}", group_id)

    # ---------------- 发送辅助 ----------------
    @staticmethod
    def split_reply(text: str, max_length: int = 500) -> List[str]:
        if len(text) <= max_length:
            return [text]
        chunks, current = [], ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > max_length:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
            # 单行超长兜底切一刀
            while len(current) > max_length:
                chunks.append(current[:max_length])
                current = current[max_length:]
        if current:
            chunks.append(current)
        return chunks

    async def send_group(self, group_id: int, text: str, at_users: Optional[List[int]] = None,
                         quote_message_id: Optional[int] = None):
        if self.config.signature_enabled:
            text = append_signature(text)
        for i, part in enumerate(self.split_reply(text, self.config.reply_max_length)):
            try:
                await self.im.asend_group_raw(
                    group_id, part, MessageType.TEXT,
                    at_users=at_users,
                    quote_message_id=quote_message_id if i == 0 else None,
                )
                at_users = None    # 只在第一段 @，引用也只挂第一段
            except Exception as exc:
                logger.warning("群消息发送失败: %s", exc)
                await asyncio.sleep(1)

    async def reply(self, msg: dict, is_group: bool, text: str, group_id: int = 0):
        if is_group and group_id:
            await self.send_group(group_id, text)
        else:
            sender = int(msg.get("sendId") or 0)
            if self.config.signature_enabled:
                text = append_signature(text)
            for part in self.split_reply(text, self.config.reply_max_length):
                try:
                    await self.im.asend_text(sender, part)
                except Exception as exc:
                    logger.warning("私聊回复失败: %s", exc)

    # ---------------- 后台任务 ----------------
    async def _cleanup_loop(self):
        """定期清理过期违规记录。"""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.config.violation_cleanup_interval)
            except asyncio.TimeoutError:
                removed = self.violations.cleanup()
                if removed:
                    logger.info("自动清理过期违规记录 %s 条", removed)
            except asyncio.CancelledError:
                return

    async def _token_refresh_loop(self):
        """每 6 小时刷新一次 token，失败不致命。"""
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=6 * 3600)
            except asyncio.TimeoutError:
                try:
                    await self.im.arefresh_token()
                    logger.info("token 已刷新")
                except Exception as exc:
                    logger.warning("token 刷新失败: %s", exc)
            except asyncio.CancelledError:
                return

    async def shutdown(self):
        logger.info("正在关闭机器人...")
        for key, task in list(self.unmute_tasks.items()):
            if task and not task.done():
                task.cancel()
        self.unmute_tasks.clear()
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        try:
            if self.web:
                await self.web.stop()
        except Exception:
            pass
        try:
            await self.human_ai.close()
        except Exception:
            pass
        await self.detector.close()
        await self.ai.close()
        self._print_final_stats()
        try:
            await self.im.aclose()
        except Exception:
            pass

    def _print_final_stats(self):
        s = self.stats
        cs = self.complaints.stats()
        print("=" * 50)
        print("最终统计:")
        print(f"  检查消息: {s['checked_messages']} | 群消息: {s['group_messages']}")
        print(f"  敏感检测: {s['sensitive_detected']} | 撤回: {s['recalled']}")
        print(f"  AI 检查: {self.ai.check_count}（确认 {self.ai.confirmed_count} / 推翻 {self.ai.overturned_count}）")
        print(f"  禁言: {s['muted_users']} | 解禁: {s['unmuted_users']}")
        print(f"  举报: {cs['total']} 次（成功 {cs['success']}）")
        print("=" * 50)


# --------------------------------------------------------------------------
def main():
    config = BotConfig()
    if not config.username or not config.password:
        print("请设置 BOXIM_USERNAME / BOXIM_PASSWORD 环境变量或在 config.py 填写账号")
        sys.exit(1)

    bot = GroupModerationBot(config)

    async def run():
        loop = asyncio.get_running_loop()
        stop_signals = (signal.SIGINT, signal.SIGTERM)
        for sig in stop_signals:
            loop.add_signal_handler(sig, bot._shutdown.set)
        await bot.start()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
