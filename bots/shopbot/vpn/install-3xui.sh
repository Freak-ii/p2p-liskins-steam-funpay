#!/usr/bin/env bash
# DPB Fastchange: свой VPN-сервер за одну команду.
# Ставит панель 3x-ui (Xray), включает VLESS Reality на 443 и подписки для Happ, закрывает лишние порты,
# включает BBR и печатает блок настроек для ShopBot (services.panels).
#
# Запуск на чистом Ubuntu 22.04/24.04 или Debian 12 от root:
#   bash install-3xui.sh nl1            # имя сервера: nl1, de1, fi1 …
# Повторный запуск не ломает работающий сервер: если панель уже стоит, скрипт только печатает настройки.
set -euo pipefail

NAME="${1:-nl1}"
SNI="${SNI:-www.microsoft.com}"          # сайт, под который маскируется Reality (TLS 1.3 + H2)
SUB_PORT="${SUB_PORT:-2096}"
XUI_VERSION="${XUI_VERSION:-}"            # пусто — последняя версия 3x-ui; можно закрепить, например v2.6.0
STATE=/root/dpb-vpn.env

[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

if [ ! -f "$STATE" ]; then
  echo "== 1/6 пакеты"
  apt-get update -y >/dev/null
  apt-get install -y curl openssl jq ufw ca-certificates >/dev/null

  echo "== 2/6 3x-ui"
  # установщик спрашивает, менять ли порт панели — отвечаем «нет», ниже ставим свои значения
  printf 'n\n' | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) $XUI_VERSION

  echo "== 3/6 доступ к панели"
  XUSER="dpb$(openssl rand -hex 3)"
  XPASS="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
  XPORT="$((20000 + RANDOM % 30000))"
  XPATH="/$(openssl rand -hex 8)/"
  SUBPATH="/$(openssl rand -hex 6)/"
  /usr/local/x-ui/x-ui setting -username "$XUSER" -password "$XPASS" -port "$XPORT" -webBasePath "$XPATH" >/dev/null
  IP="$(curl -s4 --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')"
  mkdir -p /root/cert
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$IP" \
    -keyout /root/cert/panel.key -out /root/cert/panel.crt >/dev/null 2>&1
  /usr/local/x-ui/x-ui cert -webCert /root/cert/panel.crt -webCertKey /root/cert/panel.key >/dev/null
  x-ui restart >/dev/null 2>&1 || systemctl restart x-ui
  sleep 4

  BASE="https://127.0.0.1:${XPORT}${XPATH%/}"
  JAR="$(mktemp)"
  curl -sk -c "$JAR" --data-urlencode "username=$XUSER" --data-urlencode "password=$XPASS" "$BASE/login" \
    | jq -e '.success' >/dev/null || { echo "Не вошёл в панель"; exit 1; }

  echo "== 4/6 VLESS Reality на 443"
  XRAY="$(ls /usr/local/x-ui/bin/xray-linux-* | head -1)"
  KEYS="$("$XRAY" x25519)"
  PRIV="$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /private/ {print $2}')"
  PUB="$(echo "$KEYS" | awk -F': *' 'tolower($1) ~ /public|password/ {print $2}')"
  SID="$(openssl rand -hex 8)"
  SETTINGS="$(jq -nc '{clients:[],decryption:"none",fallbacks:[]}')"
  STREAM="$(jq -nc --arg pk "$PRIV" --arg pub "$PUB" --arg sni "$SNI" --arg sid "$SID" '{
    network:"tcp", security:"reality", externalProxy:[],
    realitySettings:{show:false, xver:0, dest:($sni+":443"), serverNames:[$sni], privateKey:$pk,
                     minClient:"", maxClient:"", maxTimediff:0, shortIds:[$sid],
                     settings:{publicKey:$pub, fingerprint:"chrome", serverName:"", spiderX:"/"}},
    tcpSettings:{acceptProxyProtocol:false, header:{type:"none"}}}')"
  SNIFF="$(jq -nc '{enabled:true,destOverride:["http","tls","quic"],metadataOnly:false,routeOnly:false}')"
  BODY="$(jq -nc --arg s "$SETTINGS" --arg st "$STREAM" --arg sn "$SNIFF" --arg r "DPB-$NAME" '{
    up:0, down:0, total:0, remark:$r, enable:true, expiryTime:0, listen:"", port:443, protocol:"vless",
    settings:$s, streamSettings:$st, sniffing:$sn}')"
  INBOUND="$(curl -sk -b "$JAR" -H 'Content-Type: application/json' -d "$BODY" "$BASE/panel/api/inbounds/add" \
    | jq -er '.obj.id')"

  echo "== 5/6 подписки для Happ"
  ALL="$(curl -sk -b "$JAR" -X POST "$BASE/panel/setting/all" | jq -c '.obj')"
  NEW="$(echo "$ALL" | jq -c --arg p "$SUBPATH" --argjson port "$SUB_PORT" --arg t "DPB Fastchange" \
    '.subEnable=true | .subPort=$port | .subPath=$p | .subTitle=$t | .subUpdates=12')"
  curl -sk -b "$JAR" -H 'Content-Type: application/json' -d "$NEW" "$BASE/panel/setting/update" \
    | jq -e '.success' >/dev/null || echo "!! подписки не включились — включите в панели: Настройки → Подписка"
  rm -f "$JAR"
  x-ui restart >/dev/null 2>&1 || systemctl restart x-ui

  echo "== 6/6 сеть: BBR и файрвол"
  grep -q 'tcp_congestion_control=bbr' /etc/sysctl.conf || \
    printf 'net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n' >> /etc/sysctl.conf
  sysctl -p >/dev/null 2>&1 || true
  SSH_PORT="$(ss -tlnp | awk '/sshd/ {split($4,a,":"); print a[length(a)]; exit}')"
  ufw allow "${SSH_PORT:-22}/tcp" >/dev/null
  ufw allow 443/tcp >/dev/null
  ufw allow "$XPORT/tcp" >/dev/null
  ufw allow "$SUB_PORT/tcp" >/dev/null
  ufw --force enable >/dev/null

  umask 077
  cat > "$STATE" <<ENV
NAME=$NAME
IP=$IP
XUSER=$XUSER
XPASS=$XPASS
XPORT=$XPORT
XPATH=$XPATH
SUBPATH=$SUBPATH
INBOUND=$INBOUND
ENV
fi

# shellcheck disable=SC1090
. "$STATE"
cat <<OUT

================= ГОТОВО: сервер $NAME ($IP) =================
Пришлите в чат блок ниже целиком (это пароль панели — больше никому):

  - name: $NAME
    kind: 3xui
    url: https://$IP:$XPORT${XPATH%/}
    username: $XUSER
    password: $XPASS
    inbound_id: $INBOUND
    host: $IP
    sub_base: http://$IP:$SUB_PORT${SUBPATH%/}
    verify_tls: false

Панель в браузере: https://$IP:$XPORT$XPATH (сертификат самоподписанный — браузер предупредит, это нормально).
=================================================================
OUT
