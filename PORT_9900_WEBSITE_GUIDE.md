# Port 9900 → Website banavva ni Complete Guide

> Goal: Aapdo **iTick Real Spot Order-Flow Terminal (Port 9900)** ne local `127.0.0.1:9900`
> mathi ek **public website** (da.t. `orderflow.tumaru-domain.com`) ma convert karvu — jyaan
> tame ya biju koi browser mathi open kari sako, phone par pan.

---

## 0. Pela AA samjho — 9900 atyare kem chale che

Aapdo terminal 2 bhaag no che (ek j Python file ma):

```
┌─────────────────────────────────────────────────────────────┐
│  PORT_9900_ITICK_ORDERFLOW_TERMINAL.py  (FastAPI + uvicorn)  │
│                                                               │
│  [A] BACKEND (server-side, hamesha chalvu joye)               │
│      • iTick WebSocket loop  → wss://api-free.itick.org       │
│      • real trades aave → candles/CVD/DOM/tape calculate      │
│      • SQLite ma save (orderflow_9900.db) → restart-safe      │
│      • REST endpoints:  /api/chart_data  /api/dom             │
│                         /api/heatmap     /api/tape            │
│                                                               │
│  [B] FRONTEND (browser)                                       │
│      • "/" par 1 HTML page (Lightweight Charts + JS)          │
│      • dar ~1 second e endpoints ne fetch kare → screen update│
└─────────────────────────────────────────────────────────────┘
```

**Key point:** iTick WebSocket **server par** chale che, browser ma nai. Etle "website"
banavva mate tame frontend ne alag nahi kaadhi shako — backend hamesha koik jagya e
24/7 chalvu joye. AA j sauthi mahatv ni vaat che.

---

## 1. Sauthi pela ek j decision lyo

| Option | Kone joyu? | Mehnat | Kharcho |
|--------|-----------|--------|--------|
| **A. Fakt tamare j joyu che** (phone/laptop par) | Private link | Ochu | ~$5/mo VPS ya free tier |
| **B. Bija loko ne pan batavvu che** (public product) | Public + login | Vadhu | $5–20/mo + domain |

Meri **recommendation:** shuruaat ma **Option A** karo (same code, just cloud par mukho).
Kaam kare pachi j Option B (login/users/billing) ma jaao. Niche banne aapelu che.

---

## 2. Sauthi moti 3 chetavani (AA na samjho to fasi jaso)

1. **API key kadi frontend ma na naakho.**
   iTick key (`GOLDFLOW_9900_ITICK_KEY`) fakt **server env var** ma rehvi joye. Aapdo
   current design saru che — browser ne fakt calculated JSON male che, key nai. AA j
   rakhvu. GitHub par push karo to `.env` ne `.gitignore` ma naakho.

2. **1 j process (single worker).**
   iTick WebSocket + in-memory state 1 j process ma che. Gunicorn/uvicorn ne
   **`--workers 1`** j rakhvu, nahi to har worker no potano alag CVD/candles thashe →
   data mismatch. Scale joye to "worker ne alag karo" (niche §6).

3. **Process 24/7 chalvu joye + auto-restart.**
   Local par tame hath thi launch karo cho. Server par e **crash thay to auto-restart**
   thavu joye — `systemd` ya Docker `restart: always` vaparo (niche step ma).

---

## 3. Option A — jem che em cloud par mukho (SAUTHI FAST)

Aa sauthi ochi mehnat: **same Python file**, fakt public server par. 2 rasta:

### 3A. Railway / Render (easiest, no Linux knowledge)

**Steps:**

1. **Code ready karo** — 3 nani files add karo (backend badalvo NAHI):

   `requirements.txt`:
   ```
   fastapi
   uvicorn[standard]
   websockets
   ```

   `Procfile` (Railway/Render aa vaanche):
   ```
   web: python PORT_9900_ITICK_ORDERFLOW_TERMINAL.py
   ```

   `.gitignore`:
   ```
   *.db
   .env
   __pycache__/
   ```

2. **1 nano code-change joyshe** (host + port):
   Current file ma `HOST = "127.0.0.1"` che. Cloud par **`0.0.0.0`** joye ane port
   cloud aape (env var `PORT`). Etlu badlo:
   ```python
   HOST = os.environ.get("HOST", "127.0.0.1")
   PORT = int(os.environ.get("PORT", 9900))
   ```
   (Local par kaain farak nai padto — default 127.0.0.1:9900 j rehshe.)

