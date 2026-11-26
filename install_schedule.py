#!/usr/bin/env python3
"""Install the Tuesday–Thursday macOS sync after the first successful run."""
import os
import argparse
import plistlib
import subprocess
import sys
from pathlib import Path


root = Path(__file__).resolve().parent
label = 'local.lse-courses-ai.sync'
agents = Path.home() / 'Library' / 'LaunchAgents'
path = agents / f'{label}.plist'
state = root / '.local'
domain = f'gui/{os.getuid()}'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hour', type=int, choices=range(24), default=20, metavar='0-23')
parser.add_argument('--minute', type=int, choices=range(60), default=0, metavar='0-59')
args = parser.parse_args()

if not (state / 'session.json').is_file():
    sys.exit('请先运行 lse_weekly.py --login。')
if path.exists():
    sys.exit(f'已有任务配置：{path}')
agents.mkdir(exist_ok=True)
config = {
    'Label': label,
    'ProgramArguments': [sys.executable, str(root / 'sync_courses.py')],
    'WorkingDirectory': str(root),
    'StartCalendarInterval': [{'Weekday': day, 'Hour': args.hour, 'Minute': args.minute}
                              for day in (2, 3, 4)],
    'StandardOutPath': str(state / 'sync.log'),
    'StandardErrorPath': str(state / 'sync.log'),
    'EnvironmentVariables': {'PYTHONUNBUFFERED': '1'},
}
with path.open('xb') as file:
    plistlib.dump(config, file)
try:
    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(path)], check=True)
except subprocess.CalledProcessError:
    path.unlink()
    raise
print(f'已安装每周二、三、四 {args.hour:02}:{args.minute:02} 下载。日志：{state / "sync.log"}')
