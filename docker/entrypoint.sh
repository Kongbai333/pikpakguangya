#!/bin/sh
# 容器启动脚本：准备数据目录 → 首次生成配置 → 起服务。
#
# 日志和媒体目录已经在构建期软链到 /data（见 Dockerfile），这里不碰，
# 免得把用户 bind mount 进来的目录删掉。
set -e

DATA_DIR="$(dirname "$PIKPAK_CONFIG")"

mkdir -p "$DATA_DIR/session" "$DATA_DIR/logs" "$DATA_DIR/media/thumbs"

if [ ! -f "$PIKPAK_CONFIG" ]; then
    cp /app/docker/config.toml "$PIKPAK_CONFIG"
    echo "[entrypoint] 首次启动，已生成 $PIKPAK_CONFIG"
    echo "[entrypoint] 编辑它填上 Telegram api_id / api_hash 和 [admin] password，然后重启容器。"
fi

# 没填管理员密码就说清楚，别让人对着 503 猜半天
if ! grep -qE '^[[:space:]]*password[[:space:]]*=[[:space:]]*"[^"]+"' "$PIKPAK_CONFIG"; then
    echo "[entrypoint] ⚠ $PIKPAK_CONFIG 里 [admin] password 还是空的，管理后台会拒绝登录。"
fi

echo "[entrypoint] 管理后台 http://<主机>:8000/#/admin"
echo "[entrypoint] 数据目录 $DATA_DIR"

exec python -m app.api
