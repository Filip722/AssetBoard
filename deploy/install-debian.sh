#!/usr/bin/env bash
# Install or upgrade AssetBoard natively on Debian/Ubuntu with systemd.
# Docs: docs/install/native.md. Run as root; safe to run again (it is idempotent).
#
#   install-debian.sh                    install or upgrade to the latest release
#   install-debian.sh --version 0.3.0    install or upgrade to a given release
#
# Each release lives in its own virtual environment under $prefix/releases/<version>;
# $prefix/current points at the active one, so an upgrade is a symlink swap and a rollback
# is pointing the link back (after restoring the backup taken before the upgrade).
set -euo pipefail

# Branding: the only place this script names the app (see AGENTS.md "Branding").
app=assetboard
repo_url=https://github.com/LexanderPL/AssetBoard

version=latest
release_url=
python_bin=python3
root=
start=1

usage() {
  cat <<EOF
Usage: $0 [--version X.Y.Z|latest] [--python PATH] [--release-url URL] [--root DIR] [--no-start]

  --version      Release to install (default: latest).
  --python       Python 3.13+ interpreter used for the virtual environment (default: python3).
  --release-url  Directory holding the release files, instead of the GitHub release
                 (https:// or file://, for mirrors and testing).
  --root DIR     Install under DIR without creating users or touching systemd (for testing).
  --no-start     Install and enable the service but do not (re)start it.
EOF
}

while (($#)); do
  case $1 in
    --version) version=${2:?--version needs a value}; shift 2 ;;
    --python) python_bin=${2:?--python needs a value}; shift 2 ;;
    --release-url) release_url=${2:?--release-url needs a value}; shift 2 ;;
    --root) root=${2:?--root needs a value}; shift 2 ;;
    --no-start) start=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

prefix=$root/opt/$app
config_dir=$root/etc/$app
env_file=$config_dir/$app.env
data_dir=$root/var/lib/$app
backup_dir=$root/var/backups/$app
unit_path=$root/etc/systemd/system/$app.service

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Runs a command as the service user (as the current user with --root).
as_app() {
  if [[ -n $root ]]; then "$@"; else runuser -u "$app" -- "$@"; fi
}

if [[ -z $root && $EUID -ne 0 ]]; then
  die "run as root (or with sudo)"
fi

