#!/usr/bin/env bash
# astrbot-plugin-wpp 部署脚本: docker cp → 依赖 → 重启 → 日志检查
set -euo pipefail

PLUGIN_SRC="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="astrbot"
PLUGIN_DIR="/AstrBot/data/plugins/astrbot-plugin-wpp"

echo "==> [1/4] 拷贝插件到容器 $CONTAINER"
docker exec "$CONTAINER" sh -c "mkdir -p $PLUGIN_DIR"
docker cp "$PLUGIN_SRC"/. "$CONTAINER":"$PLUGIN_DIR"/

echo "==> [2/4] 检查依赖 aiohttp"
docker exec "$CONTAINER" sh -c 'python3 -c "import aiohttp" >/dev/null 2>&1 || pip install aiohttp -i https://mirrors.aliyun.com/pypi/simple/'

echo "==> [3/4] 重启 AstrBot"
docker restart "$CONTAINER"

echo "==> [4/4] 等待启动并看日志"
sleep 6
docker logs --tail 40 "$CONTAINER" 2>&1 | grep -iE "wpp|plugin|adapter|error" || true

echo ""
echo "部署完成。如未见 'Platform adapter registered: wpp', 检查:"
echo "  docker logs astrbot 2>&1 | tail -50"
echo "  Dashboard http://<host>:6185 → 平台适配器 → 添加 微信 WPP"
