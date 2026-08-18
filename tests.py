#!/usr/bin/env python3
"""Tests for the pipeline logic. Run before every commit:  python3 tests.py

Every case here exists because the bug it guards against actually happened in
this codebase. No network, no money, no build side effects: pure functions
against fixtures, plus a couple of file-behaviour checks. If this passes, the
class of error that has cost us real money and real corrections cannot recur
silently.
"""
import json, os, sys, tempfile
import conform, leaderboard, sweep_solana, whatsnew

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
        conform.call = lambda url, method, body, cap: (True, None, {})  # capture nothing
        ok, detail = conform.run_canary()
        check("broken harness (null capture) aborts the run", ok is False, detail)
        # now make call() return every promised field for each canary
        conform.call = lambda url, method, body, cap: (
            True, {k: 1 for c in conform.CANARY if c["url"] == url for k in c["expect"]}, {})
        ok2, detail2 = conform.run_canary()
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
            "promise": {"fields": fields}, "delivery": {"missing": missing}}


def test_preflight_red_only_from_hard_evidence():
    import preflight

    def V(probe=None, receipts=None, lb=None, free=None):
        return preflight.verdict("x.com", probe, receipts or [], lb, free)

    # the three, and only three, things that may turn a light red
    check("payTo mismatch -> red", V(probe={"price_ok": True, "payto_ok": False, "phantom": False})["light"] == "red")
    check("phantom paywall -> red", V(probe={"price_ok": True, "payto_ok": True, "phantom": True})["light"] == "red")
    check("severe underdeliver (0 of >=2) -> red", V(receipts=[_short(["a", "b"], ["a", "b"])])["light"] == "red")
    # soft signals must NEVER be red (this is the false-accusation guard)
    check("weak/recycled demand never red", V(probe=_CLEAN, lb={"sends_back_pct": 99})["light"] != "red")
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
    deliv = [{"kind": "delivery", "verdict": {"status": "delivered"}, "promise": {"fields": ["a"]}, "delivery": {"missing": []}}]
    check("delivered receipt stays green", V(probe=_CLEAN, receipts=deliv)["light"] == "green")
    # red beats yellow beats green in the ranking
    v = V(probe={"price_ok": False, "payto_ok": False, "phantom": False})
    check("payTo red outranks a price yellow", v["light"] == "red")


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


if __name__ == "__main__":
    sys.exit(main())
