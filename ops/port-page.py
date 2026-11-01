#!/usr/bin/env python3
"""Port the dashboard page from home-devops: the two boxes run the same page
and differ only in the BOX settings block and a few identity strings.
Run from the pve-stack root:
    python3 ops/port-page.py ../home-devops/dashboard

The BOX block is swapped as a whole (found by its start and end markers), so
adding a setting on the home side only needs the office's copy of it below.
classic.html (the show report, kept at /classic) is not ported: each box keeps
its own copy as it was when Midnight replaced it.
"""
import re
import sys

src = sys.argv[1] if len(sys.argv) > 1 else "../home-devops/dashboard"

OFFICE_BOX = '''const BOX = {name: "pve-code1", where: "Office", product: "Dev-Ops", accent: "#f472b6", accent2: "#a78bfa", peerAccent: "#2dd4bf",
  wan: "nic0", rebootConfirm: "REBOOT-PVE-CODE1", nicRejoin: true, unitLabels: {camrelay: "K2 Plus camera (camrelay)"},
  bridges: {vmbr0: ["lighting network", "everyone's way out: the DMX fixtures, the K2 printer and the internet via 2.0.0.2 (the host falls back to wifi)"],
            vmbr1: ["internal · host only", "how the host's tunnel and the dashboard reach containers; nothing outside can reach it"]}};'''

PAGE = [
    ('<title>Home Dev-Ops · promox</title>', '<title>Dev-Ops · pve-code1</title>'),
    ('<h1 id="boxname">promox</h1><span id="sub">Home Dev-Ops</span>', '<h1 id="boxname">pve-code1</h1><span id="sub">Dev-Ops</span>'),
]
LOGIN = [
    ('<title>Sign in · Home Dev-Ops · promox</title>', '<title>Sign in · Dev-Ops · pve-code1</title>'),
    ('--accent:#2dd4bf;--accent-2:#3b82f6;', '--accent:#f472b6;--accent-2:#a78bfa;'),
    ('<h1>promox</h1>', '<h1>pve-code1</h1>'),
    ('<p class="sub">Home Dev-Ops. Sign in to see how this box is doing.</p>', '<p class="sub">Dev-Ops. Sign in to see how this box is doing.</p>'),
]


def swap(name, s, pairs):
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"{name}: expected exactly one of {old[:60]!r} - the home page changed shape; update port-page.py")
        s = s.replace(old, new)
    return s


page = open(f"{src}/page.html").read()
m = re.search(r"^const BOX = \{.*?\};$", page, re.S | re.M)
if not m or 'name: "promox"' not in m.group(0):
    sys.exit("page.html: could not find the home BOX block")
page = swap("page.html", page[:m.start()] + OFFICE_BOX + page[m.end():], PAGE)
open("ops/page.html", "w").write(page)
open("ops/login.html", "w").write(swap("login.html", open(f"{src}/login.html").read(), LOGIN))
print("ported page.html and login.html")
