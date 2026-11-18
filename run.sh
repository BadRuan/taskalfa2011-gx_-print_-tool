#!/usr/bin/env bash
# 在 fedora 上部署/更新局域网打印服务（rootless podman）
# 用法: ./run.sh          # 构建并(重)启动
#       ./run.sh logs     # 查看日志
#       ./run.sh stop     # 停止并删除容器
#
# 目录约定：
#   本仓库（设计文件）      = ~/Documents/taskalfa-print
#   持久化数据（容器 /data） = $DATA（下方绝对路径，不随仓库走）
#   移动过数据目录后必须重新 ./run.sh 重建容器——挂载源是创建时写死的。
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=taskalfa-print:latest
NAME=taskalfa-print
DATA=/home/ruanfumin/Documents/Docker/tasklafa_print/data
PORT=8080

case "${1:-start}" in
  logs)
    podman logs -f --tail 100 "$NAME"; exit 0;;
  stop)
    podman rm -f "$NAME" 2>/dev/null || true; exit 0;;
  state)
    podman ps -a --filter "name=$NAME"; exit 0;;
esac

mkdir -p "$DATA"

echo "==> 构建镜像 $IMAGE"
# --format docker：OCI 格式会忽略 HEALTHCHECK，必须用 docker 格式（见 Dockerfile 注释）
podman build --format docker -f Dockerfile -t "$IMAGE" "$PWD"

echo "==> 重新启动容器"
podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d --name "$NAME" \
  --restart=unless-stopped \
  --network=host \
  -e PRINTER_IP=192.168.0.199 \
  -e PRINTER_PORT=9100 \
  -e TZ=Asia/Shanghai \
  -e WEB_PORT="$PORT" \
  -v "$DATA:/data:Z" \
  "$IMAGE"

sleep 3
podman ps --filter "name=$NAME" --format '{{.Names}}  {{.Status}}  {{.Ports}}'
echo "==> 本机接口: http://localhost:$PORT/"
curl -s -m 10 "http://127.0.0.1:$PORT/api/health"; echo
