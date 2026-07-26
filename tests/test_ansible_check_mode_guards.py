#!/usr/bin/env python3
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_role_file(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class AnsibleCheckModeGuardTests(unittest.TestCase):
    def test_handlers_do_not_reload_or_restart_services_in_check_mode(self):
        handlers = read_role_file("ansible/roles/backend_baseline/handlers/main.yml")
        handler_blocks = re.findall(r"(?m)^- name: .*(?:\n(?!- name: ).*)*", handlers)
        guarded_handlers = [block for block in handler_blocks if "when: not ansible_check_mode" in block]
        self.assertEqual(len(guarded_handlers), len(handler_blocks))

    def test_fail2ban_skips_service_management_when_check_mode_package_is_pending(self):
        fail2ban = read_role_file("ansible/roles/backend_baseline/tasks/fail2ban.yml")
        self.assertIn("register: backend_fail2ban_package", fail2ban)
        self.assertIn("backend_fail2ban_service_manageable", fail2ban)
        self.assertIn("not (backend_fail2ban_package is changed)", fail2ban)
        self.assertIn("when: backend_fail2ban_service_manageable | bool", fail2ban)

    def test_sysstat_skips_service_management_when_check_mode_package_is_pending(self):
        monitoring = read_role_file("ansible/roles/backend_baseline/tasks/monitoring.yml")
        self.assertIn("register: backend_monitoring_packages", monitoring)
        self.assertIn("backend_monitoring_sysstat_manageable", monitoring)
        self.assertIn("not (backend_monitoring_packages is changed)", monitoring)
        self.assertIn("when: backend_monitoring_sysstat_manageable | bool", monitoring)

    def test_swapfile_permissions_wait_for_real_swapfile_creation_in_check_mode(self):
        swap = read_role_file("ansible/roles/backend_baseline/tasks/swap.yml")
        self.assertIn("backend_swapfile_manageable", swap)
        self.assertIn("backend_swapfile_stat.stat.exists | default(false)", swap)
        self.assertGreaterEqual(swap.count("backend_swapfile_manageable | bool"), 2)

    def test_baseline_package_upgrade_and_reboot_are_opt_in(self):
        defaults = read_role_file("ansible/roles/backend_baseline/defaults/main.yml")
        maintenance = read_role_file("ansible/roles/backend_baseline/tasks/maintenance.yml")
        self.assertIn("backend_apply_dist_upgrade: false", defaults)
        self.assertIn("backend_apt_autoremove: false", defaults)
        self.assertIn("backend_reboot_if_required: false", defaults)
        self.assertIn("when: backend_apply_dist_upgrade | bool", maintenance)
        self.assertIn("when: backend_apt_autoremove | bool", maintenance)

    def test_backup_timers_are_not_started_in_check_mode(self):
        defaults = read_role_file("ansible/roles/backend_baseline/defaults/main.yml")
        backup = read_role_file("ansible/roles/backend_baseline/tasks/backup.yml")
        self.assertIn("backend_backup_enabled: false", defaults)
        self.assertIn("Enable backup timers", backup)
        self.assertIn("when: not ansible_check_mode", backup)
        self.assertIn("no_log: true", backup)

    def test_postgres_skips_service_management_when_check_mode_packages_are_pending(self):
        postgres = read_role_file("ansible/roles/backend_baseline/tasks/postgres.yml")
        self.assertIn("register: backend_postgres_package_result", postgres)
        self.assertIn("backend_postgres_manageable", postgres)
        self.assertIn("not (backend_postgres_package_result is changed)", postgres)
        self.assertIn("backend_db_dashboard_manageable", postgres)
        self.assertIn("when: backend_postgres_manageable | bool", postgres)
        self.assertIn("register: backend_postgres_validation_user_result", postgres)
        self.assertIn("register: backend_postgres_app_rehearsal_user_result", postgres)
        self.assertIn("backend_postgres_database_manageable", postgres)
        self.assertIn("backend_postgres_database_dependents_manageable", postgres)
        self.assertIn("backend_postgres_privilege_roles_manageable", postgres)
        self.assertIn("(backend_postgres_validation_user_result is changed)", postgres)
        self.assertIn("backend_postgres_privilege_roles_manageable | default(false) | bool", postgres)

    def test_worker_api_skips_service_management_when_check_mode_package_is_pending(self):
        worker_api = read_role_file("ansible/roles/backend_baseline/tasks/worker_api.yml")
        self.assertIn("register: backend_worker_api_packages", worker_api)
        self.assertIn("backend_worker_api_manageable", worker_api)
        self.assertIn("not (backend_worker_api_packages is changed)", worker_api)
        self.assertIn("when: backend_worker_api_manageable | bool", worker_api)
        service_start_block = worker_api.split("- name: Ensure Worker database API service is enabled and started", 1)[1].split(
            "- name: Capture Worker database API loopback health",
            1,
        )[0]
        self.assertIn("not ansible_check_mode", service_start_block)
        self.assertIn("no_log: true", worker_api)

    def test_rabbitmq_repairs_data_tree_before_runtime_probe(self):
        defaults = read_role_file("ansible/roles/backend_rabbitmq/defaults/main.yml")
        tasks = read_role_file("ansible/roles/backend_rabbitmq/tasks/main.yml")
        self.assertIn('backend_rabbitmq_container_uid: "999"', defaults)
        self.assertIn('backend_rabbitmq_container_gid: "999"', defaults)
        self.assertIn("backend_rabbitmq_probe_state_dir: /var/lib/nutsnews/rabbitmq-probes", defaults)
        self.assertIn("Repair RabbitMQ persistent data tree ownership", tasks)
        data_tree_task = tasks.split("Repair RabbitMQ persistent data tree ownership", 1)[1].split("- name:", 1)[0]
        self.assertIn("recurse: true", data_tree_task)
        self.assertIn('mode: "u+rwX"', data_tree_task)
        self.assertIn("register: backend_rabbitmq_data_tree_permissions", data_tree_task)
        self.assertNotIn("notify:", data_tree_task)
        self.assertNotIn("Restart RabbitMQ", data_tree_task)
        self.assertIn("backend_rabbitmq_data_tree_permissions is changed", tasks)
        self.assertIn("Remove legacy probe state from RabbitMQ broker data directory", tasks)
        self.assertIn("backend_rabbitmq_legacy_probe_state_file is changed", tasks)


if __name__ == "__main__":
    unittest.main()
