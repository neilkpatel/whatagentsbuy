#!/usr/bin/env python3
"""Dispute receipts: one portable, hashed, self-verifying record per paid call.

Phase 1 of the dispute framework. The industry's own diagnosis is that agentic
commerce has payments but no dispute layer, because nobody records what was
promised, what was paid, and whether delivery matched, in a form a third party
can check. This assembles exactly that record from evidence the harness already
keeps, and makes the verdict reproducible so nobody has to trust us.

A receipt is arbitrable at three independent levels:

  1. INTEGRITY   receipt_id is a sha256 over the evidence fields. Recompute it;
                 change any promised field, price, tx or response shape and the
                 id no longer matches. The record is tamper-evident.
  2. VERDICT     the verdict is re-derivable offline: re-run conform.judge() on
                 the saved response shape against the promised fields and you get
                 the identical delivered/short/inconclusive. No network, no trust.
  3. RAW         the deepest check: pull the untouched bytes the seller returned
                 from captures/ (matched by url + tx) and re-derive the response
                 shape from scratch, proving the evidence itself is faithful.

A receipt carries the response SHAPE (types only, via describe_shape), never the
values, so publishing the dispute record never republishes the goods we paid for.

Usage:
  python3 receipts.py                 # backfill receipts from this week's data
  python3 receipts.py --show          # print the ledger summary
  python3 receipts.py --show <id>     # pretty-print one receipt
  python3 receipts.py --verify <id>   # re-derive a receipt's verdict, all 3 levels
  python3 receipts.py --disputes      # list only the shorts (the actual disputes)
"""
import glob
import hashlib
import json
import os
import re
import sys

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CAPTURES = conform.CAPTURES
RECEIPTS_DIR = os.path.join(DATA, "receipts")
RECEIPTS = os.path.join(RECEIPTS_DIR, "receipts.jsonl")
SUMMARY = os.path.join(RECEIPTS_DIR, "summary.json")
# VERSION 2 (2026-09-07, explicit migration): the accuracy evidence hash now
# binds the payment identity (tx, free flag, chain) and the raw-capture digest,
# after an external audit showed a v1 accuracy receipt's tx/chain/free could be
# altered without breaking its id. Delivery evidence gains chain. Backfill
# supersedes every v1 receipt with its v2 successor (same logical call); the v1
# ledger survives in git history and verify_receipt still checks v1 records.
VERSION = "2"


# ---- coercion: the conformance record stores some fields as strings -----------
def _s(v):
    return None if v is None else str(v)


