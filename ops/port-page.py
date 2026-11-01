#!/usr/bin/env python3
"""Port the dashboard page from home-devops: the two boxes run the same page
and differ only in the lines replaced below. Run from the pve-stack root:
    python3 ops/port-page.py ../home-devops/dashboard
"""
import sys
src = sys.argv[1] if len(sys.argv) > 1 else "../home-devops/dashboard"
PAGE = [
    ('<html lang="en" data-copy="canary">', '<html lang="en" data-copy="pink">'),
    ('<title>Home Dev-Ops · promox</title>', '<title>Dev-Ops · pve-code1</title>'),
    ('<h1><span id="product">Home Dev-Ops</span> &middot; <b id="boxname">promox</b></h1>',
     '<h1><span id="product">Dev-Ops</span> &middot; <b id="boxname">pve-code1</b></h1>'),
    ('''const BOX = {name: "promox", where: "Home", product: "Home Dev-Ops", copy: "canary",
  rebootConfirm: "REBOOT-HOST", nicRejoin: false, unitLabels: {}};''',
     '''const BOX = {name: "pve-code1", where: "Office", product: "Dev-Ops", copy: "pink",
  rebootConfirm: "REBOOT-PVE-CODE1", nicRejoin: true, unitLabels: {camrelay: "K2 Plus camera (camrelay)"}};'''),
]
LOGIN = [
    ('<title>Sign in · Home Dev-Ops · promox</title>', '<title>Sign in · Dev-Ops · pve-code1</title>'),
    ('--copy:#f6e6a0;--copy-deep:#e6cf63}', '--copy:#f6cfd8;--copy-deep:#e8a5b6}'),
    ('<h1>Home Dev-Ops &middot; <b>promox</b></h1>', '<h1>Dev-Ops &middot; <b>pve-code1</b></h1>'),
]
for name, pairs in (("page.html", PAGE), ("login.html", LOGIN)):
    s = open(f"{src}/{name}").read()
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"{name}: expected exactly one of {old[:50]!r} - the home page changed shape; update port-page.py")
        s = s.replace(old, new)
    open(f"ops/{name}", "w").write(s)
    print("ported", name)
