# Isolated recovery of inactive workers

The fixed Observe exporter includes scheduler and translation inputs. `docker cp CONTAINER:/app/. -` reads deployed files from running or stopped containers without executing a process inside them. It never starts, restarts or changes the live worker. Configuration and database exports continue through the restricted recovery account into encrypted archives.

The local recovery service restores actual PostgreSQL state and broker topology in a disposable private network with two CPUs and eight GiB. Scheduler verification requires backed-up leases, dependency readiness, new isolated leases and messages from restored feed data. Translation requires restored completed-message replay and an isolated Qwen inference. Failed restoration stays failed; no readiness or production-performance credit follows from this drill.

Apply only the protected Observe recovery configuration workflow, first with apply=false. The unrelated full baseline must not run for this exporter-only release because adopted application configuration has drift. Rollback the exporter revision through this same scoped workflow. Existing exports remain supported.
