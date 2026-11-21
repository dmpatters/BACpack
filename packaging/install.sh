#!/usr/bin/env bash
# Installs, upgrades or removes BACpack as a systemd service.
#
#   sudo ./packaging/install.sh [--python PATH] [--port N] [--login-user NAME | --no-login]
#   sudo ./packaging/install.sh --uninstall [--purge]
#
# --login-user NAME creates the web login: the password comes from
# BACPACK_LOGIN_PASSWORD (keep it through sudo with --preserve-env), else it is
# asked twice; never a command-line argument. --no-login records "run without
# login". With neither, a new install asks in the browser and an upgrade keeps
# its current setting.
#
# The web UI is HTTPS only, on --port (default 8000), with a self-signed
# certificate until you install your own (on the Web Server page).
#
# See the README's Linux section.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR=/opt/bacpack
VENV="$INSTALL_DIR/.venv"
DATA_DIR=/var/lib/bacpack
ENV_FILE=/etc/bacpack.env
UNIT=/etc/systemd/system/bacpack.service
SERVICE_USER=bacpack
BACNET_PORT=47808

PYTHON_ARG=""
PORT_ARG=""
UNINSTALL=0
PURGE=0
LOGIN_USER=""
NO_LOGIN=0
# /usr/sbin: RHEL-family sudo's secure_path leaves /usr/local/bin out.
ADMIN_BIN=/usr/sbin/bacpack-admin

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) PYTHON_ARG="${2:-}"; shift 2 ;;
    --port) PORT_ARG="${2:-}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    --login-user) LOGIN_USER="${2:-}"; [[ -n $LOGIN_USER ]] || die "--login-user needs a name"; shift 2 ;;
    --no-login) NO_LOGIN=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ -z $LOGIN_USER || $NO_LOGIN -eq 0 ]] || die "--login-user and --no-login can't be used together"
[[ $EUID -eq 0 ]] || die "run as root: sudo $0 $*"
[[ -d /run/systemd/system ]] && command -v systemctl >/dev/null || die "systemd is required"

env_value() {  # env_value KEY -> value from $ENV_FILE, or empty
  [[ -f $ENV_FILE ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 || true
}

# --- firewall (only rules this script added are ever removed) -------------
firewall_kind() {
  if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then echo firewalld
  elif command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then echo ufw
  fi
}

firewall_add() {  # firewall_add KIND PORT/PROTO -> prints the rule if this call added it
  local kind=$1 rule=$2
  case $kind in
    firewalld)
      firewall-cmd --permanent --query-port="$rule" >/dev/null 2>&1 && return 0
      firewall-cmd --permanent --add-port="$rule" >/dev/null && echo "firewalld:$rule" ;;
    ufw)
      ufw status | grep -qw "$rule" && return 0
      ufw allow "$rule" >/dev/null && echo "ufw:$rule" ;;
  esac
}

firewall_remove_recorded() {
  local entry kind rule reload=0
  for entry in $(env_value BACPACK_FIREWALL); do
    kind=${entry%%:*}; rule=${entry#*:}
    case $kind in
      firewalld) firewall-cmd --permanent --remove-port="$rule" >/dev/null 2>&1 || true; reload=1 ;;
      ufw) ufw delete allow "$rule" >/dev/null 2>&1 || true ;;
    esac
  done
  [[ $reload -eq 1 ]] && firewall-cmd --reload >/dev/null 2>&1 || true
}

# --- uninstall ------------------------------------------------------------
if [[ $UNINSTALL -eq 1 ]]; then
  systemctl disable --now bacpack.service 2>/dev/null || true
  # KillMode=process: devices left by a crashed web process outlive the stop,
  # would keep serving from a deleted install and make userdel fail.
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    for signal in TERM KILL; do
      pkill "-$signal" -u "$SERVICE_USER" || true
      for _ in $(seq 20); do pgrep -u "$SERVICE_USER" >/dev/null || break; sleep 0.5; done
    done
  fi
  rm -f "$UNIT" "$ADMIN_BIN"
  systemctl daemon-reload
  firewall_remove_recorded
  rm -f "$ENV_FILE"
  rm -rf "$INSTALL_DIR"
  id -u "$SERVICE_USER" >/dev/null 2>&1 && userdel "$SERVICE_USER" || true
  if [[ $PURGE -eq 1 ]]; then
    rm -rf "$DATA_DIR"
    echo "BACpack removed, data deleted."
  else
    echo "BACpack removed. Data kept in $DATA_DIR (reinstall picks it up; --purge deletes it)."
  fi
  exit 0
fi

