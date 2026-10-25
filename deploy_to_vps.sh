#!/bin/bash
set -e

VPS_HOST="vps-gateway.sorahost.net"
VPS_PORT="62932"
VPS_USER="rds9"
SSH_KEY="/home/yuto0926space/id_ed25519_rds9"
REMOTE_DIR="/home/rds9/bots/worldwarbot"

echo "=== Packaging World War Bot ==="
ARCHIVE="/tmp/worldwarbot_deploy.tar.gz"
tar --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='data/*.db' --exclude='.env' -czf "$ARCHIVE" -C /home/yuto0926space/worldwarbot .

echo "=== Uploading to VPS ==="
scp -i "$SSH_KEY" -P "$VPS_PORT" "$ARCHIVE" "$VPS_USER@$VPS_HOST:/tmp/worldwarbot.tar.gz"

echo "=== Deploying and restarting on VPS ==="
ssh -i "$SSH_KEY" -p "$VPS_PORT" "$VPS_USER@$VPS_HOST" << 'REMOTE_SCRIPT'
set -e
mkdir -p /home/rds9/bots/worldwarbot
cd /home/rds9/bots/worldwarbot
tar -xzf /tmp/worldwarbot.tar.gz -C .
rm -f /tmp/worldwarbot.tar.gz

# PM2で再起動
pm2 restart worldwarbot || pm2 start ecosystem.config.js
pm2 save
echo "=== Deployment finished successfully ==="
REMOTE_SCRIPT

rm -f "$ARCHIVE"
