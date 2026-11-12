#!/bin/bash
# 启动机器人（后台运行，日志写入 bot.log）
# 账号密码通过环境变量传入，请勿写死在脚本里
cd "$(dirname "$0")"
export BOXIM_USERNAME="${BOXIM_USERNAME:?请先设置 BOXIM_USERNAME 环境变量}"
export BOXIM_PASSWORD="${BOXIM_PASSWORD:?请先设置 BOXIM_PASSWORD 环境变量}"
nohup python3 bot.py >> bot.log 2>&1 &
echo "机器人已启动 PID: $!  日志: $(pwd)/bot.log"