# --- preflight: nothing is changed before all of this passes ---------------
pick_python() {
  local candidate
  if [[ -n $PYTHON_ARG ]]; then echo "$PYTHON_ARG"; return; fi
  # An upgrade keeps the interpreter the install was made with (often a
  # --python one), while it still exists.
  if [[ -f $VENV/pyvenv.cfg ]]; then
    candidate=$(sed -n 's/^executable *= *//p' "$VENV/pyvenv.cfg")
    if [[ -x $candidate ]] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 13))' 2>/dev/null; then
      echo "$candidate"; return
    fi
  fi
  for candidate in python3.14 python3.13 python3; do
    if command -v "$candidate" >/dev/null &&
       "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 13))' 2>/dev/null; then
      command -v "$candidate"; return
    fi
  done
}

PY=$(pick_python)
[[ -n $PY ]] || die "BACpack needs Python 3.13 or newer, and none was found (python3.14, python3.13, python3).
Install one from your distribution (Debian 13, Ubuntu 26.04 and Fedora ship it; on RHEL 9 and its
rebuilds: dnf install python3.14), or install a standalone build (for example: uv python install 3.13) and pass it with --python /path/to/python3.13."
[[ -x $PY ]] || die "--python $PY is not an executable"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 13))' ||
  die "$PY is Python $("$PY" -c 'import platform; print(platform.python_version())'); BACpack needs 3.13 or newer."
"$PY" -c 'import venv, ensurepip' 2>/dev/null ||
  die "$PY has no venv/ensurepip module. On Debian/Ubuntu: apt install python3-venv (or python3.13-venv)."
"$PY" -c 'import sqlite3, sys; sys.exit(sqlite3.sqlite_version_info < (3, 34, 0))' ||
  die "$PY uses SQLite $("$PY" -c 'import sqlite3; print(sqlite3.sqlite_version)'); BACpack needs 3.34 or newer.
Use a Python that bundles a newer SQLite (for example: uv python install 3.13) and pass it with --python."

PORT=${PORT_ARG:-$(env_value BACPACK_PORT)}
PORT=${PORT:-8000}
[[ $PORT =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) || die "--port must be 1-65535, not '$PORT'"

# The service user may not bind a port below 1024 unless a drop-in grants it
# the capability (the installer doesn't add it): refuse now, before the
# running install is stopped and its settings rewritten for a port that can't
# start.
capability_granted() {  # capability_granted DIR PORT -> 0 if PORT is bindable by the service user
  # A host that lowered the unprivileged range needs no capability; systemd
  # reads the directive and the capability name in any case.
  local start
  start=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null) || start=1024
  [[ $start =~ ^[0-9]+$ ]] && (( start <= $2 )) && return 0
  compgen -G "$1/*.conf" >/dev/null &&
    grep -qsiE '^[[:space:]]*AmbientCapabilities=.*CAP_NET_BIND_SERVICE' "$1"/*.conf
}
if (( PORT < 1024 )) && ! capability_granted /etc/systemd/system/bacpack.service.d "$PORT"; then
  die "port $PORT is below 1024, which the bacpack service user may not bind.
Grant it first (this works before the service exists too):
  sudo mkdir -p /etc/systemd/system/bacpack.service.d
  printf '[Service]\nAmbientCapabilities=CAP_NET_BIND_SERVICE\n' | sudo tee /etc/systemd/system/bacpack.service.d/override.conf
then run this installer again with --port $PORT. Nothing has been changed."
fi

# The login password is settled here, before anything changes: from the
# environment (unattended), else asked twice. The admin command checks the
# rules again; this catches the common mistake early.
LOGIN_PASSWORD=""
if [[ -n $LOGIN_USER ]]; then
  LOGIN_PASSWORD=${BACPACK_LOGIN_PASSWORD:-}
  if [[ -z $LOGIN_PASSWORD ]]; then
    [[ -t 0 ]] || die "--login-user needs a password: set BACPACK_LOGIN_PASSWORD, or run this from a terminal"
    read -rsp "Password for the web login '$LOGIN_USER' (12 characters or more): " LOGIN_PASSWORD; echo
    read -rsp "Type it again: " LOGIN_AGAIN; echo
    [[ $LOGIN_PASSWORD == "$LOGIN_AGAIN" ]] || die "the two passwords differ"
  fi
  (( ${#LOGIN_PASSWORD} >= 12 )) || die "the web login password needs at least 12 characters"
fi

# --- stop an existing install (devices stop and are remembered) -----------
WAS_ACTIVE=0; RESTORE_ON_FAIL=0; STAGE=""
# BACPACK_STARTED=1 in the env file: the last run started the service and the
# start held. A unit that exists says only that an install began, not that it
# ever ran (a refused first start, a failed login step).
STARTED_BEFORE=0
[[ $(env_value BACPACK_STARTED) == 1 ]] && STARTED_BEFORE=1
# A failed upgrade (a pip or network error) mustn't leave a working install
# stopped: start it again, on the previous code.
on_exit() {
  local status=$?
  [[ -n $STAGE ]] && rm -rf "$STAGE"
  if [[ $status -ne 0 && $RESTORE_ON_FAIL -eq 1 ]]; then
    echo "error: the upgrade failed; starting the previous install again." >&2
    systemctl start bacpack.service ||
      echo "error: it didn't start either; fix the cause above and run the installer again." >&2
  fi
}
trap on_exit EXIT
if [[ -f $UNIT ]]; then
  if systemctl is-active --quiet bacpack.service; then
    WAS_ACTIVE=1; RESTORE_ON_FAIL=1
    echo "Stopping BACpack (its running devices are restarted afterwards)..."
    systemctl stop bacpack.service
  fi
fi
# Start unless the user stopped a marked install: no marker is a first install
# (or one that never ran); a running install is started again whatever the marker.
START=0
[[ $STARTED_BEFORE -eq 0 || $WAS_ACTIVE -eq 1 ]] && START=1

# On a taken web port the service exits a few seconds into each start and
# systemd restarts it every 5 s, for good: refuse before anything changes.
port_owner() {
  command -v ss >/dev/null || return 0
  local line
  line=$(ss -Hltnp "sport = :$1" 2>/dev/null | head -n1) || true
  [[ -n $line ]] || return 0
  local name
  name=$(sed -n 's/.*users:(("\([^"]*\)",pid=\([0-9]*\).*/\1 (process \2)/p' <<<"$line")
  echo "${name:-another program}"
}
if [[ $START -eq 1 ]]; then
  owner=$(port_owner "$PORT")
  [[ -z $owner ]] || die "port $PORT/tcp is in use by $owner. Stop that program, or run the installer again with --port N."
