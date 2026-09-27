"""Explicit live integration test; writes only inside a temporary directory."""
import json
import os
from pathlib import Path
import sys
import tempfile
from termux_agent.agent import Agent
from termux_agent.config import Config


def main():
    if '--key-stdin' in sys.argv:
        os.environ['TERMUX_AGENT_API_KEY'] = sys.stdin.readline().strip()
    report = {}
    cfg = Config.load()
    events = []
    with tempfile.TemporaryDirectory(prefix='ta-smoke-', dir=Path.cwd()) as directory:
        # Allow only calls confined to this test. Unknown/risky calls are denied.
        def approve(name, args):
            return False
        agent = Agent(cfg, approve=approve, event=lambda k, p: events.append({'kind': k, **p}))
        try:
            for label, prompt in [
                ('hello', 'hello'),
                ('uname', '执行 uname -a，并报告结果。'),
                ('android', '使用 shell 查看 Android 版本（getprop ro.build.version.release）和 uname -a。'),
                ('files', f'请用 write_file 在 {directory}/sample.txt 写入 alpha，然后用 edit_file 将 alpha 改为 beta，最后必须用 read_file 重新读取验证后报告。不要用 shell。'),
                ('error_recovery', f'请用 read_file 读取 {directory}/missing.txt；若不存在，如实说明错误，不要创建文件。'),
            ]:
                events.clear()
                answer = agent.run(prompt)
                report[label] = {'answer': answer, 'stats': agent.stats(), 'events': list(events)}
            report['file_verified_by_host'] = Path(directory, 'sample.txt').read_text().strip() == 'beta' if Path(directory, 'sample.txt').exists() else False
        finally:
            agent.client.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
