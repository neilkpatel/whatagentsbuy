#!/usr/bin/env python3
"""The buy list: what has been tested, what is next, what is worth trying.

  python3 buylist.py                      # show everything, grouped
  python3 buylist.py next                 # just the shortlist
  python3 buylist.py add --service x.dev --url https://x.dev/api --why "..." [--price "$0.01"]
  python3 buylist.py done <id> --payment 2 --result "Overcharged. Quoted $25, charged $25, nothing arrived."
  python3 buylist.py promote <id>         # idea -> next
  python3 buylist.py drop <id>            # remove
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
Q = os.path.join(HERE, "data", "queue.json")
ORDER = {"next": 0, "idea": 1, "done": 2}
LABEL = {"next": "UP NEXT", "idea": "IDEAS", "done": "DONE"}
C = {"next": "\033[1;33m", "idea": "\033[0;36m", "done": "\033[0;32m", "off": "\033[0m",
     "dim": "\033[2m", "bold": "\033[1m"}


def load():
    return json.load(open(Q)) if os.path.exists(Q) else []


def save(items):
    json.dump(items, open(Q, "w"), indent=1)


def wrap(text, width=86, indent=" " * 6):
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    out.append(line)
    return f"\n{indent}".join(out)


def show(items, only=None):
    if not items:
        print("nothing queued yet")
        return
    groups = {}
    for it in items:
        groups.setdefault(it.get("status", "idea"), []).append(it)
    for status in sorted(groups, key=lambda s: ORDER.get(s, 9)):
        if only and status != only:
            continue
        rows = groups[status]
        print(f"\n{C[status]}{C['bold']}{LABEL.get(status, status.upper())}{C['off']} ({len(rows)})")
        for it in rows:
            tag = f"#{it['payment']:02d}" if it.get("payment") else "   "
            price = f" {C['dim']}{it.get('price', '')}{C['off']}" if it.get("price") else ""
            print(f"  {C['dim']}{tag}{C['off']} {C['bold']}{it['service']}{C['off']}"
                  f"{price}  {C['dim']}[{it['id']}]{C['off']}")
            if it.get("result"):
                print(f"      {C['done']}{it['result']}{C['off']}")
            elif it.get("why"):
                print(f"      {wrap(it['why'])}")
            if it.get("risk"):
                print(f"      {C['next']}risk: {wrap(it['risk'], indent=' ' * 12)}{C['off']}")
    done = len(groups.get("done", []))
    print(f"\n{C['dim']}{done} tested, {len(groups.get('next', []))} queued, "
          f"{len(groups.get('idea', []))} ideas{C['off']}\n")


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("cmd", nargs="?", default="list")
    ap.add_argument("target", nargs="?")
    ap.add_argument("--service"); ap.add_argument("--url"); ap.add_argument("--why")
    ap.add_argument("--price"); ap.add_argument("--risk"); ap.add_argument("--id")
    ap.add_argument("--payment", type=int); ap.add_argument("--result")
    a = ap.parse_args()
    items = load()

    if a.cmd in ("list", "all"):
        show(items)
    elif a.cmd == "next":
        show(items, only="next")
    elif a.cmd == "add":
        if not (a.service and a.why):
            sys.exit("need --service and --why")
        new = {"id": a.id or a.service.split(".")[0].replace("/", "-"),
               "service": a.service, "url": a.url, "price": a.price,
               "why": a.why, "status": "next" if a.risk is None else "next"}
        if a.risk:
            new["risk"] = a.risk
        items.append(new)
        save(items)
        print(f"queued {new['id']}")
    elif a.cmd in ("done", "promote", "drop"):
        hit = next((i for i in items if i["id"] == a.target), None)
        if not hit:
            sys.exit(f"no such id: {a.target}")
        if a.cmd == "done":
            hit["status"] = "done"
            if a.payment:
                hit["payment"] = a.payment
            if a.result:
                hit["result"] = a.result
            hit.pop("risk", None)
        elif a.cmd == "promote":
            hit["status"] = "next"
        else:
            items = [i for i in items if i["id"] != a.target]
        save(items)
        print(f"{a.cmd}: {a.target}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
