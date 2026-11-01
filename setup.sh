#!/usr/bin/env bash
# local-feather-v2 :: one-shot setup
#
#   sudo ./setup.sh
#
# What it does (safe to re-run):
#   1. installs KVM + libvirt, Docker (if missing) and a few helper tools
#   2. installs the local-feather agent (Proxmox-compatible API over libvirt)
#      as a systemd service and downloads Debian 12 / Ubuntu 24.04 cloud images
#   3. patches FeatherPanel (adds the "Create Local Node" button next to
#      "Create VDS Node") and rebuilds ONLY the backend + frontend containers
#      of your existing panel, keeping its database and volumes.
#      If no panel is installed yet, a fresh one is built from source.
#
# Optional environment variables:
#   PANEL_REF=v1.5.3     FeatherPanel git tag/branch to build (patch is tested on v1.5.3)
#   PANEL_DIR=...        compose directory of an existing panel (auto-detected from Docker)
#   AGENT_PORT=8007      port of the local-feather agent
#   SKIP_IMAGES=1        do not download the OS cloud images
#   SKIP_PANEL=1         only install KVM + agent, leave the panel alone
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PANEL_REF="${PANEL_REF:-v1.5.3}"
PANEL_REPO="${PANEL_REPO:-https://github.com/MythicalLTD/FeatherPanel}"
WORK="${LF_HOME:-/opt/local-feather}"
SRC="$WORK/FeatherPanel"
DATA_DIR="${LF_DATA_DIR:-/var/lib/local-feather}"
CONF_DIR="/etc/local-feather"
AGENT_PORT="${AGENT_PORT:-8007}"
SKIP_IMAGES="${SKIP_IMAGES:-0}"
SKIP_PANEL="${SKIP_PANEL:-0}"
PANEL_DIR="${PANEL_DIR:-}"

c_ok=$'\033[1;32m'; c_warn=$'\033[1;33m'; c_err=$'\033[1;31m'; c_off=$'\033[0m'
log()  { printf '%s==>%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '%s[!]%s %s\n' "$c_warn" "$c_off" "$*" >&2; }
die()  { printf '%s[x]%s %s\n' "$c_err" "$c_off" "$*" >&2; exit 1; }
trap 'die "setup failed at line $LINENO (command: $BASH_COMMAND)"' ERR

# ---------------------------------------------------------------- preflight
[[ $EUID -eq 0 ]] || die "run as root:  sudo ./setup.sh"
[[ -f "$HERE/agent/local-feather-agent.py" ]] || die "run setup.sh from inside the local-feather-v2 folder"
[[ "$(uname -m)" == "x86_64" ]] || die "only x86_64 hosts are supported (got $(uname -m))"
. /etc/os-release
case "${ID:-}" in
  debian|ubuntu) ;;
  *) die "Debian or Ubuntu is required (found: ${PRETTY_NAME:-unknown})" ;;
esac

if ! grep -Eq '(vmx|svm)' /proc/cpuinfo || [[ ! -e /dev/kvm ]]; then
  warn "No hardware virtualization (/dev/kvm) here. VDS will still work, but via slow software emulation."
  warn "On a VPS, ask the provider to enable nested virtualization for real speed."
fi
mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
if [[ "$SKIP_PANEL" != "1" && "$mem_mb" -lt 3500 ]]; then
  warn "Only ${mem_mb} MB RAM: building the panel needs ~4 GB. Add swap if the build gets killed."
fi

# ---------------------------------------------------------------- packages
log "Installing KVM / libvirt / tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients dnsmasq-base \
  iptables iproute2 cloud-image-utils genisoimage python3 git curl ca-certificates openssl

if ! command -v docker >/dev/null 2>&1; then
  log "Installing Docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin
docker compose version >/dev/null 2>&1 || die "docker compose plugin is missing"

# ---------------------------------------------------------------- libvirt
log "Starting libvirt"
systemctl enable --now libvirtd >/dev/null 2>&1 || systemctl enable --now virtqemud.socket >/dev/null 2>&1 || true
virsh -c qemu:///system net-info default >/dev/null 2>&1 || \
  virsh -c qemu:///system net-define /usr/share/libvirt/networks/default.xml >/dev/null 2>&1 || true
virsh -c qemu:///system net-autostart default >/dev/null 2>&1 || true
virsh -c qemu:///system net-start default >/dev/null 2>&1 || true
virsh -c qemu:///system list --all >/dev/null || die "libvirt is not working (virsh list failed)"

