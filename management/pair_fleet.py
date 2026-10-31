#!/usr/bin/env python3
"""Provision one network-volume pod per PAIR of participants for a meeting.

Participants work in pairs, so a meeting with N attendees needs ceil(N/2) pods.
Each pair's pod sits on its own persistent network volume (see NETWORK_VOLUMES.md),
so a pod can be destroyed after the meeting and rebuilt next time on the same data.

Pod slots are the first ceil(N/2) names in MACHINE_NAME_LIST, in order. A slot
keeps its volume across meetings ("gpu-alder" always mounts "gpu-alder-vol"), so
keep a pair on the same slot from one meeting to the next if their work should
follow them.

How many pairs (pick one):
  pair_fleet --participants 23           # -> 12 pods
  pair_fleet --from-airtable             # count "Attended" rows in the check-in view
  pair_fleet --from-airtable --planned   # count every row in the view (RSVPs)

Other options:
  --assign       after pods are requested, write each person's pod name into the
                 Airtable pod field (two people per pod, in the view's order)
  --dry-run      print the plan and stop
  --yes          skip the confirmation prompt (pods bill from the moment they exist)
  --gpu-type / --data-center / --volume-size / --timeout   override config.env

Then run `ready_pods` to bring them online and update the proxy.

Settings in config.env (see config.env.example): PAIR_GPU_TYPE, PAIR_DATA_CENTER,
PAIR_VOLUME_SIZE_GB, and, for --from-airtable / --assign, AIRTABLE_TOKEN,
AIRTABLE_BASE_ID, AIRTABLE_TABLE_ID, AIRTABLE_VIEW_ID, AIRTABLE_ATTENDED_FIELD,
AIRTABLE_POD_FIELD, AIRTABLE_NAME_FIELD.
"""
import argparse
import ast
import json
import math
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from mydotenv import load_env
load_env()
import runpod_compat as runpod
from pod_names import to_full

runpod.api_key = os.getenv("RUNPOD_API_KEY")
HERE = os.path.dirname(os.path.abspath(__file__))
AIRTABLE_API = "https://api.airtable.com/v0"


# --- Airtable (stdlib only) -------------------------------------------------

def _airtable_env(*keys):
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        raise SystemExit("Missing in config.env: " + ", ".join(missing))
    return [os.environ[k] for k in keys]


def _airtable(method, path, body=None):
    (token,) = _airtable_env("AIRTABLE_TOKEN")
    req = urllib.request.Request(
        f"{AIRTABLE_API}/{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Airtable {method} failed ({e.code}): {e.read().decode()[:300]}")


def airtable_people(planned):
    """Rows in the check-in view, in the view's order. Attended only, unless planned."""
    base, table, view = _airtable_env("AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID", "AIRTABLE_VIEW_ID")
    attended = os.getenv("AIRTABLE_ATTENDED_FIELD", "")
    name_field = os.getenv("AIRTABLE_NAME_FIELD", "Name")
    if not planned and not attended:
        raise SystemExit("Set AIRTABLE_ATTENDED_FIELD in config.env, or pass --planned.")
    rows, offset = [], None
    while True:
        q = [("view", view), ("pageSize", "100")]
        if not planned:
            q.append(("filterByFormula", "{%s}" % attended))
        if offset:
            q.append(("offset", offset))
        page = _airtable("GET", f"{base}/{table}?{urllib.parse.urlencode(q)}")
        rows += [(r["id"], r["fields"].get(name_field, r["id"])) for r in page["records"]]
        offset = page.get("offset")
        if not offset:
            return rows


