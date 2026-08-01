from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = (
    ROOT / ".github" / "workflows" / "backend-observability-failure-drills.yml"
)

DRILLS = {
    "worker-unavailable",
    "rabbitmq-zero-consumer",
    "rabbitmq-growing-dlq",
    "postgres-relay-lag",
    "backend-readiness-failed",
}
INPUTS = {
    "drill",
    "evidence_id",
    "dry_run",
    "confirm_repository",
    "confirm_environment",
    "confirm_target",
    "confirm_drill",
}


def extract_job(workflow: str, job_name: str) -> str:
    match = re.search(rf"^  {re.escape(job_name)}:\n", workflow, re.MULTILINE)
    if match is None:
        return ""
    next_job = re.search(r"^  [a-z0-9-]+:\n", workflow[match.end() :], re.MULTILINE)
    end = len(workflow) if next_job is None else match.end() + next_job.start()
    return workflow[match.start() : end]


def workflow_inputs(workflow: str) -> set[str]:
    match = re.search(
        r"(?ms)^    inputs:\n(?P<inputs>.*?)^permissions:\n",
        workflow,
    )
    if match is None:
        return set()
    return set(re.findall(r"^      ([a-z][a-z0-9_]*):\n", match.group("inputs"), re.MULTILINE))


def drill_options(workflow: str) -> set[str]:
    match = re.search(
        r"(?ms)^      drill:\n.*?^        options:\n(?P<options>(?:^          - [^\n]+\n)+)",
        workflow,
    )
    if match is None:
        return set()
    return {
        line.strip().removeprefix("- ")
        for line in match.group("options").splitlines()
    }