3. **GitHub par push karo** (private repo rakhjo).

4. **Railway.app** ma → "New Project" → "Deploy from GitHub repo" → aa repo chuno.

5. **Environment variable** set karo dashboard ma:
   ```
   GOLDFLOW_9900_ITICK_KEY = <tamari iTick key>
   HOST = 0.0.0.0
   ```

6. Deploy thay → Railway ek public URL aape (`xxxx.up.railway.app`). Bas, live!

> ⚠️ **SQLite gotcha:** Railway/Render no filesystem **ephemeral** che — redeploy par
> `orderflow_9900.db` udi jashe. Persist joye to Railway "Volume" attach karo, ya
> Postgres par migrate karo (niche §6). Fakt live view joye to vaandho nai — restart
> par iTick thi pachu bharashe.

### 3B. VPS (DigitalOcean / Hetzner / Contabo — ~$5/mo, complete control)

AA best che ek serious 24/7 terminal mate (SQLite pan tiki rahe).

1. **VPS lyo** (Ubuntu 22.04, sauthi nano plan chalse).

2. SSH thi login karo, pachi:
   ```bash
   sudo apt update && sudo apt install -y python3-pip git
   git clone <tamaro-repo>  # ya scp thi file upload karo
   cd <repo>
   pip3 install fastapi "uvicorn[standard]" websockets
   ```

3. Same `HOST=0.0.0.0` change (§3A step 2).

4. **systemd service** banavo — 24/7 chale + crash par auto-restart:
   `/etc/systemd/system/orderflow9900.service`:
   ```ini
   [Unit]
   Description=iTick Order-Flow Terminal 9900
   After=network.target

   [Service]
   WorkingDirectory=/root/<repo>
   Environment=GOLDFLOW_9900_ITICK_KEY=<tamari-key>
   Environment=HOST=0.0.0.0
   ExecStart=/usr/bin/python3 PORT_9900_ITICK_ORDERFLOW_TERMINAL.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
   Pachi:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now orderflow9900
   sudo systemctl status orderflow9900   # chale che ke nai check
   ```

5. **Domain + HTTPS** (Caddy sauthi saral — auto SSL):
   ```bash
   sudo apt install -y caddy
   ```
   `/etc/caddy/Caddyfile`:
   ```
   orderflow.tumaru-domain.com {
       reverse_proxy 127.0.0.1:9900
   }
   ```
   ```bash
   sudo systemctl restart caddy
   ```
   DNS ma A-record → VPS na IP par point karo. Caddy aapo-aap SSL laavshe.
   Have `https://orderflow.tumaru-domain.com` par live! 🎉

---

## 4. Option B — proper "product" website (login + multi-user)

Jyare tame bija loko ne aapvu hoy tyare. Aa vadhu che — 3 layer:

```
Frontend (React/Next.js)  →  Backend API (tamaru 9900, refactored)  →  iTick
   • login page                • 1 shared iTick feed (badha users mate)
   • chart UI                  • per-user auth (JWT)
   • Vercel par host           • WebSocket → browser (polling ne badle)
```

**Mahatva na badlaao:**
- **Polling → WebSocket push:** Aatyare browser dar 1s e fetch kare che. 100 users
  thay to aa bhaare. FastAPI ma ek `@app.websocket("/ws")` add karo je real-time
  data push kare — badha browsers ne ek j broadcast.
- **1 shared feed:** iTick 1 j connection, badha users ne same data. (Per-user
  connection **nai** — iTick free tier limit che.)
- **Auth:** login joye to Supabase/Clerk vaparo (ready-made, free tier).
- Aa point e mane kaho — hu step-by-step refactor kari aapish (aatyare over-engineer
  na karvu; pela Option A chale).

---

## 5. 🤖 READY PROMPT — AI builder (v0 / Lovable / Cursor) ne aapva mate

Jo tame **navesar thi ek modern website frontend** banavvu hoy (v0.dev, Lovable, ya
Cursor jeva AI tool ma), aa prompt **as-is copy-paste** karo. Aa tamara backend na exact
API ne match kare che:

