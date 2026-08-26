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
VERSION = "1"
VERSION_BOUND = "2"   # binds settlement identity and/or archived agentcash stdout
SUPPORTED_VERSIONS = frozenset({VERSION, VERSION_BOUND})
CHAIN_CANON = "base"
CAPTURE_NAME_RE = re.compile(r"^raw_\d{4}-\d{2}-\d{2}\.jsonl$")
RESPONSE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _str_list(val):
    """Exact list of strings for hashed field lists; None if wrong type."""
    if val is None:
        return []
    if not isinstance(val, list):
        return None
    if not all(isinstance(x, str) for x in val):
        return None
    return sorted(val)


def _compare_float(val):
    """Numeric value for tolerance math; None if not a real number."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    return None


def _why_str(val):
    """Verdict why text; empty string if absent, None if wrong type."""
    if val is None:
        return ""
    if not isinstance(val, str):
        return None
    return val


def _canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_id(ev):
    """Stable, tamper-evident id: a sha256 over the canonicalised evidence."""
    return "wab_" + hashlib.sha256(_canon(ev).encode()).hexdigest()[:16]


def _normalize_tx(tx):
    """Canonical Base settlement id: lowercase 0x plus 64 hex digits, or None."""
    if not tx or not isinstance(tx, str):
        return None
    t = tx.strip().lower()
    if not t.startswith("0x") or len(t) != 66:
        return None
    try:
        int(t[2:], 16)
    except ValueError:
        return None
    return t


def _parse_version(receipt):
    """Exact supported version string only; no trim or coercion."""
    if not isinstance(receipt, dict):
        return None
    if "version" not in receipt:
        return VERSION
    v = receipt.get("version")
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, str):
        return None
    if v not in SUPPORTED_VERSIONS:
        return None
    return v


def _valid_response_digest(digest):
    """Exact archived stdout digest contract: lowercase 64-char SHA-256 hex."""
    return isinstance(digest, str) and bool(RESPONSE_DIGEST_RE.match(digest))


def _digest_claimed(receipt):
    d = (receipt.get("delivery") if isinstance(receipt, dict) else None) or {}
    return _valid_response_digest(d.get("response_digest"))


def _digest_field_present(receipt):
    d = (receipt.get("delivery") if isinstance(receipt, dict) else None) or {}
    return "response_digest" in d and d["response_digest"] is not None


def _digest_forbidden(receipt, ver):
    """v1 must not carry response_digest; v2 rejects non-contract digest values."""
    if ver not in SUPPORTED_VERSIONS:
        return _digest_field_present(receipt)
    if not _digest_field_present(receipt):
        return False
    digest = (((receipt.get("delivery") if isinstance(receipt, dict) else None) or {})
              .get("response_digest"))
    if ver == VERSION:
        return True
    return not _valid_response_digest(digest)


def _v2_incoherent(receipt, ver):
    """v2 must carry a real binding; presentation fields must normalize exactly."""
    if ver != VERSION_BOUND:
        return False
    payment = receipt.get("payment")
    delivery = receipt.get("delivery")
    if not isinstance(payment, dict) or not isinstance(delivery, dict):
        return True
    tx_raw = payment.get("tx")
    chain_raw = payment.get("chain")
    ntx = _normalize_tx(tx_raw) if tx_raw is not None else None
    nch = _normalize_chain(chain_raw) if chain_raw is not None else None
    has_digest = _digest_claimed(receipt)
    has_settlement = ntx is not None and nch == CHAIN_CANON
    if not has_digest and not has_settlement:
        return True
    if tx_raw is not None and ntx is None:
        return True
    if chain_raw is not None and nch is None:
        return True
    if has_settlement and nch != CHAIN_CANON:
        return True
    return False


def _normalize_chain(chain):
    """Canonical settlement network for Base mainnet USDC, or None if unknown."""
    if not chain or not isinstance(chain, str):
        return None
    c = chain.strip().lower()
    if c in ("base", "eip155:8453", "8453"):
        return CHAIN_CANON
    return None


def _safe_capture_path(basename):
    """Resolve a regular, non-symlink capture file under CAPTURES/."""
    if not basename or not isinstance(basename, str):
        return None
    name = basename.replace("\\", "/")
    if name.startswith("/") or ".." in name.split("/"):
        return None
    if "/" in name or name.startswith("."):
        return None
    if name != os.path.basename(name):
        return None
    if not CAPTURE_NAME_RE.match(name):
        return None
    path = os.path.join(CAPTURES, name)
    root = os.path.realpath(CAPTURES)
    if os.path.islink(path):
        return None
    if not os.path.isfile(path):
        return None
    real_path = os.path.realpath(path)
    if not real_path.startswith(root + os.sep):
        return None
    return path


def _parse_line_no(line):
    """Exact non-negative int line index only; no bool/str/float coercion."""
    if type(line) is not int:
        return None
    if line < 0:
        return None
    return line


def _response_digest(raw_ref):
    """sha256 over the UTF-8 encoding of the exact archived agentcash stdout string."""
    stdout = load_raw_stdout(raw_ref)
    if stdout is None:
        return None
    return hashlib.sha256(stdout.encode("utf-8")).hexdigest()


def _receipt_version(ev):
    """v2 when settlement identity (tx+chain) and/or stdout digest is bound."""
    if ev.get("response_digest"):
        return VERSION_BOUND
    if ev.get("tx") is not None and ev.get("chain") is not None:
        return VERSION_BOUND
    return VERSION


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
    pl = _str_list(promised)
    mi = _str_list(missing)
    ex = _str_list(extra)
    ws = _why_str(why)
    if pl is None or mi is None or ex is None or ws is None:
        return None
    return {
        "url": url, "host": host,
        "quoted": _s(quoted), "charged": _s(charged),
        "paid": _b(paid), "free": _b(free),
        "tx": _normalize_tx(tx) if tx else None,
        "promised": pl,
        "observed_schema": observed_schema,
        "missing": mi, "extra": ex,
        "status": status, "why": ws,
    }


def _wrap(ev, *, kind, ts, latency_ms, raw_ref, truth=None):
    """Assemble the human- and machine-readable receipt around hashed evidence."""
    ev_h = dict(ev)
    if ev_h.get("tx") and not ev_h.get("chain"):
        ev_h["chain"] = CHAIN_CANON
    digest = _response_digest(raw_ref) if raw_ref else None
    if digest:
        ev_h["response_digest"] = digest
    rid = content_id(ev_h)
    reverified = ev_h["why"] == "shortfall confirmed on two calls"
    two_call = reverified or "re-verify" in (ev_h["why"] or "")
    r = {
        "receipt_id": rid,
        "version": _receipt_version(ev_h),
        "kind": kind,                       # delivery (field-presence) | accuracy
        "ts": ts or "",
        "seller": {"host": ev_h["host"], "url": ev_h["url"]},
        "promise": {
            "price_usdc": ev_h["quoted"],
            "fields": ev_h["promised"],
            "source": "x402 Bazaar (CDP discovery)",
        },
        "payment": {
            "charged_usdc": ev_h["charged"], "paid": ev_h["paid"], "free": ev_h["free"],
            "tx": ev_h["tx"], "chain": CHAIN_CANON,
            "settlement": "EIP-3009, submitted by a facilitator",
        },
        "delivery": {
            "latency_ms": latency_ms,
            "observed_schema": ev_h["observed_schema"],
            "missing": ev_h["missing"], "extra": ev_h["extra"],
            "raw_ref": raw_ref,             # pointer into captures/, never the goods
            **({"response_digest": digest} if digest else {}),
        },
        "verdict": {
            "status": ev_h["status"], "why": ev_h["why"],
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
                      source, dev_value, tol_value, unit, field, ts, cap_index,
                      tx=None, note=None):
    status = ("inconclusive" if returned is None
              else "accurate" if (dev_value is not None and abs(dev_value) <= tol_value)
              else "off")
    ntx = _normalize_tx(tx)
    ref = cap_index.get((url, ntx)) if (cap_index is not None and ntx) else None
    digest = _response_digest(ref) if ref else None
    ev = {
        "url": url, "host": host, "quoted": _s(quoted), "paid": _b(paid),
        "metric": metric, "returned": returned, "truth": truth,
        "truth_source": source, "dev_value": dev_value, "tol_value": tol_value,
        "unit": unit, "field": field, "status": status,
    }
    if ntx:
        ev["tx"] = ntx
        ev["chain"] = CHAIN_CANON
    if digest:
        ev["response_digest"] = digest
    rid = content_id(ev)
    dev_str = None if dev_value is None else f"{dev_value:+g} {unit}"
    return {
        "receipt_id": rid, "version": _receipt_version(ev), "kind": "accuracy", "ts": ts or "",
        "seller": {"host": host, "url": url},
        "promise": {"price_usdc": _s(quoted), "metric": metric,
                    "source": "x402 Bazaar (CDP discovery)"},
        "payment": {"charged_usdc": _s(quoted) if _b(paid) else "0",
                    "paid": _b(paid), "free": not _b(paid),
                    "tx": ntx, "chain": CHAIN_CANON,
                    "settlement": "EIP-3009, submitted by a facilitator"},
        "delivery": {"returned": returned, "field": field,
                     "raw_ref": ref,
                     **({"response_digest": digest} if digest else {})},
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


def receipts_from_price(cap_index):
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
            ts=d.get("generated", "")[:10], cap_index=cap_index, note=note))
    return out


def receipts_from_balance(cap_index):
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
            field=".usdc", ts=d.get("generated", "")[:10], cap_index=cap_index))
    return out


def receipts_from_stock(cap_index):
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
            ts=d.get("generated", "")[:10], cap_index=cap_index))
    return out


def receipts_from_lab(cap_index):
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
                field=r.get("field"), ts=ts, cap_index=cap_index,
                tx=r.get("tx")))
    return out


# ---- capture index: locate the archived raw bytes for a receipt ---------------
def _tx_of(stdout):
    if not isinstance(stdout, str):
        return None
    try:
        env = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(env, dict):
        return None
    meta = env.get("metadata")
    if not isinstance(meta, dict):
        return None
    pay = meta.get("payment")
    if not isinstance(pay, dict):
        return None
    return pay.get("transactionHash")


def build_capture_index():
    """Map (url, normalized tx) -> capture pointer only when identity is unique."""
    pending = {}
    for f in sorted(glob.glob(os.path.join(CAPTURES, "raw_*.jsonl"))):
        path = _safe_capture_path(os.path.basename(f))
        if not path:
            continue
        base = os.path.basename(path)
        with open(path) as fh:
            for n, line in enumerate(fh):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                url = rec.get("url")
                if not isinstance(url, str):
                    continue
                ntx = _normalize_tx(_tx_of(rec.get("stdout", "")))
                if not url or not ntx:
                    continue
                key = (url, ntx)
                pending.setdefault(key, []).append(
                    {"capture": base, "line": n,
                     "match": {"url": url, "tx": ntx}})
    return {k: refs[0] for k, refs in pending.items() if len(refs) == 1}


def _line_record(path, line_no):
    line_no = _parse_line_no(line_no)
    if line_no is None:
        return None
    with open(path) as fh:
        for n, line in enumerate(fh):
            if n == line_no:
                try:
                    rec = json.loads(line)
                except ValueError:
                    return None
                return rec if isinstance(rec, dict) else None
    return None


def _scan_exact_url_tx(path, url, want_tx):
    """All lines in one file matching url+tx. None if not exactly one."""
    matches = []
    with open(path) as fh:
        for n, line in enumerate(fh):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("url") != url:
                continue
            got_tx = _normalize_tx(_tx_of(rec.get("stdout", "")))
            if got_tx == want_tx:
                matches.append(rec)
    if len(matches) == 1:
        return matches[0]
    return None


def _capture_match(raw_ref):
    """Locate archived stdout by safe capture name, exact line, and url+tx identity."""
    if not isinstance(raw_ref, dict):
        return None
    path = _safe_capture_path(raw_ref.get("capture"))
    if not path:
        return None
    m = raw_ref.get("match")
    if not isinstance(m, dict):
        return None
    url = m.get("url")
    if not isinstance(url, str):
        return None
    want_tx = _normalize_tx(m.get("tx"))
    if not url or not want_tx:
        return None

    if "line" in raw_ref:
        line_no = _parse_line_no(raw_ref.get("line"))
        if line_no is None:
            return None
        rec = _line_record(path, line_no)
        if not isinstance(rec, dict):
            return None
        if rec.get("url") != url:
            return None
        if _normalize_tx(_tx_of(rec.get("stdout", ""))) != want_tx:
            return None
        return rec

    return _scan_exact_url_tx(path, url, want_tx)


def load_raw_stdout(raw_ref):
    """Exact agentcash stdout archived at pay time, before any parse."""
    rec = _capture_match(raw_ref)
    if not rec:
        return None
    stdout = rec.get("stdout")
    return stdout if isinstance(stdout, str) else None


def load_raw_payload(raw_ref):
    """Pull the seller's untouched response data from captures/, matched by
    url+tx (not a fragile line number). Returns the parsed data, or None."""
    stdout = load_raw_stdout(raw_ref)
    if stdout is None:
        return None
    try:
        return json.loads(stdout).get("data")
    except (ValueError, KeyError):
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


def evidence_of(receipt, ver=None):
    """Reconstruct the hashed evidence from a stored receipt, so anyone can
    recompute the id and confirm nothing was altered."""
    if not isinstance(receipt, dict):
        return None
    ver = ver if ver is not None else _parse_version(receipt)
    if ver is None:
        return None
    kind = receipt.get("kind")
    delivery = receipt.get("delivery")
    if not isinstance(delivery, dict):
        return None
    digest = delivery.get("response_digest")
    if kind == "accuracy":
        truth = receipt.get("truth")
        seller = receipt.get("seller")
        promise = receipt.get("promise")
        payment = receipt.get("payment")
        verdict = receipt.get("verdict")
        if not all(isinstance(x, dict) for x in (truth, seller, promise, payment, verdict)):
            return None
        ev = {
            "url": seller.get("url"), "host": seller.get("host"),
            "quoted": promise.get("price_usdc"),
            "paid": payment.get("paid"),
            "metric": promise.get("metric"),
            "returned": delivery.get("returned"), "truth": truth.get("value"),
            "truth_source": truth.get("source"), "dev_value": truth.get("dev_value"),
            "tol_value": truth.get("tol_value"), "unit": truth.get("unit"),
            "field": delivery.get("field"),
            "status": verdict.get("status"),
        }
        if ver == VERSION_BOUND:
            ntx = _normalize_tx(payment.get("tx"))
            if ntx:
                ev["tx"] = ntx
                nch = _normalize_chain(payment.get("chain"))
                if nch:
                    ev["chain"] = nch
        if ver == VERSION_BOUND and _valid_response_digest(digest):
            ev["response_digest"] = digest
        return ev
    seller = receipt.get("seller")
    promise = receipt.get("promise")
    payment = receipt.get("payment")
    verdict = receipt.get("verdict")
    if not all(isinstance(x, dict) for x in (seller, promise, payment, verdict)):
        return None
    ev = _evidence(
        url=seller.get("url"), host=seller.get("host"),
        quoted=promise.get("price_usdc"),
        charged=payment.get("charged_usdc"),
        paid=payment.get("paid"), free=payment.get("free"),
        tx=payment.get("tx"), promised=promise.get("fields"),
        observed_schema=delivery.get("observed_schema"),
        missing=delivery.get("missing"), extra=delivery.get("extra"),
        status=verdict.get("status"), why=verdict.get("why"),
    )
    if ev is None:
        return None
    if ver == VERSION_BOUND:
        ntx = _normalize_tx(payment.get("tx"))
        if ntx:
            nch = _normalize_chain(payment.get("chain"))
            if nch:
                ev["chain"] = nch
        if _valid_response_digest(digest):
            ev["response_digest"] = digest
    return ev


def verify_receipt(receipt):
    """Re-derive the receipt at all three levels. Returns a dict of pass/None
    (None = not checkable here, e.g. no archived raw for this call)."""
    out = {"integrity": False, "response": None, "verdict": None, "raw": None}
    if not isinstance(receipt, dict):
        return out
    ver = _parse_version(receipt)
    if ver is None:
        out["response"] = False if _digest_field_present(receipt) else None
        return out
    if _digest_forbidden(receipt, ver):
        out["response"] = False
        out["verdict"] = False
        return out
    if _v2_incoherent(receipt, ver):
        out["response"] = False if _digest_field_present(receipt) else None
        return out
    ev = evidence_of(receipt, ver)
    out["integrity"] = ev is not None and content_id(ev) == receipt.get("receipt_id")
    delivery = receipt.get("delivery")
    if not isinstance(delivery, dict):
        out["response"] = False if _digest_field_present(receipt) else None
        return out
    digest = delivery.get("response_digest")
    if digest is None:
        out["response"] = None
    elif not _valid_response_digest(digest):
        out["response"] = False
    else:
        payment = receipt.get("payment") if isinstance(receipt.get("payment"), dict) else {}
        raw_ref = delivery.get("raw_ref")
        if raw_ref is not None and not isinstance(raw_ref, dict):
            out["response"] = False
        else:
            pay_tx = _normalize_tx(payment.get("tx"))
            ref_match = raw_ref.get("match") if isinstance(raw_ref, dict) else None
            ref_tx = (_normalize_tx(ref_match.get("tx"))
                      if isinstance(ref_match, dict) else None)
            if pay_tx and ref_tx and pay_tx != ref_tx:
                out["response"] = False
            else:
                got = _response_digest(raw_ref) if isinstance(raw_ref, dict) else None
                out["response"] = (got == digest) if got is not None else None
    if receipt.get("kind") == "accuracy":
        truth = receipt.get("truth")
        verdict = receipt.get("verdict")
        if not isinstance(truth, dict) or not isinstance(verdict, dict):
            return out
        ret = delivery.get("returned")
        dev = _compare_float(truth.get("dev_value"))
        tol = _compare_float(truth.get("tol_value"))
        if ret is None:
            expect = "inconclusive"
        elif dev is not None and tol is not None and abs(dev) <= tol:
            expect = "accurate"
        else:
            expect = "off"
        out["verdict"] = expect == verdict.get("status")
        return out
    promise = receipt.get("promise")
    verdict = receipt.get("verdict")
    if not isinstance(promise, dict) or not isinstance(verdict, dict):
        return out
    why = _why_str(verdict.get("why"))
    if why is None:
        out["verdict"] = False
    elif "re-verify" not in why:
        fields = _str_list(promise.get("fields"))
        if fields is None:
            out["verdict"] = False
        else:
            regraded = conform.judge(schema_probe(delivery.get("observed_schema")),
                                     fields)
            out["verdict"] = regraded["status"] == verdict.get("status")
    raw_ref = delivery.get("raw_ref")
    payload = load_raw_payload(raw_ref) if isinstance(raw_ref, dict) else None
    if payload is None:
        out["raw"] = None
    else:
        out["raw"] = (conform.describe_shape(payload)
                      == delivery.get("observed_schema"))
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
    acc = (receipts_from_price(cap_index) + receipts_from_balance(cap_index)
           + receipts_from_stock(cap_index) + receipts_from_lab(cap_index))
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
                 "response": "1b.RESPONSE (archived agentcash stdout digest matches the binding)",
                 "verdict": "2. VERDICT   (re-judged offline, no network)",
                 "raw": "3. RAW       (shape re-derived from archived bytes)"}
        for k in ("integrity", "response", "verdict", "raw"):
            mark = {True: "PASS", False: "FAIL", None: "n/a "}[v[k]]
            print(f"   [{mark}] {names[k]}")
        return
    print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