step "Checking prerequisites"
if [[ -z $root ]] && command -v apt-get >/dev/null; then
  missing=()
  for pkg in python3 python3-venv curl ca-certificates; do
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'ok installed' || missing+=("$pkg")
  done
  if ((${#missing[@]})); then
    echo "Installing ${missing[*]}"
    apt-get update
    apt-get install --no-install-recommends -y "${missing[@]}"
  fi
fi
command -v curl >/dev/null || die "curl is required"
command -v sha256sum >/dev/null || die "sha256sum is required"
command -v "$python_bin" >/dev/null || die "Python not found: $python_bin"
if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 13))'; then
  die "$python_bin is $("$python_bin" -V 2>&1); Python 3.13 or later is required.
See docs/install/native.md (older distributions) for installing it with uv."
fi
echo "Using $("$python_bin" -V) at $(command -v "$python_bin")"

if [[ $version == latest ]]; then
  [[ -z $release_url ]] || die "--release-url needs an explicit --version"
  step "Finding the latest release"
  latest=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$repo_url/releases/latest")
  version=${latest##*/v}
  [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]] || die "could not determine the latest version"
fi
version=${version#v}
echo "Version: $version"
release_url=${release_url:-$repo_url/releases/download/v$version}

wheel=$app-$version-py3-none-any.whl
requirements=$app-$version-requirements.txt
files=("$wheel" "$requirements" "$app.service" "$app.env.example")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

step "Downloading release files from $release_url"
for file in SHA256SUMS "${files[@]}"; do
  echo "  $file"
  curl -fsSL --proto '=https,file' -o "$work/$file" "$release_url/$file"
done

step "Verifying checksums"
for file in "${files[@]}"; do
  grep -E "^[0-9a-f]{64}  $file\$" "$work/SHA256SUMS" >>"$work/expected" ||
    die "$file is not listed in SHA256SUMS"
done
(cd "$work" && sha256sum --check --strict "$work/expected")

if [[ -z $root ]] && ! id -u "$app" >/dev/null 2>&1; then
  step "Creating system user $app"
  useradd --system --user-group --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$app"
fi

release_dir=$prefix/releases/$version
if [[ -f $release_dir/.installed ]]; then
  step "Release $version is already installed in $release_dir"
else
  step "Creating the virtual environment in $release_dir"
  # A directory without the .installed marker is a failed earlier attempt: start again.
  rm -rf "$release_dir"
  mkdir -p "$prefix/releases"
  "$python_bin" -m venv "$release_dir"
  pip=("$release_dir/bin/python" -m pip install --quiet --disable-pip-version-check)
  # Exact dependency versions and hashes, as locked for this release.
  "${pip[@]}" --require-hashes --no-deps -r "$work/$requirements"
  "${pip[@]}" --no-deps "$work/$wheel"
  "$release_dir/bin/$app" --help >/dev/null || die "the installed release does not start"
  touch "$release_dir/.installed"
fi

step "Writing configuration"
mkdir -p "$config_dir"
if [[ -e $env_file ]]; then
  echo "Keeping existing $env_file"
else
  install -m 0640 "$work/$app.env.example" "$env_file"
  echo "Created $env_file: set ASSETBOARD_BASE_URL and ASSETBOARD_TRUSTED_PROXIES there"
fi
mkdir -p "$data_dir" "$backup_dir"
chmod 0700 "$data_dir" "$backup_dir"
if [[ -z $root ]]; then
  chown "root:$app" "$env_file"
  chown "$app:$app" "$data_dir" "$backup_dir"
fi

database=$data_dir/$app.db
if [[ -f $database ]]; then
  backup=$backup_dir/$app-$(date -u +%Y%m%dT%H%M%SZ)-before-$version.db
  step "Backing up the database to $backup"
  # The SQLite backup API gives a consistent copy while the app is running (WAL mode).
  # It runs as the service user so that SQLite never leaves root-owned -wal/-shm files behind.
  as_app "$release_dir/bin/python" -c '
import sqlite3, sys
source = sqlite3.connect(sys.argv[1])
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
target.close()
source.close()
' "$database" "$backup"
  if [[ -f $data_dir/secret_key ]]; then
    as_app cp -p "$data_dir/secret_key" "$backup.secret_key"
  fi
  echo "Copy $backup_dir to another machine as part of your regular backups."
fi

step "Activating release $version"
previous=$(readlink "$prefix/current" 2>/dev/null || true)
ln -sfn "$release_dir" "$prefix/current.new"
mv -T "$prefix/current.new" "$prefix/current"
[[ -z $previous || $previous == "$release_dir" ]] || echo "Previous release: $previous"

step "Installing the systemd unit $unit_path"
mkdir -p "$(dirname "$unit_path")"
install -m 0644 "$work/$app.service" "$unit_path"
if [[ -n $root ]]; then
  echo "Skipping systemctl (--root)"
else
  systemctl daemon-reload
  systemctl enable "$app.service"
  if ((start)); then
    systemctl restart "$app.service"
    step "Waiting for the app to become ready"
    for _ in $(seq 30); do
      if curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1; then
        echo "Ready on http://127.0.0.1:8000"
        break
      fi
      sleep 1
    done
    curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1 ||
      echo "Not ready yet; check: journalctl -u $app -n 50"
  fi
fi

step "Done"
cat <<EOF
Release $version is active ($prefix/current).
- Settings: $env_file (then: systemctl restart $app)
- First run: open the app and use the setup token from: journalctl -u $app | grep "setup token"
- Logs: journalctl -u $app -f
Old releases stay in $prefix/releases; remove the ones you no longer need.
EOF
