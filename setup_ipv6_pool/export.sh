#!/bin/bash
# usage: export.sh [count]   -> prints `count` socks5h:// lines, each a unique IPv6
# 标准格式 socks5h://user:pass@host:port —— curl / curl_cffi / BitBrowser 都能直接认。
# (旧格式 socks5://host:port:user:pass 只有少数自家面板认,curl 会报 Unsupported proxy syntax)
CONF=/root/ipv6pool/pool.conf
source "$CONF"
COUNT=${1:-${COUNT:-1000}}
BASEPORT=${BASEPORT:-1000}
for ((i=1; i<=COUNT; i++)); do
  port=$((BASEPORT + i - 1))
  echo "socks5h://${USER}:${PASS}@${HOST}:${port}"
done
