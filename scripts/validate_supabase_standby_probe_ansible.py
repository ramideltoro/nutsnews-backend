#!/usr/bin/env python3
"""Validate Ansible assets for the Supabase standby forced-command probe."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "ansible" / "roles" / "supabase_standby_probe"
DEFAULTS = ROLE / "defaults" / "main.yml"
TASKS = ROLE / "tasks" / "main.yml"
HANDLERS = ROLE / "handlers" / "main.yml"
SSHD_TEMPLATE = ROLE / "templates" / "standby-probe-sshd.conf.j2"
CONFIG_TEMPLATE = ROLE / "templates" / "probe.conf.j2"
PLAYBOOK = ROOT / "ansible" / "playbooks" / "supabase_standby_probe.yml"
ANSIBLE_README = ROOT / "ansible" / "README.md"
RUNBOOK = ROOT / "runbooks" / "SUPABASE_STANDBY_PROBE_BOUNDARY.md"
BOUNDARY = ROOT / "docs" / "supabase-standby-probe-boundary.json"
BACKEND_CHECKS = ROOT / ".github" / "workflows" / "backend-checks.yml"


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"missing required file: {path}") from None


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def main() -> int:
    errors: list[str] = []

    defaults = read(DEFAULTS)
    tasks = read(TASKS)
    handlers = read(HANDLERS)
    sshd_template = read(SSHD_TEMPLATE)
    config_template = read(CONFIG_TEMPLATE)
    playbook = read(PLAYBOOK)
    ansible_readme = read(ANSIBLE_README)
    runbook = read(RUNBOOK)
    boundary = read(BOUNDARY)
    backend_checks = read(BACKEND_CHECKS)

    for required in [
        "supabase_standby_probe_state: present",
        "supabase_standby_probe_user: nutsnews-standby-probe",
        "supabase_standby_probe_group: nutsnews-standby-probe",
        "supabase_standby_probe_home: /var/lib/nutsnews-standby-probe",
        "supabase_standby_probe_command_path: /usr/local/libexec/nutsnews-standby-supabase-probe",
        "supabase_standby_probe_config_path: /etc/nutsnews-standby-probe/probe.conf",
        "supabase_standby_probe_sshd_dropin: /etc/ssh/sshd_config.d/60-nutsnews-standby-probe.conf",
        'supabase_standby_probe_public_key: ""',
        'supabase_standby_probe_expected_project_ref: ""',
        'supabase_standby_probe_expected_host: ""',
        "openssh-server",
        "postgresql-client",
    ]:
        require(required in defaults, f"Defaults missing {required}.", errors)

    for required in [
        "supabase_standby_probe_state in ['present', 'absent']",
        "supabase_standby_probe_public_key | trim | length > 0",
        "supabase_standby_probe_expected_project_ref is match('^[a-z0-9]{20}$')",
        "supabase_standby_probe_expected_host == 'db.' ~ supabase_standby_probe_expected_project_ref ~ '.supabase.co'",
        "no_log: true",
        "password_lock: true",
        'groups: ""',
        "append: false",
        "shell: /bin/sh",
        "owner: root",
        "group: root",
        'mode: "0755"',
        'command="{{ supabase_standby_probe_command_path }}",restrict',
        'mode: "0644"',
        'mode: "0640"',
        'src: "{{ supabase_standby_probe_source_path }}"',
        'dest: "{{ supabase_standby_probe_command_path }}"',
        "src: probe.conf.j2",
        "src: standby-probe-sshd.conf.j2",
        "/usr/sbin/sshd -t -f /etc/ssh/sshd_config",
        "not ansible_check_mode",
        "state: absent",
        "remove: true",
    ]:
        require(required in tasks, f"Tasks missing {required}.", errors)

    for forbidden in [
        "sudoers",
        "NOPASSWD",
        "PermitTTY yes",
        "AllowTcpForwarding yes",
        "AllowAgentForwarding yes",
        "X11Forwarding yes",
        "PermitUserEnvironment yes",
    ]:
        require(forbidden not in tasks, f"Tasks must not contain {forbidden}.", errors)

    for required in [
        "ForceCommand {{ supabase_standby_probe_command_path }}",
        "PermitTTY no",
        "AllowAgentForwarding no",
        "AllowTcpForwarding no",
        "AllowStreamLocalForwarding no",
        "X11Forwarding no",
        "PermitTunnel no",
        "PermitUserEnvironment no",
        "GatewayPorts no",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
    ]:
        require(required in sshd_template, f"sshd Match User template missing {required}.", errors)

    require(
        "expected_project_ref={{ supabase_standby_probe_expected_project_ref }}" in config_template,
        "Config template must write expected project ref.",
        errors,
    )
    require(
        "expected_host={{ supabase_standby_probe_expected_host }}" in config_template,
        "Config template must write expected host.",
        errors,
    )

    for required in [
        "name: Supabase standby forced-command probe",
        "hosts: backend",
        "become: true",
        "name: supabase_standby_probe",
        "'supabase_standby_ipv6_runner' not in group_names",
    ]:
        require(required in playbook, f"Playbook missing {required}.", errors)

    require("when: not ansible_check_mode" in handlers, "Handlers must not reload services in check mode.", errors)
    require("supabase_standby_probe.yml" in ansible_readme, "Ansible README must document the probe playbook.", errors)
    require(
        "ansible/roles/supabase_standby_probe" in runbook,
        "Probe runbook must document the Ansible role.",
        errors,
    )
    require(
        "ansible/playbooks/supabase_standby_probe.yml" in boundary,
        "Boundary must record the Ansible playbook.",
        errors,
    )
    require(
        "ansible/roles/supabase_standby_probe" in boundary,
        "Boundary must record the Ansible role.",
        errors,
    )
    require(
        "python3 scripts/validate_supabase_standby_probe_ansible.py" in backend_checks,
        "Backend checks must run the probe Ansible validator.",
        errors,
    )
    require(
        "ansible-playbook playbooks/supabase_standby_probe.yml --syntax-check" in backend_checks,
        "Backend checks must syntax-check the probe playbook.",
        errors,
    )

    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Supabase standby probe Ansible guardrails passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
