const statusRank = {
  critical: 4,
  warning: 3,
  unknown: 2,
  not_configured: 1,
  healthy: 0,
};

const statusClass = (value) => String(value || "unknown").replaceAll("-", "_");

function setText(id, value) {
  document.getElementById(id).textContent = value;
}

function percent(value) {
  return typeof value === "number" ? `${value}%` : "--";
}

function setBar(id, resource) {
  const bar = document.getElementById(id);
  const value = typeof resource?.used_percent === "number" ? resource.used_percent : 0;
  bar.style.width = `${Math.max(0, Math.min(100, value))}%`;
  bar.style.background = `var(--${statusClass(resource?.status)})`;
}

function pill(status, text = status) {
  const span = document.createElement("span");
  span.className = `pill ${statusClass(status)}`;
  span.textContent = text || "unknown";
  return span;
}

function renderDefinitionList(id, rows) {
  const list = document.getElementById(id);
  list.replaceChildren();
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value ?? "unknown";
    list.append(term, detail);
  }
}

function renderServices(services) {
  const target = document.getElementById("services");
  target.replaceChildren();
  for (const service of services || []) {
    const row = document.createElement("div");
    row.className = "status-row";
    const name = document.createElement("strong");
    name.textContent = service.name;
    row.append(name, pill(service.status, service.state));
    target.append(row);
  }
}

function renderNetwork(network) {
  const target = document.getElementById("network");
  target.replaceChildren();
  const listeners = network?.public_tcp_listeners || [];
  if (!listeners.length) {
    target.append(pill("unknown", "no public TCP listeners"));
    return;
  }
  for (const item of listeners) {
    const row = document.createElement("div");
    row.className = "status-row";
    const label = document.createElement("strong");
    label.textContent = `${item.address}:${item.port}`;
    row.append(label, pill((network.expected_public_tcp_ports || []).includes(item.port) ? "healthy" : "warning", "tcp"));
    target.append(row);
  }
}

function backupStatus(section) {
  return section?.freshness_status || section?.status || "not_configured";
}

function renderBackup(backup) {
  const target = document.getElementById("backup");
  target.replaceChildren();
  const rows = [
    ["Freshness", backup?.backup, backupStatus(backup?.backup)],
    ["Verification", backup?.verification, backupStatus(backup?.verification)],
    ["Restore Drill", backup?.restore_drill, backupStatus(backup?.restore_drill)],
    ["Quota", backup?.backup?.quota, backup?.backup?.quota?.status || "not_configured"],
  ];
  for (const [labelText, section, status] of rows) {
    const row = document.createElement("div");
    row.className = "status-row";
    const label = document.createElement("strong");
    label.textContent = labelText;
    const snapshot = section?.snapshot_id ? ` ${section.snapshot_id}` : "";
    row.append(label, pill(status, `${status}${snapshot}`));
    target.append(row);
  }
}

function renderPostgres(postgres) {
  const target = document.getElementById("postgres");
  target.replaceChildren();
  const restoreStatus = postgres?.last_restore_drill?.status || postgres?.status || "not_configured";
  const replication = postgres?.replication || {};
  const replicationDetail = [
    replication.mode || "not_configured",
    replication.max_lag_seconds !== undefined && replication.max_lag_seconds !== null ? `${replication.max_lag_seconds}s lag` : null,
    replication.slot_status ? `slot ${replication.slot_status}` : null,
  ].filter(Boolean).join(" | ");
  const rows = [
    ["Target", postgres?.database || "not_configured", postgres?.status || "not_configured"],
    ["Restore Drill", postgres?.last_restore_drill?.completed_at_utc || "not run", restoreStatus],
    ["Replication", replicationDetail || "not_configured", replication.dashboard_status || replication.lag_status || "not_configured"],
    ["Dashboard", postgres?.dashboard?.access_boundary || "not_configured", postgres?.dashboard ? "healthy" : "not_configured"],
  ];
  for (const [labelText, detailText, status] of rows) {
    const row = document.createElement("div");
    row.className = "status-row";
    const label = document.createElement("strong");
    label.textContent = labelText;
    row.append(label, pill(status, String(detailText || status)));
    target.append(row);
  }
}

function overallStatus(data) {
  const statuses = [
    data.endpoint?.status,
    data.resources?.memory?.status,
    data.resources?.swap?.status,
    data.resources?.root_disk?.status,
    data.resources?.root_inodes?.status,
    data.systemd?.failed_units_status,
    backupStatus(data.backup?.backup),
    backupStatus(data.backup?.verification),
    backupStatus(data.backup?.restore_drill),
    data.postgres?.last_restore_drill?.status || data.postgres?.status,
    data.postgres?.replication?.dashboard_status || data.postgres?.replication?.lag_status,
    ...(data.services || []).map((service) => service.status),
  ];
  return statuses.sort((a, b) => (statusRank[b] ?? 2) - (statusRank[a] ?? 2))[0] || "unknown";
}

function render(data) {
  const host = data.host || {};
  const resources = data.resources || {};
  const overall = overallStatus(data);
  const overallElement = document.getElementById("overall");
  overallElement.className = `overall ${statusClass(overall)}`;
  overallElement.textContent = overall.replace("_", " ");
  setText("subtitle", `${host.hostname || "backend"} snapshot generated ${data.generated_at || "unknown"}`);

  setText("memory-value", percent(resources.memory?.used_percent));
  setText("swap-value", percent(resources.swap?.used_percent));
  setText("disk-value", percent(resources.root_disk?.used_percent));
  setText("inode-value", percent(resources.root_inodes?.used_percent));
  setBar("memory-bar", resources.memory);
  setBar("swap-bar", resources.swap);
  setBar("disk-bar", resources.root_disk);
  setBar("inode-bar", resources.root_inodes);

  renderDefinitionList("host-list", [
    ["OS", host.os],
    ["Kernel", host.kernel],
    ["Latest Installed Kernel", host.latest_installed_kernel],
    ["Boot ID", host.boot_id],
    ["Uptime", host.uptime],
    ["Boot Time", host.boot_time],
    ["CPU Count", host.cpu_count],
    ["Load Average", (host.load_average || []).join(" ")],
    ["Pending Reboot", String(Boolean(host.reboot_required))],
    ["Upgradable Packages", host.upgradable_packages],
    ["Access Boundary", data.access_boundary],
  ]);

  const endpoint = document.getElementById("endpoint");
  endpoint.replaceChildren();
  endpoint.append(pill(data.endpoint?.status, data.endpoint?.response));
  const endpointUrl = document.createElement("p");
  endpointUrl.textContent = data.endpoint?.url || "unknown";
  endpoint.append(endpointUrl);

  renderServices(data.services);
  renderNetwork(data.network);
  renderBackup(data.backup);
  renderPostgres(data.postgres);
  setText("timers", (data.systemd?.timers || []).join("\n") || "No relevant timers reported.");
}

async function load() {
  const response = await fetch("status.json", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`status fetch failed: ${response.status}`);
  }
  render(await response.json());
}

load().catch((error) => {
  setText("subtitle", error.message);
  const overall = document.getElementById("overall");
  overall.className = "overall critical";
  overall.textContent = "critical";
});
