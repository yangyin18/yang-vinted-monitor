#!/usr/bin/env bash
# Vinted 多账号监控 一键部署脚本(在服务器上以 root 运行)
#
# 用法:
#   1. 在本机把代码上传到服务器:
#        scp monitor.py clash_pool.py server.py server_config.json \
#            accounts.txt proxy_pool.txt requirements-server.txt \
#            root@<服务器IP>:/tmp/vinted-monitor/
#   2. SSH 登录服务器后运行:
#        bash /tmp/vinted-monitor/deploy/deploy.sh
#
# 注意:proxy_pool.txt 请先换成住宅代理池(每行一个 http://user:pass@host:port),
#       服务器上没有 Clash,机场节点(clash: 条目)会被 server.py 自动过滤。

set -e

SRC="${1:-/tmp/vinted-monitor}"
DST="/opt/vinted-monitor"
SERVICE="deploy/vinted-monitor.service"

echo "==> 1/6 安装系统依赖"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl ufw

echo "==> 2/6 复制代码到 $DST"
mkdir -p "$DST"
cp -r "$SRC"/. "$DST"/
[ -f "$DST/$SERVICE" ] && cp "$SRC/$SERVICE" /etc/systemd/system/vinted-monitor.service

echo "==> 3/6 创建虚拟环境并装依赖"
python3 -m venv "$DST/venv"
"$DST/venv/bin/pip" install --upgrade pip
"$DST/venv/bin/pip" install -r "$DST/requirements-server.txt"
"$DST/venv/bin/python" -c "import curl_cffi, fastapi, uvicorn; print('deps OK')"

echo "==> 4/5 注册并启动 systemd 服务"
systemctl daemon-reload
systemctl enable vinted-monitor
systemctl restart vinted-monitor

echo "==> 5/5 防火墙放行(SSH + API)"
ufw allow 22/tcp
ufw allow 8000/tcp
ufw --force enable || true

echo
echo "部署完成。检查日志: journalctl -u vinted-monitor -f"
echo "健康检查:   curl http://127.0.0.1:8000/healthz"
echo "客户端连接: http://<服务器IP>:8000 (无密钥)"
