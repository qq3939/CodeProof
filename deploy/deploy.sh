#!/usr/bin/env bash
# 一键构建并启动（首次部署和以后每次更新代码都用它）。
# 在项目根目录运行：bash deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "缺少 .env：先运行 cp deploy/env.prod.example .env 并填好里面的值" >&2
  exit 1
fi
if grep -q "=CHANGE_ME" .env; then
  echo ".env 里还有 CHANGE_ME 没替换，请先填好：" >&2
  grep -n "=CHANGE_ME" .env >&2
  exit 1
fi

# 逐个构建镜像，而不是让 compose 并行构建：2 GB 内存同时跑 pip 和 npm 容易 OOM。
# api、worker、migrate 共用同一个镜像，构建一次就够了。
docker compose build api
docker compose build frontend

docker compose up -d --remove-orphans
# 清掉构建留下的旧镜像层，20 GB 左右的系统盘经不起每次更新都堆一份
docker image prune -f

docker compose ps
echo
echo "启动完成。几十秒后打开 https://$(grep '^DOMAIN=' .env | cut -d= -f2) 验证。"
