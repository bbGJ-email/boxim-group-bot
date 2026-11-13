# BoxIM Group Bot

基于 [boxim-sdk](https://github.com/nichengfuben/boxim-sdk) 的盒子IM群管理机器人，集敏感内容审核、违规跟踪、自动禁言/解禁、自动举报、每日签到于一体。

## 功能

### 敏感内容审核
- **AI 复检**（OpenAI 协议，流式，可开关）：先 AI 判断，判敏感后再走敏感词检测
- **敏感词检测 API**：按返回的 `*` 掩码定位敏感词、提取词、生成掩码文本
- **服务端打码直判**：消息内容含 `***`（已被 IM 服务端打码）直接判定违规
- 白名单群/用户、管理员、忽略用户自动跳过；非文本消息跳过
- AI 连续出错自动临时禁用，带重试与错误统计

### 违规处理
- 按「用户+群」记录违规（原消息、检测内容、敏感位置、AI 结果），24h 过期自动清理，**磁盘持久化**（重启不丢）
- 警告通知 @用户 + 掩码词 + AI 结果 + 当前违规次数 + 临近禁言预警
- 机器人是群管理员时自动撤回、达阈值自动禁言；非管理员仍警告+举报

### 禁言
- 达 `MAX_VIOLATIONS` 后梯度禁言（默认 60/300/1800/7200 秒），异步定时解禁，解禁后群内通知
- 支持永久禁言，手动解禁自动取消定时任务

### 自动举报
- 达阈值后自动举报（1 小时去重），附带最多 5 条违规样本；举报记录/统计/成功率查询

### 每日签到 `/punch`
- 连续天数、累计积分、随机积分、今日第 N 位、签到排行，JSON 持久化，可开关

## 管理员命令

| 命令 | 说明 |
|------|------|
| `/禁言 用户名 [时长秒] [原因]` | 不填时长永久禁言 |
| `/禁言所有人 [时长] [原因]` | 禁言普通成员（跳过管理员） |
| `/解禁 用户名` | 解禁 |
| `/解禁群所有人 [群名]` | 群内可省略群名 |
| `/违规记录 [用户名]` | 群统计或个人记录 |
| `/清理记录 [群名]` | 清理过期违规记录 |
| `/白名单 添加\|删除\|列表 群组\|用户 [名称]` | 白名单管理 |
| `/举报用户 用户名 [原因]` | 手动举报 |
| `/举报记录 [用户名]` / `/举报统计` | 举报查询 |
| `/检测测试 文本` / `/ai检查 文本` / `/ai统计` | 检测链路测试 |
| `/状态` | 运行统计总览 |
| `/签到开关 on\|off` | 签到开关 |
| `/help` / `/管理帮助` / `/菜单` | 帮助 |

> 用户参数支持**昵称、用户名、@名称**（带空格也可以，如 `/禁言 Ethan Fang 300`），群参数支持群名；纯数字按 ID 直通兼容。

## 快速开始

```bash
pip install -r requirements.txt

# 环境变量（可用 bot.env + systemd EnvironmentFile）
export BOXIM_USERNAME=你的账号
export BOXIM_PASSWORD=你的密码
export BOT_ADMIN_IDS=管理员ID1,管理员ID2          # 可选
export SENSITIVE_API_URL=敏感词检测API地址         # 可选
export SENSITIVE_API_TOKEN=API密钥                # 可选
export AI_RECHECK_ENABLED=true                    # AI 复检开关
export AI_API_URL=https://api.openai.com/v1/chat/completions
export AI_API_KEY=sk-xxx
export AI_MODEL_ID=gpt-4o-mini
export SENSITIVE_ACTION=recall_warn               # warn=仅警告 / recall_warn=撤回+警告

python3 bot.py        # 前台运行
./run.sh              # 后台运行
```

所有配置集中在 `config.py`（`BotConfig`），敏感项均从环境变量读取，代码中不含任何凭据。