fi

# --- user, data directory, settings --------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --no-create-home \
    --shell "$(command -v nologin || echo /usr/sbin/nologin)" "$SERVICE_USER"
fi
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"
# A reinstall after an uninstall without --purge gets a new service user, maybe
# with another uid: tls/ and secret.key (0700/0600) must follow it. The trailing
# slash follows a symlinked data dir itself; -h re-owns links inside it without
# following them. Modes are not touched.
chown -hR "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/"
find "$DATA_DIR/" -maxdepth 1 -type f -name 'bacpack*.db*' -exec chmod 640 {} +

OLD_PORT=$(env_value BACPACK_PORT)
KIND=$(firewall_kind)
if [[ -n $OLD_PORT && $OLD_PORT != "$PORT" ]]; then
  firewall_remove_recorded
  FIREWALL=""
else
  FIREWALL=$(env_value BACPACK_FIREWALL)
fi

# --- code: a root-owned venv, BACpack installed from a staged copy ---------
STAGE=$(mktemp -d)
cp -a "$SRC_DIR/pyproject.toml" "$SRC_DIR/README.md" "$SRC_DIR/src" "$SRC_DIR/packaging" "$STAGE"/
[[ -f $SRC_DIR/LICENSE ]] && cp -a "$SRC_DIR/LICENSE" "$STAGE"/

# The venv is rebuilt when its interpreter is gone or isn't the one chosen
# above (a --python of the same version included).
if [[ -e $VENV ]]; then
  have=$(sed -n 's/^executable *= *//p' "$VENV/pyvenv.cfg" 2>/dev/null || true)
  if ! "$VENV/bin/python" -c '' 2>/dev/null || [[ $(readlink -f "$have") != $(readlink -f "$PY") ]]; then
    echo "Recreating the virtualenv for $PY..."
    rm -rf "$VENV"
  fi
fi
install -d -m 755 "$INSTALL_DIR"
[[ -x $VENV/bin/python ]] || "$PY" -m venv "$VENV"
echo "Installing BACpack and its pinned dependencies (this can take a few minutes)..."
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -c "$STAGE/packaging/constraints.txt" "$STAGE"
# A copy of this script, so "bacpack-admin uninstall" needs no release folder.
# Refreshed at every run, so it always matches the installed version.
[[ ${BASH_SOURCE[0]} -ef $INSTALL_DIR/install.sh ]] || install -m 755 "${BASH_SOURCE[0]}" "$INSTALL_DIR/install.sh"
chown -R root:root "$INSTALL_DIR"
# The new code is in place: a failure from here is not "the upgrade failed" and
# the old install can no longer be started again.
RESTORE_ON_FAIL=0
# The marker goes now, not at the env file's rewrite below: the steps between
# can abort the script, and a stopped install must not keep a "started" record.
if [[ $START -eq 1 && -f $ENV_FILE ]]; then
  sed -i '/^BACPACK_STARTED=/d' "$ENV_FILE"
