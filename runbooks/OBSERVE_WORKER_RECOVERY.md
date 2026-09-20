# Isolated Observe worker recovery exports

The restricted Observe recovery identity can read deployed application files and effective environment for six existing worker containers: fetcher, canonicalizer, enrichment, approval, persistence and publication. The revision hashes the running image, effective environment and broker topology configuration. It cannot name another container or execute a caller-supplied command.

Additional fixed exports provide a consistent worker database dump, role definitions without passwords, RabbitMQ runtime filesystem, existing sanitized broker-definition backup (at most eight days old), and read-only queue counts. No queue messages are consumed and no worker is activated or restarted. A nonempty production queue must block a drill that lacks a complete message backup.

All exports go to the existing restricted recovery controller. Detailed archives and configuration remain private and encrypted. Restore targets must be disposable, network isolated and capped by the existing 2 CPU / 8 GiB service. Recovery credit requires successful restoration and application verification; export success alone earns no restore credit.

Deploy only through the existing Observe recovery configuration workflow, first with apply=false, then apply=true after checks. This changes the fixed helper only; generic full-host reconciliation is unnecessary. Roll back by deploying the previous helper through the same workflow. Shared documentation accompanies the change in nutsnews-docs.
