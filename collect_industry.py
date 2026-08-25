#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, shutil
from pathlib import Path

INDUSTRY = "全国地产"
base = Path(__file__).resolve().parent
text = (base / "data/versions/a_share/20260823_190219/companies.js").read_text(encoding="utf-8")
data = json.loads(text[text.index("(") + 1: text.rindex(")")])
comps = [c for c in data["companies"] if c.get("industry") == INDUSTRY]
print(f"{INDUSTRY} 公司数: {len(comps)}")

dest = base / "output" / INDUSTRY
dest.mkdir(exist_ok=True)
copied, missing = 0, []
for c in comps:
    src = base / "output" / f'{c["code"].replace(".", "_")}_value_line.pdf'
    if src.exists():
        shutil.copy2(src, dest / src.name)
        copied += 1
        print(f'  {c["code"]}  {c["name"]}')
    else:
        missing.append(c["code"])
print(f"已复制 {copied} 个到 {dest}")
if missing:
    print("缺失:", missing)
