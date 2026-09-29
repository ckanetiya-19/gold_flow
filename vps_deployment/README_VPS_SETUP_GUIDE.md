# 🚀 GOLDFLOW 24/7 CLOUD VPS SETUP & DEPLOYMENT GUIDE
### સંપૂર્ણ ગુજરાતી & ENGLISH માર્ગદર્શિકા (Comprehensive Step-by-Step Guide)

---

## 📌 ગુજરાતીમાં ઝડપી માર્ગદર્શિકા (Gujarati Quick Instructions)

આ પેકેજની મદદથી તમે તમારું સંપૂર્ણ **GoldFlow Trading System** કોઈપણ Linux Cloud VPS (જેમ કે Hostinger, DigitalOcean, Hetzner, AWS, વગેરે - Ubuntu 20.04/22.04/24.04 LTS) પર માત્ર ૧ જ કમાન્ડથી ૨૪/૭ લાઇવ ચાલુ કરી શકો છો!

---

### પગલું ૧: VPS ખરીદ્યા પછી શું કરવું? (VPS Setup)
- તમારા VPS પ્રોવાઇડર પાસેથી મળેલ **IP Address**, **Username** (સામાન્ય રીતે `root`), અને **Password / SSH Key** તૈયાર રાખો.

### પગલું ૨: ફાઇલ અપલોડ કરો (Upload Package to VPS)
તમે **FileZilla** અથવા **Command Prompt (SCP)** દ્વારા આ `GOLDFLOW_VPS_DEPLOYMENT_PACKAGE.zip` ફાઇલ VPS પર અપલોડ કરો:

**Windows Terminal / PowerShell માથી અપલોડ કરવા માટે:**
```bash
scp C:\Users\ckane\Desktop\GOLDFLOW_VPS_DEPLOYMENT_PACKAGE.zip root@YOUR_VPS_IP:/root/
```
*(અથવા FileZilla સોફ્ટવેરથી Drag & Drop કરીને `/root/` ફોલ્ડરમાં મુકી દો)*

---

### પગલું ૩: VPS માં લોગિન કરો (Login to VPS)
તમારા કમ્પ્યુટરમાં PowerShell અથવા Command Prompt ખોલીને આ લખો:
```bash
ssh root@YOUR_VPS_IP
```
(પાસવર્ડ પૂછે ત્યારે VPS નો પાસવર્ડ નાખી એન્ટર આપો)

---

### પગલું ૪: અનઝિપ કરીને ઇન્સ્ટોલ કરો (Unzip & 1-Click Install)
VPS ના ટર્મિનલમાં માત્ર આ ૩ કમાન્ડ લાઇન ચલાવો:
```bash
apt-get update && apt-get install -y unzip
unzip /root/GOLDFLOW_VPS_DEPLOYMENT_PACKAGE.zip -d /root/goldflow_install
cd /root/goldflow_install
bash deploy_vps_master.sh
```

**બસ! માત્ર ૬૦ સેકન્ડમાં આખું સિસ્ટમ ઓટોમેટિક ઇન્સ્ટોલ થઈ જશે!**
- બધા પાયથોન પેકેજ ઇન્સ્ટોલ થઈ જશે.
- ૬ અલગ-અલગ બેકગ્રાઉન્ડ સિસ્ટમ સર્વિસ (Systemd Daemons) ચાલુ થઈ જશે.
- Nginx Reverse Proxy અને ફાયરવોલ (Firewall) સેટ થઈ જશે.

---

### પગલું ૫: બ્રાઉઝરમાં કેવી રીતે ઓપન કરવું? (Access Live Dashboards)

કોઈપણ બ્રાઉઝર (Chrome / Safari / Mobile) માં આ લિંક્સ ઓપન કરો:

| Port | ડેશબોર્ડનું નામ | URL લિંક | વિગત |
| :--- | :--- | :--- | :--- |
| **80** | **Master Terminal** | `http://YOUR_VPS_IP/` | મુખ્ય ટર્મિનલ (ડિફોલ્ટ 8070) |
| **8070** | **Bloomberg Terminal** | `http://YOUR_VPS_IP:8070` | Pure Spot 1M VWAP Cross & Retest |
| **8080** | **AI Quant Sniper** | `http://YOUR_VPS_IP:8080` | Multi-Layer Algorithmic Engine |
| **8088** | **Live Consensus Engine** | `http://YOUR_VPS_IP:8088` | Model A + Model B + 5M Bar Lock |
| **8090** | **Trinity Liquidity Radar** | `http://YOUR_VPS_IP:8090` | Institutional Sessions & Heatmap |
| **8095** | **Master Stream Terminal** | `http://YOUR_VPS_IP:8095` | Low-Latency Real-Time Stream |
| **8086** | **Confluence Backtester** | `http://YOUR_VPS_IP:8086` | Historical Strategy Simulator |

🔐 **લૉગિન વિગતો (Default Login Credentials):**
- **Username:** `am`
- **Password:** `Orferflow@1910`

*(પાસવર્ડ બદલવા માટે VPS માં આ કમાન્ડ લખો: `sudo htpasswd -b /etc/nginx/.goldflow_htpasswd am નવો_પાસવર્ડ`)*

---

### જરૂરી કમાન્ડ્સ (Useful Control Commands):

- **બધા પોર્ટ્સ ચાલુ/બંધ/રીસ્ટાર્ટ કરવા:**
  ```bash
  systemctl restart goldflow-8070 goldflow-8080 goldflow-8088 goldflow-8095 goldflow-8090 goldflow-8086
  ```

- **કોઈપણ પોર્ટનું લાઇવ સ્ટેટસ જોવા:**
  ```bash
  systemctl status goldflow-8070
  ```

- **કોઈપણ પોર્ટનો લાઇવ ટ્રેડ લોગ જોવા (Live Terminal Stream):**
  ```bash
  journalctl -u goldflow-8070 -f
  journalctl -u goldflow-8088 -f
  ```

---

## 🌐 ENGLISH TECHNICAL OVERVIEW

### Architecture & Service Isolation
1. **Isolated Clusters**:
   - `/opt/goldflow/8080_8070/`: Houses Ports 8070 & 8080 with dedicated AllTick credentials and historical candle SQLite databases (`trades_terminal.db`, `trades_quant.db`).
   - `/opt/goldflow/8088_8095/`: Houses Ports 8088 & 8095 with dedicated feeds and isolated stream DB (`trades_stream_8095.db`).
   - `/opt/goldflow/radar_8090/`: Port 8090 Unified Trinity Radar.
   - `/opt/goldflow/backtest_8086/`: Port 8086 Strategy Backtester.

2. **Daemon Management (systemd)**:
   - Each port runs under its own supervised daemon (`goldflow-*.service`).
   - If any script crashes or encounters an unexpected feed drop, systemd will automatically restart it within 5 seconds.

3. **Nginx Reverse Proxy & Security**:
   - HTTP Basic Authentication (`.goldflow_htpasswd`) prevents unauthorized public access.
   - WebSocket headers (`Upgrade`, `Connection: upgrade`) are fully proxied with 24-hour persistent keep-alive (`proxy_read_timeout 86400`).
   - UFW firewall restricts traffic to SSH (22), HTTP (80), HTTPS (443), and designated trading ports.

4. **Pure Spot Data Feeds & Candle Integrity**:
   - Spot candles are generated solely from authentic institutional feeds (AllTick WebSocket, TwelveData, RealMarket).
   - MT5 broker ticks are never mixed into candle formation.
