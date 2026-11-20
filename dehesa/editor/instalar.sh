#!/bin/bash
# Instala o editor da loja Dehesa e publica-o no Caddy. Correr com: sudo bash instalar.sh
set -e
D=/home/balta/dehesa/editor
HOST=dehesa-editor.91.99.163.28.sslip.io
cp $D/dehesa-editor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable dehesa-editor
systemctl restart dehesa-editor
F=/etc/caddy/Caddyfile
if ! grep -q "$HOST" $F; then
  cp $F $F.bak-$(date +%Y%m%d%H%M%S)
  printf '\n%s {\n    reverse_proxy localhost:5056\n}\n' "$HOST" >> $F
fi
caddy validate --config $F --adapter caddyfile
systemctl reload caddy
echo "OK -> https://$HOST/_admin"