```
Build a professional dark-theme trading terminal web app called
"XAUUSD Real Order-Flow Terminal". It is a pure spot-gold order-flow
dashboard. Use React + Vite + TypeScript, TradingView Lightweight Charts
v4 for the candlestick chart, and plain fetch polling.

THEME (ATAS / Bookmap style):
  --bg:#06080f  --panel:#0c1220  --panel2:#111a2e  --accent:#00d4ff
  --up:#26de81  --down:#ff4d6d   --text:#d8e0ec  --dim:#5b6b85
  --border:#1a2740   font: 'JetBrains Mono', monospace everywhere.

LAYOUT:
  • Top bar: title "XAUUSD // iTICK REAL ORDER-FLOW" (cyan), subtitle
    "iTick tape only · real trades · no fake wicks", right side a green
    "● LIVE · last trade Ns ago" badge that turns red on disconnect.
  • Left column (flex): a candlestick chart (60% height) with a cyan
    VWAP line overlay; an OHLC readout pinned top-left of the chart.
    Below it (34% height) a "CVD / DELTA" panel showing CVD, Bar Delta,
    Bid/Ask, and Absorption (green/red colored).
  • Right column (300px): three cards —
      1. "DOM / DEPTH LADDER (synthetic)": a ladder of ask rows (red) on
         top, a centered cyan "SPOT <price>" band, bid rows (green) below.
         Each row shows price + size + a horizontal volume bar whose width
         is proportional to size (red for asks, green for bids). Footnote:
         "Synthetic (traded-volume) ladder from tape — NOT real resting L2".
      2. "TAPE (TIME & SALES)": scrolling rows: time (dim), side BUY(green)/
         SELL(red), price, size.
      3. "NOT ON SPOT (needs L2/MBO)": grey text listing OBI, Microprice,
         OFI, Liquidity-Pull as unavailable.

DATA — poll these existing backend REST endpoints (base URL from env var
VITE_API_BASE):
  GET /api/chart_data  → { connected:bool, last_trade_sec_ago:number,
       cvd:number, best_bid:number, best_ask:number, absorption:string,
       unavailable:string[], candles:[{time,open,high,low,close,vwap,delta}] }
       poll every 1000ms. Feed candles+vwap to the chart.
  GET /api/dom → { spot:number, note:string,
       asks:[{price,size}], bids:[{price,size}] }  poll every 1000ms.
  GET /api/tape → { tape:[{ts,side,price,size}] }  poll every 800ms;
       show the latest 30.

RULES:
  • Candle up/down uses --up/--down; VWAP line is --accent, width 2.
  • Never call iTick directly and never handle any API key in the frontend —
    all data comes from the backend endpoints above.
  • Colour every inferred value and keep the honest "ESTIMATED / synthetic"
    labels — this is spot gold, there is no real L2 book.
  • Fully responsive; works on a phone (stack columns vertically under 800px).
```

> Aa prompt **frontend** banave che. Backend (iTick loop + endpoints) tamaro Python
> already ready che — aa nava frontend ne fakt `VITE_API_BASE` = tamara deployed 9900
> URL par point karo, bas.

---

## 6. Jyare scale/persist joye (baad ma)

- **DB SQLite → Postgres:** multi-server ya guaranteed-persist joye to. Supabase free
  Postgres saru.
- **Worker alag karo:** iTick feed ne 1 alag "collector" process ma raakho je DB/Redis
  ma likhe; web workers (ghana) fakt DB mathi vaanche. Aa thi horizontal scale thaay.
- **Redis pub/sub:** live tick ne badha web workers/browsers sudhi broadcast karva.
- **Rate limit + login:** public karo to iTick key na abuse thaay etle rate-limit rakho.

---

## 7. TL;DR — atyare su karvu

1. `HOST=0.0.0.0` no nano change (§3A step 2).
2. `requirements.txt` + `Procfile` add karo.
3. **DigitalOcean $5 VPS** + systemd + Caddy (§3B) — 24/7, SSL, tamaru domain.
   *(ya jaldi test mate Railway — §3A, 10 minute ma live.)*
4. iTick key **fakt server env var** ma.
5. Bija loko mate joye tyare mane kaho → Option B (§4) refactor kari aapish.

---

**Note:** Aa guide fakt document che — 9900 no code me **kaain badlyo nathi**. Jyare tame
kaho ("ha deploy karo" / "0.0.0.0 change kari nakho") tyare j actual change karish,
tamari permission sathe.
