#!/usr/bin/env bash
# Deploy a Gossip index node to a Debian/Ubuntu server you own.
#   IGIRL_HOST=user@your.server IGIRL_SSH_KEY=~/.ssh/your_key ./deploy/deploy-index.sh
# Installs to /opt/internet-girl, runs as a hardened DynamicUser systemd service, opens tcp/7700.
set -euo pipefail
HOST="${IGIRL_HOST:?set IGIRL_HOST=user@server}"
KEY="${IGIRL_SSH_KEY:-$HOME/.ssh/id_ed25519}"
cd "$(dirname "$0")/.."
rsync -a --delete -e "ssh -i $KEY" --exclude .venv --exclude .git --exclude '*.egg-info' --exclude __pycache__ ./ "$HOST:/tmp/internet-girl/"
ssh -i "$KEY" "$HOST" 'set -e
dpkg -s python3-venv >/dev/null 2>&1 || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv
sudo rm -rf /opt/internet-girl && sudo mv /tmp/internet-girl /opt/internet-girl && sudo chown -R root:root /opt/internet-girl
sudo python3 -m venv /opt/internet-girl/.venv
sudo /opt/internet-girl/.venv/bin/pip install -q /opt/internet-girl
sudo install -m 644 /opt/internet-girl/deploy/igirl-index.service /etc/systemd/system/igirl-index.service
sudo systemctl daemon-reload
sudo systemctl enable --now igirl-index
sudo systemctl restart igirl-index
sudo ufw allow 7700/tcp comment "gossip index"
sleep 2; systemctl is-active igirl-index; sudo journalctl -u igirl-index -n 5 --no-pager'
echo
echo "Point a DNS name at the server (if it's behind Cloudflare: DNS only / grey cloud — the proxy can't carry Gossip),"
echo "then test:  igirl read your.index.host:7700"
