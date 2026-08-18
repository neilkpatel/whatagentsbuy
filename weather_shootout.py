#!/usr/bin/env python3
"""Buy the current temperature for one US city from many sellers, check it against
the official National Weather Service station observation.

Weather has no single true number either (stations, models and update times all
differ), so the reference is a PRIMARY source, the latest observation from the NWS
station nearest the point, not another weather API. Every seller is asked for the
same city (New York) by overriding its location input, and graded on how far it is
from the official observation, in Fahrenheit.

Every call is capped and archived by conform, so extraction is re-auditable. Run:
  python3 weather_shootout.py --limit 12 --budget 0.15
"""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from urllib.parse import urlparse

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

CITY = "New York"
LAT, LON = 40.7128, -74.0060
UA = {"user-agent": "whatagentsbuy.com weather check (contact https://whatagentsbuy.com/about)"}


def _get(url):
    req = urllib.request.Request(url, headers={**UA, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def nws_reference():
    """Latest observation temperature (F) from the NWS station nearest the point."""
    try:
        pt = _get(f"https://api.weather.gov/points/{LAT},{LON}")
        stations = _get(pt["properties"]["observationStations"])
        sid = stations["features"][0]["properties"]["stationIdentifier"]
        obs = _get(f"https://api.weather.gov/stations/{sid}/observations/latest")
        c = obs["properties"]["temperature"]["value"]
        if c is None:
            return None
        return {"f": round(c * 9 / 5 + 32, 1), "station": sid,
                "obs_time": obs["properties"]["timestamp"]}
    except Exception as e:
        print(f"  NWS reference failed: {e}")
        return None


_LOC_NAME = ("location", "city", "place", "q", "query", "name", "address")
_LAT = ("lat", "latitude")
_LON = ("lon", "lng", "long", "longitude")


def _retarget(d):
    """Override any location-ish field in an input dict to point at New York."""
    out = {}
    for k, v in (d or {}).items():
        kl = str(k).lower()
        if kl in _LAT:
            out[k] = LAT
        elif kl in _LON:
            out[k] = LON
        elif kl in _LOC_NAME:
            out[k] = CITY
        else:
            out[k] = v                        # keep other params (horizon_h, days) as advertised
    return out


def weather_candidates(max_price):
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    seen, out = set(), []
    BAD = ("btc", "defi", "yield", "pool", "opportunit", "govparse", "marine", "/areas")
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        blob = (url + " " + (r.get("description") or "")).lower()
        if not url.startswith("http") or host in seen:
            continue
        if not any(w in blob for w in ("weather", "temperature")) or any(b in blob for b in BAD):
            continue
        bz = ((r.get("extensions") or {}).get("bazaar") or {}).get("info", {})
        inp = bz.get("input", {}) or {}
        method = (inp.get("method") or "GET").upper()
        qp = _retarget(inp.get("queryParams")) if inp.get("queryParams") else None
        body = _retarget(inp.get("body")) if inp.get("body") else None
        # need a way to say "New York": either a retargetable query or body
        has_loc = (qp and any(str(k).lower() in _LOC_NAME + _LAT + _LON for k in qp)) or \
                  (body and any(str(k).lower() in _LOC_NAME + _LAT + _LON for k in body))
        if not has_loc:
            continue
        call_url = url
        if qp:
            call_url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(qp)
        p = conform.price_of(r)
        if not p or not (0 < p <= max_price):
            continue
        seen.add(host)
        out.append({"host": host, "url": call_url, "method": method, "body": body, "price": p})
    out.sort(key=lambda c: c["price"])
    return out


_TEMP_KEYS = ("temp", "temperature", "feels", "current", "fahrenheit", "celsius", "temp_f", "temp_c")


def find_temps(obj):
    """Collect numeric temperature candidates with key + inferred unit."""
    out = []

    def unit_of(kl, v):
        if "_c" in kl or "celsius" in kl or "centigrade" in kl:
            return "C"
        if "_f" in kl or "fahrenheit" in kl:
            return "F"
        return "C" if -60 <= v <= 45 else ("F" if 32 <= v <= 130 else "?")

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and -100 < v < 150:
                    if any(t in kl for t in _TEMP_KEYS):
                        out.append({"value": float(v), "key": str(k),
                                    "unit": unit_of(kl, v), "path": f"{path}.{k}"})
                else:
                    walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    return out


def to_f(t):
    return t["value"] if t["unit"] == "F" else round(t["value"] * 9 / 5 + 32, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--budget", type=float, default=0.15)
    ap.add_argument("--max-price", type=float, default=0.01)
    ap.add_argument("--pause", type=float, default=0.8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ref = nws_reference()
    if not ref and not a.dry_run:
        print("no NWS reference; aborting so we do not grade blind")
        return 1
    if ref:
        print(f"NWS reference for New York: {ref['f']}F  (station {ref['station']}, obs {ref['obs_time']})\n")

    cands = weather_candidates(a.max_price)[:a.limit]
    print(f"selected {len(cands)} weather sellers, advertised total ${sum(c['price'] for c in cands):.4f}")
    if a.dry_run:
        for c in cands:
            print(f"  ${c['price']:.4f} {c['method']:<4} {c['host']}  body={c['body']}")
        return 0

    bal0 = conform.wallet_balance()
    rows, spent = [], 0.0
    for i, c in enumerate(cands, 1):
        if spent + c["price"] > a.budget:
            print(f"stopping at {i-1}: next call would pass the budget")
            break
        cap = min(round(c["price"] * 1.25 + 0.0001, 6), 0.01)
        ok, payload, meta = conform.call(c["url"], c["method"], c["body"], cap)
        paid = (meta or {}).get("payment") if isinstance((meta or {}).get("payment"), dict) else {}
        if paid.get("success"):
            spent += cap
        temps = find_temps(payload) if ok and isinstance(payload, (dict, list)) else []
        best = temps[0] if temps else None
        bf = to_f(best) if best else None
        dev = round(bf - ref["f"], 1) if (bf is not None and ref) else None
        rows.append({"host": c["host"], "url": c["url"], "quoted": c["price"],
                     "paid": bool(paid.get("success")), "temp_f": bf, "field": best["key"] if best else None,
                     "dev_f": dev, "n_temp_fields": len(temps)})
        shown = f"{bf}F ({best['key']})" if best else "no temperature"
        d = f" {dev:+.1f}F" if dev is not None else ""
        print(f"  {i:>2}/{len(cands)} ${c['price']:.4f}  {shown:<28}{d}  {c['host']}")
        time.sleep(a.pause)

    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "city": CITY, "reference": ref,
           "spent_reported": round(spent, 6),
           "spent_onchain": round(bal0 - bal1, 6) if (bal0 is not None and bal1 is not None) else None,
           "rows": rows}
    json.dump(out, open(os.path.join(DATA, "weather_shootout.json"), "w"), indent=1)
    got = [r for r in rows if r["temp_f"] is not None]
    print(f"\n{len(got)}/{len(rows)} returned a temperature")
    if got and ref:
        devs = sorted(abs(r["dev_f"]) for r in got)
        print(f"  median abs deviation from NWS: {devs[len(devs)//2]:.1f}F")
    if bal0 is not None and bal1 is not None:
        print(f"  wallet moved ${bal0 - bal1:.6f} (reported ${spent:.6f})")
    print("  wrote data/weather_shootout.json (raw responses archived in captures/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
