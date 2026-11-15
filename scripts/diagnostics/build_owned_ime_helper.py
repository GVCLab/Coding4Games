"""Build the host-only x86 IMM helper at an idle native-test boundary."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--zig', required=True, type=Path)
parser.add_argument('--cache', required=True, type=Path)
parser.add_argument('--csc', type=Path, default=Path(
    'C:/Windows/Microsoft.NET/Framework/v4.0.30319/csc.exe'))
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
destination = root / 'coding4game_gui/benchmark/windows/drivers/windows_xinput/native_ime'
compiler = args.zig.resolve(strict=True)
version = subprocess.check_output([str(compiler), 'version'], text=True).strip()
if version != '0.13.0':
    parser.error('this build recipe requires Zig0.13.0')
environment = os.environ.copy()
environment['ZIG_GLOBAL_CACHE_DIR'] = str(args.cache.resolve())
environment['ZIG_LOCAL_CACHE_DIR'] = str(args.cache.resolve())
subprocess.run([str(compiler), 'cc', '-target', 'x86-windows-gnu', '-shared', '-O2',
                '-o', str(destination / 'owned_ime_context32.dll'),
                str(destination / 'owned_ime_context.c'), '-luser32', '-limm32'],
               check=True, env=environment)
subprocess.run([str(args.csc.resolve(strict=True)), '/nologo', '/platform:x86',
                '/target:exe', '/r:System.Web.Extensions.dll',
                '/out:' + str(destination / 'OwnedImeController.exe'),
                str(destination / 'OwnedImeController.cs')], check=True)
names = ('owned_ime_context.c', 'OwnedImeController.cs', 'OwnedImeController.exe',
         'owned_ime_context32.dll')
manifest = dict(schema=1, architecture='x86', compiler='Zig0.13.0 and .NET Framework4 csc',
                sha256={name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
                        for name in names})
(destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
print(json.dumps(manifest))
