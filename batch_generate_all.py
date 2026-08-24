#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量为版本内所有 A 股生成 Value Line PDF（逐只容错，失败不中断）。"""

import sys
import time
import traceback
from pathlib import Path

import generate_value_line_pdf as g

BASE_DIR = Path(__file__).resolve().parent


def main():
    version_id = sys.argv[1] if len(sys.argv) > 1 else None
    out_dir = BASE_DIR / "output"

    g.register_fonts()
    versions = g.load_versions()
    version_id = version_id or (versions[0]["version"] if versions else None)
    if not version_id:
        sys.exit("错误：没有可用的数据版本")

    stocks_dir = g.VERSIONS_DIR / version_id / "stocks"
    codes = sorted(p.stem for p in stocks_dir.glob("*.js"))
    total = len(codes)
    print(f"版本 {version_id}：共 {total} 只股票，输出到 {out_dir}", flush=True)

    ok = 0
    failures = []
    start = time.time()
    for i, code in enumerate(codes, 1):
        try:
            g.generate(code, version_id, out_dir)
            ok += 1
        except SystemExit as e:
            failures.append((code, str(e)))
        except Exception:
            failures.append((code, traceback.format_exc(limit=1).strip()))
        if i % 250 == 0 or i == total:
            elapsed = time.time() - start
            print(f"  进度 {i}/{total}  成功 {ok}  失败 {len(failures)}  用时 {elapsed:.0f}s", flush=True)

    print(f"\n完成：成功 {ok} / {total}，失败 {len(failures)}", flush=True)
    if failures:
        log = BASE_DIR / "batch_failures.log"
        log.write_text("\n".join(f"{c}\t{m}" for c, m in failures), encoding="utf-8")
        print(f"失败明细见 {log}", flush=True)
        for c, m in failures[:20]:
            print(f"  [失败] {c}: {m}", flush=True)


if __name__ == "__main__":
    main()