def airtable_assign(people, slots):
    """Write pod names: people[0], people[1] -> slots[0]; people[2], people[3] -> slots[1]; ..."""
    base, table = _airtable_env("AIRTABLE_BASE_ID", "AIRTABLE_TABLE_ID")
    field = os.getenv("AIRTABLE_POD_FIELD")
    if not field:
        raise SystemExit("Set AIRTABLE_POD_FIELD in config.env to use --assign.")
    updates = [{"id": rid, "fields": {field: to_full(slots[i // 2])}}
               for i, (rid, _) in enumerate(people)]
    for i in range(0, len(updates), 10):          # Airtable caps PATCH at 10 records
        _airtable("PATCH", f"{base}/{table}", {"records": updates[i:i + 10]})
    for i, (_, name) in enumerate(people):
        print(f"  {name:<28} -> {to_full(slots[i // 2])}")


# --- Provisioning ------------------------------------------------------------

def create_one(bare, args):
    cmd = [sys.executable, os.path.join(HERE, "create_volume_pod.py"), bare,
           "--timeout", str(args.timeout)]
    if args.gpu_type:
        cmd += ["--gpu-type", args.gpu_type]
    if args.data_center:
        cmd += ["--data-center", args.data_center]
    if args.volume_size:
        cmd += ["--volume-size", str(args.volume_size)]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    return bare, r.returncode == 0, tail


def main():
    ap = argparse.ArgumentParser(description="One network-volume pod per pair of participants")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--participants", type=int, help="number of attendees")
    src.add_argument("--from-airtable", action="store_true",
                     help="count attendees in the Airtable check-in view")
    ap.add_argument("--planned", action="store_true",
                    help="with --from-airtable: count every row in the view, not just attended")
    ap.add_argument("--assign", action="store_true",
                    help="write each person's pod name back to Airtable (needs --from-airtable)")
    ap.add_argument("--gpu-type", default=os.getenv("PAIR_GPU_TYPE") or None)
    ap.add_argument("--data-center", default=os.getenv("PAIR_DATA_CENTER") or None)
    ap.add_argument("--volume-size", type=int,
                    default=int(os.getenv("PAIR_VOLUME_SIZE_GB") or 0) or None)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if args.assign and not args.from_airtable:
        ap.error("--assign needs --from-airtable")

    people = airtable_people(args.planned) if args.from_airtable else None
    n = len(people) if people is not None else args.participants
    if n <= 0:
        raise SystemExit("No participants counted; nothing to create.")
    pairs = math.ceil(n / 2)

    names = ast.literal_eval(os.environ["MACHINE_NAME_LIST"])
    if pairs > len(names):
        raise SystemExit(f"{pairs} pairs but only {len(names)} names in MACHINE_NAME_LIST; "
                         "append more names to the end of the list.")
    slots = names[:pairs]
    existing = {p.get("name") for p in (runpod.get_pods() or [])}
    todo = [b for b in slots if to_full(b) not in existing]

    print(f"{n} participants -> {pairs} pairs -> pods: {', '.join(map(to_full, slots))}")
    if len(todo) < len(slots):
        print(f"Already running (left alone): "
              f"{', '.join(to_full(b) for b in slots if b not in todo)}")
    print(f"To create: {len(todo)} SECURE pod(s), GPU {args.gpu_type or '(from registry)'}, "
          f"datacenter {args.data_center or '(volume / registry)'}, "
          f"volume {args.volume_size or 200} GB each (new volumes only)")

    if args.dry_run:
        return
    if todo and not args.yes:
        if input("Pods and volumes bill from the moment they exist. Create them? (y/N) ").strip().lower() != "y":
            raise SystemExit("Cancelled; nothing created.")

    failed = []
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            for bare, ok, tail in ex.map(lambda b: create_one(b, args), todo):
                print(f"[{'OK' if ok else 'FAILED'}] {to_full(bare)}")
                if not ok:
                    failed.append(bare)
                    for line in tail:
                        print(f"    {line}")

    if args.assign:
        print("\nWriting pod assignments to Airtable:")
        airtable_assign(people, slots)

    print("\nNext: `ready_pods " + " ".join(slots) + "`")
    if failed:
        print(f"Retry the failures with: pair_fleet ... (already-running pods are skipped), "
              f"or `create_volume_pod <name>` for each of: {' '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
