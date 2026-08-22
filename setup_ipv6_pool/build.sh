#!/bin/bash
# IPv6 SOCKS5 pool builder for 3proxy 0.9.8
# Reads config from /root/ipv6pool/pool.conf
set -euo pipefail

CONF=/root/ipv6pool/pool.conf
[ -f "$CONF" ] || { echo "missing $CONF"; exit 1; }
source "$CONF"

IFACE=${IFACE:-ens3}
BASEPORT=${BASEPORT:-1000}
COUNT=${COUNT:-1000}

# 1. ensure-addresses helper (idempotent, used by build AND systemd ExecStartPre)
cat > /root/ipv6pool/add_addresses.sh <<HELP
#!/bin/bash
CONF=/root/ipv6pool/pool.conf
[ -f "\$CONF" ] || exit 0
source "\$CONF"
IFACE=\${IFACE:-ens3}; COUNT=\${COUNT:-1000}
for ((i=1; i<=COUNT; i++)); do
  hex=\$(printf '%x' "\$i")
  ip -6 addr add "\${PREFIX}\${hex}" dev "\$IFACE" nodad 2>/dev/null || true
done
HELP
chmod +x /root/ipv6pool/add_addresses.sh
# assign now
bash /root/ipv6pool/add_addresses.sh

# 2. generate 3proxy config
CFG=/etc/3proxy/3proxy.cfg
{
  echo "log /var/log/3proxy/3proxy.log"
  echo 'logformat "%d.%m.%Y %H:%M:%S %N %p %E %U %C:%c %R:%r %O %I %h %T"'
  echo "rotate 30"
  echo "nserver 8.8.8.8"
  echo "nserver 1.1.1.1"
  echo "nscache 65536"
  echo "auth strong"
  echo "users ${USER}:CL:${PASS}"
  echo "service"
  for ((i=1; i<=COUNT; i++)); do
    hex=$(printf '%x' "$i")
    port=$((BASEPORT + i - 1))
    echo "socks -p${port} -i0.0.0.0 -e${PREFIX}${hex} -64"
  done
} > "$CFG"

# 3. systemd unit
cat > /etc/systemd/system/3proxy.service <<'UNIT'
[Unit]
Description=3proxy IPv6 SOCKS5 pool
After=network.target

[Service]
Type=simple
ExecStartPre=/root/ipv6pool/add_addresses.sh
ExecStart=/usr/local/bin/3proxy /etc/3proxy/3proxy.cfg
Restart=always
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p /var/log/3proxy
systemctl daemon-reload
systemctl enable 3proxy >/dev/null 2>&1 || true
systemctl reset-failed 3proxy 2>/dev/null || true
systemctl restart 3proxy
sleep 1
echo "OK: ${COUNT} SOCKS5 proxies on ${HOST}:${BASEPORT}-$((BASEPORT+COUNT-1)) user=${USER}"
