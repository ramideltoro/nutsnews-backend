#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backend_health_report


def command(stdout: str, returncode: int = 0) -> dict[str, object]:
    return {"stdout": stdout, "stderr": "", "returncode": returncode}


def relay_unit_states(
    *,
    timer_active: str = "active",
    timer_enabled: str = "enabled",
    service_active: str = "inactive",
    service_enabled: str = "static",
    service_result: str = "success",
) -> str:
    return (
        f"nutsnews-supabase-sync-relay.timer.active={timer_active}\n"
        f"nutsnews-supabase-sync-relay.timer.enabled={timer_enabled}\n"
        "nutsnews-supabase-sync-relay.timer.load_state=loaded\n"
        "nutsnews-supabase-sync-relay.timer.sub_state=waiting\n"
        "nutsnews-supabase-sync-relay.timer.result=success\n"
        "nutsnews-supabase-sync-relay.timer.last_trigger=Thu 2026-07-16 11:59:45 UTC\n"
        f"nutsnews-supabase-sync-relay.service.active={service_active}\n"
        f"nutsnews-supabase-sync-relay.service.enabled={service_enabled}\n"
        "nutsnews-supabase-sync-relay.service.load_state=loaded\n"
        "nutsnews-supabase-sync-relay.service.sub_state=dead\n"
        f"nutsnews-supabase-sync-relay.service.result={service_result}\n"
        "nutsnews-supabase-sync-relay.service.last_trigger=\n"
    )


def relay_last_run(**overrides) -> str:
    report = {
        "schema_version": 2,
        "status": "pass",
        "mode": "sync-once",
        "checked_at_utc": "2026-07-16T11:59:45Z",
        "finished_at_utc": "2026-07-16T11:59:45Z",
        "last_success_at_utc": "2026-07-16T11:59:45Z",
        "last_applied_at_utc": "2026-07-16T11:59:45Z",
        "safe_metadata_only": True,
        "sync": {"status": "applied", "table_count": 2},
        "post_sync": {
            "status": "pass",
            "checks": [
                {"id": "table.public.articles", "status": "pass", "target_lag_rows": 0},
                {"id": "table.public.rss_feeds", "status": "pass", "target_lag_rows": 0},
                {"id": "sequence.public.rss_feeds_id_seq", "status": "pass"},
            ]
        },
        "validation_summary": {
            "expected_table_count": 2,
            "validated_table_count": 2,
            "failed_table_count": 0,
            "max_table_lag_rows": 0,
            "complete": True,
            "safe_metadata_only": True,
        },
    }
    report.update(overrides)
    return json.dumps(report) + "\n"


def fixture_report(**overrides):
    commands = {
        "memory": command(
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:     10415267840   536870912  9448928051     4194304   742391808  9878424780\n"
            "Swap:             0          0          0\n"
        ),
        "kernel": command("7.0.0-28-generic\n"),
        "latest_installed_kernel": command("7.0.0-28-generic\n"),
        "root_disk": command("/dev/vda1 82678120448 2147483648 80530636800 3% /\n"),
        "root_inodes": command("/dev/vda1 5242880 102400 5140480 2% /\n"),
        "failed_units": command(""),
        "reboot_required": command("no\n"),
        "upgradable_count": command("86\n"),
        "service_states": command(
            "ssh=active\n"
            "ufw=active\n"
            "fail2ban=unavailable\n"
            "docker=unavailable\n"
            "caddy=active\n"
            "postgresql=inactive\n"
            "alloy=unavailable\n"
            "sysstat=active\n"
        ),
        "backend_health": command("ok\n"),
        "backup_tools": command("restic=missing\nrclone=missing\npg_dump=missing\ndocker=missing\ncaddy=missing\nalloy=missing\n"),
        "backup_status": command("not_configured\n"),
        "rabbitmq_drift": command("not_configured\n"),
        "rabbitmq_smoke_status": command("not_configured\n"),
        "cleanup_status": command("not_configured\n"),
        "recovery_status": command("not_configured\n"),
        "postgres_status": command("not_configured\n"),
        "postgres_replication_health": command("not_configured\n"),
        "supabase_sync_relay_unit_states": command(relay_unit_states()),
        "supabase_sync_relay_status": command(relay_last_run()),
        "sudo_nopasswd": command("no\n"),
    }
    commands.update(overrides)
    return {
        "version": 1,
        "last_report_run_at": "2026-07-16T12:00:00Z",
        "next_report_run_at": "2026-07-17T12:00:00Z",
        "target": {"host": "65.75.201.18", "user": "rami"},
        "delivery": {"status": "skipped", "detail": "send_email=false"},
        "ssh": {"host": "65.75.201.18", "user": "rami", "commands": commands},
    }


