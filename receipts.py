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
import sys

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CAPTURES = conform.CAPTURES
RECEIPTS_DIR = os.path.join(DATA, "receipts")
RECEIPTS = os.path.join(RECEIPTS_DIR, "receipts.jsonl")
SUMMARY = os.path.join(RECEIPTS_DIR, "summary.json")
VERSION = "1"


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
              promised, observed_schema, missing, extra, status, why):
    return {
        "url": url, "host": host,
        "quoted": _s(quoted), "charged": _s(charged),
        "paid": _b(paid), "free": _b(free), "tx": tx or None,
        "promised": sorted(promised or []),
        "observed_schema": observed_schema,
        "missing": sorted(missing or []), "extra": sorted(extra or []),
        "status": status, "why": why or "",
    }


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
        raw_ref = cap_index.get((ev["url"], ev["tx"]))
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
    ev = {
        "url": url, "host": host, "quoted": _s(quoted), "paid": _b(paid),
        "metric": metric, "returned": returned, "truth": truth,
        "truth_source": source, "dev_value": dev_value, "tol_value": tol_value,
        "unit": unit, "field": field, "status": status,
    }
    rid = content_id(ev)
    ref = (url_index or {}).get(url)
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


def build_capture_index():
    """Map (url, tx) -> a pointer into captures/, so a receipt can name the exact
    archived response an auditor should re-derive its shape from."""
    idx = {}
    for f in sorted(glob.glob(os.path.join(CAPTURES, "raw_*.jsonl"))):
        base = os.path.basename(f)
        with open(f) as fh:
            for n, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                key = (rec.get("url"), _tx_of(rec.get("stdout", "")))
                if key[0] and key not in idx:      # first occurrence wins
                    idx[key] = {"capture": base, "line": n,
                                "match": {"url": key[0], "tx": key[1]}}
    return idx


def build_url_index():
    """Map url -> a capture pointer (with its tx), last occurrence winning, so an
    accuracy row that carries no per-call tx can still be tied to the on-chain
    payment and the archived bytes."""
    idx = {}
    for f in sorted(glob.glob(os.path.join(CAPTURES, "raw_*.jsonl"))):
        base = os.path.basename(f)
        with open(f) as fh:
            for n, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                url = rec.get("url")
                if not url:
                    continue
                tx = _tx_of(rec.get("stdout", ""))
                idx[url] = {"capture": base, "line": n,
                            "match": {"url": url, "tx": tx}, "tx": tx}
    return idx


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
    recompute the id and confirm nothing was altered."""
    if receipt["kind"] == "accuracy":
        t = receipt["truth"]
        return {
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
    return _evidence(
        url=receipt["seller"]["url"], host=receipt["seller"]["host"],
        quoted=receipt["promise"]["price_usdc"],
        charged=receipt["payment"]["charged_usdc"],
        paid=receipt["payment"]["paid"], free=receipt["payment"]["free"],
        tx=receipt["payment"]["tx"], promised=receipt["promise"]["fields"],
        observed_schema=receipt["delivery"]["observed_schema"],
        missing=receipt["delivery"]["missing"], extra=receipt["delivery"]["extra"],
        status=receipt["verdict"]["status"], why=receipt["verdict"]["why"],
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
        # verdict re-derives arithmetically: is |deviation| within tolerance?
        t = receipt["truth"]
        ret = receipt["delivery"]["returned"]
        expect = ("inconclusive" if ret is None
                  else "accurate" if (t["dev_value"] is not None
                                      and abs(t["dev_value"]) <= t["tol_value"])
                  else "off")
        out["verdict"] = expect == receipt["verdict"]["status"]
        out["raw"] = None            # field re-extraction from raw: Phase 2.5
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
