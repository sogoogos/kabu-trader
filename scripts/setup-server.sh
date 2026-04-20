#!/bin/bash
# Server setup script for Kabu Trader on Oracle Cloud (or any Linux server)
# Usage: ssh into your server and run:
#   curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/kabu-trader/main/scripts/setup-server.sh | bash
# Or clone the repo first and run: bash scripts/setup-server.sh

set -e

echo "=== Kabu Trader Server Setup ==="

# 1. Swap (for 1GB RAM servers)
if [ ! -f /swapfile ]; then
    echo "--- Adding 2GB swap ---"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "Swap added."
else
    echo "Swap already exists, skipping."
fi

# Detect distro (Amazon Linux needs special handling — get.docker.com rejects 'amzn')
DISTRO_ID=""
if [ -f /etc/os-release ]; then
    DISTRO_ID=$(. /etc/os-release && echo "$ID")
fi

# 2. Install Docker
if ! command -v docker &> /dev/null; then
    echo "--- Installing Docker ---"
    if [ "$DISTRO_ID" = "amzn" ]; then
        sudo yum install -y docker
        sudo systemctl enable --now docker
    else
        curl -fsSL https://get.docker.com | sudo sh
        sudo systemctl enable --now docker 2>/dev/null || true
    fi
    sudo usermod -aG docker $USER
    echo "Docker installed. You need to log out and back in for group changes."
else
    echo "Docker already installed."
fi

# 3. Install Docker Compose plugin
if ! docker compose version &> /dev/null 2>&1; then
    echo "--- Installing Docker Compose ---"
    if [ "$DISTRO_ID" = "amzn" ]; then
        # Amazon Linux: no docker-compose-plugin package — install the plugin binary manually
        COMPOSE_VERSION="v2.29.7"
        ARCH=$(uname -m)
        sudo mkdir -p /usr/libexec/docker/cli-plugins
        sudo curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
            -o /usr/libexec/docker/cli-plugins/docker-compose
        sudo chmod +x /usr/libexec/docker/cli-plugins/docker-compose
    else
        sudo apt-get update && sudo apt-get install -y docker-compose-plugin 2>/dev/null || \
        sudo yum install -y docker-compose-plugin 2>/dev/null || \
        echo "Please install docker-compose-plugin manually."
    fi
else
    echo "Docker Compose already installed."
fi

# 4. Set timezone
echo "--- Setting timezone to Asia/Tokyo ---"
sudo timedatectl set-timezone Asia/Tokyo

# 5. Create configs from examples (one per market)
for market in default us; do
    example="config/${market}.example.json"
    target="config/${market}.json"
    if [ -f "$example" ] && [ ! -f "$target" ]; then
        echo "--- Creating $target from example ---"
        cp "$example" "$target"
        echo "Created $target — edit it to add your credentials:"
        echo "  nano $target"
    fi
done

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Log out and back in (for Docker group): exit && ssh ..."
echo "  2. Edit config:  nano config/default.json"
echo "     - Set LINE credentials"
echo "     - Set OpenAI API key (optional)"
echo "  3. Train ML model:  docker compose run --rm kabu-trader train"
echo "  4. Start monitoring: docker compose up -d"
echo "  5. Check logs:       docker compose logs -f"
echo "  6. Check report:     docker compose exec kabu-trader python -m kabu_trader.cli report"
