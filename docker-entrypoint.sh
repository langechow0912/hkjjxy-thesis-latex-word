#!/bin/sh
# 容器入口：刷新字体缓存后启动 Web 服务
# 挂载宿主机字体是运行时行为，所以 fc-cache 必须在这里跑，不能只在构建时跑
set -e

if [ -d /usr/share/fonts/windows ]; then
    fc-cache -f /usr/share/fonts/windows > /dev/null 2>&1 || true
fi
fc-cache -f > /dev/null 2>&1 || true

exec python web/app.py --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}"
