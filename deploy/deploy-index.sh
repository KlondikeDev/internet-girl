#!/usr/bin/env bash
# Deploy the Gossip index node to the kunix.org VPS. Run from the repo root: ./deploy/deploy-index.sh
# Installs to /opt/internet-girl, runs as a hardened DynamicUser systemd service, opens tcp/7700.
set -euo pipefail
HOST=debian@40.160.138.68
KEY="$HOME/.ssh/id_ed25519_kunix_mail"
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
echo "Now add DNS in Cloudflare (DNS only / grey cloud — the proxy can't carry Gossip):"
echo "  index.kunix.org  A     40.160.138.68"
echo "  index.kunix.org  AAAA  2604:2dc0:222::11cf"
echo "Then test:  igirl read index.kunix.org:7700"
