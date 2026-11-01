#!/usr/bin/env python3
"""Port the dashboard page from home-devops: the two boxes run the same page
and differ only in the lines replaced below. Run from the pve-stack root:
    python3 ops/port-page.py ../home-devops/dashboard
classic.html (the show report, kept at /classic) is not ported: each box keeps
its own copy as it was when Midnight replaced it.
"""
import sys
src = sys.argv[1] if len(sys.argv) > 1 else "../home-devops/dashboard"
PAGE = [
    ('<title>Home Dev-Ops · promox</title>', '<title>Dev-Ops · pve-code1</title>'),
    ('<h1 id="boxname">promox</h1><span id="sub">Home Dev-Ops</span>', '<h1 id="boxname">pve-code1</h1><span id="sub">Dev-Ops</span>'),
    ('''const BOX = {name: "promox", where: "Home", product: "Home Dev-Ops", accent: "#2dd4bf", accent2: "#3b82f6", peerAccent: "#f472b6",
  wan: "eno1", rebootConfirm: "REBOOT-HOST", nicRejoin: false, unitLabels: {}};''',
     '''const BOX = {name: "pve-code1", where: "Office", product: "Dev-Ops", accent: "#f472b6", accent2: "#a78bfa", peerAccent: "#2dd4bf",
  wan: "wlp59s0", rebootConfirm: "REBOOT-PVE-CODE1", nicRejoin: true, unitLabels: {camrelay: "K2 Plus camera (camrelay)"}};'''),
]
LOGIN = [
    ('<title>Sign in · Home Dev-Ops · promox</title>', '<title>Sign in · Dev-Ops · pve-code1</title>'),
    ('--accent:#2dd4bf;--accent-2:#3b82f6;', '--accent:#f472b6;--accent-2:#a78bfa;'),
    ('<h1>promox</h1>', '<h1>pve-code1</h1>'),
    ('<p class="sub">Home Dev-Ops. Sign in to see how this box is doing.</p>', '<p class="sub">Dev-Ops. Sign in to see how this box is doing.</p>'),
]
for name, pairs in (("page.html", PAGE), ("login.html", LOGIN)):
    s = open(f"{src}/{name}").read()
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"{name}: expected exactly one of {old[:50]!r} - the home page changed shape; update port-page.py")
        s = s.replace(old, new)
    open(f"ops/{name}", "w").write(s)
    print("ported", name)
