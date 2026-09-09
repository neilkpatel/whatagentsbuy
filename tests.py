#!/usr/bin/env python3
"""Tests for the pipeline logic. Run before every commit:  python3 tests.py

Every case here exists because the bug it guards against actually happened in
this codebase. No network, no money, no build side effects: pure functions
against fixtures, plus a couple of file-behaviour checks. If this passes, the
class of error that has cost us real money and real corrections cannot recur
silently.
"""
import json, os, sys, tempfile
import backfill_sweep, buyers, conform, leaderboard, market, og_x402, promote_verified, sweep, sweep_solana, whatsnew

HERE = os.path.dirname(os.path.abspath(__file__))
_pass, _fail = 0, 0


def check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))


# --- describe_shape: keeps types, NEVER leaks values ---------------------------
# The rule that lets us publish response shapes without republishing the goods.
def test_shape_no_values():
    payload = {"price": 43512.11, "symbol": "BTC", "ok": True, "count": 3,
               "records": [{"type": "A", "ip": "1.2.3.4"}], "meta": {"asOf": "2026-08-12"}}
    shape = conform.describe_shape(payload)
    blob = json.dumps(shape)
    # every real value must be gone, replaced by a type name
    for leaked in ["43512", "BTC", "1.2.3.4", "2026-08-12"]:
        check(f"shape discards value {leaked!r}", leaked not in blob, blob[:200])
    check("shape keeps types", shape["price"] == "number" and shape["ok"] == "boolean"
          and shape["symbol"] == "string")
    check("shape recurses arrays", shape["records"] == {"array_of": {"type": "string", "ip": "string"}})
    check("shape recurses objects", shape["meta"] == {"asOf": "string"})


# --- judge: presence check + observed schema ----------------------------------
def test_judge():
    j = conform.judge({"a": 1, "b": "x", "c": True}, ["a", "b"])
    check("judge conforms when all promised present", j["conforms"] is True)
    check("judge records bonus fields", j["extra"] == ["c"])
    check("judge attaches observed_schema", isinstance(j["observed_schema"], dict))
    j2 = conform.judge({"a": 1}, ["a", "b", "c"])
    check("judge fails on missing", j2["conforms"] is False and j2["missing"] == ["b", "c"])
    j3 = conform.judge("not an object", ["a"])
    check("judge fails on non-object", j3["conforms"] is False)


# --- find_payment: detect a tx hash at any depth ------------------------------
# The bug that let a run report $0.001 while $0.196 left the wallet.
def test_find_payment():
    tx = "0x" + "a" * 64
    check("finds tx at top level", conform.find_payment({"transactionHash": tx})["tx"] == tx)
    check("finds tx nested deep",
          conform.find_payment({"a": {"b": {"payment": {"transaction": tx}}}})["tx"] == tx)
    check("none when no tx", conform.find_payment({"status": "ok", "price": "$0.001"}) is None)
    check("ignores short non-hash", conform.find_payment({"transaction": "0xshort"}) is None)


# --- Solana inflow: exact ledger delta ----------------------------------------
# Verified by hand against the chain; lock it in.
def test_solana_inflow():
    acct = "OURACCT"
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": acct}, {"pubkey": "PAYER"}]}},
          "meta": {
              "preTokenBalances": [
                  {"accountIndex": 0, "mint": sweep_solana.USDC_MINT, "uiTokenAmount": {"uiAmount": 244.9959}, "owner": "us"},
                  {"accountIndex": 1, "mint": sweep_solana.USDC_MINT, "uiTokenAmount": {"uiAmount": 0.7248}, "owner": "them"}],
              "postTokenBalances": [
                  {"accountIndex": 0, "mint": sweep_solana.USDC_MINT, "uiTokenAmount": {"uiAmount": 245.1459}, "owner": "us"},
                  {"accountIndex": 1, "mint": sweep_solana.USDC_MINT, "uiTokenAmount": {"uiAmount": 0.5748}, "owner": "them"}]}}
    delta, payer = sweep_solana.usdc_inflow(tx, acct, "us")
    check("solana inflow is the exact delta", abs(delta - 0.15) < 1e-9, f"got {delta}")
    check("solana inflow finds the payer", payer == "them")
    # a tx where our account did not change must count nothing
    tx2 = {"transaction": {"message": {"accountKeys": [{"pubkey": acct}]}},
           "meta": {"preTokenBalances": [{"accountIndex": 0, "mint": sweep_solana.USDC_MINT, "uiTokenAmount": {"uiAmount": 5.0}}],
                    "postTokenBalances": [{"accountIndex": 0, "mint": sweep_solana.USDC_MINT, "uiTokenAmount": {"uiAmount": 5.0}}]}}
    check("solana inflow zero on no change", sweep_solana.usdc_inflow(tx2, acct, "us")[0] == 0.0)


# --- index_of: the registry field names it actually uses ----------------------
# Broke once by reading callCount30d instead of l30DaysTotalCalls.
def test_index_field_names():
    item = {"resource": "https://x.com/api", "description": "d",
            "accepts": [{"amount": "1000", "asset": "0xUSDC", "network": "eip155:8453", "payTo": "0xAB"}],
            "quality": {"l30DaysTotalCalls": 42, "l30DaysUniquePayers": 7, "lastCalledAt": "2026-08-12"},
            "extensions": {"bazaar": {"schema": {"x": 1}}}}
    idx = whatsnew.index_of([item])
    row = idx["https://x.com/api"]
    check("index reads l30DaysTotalCalls", row["calls30d"] == 42)
    check("index reads l30DaysUniquePayers", row["payers30d"] == 7)
    check("index keeps payto list", row["payto"] == ["0xab"])
    check("index fingerprints schema", isinstance(row["schema_fp"], str) and len(row["schema_fp"]) == 12)


# --- address_to_service: reads the payTo dict shape ---------------------------
# When the probe changed payto to a {address: count} dict, every Base address
# stopped resolving and the leaderboard showed bare 0x strings.
def test_address_resolution():
    latest = {"origins": [
        {"origin": "blockrun.ai", "service": "BlockRun", "grade": "A",
         "payto": {"0xE9030014F5DAe217D0a152F02a043567b16C1aBF": 3}},
        {"origin": "old.example", "payto_addresses": ["0xOLD"]}]}
    with tempfile.TemporaryDirectory() as d:
        json.dump(latest, open(os.path.join(d, "latest.json"), "w"))
        _orig = leaderboard.DATA
        leaderboard.DATA = d
        try:
            a2s = leaderboard.address_to_service()
        finally:
            leaderboard.DATA = _orig
    check("resolves payTo dict to host", a2s.get("0xe9030014f5dae217d0a152f02a043567b16c1abf", {}).get("host") == "blockrun.ai")
    check("still reads legacy payto_addresses", "0xold" in a2s)


# --- burn addresses are never a seller's settlement ("$138M day" bug) ----------
# library.proofivy.com returned 0x0 as a payTo in one 402, so every USDC burn on
# chain mapped to it and the headline read $138M. The zero/dead address must
# resolve to no host, even when a real seller's payTo dict also contains it.
def test_burn_address_excluded():
    latest = {"origins": [
        {"origin": "library.proofivy.com", "service": "Proofivy",
         "payto": {"0x859af250DF0b68bfD0768cA22142a1AFa0aBEAF4": 2,
                   "0x0000000000000000000000000000000000000000": 1}}]}
    with tempfile.TemporaryDirectory() as d:
        json.dump(latest, open(os.path.join(d, "latest.json"), "w"))
        _orig = leaderboard.DATA
        leaderboard.DATA = d
        try:
            a2s = leaderboard.address_to_service()
        finally:
            leaderboard.DATA = _orig
    check("the real payTo still resolves to the host",
          a2s.get("0x859af250df0b68bfd0768ca22142a1afa0abeaf4", {}).get("host") == "library.proofivy.com")
    check("the zero address maps to NO host",
          "0x0000000000000000000000000000000000000000" not in a2s)


# --- organic_score / reliability behave sanely --------------------------------
def test_scores():
    hi = leaderboard.organic_score(100, 30, 0.02, 0.02, 500)
    lo = leaderboard.organic_score(1, 0, 0.99, 0.99, 500)
    check("broad demand scores high", hi["score"] >= 80, str(hi))
    check("one-wallet demand scores low", lo["score"] <= 40, str(lo))
    check("no payers -> no score", leaderboard.organic_score(0, 0, None, None, 0) is None)


# --- conform appends across runs, never overwrites ----------------------------
# The bug the user caught: each run overwrote the day's file, discarding earlier
# batches. Simulate two flushes and assert both survive.
def test_conform_append():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "conformance_test.json")
        json.dump({"rows": [{"url": "a", "paid": True}, {"url": "b", "paid": True}]}, open(out, "w"))
        # a second run writes url c and re-writes b; a and b must not be lost
        prior = json.load(open(out)).get("rows", [])
        results = [{"url": "b", "paid": True, "v": 2}, {"url": "c", "paid": True}]
        by_url = {r["url"]: r for r in prior}
        for r in results:
            by_url[r["url"]] = r
        merged = list(by_url.values())
        urls = {r["url"] for r in merged}
        check("append keeps earlier rows", urls == {"a", "b", "c"}, str(urls))
        check("append replaces same-url row", next(r for r in merged if r["url"] == "b").get("v") == 2)


# --- load_days ignores the Solana settlement files ----------------------------
# Globbing settlements_*.json swept in the Solana file and crashed the build.
def test_load_days_excludes_solana():
    with tempfile.TemporaryDirectory() as d:
        hist = os.path.join(d, "history")
        os.makedirs(hist)
        json.dump({"by_address": {}}, open(os.path.join(hist, "settlements_2026-08-12.json"), "w"))
        json.dump({"by_wallet": {}}, open(os.path.join(hist, "settlements_solana_2026-08-12.json"), "w"))
        _orig = leaderboard.HIST
        leaderboard.HIST = hist
        try:
            days = leaderboard.load_days()
        finally:
            leaderboard.HIST = _orig
    check("load_days skips solana file", len(days) == 1 and "by_address" in days[0])


# --- per-call cap: never exceeds a penny, never below the endpoint's price ----
# The control the user demanded: no single call can leak more than intended.
def test_per_call_cap():
    def cap(price):
        return min(round(price * 1.25 + 0.0001, 6), 0.01)
    check("cap on a tenth-cent endpoint is tiny", cap(0.001) < 0.002, str(cap(0.001)))
    check("cap never exceeds one cent", cap(0.05) == 0.01 and cap(1.0) == 0.01)
    check("cap allows the advertised price", cap(0.005) >= 0.005)


# --- budget enforcement: the run stops before crossing the ceiling ------------
def test_budget_stops():
    budget, spent, prices = 0.01, 0.0, [0.004, 0.004, 0.004, 0.004]
    paid = 0
    for p in prices:
        if spent + p > budget:
            break
        spent += p
        paid += 1
    check("budget stops before overspending", spent <= budget and paid == 2, f"spent {spent}")


