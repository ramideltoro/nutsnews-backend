"""Regression tests for the scheduled RabbitMQ canary dispatch contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "backend-rabbitmq-canary.yml"


class RabbitMqCanaryScheduleTests(unittest.TestCase):
    def test_schedule_normalizes_missing_workflow_dispatch_drill_input(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        fallback = "DRILL: ${{ inputs.drill || 'consumer-loss' }}"
        self.assertEqual(2, workflow.count(fallback))
        self.assertIn("github.event_name == 'schedule' && 'canary'", workflow)
        self.assertIn('if [[ "$EVENT_NAME" != "schedule"', workflow)


if __name__ == "__main__":
    unittest.main()