# ---------------------------------------------------------------- agent
log "Installing local-feather agent"
BIND_IP="$(docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
BIND_IP="${BIND_IP:-172.17.0.1}"

install -d -m 755 "$WORK" "$DATA_DIR" "$DATA_DIR/images" "$DATA_DIR/iso" "$DATA_DIR/vms"
install -d -m 700 "$CONF_DIR"
install -m 755 "$HERE/agent/local-feather-agent.py" "$WORK/local-feather-agent.py"

ADMIN_TOKEN=""
if [[ -f "$CONF_DIR/agent.json" ]]; then
  ADMIN_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("admin_token",""))' "$CONF_DIR/agent.json")"
fi
[[ -n "$ADMIN_TOKEN" ]] || ADMIN_TOKEN="$(openssl rand -hex 32)"
umask 077
cat > "$CONF_DIR/agent.json" <<EOF
{
  "admin_token": "$ADMIN_TOKEN",
  "bind": "$BIND_IP",
  "port": $AGENT_PORT,
  "data_dir": "$DATA_DIR",
  "libvirt_uri": "qemu:///system"
}
EOF
umask 022

cat > /etc/systemd/system/local-feather-agent.service <<EOF
[Unit]
Description=local-feather agent (Proxmox-compatible API over libvirt)
After=network-online.target docker.service libvirtd.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $WORK/local-feather-agent.py
Restart=always
RestartSec=3
Environment=LF_CONFIG=$CONF_DIR/agent.json

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable local-feather-agent >/dev/null 2>&1
systemctl restart local-feather-agent

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow from 172.16.0.0/12 to "$BIND_IP" port "$AGENT_PORT" proto tcp >/dev/null 2>&1 || true
fi

for _ in $(seq 1 30); do
  curl -fsS "http://$BIND_IP:$AGENT_PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://$BIND_IP:$AGENT_PORT/healthz" >/dev/null 2>&1 || {
  journalctl -u local-feather-agent -n 20 --no-pager || true
  die "agent did not come up on $BIND_IP:$AGENT_PORT"
}
log "Agent is running on $BIND_IP:$AGENT_PORT"

# ---------------------------------------------------------------- OS images (templates)
fetch_image() {  # <file name> <url>
  local name="$1" url="$2" dest="$DATA_DIR/images/$1"
  if [[ -s "$dest" ]]; then log "Image $name already present"; return; fi
  log "Downloading $name (one time, a few hundred MB)"
  curl -fL --retry 3 -C - -o "$dest.part" "$url" && mv "$dest.part" "$dest" \
    || { rm -f "$dest.part"; warn "could not download $name from $url (continuing)"; }
}
if [[ "$SKIP_IMAGES" != "1" ]]; then
  fetch_image "debian-12.qcow2" "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
  fetch_image "ubuntu-24.04.img" "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
