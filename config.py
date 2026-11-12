# -*- coding: utf-8 -*-
"""机器人配置。账号密码建议用环境变量，避免写死在代码里。"""
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class BotConfig:
    # ---- 账号 ----
    username: str = os.environ.get("BOXIM_USERNAME", "")
    password: str = os.environ.get("BOXIM_PASSWORD", "")
    base_url: str = os.environ.get("BOXIM_BASE_URL", "https://www.boximchat.com/")

    # ---- 权限 ----
    admin_ids: List[int] = field(default_factory=lambda: [
        int(x) for x in os.environ.get("BOT_ADMIN_IDS", "").replace("，", ",").split(",")
        if x.strip().strip("[]\"' ").lstrip("-").isdigit()
    ])
    ignore_user_ids: List[int] = field(default_factory=lambda: [])
    whitelist_groups: List[int] = field(default_factory=lambda: [])
    whitelist_users: List[int] = field(default_factory=lambda: [])

    # ---- 违规 ----
    max_violations: int = 3              # 达到次数后禁言
    max_violations_before_complaint: int = 5   # 达到次数后举报
    violation_expire_hours: int = 24     # 违规记录过期时间
    violation_data_file: str = os.environ.get("VIOLATION_DATA_FILE", "violation_data.json")
    mute_durations: List[int] = field(default_factory=lambda: [60, 300, 1800, 7200])  # 秒

    # ---- 处理 ----
    sensitive_action: str = "warn"       # warn=仅警告 recall_warn=撤回+警告
    trigger_char: str = "/"
    warning_message: str = "⚠️ 检测到敏感内容，请注意言行！"
    mute_notice_template: str = "🔇 {user} 因多次违规被禁言 {duration}，累计违规 {count} 次"

    # ---- 敏感词检测 API ----
    sensitive_api_url: str = os.environ.get("SENSITIVE_API_URL", "https://api.example.com/sensitive/check")
    sensitive_api_token: str = os.environ.get("SENSITIVE_API_TOKEN", "")

    # ---- AI 复检（OpenAI 协议）----
    ai_recheck_enabled: bool = os.environ.get("AI_RECHECK_ENABLED", "false").lower() in ("1", "true", "yes", "on")
    ai_api_url: str = os.environ.get("AI_API_URL", "https://api.openai.com/v1/chat/completions")
    ai_api_key: str = os.environ.get("AI_API_KEY", "")
    ai_model_id: str = os.environ.get("AI_MODEL_ID", "gpt-4o-mini")
    ai_timeout: float = 15.0
    ai_error_disable_seconds: float = 60.0   # 连续出错后临时禁用时长
    ai_max_consecutive_errors: int = 3

    # ---- 自动解禁 / 自动举报 ----
    auto_unmute_enabled: bool = True
    auto_complaint_enabled: bool = True
    complaint_interval_hours: float = 1.0    # 同一用户举报去重间隔
    complaint_type: int = 3                  # 3=不良内容
    complaint_reason: str = "群内多次发送敏感违规内容"
    complaint_target_type: int = 1           # 1=用户
    complaint_max_samples: int = 5           # 举报附带违规样本数
    complaint_content_limit: int = 500       # 举报内容字数上限

    # ---- 每日签到 ----
    punch_enabled: bool = True
    punch_min_points: int = 1
    punch_max_points: int = 10
    punch_streak_bonus: int = 1              # 连续签到每天额外积分，封顶 5
    punch_streak_bonus_cap: int = 5
    punch_data_file: str = os.environ.get("PUNCH_DATA_FILE", "punch_data.json")

    # ---- 其它 ----
    stats_group_messages: bool = True        # 消息中间件统计群消息
    reply_max_length: int = 500              # 超过则按行分段
    violation_cleanup_interval: int = 600    # 过期记录自动清理周期（秒）
