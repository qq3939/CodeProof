#!/usr/bin/env bash
# 给 2 GB 内存的服务器加 2 GB 交换空间。
# 为什么：构建前端镜像（vue-tsc + vite）和分析大仓库时内存峰值可能超过 2 GB，
# 没有 swap 时 Linux 会直接杀进程（OOM），表现为构建莫名失败或 worker 重启。
# 可以重复运行：已经有 swap 时什么也不做。
set -euo pipefail

SWAPFILE=/swapfile
SIZE=2G

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 root 运行：sudo bash deploy/setup-swap.sh" >&2
  exit 1
fi

if swapon --show | grep -q .; then
  echo "已经有 swap 了："
  swapon --show
  exit 0
fi

fallocate -l "$SIZE" "$SWAPFILE"
chmod 600 "$SWAPFILE"
mkswap "$SWAPFILE"
swapon "$SWAPFILE"

# 写进 fstab，重启后自动挂载
grep -q "^$SWAPFILE " /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
# 只在内存真的不够时才用 swap（默认 60 太积极，会让数据库变慢）
sysctl -w vm.swappiness=10
grep -q "^vm.swappiness" /etc/sysctl.conf || echo "vm.swappiness=10" >> /etc/sysctl.conf

echo "完成："
free -h
