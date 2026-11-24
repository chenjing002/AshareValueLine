#!/usr/bin/env python3
"""Stage the browser site and Agent API together; publish only allowlisted files."""
import argparse
import json
import re
import shutil
from pathlib import Path

from build_agent_data import build, read_wrapped


def stage(root: Path, output: Path, keep: int = 2):
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f'{output} already exists; choose a new staging directory')
    if keep < 1:
        raise ValueError('keep must be positive')
    output.mkdir(parents=True)
    shutil.copy2(root / 'index.html', output / 'index.html')
    shutil.copytree(root / 'static', output / 'static')
    (output / '.nojekyll').touch()
    versions = read_wrapped(root / 'data/versions.js', 'window.VL_VERSIONS = ', ';')
    for market, entries in versions.items():
        if market not in ('a_share', 'hk'):
            raise ValueError(f'unknown market: {market}')
        versions[market] = sorted(entries, key=lambda e: e['version'], reverse=True)[:keep]
        for entry in versions[market]:
            version = entry['version']
            if not re.fullmatch(r'\d{8}_\d{6}', version):
                raise ValueError(f'invalid version: {version}')
            relative = Path('data/versions') / market / version
            # Raw logs/failure diagnostics are not website assets.
            source, target = root / relative, output / relative
            target.mkdir(parents=True)
            for name in ('manifest.json', 'companies.js', 'screen.js'):
                shutil.copy2(source / name, target / name)
            (target / 'stocks').mkdir()
            for stock in (source / 'stocks').glob('*.js'):
                shutil.copy2(stock, target / 'stocks' / stock.name)
    (output / 'data/versions.js').write_text('window.VL_VERSIONS = ' + json.dumps(versions, ensure_ascii=False) + ';\n', encoding='utf-8')
    build(output)
    files = [p for p in output.rglob('*') if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    if size > 950_000_000:
        raise ValueError(f'Pages size {size:,} exceeds 950 MB safety limit')
    print(f'Pages: {len(files):,} files, {size:,} bytes; browser versions retained: {keep}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='构建 GitHub Pages 网站与 Agent 数据接口')
    parser.add_argument('--output', type=Path, default=Path('_site'))
    args = parser.parse_args()
    stage(Path(__file__).resolve().parent, args.output)
