# Observe metrics freshness

The textfile collector previously ran every five minutes, exceeding Observe's existing 180-second warning threshold between otherwise successful collections. A September 20 read-only sample measured four seconds elapsed, 1.73 CPU seconds and 35 MB peak memory. Collect every minute; retain scoring thresholds and collector logic.

Run the protected Observe metrics cadence workflow with apply=false, then apply=true after preflight passes. It installs one timer drop-in and restarts only that timer. No application, database, container, or shadow worker is restarted. Full backend baseline application is unnecessary. The Ansible default matches the new cadence.

Verify repeated fresh timestamps in the exported metrics and healthy public endpoints, and compare application PIDs and container identities. Roll back through the same protected deployment identity with scripts/apply-observe-metrics-cadence.sh rollback; remove only this drop-in. If the Ansible baseline has subsequently been applied, revert its cadence too through a reviewed change.
