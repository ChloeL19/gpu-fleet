#!/usr/bin/env python3
"""Create a RunPod pod backed by a PERSISTENT NETWORK VOLUME.

WHY THIS EXISTS
---------------
The normal fleet (create_pods.py) uses COMMUNITY
pods with an *ephemeral* container disk: when the pod is destroyed, its data
is gone. Some pods need data to survive a nuke + recreate (training
checkpoints, datasets, long runs). RunPod's answer is a **network volume** —
a persistent disk that lives in one datacenter and can be re-attached to a
fresh pod later.

Two hard facts about network volumes drive everything here:
  1. A network volume is REGION-LOCKED. The pod must be created in the SAME
     datacenter as its volume (data_center_id == volume's dataCenterId).
  2. Network volumes only exist in datacenters with storage support, and only
     work with SECURE-cloud (datacenter) pods, not community pods.

WHAT THIS DOES
--------------
  * Finds-or-creates a network volume named "<prefix>-<name>-vol" (idempotent:
    if a volume with that name already exists it is REUSED, which is exactly
    what you want when recreating a pod on its old data).
  * Creates a SECURE on-demand pod in the volume's datacenter, attaches the
    volume at --mount (default /workspace), with the requested GPU / vCPU / RAM.
  * Records the pod -> volume mapping in network_volumes.json so we always know
    which volume (and which datacenter) a pod must be recreated against.

RECREATE-ON-SAME-VOLUME
-----------------------
Run `create_volume_pod <name>` with no other flags. The pod is rebuilt from its
registry entry: the same volume (pinned by id), GPU, count, vCPU/RAM, disk,
cloud and mount. Any flag you do pass overrides the recorded value.

USAGE
-----
  # H100 SXM pod, 200 GB persistent volume, 16 vCPU / 64 GB RAM:
  create_volume_pod oak \
      --gpu-type "NVIDIA H100 80GB HBM3" --data-center US-NE-1 \
      --volume-size 200 --vcpu 16 --memory 64

  create_volume_pod oak               # recreate from the registry, same volume

See NETWORK_VOLUMES.md for the full walk-through and how to pick a datacenter.
"""
import argparse
import ast
import fcntl
import json
import os
import time

from mydotenv import load_env
load_env()
import runpod_compat as runpod
from pod_boot import runpod_start_args

from pod_names import to_bare, to_full

runpod.api_key = os.getenv("RUNPOD_API_KEY")
PREFIX = os.environ["MACHINE_NAME_PREFIX"]
IMAGE = os.environ["RUNPOD_DOCKER_IMAGE"]
REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "network_volumes.json")

# Optional boot hook (POD_SETUP_CMD in config.env); None boots the image as-is.
DOCKER_ARGS = runpod_start_args()


def read_pubkey():
    """Fatal if missing: a pod without the shared key bills while nobody can log in."""
    p = os.getenv("SHARED_SSH_KEY_PATH")
    if not p:
        raise SystemExit("SHARED_SSH_KEY_PATH is not set in config.env.")
    pub = os.path.expanduser(os.path.expandvars(p)) + ".pub"
    try:
        key = open(pub).read().strip()
    except OSError as e:
        raise SystemExit(f"Can't read the shared SSH public key {pub}: {e}")
    if not key:
        raise SystemExit(f"The shared SSH public key {pub} is empty.")
    return key


def find_or_create_volume(vol_name, size, data_center):
    """Return (volume_id, data_center_id, size_gb, created). Reuse an existing
    volume with this name (region-locked, so its datacenter wins); else
    create a new one."""
    for v in runpod.list_network_volumes():
        if v.get("name") == vol_name:
            dc = v.get("dataCenterId")
            print(f"Reusing existing volume '{vol_name}' id={v['id']} "
                  f"({v.get('size')}GB, {dc})")
            if data_center and dc != data_center:
                print(f"  NOTE: existing volume is in {dc}; --data-center "
                      f"{data_center} ignored (volumes are region-locked).")
            return v["id"], dc, v.get("size"), False
    if not data_center:
        raise SystemExit("No existing volume and no --data-center given; "
                         "cannot choose a region. Pass --data-center.")
    print(f"Creating volume '{vol_name}' ({size}GB) in {data_center}...")
    v = runpod.create_network_volume(vol_name, size, data_center)
    print(f"  created id={v['id']}")
    return v["id"], v["dataCenterId"], v.get("size", size), True


