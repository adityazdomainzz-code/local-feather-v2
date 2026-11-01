# local-feather-v2

Adds a **Create Local Node** button next to **Create VDS Node** in FeatherPanel
(Admin → Infrastructure → Nodes (VDS)). Type a name, press Create, done: a local
KVM node is registered on the same machine and you can create as many VDS on it
as you want. No Proxmox, no FQDN, no API token to copy.

```
sudo bash setup.sh
```

## How it works

| Part | What it does |
| --- | --- |
| `patches/local-node.patch` | Patch for FeatherPanel (tested on **v1.5.3** and current `main`). Adds the button + name dialog (`vds-nodes/page.tsx`) and `POST /api/admin/vm-nodes/local` (`VmNodesController::createLocal`). The endpoint asks the agent for a new node, stores it as a normal VDS node in a "Local" location and registers the OS templates. |
| `agent/local-feather-agent.py` | Small Proxmox-compatible API over libvirt (stdlib Python only). The panel's existing VDS code talks to it exactly like to a Proxmox host. Every local node has its own token. No VM / node limits are enforced. |
| `setup.sh` | Installs KVM + libvirt + Docker, installs the agent as a systemd service, downloads Debian 12 and Ubuntu 24.04 cloud images (each file in `/var/lib/local-feather/images` is a template), then patches and rebuilds **only** the panel's backend and frontend containers. DB and volumes are kept, and a DB dump is taken first. A fresh panel is built from source if none exists. |

Supported hosts: Debian 11+/Ubuntu 22.04+ on x86_64, run as root. Without
`/dev/kvm` (many VPS) VMs run in slow software emulation.

Options: `PANEL_REF`, `PANEL_DIR`, `AGENT_PORT`, `SKIP_IMAGES=1`, `SKIP_PANEL=1`
(see the top of `setup.sh`). Re-running is safe. After a panel update, run it again.

## Limits you should know about

* **"No limits"** means the agent enforces none (any number of nodes and VMs).
  Real host CPU/RAM/disk still apply, and the panel's own plan/user quotas are
  still the panel's job.
* **Not available on local nodes** (the agent answers HTTP 501): the VNC web
  console, LXC containers, backups/snapshots, the node firewall tab, ISO upload,
  Proxmox user/ACL calls.
* VMs sit on libvirt's NAT network (`virbr0`) by default. They reach the
  internet, but are not reachable from outside without port forwarding or a
  bridge. Static IPs from the panel's IP pool must be inside that subnet.
* Tested here: agent end-to-end in dry-run mode (`python3 tests/test_agent.py`),
  PHP/TSX syntax, `bash -n`, and that the patch applies to v1.5.3. **Not tested:**
  a real libvirt/KVM boot and a real panel rebuild. Try it on a spare server first.

## Rollback

Delete `docker-compose.local-feather.yml` from the panel's compose directory and
run your usual `docker compose up -d` (it pulls the stock images again).
To remove the agent: `systemctl disable --now local-feather-agent`.
