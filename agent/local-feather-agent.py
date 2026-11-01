#!/usr/bin/env python3
"""local-feather-agent

A small Proxmox-VE-compatible HTTP API on top of local KVM/libvirt, so that
FeatherPanel's "VDS node" code can manage VMs on this very machine without a
Proxmox install.

  * Only the Python standard library is needed.
  * Every "local node" is a separate API identity (own token), created by the
    panel through POST /local/nodes (admin token) when you press
    "Create Local Node" and type a name.
  * No limits are enforced here: any number of VMs per node, any number of nodes.
    (Host RAM/CPU/disk are of course still the real limit.)

Not implemented (answered with HTTP 501): LXC, VNC console proxy, backups
(vzdump), firewall, ISO upload, access/users/ACL.

Environment / config file (/etc/local-feather/agent.json):
  admin_token  secret used by the panel to create nodes   (required)
  bind, port   listen address (default 127.0.0.1:8007)
  data_dir     state + disks (default /var/lib/local-feather)
Set LF_DRY_RUN=1 to run without libvirt/qemu (used by the tests).
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("LF_CONFIG", "/etc/local-feather/agent.json")
DRY_RUN = os.environ.get("LF_DRY_RUN") == "1"
LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "admin_token": "",
    "bind": "127.0.0.1",
    "port": 8007,
    "data_dir": "/var/lib/local-feather",
    "libvirt_uri": "qemu:///system",
    "min_vmid": 100,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    for key, env in (("admin_token", "LF_ADMIN_TOKEN"), ("bind", "LF_BIND"),
                     ("port", "LF_PORT"), ("data_dir", "LF_DATA_DIR")):
        if os.environ.get(env):
            cfg[key] = int(os.environ[env]) if key == "port" else os.environ[env]
    return cfg


CFG = load_config()
DATA = CFG["data_dir"]
STATE_FILE = os.path.join(DATA, "state.json")
IMAGES_DIR = os.path.join(DATA, "images")
VMS_DIR = os.path.join(DATA, "vms")
ISO_DIR = os.path.join(DATA, "iso")

# ----------------------------------------------------------------------------
# state
# ----------------------------------------------------------------------------

STATE = {"nodes": {}, "templates": {}, "next_template_vmid": 9000}
TASKS = {}          # upid -> dict
DRY_DOMAINS = {}    # dry-run only: domain -> running bool


def save_state():
    os.makedirs(DATA, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(STATE, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as fh:
            STATE.update(json.load(fh))


def slug(name):
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return (s or "node")[:40]


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def all_vmids():
    used = set(STATE["templates"].values())
    for node in STATE["nodes"].values():
        used.update(int(v) for v in node["vms"])
    return used


# ----------------------------------------------------------------------------
# shell helpers
# ----------------------------------------------------------------------------

def run(cmd, check=True, timeout=900):
    """Run an external command (no-op in dry-run mode)."""
    if DRY_RUN:
        return ""
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd[:3]), (res.stderr or res.stdout).strip()))
    return res.stdout


def virsh(*args, check=True):
    return run(["virsh", "-c", CFG["libvirt_uri"], *args], check=check)


def domain_name(node, vmid):
    return "lf-%s-%s" % (node, vmid)


def domain_state(node, vmid):
    name = domain_name(node, vmid)
    if DRY_RUN:
        return "running" if DRY_DOMAINS.get(name) else "stopped"
    out = virsh("domstate", name, check=False).strip()
    return "running" if out == "running" else "stopped"


def kvm_available():
    return os.path.exists("/dev/kvm")


def host_bridges():
    if DRY_RUN:
        return ["virbr0"]
    try:
        out = subprocess.run(["ip", "-j", "link", "show", "type", "bridge"],
                             capture_output=True, text=True, timeout=10).stdout
        return sorted(i["ifname"] for i in json.loads(out or "[]"))
    except Exception:
        return ["virbr0"]


def host_resources():
    cpus = os.cpu_count() or 1
    mem_total = mem_free = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            info = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in fh if ":" in l}
        mem_total, mem_free = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    except OSError:
        pass
    try:
        du = shutil.disk_usage(DATA if os.path.isdir(DATA) else "/")
        disk_total, disk_used = du.total, du.used
    except OSError:
        disk_total = disk_used = 0
    return cpus, mem_total, mem_total - mem_free, disk_total, disk_used


# ----------------------------------------------------------------------------
# VM lifecycle
# ----------------------------------------------------------------------------

def parse_size_gb(value):
    m = re.match(r"^\+?(\d+(?:\.\d+)?)([KMGT]?)$", str(value).strip(), re.I)
    if not m:
        raise ValueError("invalid size: %s" % value)
    num, unit = float(m.group(1)), (m.group(2) or "G").upper()
    return num * {"K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1, "T": 1024}[unit]


def vm_dir(node, vmid):
    return os.path.join(VMS_DIR, node, str(vmid))


def disk_path(node, vmid):
    return os.path.join(vm_dir(node, vmid), "disk0.qcow2")


def net_source(bridge):
    bridges = host_bridges()
    if bridge in (None, "", "vmbr0", "virbr0") or bridge not in bridges:
        return "<interface type='network'><source network='default'/>"
    return "<interface type='bridge'><source bridge='%s'/>" % bridge


def domain_xml(node, vmid, vm):
    cfg = vm["config"]
    mem_mib = int(cfg.get("memory", 512))
    cores = int(cfg.get("cores", 1)) * int(cfg.get("sockets", 1))
    mac = vm["mac"]
    dtype = "kvm" if kvm_available() else "qemu"
    bridge = None
    m = re.search(r"bridge=([\w.-]+)", cfg.get("net0", ""))
    if m:
        bridge = m.group(1)
    seed = os.path.join(vm_dir(node, vmid), "seed.iso")
    return """<domain type='%(dtype)s'>
  <name>%(name)s</name>
  <memory unit='MiB'>%(mem)d</memory>
  <vcpu>%(cores)d</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <cpu mode='%(cpumode)s'/>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='%(disk)s'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='%(seed)s'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    %(net)s
      <mac address='%(mac)s'/>
      <model type='virtio'/>
    </interface>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>
    <video><model type='vga'/></video>
    <console type='pty'/>
    <rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
  </devices>
