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

# 2. Install Docker
if ! command -v docker &> /dev/null; then
    echo "--- Installing Docker ---"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker $USER
    echo "Docker installed. You need to log out and back in for group changes."
else
    echo "Docker already installed."
fi

# 3. Install Docker Compose plugin
if ! docker compose version &> /dev/null 2>&1; then
    echo "--- Installing Docker Compose ---"
    sudo apt-get update && sudo apt-get install -y docker-compose-plugin 2>/dev/null || \
    sudo yum install -y docker-compose-plugin 2>/dev/null || \
    echo "Please install docker-compose-plugin manually."
else
    echo "Docker Compose already installed."
fi

# 4. Set timezone
echo "--- Setting timezone to Asia/Tokyo ---"
sudo timedatectl set-timezone Asia/Tokyo

# 5. Create config from example
if [ -f config/default.example.json ] && [ ! -f config/default.json ]; then
    echo "--- Creating config from example ---"
    cp config/default.example.json config/default.json
    echo "Created config/default.json — edit it to add your credentials:"
    echo "  nano config/default.json"
fi

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
