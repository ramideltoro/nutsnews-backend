import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/backend-credential-inventory.json"
WORKFLOW = ROOT / ".github/workflows/backend-credential-readiness.yml"


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
    def test_readiness_workflow_maps_inventory_names(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        missing = [
            name
            for name in sorted(inventory_names())
            if not re.search(rf"^\s+{re.escape(name)}:", workflow, re.MULTILINE)
        ]

        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