fi

# The venv runs the base interpreter in place. One copied out of a home
# directory (a uv or pyenv build) keeps a home label that systemd may not
# execute, so its prefix gets the default labels too (outside /usr only).
PY_PREFIX=$("$PY" -c 'import sys; print(sys.base_prefix)')
if command -v selinuxenabled >/dev/null && selinuxenabled; then
  restorecon -R "$INSTALL_DIR" "$DATA_DIR"
  [[ $PY_PREFIX == /usr || $PY_PREFIX == /usr/* ]] || restorecon -R "$PY_PREFIX"
fi
PY_OWNER=$(stat -c %U "$(readlink -f "$PY")")

# --- unit, settings file, firewall ---------------------------------------
echo "Registering the service..."
install -m 644 "$STAGE/packaging/bacpack.service" "$UNIT"
if [[ -n $KIND ]]; then
  echo "Opening the firewall..."
  for rule in "$PORT/tcp" "$BACNET_PORT/udp"; do
    added=$(firewall_add "$KIND" "$rule")
    [[ -n $added && " $FIREWALL " != *" $added "* ]] && FIREWALL="${FIREWALL:+$FIREWALL }$added"
  done
  [[ $KIND == firewalld ]] && firewall-cmd --reload >/dev/null
fi
umask 022
printf 'BACPACK_PORT=%s\nBACPACK_FIREWALL=%s\n' "$PORT" "$FIREWALL" > "$ENV_FILE"
# The printf above rewrote the file without the marker (already dropped when
# starting); a stopped, marked install gets it back.
[[ $START -eq 0 ]] && echo 'BACPACK_STARTED=1' >> "$ENV_FILE"
systemctl daemon-reload

# bacpack-admin: local recovery and setup, as the service user (the database
# belongs to it). -m keeps the environment, which carries the password.
cat > "$ADMIN_BIN" <<ADMIN
#!/usr/bin/env bash
[[ \$EUID -eq 0 ]] || { echo "run as root: sudo bacpack-admin \$*" >&2; exit 1; }
# The uninstall deletes the venv and this file, so it runs from a temp copy of
# the installer kept in $INSTALL_DIR, never through Python.
if [[ \${1:-} == uninstall ]]; then
  shift
  [[ -f $INSTALL_DIR/install.sh ]] || { echo "error: $INSTALL_DIR/install.sh is missing; run install.sh --uninstall from the release folder" >&2; exit 1; }
  tmp=\$(mktemp)
  cp $INSTALL_DIR/install.sh "\$tmp"
  bash "\$tmp" --uninstall "\$@"; status=\$?
  rm -f "\$tmp"
  exit \$status
fi
export BACPACK_DB_PATH=$DATA_DIR/bacpack.db
export BACPACK_PORT=$PORT
exec runuser -u $SERVICE_USER -m -- $VENV/bin/python -P -m bacpack.admin "\$@"
ADMIN
chmod 755 "$ADMIN_BIN"

# Before the service starts, so a fresh install is never open between start and login.
if [[ -n $LOGIN_USER ]]; then
  echo "Creating the login..."
  BACPACK_LOGIN_PASSWORD=$LOGIN_PASSWORD "$ADMIN_BIN" set-login --username "$LOGIN_USER" || {
    # Exit 2 is bacpack-admin refusing (a database from a newer BACpack, bad
    # arguments): its own message above says why.
    (( $? == 2 )) && die "bacpack-admin stopped (its message is above); the install is done but BACpack was not started. Fix the cause and run this installer again: it starts the service."
    die "the login was not created (see above); the install is done but BACpack was not started. Run this installer again (--login-user NAME, --no-login, or neither): it starts the service."
  }
elif [[ $NO_LOGIN -eq 1 ]]; then
  echo "Recording 'run without login'..."
  "$ADMIN_BIN" no-login || die "recording 'run without login' failed (see above); BACpack was not started. Run this installer again: it starts the service."
fi
# Only now: a failed login step leaves the service neither enabled nor started.
systemctl enable bacpack.service >/dev/null

if [[ $START -eq 1 ]]; then
  echo "Starting BACpack..."
  systemctl start bacpack.service
  # Type=simple counts as active the moment it forks; a service that dies on
  # startup (a refused database, a bad certificate, a taken port) shows up
  # seconds later, and a restart gets a new main pid. The port was free above,
  # so a listener on it is BACpack's.
  FIRST_PID=$(systemctl show -p MainPID --value bacpack.service)
  LISTENING=0
  WAITED=0
  for _ in $(seq 90); do
    sleep 1
    if ! systemctl is-active --quiet bacpack.service ||
       [[ $(systemctl show -p MainPID --value bacpack.service) != "$FIRST_PID" ]]; then
      # systemd would keep restarting it after this installer is gone.
      systemctl stop bacpack.service || true
      echo "error: BACpack started but stopped before its web UI came up; the installer stopped the service. The last log lines:" >&2
      journalctl -u bacpack -n 20 --no-pager >&2 || true
      exit 1
    fi
    # Without ss the listener can't be seen: five seconds up is all it checks.
    if command -v ss >/dev/null; then
      [[ -n $(port_owner "$PORT") ]] && { LISTENING=1; break; }
    elif (( ++WAITED >= 5 )); then
      LISTENING=1; break
    fi
  done
  # Still running, so left running: it may be waiting for its saved listen
  # address to appear on this computer.
  [[ $LISTENING -eq 1 ]] ||
    echo "warning: BACpack is running but its web UI is not listening on port $PORT after 90 seconds; see: journalctl -u bacpack" >&2
  echo 'BACPACK_STARTED=1' >> "$ENV_FILE"
fi

# The address on the interface that carries the default route (192.0.2.1 is
# TEST-NET-1: asking for a route to it sends nothing); else the first address.
HOST=$(ip -4 route get 192.0.2.1 2>/dev/null | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -n1) || true
[[ -n $HOST ]] || HOST=$(hostname -I 2>/dev/null | awk '{print $1}') || true
REACH=${HOST:-"<this computer's address>"}
VERSION=$("$VENV/bin/python" -c 'from importlib.metadata import version; print(version("bacpack"))')
echo
echo "BACpack $VERSION is installed."
echo
echo "  Web UI:   https://localhost:$PORT"
echo "            from another computer: https://$REACH:$PORT"
echo "  Logs:     journalctl -u bacpack -f"
echo "  Service:  sudo systemctl {status|stop|start|restart} bacpack"
if [[ -n $LOGIN_USER ]]; then
  echo
  echo "Next: sign in as '$LOGIN_USER'."
elif [[ $NO_LOGIN -eq 1 ]]; then
  echo
  echo "Warning: BACpack runs without a login, so anyone on the"
  echo "network can control it. Add a login under Security in the"
  echo "web UI."
elif [[ $STARTED_BEFORE -eq 0 && $WAS_ACTIVE -eq 0 ]]; then
  echo
  echo "Next: open the web UI and create a login (the first visit"
  echo "claims it), or choose to run without one."
fi
echo
echo "Notes:"
echo "- The certificate is self-signed, so the browser warns once."
echo "    The Web Server page shows its fingerprint."
if [[ -n $KIND ]]; then
  echo "- Firewall ($KIND): $PORT/tcp and $BACNET_PORT/udp are open."
  echo "    Uninstall removes only the rules this script added."
  echo "    A device on another UDP port needs its own rule."
else
  echo "- No active firewalld or ufw found. If a firewall is in use,"
  echo "    open $PORT/tcp and $BACNET_PORT/udp yourself."
fi
echo "- For port 443, first grant the service the right to bind it:"
echo "    sudo mkdir -p /etc/systemd/system/bacpack.service.d"
echo "    put [Service] and AmbientCapabilities=CAP_NET_BIND_SERVICE in"
echo "    /etc/systemd/system/bacpack.service.d/override.conf,"
echo "    then run the installer again with --port 443."
echo "- Read SECURITY.md before you put BACpack on a shared network."
if [[ $NO_LOGIN -eq 0 ]]; then
  echo "- Forgotten password:"
  echo "    sudo bacpack-admin reset-login"
fi
if [[ $START -eq 0 ]]; then
  echo
  echo "The service was stopped before the upgrade and is left stopped."
fi
if [[ $PY_OWNER != root ]]; then
  echo
  echo "Warning: this Python is owned by $PY_OWNER, who can change"
  echo "the Python this service runs:"
  echo "  $PY_PREFIX"
  echo "  sudo chown -R root:root $PY_PREFIX"
fi
echo
exit 0