def validate_workflow_text(workflow: str) -> list[str]:
    errors: list[str] = []
    validate_job = extract_job(workflow, "validate-dispatch")
    protected_job = extract_job(workflow, "protected-drill")
    if not validate_job:
        errors.append("missing validate-dispatch job")
    if not protected_job:
        errors.append("missing protected-drill job")
    if errors:
        return errors

    if workflow_inputs(workflow) != INPUTS:
        errors.append("workflow must expose exactly the seven fixed inputs")
    if drill_options(workflow) != DRILLS:
        errors.append("drill input must expose exactly the five approved drills")
    if not re.search(r"(?ms)^      dry_run:\n.*?^        default: true$", workflow):
        errors.append("dry_run must default to true")
    if "duration_seconds:" in workflow.split("permissions:", 1)[0]:
        errors.append("workflow must not expose caller-controlled duration")
    if (
        "run-name: Backend observability drill / "
        "${{ inputs.drill }} / ${{ inputs.evidence_id }}" not in workflow
    ):
        errors.append("run name must include the fixed drill and evidence identity")

    if "environment:" in validate_job or "secrets." in validate_job:
        errors.append("dispatch validation must not access environment protection or secrets")
    if protected_job.count("environment: production-backend") != 1:
        errors.append("protected drill must use production-backend exactly once")
    if workflow.count("environment: production-backend") != 1:
        errors.append("production-backend must appear only on the protected job")
    if "needs: validate-dispatch" not in protected_job:
        errors.append("protected drill must depend on dispatch validation")
    if "timeout-minutes: 30" not in protected_job:
        errors.append("protected drill must retain a bounded 30-minute timeout")

    exact_guards = (
        '"$DISPATCH_REPOSITORY" != "ramideltoro/nutsnews-backend"',
        '"$DISPATCH_REF" != "refs/heads/main"',
        '"$CONFIRM_REPOSITORY" != "ramideltoro/nutsnews-backend"',
        '"$CONFIRM_ENVIRONMENT" != "production-backend"',
        '"$CONFIRM_TARGET" != "backend.nutsnews.com"',
        '"$CONFIRM_DRILL" != "$DRILL"',
        "^nnobs-[0-9]{10,20}-[a-f0-9]{8}$",
    )
    for guard in exact_guards:
        if guard not in validate_job:
            errors.append(f"dispatch validation is missing exact guard: {guard}")
    if '[[ "$DISPATCH_REPOSITORY" == "ramideltoro/nutsnews-backend" ]]' not in protected_job:
        errors.append("protected job must revalidate the exact repository")
    if '[[ "$DISPATCH_REF" == "refs/heads/main" ]]' not in protected_job:
        errors.append("protected job must revalidate the exact main ref")
    if '[[ "$CONFIRM_DRILL" == "$DRILL" ]]' not in protected_job:
        errors.append("protected job must revalidate the typed drill")

    if "group: backend-worker-runtime-operations" not in workflow:
        errors.append("failure drills must serialize with worker runtime operations")
    if "cancel-in-progress: false" not in workflow:
        errors.append("an in-progress failure drill must never be cancelled by concurrency")
    if "permissions:\n  contents: read" not in workflow:
        errors.append("workflow permissions must remain contents: read")

    required_remote_fragments = (
        'readonly remote_hook="/usr/local/sbin/nutsnews-observability-failure-drill"',
        'readonly remote_host="65.75.201.18"',
        'readonly drill_id="$EVIDENCE_ID"',
        'readonly duration_seconds="900"',
        '--action "$action"',
        '--drill "$DRILL"',
        '--drill-id "$drill_id"',
        '--duration-seconds "$duration_seconds"',
        '--confirm-target "$CONFIRM_TARGET"',
        '--confirm-drill "$CONFIRM_DRILL"',
        'if [[ "$action" == "inject" ]]',
        "remote_args+=(--execute)",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=",
    )
    for fragment in required_remote_fragments:
        if fragment not in protected_job:
            errors.append(f"protected hook invocation is missing: {fragment}")
    if protected_job.count("remote_args+=(--execute)") != 1:
        errors.append("--execute must exist only on the fixed inject action")

    recovery_arm = protected_job.find("recovery_required=true")
    injection = protected_job.find("invoke_hook inject")
    if recovery_arm < 0 or injection < 0 or recovery_arm > injection:
        errors.append("recovery must be armed before injection")
    for fragment in (
        "trap recover_on_exit EXIT INT TERM",
        "invoke_hook recover || recovery_rc=",
        'sleep "$duration_seconds"',
        "invoke_hook status",
    ):
        if fragment not in protected_job:
            errors.append(f"protected drill is missing recovery/observation guardrail: {fragment}")
    dry_exit = protected_job.find('if [[ "$DRY_RUN" == "true" ]]')
    if dry_exit < 0 or injection < 0 or dry_exit > injection:
        errors.append("dry-run must exit before the injection action")

    for fragment in (
        "safe_metadata_only",
        "allowed_scalar = {",
        'set(check) != {"name", "status"}',
        "len(checks) > 32",
        "65_536",
        "262_144",
        "backend-observability-drill-artifact/evidence.json",
        "retention-days: 120",
    ):
        if fragment not in protected_job:
            errors.append(f"bounded artifact handling is missing: {fragment}")
    upload_match = re.search(
        r"(?ms)^      - name: Upload bounded observability drill evidence\n(?P<step>.*)\Z",
        protected_job,
    )
    if upload_match is None:
        errors.append("missing bounded artifact upload")
    else:
        upload_step = upload_match.group("step")
        if "backend-observability-drill-artifact/evidence.json" not in upload_step:
            errors.append("artifact upload must include only the combined evidence file")
        if "drill-raw" in upload_step or "drill-safe" in upload_step:
            errors.append("raw or intermediate hook reports must never be uploaded")
        if not re.search(r"uses: actions/upload-artifact@[0-9a-f]{40}", upload_step):
            errors.append("artifact upload action must be pinned to a full commit SHA")

    for forbidden in (
        "command_input",
        "remote_command_input",
        "script_body",
        "service_name:",
        "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    ):
        if forbidden in workflow:
            errors.append(f"workflow contains forbidden free-form or sensitive input: {forbidden}")
    return errors


class BackendObservabilityFailureDrillWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def validate(self, workflow: str | None = None) -> list[str]:
        return validate_workflow_text(self.workflow if workflow is None else workflow)

    def test_committed_workflow_passes(self) -> None:
        self.assertEqual(self.validate(), [])

    def test_dry_run_must_remain_default(self) -> None:
        altered = self.workflow.replace("        default: true\n", "        default: false\n", 1)
        self.assertIn("dry_run must default to true", self.validate(altered))

    def test_drill_set_is_fixed(self) -> None:
        altered = self.workflow.replace(
            "          - backend-readiness-failed\n",
            "          - backend-readiness-failed\n          - arbitrary-command\n",
            1,
        )
        self.assertIn(
            "drill input must expose exactly the five approved drills",
            self.validate(altered),
        )

    def test_main_and_environment_are_immutable(self) -> None:
        altered = self.workflow.replace("refs/heads/main", "refs/heads/release", 2)
        errors = self.validate(altered)
        self.assertTrue(any("refs/heads/main" in error for error in errors))
        altered = self.workflow.replace("    environment: production-backend\n", "", 1)
        self.assertIn(
            "protected drill must use production-backend exactly once",
            self.validate(altered),
        )

    def test_duration_cannot_be_caller_controlled(self) -> None:
        altered = self.workflow.replace(
            "      dry_run:\n",
            "      duration_seconds:\n        type: string\n      dry_run:\n",
            1,
        )
        errors = self.validate(altered)
        self.assertIn("workflow must expose exactly the seven fixed inputs", errors)
        self.assertIn("workflow must not expose caller-controlled duration", errors)

    def test_execute_remains_inject_only(self) -> None:
        altered = self.workflow.replace(
            'if [[ "$action" == "inject" ]]; then',
            'if [[ -n "$action" ]]; then',
            1,
        )
        self.assertTrue(
            any(
                'if [[ "$action" == "inject" ]]' in error
                for error in self.validate(altered)
            )
        )

    def test_recovery_is_armed_before_injection(self) -> None:
        altered = self.workflow.replace(
            "          recovery_required=true\n          invoke_hook inject\n",
            "          invoke_hook inject\n          recovery_required=true\n",
            1,
        )
        self.assertIn("recovery must be armed before injection", self.validate(altered))

    def test_raw_hook_output_cannot_be_uploaded(self) -> None:
        altered = self.workflow.replace(
            "backend-observability-drill-artifact/evidence.json",
            "backend-observability-drill-raw",
            1,
        )
        self.assertTrue(
            any("artifact upload" in error for error in self.validate(altered))
        )

    def test_evidence_retention_is_120_days(self) -> None:
        altered = self.workflow.replace("retention-days: 120", "retention-days: 14", 1)
        self.assertTrue(
            any("retention-days: 120" in error for error in self.validate(altered))
        )


if __name__ == "__main__":
    unittest.main()
