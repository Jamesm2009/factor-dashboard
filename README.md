# Factor & Sector Analysis Dashboard

Absolute and Relative Score analysis for S&P 500 sectors and US equity style factors, benchmarked against SPY. Inspired by the RealInvestmentAdvice.com factor/sector scoring tool.

---

## What It Does

Scores every ETF on two dimensions using a 52-week lookback:

| Score | Formula | What It Tells You |
|-------|---------|-------------------|
| **Absolute Score** | `2 × (price − 52wk low) / (52wk high − 52wk low) − 1` | Where the ETF sits in its own trailing range (−1 = at the bottom, +1 = at the top) |
| **Relative Score** | Same formula applied to the **ETF ÷ SPY price ratio** | Whether the ETF is outperforming or underperforming SPY on a normalized basis |

Both scores map to **−1 (extremely oversold)** through **+1 (extremely overbought)**.

### The Scatter Chart

Each checked ETF is plotted with Absolute Score on the X-axis and Relative Score on the Y-axis, creating four quadrants:

| Quadrant | Meaning |
|----------|---------|
| **Top-left** (oversold + outperforming) | Relative strength despite being cheap — potential opportunity |
| **Top-right** (overbought + outperforming) | Strong and expensive — momentum play, watch for reversal |
| **Bottom-left** (oversold + underperforming) | Weak and cheap — contrarian value or falling knife |
| **Bottom-right** (overbought + underperforming) | Expensive but losing relative strength — potential trim |

**Tails** show each ETF's weekly trajectory over the selected timeframe (4, 6, 8, 10, or 12 weeks), so you can see the *direction of travel* — not just where something is, but where it's heading.

---

## Two Tabs

| Tab | ETFs | Coverage |
|-----|------|----------|
| **Sectors** | XLC, XLY, XLP, XLU, XLRE, XLF, XLB, XLI, XLE, XLV, XTN, XLK | All 11 S&P 500 GICS sectors + Transportation |
| **Factors** | MGK, GDX, IVW, ARKK, EFA, EEM, VYM, SPLV, PKW, VEA, MDYV, VBR, FDM, RSP, SPHB, VFQY, MTUM, MDY, VTV, VBK, MDYG, IWM | Size, value, growth, momentum, quality, low-vol, high-beta, international, and alternatives |

ETFs can be added or removed by editing `funds.json`.

---

## How To Read the Table

- **Green cells** = negative score = oversold = potential buying opportunity
- **Red cells** = positive score = overbought = potential trim candidate
- Darker color = more extreme reading
- **Sort** by clicking any column header
- **Check/uncheck** the Chart column to add or remove ETFs from the scatter plot

**Important:** Scores can stay extremely overbought or oversold for weeks. These are positioning signals, not timing signals. Patience is required.

---

## Stack

| Component | Technology |
|-----------|-----------|
| Backend | Flask (Python) |
| Data | Yahoo Finance (yfinance) — no API key needed |
| Cache | Upstash Redis (25-hour TTL) |
| Chart | Chart.js 4.4.1 + chartjs-plugin-datalabels |
| Hosting | Dokku on DigitalOcean |

---

## Files

```
factor-dashboard/
├── app.py                 # Flask server, yFinance download, score computation
├── funds.json             # Sector and factor ETF definitions
├── templates/
│   └── index.html         # Full UI — table, scatter chart, tabs, tail selector
├── requirements.txt       # Python dependencies
├── Procfile               # Gunicorn start command
└── README.md              # This file
```

---

## Deployment (Dokku)

### 1. Create the GitHub repo

Create a new repo (e.g., `factor-dashboard`), upload all files. Keep `index.html` inside the `templates/` folder.

### 2. Create the Dokku app

```bash
dokku apps:create factor-dashboard
dokku config:set factor-dashboard \
  UPSTASH_REDIS_REST_URL=https://your-redis-url \
  UPSTASH_REDIS_REST_TOKEN=your-token
```

### 3. Deploy

```bash
dokku git:sync --build factor-dashboard \
  https://TOKEN@github.com/Jamesm2009/factor-dashboard main
```

### 4. Domain and SSL

```bash
dokku domains:set factor-dashboard factors.market-dashboards.com
dokku letsencrypt:enable factor-dashboard
```

### 5. Daily refresh

Add to the droplet crontab (e.g., 3:40 PM CT, after the other dashboards):

```
40 15 * * 1-5 curl -s https://factors.market-dashboards.com/refresh > /dev/null
```

---

## Endpoints

| Route | Purpose |
|-------|---------|
| `/` | Main dashboard |
| `/refresh` | Force full data reload (hit after market close) |
| `/status` | JSON progress — phase, counts, timestamps |
| `/api/data` | Raw JSON of all scores and trail data |

---

## How the Scores Are Computed

1. **Download** ~20 months of daily close prices for all 35 tickers (batch call via yFinance)
2. For each weekly snapshot going back 12 weeks:
   - Look back 52 weeks from that snapshot date
   - **Absolute Score** = where the price sits in that 52-week high/low range, mapped to [−1, +1]
   - **Relative Score** = same calculation on the ETF÷SPY price ratio
3. The trail of weekly snapshots becomes the **tail** on the scatter chart
4. Current scores populate the table

The entire computation takes ~10 seconds. Results are cached in Redis and served instantly on repeat visits.

---

## Customizing

### Add or remove ETFs

Edit `funds.json`. Each entry needs a `symbol` and `name`:

```json
{ "symbol": "QUAL", "name": "Quality Factor" }
```

Add to either the `sectors` or `factors` array. Redeploy and hit `/refresh`.

### Change the lookback window

The 52-week lookback is set in `compute_scores()` via the `lookback_weeks` parameter. Changing this affects how "overbought" and "oversold" are defined — shorter windows make scores more volatile.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Blank chart | Check that at least one ETF is checked in the Chart column |
| Missing ETF data | Some tickers may not have 52 weeks of history — yFinance will skip them |
| Stale data | Hit `/refresh` or check `/status` to confirm phase = 4 |
| Redis not connecting | Verify `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` via `dokku config:show factor-dashboard` |
| SSL fails | Remove duplicate vhosts: `dokku domains:report factor-dashboard`, remove extras, re-enable letsencrypt |