class BackendHealthReportTests(unittest.TestCase):
    def test_redaction_removes_sensitive_values(self):
        raw = (
            "github_pat_1234567890abcdefghijklmnopqrstuvwxyzABCDEF "
            "postgres://user:secret@example.com/db "
            "person@example.com "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
        redacted = backend_health_report.redact(raw)
        self.assertNotIn("github_pat_", redacted)
        self.assertNotIn("secret@example", redacted)
        self.assertNotIn("person@example.com", redacted)
        self.assertNotIn("abc\n-----END", redacted)

    def test_classify_fixture_distinguishes_known_missing_services(self):
        checks, summary = backend_health_report.classify(fixture_report())
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["root_disk_used_percent"]["status"], "healthy")
        self.assertEqual(by_name["package_updates"]["status"], "warning")
        self.assertEqual(by_name["kernel_alignment"]["status"], "healthy")
        self.assertEqual(by_name["service_docker"]["status"], "not_configured")
        self.assertEqual(by_name["backend_endpoint_health"]["status"], "healthy")
        self.assertEqual(by_name["backup_tooling"]["status"], "not_configured")
        self.assertEqual(by_name["backup_freshness"]["status"], "not_configured")
        self.assertEqual(by_name["backup_verification"]["status"], "not_configured")
        self.assertEqual(by_name["backup_restore_drill"]["status"], "not_configured")
        self.assertEqual(by_name["cleanup_last_run"]["status"], "not_configured")
        self.assertEqual(by_name["recovery_last_run"]["status"], "not_configured")
        self.assertEqual(by_name["postgres_restore_readiness"]["status"], "not_configured")
        self.assertEqual(by_name["sudo_nopasswd"]["status"], "warning")
        self.assertGreaterEqual(summary["warning"], 2)

    def test_postgres_restore_readiness_is_exposed_when_present(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                postgres_status=command(
                    "{"
                    '"status":"healthy",'
                    '"database":"nutsnews_failover",'
                    '"last_restore_drill":{"status":"healthy"},'
                    '"replication":{"lag_status":"not_configured"}'
                    "}\n"
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["postgres_restore_readiness"]["status"], "healthy")
        self.assertIn("database=nutsnews_failover", by_name["postgres_restore_readiness"]["summary"])

    def test_postgres_replication_health_alerts_on_lag(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                postgres_replication_health=command(
                    "{"
                    '"replication":{"status":"fail","lag_status":"lagging","max_lag_seconds":601,"blockers":["replication_lag_exceeds_threshold"]}'
                    "}\n"
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["postgres_replication_health"]["status"], "critical")
        alerts = backend_health_report.current_alerts_from_checks([by_name["postgres_replication_health"]])
        self.assertEqual(alerts[0]["failure_class"], "replication_health")

    def test_postgres_replication_health_allows_idle_slot_when_lag_is_healthy(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                postgres_replication_health=command(
                    "{"
                    '"replication":{'
                    '"mode":"logical_replication",'
                    '"lag_status":"healthy",'
                    '"max_lag_seconds":5,'
                    '"slot_status":"inactive",'
                    '"validation_status":"current",'
                    '"blockers":[]'
                    "}"
                    "}\n"
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["postgres_replication_health"]["status"], "healthy")
        self.assertEqual(backend_health_report.current_alerts_from_checks([by_name["postgres_replication_health"]]), [])

    def test_supabase_sync_relay_health_is_healthy_when_recent_and_timer_active(self):
        checks, _ = backend_health_report.classify(fixture_report())
        by_name = {item["name"]: item for item in checks}
        relay = by_name["supabase_sync_relay_health"]
        self.assertEqual(relay["status"], "healthy")
        self.assertEqual(relay["lag_seconds"], 15)
        self.assertEqual(relay["failed_table_count"], 0)
        self.assertIn("timer=active", relay["summary"])
        self.assertIn("lag_seconds=15", relay["summary"])
        self.assertIn("standby_failover_blocked=false", relay["summary"])

    def test_supabase_sync_relay_health_accepts_recent_noop_parity_pass(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                supabase_sync_relay_status=command(
                    relay_last_run(
                        sync={"status": "not_required", "reason": "standby_already_in_sync"},
                        last_applied_at_utc="2026-07-16T10:00:00Z",
                    )
                )
            )
        )
        relay = {item["name"]: item for item in checks}["supabase_sync_relay_health"]
        self.assertEqual("healthy", relay["status"])
        self.assertEqual(15, relay["lag_seconds"])
        self.assertNotIn("relay_last_run_not_pass", relay["blockers"])

    def test_supabase_sync_relay_lag_over_180_seconds_is_critical_alert(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                supabase_sync_relay_status=command(
                    relay_last_run(
                        checked_at_utc="2026-07-16T11:56:00Z",
                        last_success_at_utc="2026-07-16T11:56:00Z",
                        last_applied_at_utc="2026-07-16T11:56:00Z",
                    )
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        relay = by_name["supabase_sync_relay_health"]
        self.assertEqual(relay["status"], "critical")
        self.assertEqual(relay["lag_seconds"], 240)
        self.assertIn("relay_lag_exceeds_threshold", relay["blockers"])
        alerts = backend_health_report.current_alerts_from_checks([relay])
        self.assertEqual(alerts[0]["failure_class"], "supabase_sync_relay_lag")
        self.assertIn("standby_failover_blocked=true", alerts[0]["message"])

    def test_supabase_sync_relay_missing_or_stopped_is_critical_alert(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                supabase_sync_relay_unit_states=command(relay_unit_states(timer_active="inactive", timer_enabled="disabled")),
                supabase_sync_relay_status=command("not_configured\n"),
            )
        )
        by_name = {item["name"]: item for item in checks}
        relay = by_name["supabase_sync_relay_health"]
        self.assertEqual(relay["status"], "critical")
        self.assertIn("relay_timer_stopped", relay["blockers"])
        self.assertIn("relay_report_missing", relay["blockers"])
        alerts = backend_health_report.current_alerts_from_checks([relay])
        self.assertEqual(alerts[0]["failure_class"], "supabase_sync_relay_stopped")

    def test_supabase_sync_relay_failed_table_count_is_critical(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                supabase_sync_relay_status=command(
                    relay_last_run(
                        status="fail",
                        post_sync={
                            "status": "fail",
                            "failed_required_checks": ["table.public.articles"],
                            "checks": [
                                {"id": "table.public.articles", "status": "fail", "target_lag_rows": 1},
                                {"id": "table.public.rss_feeds", "status": "pass", "target_lag_rows": 0},
                            ],
                        },
                        validation_summary={
                            "expected_table_count": 2,
                            "validated_table_count": 2,
                            "failed_table_count": 1,
                            "max_table_lag_rows": 1,
                            "complete": True,
                            "safe_metadata_only": True,
                        },
                    )
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        relay = by_name["supabase_sync_relay_health"]
        self.assertEqual(relay["status"], "critical")
        self.assertEqual(relay["failed_table_count"], 1)
        self.assertIn("relay_failed_tables_present", relay["blockers"])
        self.assertIn("last_error=post_sync_failed_required_checks:1", relay["summary"])

    def test_cleanup_status_is_exposed_when_present(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                cleanup_status=command(
                    '{"status":"healthy","action":"apply","generated_at_utc":"2026-07-17T02:00:00Z"}\n'
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["cleanup_last_run"]["status"], "healthy")
        self.assertIn("last_action=apply", by_name["cleanup_last_run"]["summary"])

    def test_recovery_status_is_exposed_when_present(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                recovery_status=command(
                    '{"status":"pass","action":"refresh-metrics","mode":"apply","finished_at_utc":"2026-07-17T02:30:00Z"}\n'
                )
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["recovery_last_run"]["status"], "healthy")
        self.assertIn("last_action=refresh-metrics", by_name["recovery_last_run"]["summary"])

    def test_backup_status_checks_are_distinct(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                backup_tools=command("restic=present\n"),
                backup_status=command(
                    "{"
                    '"backup":{"status":"healthy","freshness_status":"healthy","snapshot_id":"abc","quota":{"status":"warning"}},'
                    '"verification":{"status":"healthy","snapshot_id":"abc"},'
                    '"restore_drill":{"status":"critical","snapshot_id":"abc"},'
                    '"rabbitmq_recovery":{'
                    '"definition_export":{"status":"healthy","finished_at_utc":"2026-07-17T00:16:22Z"},'
                    '"clean_rebuild_drill":{"status":"healthy","finished_at_utc":"2026-07-17T00:20:22Z"},'
                    '"stopped_volume_restore_drill":{"status":"warning","finished_at_utc":"2026-07-17T00:30:22Z"}'
                    "}"
                    "}\n"
                ),
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["backup_freshness"]["status"], "healthy")
        self.assertEqual(by_name["backup_verification"]["status"], "healthy")
        self.assertEqual(by_name["backup_restore_drill"]["status"], "critical")
        self.assertEqual(by_name["backup_storage_quota"]["status"], "warning")
        self.assertEqual(by_name["rabbitmq_definition_export"]["status"], "healthy")
        self.assertEqual(by_name["rabbitmq_clean_rebuild_drill"]["status"], "healthy")
        self.assertEqual(by_name["rabbitmq_stopped_volume_restore_drill"]["status"], "warning")

    def test_rabbitmq_drift_and_smoke_are_exposed_when_present(self):
        checks, _ = backend_health_report.classify(
            fixture_report(
                rabbitmq_drift=command(
                    '{"status":"fail","summary":{"high_priority_unexpected":["rabbitmq_config_checksum:compose"]}}\n'
                ),
                rabbitmq_smoke_status=command(
                    '{"status":"pass","finished_at_utc":"2026-07-23T13:00:00Z","checks":[]}\n'
                ),
            )
        )
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["rabbitmq_drift"]["status"], "critical")
        self.assertIn("rabbitmq_config_checksum:compose", by_name["rabbitmq_drift"]["summary"])
        self.assertEqual(by_name["rabbitmq_smoke_last_run"]["status"], "healthy")
        self.assertIn("2026-07-23T13:00:00Z", by_name["rabbitmq_smoke_last_run"]["summary"])

    def test_active_caddy_with_failed_endpoint_is_critical(self):
        checks, summary = backend_health_report.classify(fixture_report(backend_health=command("")))
        by_name = {item["name"]: item for item in checks}
        self.assertEqual(by_name["backend_endpoint_health"]["status"], "critical")
        self.assertGreaterEqual(summary["critical"], 1)

    def test_critical_report_preserves_prior_success_and_is_not_successful(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            key_path = root / "key"
            known_hosts_path = root / "known_hosts"
            previous_path = root / "previous.json"
            key_path.touch()
            known_hosts_path.touch()
            previous_path.write_text(
                json.dumps({"last_report_success_at": "2026-07-15T12:00:00Z"}),
                encoding="utf-8",
            )
            args = backend_health_report.parse_args(
                [
                    "--ssh-key",
                    str(key_path),
                    "--known-hosts",
                    str(known_hosts_path),
                    "--previous-state",
                    str(previous_path),
                    "--output",
                    str(root / "report.json"),
                ]
            )
            collected = fixture_report(backend_health=command(""))["ssh"]
            with (
                patch.object(backend_health_report, "utc_now", return_value="2026-07-16T12:00:00Z"),
                patch.object(backend_health_report, "iso_after", return_value="2026-07-17T12:00:00Z"),
                patch.object(backend_health_report, "collect_ssh", return_value=collected),
            ):
                report = backend_health_report.build_report(args)

        self.assertGreater(report["summary"]["critical"], 0)
        self.assertEqual(report["conclusion"], "failure")
        self.assertEqual(report["last_report_success_at"], "2026-07-15T12:00:00Z")
        self.assertFalse(backend_health_report.report_succeeded(report))

    def test_main_writes_report_and_summary_before_critical_exit(self):
        report = fixture_report(backend_health=command(""))
        report["checks"], report["summary"] = backend_health_report.classify(report)
        report.update(
            {
                "last_report_success_at": "2026-07-15T12:00:00Z",
                "last_error": None,
                "conclusion": "failure",
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"
            summary_path = Path(tmpdir) / "summary.md"
            with (
                patch.object(backend_health_report, "build_report", return_value=report),
                patch("builtins.print"),
            ):
                exit_code = backend_health_report.main(
                    ["--output", str(output_path), "--summary", str(summary_path)]
                )

            persisted = json.loads(output_path.read_text(encoding="utf-8"))
            summary_text = summary_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertGreater(persisted["summary"]["critical"], 0)
        self.assertIn("# Backend Health Report", summary_text)
        self.assertIn("Conclusion: `failure`", summary_text)
        self.assertIn("`critical`", summary_text)

    def test_missing_smtp_degrades_without_failure(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("NUTSNEWS_REPORT_")}
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, env, clear=True):
            args = backend_health_report.parse_args(
                [
                    "--ssh-key",
                    str(Path(tmpdir) / "missing-key"),
                    "--known-hosts",
                    str(Path(tmpdir) / "missing-known-hosts"),
                    "--output",
                    str(Path(tmpdir) / "report.json"),
                    "--send-email",
                ]
            )
            report = backend_health_report.build_report(args)
        self.assertEqual(report["delivery"]["status"], "not_configured")
        self.assertIn("NUTSNEWS_REPORT_SMTP_HOST", report["delivery"]["detail"])

    def test_remote_commands_are_fixed_and_do_not_include_shell_input_placeholder(self):
        self.assertIn("failed_units", backend_health_report.REMOTE_COMMANDS)
        for command_text in backend_health_report.REMOTE_COMMANDS.values():
            self.assertNotIn("{command", command_text)
            self.assertNotIn("$INPUT", command_text)

    def test_render_text_contains_status_counts(self):
        report = fixture_report()
        checks, summary = backend_health_report.classify(report)
        report["checks"] = checks
        report["summary"] = summary
        text = backend_health_report.render_text(report)
        self.assertIn("NutsNews backend health report", text)
        self.assertIn("- warning:", text)
        self.assertIn("package_updates", text)

    def test_current_alerts_preserve_distinct_failure_classes(self):
        alerts = backend_health_report.current_alerts_from_checks(
            [
                {"name": "backup_freshness", "status": "critical", "summary": "snapshot=abc"},
                {"name": "backup_verification", "status": "warning", "summary": "snapshot=abc"},
                {"name": "backup_storage_quota", "status": "warning", "summary": "quota_status=warning"},
                {"name": "root_disk_used_percent", "status": "warning", "summary": "root_disk_used_percent=82%"},
                {"name": "service_fail2ban", "status": "warning", "summary": "fail2ban=inactive"},
                {"name": "memory_used_percent", "status": "unknown", "summary": "memory_used_percent=unknown"},
                {"name": "backup_tooling", "status": "not_configured", "summary": "restic=missing"},
            ]
        )
        by_service = {alert["service"]: alert for alert in alerts}
        self.assertEqual(by_service["backup_freshness"]["failure_class"], "backup_freshness")
        self.assertEqual(by_service["backup_verification"]["failure_class"], "backup_verification")
        self.assertEqual(by_service["backup_storage_quota"]["failure_class"], "backup_storage_quota")
        self.assertEqual(by_service["root_disk_used_percent"]["failure_class"], "disk_pressure")
        self.assertEqual(by_service["service_fail2ban"]["failure_class"], "service_down")
        self.assertEqual(by_service["memory_used_percent"]["failure_class"], "missing_data")
        self.assertNotIn("backup_tooling", by_service)

    def test_render_text_exposes_alerting_status(self):
        report = fixture_report()
        checks, summary = backend_health_report.classify(report)
        report["checks"] = checks
        report["summary"] = summary
        report["alerting"] = {
            "summary": {
                "active_alert_count": 2,
                "notification_count": 1,
                "suppressed_count": 1,
                "recovered_count": 0,
                "last_sent_at": "2026-07-17T01:00:00Z",
                "last_error": "fail2ban=inactive",
            }
        }
        text = backend_health_report.render_text(report)
        self.assertIn("Alerting:", text)
        self.assertIn("active_alert_count: 2", text)
        self.assertIn("last_error: fail2ban=inactive", text)


if __name__ == "__main__":
    unittest.main()