def record(bare, full, vol_id, vol_name, dc, size, gpu, cloud, mount,
           vcpu=None, memory=None, disk=None, gpu_count=1, note=None):
    # Lock: pair_fleet runs several of these at once, and an unlocked
    # read-modify-write would drop other pods' entries.
    lock = open(REGISTRY + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    reg = {}
    if os.path.exists(REGISTRY):
        reg = json.load(open(REGISTRY))
    # recreate_cmd carries every flag not derivable from the (region-locked,
    # name-matched) volume, so pasting it rebuilds the pod faithfully.
    cmd = (f'create_volume_pod {bare} --volume-id {vol_id} --gpu-type "{gpu}" '
           f'--gpu-count {gpu_count} --vcpu {vcpu} --memory {memory} '
           f'--disk {disk} --cloud-type {cloud} --mount {mount}')
    reg[full] = {
        "network_volume_id": vol_id, "network_volume_name": vol_name,
        "data_center_id": dc, "volume_size_gb": size, "gpu_type": gpu,
        "gpu_count": gpu_count, "cloud_type": cloud, "mount_path": mount,
        "min_vcpu": vcpu, "min_memory_gb": memory, "container_disk_gb": disk,
        "note": note,
        "recreate_cmd": cmd,
    }
    json.dump(reg, open(REGISTRY, "w"), indent=2, sort_keys=True)
    open(REGISTRY, "a").write("\n")
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    print(f"Recorded {full} -> volume {vol_id} in {REGISTRY}")


def main():
    ap = argparse.ArgumentParser(description="Create a RunPod pod on a persistent network volume")
    ap.add_argument("name", help="bare pod name, e.g. oak")
    # Defaults are None so that a recreate can tell "not given" (use the
    # registry's recorded value) from an explicit override.
    ap.add_argument("--gpu-type", help='e.g. "NVIDIA A40" (required for a new pod)')
    ap.add_argument("--gpu-count", type=int)
    ap.add_argument("--data-center", help="e.g. US-NE-1 (required for a new volume)")
    ap.add_argument("--volume-size", type=int, default=200, help="GB (new volume only)")
    ap.add_argument("--volume-id", help="pin an existing volume id (skips name lookup)")
    ap.add_argument("--vcpu", type=int, help="min vCPUs for the pod (default 8)")
    ap.add_argument("--memory", type=int, help="min RAM GB (default 32)")
    ap.add_argument("--disk", type=int, help="ephemeral container disk GB (default 100)")
    ap.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY"], help="default SECURE")
    ap.add_argument("--mount", help="volume mount path (default /workspace)")
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds to keep retrying while the datacenter has no capacity")
    args = ap.parse_args()

    bare = to_bare(args.name)
    full = to_full(bare)
    vol_name = f"{full}-vol"
    if bare not in ast.literal_eval(os.environ["MACHINE_NAME_LIST"]):
        raise SystemExit(f"{bare} is not in MACHINE_NAME_LIST. The proxy only routes "
                         "listed names; add it to the END of the list in config.env first.")

    # Fill anything not given from this pod's registry entry (a recreate),
    # then from the defaults.
    rec = {}
    if os.path.exists(REGISTRY):
        rec = json.load(open(REGISTRY)).get(full, {})
    if rec:
        print(f"Using the recorded spec for {full} from network_volumes.json "
              "(flags you pass override it).")
    fill = {"gpu_type": ("gpu_type", None), "gpu_count": ("gpu_count", 1),
            "vcpu": ("min_vcpu", 8), "memory": ("min_memory_gb", 32),
            "disk": ("container_disk_gb", 100), "cloud_type": ("cloud_type", "SECURE"),
            "mount": ("mount_path", "/workspace"), "volume_id": ("network_volume_id", None)}
    for attr, (rkey, default) in fill.items():
        if getattr(args, attr) is None:
            setattr(args, attr, rec.get(rkey) if rec.get(rkey) is not None else default)
    if not args.gpu_type:
        raise SystemExit("New volume pod: pass --gpu-type (e.g. \"NVIDIA A40\"). "
                         "Recreates take it from network_volumes.json.")

    # Guard against duplicates: bail if the pod is already up.
    for p in runpod.get_pods() or []:
        if p.get("name") == full:
            raise SystemExit(f"{full} already exists (id={p.get('id')}). "
                             f"Nuke it first if you mean to recreate.")

    if args.volume_id:
        vol = next((v for v in runpod.list_network_volumes() if v["id"] == args.volume_id), None)
        if not vol:
            raise SystemExit(f"volume id {args.volume_id} not found")
        vol_id, dc, vol_size, created = vol["id"], vol["dataCenterId"], vol["size"], False
        vol_name = vol.get("name") or vol_name   # record the real (maybe shared) volume's name
    else:
        vol_id, dc, vol_size, created = find_or_create_volume(vol_name, args.volume_size, args.data_center)

    pubkey = read_pubkey()
    env = {"MACHINE_NAME": bare}
    if pubkey:
        env["PUBLIC_KEY"] = pubkey

    print(f"\nCreating pod {full}: {args.gpu_count}x {args.gpu_type} in {dc}, "
          f"{args.cloud_type}, {args.vcpu} vCPU / {args.memory}GB RAM, "
          f"volume {vol_id} @ {args.mount}")
    deadline = time.time() + args.timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            res = runpod.create_pod(
                name=full, image_name=IMAGE, gpu_type_id=args.gpu_type,
                gpu_count=args.gpu_count,
                cloud_type=args.cloud_type, data_center_id=dc,
                network_volume_id=vol_id, volume_mount_path=args.mount,
                volume_in_gb=0, container_disk_in_gb=args.disk,
                min_vcpu_count=args.vcpu, min_memory_in_gb=args.memory,
                start_ssh=True,
                ports="8888/http,22/tcp", docker_args=DOCKER_ARGS, env=env,
            )
            break
        except Exception as e:  # noqa: BLE001
            if runpod.is_capacity_error(e) and time.time() < deadline:
                if attempt % 10 == 1:
                    print(f"  no capacity in {dc} yet (attempt {attempt}); retrying...", flush=True)
                time.sleep(5)
                continue
            note = (f"\nThe volume {vol_name} ({vol_id}) was created for this pod and "
                    "still exists; it is billed for storage until deleted (see "
                    "NETWORK_VOLUMES.md). Re-running this command reuses it."
                    if created else "")
            raise SystemExit(f"Pod create failed after {attempt} attempt(s): {e}{note}")
    pod_id = res.get("id") if isinstance(res, dict) else None
    print(f"  created pod id={pod_id}")
    record(bare, full, vol_id, vol_name, dc, vol_size, args.gpu_type,
           args.cloud_type, args.mount, args.vcpu, args.memory, args.disk,
           args.gpu_count)
    print("\nNext: `ready_pods " + bare + "` (waits for the IP, updates the proxy, checks the pod).")


if __name__ == "__main__":
    main()