def _b(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_id(ev):
    """Stable, tamper-evident id: a sha256 over the canonicalised evidence."""
    return "wab_" + hashlib.sha256(_canon(ev).encode()).hexdigest()[:16]


def _logical_key(receipt):
    """Identity of the underlying CALL, independent of its verdict. Re-grading a
    call changes its content id but not this, so a new receipt supersedes the old
    one for the same call/day rather than piling up beside it."""
    return (receipt["kind"], receipt["seller"]["url"],
            receipt.get("promise", {}).get("metric", ""), receipt.get("ts", ""))


# The exact fields a dispute turns on. Everything here is hashed into receipt_id;
# nothing here can change without breaking the id. Presentational context
# (timestamps, latency, source labels) is deliberately left out of the hash.
def _evidence(url, host, quoted, charged, paid, free, tx,
              promised, observed_schema, missing, extra, status, why,
              version=VERSION):
    ev = {
        "url": url, "host": host,
        "quoted": _s(quoted), "charged": _s(charged),
        "paid": _b(paid), "free": _b(free), "tx": tx or None,
        "promised": sorted(promised or []),
        "observed_schema": observed_schema,
        "missing": sorted(missing or []), "extra": sorted(extra or []),
        "status": status, "why": why or "",
    }
    if version != "1":
        ev["chain"] = "base"
        ev["v"] = version
    return ev


def _wrap(ev, *, kind, ts, latency_ms, raw_ref, truth=None):
    """Assemble the human- and machine-readable receipt around hashed evidence."""
    rid = content_id(ev)
    reverified = ev["why"] == "shortfall confirmed on two calls"
    two_call = reverified or "re-verify" in (ev["why"] or "")
    r = {
        "receipt_id": rid,
        "version": VERSION,
        "kind": kind,                       # delivery (field-presence) | accuracy
        "ts": ts or "",
        "seller": {"host": ev["host"], "url": ev["url"]},
        "promise": {
            "price_usdc": ev["quoted"],
            "fields": ev["promised"],
            "source": "x402 Bazaar (CDP discovery)",
        },
        "payment": {
            "charged_usdc": ev["charged"], "paid": ev["paid"], "free": ev["free"],
            "tx": ev["tx"], "chain": "base",
            "settlement": "EIP-3009, submitted by a facilitator",
        },
        "delivery": {
            "latency_ms": latency_ms,
            "observed_schema": ev["observed_schema"],
            "missing": ev["missing"], "extra": ev["extra"],
            "raw_ref": raw_ref,             # pointer into captures/, never the goods
        },
        "verdict": {
            "status": ev["status"], "why": ev["why"],
            "reverified": reverified,
            "decided_by": "two-call reconcile" if two_call else "single call",
            "method": "field-presence vs promised (conform.judge)",
        },
        "verify": {
            "integrity": "sha256 over the evidence fields must equal receipt_id",
            "verdict": "conform.judge(delivery.observed_schema, promise.fields).status == verdict.status",
            "raw": "re-derive observed_schema from delivery.raw_ref, then re-judge",
            "cmd": "python3 receipts.py --verify " + rid,
        },
    }
    if truth is not None:
        r["truth"] = truth
    return r


# ---- adapters: turn a graded row into a receipt -------------------------------
def receipt_from_conformance(row, cap_index=None, generated_ts=""):
    ev = _evidence(
        url=row.get("url"), host=row.get("host"),
        quoted=row.get("quoted"), charged=row.get("charged"),
        paid=row.get("paid"), free=row.get("free"), tx=row.get("tx"),
        promised=row.get("promised"), observed_schema=row.get("observed_schema"),
        missing=row.get("missing"), extra=row.get("extra"),
        status=row.get("status"), why=row.get("why"),
    )
    raw_ref = None
    if cap_index is not None:
        raw_ref = find_delivery_capture(cap_index, ev["url"], ev["tx"],
                                        call_id=row.get("call_id"))
    return _wrap(ev, kind="delivery", ts=generated_ts,
                 latency_ms=_i(row.get("ms")), raw_ref=raw_ref)


# ---- accuracy receipts: graded against a PRIMARY source, not field-presence ---
# The differentiator. A delivery receipt asks "did the promised fields arrive?".
# An accuracy receipt asks "was the number right?", graded against a source that
# cannot be a reseller: an exchange median, or the chain's own balanceOf. Only
# clean, objective ground truth qualifies; fuzzy references (weather stations,
# multi-field gas) stay studies, never a per-seller accuracy verdict.
def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _accuracy_receipt(*, host, url, quoted, paid, metric, returned, truth,
                      source, dev_value, tol_value, unit, field, ts, url_index,
                      note=None):
    status = ("inconclusive" if returned is None
              else "accurate" if (dev_value is not None and abs(dev_value) <= tol_value)
              else "off")
    # Resolve the capture binding BEFORE the evidence is hashed, and by the
    # MEASUREMENT DATE, never "latest capture at this URL": 10 of 111 published
    # accuracy receipts once pointed at captures dated after their measurement,
    # two different measurements citing the same raw bytes.
    ref = lookup_capture(url_index, url, ts)
    ev = {
        "url": url, "host": host, "quoted": _s(quoted), "paid": _b(paid),
        "metric": metric, "returned": returned, "truth": truth,
        "truth_source": source, "dev_value": dev_value, "tol_value": tol_value,
        "unit": unit, "field": field, "status": status,
        # v2: the payment identity and the raw-capture digest are part of the
        # evidence. A v1 receipt's tx, chain, and free flag sat outside the
        # hash, so they could be altered without breaking the id.
        "tx": (ref or {}).get("tx"), "free": not _b(paid), "chain": "base",
        "raw_sha256": (ref or {}).get("sha256"), "v": VERSION,
    }
    rid = content_id(ev)
    dev_str = None if dev_value is None else f"{dev_value:+g} {unit}"
    return {
        "receipt_id": rid, "version": VERSION, "kind": "accuracy", "ts": ts or "",
        "seller": {"host": host, "url": url},
        "promise": {"price_usdc": _s(quoted), "metric": metric,
                    "source": "x402 Bazaar (CDP discovery)"},
        "payment": {"charged_usdc": _s(quoted) if _b(paid) else "0",
                    "paid": _b(paid), "free": not _b(paid),
                    "tx": (ref or {}).get("tx"), "chain": "base",
                    "settlement": "EIP-3009, submitted by a facilitator"},
        "delivery": {"returned": returned, "field": field,
                     "raw_ref": ref},
        "truth": {"value": truth, "source": source, "deviation": dev_str,
                  "tolerance": f"±{tol_value:g} {unit}",
                  "dev_value": dev_value, "tol_value": tol_value, "unit": unit},
        "verdict": {"status": status,
                    "why": ((note or "no value returned to grade") if status == "inconclusive"
                            else "within tolerance of the primary source" if status == "accurate"
                            else "outside tolerance of the primary source"),
                    "reverified": False, "decided_by": "single call",
                    "method": "returned value vs a primary source, within a stated tolerance"},
        "verify": {
            "integrity": "sha256 over the evidence fields must equal receipt_id",
            "verdict": "recompute status from |truth.dev_value| vs truth.tol_value",
            "raw": "re-read delivery.field from delivery.raw_ref and recompute the deviation",
            "cmd": "python3 receipts.py --verify " + rid,
        },
    }


def receipts_from_price(url_index):
    p = os.path.join(DATA, "price_shootout.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p))
    truth = d.get("reference_end")
    srcs = "/".join((d.get("reference_sources") or {}).keys()) or "primary exchanges"
    # A field the picker chose that plainly is not a spot price (a volume, a
    # market cap, a supply) must never produce an accuracy verdict. It is a
    # measurement we could not make, not a seller being wrong. verify-before-accusing.
    NONPRICE = ("volume", "vlm", "cap", "supply", "count", "change", "pct",
                "percent", "ntl")
    out = []
    for r in d.get("rows", []):
        if not r.get("url"):
            continue
        field = r.get("field") or ""
        bad_field = any(t in field.lower() for t in NONPRICE)
        note = (f"could not isolate a spot-price field; the closest match in the "
                f"response was {field}, which is not a price") if bad_field else None
        out.append(_accuracy_receipt(
            host=r.get("host"), url=r.get("url"), quoted=r.get("quoted"),
            paid=r.get("paid"), metric=f"{d.get('symbol', 'spot')} price",
            returned=(None if bad_field else _num(r.get("price"))), truth=truth,
            source=f"median of {srcs}",
            dev_value=(None if bad_field else _num(r.get("dev_bps"))),
            tol_value=50.0, unit="bps", field=r.get("field"),
            ts=d.get("generated", "")[:10], url_index=url_index, note=note))
    return out


def receipts_from_balance(url_index):
    p = os.path.join(DATA, "balance_shootout.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p))
    truth = d.get("chain_usdc_end")
    tgt = d.get("target", "")
    short = (tgt[:6] + "…" + tgt[-4:]) if len(tgt) > 12 else tgt
    out = []
    for r in d.get("rows", []):
        if not r.get("url"):
            continue
        out.append(_accuracy_receipt(
            host=r.get("host"), url=r.get("url"), quoted=r.get("quoted"),
            paid=r.get("paid", True), metric=f"USDC balance of {short} on Base",
            returned=_num(r.get("usdc")), truth=truth,
            source="Base chain balanceOf (latest block)",
            dev_value=_num(r.get("dev")), tol_value=0.01, unit="USDC",
            field=".usdc", ts=d.get("generated", "")[:10], url_index=url_index))
    return out


def receipts_from_stock(url_index):
    p = os.path.join(DATA, "stock_shootout.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p))
    truth = d.get("reference")
    sym = d.get("symbol", "stock")
    out = []
    for r in d.get("rows", []):
        if not r.get("url"):
            continue
        out.append(_accuracy_receipt(
            host=r.get("host"), url=r.get("url"), quoted=r.get("quoted"),
            paid=r.get("paid", True), metric=f"{sym} real-time stock price",
            returned=_num(r.get("price")), truth=truth,
            source="FMP real-time quote", dev_value=_num(r.get("dev_bps")),
            tol_value=50.0, unit="bps", field=r.get("field"),
            ts=d.get("generated", "")[:10], url_index=url_index))
    return out


def receipts_from_lab(url_index):
    """Accuracy receipts from the daily lab (lab.json), which grades every
    accuracy category against its primary source. This is the corpus feed; the
    per-shootout adapters above are the legacy single-category path."""
    p = os.path.join(DATA, "lab.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p))
    ts = d.get("generated", "")[:10]
    out = []
    for cat, c in (d.get("categories") or {}).items():
        truth = c.get("reference")
        for r in c.get("rows", []):
            # Only the actual graded claims become receipts. A row with no value
            # is ambiguous (our generic injection may have missed, not the
            # seller's fault) and stays in lab.json + the raw archive, not the
            # ledger, so we never imply a seller failed on our own bad probe.
            if not r.get("url") or r.get("value") is None:
                continue
            out.append(_accuracy_receipt(
                host=r.get("host"), url=r.get("url"), quoted=r.get("quoted"),
                paid=r.get("paid", True), metric=c.get("metric", cat),
                returned=_num(r.get("value")), truth=truth,
                source=c.get("source", ""), dev_value=_num(r.get("dev")),
                tol_value=c.get("tol", 100.0), unit=c.get("unit", ""),
                field=r.get("field"), ts=ts, url_index=url_index))
    return out


# ---- capture index: locate the archived raw bytes for a receipt ---------------
def _tx_of(stdout):
    try:
        env = json.loads(stdout)
        pay = (env.get("metadata") or {}).get("payment") or {}
        return pay.get("transactionHash")
    except (ValueError, AttributeError):
        return None


def _entry(base, n, rec):
    """One capture pointer: file, line, identity (url/tx/call_id) and a digest
    of the untouched stdout so the binding is content-addressed, not guessed."""
    stdout = rec.get("stdout", "")
    return {"capture": base, "line": n,
            "match": {"url": rec.get("url"), "tx": _tx_of(stdout),
                      "call_id": rec.get("call_id")},
            "tx": _tx_of(stdout), "call_id": rec.get("call_id"),
            "date": base.replace("raw_", "").replace(".jsonl", ""),
            "sha256": hashlib.sha256(stdout.encode()).hexdigest()[:16]}


def _iter_captures():
    for f in sorted(glob.glob(os.path.join(CAPTURES, "raw_*.jsonl"))):
        base = os.path.basename(f)
        with open(f) as fh:
            for n, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                yield base, n, rec


def build_capture_index():
    """Three ways to locate the archived response behind a delivery row, tried in
    order of identity strength: the dispatch-minted call_id (cannot drift), the
    exact (url, tx) pair, and finally the tx alone with the URL validated modulo
    query string. The tx fallback exists because conform.call() appends declared
    query params before archiving while the row keeps the registry URL, which
    orphaned 186 published receipts from captures that demonstrably held their
    transaction."""
    by_urltx, by_tx, by_call = {}, {}, {}
    for base, n, rec in _iter_captures():
        url = rec.get("url")
        if not url:
            continue
        e = _entry(base, n, rec)
        key = (url, e["tx"])
        if key not in by_urltx:                    # first occurrence wins
            by_urltx[key] = e
        if e["tx"] and e["tx"] not in by_tx:
            by_tx[e["tx"]] = e
        if e["call_id"] and e["call_id"] not in by_call:
            by_call[e["call_id"]] = e
    return {"by_urltx": by_urltx, "by_tx": by_tx, "by_call": by_call}


def _same_endpoint(archived_url, row_url):
    """The archived URL may carry appended query params; scheme+host+path must
    still be the row's endpoint, or a tx-only match could bind the wrong call."""
    try:
        from urllib.parse import urlparse
        a, b = urlparse(archived_url or ""), urlparse(row_url or "")
        return (a.scheme, a.netloc, a.path) == (b.scheme, b.netloc, b.path)
    except Exception:
        return False


def find_delivery_capture(idx, url, tx, call_id=None):
    if call_id and call_id in idx["by_call"]:
        return idx["by_call"][call_id]
    e = idx["by_urltx"].get((url, tx))
    if e:
        return e
    if tx:
        e = idx["by_tx"].get(tx)
        if e and _same_endpoint(e["match"]["url"], url):
            return e
    return None


def build_url_index():
    """Map url -> EVERY capture of it, in file (date) order, so a measurement can
    be bound to the capture from its own day. The old single-slot version kept
    only the LATEST capture per URL, which attached later raw bytes to earlier
    measurements: 10 of 111 published accuracy receipts carried wrong-date
    bindings, two of them citing the same capture for different measured values."""
    idx = {}
    for base, n, rec in _iter_captures():
        url = rec.get("url")
        if not url:
            continue
        idx.setdefault(url, []).append(_entry(base, n, rec))
    return idx


def lookup_capture(url_index, url, ts):
    """The capture for THIS measurement: same-day first, else the newest capture
    dated on or before the measurement. A later capture is never returned; a
    measurement with no plausible capture stays honestly unbound."""
    versions = (url_index or {}).get(url) or []
    if not versions:
        return None
    if ts:
        same = [e for e in versions if e["date"] == ts]
        if same:
            return same[-1]
        before = [e for e in versions if e["date"] <= ts]
        if before:
            return before[-1]
        return None
    return versions[-1]


def load_raw_payload(raw_ref):
    """Pull the seller's untouched response data from captures/, matched by
    url+tx (not a fragile line number). Returns the parsed data, or None."""
    if not raw_ref:
        return None
    m = raw_ref.get("match", {})
    path = os.path.join(CAPTURES, raw_ref.get("capture", ""))
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if m.get("call_id"):
                # the dispatch-minted id is the strongest identity; nothing else
                # needs to match when it does
                if rec.get("call_id") != m["call_id"]:
                    continue
            else:
                if rec.get("url") != m.get("url"):
                    continue
                if _tx_of(rec.get("stdout", "")) != m.get("tx"):
                    continue
            try:
                return json.loads(rec["stdout"]).get("data")
            except (ValueError, KeyError):
                return None
    return None


# ---- verification: the trustless core -----------------------------------------
def _recompute_dev(returned, truth, unit):
    """Re-derive the deviation from the two stored values, in the receipt's own
    unit: bps is relative to the reference; anything else is a plain difference."""
    if returned is None or truth is None:
        return None
    if (unit or "").lower() == "bps":
        if not truth:
            return None
        return (returned - truth) / truth * 1e4
    return returned - truth


def _extract_field(payload, field):
    """Walk a recorded field path like '.data.rates[0].usd' into the raw payload
    and return the number there, or None if the path does not resolve."""
    if payload is None or not field:
        return None
    cur = payload
    for seg in str(field).strip(".").split("."):
        if not seg:
            continue
        m = re.match(r"([^\[\]]*)((?:\[\d+\])*)$", seg)
        if not m:
            return None
        name, idxs = m.group(1), re.findall(r"\[(\d+)\]", m.group(2) or "")
        if name:
            if not isinstance(cur, dict) or name not in cur:
                return None
            cur = cur[name]
        for i in idxs:
            if not isinstance(cur, list) or int(i) >= len(cur):
                return None
            cur = cur[int(i)]
    try:
        return float(str(cur).replace(",", "").lstrip("$"))
    except (TypeError, ValueError):
        return None


def _value_anywhere(payload, value, depth=0):
    """Does the recorded measurement appear as SOME numeric leaf of the capture?
    Used only for provenance (is this the right raw record), never for grading."""
    if depth > 6 or value is None:
        return False
    if isinstance(payload, dict):
        return any(_value_anywhere(v, value, depth + 1) for v in payload.values())
    if isinstance(payload, list):
        return any(_value_anywhere(v, value, depth + 1) for v in payload)
    try:
        return abs(float(str(payload).replace(",", "").lstrip("$")) - value) \
            <= max(1e-9, abs(value) * 1e-6)
    except (TypeError, ValueError):
        return False


def schema_probe(schema):
    """Rebuild a payload that conform.judge() classifies identically to the
    original response, from the saved shape alone. describe_shape() records a
    list as {"array_of": ...} and a scalar as a type name; judge() only inspects
    keys and dict-vs-not, so this reproduces its verdict without the real values.
    The one fact that must survive is 'was the top level a dict', because a bare
    list or scalar is inconclusive ('we could not measure'), never a short."""
    if isinstance(schema, dict):
        if set(schema.keys()) == {"array_of"}:          # the shape of a JSON list
            return [schema_probe(schema["array_of"])]
        return {k: schema_probe(v) for k, v in schema.items()}
    if schema == "array":
        return []
    if schema == "object":
        return {}
    return None                                          # a scalar leaf -> non-dict


def evidence_of(receipt):
    """Reconstruct the hashed evidence from a stored receipt, so anyone can
    recompute the id and confirm nothing was altered. Version-aware: v1 records
    (in git history, or an old copy someone saved) still verify under the v1
    field set; v2 records additionally bind the payment identity and raw digest."""
    v1 = receipt.get("version", "1") == "1"
    if receipt["kind"] == "accuracy":
        t = receipt["truth"]
        ev = {
            "url": receipt["seller"]["url"], "host": receipt["seller"]["host"],
            "quoted": receipt["promise"]["price_usdc"],
            "paid": receipt["payment"]["paid"],
            "metric": receipt["promise"]["metric"],
            "returned": receipt["delivery"]["returned"], "truth": t["value"],
            "truth_source": t["source"], "dev_value": t["dev_value"],
            "tol_value": t["tol_value"], "unit": t["unit"],
            "field": receipt["delivery"]["field"],
            "status": receipt["verdict"]["status"],
        }
        if not v1:
            ref = receipt["delivery"].get("raw_ref") or {}
            ev.update({"tx": receipt["payment"].get("tx"),
                       "free": receipt["payment"].get("free"),
                       "chain": receipt["payment"].get("chain"),
                       "raw_sha256": ref.get("sha256"),
                       "v": receipt.get("version")})
        return ev
    return _evidence(
        url=receipt["seller"]["url"], host=receipt["seller"]["host"],
        quoted=receipt["promise"]["price_usdc"],
        charged=receipt["payment"]["charged_usdc"],
        paid=receipt["payment"]["paid"], free=receipt["payment"]["free"],
        tx=receipt["payment"]["tx"], promised=receipt["promise"]["fields"],
        observed_schema=receipt["delivery"]["observed_schema"],
        missing=receipt["delivery"]["missing"], extra=receipt["delivery"]["extra"],
        status=receipt["verdict"]["status"], why=receipt["verdict"]["why"],
        version=receipt.get("version", "1"),
    )


def verify_receipt(receipt):
    """Re-derive the receipt at all three levels. Returns a dict of pass/None
    (None = not checkable here, e.g. no archived raw for this call)."""
    out = {}
    # 1. integrity: the id is a faithful hash of the evidence
    out["integrity"] = content_id(evidence_of(receipt)) == receipt["receipt_id"]
    # 2. verdict: re-judge the saved response shape against the promise, offline.
    #    A verdict decided by the two-call reconcile cannot be reproduced from one
    #    stored shape, so it is honestly n/a here (the raw level still checks it).
    if receipt["kind"] == "accuracy":
        # verdict: RECOMPUTED from the returned and reference values, never
        # trusted from the stored deviation. A tampered record saying
        # "returned 1, reference 60000, deviation 0" used to verify as accurate
        # because only the stored dev_value was consulted.
        t = receipt["truth"]
        ret = _num(receipt["delivery"]["returned"])
        truth = _num(t["value"])
        rdev = _recompute_dev(ret, truth, t["unit"])
        stored = t["dev_value"]
        dev_consistent = ((rdev is None and stored is None)
                          or (rdev is not None and stored is not None
                              and abs(rdev - _num(stored)) <= max(abs(t["tol_value"]) * 0.05, 1e-6)))
        expect = ("inconclusive" if ret is None
                  else "accurate" if (rdev is not None and abs(rdev) <= t["tol_value"])
                  else "off")
        out["verdict"] = dev_consistent and expect == receipt["verdict"]["status"]
        # raw: literal provenance — the bound capture must actually contain the
        # recorded measurement. The recorded field is extracted from the raw
        # payload and compared to `returned`; if the exact field moved, any
        # numeric leaf equal to the recorded value still counts (a provenance
        # check, not a grading one). CAVEAT: a unit-converted reading (weather
        # C->F) can legitimately fail this literal check; None = not assessable.
        payload = load_raw_payload(receipt["delivery"].get("raw_ref"))
        if payload is None or ret is None:
            out["raw"] = None
        else:
            fv = _extract_field(payload, receipt["delivery"].get("field"))
            exact = fv is not None and abs(fv - ret) <= max(1e-9, abs(ret) * 1e-6)
            out["raw"] = exact or _value_anywhere(payload, ret)
        return out
    reconciled = "re-verify" in (receipt["verdict"].get("why") or "")
    if not reconciled:
        regraded = conform.judge(schema_probe(receipt["delivery"]["observed_schema"]),
                                 receipt["promise"]["fields"])
        out["verdict"] = regraded["status"] == receipt["verdict"]["status"]
    else:
        out["verdict"] = None
    # 3. raw: re-derive the shape from the untouched archived bytes
    payload = load_raw_payload(receipt["delivery"].get("raw_ref"))
    if payload is None:
        out["raw"] = None
    else:
        out["raw"] = (conform.describe_shape(payload)
                      == receipt["delivery"]["observed_schema"])
    return out


# ---- ledger I/O ---------------------------------------------------------------
def load_ledger():
    if not os.path.exists(RECEIPTS):
        return {}
    out = {}
    with open(RECEIPTS) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[r["receipt_id"]] = r
    return out


def write_ledger(by_id):
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
    with open(RECEIPTS, "w") as fh:
        for rid in sorted(by_id):
            fh.write(_canon(by_id[rid]) + "\n")


def backfill():
    """Assemble receipts from every graded paid call we have, merge into the
    accumulating ledger (dedup by content id), and write a summary."""
    cap_index = build_capture_index()
    url_index = build_url_index()
    ledger = load_ledger()
    before = len(ledger)

    src = os.path.join(DATA, "conformance_verified.json")
    doc = json.load(open(src))
    gts = doc.get("generated", "")
    fresh = []
    for row in doc.get("rows", []):
        if row.get("url"):
            fresh.append(receipt_from_conformance(row, cap_index, gts))
    made = len(fresh)
    with_raw = sum(1 for r in fresh if r["delivery"]["raw_ref"])

    # accuracy receipts: graded against a primary source (exchange median, chain)
    acc = (receipts_from_price(url_index) + receipts_from_balance(url_index)
           + receipts_from_stock(url_index) + receipts_from_lab(url_index))
    fresh += acc
    print(f"assembled {len(acc)} accuracy receipts (shootouts + daily lab) graded vs a primary source")

    # Stamp each receipt with its own verification result so a reader (or the
    # site build) sees the three levels without re-deriving 576 receipts. This is
    # derived, not hashed: it never affects the id.
    for r in fresh:
        r["checks"] = verify_receipt(r)

    # Supersede: a receipt is content-addressed, so re-grading a call mints a new
    # id. For a published ledger we keep the CURRENT verdict per logical call, not
    # every past version (git history + captures/ are the immutable audit trail).
    fresh_keys = {_logical_key(r) for r in fresh}
    ledger = {rid: r for rid, r in ledger.items() if _logical_key(r) not in fresh_keys}
    for r in fresh:
        ledger[r["receipt_id"]] = r

    write_ledger(ledger)

    from collections import Counter
    delivery = [r for r in ledger.values() if r["kind"] == "delivery"]
    accuracy = [r for r in ledger.values() if r["kind"] == "accuracy"]
    disputes = [r for r in ledger.values() if r["verdict"]["status"] == "short"]
    summary = {
        "generated": gts,
        "receipts": len(ledger),
        "delivery": {"count": len(delivery),
                     "by_status": dict(Counter(r["verdict"]["status"] for r in delivery))},
        "accuracy": {"count": len(accuracy),
                     "by_status": dict(Counter(r["verdict"]["status"] for r in accuracy))},
        "disputes": len(disputes),
        "with_archived_raw": sum(1 for r in ledger.values() if r["delivery"].get("raw_ref")),
        "capture_index_size": len(cap_index),
    }
    tally = Counter(r["verdict"]["status"] for r in ledger.values())
    os.makedirs(RECEIPTS_DIR, exist_ok=True)
    json.dump(summary, open(SUMMARY, "w"), indent=1)

    print(f"assembled {made} receipts from {os.path.basename(src)}")
    print(f"  ledger: {before} -> {len(ledger)} ({len(ledger) - before} new)")
    print(f"  by verdict: {dict(tally)}")
    print(f"  disputes (short): {len(disputes)}")
    print(f"  raw bytes located for {with_raw}/{made} ({with_raw * 100 // max(made,1)}%)")
    print(f"  wrote {os.path.relpath(RECEIPTS, HERE)} and {os.path.relpath(SUMMARY, HERE)}")

    # verify the whole ledger reproduces. The guarantee that matters: every
    # arbitrable CLAIM (a delivered or a short) re-derives; inconclusives are
    # explicit non-claims. A single mismatch is a real problem, so it is loud.
    integ_ok = 0
    v_pass = v_check = 0            # offline shape re-grade, where it applies
    v_mismatch = []
    claims = claims_ok = 0
    for r in ledger.values():
        v = verify_receipt(r)
        integ_ok += 1 if v["integrity"] else 0
        if v["verdict"] is not None:
            v_check += 1
            if v["verdict"]:
                v_pass += 1
            else:
                v_mismatch.append(r)
        if r["verdict"]["status"] in ("delivered", "short", "accurate", "off"):
            claims += 1
            # a claim is reproduced if its shape re-grades, or (two-call cases)
            # if the archived raw re-derives its verdict
            if v["verdict"] or v["raw"]:
                claims_ok += 1
    n = len(ledger)
    print(f"\nself-check across {n} receipts:")
    print(f"  integrity reproduced:            {integ_ok}/{n}")
    print(f"  arbitrable claims reproduced:    {claims_ok}/{claims}  (delivered/short/accurate/off)")
    print(f"  offline shape re-grade matches:  {v_pass}/{v_check}  (two-call verdicts checked via raw)")
    if integ_ok != n or v_mismatch or claims_ok != claims:
        print(f"  WARNING: {len(v_mismatch)} verdict mismatch, "
              f"{claims - claims_ok} claim(s) unreproduced; investigate before publishing")
        for r in v_mismatch[:5]:
            print(f"    mismatch: {r['seller']['host']} stored={r['verdict']['status']}")

    if disputes:
        d = sorted(disputes, key=lambda r: (r["delivery"]["raw_ref"] is None, r["seller"]["host"]))[0]
        print("\n--- a worked dispute receipt (a real short) ---")
        print(json.dumps(d, indent=1, ensure_ascii=False))
        print("verify:", verify_receipt(d))


def _find(rid, ledger):
    if rid in ledger:
        return ledger[rid]
    hits = [r for r in ledger.values() if r["receipt_id"].endswith(rid) or rid in r["seller"]["host"]]
    return hits[0] if len(hits) == 1 else None


def main(argv):
    if not argv:
        return backfill()
    ledger = load_ledger()
    if argv[0] == "--show":
        if len(argv) > 1:
            r = _find(argv[1], ledger)
            print(json.dumps(r, indent=1, ensure_ascii=False) if r else "no such receipt")
            return
        print(json.load(open(SUMMARY)) if os.path.exists(SUMMARY) else "no summary; run backfill")
        return
    if argv[0] == "--disputes":
        for r in sorted((x for x in ledger.values() if x["verdict"]["status"] == "short"),
                        key=lambda r: r["seller"]["host"]):
            miss = ",".join(r["delivery"]["missing"])
            print(f"  {r['receipt_id']}  {r['seller']['host']:<34} paid ${r['payment']['charged_usdc']}"
                  f"  missing: {miss}")
        return
    if argv[0] == "--verify":
        r = _find(argv[1], ledger) if len(argv) > 1 else None
        if not r:
            print("no such receipt")
            return
        v = verify_receipt(r)
        print(f"receipt {r['receipt_id']}  ({r['seller']['host']}, verdict: {r['verdict']['status']})")
        names = {"integrity": "1. INTEGRITY (id is a faithful hash of the evidence)",
                 "verdict": "2. VERDICT   (re-judged offline, no network)",
                 "raw": "3. RAW       (shape re-derived from archived bytes)"}
        for k in ("integrity", "verdict", "raw"):
            mark = {True: "PASS", False: "FAIL", None: "n/a "}[v[k]]
            print(f"   [{mark}] {names[k]}")
        return
    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
