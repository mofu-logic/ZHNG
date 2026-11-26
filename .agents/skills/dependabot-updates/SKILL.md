---
name: dependabot-updates
description: Triage and merge Dependabot PRs (pip and Docker) one at a time with deploy verification, including the PostgreSQL major-upgrade migration procedure. Use when Dependabot PRs are open.
---

# Handle Dependabot updates

## Order and pace

1. Merge **one PR at a time**: lowest risk first (patch/minor pip
   bumps), larger version jumps later, database image last.
2. After each merge: pull on the deployment host, rebuild
   (`docker compose up -d --build web`), and verify before moving on:
   `/healthz` returns `{"ok": true}`, and `/`, `/summary`, `/history`,
   `/master`, `/settings`, `/data` all return 200.
3. If a PR conflicts because sibling PRs touched adjacent lines of
   `requirements.txt`, merge `main` into the PR branch, resolve to
   the union of the bumps, push, and merge once GitHub reports it
   mergeable (its mergeability flag can lag a few seconds behind a
   push — re-check before concluding it is stuck).

## PostgreSQL major upgrades (e.g. 16 → 18)

An image bump alone is **not** enough:

- Since PostgreSQL 18 the image's data directory layout changed:
  the volume mounts at `/var/lib/postgresql` and the cluster lives
  in a versioned subdirectory. The compose change (image **and**
  volume mount) must land together.
- Migrate by dump/restore, keeping two independent backups until the
  new version has proven itself (e.g. one nightly batch completed):
  1. Record row counts of all tables.
  2. `pg_dump` the database to a file outside the repo, and copy the
     old volume to a backup volume.
  3. Stop the stack, remove the old volume, start `db` on the new
     image so it initialises a fresh cluster, restore the dump.
  4. Verify row counts match exactly and all pages return 200.
- Never delete the backups in the same session as the migration.
