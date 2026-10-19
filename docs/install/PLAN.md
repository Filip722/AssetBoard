# Install methods beyond Docker: parked plan

Status: approved, parked until just before the 1.0 release. Branch `claude/docs-install-methods`.

## Goal
Run the app easily without Docker (native systemd, Proxmox LXC, Podman) and document each way.
Docker stays the recommended way: the README should say "recommended", not "required".

## Decisions
- P1, support tiers: Docker and native systemd are supported. Proxmox LXC is documented and runs
  the native install in an unprivileged Debian 13 CT. Podman (rootless Quadlet) is documented but
  untested. Creating an LXC from an OCI image (a Proxmox VE 9.1 tech preview) is marked
  experimental and untested. Docker in a VM, or in an LXC with nesting, is the alternative.
  Kubernetes, Helm, Nix and NAS app stores come after 1.0.
- P2: ship `deploy/install-debian.sh`. Requirements:
  - idempotent, `set -euo pipefail`, shellcheck clean, prints each step;
  - downloads the release wheel, `requirements.txt` and `SHA256SUMS`, and verifies them;
  - `--version` flag;
  - upgrades into a new venv, then swaps a symlink;
  - backs up first with the SQLite backup API, run as the service user.
- P3: Debian 13 / Ubuntu 24.04+ (Python 3.13) are tested. Older distros use uv-managed Python,
  installed outside /root (for example `UV_PYTHON_INSTALL_DIR=/opt/uv-python`), because the unit
  sets ProtectHome.
- T1, branding: the script keeps the name in one variable at the top. Add the `deploy/`
  packaging files to the AGENTS.md "Branding" list.
- T2: add a small release job (needs `contents: write`) that builds the wheel and runs
  `scripts/verify_wheel.py`. It attaches to the GitHub release: the wheel,
  `<name>-<ver>-requirements.txt` (`uv export --frozen --no-default-groups --no-emit-project`,
  with hashes), the unit, the env example, the install script and `SHA256SUMS`.
  No PyPI: it is neither a standard nor a necessity for these methods.
- T3: run shellcheck on the script in CI. No `systemd-analyze` in CI.
- T4: `*_FILE` variables and systemd credentials for secrets are a follow-up.
- Leave `MemoryDenyWriteExecute` off, with a comment in the unit (cffi/argon2 risk, untested).
- Docs say plainly what was tested here (native install) and what was not (Proxmox, Podman).

## Done so far (WIP)
- `deploy/systemd/assetboard.service`: hardened unit with StateDirectory 0700 and UMask 0077, bound
  to loopback.
- `deploy/assetboard.env.example` and `deploy/podman/assetboard.container` (Quadlet). The image
  version is kept current by release-please through `release-please-config.json`.
- `deploy/install-debian.sh`: shellcheck clean. A dry run with `--root` against a local `file://`
  release dir installed correctly.

## Remaining
1. Release job in `.github/workflows/release-please.yml`. Add shellcheck of
   `deploy/install-debian.sh` to `ci.yml`.
2. Docs: `docs/install/{README,docker,native,proxmox-lxc,podman}.md`; README quick start
   ("Docker recommended", link to the install docs); `docs/deployment.md` (admin commands outside
   containers via `systemd-run -p User=... -p EnvironmentFile=...`; a shared Backup section:
   SQLite backup API or stop-and-copy, include `secret_key`, vzdump in stop mode); the AGENTS.md
   branding line.
3. Verification, then `make check`. Remove this file when done.

## Verification steps
- Build the wheel, export requirements, install into a fresh venv outside the repo with
  `pip install --require-hashes --no-deps -r requirements.txt` plus `pip install --no-deps <wheel>`.
- Run `assetboard serve` with a temp data dir, then curl `/healthz`, `/readyz`, `/setup` and
  `/static/css/app.css`. Already proven once with `uv pip install <wheel>`: it migrated from an
  empty database, the setup page returned 200, and Polish worked.
- Run `systemd-analyze verify` on the shipped unit.
- Dry-run the script with `--root` against a `file://` release dir (done once). Run it again to
  check idempotence, and run an upgrade with an existing database to test the backup step.