</domain>
""" % {"dtype": dtype, "name": domain_name(node, vmid), "mem": mem_mib, "cores": max(cores, 1),
       "cpumode": "host-passthrough" if dtype == "kvm" else "custom",
       "disk": disk_path(node, vmid), "seed": seed, "net": net_source(bridge), "mac": mac}


def cloud_init_files(vm):
    cfg = vm["config"]
    user = cfg.get("ciuser", "debian")
    lines = ["#cloud-config", "hostname: %s" % slug(cfg.get("name", "vm")),
             "manage_etc_hosts: true", "ssh_pwauth: true", "disable_root: false"]
    pw = cfg.get("cipassword")
    if pw:
        lines += ["chpasswd:", "  expire: false", "  list: |", "    %s:%s" % (user, pw)]
        if user != "root":
            lines += ["    root:%s" % pw]
    keys = urllib.parse.unquote(cfg.get("sshkeys", "")).strip()
    if keys:
        lines += ["ssh_authorized_keys:"] + ["  - %s" % k for k in keys.splitlines() if k.strip()]
    user_data = "\n".join(lines) + "\n"
    meta = "instance-id: %s-%s\nlocal-hostname: %s\n" % (vm["uuid"], vm.get("rev", 0), slug(cfg.get("name", "vm")))
    ipc = cfg.get("ipconfig0", "")
    net = ["version: 2", "ethernets:", "  id0:", "    match:", "      macaddress: '%s'" % vm["mac"]]
    m = re.search(r"ip=([\d.]+)/(\d+)", ipc)
    if m:
        net += ["    addresses: ['%s/%s']" % (m.group(1), m.group(2))]
        g = re.search(r"gw=([\d.]+)", ipc)
        if g:
            net += ["    routes:", "      - to: default", "        via: %s" % g.group(1)]
        ns = [x for x in cfg.get("nameserver", "1.1.1.1 8.8.8.8").replace(",", " ").split() if x]
        net += ["    nameservers:", "      addresses: [%s]" % ", ".join(ns)]
    else:
        net += ["    dhcp4: true"]
    return user_data, meta, "\n".join(net) + "\n"


def build_seed(node, vmid, vm):
    d = vm_dir(node, vmid)
    os.makedirs(d, exist_ok=True)
    ud, md, nc = cloud_init_files(vm)
    for name, body in (("user-data", ud), ("meta-data", md), ("network-config", nc)):
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(body)
    seed = os.path.join(d, "seed.iso")
    if DRY_RUN:
        open(seed, "w").close()
        return
    if shutil.which("cloud-localds"):
        run(["cloud-localds", "--network-config=" + os.path.join(d, "network-config"), seed,
             os.path.join(d, "user-data"), os.path.join(d, "meta-data")])
    else:
        tool = shutil.which("genisoimage") or shutil.which("mkisofs")
        if not tool:
            raise RuntimeError("cloud-localds/genisoimage not installed")
        run([tool, "-output", seed, "-volid", "cidata", "-joliet", "-rock",
             os.path.join(d, "user-data"), os.path.join(d, "meta-data"), os.path.join(d, "network-config")])


def define_domain(node, vmid, vm):
    xml_path = os.path.join(vm_dir(node, vmid), "domain.xml")
    with open(xml_path, "w", encoding="utf-8") as fh:
        fh.write(domain_xml(node, vmid, vm))
    virsh("define", xml_path)


def new_mac():
    return "52:54:00:%02x:%02x:%02x" % tuple(secrets.randbelow(256) for _ in range(3))


def vm_config_view(node, vmid, vm):
    cfg = dict(vm["config"])
    cfg.setdefault("cores", 1)
    cfg.setdefault("memory", 512)
    cfg["scsi0"] = "local:%s/disk0.qcow2,size=%dG" % (vmid, int(round(vm["disk_gb"])))
    cfg.setdefault("boot", "order=scsi0")
    cfg["net0"] = cfg.get("net0") or "virtio=%s,bridge=vmbr0" % vm["mac"]
    cfg["ide2"] = "local:%s/seed.iso,media=cdrom" % vmid
    cfg["digest"] = sha(json.dumps(vm["config"], sort_keys=True))[:40]
    if vm.get("template"):
        cfg["template"] = 1
    return cfg


def vm_status_view(node, vmid, vm):
    st = domain_state(node, vmid)
    cfg = vm["config"]
    maxmem = int(cfg.get("memory", 512)) * 1024 * 1024
    return {"status": st, "qmpstatus": st, "vmid": int(vmid), "name": cfg.get("name", "vm-%s" % vmid),
            "cpus": int(cfg.get("cores", 1)), "maxmem": maxmem, "mem": maxmem // 4 if st == "running" else 0,
            "maxdisk": int(vm["disk_gb"] * 1024 ** 3), "disk": 0, "cpu": 0, "uptime": 0,
            "netin": 0, "netout": 0, "diskread": 0, "diskwrite": 0, "template": 1 if vm.get("template") else 0}


# ----------------------------------------------------------------------------
# tasks (everything runs in a thread and is exposed like a PVE task)
# ----------------------------------------------------------------------------

def start_task(node, ttype, vmid, func):
    start = int(time.time())
    upid = "UPID:%s:%08X:%08X:%08X:%s:%s:root@pam:" % (node, os.getpid(), secrets.randbelow(2 ** 31), start, ttype, vmid)
    task = {"upid": upid, "node": node, "type": ttype, "id": str(vmid), "starttime": start,
            "status": "running", "exitstatus": None, "log": ["starting task %s" % ttype]}
    TASKS[upid] = task

    def runner():
        try:
            func(task)
            task["log"].append("TASK OK")
            task["exitstatus"] = "OK"
        except Exception as exc:  # noqa: BLE001 - reported through the task
            task["log"].append("TASK ERROR: %s" % exc)
            task["exitstatus"] = str(exc)[:200] or "ERROR"
        finally:
            task["status"] = "stopped"
            task["endtime"] = int(time.time())

    threading.Thread(target=runner, daemon=True).start()
    return upid


def find_template_image(tvmid):
    for fname, vmid in STATE["templates"].items():
        if vmid == tvmid:
            return os.path.join(IMAGES_DIR, fname)
    return None


def sync_templates():
    """Every *.qcow2/*.img in images/ is a template VM (same vmid on every node)."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    found = sorted(f for f in os.listdir(IMAGES_DIR) if f.endswith((".qcow2", ".img")))
    changed = False
    for f in found:
        if f not in STATE["templates"]:
            STATE["templates"][f] = STATE["next_template_vmid"]
            STATE["next_template_vmid"] += 1
            changed = True
    if changed:
        save_state()


def do_clone(node, src_vmid, newid, name):
    def work(task):
        image = find_template_image(src_vmid)
        if image is None:
            src = STATE["nodes"][node]["vms"].get(str(src_vmid))
            if not src:
                raise RuntimeError("source VM %s not found" % src_vmid)
            image = disk_path(node, src_vmid)
        os.makedirs(vm_dir(node, newid), exist_ok=True)
        task["log"].append("copying disk image")
        if DRY_RUN:
            open(disk_path(node, newid), "w").close()
            size = 10.0
        else:
            run(["qemu-img", "convert", "-O", "qcow2", image, disk_path(node, newid)])
            info = json.loads(run(["qemu-img", "info", "--output=json", disk_path(node, newid)]))
            size = info["virtual-size"] / 1024 ** 3
        vm = {"uuid": secrets.token_hex(8), "mac": new_mac(), "disk_gb": size, "rev": 0,
              "config": {"name": name, "cores": 1, "memory": 512, "ciuser": "debian"}}
        with LOCK:
            STATE["nodes"][node]["vms"][str(newid)] = vm
            save_state()
        build_seed(node, newid, vm)
        define_domain(node, newid, vm)
    return start_task(node, "qmclone", newid, work)


def do_power(node, vmid, action):
    name = domain_name(node, vmid)

    def work(task):
        if DRY_RUN:
            if action in ("start", "reboot", "reset"):
                DRY_DOMAINS[name] = True
            elif action in ("stop", "shutdown"):
                DRY_DOMAINS[name] = False
            return
        if action == "start":
            virsh("start", name)
        elif action == "shutdown":
            virsh("shutdown", name)
        elif action == "stop":
            virsh("destroy", name, check=False)
        elif action == "reboot":
            virsh("reboot", name)
        elif action == "reset":
            virsh("reset", name)
    return start_task(node, "qm" + action, vmid, work)


def do_delete(node, vmid):
    name = domain_name(node, vmid)

    def work(task):
        if DRY_RUN:
            DRY_DOMAINS.pop(name, None)
        else:
            virsh("destroy", name, check=False)
            virsh("undefine", name, "--nvram", check=False)
        shutil.rmtree(vm_dir(node, vmid), ignore_errors=True)
        with LOCK:
            STATE["nodes"][node]["vms"].pop(str(vmid), None)
            save_state()
    return start_task(node, "qmdestroy", vmid, work)


def apply_config(node, vmid, params):
    vm = STATE["nodes"][node]["vms"][str(vmid)]
    cfg = vm["config"]
    for key in [k for k in params.get("delete", "").split(",") if k]:
        cfg.pop(key, None)
    skip = {"delete", "digest", "scsi0", "ide2", "boot", "agent", "ostype", "serial0", "vga", "scsihw"}
    for k, v in params.items():
        if k in skip:
            continue
        if k == "net0":
            m = re.search(r"(?:virtio|e1000|rtl8139)=([0-9A-Fa-f:]{17})", v)
            if m:
                vm["mac"] = m.group(1).lower()
        cfg[k] = v
    vm["rev"] = vm.get("rev", 0) + 1
    build_seed(node, vmid, vm)
    define_domain(node, vmid, vm)
    save_state()


def do_resize(node, vmid, size):
    vm = STATE["nodes"][node]["vms"][str(vmid)]
    gb = parse_size_gb(size)
    target = vm["disk_gb"] + gb if str(size).startswith("+") else gb
    if target < vm["disk_gb"] - 1e-6:
        raise ValueError("cannot shrink disk")
    if not DRY_RUN:
        run(["qemu-img", "resize", disk_path(node, vmid), "%dG" % int(round(target))])
    vm["disk_gb"] = target
    save_state()


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------

class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Unsupported(Exception):
    pass


def node_view(name):
    cpus, mem_total, mem_used, disk_total, disk_used = host_resources()
    return {"node": name, "status": "online", "type": "node", "id": "node/%s" % name, "level": "",
            "cpu": 0.0, "maxcpu": cpus, "mem": mem_used, "maxmem": mem_total,
            "disk": disk_used, "maxdisk": disk_total, "uptime": int(time.time() - START_TIME),
            "ssl_fingerprint": ""}


def vm_list_entry(node, vmid, vm):
    s = vm_status_view(node, vmid, vm)
    s.update({"id": "qemu/%s" % vmid, "type": "qemu", "node": node})
    return s


def template_entry(node, name, vmid):
    return {"id": "qemu/%s" % vmid, "type": "qemu", "node": node, "vmid": vmid, "name": name.rsplit(".", 1)[0],
            "status": "stopped", "template": 1, "maxcpu": 1, "maxmem": 512 * 1024 ** 2, "maxdisk": 10 * 1024 ** 3,
            "cpu": 0, "mem": 0, "disk": 0, "uptime": 0}


def route(method, path, query, body, ident):
    """Return the 'data' value for a PVE-style request. ident = node name."""
    node = ident
    parts = [p for p in path.split("/") if p]
    # strip /api2/json
    if parts[:2] == ["api2", "json"]:
        parts = parts[2:]
    n = len(parts)
    sync_templates()

    if parts == ["version"]:
        return {"version": "8.2-local", "release": "local-feather", "repoid": "lf000000"}
    if parts == ["nodes"] and method == "GET":
        return [node_view(node)]
    if parts == ["cluster", "nextid"]:
        start = int(query.get("vmid") or CFG["min_vmid"])
        used = all_vmids()
        while start in used:
            start += 1
        return str(start)
    if parts == ["cluster", "resources"]:
        rtype = query.get("type")
        out = []
        if rtype in (None, "node"):
            out.append(node_view(node))
        if rtype in (None, "vm"):
            for fname, tvmid in STATE["templates"].items():
                out.append(template_entry(node, fname, tvmid))
            for vmid, vm in STATE["nodes"][node]["vms"].items():
                out.append(vm_list_entry(node, vmid, vm))
        return out
    if parts[:2] == ["cluster", "firewall"] or parts[:1] == ["access"]:
        raise Unsupported(path)

    if n >= 2 and parts[0] == "nodes":
        if parts[1] != node:
            raise Forbidden("token is not allowed on node %s" % parts[1])
        rest = parts[2:]
        vms = STATE["nodes"][node]["vms"]
        if rest == ["status"]:
            cpus, mt, mu, dt, du = host_resources()
            return {"uptime": int(time.time() - START_TIME), "cpu": 0.0, "pveversion": "local-feather/8.2",
                    "memory": {"total": mt, "used": mu, "free": mt - mu},
                    "rootfs": {"total": dt, "used": du, "free": dt - du, "avail": dt - du},
                    "cpuinfo": {"cpus": cpus, "cores": cpus, "sockets": 1, "model": "host", "mhz": "0"},
                    "kversion": os.uname().release if hasattr(os, "uname") else ""}
        if rest == ["network"]:
            return [{"iface": b, "type": "bridge", "active": 1, "method": "manual"} for b in host_bridges()]
        if rest == ["storage"]:
            _, _, _, dt, du = host_resources()
            return [{"storage": "local", "type": "dir", "content": "images,rootdir,iso,vztmpl,backup",
                     "active": 1, "enabled": 1, "shared": 0, "total": dt, "used": du, "avail": dt - du}]
        if len(rest) >= 3 and rest[0] == "storage" and rest[2] == "content":
            if query.get("content") == "iso" and os.path.isdir(ISO_DIR):
                return [{"volid": "local:iso/%s" % f, "content": "iso", "format": "iso",
                         "size": os.path.getsize(os.path.join(ISO_DIR, f))}
                        for f in sorted(os.listdir(ISO_DIR)) if f.endswith(".iso")]
            return []
        if rest and rest[0] == "tasks" and len(rest) >= 3:
            upid = urllib.parse.unquote(rest[1])
            task = TASKS.get(upid)
            if not task:
                raise NotFound("no such task")
            if rest[2] == "status":
                return {"status": task["status"], "exitstatus": task["exitstatus"], "upid": upid, "node": node,
                        "type": task["type"], "id": task["id"], "user": "root@pam", "starttime": task["starttime"],
                        "pid": os.getpid(), "pstart": task["starttime"]}
            if rest[2] == "log":
                return [{"n": i + 1, "t": line} for i, line in enumerate(task["log"])]
        if rest == ["lxc"]:
            return []
        if rest and rest[0] == "lxc":
            raise Unsupported("LXC containers are not supported on local nodes")
        if rest == ["qemu"] and method == "GET":
            return [vm_list_entry(node, v, vms[v]) for v in vms]
        if rest == ["qemu"] and method == "POST":
            newid = int(body["vmid"])
            if newid in all_vmids():
                raise ValueError("VM %s already exists" % newid)
            raise Unsupported("create from scratch is not supported; clone a template")
        if len(rest) >= 2 and rest[0] == "qemu":
            vmid = int(rest[1])
            sub = rest[2:]
            is_tpl = find_template_image(vmid) is not None
            if sub == ["clone"] and method == "POST":
                if vmid not in set(STATE["templates"].values()) and str(vmid) not in vms:
                    raise NotFound("source VM %s not found" % vmid)
                newid = int(body["newid"])
                if newid in all_vmids():
                    raise ValueError("VM %s already exists" % newid)
                return do_clone(node, vmid, newid, body.get("name") or "vm-%s" % newid)
            if is_tpl:
                raise NotFound("template VMs are read-only")
            if str(vmid) not in vms:
                raise NotFound("VM %s does not exist" % vmid)
            vm = vms[str(vmid)]
            if sub == [] and method == "DELETE":
                return do_delete(node, vmid)
            if sub == ["status", "current"]:
                return vm_status_view(node, vmid, vm)
            if len(sub) == 2 and sub[0] == "status" and method == "POST":
                if sub[1] in ("start", "stop", "shutdown", "reboot", "reset"):
                    return do_power(node, vmid, sub[1])
                if sub[1] == "suspend" or sub[1] == "resume":
                    raise Unsupported(sub[1])
            if sub == ["config"]:
                if method == "GET":
                    return vm_config_view(node, vmid, vm)
                apply_config(node, vmid, body)
                return None
            if sub == ["resize"] and method in ("PUT", "POST"):
                do_resize(node, vmid, body["size"])
                return None
            if sub == ["unlink"]:
                return None
            raise Unsupported("/".join(sub))
    raise NotFound(path)


START_TIME = time.time()


def create_node(name):
    node = slug(name)
    with LOCK:
        if node in STATE["nodes"]:
            raise ValueError("a local node named %s already exists" % node)
        token_id = "lf" + secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        STATE["nodes"][node] = {"title": name, "token_id": token_id, "secret_sha": sha(secret),
                                "created": int(time.time()), "vms": {}}
        save_state()
    os.makedirs(os.path.join(VMS_DIR, node), exist_ok=True)
    sync_templates()
    templates = [{"vmid": vmid, "name": fname.rsplit(".", 1)[0]}
                 for fname, vmid in sorted(STATE["templates"].items(), key=lambda kv: kv[1])]
    return {"node": node, "user": "root@pam", "token_id": token_id, "secret": secret, "templates": templates}


class Handler(BaseHTTPRequestHandler):
    server_version = "local-feather-agent/1"

    def log_message(self, fmt, *args):  # quiet, journald gets stderr
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers
    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        if not raw:
            return {}
        if "json" in (self.headers.get("Content-Type") or ""):
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        return {k: v[-1] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _is_admin(self):
        auth = self.headers.get("Authorization", "")
        return bool(CFG["admin_token"]) and auth.startswith("Bearer ") and \
            hmac.compare_digest(auth[7:].strip().encode(), CFG["admin_token"].encode())

    def _node_ident(self):
        auth = self.headers.get("Authorization", "")
        m = re.match(r"^PVEAPIToken=[^!]+!([^=]+)=(.+)$", auth)
        if not m:
            return None
        for name, node in STATE["nodes"].items():
            if hmac.compare_digest(node["token_id"].encode(), m.group(1).encode()) and \
                    hmac.compare_digest(node["secret_sha"].encode(), sha(m.group(2)).encode()):
                return name
        return None

    def _handle(self, method):
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/healthz":
                return self._send(200, {"ok": True})
            if url.path.startswith("/local/"):
                return self._admin(method, url.path)
            ident = self._node_ident()
            if ident is None:
                return self._send(401, {"data": None, "message": "authentication failure"})
            body = self._body() if method in ("POST", "PUT") else {}
            with LOCK:
                data = route(method, url.path, query, body, ident)
            return self._send(200, {"data": data})
        except NotFound as exc:
            self._send(404, {"data": None, "message": str(exc)})
        except Forbidden as exc:
            self._send(403, {"data": None, "message": str(exc)})
        except Unsupported as exc:
            self._send(501, {"data": None, "message": "not supported by local-feather-agent: %s" % exc})
        except (ValueError, KeyError) as exc:
            self._send(400, {"data": None, "message": "bad request: %s" % exc})
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("internal error: %r\n" % exc)
            self._send(500, {"data": None, "message": "internal error: %s" % exc})

    def _admin(self, method, path):
        if not self._is_admin():
            return self._send(401, {"error": "invalid admin token"})
        parts = [p for p in path.split("/") if p]  # local, nodes, [name]
        if parts == ["local", "nodes"] and method == "POST":
            name = str(self._body().get("name", "")).strip()
            if not name or len(name) > 255:
                return self._send(400, {"error": "name is required (max 255 chars)"})
            try:
                return self._send(201, {"data": create_node(name)})
            except ValueError as exc:
                return self._send(409, {"error": str(exc)})
        if parts == ["local", "nodes"] and method == "GET":
            return self._send(200, {"data": [{"node": k, "title": v["title"], "vms": len(v["vms"])}
                                             for k, v in STATE["nodes"].items()]})
        if len(parts) == 3 and parts[:2] == ["local", "nodes"] and method == "DELETE":
            with LOCK:
                node = STATE["nodes"].get(parts[2])
                if not node:
                    return self._send(404, {"error": "no such node"})
                if node["vms"]:
                    return self._send(409, {"error": "node still has VMs"})
                del STATE["nodes"][parts[2]]
                save_state()
            return self._send(200, {"data": True})
        return self._send(404, {"error": "not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    if not CFG["admin_token"]:
        sys.exit("admin_token missing: set it in %s or LF_ADMIN_TOKEN" % CONFIG_PATH)
    for d in (DATA, IMAGES_DIR, VMS_DIR, ISO_DIR):
        os.makedirs(d, exist_ok=True)
    load_state()
    sync_templates()
    srv = ThreadingHTTPServer((CFG["bind"], int(CFG["port"])), Handler)
    sys.stderr.write("local-feather-agent listening on %s:%s (dry-run=%s)\n" % (CFG["bind"], CFG["port"], DRY_RUN))
    srv.serve_forever()


if __name__ == "__main__":
    main()
