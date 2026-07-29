import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/backend-credential-inventory.json"
READINESS_WORKFLOW = ROOT / ".github/workflows/backend-credential-readiness.yml"
VALUE_AUDIT_WORKFLOW = ROOT / ".github/workflows/backend-protected-value-audit.yml"


def inventory_names() -> set[str]:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    names = {variable["name"] for variable in inventory.get("non_secret_variables", [])}
    for group in inventory.get("secret_groups", []):
        for key in ("secrets", "conditional_secrets"):
            names.update(secret["name"] for secret in group.get(key, []))
        for credential_set in group.get("credential_sets", []):
            names.update(credential_set.get("any_of", []))
            names.update(credential_set.get("optional", []))
    return names


class BackendCredentialReadinessWorkflowTest(unittest.TestCase):
    def test_readiness_workflow_uses_metadata_without_protected_environment(self) -> None:
        workflow = READINESS_WORKFLOW.read_text(encoding="utf-8")

        self.assertNotIn("environment: production-backend", workflow)
        self.assertIn("environments/production-backend/secrets", workflow)
        self.assertIn("environments/production-backend/variables", workflow)
        self.assertIn("--environment-secrets-json", workflow)
        self.assertIn("--environment-variables-json", workflow)

        referenced_secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", workflow))
        self.assertEqual({"NUTSNEWS_MAINTENANCE_GITHUB_TOKEN"}, referenced_secrets)

    def test_protected_value_audit_maps_inventory_names(self) -> None:
        workflow = VALUE_AUDIT_WORKFLOW.read_text(encoding="utf-8")

        missing = [
            name
            for name in sorted(inventory_names())
            if not re.search(rf"^\s+{re.escape(name)}:", workflow, re.MULTILINE)
        ]

        self.assertEqual([], missing)
        self.assertIn("environment: production-backend", workflow)


if __name__ == "__main__":
    unittest.main()
