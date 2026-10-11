#!/bin/bash

# FN Message Bot 部署脚本

set -e  # 遇到错误时退出

echo "FN Message Bot 部署脚本"
echo "========================"

# 检查是否安装了 Docker
if ! command -v docker &> /dev/null; then
    echo "错误: 未找到 Docker，请先安装 Docker"
    exit 1
fi

# 检查是否安装了 Docker Compose
if ! docker compose version &> /dev/null; then
    echo "错误: 未找到 Docker Compose，请确保安装了 Docker Compose V2"
    exit 1
fi

# 检查 docker-compose.yml 文件是否存在
if [ ! -f "docker-compose.yml" ]; then
    echo "错误: 未找到 docker-compose.yml 文件"
    exit 1
fi

# 提示：本仓库的 compose 用 build: .（从源码构建），推送渠道在 Web 配置页里填，
# 不再需要编辑 docker-compose.yml 里的 webhook 占位符。
# 若要用预构建镜像（如 GitHub Actions 推到 GHCR 的），把 compose 里的 build: . 换成 image:。

echo "开始部署 FN Message Bot..."

# 构建并启动（build 会顺带应用代码变更；不需要单独的 pull：
# compose 用的是 build: .，没有可拉取的远程镜像）
echo "构建镜像..."
docker compose build

echo "启动服务..."
docker compose up -d

# 显示服务状态
echo "服务状态:"
docker compose ps

# 显示最近的日志
echo ""
echo "最近的日志:"
docker compose logs --tail=20

echo ""
echo "部署完成！"
echo "服务正在后台运行。"
echo ""
echo "常用命令："
echo "  查看实时日志: docker compose logs -f"
echo "  停止服务: docker compose down"
echo "  重启服务: docker compose restart"
echo "  查看服务状态: docker compose ps"