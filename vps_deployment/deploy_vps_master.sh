#!/usr/bin/env bash
# =============================================================================
#  ? GOLDFLOW INSTITUTIONAL TRADING SYSTEM - VPS 1-CLICK MASTER DEPLOYMENT ?
#  Target OS: Ubuntu 20.04 / 22.04 / 24.04 LTS or Debian 11/12
#  Microservices: Ports 8070, 8080, 8086, 8088, 8090, 8095
# =============================================================================

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo -e "${CYAN}${BOLD}"
echo "==============================================================================="
echo "        ? GOLDFLOW 24/7 CLOUD VPS AUTOMATED DEPLOYMENT SEQUENCE ?"
echo "==============================================================================="
echo -e "${NC}"

# Check for root privilege
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Please run this script as root: sudo bash deploy_vps_master.sh${NC}"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/opt/goldflow"

echo -e "${YELLOW}[1/7] Updating APT package repositories and installing prerequisites...${NC}"
apt-get update -y
apt-get install -y python3 python3-pip python3-venv nginx apache2-utils curl ufw sqlite3 git

echo -e "${YELLOW}[2/7] Creating dedicated service user 'goldflow'...${NC}"
if ! id "goldflow" &>/dev/null; then
    useradd -r -s /bin/false goldflow
    echo -e "${GREEN}? Created system user: goldflow${NC}"
else
    echo -e "${GREEN}? User 'goldflow' already exists.${NC}"
fi

echo -e "${YELLOW}[3/7] Copying GoldFlow microservices into ${TARGET_DIR}...${NC}"
mkdir -p "${TARGET_DIR}"
cp -r "${SCRIPT_DIR}/8080_8070" "${TARGET_DIR}/"
cp -r "${SCRIPT_DIR}/8088_8095" "${TARGET_DIR}/"
cp -r "${SCRIPT_DIR}/radar_8090" "${TARGET_DIR}/"
cp -r "${SCRIPT_DIR}/backtest_8086" "${TARGET_DIR}/"
cp "${SCRIPT_DIR}/requirements_vps.txt" "${TARGET_DIR}/requirements.txt"

echo -e "${YELLOW}[4/7] Setting up Python virtual environment and installing wheels...${NC}"
if [ ! -d "${TARGET_DIR}/venv" ]; then
    python3 -m venv "${TARGET_DIR}/venv"
fi
"${TARGET_DIR}/venv/bin/pip" install --upgrade pip
"${TARGET_DIR}/venv/bin/pip" install -r "${TARGET_DIR}/requirements.txt"

# Set file permissions
chown -R goldflow:goldflow "${TARGET_DIR}"
chmod -R 755 "${TARGET_DIR}"
chmod -R 664 "${TARGET_DIR}"/*/*.db 2>/dev/null || true

echo -e "${YELLOW}[5/7] Installing Systemd microservice daemons...${NC}"
cp "${SCRIPT_DIR}/systemd/"*.service /etc/systemd/system/
systemctl daemon-reload

SERVICES=("goldflow-8070" "goldflow-8080" "goldflow-8088" "goldflow-8095" "goldflow-8090" "goldflow-8086")
for svc in "${SERVICES[@]}"; do
    systemctl enable "${svc}"
    systemctl restart "${svc}"
    echo -e "${GREEN}? Enabled & Started: ${svc}${NC}"
done

echo -e "${YELLOW}[6/7] Configuring Nginx Reverse Proxy with Basic Authentication...${NC}"
HTPASSWD_FILE="/etc/nginx/.goldflow_htpasswd"
if [ ! -f "${HTPASSWD_FILE}" ]; then
    DEFAULT_PASS="goldflow123"
    htpasswd -bc "${HTPASSWD_FILE}" admin "${DEFAULT_PASS}"
    echo -e "${GREEN}? Created default credentials: Username: admin | Password: ${DEFAULT_PASS}${NC}"
    echo -e "${CYAN}  (You can change password later using: sudo htpasswd -b /etc/nginx/.goldflow_htpasswd admin <NewPassword>)${NC}"
else
    echo -e "${GREEN}? Existing htpasswd credentials preserved.${NC}"
fi

cp "${SCRIPT_DIR}/nginx/goldflow_vps.conf" /etc/nginx/sites-available/goldflow.conf
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/goldflow.conf /etc/nginx/sites-enabled/goldflow.conf

nginx -t
systemctl restart nginx
echo -e "${GREEN}? Nginx reverse proxy configured and restarted successfully.${NC}"

echo -e "${YELLOW}[7/7] Configuring UFW Firewall rules (SSH + HTTP + Ports)...${NC}"
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'GoldFlow HTTP'
ufw allow 443/tcp comment 'GoldFlow HTTPS'
ufw allow 8070/tcp comment 'Bloomberg 8070'
ufw allow 8080/tcp comment 'Quant 8080'
ufw allow 8086/tcp comment 'Backtest 8086'
ufw allow 8088/tcp comment 'Live Engine 8088'
ufw allow 8090/tcp comment 'Radar 8090'
ufw allow 8095/tcp comment 'Stream 8095'
ufw --force enable
echo -e "${GREEN}? Firewall rules applied and enabled.${NC}"

SERVER_IP=$(curl -s --max-time 3 ifconfig.me || curl -s --max-time 3 icanhazip.com || echo "YOUR_VPS_IP")

echo ""
echo -e "${GREEN}${BOLD}==============================================================================="
echo "                ?? GOLDFLOW VPS DEPLOYMENT COMPLETED SUCCESSFULLY!"
echo "===============================================================================${NC}"
echo ""
echo -e "${BOLD}Your Live Trading Terminals are now active 24/7:${NC}"
echo -e "  ?? Master Terminal (Port 80):   ${CYAN}http://${SERVER_IP}/${NC}"
echo -e "  ?? Bloomberg Terminal (8070):  ${CYAN}http://${SERVER_IP}:8070/${NC}"
echo -e "  ?? AI Quant Sniper (8080):     ${CYAN}http://${SERVER_IP}:8080/${NC}"
echo -e "  ? Live Consensus Engine (8088): ${CYAN}http://${SERVER_IP}:8088/${NC}"
echo -e "  ?? Unified Trinity Radar (8090): ${CYAN}http://${SERVER_IP}:8090/${NC}"
echo -e "  ?? Master Stream (8095):        ${CYAN}http://${SERVER_IP}:8095/${NC}"
echo -e "  ?? Confluence Backtester (8086): ${CYAN}http://${SERVER_IP}:8086/${NC}"
echo ""
echo -e "${BOLD}Default Authentication:${NC}"
echo -e "  Username: ${YELLOW}admin${NC}"
echo -e "  Password: ${YELLOW}goldflow123${NC}"
echo ""
echo -e "${BOLD}Useful Management Commands:${NC}"
echo -e "  * Check service status:   ${CYAN}systemctl status goldflow-8070${NC}"
echo -e "  * View real-time logs:    ${CYAN}journalctl -u goldflow-8070 -f${NC}"
echo -e "  * Restart all services:   ${CYAN}systemctl restart goldflow-8070 goldflow-8080 goldflow-8088 goldflow-8095 goldflow-8090 goldflow-8086${NC}"
echo -e "  * Change password:        ${CYAN}sudo htpasswd -b /etc/nginx/.goldflow_htpasswd admin <NewPassword>${NC}"
echo "==============================================================================="