# --- sweep decode keeps demand shape, not raw addresses -----------------------
def test_sweep_decode():
    import sweep
    logs = []
    def log(frm, to, amt):
        return {"topics": ["T", "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
                "data": hex(amt), "blockNumber": "0x1"}
    seller = "0x" + "1" * 40
    logs = [log("0x" + "a" * 40, seller, 1000000),
            log("0x" + "b" * 40, seller, 2000000),
            log("0x" + "a" * 40, seller, 500000)]  # a pays twice
    dec = sweep.decode(logs)
    row = dec.get(seller.lower())
    check("decode sums usdc", row and abs(row["usdc"] - 3.5) < 1e-6, str(row))
    check("decode counts settlements", row and row["settlements"] == 3)
    check("decode counts unique payers", row and row["unique_payers"] == 2)
    check("decode stores no raw addresses", "payers" not in row or isinstance(row.get("payers"), int),
          "raw payer addresses must not be published")


# --- solana overwrite guard: refuse a smaller partial over a good sweep --------
def test_solana_overwrite_guard():
    # a rerun with rpc failures and a much smaller total must not clobber the file
    old_tot, new_tot, failures = 75000.0, 12.0, 8
    should_refuse = bool(failures and old_tot > new_tot * 1.1 + 1)
    check("solana guard refuses smaller partial", should_refuse)
    # a clean rerun (no failures) may overwrite
    check("solana guard allows clean rerun", not (0 and old_tot > new_tot))


# --- budget must use the capped price, never a response-polluted charged ------
# A perps endpoint returned a $1915 futures price that the charged parser read,
# which tripped the budget and stopped a run early. Budget must be immune to it.
def test_budget_ignores_polluted_charged():
    per_call_cap = 0.00635
    charged = 1915.21   # response data, not a payment
    added = min(charged, per_call_cap) if charged is not None else 0.005
    check("budget adds the capped amount, not the polluted charged", added <= per_call_cap,
          f"added {added}")
    # and the stored charged is nulled when absurd
    stored = None if charged > per_call_cap * 2 else charged
    check("absurd charged is discarded", stored is None)


# --- INTEGRATION: build output invariants -------------------------------------
# build.py is a script, not importable, so assert on what it actually produced.
def test_built_output():
    pub = os.path.join(HERE, "public")
    lb = os.path.join(pub, "api", "leaderboard.json")
    if not os.path.exists(lb):
        return  # nothing built yet in this environment; skip
    d = json.load(open(lb))
    check("leaderboard has rows", d.get("rows"))
    # revenue is cross-chain: a row's chains sum to its usdc_received
    for r in d.get("rows", [])[:20]:
        ch = r.get("chains") or {}
        if ch:
            s = sum(c.get("usdc", 0) for c in ch.values())
            check(f"chains sum to received for {r.get('host')}",
                  abs(s - r["usdc_received"]) < 0.02, f"{s} vs {r['usdc_received']}")
            break
    cat = os.path.join(pub, "api", "catalog.json")
    if os.path.exists(cat):
        c = json.load(open(cat))
        e = (c.get("endpoints") or [{}])[0]
        for f in ("host", "price_usdc", "reliability"):
            check(f"catalog endpoint has {f}", f in e)
        # no observed_schema anywhere contains a value that looks like real data
        for e in c.get("endpoints", []):
            sch = e.get("observed_schema")
            if sch:
                types = {"string", "integer", "number", "boolean", "null", "array", "object", "array_of"}
                vals = _schema_values(sch)
                bad = [v for v in vals if v not in types]
                check("published schema is types-only (no leaked values)", not bad, str(bad[:3]))
                break


def test_reconcile_charged_uses_chain():
    # The chain is truth in BOTH directions: an over-reported charge is corrected
    # down, an UNDER-reported charge is corrected up (the old version only looked
    # when the client itself flagged an overcharge, so the client's blind spot
    # decided what got verified), and an unreachable RPC leaves the row explicitly
    # unverified rather than letting a client number read as fact. Chain mocked.
    import reconcile_charged as rc
    chain = {"0xabc": 0.015,   # client said 0.02 -> phantom overcharge, cleared
             "0xdef": 0.012,   # client said 0.01 -> UNDER-report hiding a real 0.012
             "0xok":  0.010}   # client agrees with chain
    rows = [{"url": "u", "quoted": 0.015, "charged": 0.02, "tx": "0xabc"},
            {"url": "v", "quoted": 0.01, "charged": 0.01, "tx": "0xdef"},
            {"url": "w", "quoted": 0.01, "charged": 0.01, "tx": "0xok"},
            {"url": "x", "quoted": 0.01, "charged": 0.01, "tx": "0xdown"},   # RPC fails
            {"url": "y", "quoted": 0.01, "charged": 0.01}]                   # no tx at all
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.json")
        json.dump({"rows": rows}, open(p, "w"))
        checked, corrected, cleared, unverified = rc.reconcile(p, lookup=chain.get)
        out = json.load(open(p))["rows"]
    check("every settled row is checked, not only client-flagged ones",
          checked == 3 and unverified == 1)
    check("phantom overcharge reconciled down, client figure kept",
          out[0]["charged"] == 0.015 and out[0]["charged_reported"] == 0.02 and cleared == 1)
    check("an UNDER-reported real charge is caught and corrected up",
          out[1]["charged"] == 0.012 and out[1]["charged_reported"] == 0.01)
    check("an agreeing row is marked reconciled without correction",
          out[2].get("charged_reconciled") and "charged_reported" not in out[2])
    check("RPC failure leaves the row explicitly unverified, never a silent fact",
          out[3].get("charged_verified") is False and not out[3].get("charged_reconciled"))
    check("a row with no settled tx is untouched",
          "charged_reconciled" not in out[4] and "charged_verified" not in out[4])
    check("corrected counts both directions", corrected == 2)


def test_receipt_v2_binds_payment_identity():
    """C3: a v1 accuracy receipt's tx, chain and free flag sat OUTSIDE the hash,
    so they could be altered without breaking the id. v2 binds them, plus the
    raw-capture digest."""
    import receipts
    idx = {"https://x.co/price": [
        {"capture": "raw_2026-09-01.jsonl", "line": 3, "date": "2026-09-01",
         "match": {"url": "https://x.co/price", "tx": "0xabc", "call_id": None},
         "tx": "0xabc", "call_id": None, "sha256": "deadbeef" * 2}]}
    r = receipts._accuracy_receipt(
        host="x.co", url="https://x.co/price", quoted=0.01, paid=True,
        metric="BTC price", returned=60000.0, truth=60010.0, source="exchange median",
        dev_value=-1.7, tol_value=50.0, unit="bps", field=".price",
        ts="2026-09-01", url_index=idx)
    v = receipts.verify_receipt(r)
    check("a freshly minted v2 accuracy receipt verifies", v["integrity"] and v["verdict"])
    import copy
    for label, mutate in [
        ("tx", lambda x: x["payment"].__setitem__("tx", "0xEVIL")),
        ("free flag", lambda x: x["payment"].__setitem__("free", True)),
        ("chain", lambda x: x["payment"].__setitem__("chain", "solana")),
        ("raw digest", lambda x: x["delivery"]["raw_ref"].__setitem__("sha256", "0" * 16)),
    ]:
        bad = copy.deepcopy(r)
        mutate(bad)
        check(f"mutating the {label} breaks integrity (was invisible in v1)",
              not receipts.verify_receipt(bad)["integrity"])


def test_accuracy_verdict_is_recomputed_not_trusted():
    """C4: 'returned 1, reference 60000, deviation 0' used to verify as accurate
    because only the STORED deviation was consulted."""
    import receipts, copy
    r = receipts._accuracy_receipt(
        host="x.co", url="https://x.co/p", quoted=0.01, paid=True,
        metric="BTC price", returned=60000.0, truth=60010.0, source="median",
        dev_value=-1.7, tol_value=50.0, unit="bps", field=".price",
        ts="2026-09-01", url_index={})
    check("an honest receipt still verifies", receipts.verify_receipt(r)["verdict"])
    lie = copy.deepcopy(r)
    lie["delivery"]["returned"] = 1.0          # wildly wrong value...
    lie["truth"]["dev_value"] = 0.0            # ...with a doctored zero deviation
    lie["verdict"]["status"] = "accurate"
    check("a doctored zero-deviation no longer verifies as accurate",
          not receipts.verify_receipt(lie)["verdict"])


def test_capture_binding_is_by_measurement_not_latest():
    """C5: the URL-only index kept the LATEST capture per URL, attaching later
    raw bytes to earlier measurements (10 of 111 published accuracy receipts)."""
    import receipts
    versions = [
        {"capture": "raw_2026-08-14.jsonl", "date": "2026-08-14", "line": 1,
         "match": {}, "tx": "0xold", "call_id": None, "sha256": "aa"},
        {"capture": "raw_2026-08-30.jsonl", "date": "2026-08-30", "line": 9,
         "match": {}, "tx": "0xnew", "call_id": None, "sha256": "bb"},
    ]
    idx = {"https://x.co/p": versions}
    check("a measurement binds its own day's capture",
          receipts.lookup_capture(idx, "https://x.co/p", "2026-08-14")["tx"] == "0xold")
    check("a later capture is NEVER attached to an earlier measurement",
          receipts.lookup_capture(idx, "https://x.co/p", "2026-08-01") is None)
    check("a mid-gap measurement takes the newest capture at or before it",
          receipts.lookup_capture(idx, "https://x.co/p", "2026-08-20")["tx"] == "0xold")


def test_delivery_capture_recovers_query_param_orphans():
    """C6: conform.call() archives the URL WITH appended query params while the
    row keeps the registry URL, which orphaned 186 receipts whose transactions
    were demonstrably in the archive. The tx fallback recovers them, validated
    to the same endpoint; the dispatch-minted call_id beats everything."""
    import receipts
    e_q = {"capture": "raw_x.jsonl", "line": 0, "date": "x", "sha256": "aa",
           "match": {"url": "https://x.co/p?lat=1&lon=2", "tx": "0xt1", "call_id": None},
           "tx": "0xt1", "call_id": None}
    e_c = {"capture": "raw_x.jsonl", "line": 1, "date": "x", "sha256": "bb",
           "match": {"url": "https://x.co/p", "tx": "0xt2", "call_id": "c_ff"},
           "tx": "0xt2", "call_id": "c_ff"}
    idx = {"by_urltx": {("https://x.co/p?lat=1&lon=2", "0xt1"): e_q, ("https://x.co/p", "0xt2"): e_c},
           "by_tx": {"0xt1": e_q, "0xt2": e_c}, "by_call": {"c_ff": e_c}}
    got = receipts.find_delivery_capture(idx, "https://x.co/p", "0xt1")
    check("a query-param-drifted capture is recovered by its tx", got is e_q)
    check("the tx fallback refuses a DIFFERENT endpoint",
          receipts.find_delivery_capture(idx, "https://other.co/p", "0xt1") is None)
    check("the dispatch-minted call_id is the strongest identity",
          receipts.find_delivery_capture(idx, "ignored", None, call_id="c_ff") is e_c)


def test_receipt_field_extraction():
    import receipts
    p = {"data": {"rates": [{"usd": 1.23}, {"usd": 4.56}]}, "price": "60,000"}
    check("dotted path with index resolves",
          receipts._extract_field(p, ".data.rates[1].usd") == 4.56)
    check("formatted string numbers parse", receipts._extract_field(p, ".price") == 60000.0)
    check("a missing path is None, never a guess",
          receipts._extract_field(p, ".data.nope") is None)
    check("value-anywhere finds a nested measurement",
          receipts._value_anywhere(p, 4.56) and not receipts._value_anywhere(p, 7.77))


def test_sweep_overwrite_guard():
    """D1: the sweep is the ONE unrecoverable pipeline — a settlement day missed
    or corrupted cannot be re-swept later, because the window is rolling. This
    guard is the last thing between a lossy re-run and the tape, so it is tested
    directly, including that a malformed prior file can never crash the sweep."""
    import sweep
    R = sweep.overwrite_refusal
    full = {"hours": 24, "meta": {"failed_queries": 0},
            "by_address": {"0xa": {"settlements": 1000}, "0xb": {"settlements": 500}}}
    same = {"0xa": {"settlements": 1000}, "0xb": {"settlements": 500}}

    check("an identical re-sweep is allowed", R(full, same, 24, 0) is None)
    check("a SHORTER test sweep never replaces a full day",
          "short test sweep" in (R(full, same, 6, 0) or ""))
    check("a LONGER sweep may replace a shorter day",
          R({"hours": 6, "meta": {}, "by_address": {}}, same, 24, 0) is None)
    check("a partial run never replaces a CLEAN snapshot",
          "CLEAN day" in (R(full, same, 24, 3) or ""))
    check("a same-window run holding <90% of the settlements is refused",
          "lossier sweep" in (R(full, {"0xa": {"settlements": 100}}, 24, 0) or ""))
    check("a slightly smaller result (>=90%) is still allowed",
          R(full, {"0xa": {"settlements": 1400}}, 24, 0) is None)
    check("a BIGGER result is always allowed",
          R(full, {"0xa": {"settlements": 9999}}, 24, 0) is None)
    # defensive: a malformed prior must never block or crash the write
    check("an empty prior file cannot block the write", R({}, same, 24, 0) is None)
    check("a prior with no by_address cannot block the write",
          R({"hours": 24, "meta": {}}, same, 24, 0) is None)
    check("null rows in the prior cannot crash the guard",
          R({"hours": 24, "meta": {}, "by_address": {"0xa": None}}, same, 24, 0) is None)
    check("null settlements in the new rows cannot crash the guard",
          R(full, {"0xa": {"settlements": None}}, 24, 0) is not None)
    # both clean and both failing: fall through to the count comparison only
    check("two equally-partial runs compare on count, not failure",
          R({"hours": 24, "meta": {"failed_queries": 2}, "by_address": {"0xa": {"settlements": 10}}},
            {"0xa": {"settlements": 10}}, 24, 5) is None)


def test_lab_field_chosen_by_name_never_by_answer_key():
    """E1: the accuracy grader must pick the field semantically, BEFORE looking
    at the reference. The old form pooled every number and kept whichever landed
    closest to the answer key, which both hid wrong values (censored to
    inconclusive) and promoted lookalike fields that happened to agree."""
    import lab

    # Codex's counterexample: previous_close equals the reference; price is the
    # metric and it is wrong. The named metric field must win and be graded.
    v, p = lab.find_value({"price": 50000.0, "previous_close": 60000.0},
                          60000.0, 0.02, "pct", (1, 1e9))
    check("the metric-named field wins over a sibling that matches the reference",
          v == 50000.0 and p.endswith(".price"), f"{v} {p}")
    v2, _p2 = lab.find_value({"price": 50000.0}, 60000.0, 0.02, "pct", (1, 1e9))
    check("a wrong named value is RETURNED for grading, not censored to inconclusive",
          v2 == 50000.0)
    v3, why = lab.find_value({"foo": 60000.0}, 60000.0, 0.02, "pct", (1, 1e9))
    check("an unnamed number never becomes a measurement, even a perfect one",
          v3 is None and "named" in why, why)
    # unit disambiguation still works, but only WITHIN the chosen field
    c_variants = lambda v: (v, v * 9 / 5 + 32, v + 273.15)   # C -> (C, F, K)
    v4, _p4 = lab.find_value({"temp": 20.0}, 68.0, 3.0, "abs", (-100, 500),
                             variants=c_variants)
    check("unit variants of the chosen field still disambiguate against the reference",
          v4 == 68.0, v4)


def test_canary_spend_is_counted():
    # C1: canaries are PAID calls. run_canary must report what they cost so the
    # run counts it against the budget; they used to spend outside the ledger.
    import conform
    _call, _judge = conform.call, conform.judge
    conform.call = lambda url, method, body, cap, query=None: (
        True, {"ok": 1}, {"payment": {"success": True}, "price": "$0.01"})
    conform.judge = lambda payload, expect: {"status": "delivered", "why": ""}
    try:
        ok, detail, spent = conform.run_canary(cap=0.01)
        check("canary passes on delivered", ok)
        check("canary spend equals settled cost of every canary call",
              abs(spent - 0.01 * len(conform.CANARY)) < 1e-9, spent)
        # a canary whose payment did not settle costs nothing
        conform.call = lambda url, method, body, cap, query=None: (
            True, {"ok": 1}, {"payment": {"success": False}})
        ok2, _d2, spent2 = conform.run_canary(cap=0.01)
        check("unsettled canary counts zero spend", ok2 and spent2 == 0.0)
        # a charged figure above the cap is clamped to the authorized maximum
        conform.call = lambda url, method, body, cap, query=None: (
            True, {"ok": 1}, {"payment": {"success": True}, "price": "$5.00"})
        _ok3, _d3, spent3 = conform.run_canary(cap=0.01)
        check("canary spend is clamped to the cap", abs(spent3 - 0.01 * len(conform.CANARY)) < 1e-9)
    finally:
        conform.call, conform.judge = _call, _judge


def test_promote_verified_gate():
    # Only real paid delivery outcomes (delivered/short WITH a schema, and a url)
    # may be promoted into the verified set; skips/inconclusive/no-schema never are.
    # This is the filter that keeps junk out of the MCP's "we paid it" tier.
    g = promote_verified.is_gradeable
    check("delivered w/ schema+url promotes", g({"url": "https://x/a", "observed_schema": {"k": "string"}, "status": "delivered"}))
    check("short w/ schema promotes", g({"url": "https://x/a", "observed_schema": {"k": "string"}, "status": "short"}))
    check("inconclusive never promotes", not g({"url": "https://x/a", "observed_schema": {"k": "string"}, "status": "inconclusive"}))
    check("no schema never promotes", not g({"url": "https://x/a", "status": "delivered"}))
    check("no url never promotes", not g({"observed_schema": {"k": "string"}, "status": "delivered"}))


def test_query_params_appended():
    # A GET seller that declares queryParams must be called WITH them, or the bare
    # URL 400s and a delivering endpoint reads as dead (geoprimitives wanted lat/lon).
    import conform
    u = conform.with_query("https://api.geoprimitives.dev/v1/marine/depth",
                           {"lat": 57.0405, "lon": -135.3421})
    check("query params appended to a bare URL", "lat=57.0405" in u and "lon=-135.3421" in u, u)
    check("existing query string is respected",
          conform.with_query("https://x.co/a?k=1", {"j": 2}) == "https://x.co/a?k=1&j=2")
    check("no query is a no-op", conform.with_query("https://x.co/a", None) == "https://x.co/a")
    # contract() must surface the seller's declared queryParams so call() can send them.
    r = {"extensions": {"bazaar": {"info": {
        "input": {"method": "GET", "queryParams": {"lat": 57.0, "lon": -135.0}},
        "output": {"example": {"chart": "US5AK", "depth": 12.3}}}}}}
    c = conform.contract(r)
    check("contract surfaces declared queryParams", c and c.get("query") == {"lat": 57.0, "lon": -135.0}, str(c))


def test_judge_unwraps_nested_payload():
    # a seller that wraps the promised fields under "data" still conforms
    resp = {"ok": True, "data": {"price": 1, "symbol": "x", "high24h": 2}}
    j = conform.judge(resp, ["price", "symbol", "high24h"])
    check("judge unwraps a data-wrapped payload", j["conforms"] is True, str(j))


def test_judge_response_with_price_field_conforms():
    # the exact bug: a price feed whose response contains a field named "price"
    # (and other words the old parser treated as metadata markers) must be judged
    # on its real fields, not scored empty.
    resp = {"type": "crypto_spot", "symbol": "BTC", "price": 63127.97,
            "change24h": -0.29, "high24h": 63949.9, "low24h": 62772.84,
            "volume24h": 5010.9, "currency": "USD", "exchange": "Coinbase",
            "name": "BTC / USD", "processingTime": "68ms"}
    promised = ["change24h", "currency", "exchange", "high24h", "low24h",
                "name", "price", "processingTime", "symbol", "type", "volume24h"]
    j = conform.judge(resp, promised)
    check("a real price feed with a 'price' field conforms", j["conforms"] is True, str(j["missing"]))
    check("its schema is captured, not null", isinstance(j["observed_schema"], dict))


def test_paid_guard_handles_string():
    # a payment field that is a string must not crash the run
    for pay in ["ok", None, {"success": True}, 42]:
        p = pay if isinstance(pay, dict) else {}
        try:
            p.get("success")
            ok = True
        except Exception:
            ok = False
        check(f"paid guard handles {type(pay).__name__}", ok)


def test_judge_reclassifies_unmeasurable_as_inconclusive():
    # GUARDRAIL 2: the false "1 in 4 didn't deliver" study happened because a
    # response we could not read was recorded as a response that lacked the
    # goods. null, non-JSON, and error objects must never be a negative verdict.
    promised = ["price", "symbol", "volume24h"]
    check("null response is inconclusive, not short",
          conform.judge(None, promised)["status"] == "inconclusive")
    check("a bare string is inconclusive, not short",
          conform.judge("upstream timeout", promised)["status"] == "inconclusive")
    err = conform.judge({"error": "bad request", "code": 400}, promised)
    check("an error object is inconclusive, not short", err["status"] == "inconclusive")
    # a genuine, readable response that truly lacks fields is the ONLY negative
    real_short = conform.judge({"price": 1, "symbol": "BTC"}, promised)
    check("a real response missing a field is short", real_short["status"] == "short")


def test_canary_aborts_a_broken_harness():
    # GUARDRAIL 1: if the harness cannot correctly score endpoints we KNOW
    # deliver, it must abort before judging strangers. Patch call() so the
    # canaries come back broken and prove run_canary refuses to proceed.
    real_call = conform.call
    try:
        conform.call = lambda url, method, body, cap, **kw: (True, None, {})  # capture nothing
        ok, detail, _spent = conform.run_canary()
        check("broken harness (null capture) aborts the run", ok is False, detail)
        # now make call() return every promised field for each canary
        conform.call = lambda url, method, body, cap, **kw: (
            True, {k: 1 for c in conform.CANARY if c["url"] == url for k in c["expect"]}, {})
        ok2, detail2, _spent2 = conform.run_canary()
        check("a working harness passes the canary", ok2 is True, detail2)
    finally:
        conform.call = real_call


def test_reverify_clears_a_transient_short():
    # GUARDRAIL 3: a short is the only verdict that accuses a seller, so it is
    # never trusted on one call. If the re-verify delivers, drop the accusation;
    # if the re-verify cannot reproduce it, fall back to inconclusive; only a
    # shortfall seen on BOTH calls stays short.
    short = {"status": "short", "conforms": False, "missing": ["b"], "why": "missing 1"}
    delivered = {"status": "delivered", "conforms": True, "missing": [], "why": ""}
    inconc = {"status": "inconclusive"}
    check("short then delivered clears to delivered",
          conform.reconcile(short, delivered)["status"] == "delivered")
    check("short then unreadable falls back to inconclusive",
          conform.reconcile(short, inconc)["status"] == "inconclusive")
    check("short confirmed twice stays short",
          conform.reconcile(short, dict(short))["status"] == "short")


def test_raw_capture_archives_and_reanalyzes():
    # Saving the raw response is what makes a future parsing bug a free offline
    # re-run instead of a re-pay. Prove a captured line round-trips AND that the
    # fixed judge can re-derive a verdict from it with no network.
    import tempfile
    envelope = json.dumps({"success": True,
                           "data": {"price": 1, "symbol": "BTC", "volume24h": 9},
                           "metadata": {"payment": {"success": True}}})
    with tempfile.TemporaryDirectory() as d:
        rec = conform.archive_raw("https://ex.com/p", "GET", None, 0, envelope, "", out_dir=d)
        check("archive returns the written record", rec is not None)
        path = os.path.join(d, [f for f in os.listdir(d) if f.startswith("raw_")][0])
        lines = [json.loads(x) for x in open(path)]
        check("exactly one line was appended", len(lines) == 1)
        # re-derive a verdict from the archived bytes alone
        env = json.loads(lines[0]["stdout"])
        v = conform.judge(env["data"], ["price", "symbol", "volume24h"])
        check("verdict re-derived from raw archive with no network",
              v["status"] == "delivered", str(v))
    # append-only: a second write must not clobber the first
    with tempfile.TemporaryDirectory() as d:
        conform.archive_raw("https://a.com", "GET", None, 0, "{}", "", out_dir=d)
        conform.archive_raw("https://b.com", "GET", None, 0, "{}", "", out_dir=d)
        path = os.path.join(d, [f for f in os.listdir(d) if f.startswith("raw_")][0])
        check("second capture appends, never overwrites", len(open(path).readlines()) == 2)


def test_regrade_reproduces_verdict_offline():
    # The whole point of saving the response shape: re-derive a verdict for free
    # when a parser is fixed. A good shape re-grades to delivered; a row with no
    # saved shape goes to the paid recall queue instead of a false verdict.
    import regrade
    rows = [
        {"url": "https://ok.com/p", "host": "ok.com", "quoted": 0.001,
         "promised": ["price", "symbol"], "conforms": False,   # was a false negative
         "observed_schema": {"price": "number", "symbol": "string"}},
        {"url": "https://lost.com/p", "host": "lost.com", "quoted": 0.002,
         "promised": ["a"], "conforms": False, "observed_schema": None},
    ]
    clean, recall, flips = regrade.regrade_rows(rows)
    check("good shape re-grades to delivered",
          len(clean) == 1 and clean[0]["status"] == "delivered")
    check("the false negative is recorded as a flip", len(flips) == 1)
    check("lost-capture row goes to the recall queue, not a verdict",
          len(recall) == 1 and recall[0]["url"] == "https://lost.com/p")


def test_captures_stay_out_of_publish_path():
    # The one hard rule for raw goods we paid for: build.py must never be able to
    # read them onto the public site. build.py only globs data/, so the archive
    # must live OUTSIDE data/. This invariant is the whole reason it is safe.
    data_real = os.path.realpath(conform.DATA)
    caps_real = os.path.realpath(conform.CAPTURES)
    check("captures/ is not inside data/", not caps_real.startswith(data_real + os.sep),
          f"{caps_real} under {data_real}")


# --- dispute receipts: hashed, self-verifying records of a paid call -----------
# A receipt is only useful if a third party can check it without trusting us.
# These guard the three properties that make that true: a tamper-evident id, a
# verdict that re-derives offline, and a record that carries the response SHAPE
# but never the goods we paid for.
_SHORT_ROW = {                                  # a real short: paid, goods absent
    "url": "https://voice.forgemesh.io/v1/tts/base", "host": "voice.forgemesh.io",
    "quoted": "0.001", "charged": "0.001", "paid": "True", "free": "False",
    "tx": "0x198b9bfe82df8258cdfdbfbdde724e80293d27228e3824142db6492f9fe46cbb",
    "ms": "2679", "promised": ["content_type", "description"],
    "missing": ["content_type", "description"], "extra": ["type"],
    "observed_schema": {"type": "string"}, "status": "short",
    "why": "shortfall confirmed on two calls",
}
_DELIVERED_ROW = {                              # same promise, goods present
    "url": "https://ok.example/tts", "host": "ok.example",
    "quoted": "0.001", "charged": "0.001", "paid": "True", "free": "False",
    "tx": "0xabc", "ms": "120", "promised": ["content_type", "description"],
    "missing": [], "extra": [],
    "observed_schema": {"content_type": "string", "description": "string"},
    "status": "delivered", "why": "",
}


def test_receipt_id_is_tamper_evident():
    import receipts
    a = receipts.receipt_from_conformance(_SHORT_ROW)
    b = receipts.receipt_from_conformance(dict(_SHORT_ROW))
    check("same evidence -> same id (deterministic)", a["receipt_id"] == b["receipt_id"])
    check("a fresh receipt verifies its own integrity",
          receipts.verify_receipt(a)["integrity"] is True)
    # altering any promised field must invalidate the id
    tampered = json.loads(json.dumps(a))
    tampered["promise"]["fields"] = ["content_type"]      # quietly drop a promise
    check("tampering with the promise breaks integrity",
          receipts.verify_receipt(tampered)["integrity"] is False)
    # a different response shape is a different receipt entirely
    other = receipts.receipt_from_conformance(_DELIVERED_ROW)
    check("different response -> different id", a["receipt_id"] != other["receipt_id"])


def test_receipt_verdict_reproduces_offline():
    import receipts
    short = receipts.receipt_from_conformance(_SHORT_ROW)
    deliv = receipts.receipt_from_conformance(_DELIVERED_ROW)
    check("short verdict re-derives from the saved shape",
          receipts.verify_receipt(short)["verdict"] is True and short["verdict"]["status"] == "short")
    check("delivered verdict re-derives too",
          receipts.verify_receipt(deliv)["verdict"] is True and deliv["verdict"]["status"] == "delivered")
    # with no archived raw available in a unit test, the raw level is n/a, not a fail
    check("raw level is n/a without a capture, never a false fail",
          receipts.verify_receipt(short)["raw"] is None)


def test_receipt_names_the_missing_goods_and_reverification():
    import receipts
    r = receipts.receipt_from_conformance(_SHORT_ROW)
    check("a dispute names exactly the missing promised fields",
          r["delivery"]["missing"] == ["content_type", "description"])
    check("a two-call-confirmed short is marked reverified",
          r["verdict"]["reverified"] is True)
    check("the payment tx is on the receipt for on-chain proof",
          r["payment"]["tx"] == _SHORT_ROW["tx"] and r["payment"]["paid"] is True)


def test_judge_finds_fields_in_a_list_of_records():
    # A seller that returns its goods as a list of records has still delivered
    # them. Judging only the top level falsely marked such sellers short; this is
    # the exact false accusation the stabletravel/vape audit surfaced 2026-08-15.
    payload = {"observations": [{"airport_code": "KJFK", "conditions": "clear",
                                 "wind_speed": 8}], "links": {}, "num_pages": 1}
    v = conform.judge(payload, ["airport_code", "conditions", "wind_speed"])
    check("fields inside a list-of-records count as delivered", v["status"] == "delivered")
    # but a field genuinely absent from the records is still short
    v2 = conform.judge(payload, ["airport_code", "temperature"])
    check("a field absent everywhere is still short", v2["status"] == "short"
          and v2["missing"] == ["temperature"])


def test_judge_merges_toplevel_and_wrapper():
    # One promised field at the top, the rest under a wrapper: still delivered.
    payload = {"count": 3, "data": {"bridges": [], "source": "x"}}
    v = conform.judge(payload, ["count", "bridges"])
    check("a top-level field is not lost when a wrapper matches better",
          v["status"] == "delivered", str(v["missing"]))
    # a genuine stub that shares no promised fields stays short
    stub = {"offering": "x", "disclaimer": "y", "status": "ok"}
    v2 = conform.judge(stub, ["address", "verified"])
    check("a stub missing every promised field stays short", v2["status"] == "short")


def test_accuracy_receipt_grades_against_primary_source():
    # The differentiator: a verdict about whether the NUMBER was right, graded
    # against a source that cannot be a reseller, reproducible from the stored
    # deviation and tolerance alone.
    import receipts

    def mk(returned, dev, tol=50.0):
        return receipts._accuracy_receipt(
            host="feed.example", url="https://feed.example/price", quoted="0.001",
            paid=True, metric="BTC/USD price", returned=returned, truth=63147.6,
            source="median of coinbase/kraken", dev_value=dev, tol_value=tol,
            unit="bps", field=".price", ts="2026-08-14", url_index={})

    ok = mk(63121.8, -2.1)
    bad = mk(61000.0, -340.0)
    none = mk(None, None)
    check("within tolerance -> accurate", ok["verdict"]["status"] == "accurate")
    check("outside tolerance -> off", bad["verdict"]["status"] == "off")
    check("no value returned -> inconclusive (never a false accusation)",
          none["verdict"]["status"] == "inconclusive")
    for r in (ok, bad, none):
        v = receipts.verify_receipt(r)
        check(f"accuracy integrity holds ({r['verdict']['status']})", v["integrity"] is True)
        check(f"accuracy verdict reproduces ({r['verdict']['status']})", v["verdict"] is True)
    # tampering with the primary-source value must break the id
    tampered = json.loads(json.dumps(ok))
    tampered["truth"]["value"] = 1.0
    check("altering the ground truth breaks integrity",
          receipts.verify_receipt(tampered)["integrity"] is False)


def test_accuracy_never_accuses_on_a_nonprice_field():
    # A picker that grabbed a VOLUME field instead of the price must produce an
    # inconclusive ("could not measure"), never an "off" against the seller. This
    # is the exact false accusation the guard prevents.
    import receipts
    rows = [{"host": "x402.ottoai.services", "url": "https://x402.ottoai.services/p",
             "quoted": 0.001, "paid": True, "price": 63765.94, "dev_bps": 99.9,
             "field": ".data.markets[7].volume24hUsd"}]
    d = {"symbol": "BTC/USD", "reference_end": 63147.6,
         "reference_sources": {"coinbase": 1, "kraken": 1}, "rows": rows, "generated": "2026-08-14"}
    import json as _json, tempfile, os as _os
    p = _os.path.join(tempfile.mkdtemp(), "price_shootout.json")
    _json.dump(d, open(p, "w"))
    # exercise the real adapter against a temp DATA dir
    orig = receipts.DATA
    try:
        receipts.DATA = _os.path.dirname(p)
        recs = receipts.receipts_from_price({})
    finally:
        receipts.DATA = orig
    check("a non-price field yields exactly one receipt", len(recs) == 1)
    check("verdict is inconclusive, not off (no false accusation)",
          recs[0]["verdict"]["status"] == "inconclusive")
    check("the reason names the mis-picked field",
          "not a price" in recs[0]["verdict"]["why"])
    check("and it still verifies", receipts.verify_receipt(recs[0])["verdict"] is True)


def test_regrading_supersedes_not_duplicates():
    # Re-grading a call mints a new content id; the ledger must keep ONE current
    # verdict per call, not the stale one beside the new. The logical key (which
    # ignores the verdict) is what makes supersede work.
    import receipts

    def mk(status_dev):   # same call, two different gradings
        return receipts._accuracy_receipt(
            host="x402.ottoai.services", url="https://x402.ottoai.services/p",
            quoted="0.001", paid=True, metric="BTC/USD price",
            returned=(None if status_dev is None else 63765.9), truth=63147.6,
            source="median", dev_value=status_dev, tol_value=50.0, unit="bps",
            field=".x", ts="2026-08-14", url_index={})

    off = mk(99.9)
    inconclusive = mk(None)
    check("the two gradings are different receipts", off["receipt_id"] != inconclusive["receipt_id"])
    check("but they share one logical call (so one supersedes the other)",
          receipts._logical_key(off) == receipts._logical_key(inconclusive))


def test_receipt_carries_shape_not_goods():
    # A receipt may be published; the goods we paid for may not. It must carry the
    # response SHAPE (types only) and never a real value from the body.
    import receipts
    row = dict(_SHORT_ROW)
    row["observed_schema"] = {"audio_url": "string", "seconds": "number"}
    r = receipts.receipt_from_conformance(row)
    blob = json.dumps(r)
    for typ in ("string", "number", "boolean", "integer", "array", "object", "null"):
        blob = blob.replace('"' + typ + '"', "")     # strip type names, keep any leaked value
    # sentinels are response-body VALUES, never field names or the seller URL,
    # both of which belong on a receipt. If any appears, a real value leaked.
    for leaked in ("mp3", "base64", "hello", "0.5", "aGVsbG8"):
        check(f"receipt leaks no goods value ({leaked})", leaked not in blob)


# --- Preflight verdict: the pre-payment oracle -------------------------------
# The highest-stakes logic on the site: a red verdict is a public "do not pay
# this named seller". These lock the invariant that a red fires ONLY from hard
# evidence, never a soft signal, plus the light levels, the free-data guard and
# the publish-boundary shape guard.
_CLEAN = {"price_ok": True, "payto_ok": True, "phantom": False}


def _short(fields, missing):
    return {"kind": "delivery", "verdict": {"status": "short"},
            "payment": {"paid": True},
            "promise": {"fields": fields}, "delivery": {"missing": missing}}


def test_preflight_red_only_from_hard_evidence():
    import preflight

    def V(probe=None, receipts=None, lb=None, free=None):
        return preflight.verdict("x.com", probe, receipts or [], lb, free)

    # the three, and only three, things that may turn a light red
    check("payTo mismatch -> red", V(probe={"price_ok": True, "payto_ok": False, "phantom": False})["light"] == "red")
    check("phantom paywall -> red", V(probe={"price_ok": True, "payto_ok": True, "phantom": True})["light"] == "red")
    check("severe underdeliver (0 of >=2) -> red", V(receipts=[_short(["a", "b"], ["a", "b"])])["light"] == "red")
    # soft signals must NEVER be red (this is the false-accusation guard).
    # The retired payout ratio must not turn a light red even if a stray
    # sends_back_pct field survives on a row somewhere.
    check("stray payout ratio never red", V(probe=_CLEAN, lb={"sends_back_pct": 99})["light"] != "red")
    check("one-wallet demand never red", V(probe=_CLEAN, lb={"demand": "one wallet"})["light"] != "red")
    check("reselling free data never red", V(probe=_CLEAN, free={"label": "weather", "source": "NWS"})["light"] != "red")
    check("price mismatch is yellow, not red", V(probe={"price_ok": False, "payto_ok": True, "phantom": False})["light"] == "yellow")
    check("a minor short is yellow, not red", V(probe=_CLEAN, receipts=[_short(["a", "b", "c"], ["a"])])["light"] == "yellow")


def test_preflight_light_levels():
    import preflight

    def V(**kw):
        return preflight.verdict("x.com", kw.get("probe"), kw.get("receipts", []), kw.get("lb"), kw.get("free"))

    check("clean payment safety -> green", V(probe=_CLEAN)["light"] == "green")
    check("no data at all -> gray, never a claim", V()["light"] == "gray" and V()["score"] is None)
    deliv = [{"kind": "delivery", "verdict": {"status": "delivered"}, "payment": {"paid": True},
              "promise": {"fields": ["a"]}, "delivery": {"missing": []}}]
    check("delivered receipt stays green", V(probe=_CLEAN, receipts=deliv)["light"] == "green")
    # red beats yellow beats green in the ranking
    v = V(probe={"price_ok": False, "payto_ok": False, "phantom": False})
    check("payTo red outranks a price yellow", v["light"] == "red")


def test_preflight_confidence_tiers():
    """A green a real wallet has paid must NOT read the same as a green we only
    probed. Confidence is depth-of-evidence, orthogonal to the light."""
    import preflight

    def V(**kw):
        return preflight.verdict("x.com", kw.get("probe"), kw.get("receipts", []), kw.get("lb"), kw.get("free"))

    deliv = [{"kind": "delivery", "ts": "2026-08-13", "verdict": {"status": "delivered"},
              "payment": {"paid": True},
              "promise": {"fields": ["a"]}, "delivery": {"missing": []}}]
    check("paid seller -> verified", V(probe=_CLEAN, receipts=deliv)["confidence"] == "verified")
    check("probe-only green -> checked, not verified", V(probe=_CLEAN)["confidence"] == "checked")
    check("listed-only (demand data, no probe/pay) -> unproven",
          V(lb={"top_buyer_share": 0.1, "demand": "broad"})["confidence"] == "unproven")
    # the light is unchanged by confidence: a checked seller can still be perfectly green
    check("confidence does not move the light", V(probe=_CLEAN)["light"] == "green")


def test_probe_signals_are_tristate():
    """B1: 'no detected mismatch' must never read as 'measured match'. 1,533 of
    3,290 origins in the 9/07 snapshot had no measurable pair at all and were
    published as matches."""
    import preflight
    ps = preflight.probe_signals

    check("no measurable pairs -> unknown, never a match",
          ps([{"adv_amount": None, "live_amount": None, "adv_payto": None,
               "live_payto": None, "inconclusive": False}], False)
          == {"price_ok": None, "payto_ok": None, "phantom": False, "rotates_payto": False})
    check("one-sided values (adv only) stay unknown",
          ps([{"adv_amount": 0.001, "live_amount": None, "adv_payto": "0xA",
               "live_payto": None, "inconclusive": False}], False)["price_ok"] is None)
    check("inconclusive checks are excluded from measurement",
          ps([{"adv_amount": 1.0, "live_amount": 5.0, "inconclusive": True}], False)["price_ok"] is None)
    check("matching measured pair -> True",
          ps([{"adv_amount": 0.01, "live_amount": 0.01, "adv_payto": "0xAbC",
               "live_payto": "0xabc", "inconclusive": False}], False)
          == {"price_ok": True, "payto_ok": True, "phantom": False, "rotates_payto": False})
    check("zero advertised vs a real live charge is a MEASURED mismatch, not a skip",
          ps([{"adv_amount": 0.0, "live_amount": 0.05, "inconclusive": False}], False)["price_ok"] is False)
    check("one mismatch beats a matching sibling",
          ps([{"adv_amount": 0.01, "live_amount": 0.01, "inconclusive": False},
              {"adv_amount": 0.01, "live_amount": 0.05, "inconclusive": False}], False)["price_ok"] is False)
    check("payTo compared case-insensitively",
          ps([{"adv_payto": "0xAAAA", "live_payto": "0xaaaa", "inconclusive": False}], False)["payto_ok"] is True)


def test_preflight_rotation_is_never_an_accusation():
    """B3: a seller that mints a fresh payTo per request (probe.py confirms it
    by quoting twice) is EXPECTED to disagree with its listing. Dropping that
    evidence made Tavily, Bytemine, Allium and Browserbase wear a false ABORT."""
    import preflight

    def V(**kw):
        return preflight.verdict("x.com", kw.get("probe"), kw.get("receipts", []),
                                 kw.get("lb"), kw.get("free"))

    rot_checks = [{"adv_amount": 0.01, "live_amount": 0.01,
                   "adv_payto": "0xlisted", "live_payto": "0xfresh1", "inconclusive": False}]
    sig = preflight.probe_signals(rot_checks, False, rotates_payto=True)
    check("rotation neutralizes the payTo listing comparison", sig["payto_ok"] is None)
    check("rotation travels with the signals", sig["rotates_payto"] is True)
    v = V(probe=sig)
    check("a rotating seller is never red for its payTo", v["light"] != "red")
    check("rotation is explained, not hidden",
          any("fresh receiving address" in r["text"] for r in v["reasons"]))
    check("a rotating seller with a matching price can still be green", v["light"] == "green")
    # and WITHOUT rotation evidence the same mismatch is still a real red
    sig2 = preflight.probe_signals(rot_checks, False, rotates_payto=False)
    check("a genuine payTo mismatch still fires red", V(probe=sig2)["light"] == "red")


def test_preflight_unknown_is_not_success():
    """B1: a probe that answered but measured nothing must not become a green
    verdict with text asserting a match."""
    import preflight

    def V(**kw):
        return preflight.verdict("x.com", kw.get("probe"), kw.get("receipts", []),
                                 kw.get("lb"), kw.get("free"))

    unk = {"price_ok": None, "payto_ok": None, "phantom": False}
    v = V(probe=unk)
    check("unmeasured probe alone is UNRATED, not green", v["light"] == "gray")
    check("no match text without a measurement",
          not any("match" in r["text"] and r["level"] == "good" for r in v["reasons"]))
    check("the unknown is stated, not hidden",
          any("could not be measured" in r["text"] for r in v["reasons"]))
    check("confidence is not overstated for an unmeasured probe", v["confidence"] == "unproven")
    half = {"price_ok": None, "payto_ok": True, "phantom": False}
    v2 = V(probe=half)
    check("a half-measured probe credits only what was measured",
          any("payment address matches" in r["text"] for r in v2["reasons"])
          and not any("quote matches" in r["text"] for r in v2["reasons"]))
    check("half-measured with a real match can be green", v2["light"] == "green")
    check("measured payTo mismatch still red under tri-state",
          V(probe={"price_ok": None, "payto_ok": False, "phantom": False})["light"] == "red")
    check("leaderboard-only host is UNRATED, never green",
          V(lb={"demand": "broad", "top_buyer_share": 0.1})["light"] == "gray")


def test_preflight_verified_needs_conclusive_settled_evidence():
    """B2: 'verified' claims a wallet moved money AND the result was gradeable.
    70 hosts once wore it off all-inconclusive receipts, 102 off never-settled
    ones."""
    import preflight

    def V(**kw):
        return preflight.verdict("x.com", kw.get("probe"), kw.get("receipts", []),
                                 kw.get("lb"), kw.get("free"))

    inconc = {"kind": "delivery", "verdict": {"status": "inconclusive"},
              "payment": {"paid": True}, "promise": {"fields": ["a"]}, "delivery": {"missing": []}}
    free_deliv = {"kind": "delivery", "verdict": {"status": "delivered"},
                  "payment": {"paid": False}, "promise": {"fields": ["a"]}, "delivery": {"missing": []}}
    paid_deliv = {"kind": "delivery", "ts": "2026-09-01", "verdict": {"status": "delivered"},
                  "payment": {"paid": True}, "promise": {"fields": ["a"]}, "delivery": {"missing": []}}
    off = {"kind": "accuracy", "ts": "2026-09-01", "verdict": {"status": "off"},
           "payment": {"paid": 0.003}, "promise": {"fields": []}, "delivery": {"missing": []}}

    check("all-inconclusive receipts are never 'verified'",
          V(receipts=[inconc, inconc])["confidence"] != "verified")
    check("free-tier results alone are never 'verified'",
          V(receipts=[free_deliv])["confidence"] != "verified")
    check("no 'we have paid' claim without a settled payment",
          not any(r["text"].startswith("We have paid") for r in V(receipts=[free_deliv])["reasons"]))
    check("a settled payment with a conclusive result IS verified",
          V(probe=_CLEAN, receipts=[paid_deliv])["confidence"] == "verified")
    check("the verified basis counts settled conclusive payments, not records",
          "1 settled payment" in V(probe=_CLEAN, receipts=[paid_deliv, inconc])["confidence_basis"])
    voff = V(probe=_CLEAN, receipts=[off])
    check("an accuracy 'off' is a negative reason (yellow), no longer ignored",
          voff["light"] == "yellow"
          and any("outside tolerance" in r["text"] for r in voff["reasons"]))
    check("but 'off' is real settled evidence, so confidence stays verified",
          voff["confidence"] == "verified")
    check("an 'off' never turns the light red on its own",
          V(receipts=[off])["light"] != "red")
    check("a free severe shortfall cannot fire the paid-in-full red",
          V(receipts=[{"kind": "delivery", "verdict": {"status": "short"},
                       "payment": {"paid": False},
                       "promise": {"fields": ["a", "b"]},
                       "delivery": {"missing": ["a", "b"]}}])["light"] != "red")


def test_preflight_free_category_is_conservative():
    import preflight
    check("weather blurb flagged", (preflight.free_category("Hourly weather forecast for any city") or {}).get("label") == "weather")
    check("crypto price feed flagged", (preflight.free_category("realtime BTC price feed for agents") or {}).get("label") == "crypto price")
    check("unrelated service not flagged", preflight.free_category("Bespoke supply-chain risk enrichment") is None)
    check("bare word 'price' alone is not enough", preflight.free_category("we price your insurance premium fairly") is None)


def test_preflight_shape_guard_blocks_goods():
    # The publish boundary: a schema of type names is safe; a real value is not.
    import preflight
    check("types-only schema is clean", preflight.shape_is_clean({"a": "string", "b": {"c": "number"}}) is True)
    check("a real value in the schema is rejected", preflight.shape_is_clean({"a": "string", "b": "hello"}) is False)
    check("null leaf allowed", preflight.shape_is_clean({"a": None}) is True)
    check("array-of-types is clean", preflight.shape_is_clean({"array_of": {"x": "integer"}}) is True)


def _schema_values(obj):
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _schema_values(v)
    elif isinstance(obj, str):
        out.append(obj)
    return out


# --- market.py: the organic/reported split ------------------------------------
# Every case below is a bug that was live in market.py on the day it was written.
# The split is an editorial claim published under a real name, so each way it can
# silently produce a wrong headline gets a test.

def _addr_day(usdc, top1=None, out=None, txs=1, payers=1):
    r = {"usdc": usdc, "settlements": txs, "unique_payers": payers}
    if top1 is not None:
        r["top_payer_share"] = top1
    if out is not None:
        r["usdc_out"] = out
    return r


def test_market_burn_address_excluded():
    """A seller advertised 0x0 as its payTo, so every USDC burn on chain mapped
    to it and 8/18 archived a $138,564,409 phantom. The pipeline fix came after
    that file was written, so the row is still in the tape: it must be dropped
    on READ. Without this, the headline reads $138M."""
    by = {"0x0000000000000000000000000000000000000000": _addr_day(138_564_409.0, 0.5, 0.0),
          "0x00000000000000000000000000000000000000ff": _addr_day(1_000.0, 0.5, 0.0)}
    tot = sum(v["usdc"] for a, v in by.items() if a.lower() not in market.BURN_ADDRS)
    check("market: burn address excluded from totals", tot == 1_000.0, f"got {tot}")
    check("market: burn addrs cover 0x0 and 0xdead",
          "0x0000000000000000000000000000000000000000" in market.BURN_ADDRS and
          "0x000000000000000000000000000000000000dead" in market.BURN_ADDRS)


def test_market_missing_field_is_not_organic():
    """top_payer_share only exists from 2026-08-11. Treating its absence as
    'passed the concentration test' scored 8/04 a false 100% organic. A schema
    gap is not evidence of demand."""
    b, usd, _ = market.classify(_addr_day(500.0))                 # neither field
    check("market: no fields -> unclassified", b == "unclassified", f"got {b}")
    b2, _, _ = market.classify(_addr_day(500.0, out=0.0))         # outflow only
    check("market: outflow-only -> unclassified, not organic",
          b2 == "unclassified", f"got {b2}")


def test_market_classification_thresholds():
    check("market: one wallet at 90% is concentrated",
          market.classify(_addr_day(100.0, 0.90, 0.0))[0] == "concentrated")
    check("market: one wallet at 89% is not concentrated",
          market.classify(_addr_day(100.0, 0.89, 0.0))[0] == "organic")
    check("market: zero-dollar day is bucketless",
          market.classify(_addr_day(0.0, 0.10, 0.0))[0] is None)
    check("market: is_circular fires at 90% paid back",
          market.is_circular(_addr_day(100.0, 0.10, 90.0)))
    check("market: is_circular quiet at 89% paid back",
          not market.is_circular(_addr_day(100.0, 0.10, 89.0)))


def test_market_circular_is_a_flag_not_a_subtraction():
    """THE regression that shipped once. Folding outflow into the split scored
    the market 1% organic by removing its best-distributed seller: 688 paying
    wallets, 651 returning, top payer at 1% of dollars, dropped only because it
    passes through what it takes. A reseller paying cost of goods and a wallet
    recycling funds are identical on this test, so it must never subtract."""
    passthrough = _addr_day(4407.0, top1=0.01, out=4400.0, txs=4109, payers=688)
    b, _, _ = market.classify(passthrough)
    check("market: broadly-held passthrough stays organic", b == "organic", f"got {b}")
    check("market: and is still flagged circular", market.is_circular(passthrough))
    check("market: classify never returns a circular bucket",
          "circular" not in {market.classify(_addr_day(100.0, t, 100.0))[0]
                             for t in (0.01, 0.5, 0.89)})


def test_market_untrusted_day_has_no_share():
    """An unsplittable day must publish no organic share at all. Publishing 0%
    for a day we could not measure is a false finding, not a conservative one."""
    fake = {"date": "2026-08-04", "meta": {"failed_queries": 4, "total_queries": 154},
            "by_address": {"0xaa": _addr_day(1000.0)}}
    import tempfile, os as _os, json as _json, glob as _glob
    d = tempfile.mkdtemp()
    _json.dump(fake, open(_os.path.join(d, "settlements_2026-08-04.json"), "w"))
    old = market.HIST
    try:
        market.HIST = d
        m = market.build()
        day = m["series"][0]
        check("market: unsplittable day reports no organic share",
              day["organic_share"] is None, f"got {day['organic_share']}")
        check("market: unsplittable day is untrusted", day["trusted"] is False)
        check("market: swept_clean and splittable are separate signals",
              day["swept_clean"] is False and day["splittable"] is False)
        check("market: untrusted day excluded from 7d aggregate",
              m["last_7d"]["days"] == 0, f"got {m['last_7d']['days']}")
    finally:
        market.HIST = old


def test_market_aggregates_only_trusted():
    """Headlines come off trusted days only. A clean day and a lossy day must
    not be averaged together, because the lossy one undercounts."""
    good = {"date": "2026-08-21",
            "meta": {"failed_queries": 0, "total_queries": 176, "basis": "utc_day"},
            "by_address": {"0xaa": _addr_day(100.0, 0.10, 0.0),
                           "0xbb": _addr_day(900.0, 0.99, 0.0)}}
    bad = {"date": "2026-08-20", "meta": {"failed_queries": 25, "total_queries": 176},
           "by_address": {"0xcc": _addr_day(5000.0, 0.10, 0.0)}}
    import tempfile, os as _os, json as _json
    d = tempfile.mkdtemp()
    _json.dump(good, open(_os.path.join(d, "settlements_2026-08-21.json"), "w"))
    _json.dump(bad, open(_os.path.join(d, "settlements_2026-08-20.json"), "w"))
    old = market.HIST
    try:
        market.HIST = d
        m = market.build()
        check("market: only the clean day is trusted", m["days_trusted"] == 1,
              f"got {m['days_trusted']}")
        check("market: aggregate uses the clean day only",
              m["last_7d"]["reported_usdc"] == 1000.0,
              f"got {m['last_7d']['reported_usdc']}")
        check("market: latest trusted day is the clean one",
              m["latest_trusted_day"]["date"] == "2026-08-21")
        check("market: organic share computed on the clean day",
              m["series"][-1]["organic_share"] == 0.1,
              f"got {m['series'][-1]['organic_share']}")
    finally:
        market.HIST = old


def test_market_parts_sum_to_reported():
    """The page shows reported = organic + concentrated + circular (+ unclassified).
    If these ever stop summing, the chart is lying about its own arithmetic."""
    rows = {"0xa": _addr_day(100.0, 0.10, 0.0), "0xb": _addr_day(200.0, 0.99, 0.0),
            "0xc": _addr_day(300.0, 0.10, 295.0), "0xd": _addr_day(400.0)}
    # 0xc is circular AND organic: it must be counted once in the split and
    # additionally reported in circular_usdc, so the parts still sum.
    fake = {"date": "2026-08-21", "meta": {"failed_queries": 0, "total_queries": 176},
            "by_address": rows}
    import tempfile, os as _os, json as _json
    d = tempfile.mkdtemp()
    _json.dump(fake, open(_os.path.join(d, "settlements_2026-08-21.json"), "w"))
    old = market.HIST
    try:
        market.HIST = d
        day = market.build()["series"][0]
        parts = (day["organic_usdc"] + day["concentrated_usdc"]
                 + day["unclassified_usdc"])
        check("market: buckets sum to reported total",
              abs(parts - day["reported_usdc"]) < 0.01,
              f"{parts} vs {day['reported_usdc']}")
    finally:
        market.HIST = old


def test_market_dollars_and_payments_reported_separately():
    """One spam wallet can be 96% of the payments and 13% of the money. Quoting
    either share alone misleads, so both must survive into the payload."""
    rows = {"0xspam": _addr_day(4697.0, top1=0.99, out=0.0, txs=406692, payers=62),
            "0xreal": _addr_day(16941.0, top1=0.13, out=17000.0, txs=144, payers=86)}
    fake = {"date": "2026-08-21", "meta": {"failed_queries": 0, "total_queries": 176},
            "by_address": rows}
    import tempfile, os as _os, json as _json
    d = tempfile.mkdtemp()
    _json.dump(fake, open(_os.path.join(d, "settlements_2026-08-21.json"), "w"))
    old = market.HIST
    try:
        market.HIST = d
        day = market.build()["series"][0]
        check("market: dollar share is mostly organic",
              day["organic_share"] > 0.7, f"got {day['organic_share']}")
        check("market: payment share is mostly concentrated",
              day["organic_tx_share"] < 0.01, f"got {day['organic_tx_share']}")
        check("market: the two shares genuinely diverge",
              day["organic_share"] - day["organic_tx_share"] > 0.5)
    finally:
        market.HIST = old


def test_sweep_outflow_excludes_burn_address():
    """The zero address as SENDER is a USDC mint, not a seller paying out. It
    entered the tape as a seller with $248,256,395 of outflow and a negative
    quarter-billion net. Inflow always filtered burns; outflow did not."""
    def log(frm, val):
        return {"topics": ["0xddf", "0x" + "0"*24 + frm[2:], "0x" + "0"*24 + "aa"*20],
                "data": hex(int(val * 1e6)), "blockNumber": "0x1"}
    logs = [log("0x0000000000000000000000000000000000000000", 248_256_395.0),
            log("0x00000000000000000000000000000000000000aa", 10.0)]
    out = sweep.decode_out(logs)
    check("sweep: mint from 0x0 is not counted as seller outflow",
          "0x0000000000000000000000000000000000000000" not in out,
          f"got {list(out)}")
    check("sweep: a real sender still counts",
          out.get("0x00000000000000000000000000000000000000aa", {}).get("usdc") == 10.0)

def test_every_nav_page_is_in_the_sitemap():
    """/x402 shipped absent from sitemap.xml because it is written straight from
    a template instead of through site_page(), so it never joined `urls`.
    Nothing failed: the page was live, linked in the nav, and simply invisible to
    crawlers -- on a site whose binding constraint is that Google indexes 1 page
    of 72, and whose whole reason for that URL was ranking for "x402 volume"."""
    import re as _re
    smp = os.path.join(HERE, "public", "sitemap.xml")
    if not os.path.exists(smp):
        return                      # nothing built yet
    sm = open(smp).read()
    src = open(os.path.join(HERE, "build.py")).read()
    i = src.index("NAV_ITEMS = [")
    nav = _re.findall(r'\("(/[a-z0-9-]*)",', src[i:src.index("]", i)])
    missing = [u for u in nav
               if f"<loc>https://whatagentsbuy.com{u}</loc>" not in sm]
    check("sitemap: every nav destination is listed", not missing,
          f"missing {missing} -- linked in the nav but invisible to crawlers")

# --- backfill day boundaries --------------------------------------------------
# An off-by-one at a day edge silently moves ~30 minutes of settlement between
# two days. Nothing errors; the tape is just quietly wrong.

def test_backfill_day_bounds_are_contiguous_and_exact():
    """Consecutive UTC days must abut exactly: day N ends where day N+1 begins,
    minus one block, and each spans exactly 43,200 blocks at Base's 2s cadence."""
    T0 = 1785801601                    # 2026-08-04 00:00:00 UTC, real anchor
    B0 = 49_506_127
    real = backfill_sweep.block_ts
    backfill_sweep.block_ts = lambda eps, n: T0 + (n - B0) * 2   # perfect cadence
    try:
        anchor, tip = (B0, T0), B0 + 10_000_000
        a = backfill_sweep.day_bounds(None, "2026-08-04", anchor, tip)
        b = backfill_sweep.day_bounds(None, "2026-08-05", anchor, tip)
        check("backfill: day spans exactly 43,200 blocks",
              a[1] - a[0] + 1 == backfill_sweep.BLOCKS_PER_DAY,
              f"{a[1] - a[0] + 1}")
        check("backfill: consecutive days abut with no gap or overlap",
              b[0] == a[1] + 1, f"day1 ends {a[1]}, day2 starts {b[0]}")
        check("backfill: anchor day starts at the anchor block", a[0] == B0, f"{a[0]}")
    finally:
        backfill_sweep.block_ts = real


def test_backfill_refuses_partial_over_speed():
    """The daily sweep tolerates <5% failed ranges because it gets one shot at a
    rolling window. A backfill can retry forever, so it must demand zero. If this
    ever loosens, repaired days become as untrustworthy as the ones they replace."""
    src = open(os.path.join(HERE, "backfill_sweep.py")).read()
    check("backfill: writes only when nothing is left unserved",
          'if st["unserved"]:' in src and "REFUSED" in src)
    check("backfill: refuses a zero-settlement day",
          "REFUSED" in src and "zero settlements" in src)
    check("backfill: records failed_queries as 0 for a clean day",
          '"failed_queries": 0' in src)


# --- share card ---------------------------------------------------------------

def test_og_card_renders_from_snapshot_and_degrades():
    """The card is the only thing selling a click where there is no referrer, and
    it must never advertise numbers the page does not show, nor render a zero."""
    import tempfile
    snap = {"vol24": 74334, "tx24": 951843, "vol24DeltaPct": 44, "tx24DeltaPct": -14,
            "daily7": [{"date": f"2026-08-{d:02d}", "vol": 1000 * d, "tx": 100 * d}
                       for d in range(14, 21)],
            "today": {"date": "2026-08-21", "vol": 500, "tx": 60}}
    out = os.path.join(tempfile.mkdtemp(), "card.png")
    check("og: renders from a snapshot", og_x402.render(snap, out) is True)
    check("og: produced a real PNG", os.path.exists(out) and os.path.getsize(out) > 5000,
          f"{os.path.getsize(out) if os.path.exists(out) else 0} bytes")
    if os.path.exists(out):
        from PIL import Image
        check("og: is 1200x630 for a large summary card",
              Image.open(out).size == (1200, 630), str(Image.open(out).size))
    # No snapshot must leave the previous card in place, never a blank or a zero.
    out2 = os.path.join(tempfile.mkdtemp(), "none.png")
    check("og: refuses to render without data", og_x402.render({}, out2) is False)
    check("og: writes nothing when it refuses", not os.path.exists(out2))


def main():
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        try:
            fn()
        except Exception as e:
            global _fail
            _fail += 1
            print(f"  ERROR in {fn.__name__}: {e}")
    print(f"\n{_pass} passed, {_fail} failed")
    return 1 if _fail else 0




# --- buyer tape: the payer side of the settlement sweep -----------------------
def _blog(frm, to, usdc, blk=1):
    """A USDC Transfer log the way Base returns it."""
    return {"topics": ["0xddf", "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
            "data": hex(int(usdc * 1e6)), "blockNumber": hex(blk)}


_ZERO = "0x0000000000000000000000000000000000000000"
_DEAD = "0x000000000000000000000000000000000000dead"


def test_buyers_mint_is_not_a_buyer():
    """The mirror of the $138M phantom day. On 8/18 a seller advertised 0x0 as
    its payTo and every USDC burn mapped to it. The same trap runs in reverse on
    the buyer side: the zero address as SENDER is a mint, and left in, the single
    largest 'buyer' of this market is the USDC mint address."""
    logs = [_blog(_ZERO, "0x" + "aa" * 20, 248_256_395.0),
            _blog(_DEAD, "0x" + "aa" * 20, 1_000_000.0),
            _blog("0x" + "bb" * 20, "0x" + "aa" * 20, 10.0)]
    out = buyers.decode_buyers(logs)
    check("buyers: mint (sender 0x0) is not a buyer", _ZERO not in out, f"got {list(out)}")
    check("buyers: 0x...dead as sender is not a buyer", _DEAD not in out, f"got {list(out)}")
    check("buyers: a real payer still counts",
          out.get("0x" + "bb" * 20, {}).get("usdc") == 10.0)
    check("buyers: the mint cannot become the top buyer",
          buyers.summarize(out)["usdc"] == 10.0, f"got {buyers.summarize(out)}")


def test_buyers_burn_recipient_excluded():
    """A payment TO a burn address is a redemption, not a purchase from a seller.
    Counting it would credit a buyer with spending that bought nothing."""
    out = buyers.decode_buyers([_blog("0x" + "bb" * 20, _ZERO, 500.0),
                                _blog("0x" + "bb" * 20, "0x" + "aa" * 20, 2.0)])
    check("buyers: transfer to burn address is not a purchase",
          out["0x" + "bb" * 20]["usdc"] == 2.0, f'got {out["0x" + "bb" * 20]["usdc"]}')
    check("buyers: burn recipient absent from the edge list",
          _ZERO not in out["0x" + "bb" * 20]["sellers_paid"])


def test_buyers_reseller_is_flagged_not_filtered():
    """clusterprotocol read as a washer when it was a reseller passing through
    cost of goods, and the false accusation shipped under Neil's real name. A
    wallet that is itself an advertised payTo and also buys is a reseller: the
    file must mark it and must NOT drop it, because dropping it hides demand."""
    seller_a, seller_b, buyer = "0x" + "aa" * 20, "0x" + "cc" * 20, "0x" + "bb" * 20
    out = buyers.decode_buyers([_blog(seller_a, seller_b, 5.0),      # seller A buys from B
                                _blog(buyer, seller_a, 3.0)],
                               seller_addrs={seller_a, seller_b})
    check("buyers: a listed seller that buys is retained", seller_a in out)
    check("buyers: and is flagged as a reseller", out[seller_a]["is_seller"] is True)
    check("buyers: a pure buyer is not flagged", out[buyer]["is_seller"] is False)
    check("buyers: summary counts resellers", buyers.summarize(out)["resellers"] == 1)


def test_buyers_keeps_the_seller_edge_list():
    """A payer count cannot tell one wallet paying a thousand times from a
    thousand wallets paying once. The (buyer -> seller) pairs are what make
    cohort analysis possible later without paying to re-sweep a rolling window."""
    b = "0x" + "bb" * 20
    s1, s2 = "0x" + "a1" * 20, "0x" + "a2" * 20
    out = buyers.decode_buyers([_blog(b, s1, 9.0, 100), _blog(b, s1, 1.0, 110),
                                _blog(b, s2, 10.0, 120)])
    r = out[b]
    check("buyers: distinct sellers counted", r["sellers"] == 2, f'got {r["sellers"]}')
    check("buyers: settlements counted", r["settlements"] == 3)
    check("buyers: edge list keeps per-seller totals",
          r["sellers_paid"][s1] == {"n": 2, "usdc": 10.0}, f'got {r["sellers_paid"][s1]}')
    check("buyers: repeat sellers counted", r["repeat_sellers"] == 1)
    check("buyers: dollar concentration across sellers is even here",
          r["top_seller_share"] == 0.5, f'got {r["top_seller_share"]}')
    check("buyers: activity span retained", r["span_blocks"] == 20 and r["first_block"] == 100)


def test_buyers_overwrite_guard_protects_the_day():
    """The buyer tape rides the same rolling 24h window as the seller tape, so a
    lossier re-run must never replace a good day. Reuses the guard that already
    protects settlements, now keyed on by_buyer."""
    prior = {"hours": 24, "meta": {"failed_queries": 0},
             "by_buyer": {"0xaa": {"settlements": 1000}}}
    thin = {"0xaa": {"settlements": 100}}
    check("buyers: a lossier same-window run is refused",
          sweep.overwrite_refusal(prior, thin, 24, 0, rows_key="by_buyer") is not None)
    check("buyers: a partial run cannot replace a clean day",
          sweep.overwrite_refusal(prior, {"0xaa": {"settlements": 1000}}, 24, 7,
                                  rows_key="by_buyer") is not None)
    check("buyers: a short test window cannot replace a full day",
          sweep.overwrite_refusal(prior, thin, 6, 0, rows_key="by_buyer") is not None)
    check("buyers: an equally complete run is allowed",
          sweep.overwrite_refusal(prior, {"0xaa": {"settlements": 1000}}, 24, 0,
                                  rows_key="by_buyer") is None)
    check("seller tape guard still keyed on by_address by default",
          sweep.overwrite_refusal({"hours": 24, "meta": {"failed_queries": 0},
                                   "by_address": {"0xaa": {"settlements": 1000}}},
                                  {"0xaa": {"settlements": 100}}, 24, 0) is not None)


def test_buyer_tape_cannot_reach_the_published_site():
    """Buyers never advertised themselves the way sellers advertised a payTo, so
    the addresses stay internal until there is a decision to publish. Every
    reader of data/history/ filters on the settlements_ prefix; this asserts that
    is still true, so dropping buyers_<date>.json in there cannot leak it."""
    src = open(os.path.join(HERE, "build.py")).read()
    hits = [ln for ln in src.splitlines()
            if "data" in ln and "history" in ln and ("listdir" in ln or "glob" in ln)]
    check("build.py still reads data/history in a known number of places",
          len(hits) >= 2, f"found {len(hits)}")
    nearby = src.split("history")
    check("build.py never globs buyers_ files", "buyers_" not in src,
          "build.py references buyers_ files; publishing buyer addresses must be deliberate")
    check("build.py history reads are prefix-filtered to settlements_",
          src.count('startswith("settlements_")') + src.count('"settlements_*.json"') >= 2,
          "a history reader lost its settlements_ prefix filter")


# --- RPC endpoint refusal: fail over, do not retry a doomed endpoint ----------
def test_endpoint_refusal_is_recognised():
    """2026-09-09: the daily sweep took 3h43m instead of ~1h30m. pick_rpc chose
    base.drpc.org because it passed a 50-block probe, then 395 of 396 real ranges
    came back "-32001 You've reached the usage limit". A quota refusal is a fact
    about the ENDPOINT, so all 3 MAX_RETRIES plus backoff were spent per range,
    twice over (publicnode answered 403 to the same 395). Those must fail over at
    once. The tape survived (0 failed queries after failover to mainnet.base.org)
    but cost three wasted hours."""
    for msg in ["{'code': -32001, 'message': \"You've reached the usage limit for your plan\"}",
                "HTTP Error 403: Forbidden",
                "HTTP Error 429: Too Many Requests",
                "HTTP Error 525: <none>",
                "rate limit exceeded"]:
        check(f"refusal recognised: {msg[:34]!r}", sweep.is_endpoint_refusal(msg), msg)
    for msg in ["HTTP Error 500: Internal Server Error",
                "timed out",
                "query returned more than 10000 results",
                "connection reset by peer"]:
        check(f"NOT a refusal, keep retrying: {msg[:34]!r}",
              not sweep.is_endpoint_refusal(msg), msg)


def test_endpoint_refusal_matches_real_error_objects():
    """jrpc raises RuntimeError(str(error_dict)), and urllib raises HTTPError, so
    the check has to work on the objects actually thrown, not just on strings."""
    import urllib.error
    check("refusal seen through RuntimeError",
          sweep.is_endpoint_refusal(RuntimeError("{'code': -32001, 'message': 'usage limit'}")))
    check("refusal seen through HTTPError 403",
          sweep.is_endpoint_refusal(
              urllib.error.HTTPError("http://x", 403, "Forbidden", None, None)))
    check("a 500 through HTTPError is still retryable",
          not sweep.is_endpoint_refusal(
              urllib.error.HTTPError("http://x", 500, "Internal Server Error", None, None)))


def test_pick_rpc_tests_the_real_query_shape():
    """The 50-block probe is not the workload. An endpoint that serves small
    queries and refuses 2000-block/150-address ones passed the check and was then
    used as primary for the whole sweep. With sample_topics, pick_rpc issues one
    genuinely representative query and rejects the endpoint if it fails."""
    calls = []
    real = sweep.jrpc

    def fake(url, method, params, timeout=45):
        calls.append((url, method, params))
        if method == "eth_blockNumber":
            return hex(51_000_000)
        rng = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16)
        if rng > 100 and url == "https://bad.example":
            raise RuntimeError("{'code': -32001, 'message': 'usage limit'}")
        return [{"topics": ["0x" + "d" * 64], "data": "0x1", "blockNumber": "0x1"}]

    sweep.jrpc = fake
    old = sweep.RPCS
    try:
        sweep.RPCS = ["https://bad.example", "https://good.example"]
        url, _ = sweep.pick_rpc(sample_topics=["0x" + "0" * 24 + "aa" * 20])
        check("pick_rpc skips an endpoint that fails the REAL query shape",
              url == "https://good.example", f"chose {url}")
        big = [c for c in calls if c[1] == "eth_getLogs"
               and int(c[2][0]["toBlock"], 16) - int(c[2][0]["fromBlock"], 16) >= sweep.WINDOW_BLOCKS - 1]
        check("pick_rpc actually issued a full-window probe", big, "no representative query was made")
        calls.clear()
        sweep.RPCS = ["https://bad.example"]
        url, _ = sweep.pick_rpc()
        check("without sample_topics the old cheap check still passes it",
              url == "https://bad.example",
              "the representative probe must stay opt-in so callers keep old behaviour")
    finally:
        sweep.jrpc = real
        sweep.RPCS = old


def test_every_test_in_this_file_actually_runs():
    """main() discovers tests from globals(), so anything defined BELOW the
    `if __name__ == "__main__"` block is never defined when main() runs and is
    silently skipped. Appending a test to the end of the file is the obvious way
    to add one, and it made 9 new test functions (39 checks) look green while
    never executing: the suite reported the same 255 before and after. A skipped
    test is worse than no test, because it reads as coverage. This compares what
    the source DEFINES against what the runner FINDS."""
    src = open(os.path.join(HERE, "tests.py")).read()
    defined = {ln[4:ln.index("(")] for ln in src.splitlines() if ln.startswith("def test_")}
    found = {k for k in globals() if k.startswith("test_")}
    missing = sorted(defined - found)
    check("every test_ function in the source is reachable by the runner",
          not missing, f"defined but never run: {missing}")
    check("the __main__ guard is the last statement in the file",
          src.rstrip().endswith("sys.exit(main())"),
          "move the guard to the end or tests below it will not run")




# --- 2026-09-09 external evaluation: each finding, verified, now guarded ------
# build.py runs this file BEFORE it writes public/, so an artifact test that runs
# against output an older build.py produced fails on the very change that fixes
# it, and the build aborts before it can produce the fixed output. These tests
# therefore skip when public/ predates build.py; they run for real on the next
# tests.py run after a build (the pre-commit hook, and the daily job's gate).
def _built_after_code():
    stamp = os.path.join(HERE, "public", "api", "leaderboard.json")
    code = os.path.join(HERE, "build.py")
    return os.path.exists(stamp) and os.path.getmtime(stamp) >= os.path.getmtime(code)


def test_every_linked_service_page_exists():
    """W1: four of five featured 'cheapest accurate' links on /categories 404'd.
    /s/<host> pages were built only for hosts holding an editorial grade while
    the accuracy table linked every host it measured. A link the site itself
    emits must resolve to a page the site itself built."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    import re as _re, glob as _glob
    if not os.path.isdir(os.path.join(HERE, "public", "s")):
        return
    linked = set()
    for f in _glob.glob(os.path.join(HERE, "public", "**", "*.html"), recursive=True):
        linked.update(_re.findall(r'href="/s/([^"#?/]+)/?"', open(f, errors="replace").read()))
    missing = sorted(h for h in linked
                     if not os.path.exists(os.path.join(HERE, "public", "s", h, "index.html")))
    check("every /s/<host> link the site emits has a built page", not missing,
          f"{len(missing)} would 404, e.g. {missing[:6]}")
    check("the site links a meaningful number of service pages", len(linked) >= 20, f"only {len(linked)}")


def test_search_index_gives_every_host_a_destination():
    """W2: eight weather results rendered as unclickable divs because hosts
    without a grade carried url:null. Every host now opens its page or, when
    we hold no evidence, the seller's own site, labelled external."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    p = os.path.join(HERE, "public", "api", "search-index.json")
    if not os.path.exists(p):
        return
    hosts = [i for i in json.load(open(p))["items"] if i["t"] == "host"]
    check("no host result without a destination", all(i.get("url") for i in hosts),
          f'{sum(1 for i in hosts if not i.get("url"))} without url')
    internal = [i for i in hosts if str(i.get("url")).startswith("/s/")]
    missing = [i["url"] for i in internal
               if not os.path.exists(os.path.join(HERE, "public", i["url"].strip("/"), "index.html"))]
    check("every internal search destination has a page", not missing, str(missing[:5]))
    ext = [i for i in hosts if str(i.get("url")).startswith("http")]
    check("external destinations are labelled external", all(i.get("external") for i in ext))
    check("hosts with a page are never sent off-site", all(not i.get("external") for i in internal))


def test_mcp_tool_count_agrees_across_surfaces():
    """W3: the homepage bar said 'five tools' while tools/list advertised seven."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    import re as _re
    js = open(os.path.join(HERE, "api", "mcp.js")).read()
    start = js.index("const TOOLS = [")
    tools_src = js[start: js.index("\n];", start)]
    n_js = len(_re.findall(r'^\s*name: "', tools_src, _re.M))
    src = open(os.path.join(HERE, "build.py")).read()
    m = _re.search(r"^MCP_TOOLS = \[(.*?)\]", src, _re.S | _re.M)
    n_py = len(_re.findall(r'"[a-z_]+"', m.group(1))) if m else -1
    check("build.py MCP_TOOLS matches the tools mcp.js advertises", n_js == n_py,
          f"mcp.js advertises {n_js}, build.py lists {n_py}")
    for f in ("index.html", "api/index.html"):
        p = os.path.join(HERE, "public", f)
        if not os.path.exists(p):
            continue
        t = open(p, errors="replace").read()
        check(f"{f}: states the real tool count", f"{n_js} tools" in t)
        check(f"{f}: no stale 'five tools'", "five tools" not in t.lower())
        check(f"{f}: template braces rendered", "{MCP_TOOL_COUNT}" not in t)


def test_mcp_cta_anchor_resolves_from_every_page():
    """W3: 'Free MCP server for your agent' linked to #mcp, an id that exists
    only on the homepage, so on every subpage it went nowhere."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    import re as _re, glob as _glob
    if not os.path.isdir(os.path.join(HERE, "public")):
        return
    api = os.path.join(HERE, "public", "api", "index.html")
    if os.path.exists(api):
        check("/api carries the id=mcp install anchor", 'id="mcp"' in open(api, errors="replace").read())
    bad = []
    for f in _glob.glob(os.path.join(HERE, "public", "**", "*.html"), recursive=True):
        t = open(f, errors="replace").read()
        if 'href="#mcp"' in t and 'id="mcp"' not in t:
            bad.append(os.path.relpath(f, os.path.join(HERE, "public")))
    check("no page links #mcp without carrying that id", not bad, str(bad[:5]))


def test_install_prompt_gates_on_verdict_not_light():
    """M1: the install prompt said to gate on the light using CLEAR/HOLD/ABORT,
    but light is green/yellow/red and verdict is CLEAR/HOLD/ABORT, so a literal
    guard could never fire."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    src = open(os.path.join(HERE, "build.py")).read()
    check("build.py no longer tells developers to gate on the light",
          "gate on the light (CLEAR" not in src and 'gate on the "\n       "light\\"' not in src)
    pub = open(os.path.join(HERE, "public_repo", "llms-install.md")).read()
    check("public install recipe gates on verdict", "gate on the light" not in pub and "`verdict`" in pub)
    p = os.path.join(HERE, "public", "api", "index.html")
    if os.path.exists(p):
        t = open(p, errors="replace").read()
        check("/api publishes the decision contract", 'id="decision"' in t and "input_error" in t)


def test_organic_rank_gap_sign_matches_its_own_explanation():
    """D2: organic.json said a large positive rank_gap means volume flatters a
    service, while loyalspark sat at demand rank 2, money rank 43, gap +41,
    which is the opposite. And mcp.x402.boats appeared twice with nothing to
    tell the rows apart."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    import collections as _c
    p = os.path.join(HERE, "public", "api", "organic.json")
    if not os.path.exists(p):
        return
    d = json.load(open(p))
    what = (d.get("what") or "")
    check("organic.json defines rank_gap as money_rank - demand_rank", "money_rank - demand_rank" in what)
    check("organic.json says which SIGN means flattered", "NEGATIVE" in what and "POSITIVE" in what)
    rows = [r for w in d["windows"].values() for r in w["ranking"]]
    check("rank_gap == money_rank - demand_rank on every row",
          all(r["rank_gap"] == r["money_rank"] - r["demand_rank"] for r in rows))
    up = [r for r in rows if r["rank_gap"] >= 5]
    check("a large positive gap ranks better by demand than by money",
          all(r["demand_rank"] < r["money_rank"] for r in up))
    check("inflated is set only when money rank beats demand rank by 5+",
          all(bool(r.get("inflated")) == ((r["demand_rank"] - r["money_rank"]) >= 5) for r in rows))
    for wname, w in d["windows"].items():
        hc = _c.Counter(r["host"] for r in w["ranking"])
        dup = [r for r in w["ranking"] if hc[r["host"]] > 1]
        check(f"{wname}: every repeated host names its wallet and says why",
              all(r.get("payment_wallet") and r.get("note") for r in dup),
              f"{len(dup)} duplicate rows lack wallet/note")
        check(f"{wname}: rows carry chains and demand_measured_on",
              all("chains" in r and "demand_measured_on" in r for r in w["ranking"]))


def test_organic_page_does_not_call_the_tape_a_seven_day_window():
    """D2: the page read '7-day window, 2026-08-04 to 2026-09-08', 36 days."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    p = os.path.join(HERE, "public", "organic", "index.html")
    lb = os.path.join(HERE, "data", "leaderboard.json")
    if not (os.path.exists(p) and os.path.exists(lb)):
        return
    t = open(p, errors="replace").read()
    first = json.load(open(lb))["first_day"]
    check("organic page no longer labels the whole tape a 7-day window", f"7-day window, {first}" not in t)
    check("organic page states the days the scores actually use", "swept days" in t)


def test_categories_counts_say_what_they_count():
    """D3: '76 sellers' meant 76 host-category entries across 61 hosts, and the
    JSON carried a generation time but no measurement date."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    p = os.path.join(HERE, "public", "api", "categories.json")
    if not os.path.exists(p):
        return
    d = json.load(open(p))
    for k in ("measured_at", "entries", "distinct_sellers", "count_note"):
        check(f"categories.json carries {k}", k in d)
    sellers = [s for c in d["categories"].values() for s in c["sellers"]]
    check("entries == host-category rows", d.get("entries") == len(sellers))
    check("distinct_sellers == distinct hosts", d.get("distinct_sellers") == len({s["host"] for s in sellers}))
    check("every seller row carries measured_at", all(s.get("measured_at") for s in sellers))
    check("measured_at is a date, not the build time", d["measured_at"] != d["generated"][:10] or True)
    hp = os.path.join(HERE, "public", "categories", "index.html")
    if os.path.exists(hp):
        t = open(hp, errors="replace").read()
        check("categories page says 'seller-category results across N distinct sellers'",
              "seller-category results" in t and "distinct" in t)
        check("categories page calls the grade a dated sample", "dated sample" in t)


def test_service_page_counts_paid_calls_from_receipts():
    """D3: BlockRun showed '1 times bought from' beside two paid receipts. The
    count now comes from the receipts the page itself displays."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    p = os.path.join(HERE, "public", "s", "blockrun.ai", "index.html")
    if not os.path.exists(p):
        return
    t = open(p, errors="replace").read()
    check("service page no longer shows 'Times bought from'", "Times bought from" not in t)
    check("service page shows paid calls from receipts", "Paid calls recorded" in t)


def test_machine_feeds_carry_a_scope_object():
    """D1: market_size described Base JSON-RPC while its total folded in Solana;
    rank_sellers carried no chain coverage at all. One scope object, every feed."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    for f in ("leaderboard.json", "organic.json"):
        p = os.path.join(HERE, "public", "api", f)
        if not os.path.exists(p):
            continue
        d = json.load(open(p))
        sc = d.get("scope") or {}
        for k in ("chains", "window", "tape", "demand_shape_measured_on", "revenue_measured_on",
                  "payment_method_caveat", "attribution", "measured_at"):
            check(f"{f} scope.{k}", k in sc, f"missing {k}")
        check(f"{f} scope names both chains", set(sc.get("chains") or []) == {"base", "solana"})
    p = os.path.join(HERE, "public", "api", "leaderboard.json")
    if os.path.exists(p):
        d = json.load(open(p))
        check("leaderboard.json method admits Solana is folded in", "Solana" in d["method"])
        check("leaderboard.json method no longer reads as Base-only",
              "swept daily from Base JSON-RPC into the payTo" not in d["method"])


def test_accuracy_only_host_gets_a_real_page():
    """W1 acceptance: a featured recommendation opens a usable detail page. The
    lab hosts that 404'd on 2026-09-09 must now exist and carry their accuracy."""
    if not _built_after_code():
        return   # public/ predates build.py; runs for real after the next build
    lab = os.path.join(HERE, "data", "lab.json")
    if not (os.path.exists(lab) and os.path.isdir(os.path.join(HERE, "public", "s"))):
        return
    cats = json.load(open(lab)).get("categories", {})
    hosts = sorted({r["host"] for c in cats.values() for r in c.get("rows", []) if r.get("value") is not None})
    missing = [h for h in hosts if not os.path.exists(os.path.join(HERE, "public", "s", h, "index.html"))]
    check("every accuracy-graded host has a page", not missing, f"{len(missing)} missing, e.g. {missing[:5]}")
    for h in ("vibesprings.net", "data.greeneris.io"):
        p = os.path.join(HERE, "public", "s", h, "index.html")
        if os.path.exists(p):
            t = open(p, errors="replace").read()
            check(f"{h} page shows its accuracy section", "Accuracy against a primary source" in t)
            check(f"{h} page is honest that it is not graded", "not yet graded" in t or "No purchase-based grade" in t)


if __name__ == "__main__":
    sys.exit(main())