fi
if ! ls "$DATA_DIR"/images/*.qcow2 "$DATA_DIR"/images/*.img >/dev/null 2>&1; then
  warn "No OS image in $DATA_DIR/images yet. Drop any cloud-init *.qcow2/*.img there; each file becomes a template."
fi

# ---------------------------------------------------------------- panel
if [[ "$SKIP_PANEL" == "1" ]]; then
  log "SKIP_PANEL=1 -> panel left untouched"
else
  log "Fetching FeatherPanel $PANEL_REF"
  rm -rf "$SRC"
  git clone --depth 1 --branch "$PANEL_REF" "$PANEL_REPO" "$SRC"
  git -C "$SRC" apply --check "$HERE/patches/local-node.patch" \
    || die "patch does not fit FeatherPanel $PANEL_REF. Use PANEL_REF=v1.5.3 (tested)."
  git -C "$SRC" apply "$HERE/patches/local-node.patch"
  log "Patch applied (Create Local Node button + API)"

  # existing panel? (found through its Docker labels, so the data stays untouched)
  PROJECT="featherpanel"; BASE_FILES=(); FRESH=0
  if docker inspect featherpanel_backend >/dev/null 2>&1; then
    PROJECT="$(docker inspect featherpanel_backend -f '{{ index .Config.Labels "com.docker.compose.project" }}')"
    [[ -n "$PANEL_DIR" ]] || PANEL_DIR="$(docker inspect featherpanel_backend -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
    cfg_files="$(docker inspect featherpanel_backend -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
    IFS=',' read -r -a BASE_FILES <<< "$cfg_files"
    [[ -n "$PANEL_DIR" && ${#BASE_FILES[@]} -gt 0 && -n "${BASE_FILES[0]}" ]] \
      || die "found a panel container but not its compose files. Re-run with PANEL_DIR=/path/to/compose/dir"
    log "Existing panel found (project '$PROJECT', dir $PANEL_DIR)"
    if [[ -n "$(docker ps -q -f name='^featherpanel_mysql$')" ]]; then
      bk="$WORK/db-backup-$(date +%F-%H%M%S).sql.gz"
      log "Backing up panel database -> $bk"
      docker exec featherpanel_mysql sh -c 'exec mariadb-dump --all-databases -uroot -p"$MARIADB_ROOT_PASSWORD"' 2>/dev/null | gzip > "$bk" \
        || warn "database backup failed (continuing)"
    fi
  else
    FRESH=1
    PANEL_DIR="$SRC"
    BASE_FILES=("$SRC/docker-compose.v2.git.yml")
    log "No panel found -> installing a fresh one from source"
  fi

  FILES=()
  for f in "${BASE_FILES[@]}"; do FILES+=(-f "$f"); done
  OVERRIDE="$PANEL_DIR/docker-compose.local-feather.yml"

  needs_host_gateway=1
  base_cfg="$(docker compose --project-name "$PROJECT" --project-directory "$PANEL_DIR" "${FILES[@]}" config 2>/dev/null || true)"
  if grep -q 'host.docker.internal' <<<"$base_cfg"; then
    needs_host_gateway=0
  fi

  umask 077
  {
    echo "# generated by local-feather-v2 setup.sh - contains a secret, keep private"
    echo "services:"
    echo "  backend:"
    echo "    image: local-feather/backend:$PANEL_REF"
    echo "    build:"
    echo "      context: $SRC/backend"
    echo "      dockerfile: Dockerfile"
    echo "    environment:"
    echo "      - LOCAL_NODE_AGENT_URL=http://host.docker.internal:$AGENT_PORT"
    echo "      - LOCAL_NODE_AGENT_ADMIN_TOKEN=$ADMIN_TOKEN"
    if [[ "$needs_host_gateway" == "1" ]]; then
      echo "    extra_hosts:"
      echo "      - \"host.docker.internal:host-gateway\""
    fi
    echo "  frontendv2:"
    echo "    image: local-feather/frontendv2:$PANEL_REF"
    echo "    build:"
    echo "      context: $SRC/frontendv2"
    echo "      dockerfile: Dockerfile"
  } > "$OVERRIDE"
  umask 022

  COMPOSE=(docker compose --project-name "$PROJECT" --project-directory "$PANEL_DIR" "${FILES[@]}" -f "$OVERRIDE")
  services="$("${COMPOSE[@]}" config --services)"
  grep -qx backend <<<"$services"    || die "compose project has no 'backend' service"
  grep -qx frontendv2 <<<"$services" || die "compose project has no 'frontendv2' service"

  log "Building panel (backend + frontend). This takes several minutes..."
  if [[ "$FRESH" == "1" ]]; then
    "${COMPOSE[@]}" up -d --build
  else
    "${COMPOSE[@]}" up -d --build backend frontendv2
  fi

  log "Waiting for the backend to become healthy"
  for _ in $(seq 1 90); do
    st="$(docker inspect featherpanel_backend -f '{{ .State.Health.Status }}' 2>/dev/null || echo starting)"
    [[ "$st" == "healthy" ]] && break
    sleep 5
  done
  [[ "${st:-}" == "healthy" ]] || warn "backend not healthy yet; check: docker logs featherpanel_backend"
fi

# ---------------------------------------------------------------- done
port="${FEATHERPANEL_PANEL_PORT:-4831}"
cat <<EOF

${c_ok}Done.${c_off}
  Panel:        http://<this-server-ip>:$port   (Admin -> Infrastructure -> Nodes (VDS))
  Next step:    click "Create Local Node", type a name, press Create. That's all.
  Repeat as often as you like: every name = one more local node, no limits.

  Agent:        systemctl status local-feather-agent
  OS templates: $DATA_DIR/images   (every *.qcow2 / *.img = one template)
  Agent secret: $CONF_DIR/agent.json
EOF
