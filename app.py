import os
import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title=“Trinity SPXW Flow API”)

app.add_middleware(
CORSMiddleware,
allow_origins=[”*”],
allow_methods=[“GET”],
allow_headers=[”*”],
)

MASSIVE_API_KEY = os.environ.get(“MASSIVE_API_KEY”, “”)
POLYGON_BASE = “https://api.polygon.io”

def build_rows(contracts: list) -> list:
by_strike: dict = {}
for r in contracts:
strike = r.get(“details”, {}).get(“strike_price”)
if strike is None:
continue
if strike not in by_strike:
by_strike[strike] = {“calls”: [], “puts”: []}
ctype = r.get(“details”, {}).get(“contract_type”, “”).lower()
if ctype == “call”:
by_strike[strike][“calls”].append(r)
else:
by_strike[strike][“puts”].append(r)

```
rows = []
for strike, cp in by_strike.items():
    calls, puts = cp["calls"], cp["puts"]
    call_oi = sum(c.get("open_interest", 0) for c in calls)
    put_oi  = sum(c.get("open_interest", 0) for c in puts)
    c0 = calls[0] if calls else {}
    p0 = puts[0]  if puts  else {}
    c_bid = c0.get("last_quote", {}).get("bid", 0) or 0
    c_ask = c0.get("last_quote", {}).get("ask", 0) or 0
    p_bid = p0.get("last_quote", {}).get("bid", 0) or 0
    p_ask = p0.get("last_quote", {}).get("ask", 0) or 0
    c_mid = (c_bid + c_ask) / 2 or c0.get("day", {}).get("vw", 0) or 0
    p_mid = (p_bid + p_ask) / 2 or p0.get("day", {}).get("vw", 0) or 0
    net_flow = call_oi * c_mid * 100 - put_oi * p_mid * 100
    iv    = c0.get("implied_volatility") or p0.get("implied_volatility")
    gamma = (c0.get("greeks", {}).get("gamma") or 0) - (p0.get("greeks", {}).get("gamma") or 0)
    rows.append({
        "strike": float(strike), "call_oi": call_oi, "put_oi": put_oi,
        "net_flow": net_flow, "iv": iv, "gamma": gamma,
    })
return sorted(rows, key=lambda x: x["strike"], reverse=True)
```

def calc_metrics(rows: list) -> dict:
if not rows:
return {}
call_wall  = max(rows, key=lambda r: r[“call_oi”])[“strike”]
put_wall   = max(rows, key=lambda r: r[“put_oi”])[“strike”]
gamma_flip = min(rows, key=lambda r: abs(r[“gamma”] or 0))[“strike”]
net_flow   = sum(r[“net_flow”] for r in rows)
total_call = sum(r[“call_oi”] for r in rows)
total_put  = sum(r[“put_oi”]  for r in rows)
pcr        = total_put / total_call if total_call else 0
max_pain   = rows[0][“strike”]
min_pain   = float(“inf”)
for row in rows:
pain = sum(
max(0, r[“strike”] - row[“strike”]) * r[“call_oi”] +
max(0, row[“strike”] - r[“strike”]) * r[“put_oi”]
for r in rows
)
if pain < min_pain:
min_pain = pain
max_pain = row[“strike”]
return {
“call_wall”: call_wall, “put_wall”: put_wall,
“gamma_flip”: gamma_flip, “max_pain”: max_pain,
“net_flow”: net_flow, “pcr”: round(pcr, 3),
}

@app.get(”/api/spxw-flow”)
async def spxw_flow(exp: str = Query(…, description=“Expiration date YYYY-MM-DD”)):
if not MASSIVE_API_KEY:
raise HTTPException(status_code=500, detail=“MASSIVE_API_KEY not set on server”)
url = f”{POLYGON_BASE}/v3/snapshot/options/I:SPX?limit=250&apiKey={MASSIVE_API_KEY}”
async with httpx.AsyncClient(timeout=20) as client:
try:
resp = await client.get(url)
resp.raise_for_status()
except httpx.HTTPStatusError as e:
raise HTTPException(status_code=502, detail=f”Massive API error: {e.response.status_code}”)
except httpx.RequestError as e:
raise HTTPException(status_code=502, detail=f”Network error: {str(e)}”)
data = resp.json()
if data.get(“status”) == “ERROR”:
raise HTTPException(status_code=502, detail=f”Massive: {data.get(‘error’, ‘unknown error’)}”)
all_contracts = data.get(“results”, [])
spxw = [
r for r in all_contracts
if (r.get(“details”, {}).get(“ticker”, “”) or “”).startswith(“O:SPXW”)
and r.get(“details”, {}).get(“expiration_date”, “”) == exp
]
all_exps = sorted(set(
r.get(“details”, {}).get(“expiration_date”, “”)
for r in all_contracts
if (r.get(“details”, {}).get(“ticker”, “”) or “”).startswith(“O:SPXW”)
) - {””})
spot = None
if all_contracts:
spot = all_contracts[0].get(“underlying_asset”, {}).get(“price”)
if not spot:
async with httpx.AsyncClient(timeout=10) as client:
try:
pr = await client.get(f”{POLYGON_BASE}/v2/aggs/ticker/I:SPX/prev?adjusted=true&apiKey={MASSIVE_API_KEY}”)
if pr.status_code == 200:
pd = pr.json()
spot = (pd.get(“results”) or [{}])[0].get(“c”)
except Exception:
pass
if not spxw:
return JSONResponse({“ok”: True, “empty”: True, “reason”: f”No SPXW contracts for {exp}”,
“available_expirations”: all_exps, “spot”: spot, “rows”: [],
“call_wall”: None, “put_wall”: None, “gamma_flip”: None,
“max_pain”: None, “net_flow”: 0, “pcr”: None})
rows    = build_rows(spxw)
metrics = calc_metrics(rows)
return JSONResponse({“ok”: True, “empty”: False, “exp”: exp, “spot”: spot,
“count”: len(spxw), “rows”: rows, **metrics})

@app.get(”/health”)
async def health():
return {“status”: “ok”, “key_set”: bool(MASSIVE_API_KEY)}
