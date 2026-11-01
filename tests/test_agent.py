#!/usr/bin/env python3
"""End-to-end test of the agent in dry-run mode (no libvirt needed).

Mimics what FeatherPanel's Proxmox client sends (PVEAPIToken header,
form-encoded POST bodies) and walks the whole VM lifecycle.
"""
import json, os, subprocess, sys, tempfile, time, urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "..", "agent", "local-feather-agent.py")
PORT = 18007
BASE = "http://127.0.0.1:%d" % PORT
ADMIN = "test-admin-token"

def call(method, path, headers=None, form=None, js=None):
    data = None
    h = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if js is not None:
        data = json.dumps(js).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def wait_task(hdr, node, upid):
    for _ in range(50):
        _, r = call("GET", "/api2/json/nodes/%s/tasks/%s/status" % (node, urllib.parse.quote(upid, safe="")), hdr)
        if r["data"]["status"] == "stopped":
            return r["data"]["exitstatus"]
        time.sleep(0.1)
    raise AssertionError("task timeout")

def main():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "images"))
    open(os.path.join(tmp, "images", "debian-12.qcow2"), "w").close()
    env = dict(os.environ, LF_DRY_RUN="1", LF_ADMIN_TOKEN=ADMIN, LF_PORT=str(PORT), LF_BIND="127.0.0.1",
               LF_DATA_DIR=tmp, LF_CONFIG=os.path.join(tmp, "none.json"))
    proc = subprocess.Popen([sys.executable, AGENT], env=env, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                if call("GET", "/healthz")[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        adm = {"Authorization": "Bearer " + ADMIN}
        assert call("POST", "/local/nodes", {"Authorization": "Bearer nope"}, js={"name": "x"})[0] == 401
        code, r = call("POST", "/local/nodes", adm, js={"name": "My Local 1"})
        assert code == 201, (code, r)
        n1 = r["data"]
        assert n1["node"] == "my-local-1", n1
        assert n1["templates"] == [{"vmid": 9000, "name": "debian-12"}], n1
        assert call("POST", "/local/nodes", adm, js={"name": "My Local 1"})[0] == 409
        n2 = call("POST", "/local/nodes", adm, js={"name": "second"})[1]["data"]
        h1 = {"Authorization": "PVEAPIToken=%s!%s=%s" % (n1["user"], n1["token_id"], n1["secret"])}
        h2 = {"Authorization": "PVEAPIToken=%s!%s=%s" % (n2["user"], n2["token_id"], n2["secret"])}
        assert call("GET", "/api2/json/nodes")[0] == 401
        assert call("GET", "/api2/json/nodes", {"Authorization": "PVEAPIToken=root@pam!bad=bad"})[0] == 401
        node = n1["node"]
        code, r = call("GET", "/api2/json/nodes", h1)
        assert code == 200 and [x["node"] for x in r["data"]] == [node], r
        assert call("GET", "/api2/json/nodes/%s/qemu" % n2["node"], h1)[0] == 403
        assert call("GET", "/api2/json/version", h1)[1]["data"]["version"]
        assert call("GET", "/api2/json/nodes/%s/storage" % node, h1)[1]["data"][0]["active"] == 1
        assert call("GET", "/api2/json/nodes/%s/network?type=bridge" % node, h1)[1]["data"][0]["iface"] == "virbr0"

        # templates visible as cluster resources
        res = call("GET", "/api2/json/cluster/resources?type=vm", h1)[1]["data"]
        tpl = [x for x in res if x.get("template") == 1]
        assert len(tpl) == 1 and tpl[0]["vmid"] == 9000, res
        nid = int(call("GET", "/api2/json/cluster/nextid?vmid=5000", h1)[1]["data"])
        assert nid == 5000

        # clone -> config -> start -> resize -> stop -> delete
        code, r = call("POST", "/api2/json/nodes/%s/qemu/9000/clone" % node, h1, form={"newid": 5000, "name": "vm-a", "full": 1})
        assert code == 200 and r["data"].startswith("UPID:"), r
        assert wait_task(h1, node, r["data"]) == "OK"
        assert int(call("GET", "/api2/json/cluster/nextid?vmid=5000", h1)[1]["data"]) == 5001
        # vmid space is global: node2 may not reuse 5000
        assert int(call("GET", "/api2/json/cluster/nextid?vmid=5000", h2)[1]["data"]) == 5001
        code, r = call("PUT", "/api2/json/nodes/%s/qemu/5000/config" % node, h1,
                       form={"cores": 4, "memory": 2048, "ciuser": "root", "cipassword": "s3cret",
                             "ipconfig0": "ip=192.168.122.50/24,gw=192.168.122.1", "net0": "virtio=52:54:00:aa:bb:cc,bridge=vmbr0"})
        assert code == 200, r
        cfg = call("GET", "/api2/json/nodes/%s/qemu/5000/config" % node, h1)[1]["data"]
        assert cfg["cores"] == "4" and cfg["memory"] == "2048" and "size=" in cfg["scsi0"], cfg
        assert cfg["net0"].startswith("virtio=52:54:00:aa:bb:cc"), cfg
        seed = open(os.path.join(tmp, "vms", node, "5000", "network-config")).read()
        assert "192.168.122.50/24" in seed and "192.168.122.1" in seed, seed
        st = call("GET", "/api2/json/nodes/%s/qemu/5000/status/current" % node, h1)[1]["data"]
        assert st["status"] == "stopped", st
        r = call("POST", "/api2/json/nodes/%s/qemu/5000/status/start" % node, h1, form={})[1]["data"]
        assert wait_task(h1, node, r) == "OK"
        assert call("GET", "/api2/json/nodes/%s/qemu/5000/status/current" % node, h1)[1]["data"]["status"] == "running"
        assert call("PUT", "/api2/json/nodes/%s/qemu/5000/resize" % node, h1, form={"disk": "scsi0", "size": "+5G"})[0] == 200
        cfg = call("GET", "/api2/json/nodes/%s/qemu/5000/config" % node, h1)[1]["data"]
        assert "size=15G" in cfg["scsi0"], cfg["scsi0"]
        r = call("POST", "/api2/json/nodes/%s/qemu/5000/status/stop" % node, h1, form={})[1]["data"]
        assert wait_task(h1, node, r) == "OK"

        # unlimited: create many VMs on the same node
        for i in range(5001, 5011):
            r = call("POST", "/api2/json/nodes/%s/qemu/9000/clone" % node, h1, form={"newid": i, "name": "vm-%d" % i, "full": 1})[1]["data"]
            assert wait_task(h1, node, r) == "OK"
        assert len(call("GET", "/api2/json/nodes/%s/qemu" % node, h1)[1]["data"]) == 11

        # unsupported things say so instead of crashing
        assert call("POST", "/api2/json/nodes/%s/qemu/5000/vncproxy" % node, h1, form={"websocket": 1})[0] == 501
        assert call("GET", "/api2/json/nodes/%s/lxc" % node, h1)[1]["data"] == []

        # delete node refused while it has VMs, VM delete works
        assert call("DELETE", "/local/nodes/%s" % node, adm)[0] == 409
        r = call("DELETE", "/api2/json/nodes/%s/qemu/5000" % node, h1)[1]["data"]
        assert wait_task(h1, node, r) == "OK"
        assert call("GET", "/api2/json/nodes/%s/qemu/5000/status/current" % node, h1)[0] == 404
        assert call("DELETE", "/local/nodes/second", adm)[0] == 200
        print("agent dry-run tests: OK")
    finally:
        proc.terminate()
        proc.wait(timeout=5)

if __name__ == "__main__":
    main()
