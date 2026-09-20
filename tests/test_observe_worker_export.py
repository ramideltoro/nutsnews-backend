import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/observe-recovery-export.sh'

class WorkerExportTest(unittest.TestCase):
    def test_inactive_workers_are_copied_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / 'docker'
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            docker.chmod(0o755)
            env = {**os.environ, 'PATH': directory + ':' + os.environ['PATH']}
            for stage in ('scheduler', 'translation', 'fetcher'):
                result = subprocess.run(['bash', str(SCRIPT), 'worker-' + stage + '-app'], env=env, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.splitlines(), ['cp', 'nutsnews-worker-uplift-' + stage + '-1:/app/.', '-'])
            for action in ('worker-translation-start', 'worker-scheduler-restart', 'worker-other-app', 'worker-translation-app;id'):
                result = subprocess.run(['bash', str(SCRIPT), action], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 64)
