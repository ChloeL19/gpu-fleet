# Network-volume pods (persistent storage)

Most pods use an **ephemeral container disk** — stop or destroy the pod and its
data is gone. Some pods instead need data that survives a nuke + recreate
(training checkpoints, datasets, research projects that span several sessions). For those we
attach a RunPod **network volume**: a persistent disk that lives in one
datacenter and can be re-attached to a fresh pod later.

> **The two rules that make network volumes different from normal pods:**
> 1. A volume is **region-locked** — the pod must run in the **same datacenter**
>    as its volume (`data_center_id` == the volume's `dataCenterId`).
> 2. Volumes only exist in datacenters with storage support and only attach to
>    **SECURE-cloud** (datacenter) pods — not community pods. So these cost
>    secure-tier prices, with no cheaper community-cloud fallback.

Because the data is the whole point, **every network-volume pod is recorded in
[`management/network_volumes.json`](management/network_volumes.json)** so we
always know which volume (and datacenter) to recreate it against. Never
recreate one of these pods by hand from `create_pods` — you'll get a fresh
empty disk in the wrong region. Use the wrapper below.

## Create one

```bash
create_volume_pod oak \
    --gpu-type "NVIDIA H100 80GB HBM3" --data-center US-NE-1 \
    --volume-size 200 --vcpu 16 --memory 64
```

`create_volume_pod` (→ `management/create_volume_pod.py`) does three things:

1. **Find-or-create** a volume named `<prefix>-<name>-vol` (or uses the one you
   pin with `--volume-id`, e.g. a volume shared by several pods). An existing
   volume is reused, and its datacenter wins, since volumes are region-locked.
2. **Create a SECURE on-demand pod** in the volume's datacenter with the volume
   attached at `--mount` (default `/workspace`), plus the GPU you name
   (`--gpu-type` is required for a new pod), minimum vCPU/RAM (default 8 / 32 GB)
   and an ephemeral `--disk` (default 100 GB) for the OS/image. If the
   datacenter has no capacity it keeps retrying for `--timeout` (default 10 min).
3. **Record** the pod → volume mapping in `network_volumes.json`.

If the pod can't be created after a new volume was made, it says so: the volume
still exists and is billed until deleted, and re-running reuses it.

Then `ready_pods <name>` (waits for the IP, updates the proxy, checks the pod).

### Picking a datacenter

The volume and the GPU must live in the same datacenter, and not every
datacenter has both storage support and your GPU in stock. List the valid
intersection before creating:

```bash
cd management && python3 - <<'PY'
from mydotenv import load_env; load_env()
import os
import runpod_compat as runpod
runpod.api_key = os.getenv("RUNPOD_API_KEY")
GPU = "H100 80GB HBM3"   # substring to match
for dc in runpod._call("GET", "/catalog/datacenters?include=GPU_AVAILABILITY")["dataCenters"]:
    if not dc.get("networkVolumeTypes"):
        continue  # no network-volume support here
    for g in dc.get("gpuAvailability") or []:
        if GPU in (g.get("id") or "") and g.get("availability") != "NONE":
            print(dc["id"], g["id"], g.get("availability"))
PY
```

## One per pair of participants

`pair_fleet --participants 23` (or `--from-airtable`) runs `create_volume_pod` for
each of the first `ceil(N/2)` names, in parallel, using `PAIR_GPU_TYPE`,
`PAIR_DATA_CENTER` and `PAIR_VOLUME_SIZE_GB` from `config.env`. See CLAUDE.md.

## Recreate one on its existing data

Run it again with just the pod name. It rebuilds the pod from its entry in
`network_volumes.json`: the same volume (pinned by id, so a shared volume stays
shared), GPU, count, vCPU/RAM, disk, cloud and mount. Any flag you pass
overrides the recorded value, e.g. a bigger GPU on the same data:

```bash
create_volume_pod oak                                  # same spec, same data
create_volume_pod oak --gpu-type "NVIDIA A100-SXM4-80GB"   # same data, different GPU
```

Each entry also carries a `recreate_cmd` with every flag spelled out.

## Volumes are managed via REST v2

Everything RunPod goes through `management/runpod_compat.py` (REST v2 at
`https://api.runpod.io/v2`, Bearer = `RUNPOD_API_KEY`). Volume helpers:
`runpod_compat.list_network_volumes()` / `create_network_volume(name, size,
data_center_id)`; attach with `create_pod(..., network_volume_id=,
data_center_id=)`.

```bash
# list
curl -s https://api.runpod.io/v2/network-volumes -H "Authorization: Bearer $RUNPOD_API_KEY"
# create   POST {name,size,dataCenter}
# delete   DELETE /network-volumes/<id>
```

`create_volume_pod.py` wraps the create + list calls; deletion is manual and
**deliberately not automated** — deleting a volume destroys its data
irreversibly. A volume also keeps costing storage $ after its pod is gone, so
when a project truly ends, delete the volume by hand and drop its row from
`network_volumes.json`.

> **Only ever touch `<prefix>-*-vol` volumes** when cleaning up — filter to your
> `MACHINE_NAME_PREFIX` so you never delete a volume created outside this tooling.

## Advanced provisioning (scarce / multi-GPU / big-disk / seeding)

`create_pods` covers most of this already, with retries: SECURE cloud (`--cloud
SECURE`), multi-GPU (`--gpu-count`), specific types (`--gpu-types`) and big container
disks (`--disk`). Only a pod on a network volume needs `create_volume_pod`, which
retries while its datacenter has no capacity (`--timeout`, default 10 minutes) but
only for its one `--gpu-type`. To rotate across several GPU types for a volume pod,
drive `runpod_compat.create_pod` (REST v2) yourself in a **retry loop**. Every failed
create is free (no pod, no charge), so retrying hard is safe and correct:

```python
import runpod_compat as runpod    # REST v2 client; not the pip `runpod` SDK (GraphQL)
import create_volume_pod as cvp   # reuse IMAGE, DOCKER_ARGS, read_pubkey, find_or_create_volume, record
created = None
while time.time() < deadline and not created:
    try:                                   # ONLY the create in the try —
        res = runpod.create_pod(           # retrying after success would dupe
            name=full, image_name=cvp.IMAGE, gpu_type_id=gpu, gpu_count=N,
            cloud_type="SECURE",           # or COMMUNITY
            data_center_id=DC,             # required if attaching a volume
            network_volume_id=vol, volume_mount_path="/workspace", volume_in_gb=0,
            container_disk_in_gb=DISK,     # e.g. 500 for a no-volume fine-tune box
            min_vcpu_count=16, min_memory_in_gb=64,
            start_ssh=True, ports="8888/http,22/tcp",
            docker_args=cvp.DOCKER_ARGS, env={"MACHINE_NAME": bare, "PUBLIC_KEY": pub})
    except Exception as e:
        if runpod.is_capacity_error(e):       # "no capacity right now"
            time.sleep(3); continue
        ...
    created = res.get("id")
```
Rotate `gpu_type_id` across variants to widen availability (e.g. H100 `HBM3`→`NVL`→
`PCIe`; A100 prefer `A100-SXM4-80GB` over `80GB PCIe`, which draws the NO-IP bad host).
No-volume pods with big models need a big **container disk** (weights + checkpoints
live there) — a 2×H100 fine-tune box got 500 GB. Then run `ready_pods <name>`.

### Fresh volume + scarce GPU → race datacenters
A volume is region-locked, but a scarce GPU (H100 SXM, B200) may not exist in a
given DC. If the volume is NEW (no data to keep), create one volume **per candidate
DC** (same name), run one retry-create per (DC, volume), keep the first pod that
lands, terminate any duplicate, and delete the losing DCs' volumes. If the volume
already holds data you're pinned to its DC — just retry there.

### Seeding a new volume from another pod (cross-region)
Volumes can't be shared across regions, so "attach the same volume" is often
impossible — SEED a one-time copy instead. rsync source→dest over SSH
**agent-forwarding** so no private key lands on a pod:
```
eval "$(ssh-agent -s)"; ssh-add ~/.ssh/<shared_key>
ssh -A <prefix>-SRC "rsync -a -e 'ssh -o StrictHostKeyChecking=no -p <DEST_PORT>' \
    /workspace/DIR1 /workspace/DIR2 root@<DEST_IP>:/workspace/"
```
Don't copy re-downloadable HF caches cross-region — instead set `HF_HOME=/workspace/hf`
on the dest, `deploy_keys --pod <dest>` for the token, and let models re-download
from HF (faster than SSH, persists on the volume). If the source pod is in use
while you copy, run a second incremental rsync pass to catch files that changed.

## The volume registry

`management/network_volumes.json` is the live registry — read it rather than
any list written here. It is per-program run state, so it is **not tracked in
git**: a fresh clone has no registry, and `create_volume_pod` creates one the
first time it records a pod. Each entry the wrapper adds carries the volume name, datacenter, GPU, vCPU/RAM
and a ready-to-run `recreate_cmd`, which is everything needed to rebuild that
pod on its existing data.
