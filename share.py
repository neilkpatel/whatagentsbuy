#!/usr/bin/env python3
"""Make a tagged link, so a share can be attributed later.

88% of visitors arrive with no referrer. That is not a gap in the analytics:
messaging apps, email clients and direct visits genuinely send nothing. The only
thing that survives is what you put in the URL yourself.

  python3 share.py                       # the common ones, ready to copy
  python3 share.py /checklist twitter
  python3 share.py /p/nansen-audits-us danny
"""
import sys, urllib.parse

SITE = "https://whatagentsbuy.com"

# medium is what Vercel groups on; source is who specifically.
CHANNELS = {
    "twitter":  ("social", "twitter"),
    "x":        ("social", "twitter"),
    "linkedin": ("social", "linkedin"),
    "reddit":   ("social", "reddit"),
    "hn":       ("social", "hackernews"),
    "discord":  ("chat", "discord"),
    "slack":    ("chat", "slack"),
    "telegram": ("chat", "telegram"),
    "imessage": ("chat", "imessage"),
    "email":    ("email", "email"),
    "dm":       ("chat", "dm"),
}


def tag(path, channel, campaign=None):
    medium, source = CHANNELS.get(channel, ("referral", channel))
    q = {"utm_source": source, "utm_medium": medium}
    if campaign:
        q["utm_campaign"] = campaign
    return f"{SITE}{path}?{urllib.parse.urlencode(q)}"


COMMON = [("/", "twitter"), ("/checklist", "twitter"), ("/ratings", "twitter"),
          ("/", "linkedin"), ("/checklist", "hn"), ("/leaderboard", "reddit"),
          ("/", "dm"), ("/checklist", "dm")]

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        print(tag(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    else:
        print("\n  tag every link you share. anything untagged is unattributable.\n")
        for p, c in COMMON:
            print(f"  {c:9s} {tag(p, c)}")
        print(f"\n  any other: python3 share.py <path> <{'|'.join(list(CHANNELS)[:6])}|...>")
        print("  a named person works too: python3 share.py /checklist danny\n")
