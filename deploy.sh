#!/usr/bin/env bash
# astrbot_plugin_wpp 部署脚本: docker cp → 依赖 → 重启 → 日志检查
set -euo pipefail

PLUGIN_SRC="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="astrbot"
PLUGIN_DIR="/AstrBot/data/plugins/astrbot_plugin_wpp"

echo "==> [1/4] 拷贝插件到容器 $CONTAINER"
# 先在宿主机打包「运行所需文件」: 测试/缓存/git 元数据不进容器
# (旧版直接 docker cp 整个 dev 目录, tests/.github/__pycache__ 全被带进去)。
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
tar -C "$PLUGIN_SRC" \
    --exclude=.git --exclude=.github --exclude=tests \
    --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.pytest_cache --exclude=.ruff_cache --exclude=data \
    -cf - . | tar -C "$STAGE" -xf -
# 整目录替换: 源里已删除/改名的模块不会作为旧文件残留在容器里。
# 运行时数据在 /AstrBot/data/plugin_data/astrbot_plugin_wpp, 不在本目录。
docker exec "$CONTAINER" sh -c "rm -rf '$PLUGIN_DIR' && mkdir -p '$PLUGIN_DIR'"
docker cp "$STAGE"/. "$CONTAINER":"$PLUGIN_DIR"/

echo "==> [2/4] 检查依赖 aiohttp + websockets"
docker exec "$CONTAINER" sh -c 'python3 -c "import aiohttp, websockets" >/dev/null 2>&1 || pip install aiohttp websockets -i https://mirrors.aliyun.com/pypi/simple/'

echo "==> [3/4] 重启 AstrBot"
docker restart "$CONTAINER"

echo "==> [4/4] 等待启动并看日志"
sleep 6
docker logs --tail 40 "$CONTAINER" 2>&1 | grep -iE "wpp|plugin|adapter|error" || true

echo ""
echo "部署完成。如未见 'Platform adapter registered: wpp', 检查:"
echo "  docker logs astrbot 2>&1 | tail -50"
echo "  Dashboard http://<host>:6185 → 平台适配器 → 添加 个人微信（Pad协议）"
