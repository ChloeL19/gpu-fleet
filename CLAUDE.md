# Claude operating notes

This repo runs a fleet of GPU pods for a program: pods on RunPod (and optionally
Vast.ai), reached by participants through an nginx SSH proxy on an always-on box.
You (Claude) are usually invoked on that **proxy box** by an organizer, to provision
pods, diagnose problems, or run routine ops. Default to the shortcut commands,
default to small reversible actions, and never destroy a pod without explicit
approval.

**Clock.** The proxy box runs on UTC, so `date` won't show the organizer's local
time. When they say "at 9am", ask once which timezone they mean, and write it here
(below) so future sessions don't have to ask:

- Organizer timezone: America/New_York (Collider, NYC)

---

## Where to look first

| Question | Source |
|---|---|
| What is this, how is it set up? | `README.md` |
| How do I run a session well / avoid costly mistakes? | `OPS_PLAYBOOK.md` |
| A pod needs persistent storage | `NETWORK_VOLUMES.md` + `create_volume_pod` |
| Settings (names, GPU, image, proxy host) | `config.env` (never commit it) |
| API keys to hand out | `keys/*.csv` (gitignored) + `deploy_keys` |
| How scripts talk to RunPod | `management/runpod_compat.py`: a small REST v2 client (`https://api.runpod.io/v2`). Import it as `import runpod_compat as runpod`. **Never use the pip `runpod` SDK** — it speaks RunPod's GraphQL API, which is being retired. |

---

## Shortcut commands

Installed by `install.sh` as symlinks from `bin/` onto the PATH. Each is a thin
wrapper over a script in `management/` or `proxy/`. **Read this reference or the
script instead of running `--help`.**

A session's fleet, start to finish:
```bash
create_pods -n 12 --max-price 0.50   # retries until all 12 land (run it in the background)
ready_pods                           # per pod: wait for IP -> update_proxy -> check -> keys
# ... session ...
destroy_pods --all
```

**Never chain a create with `update_proxy` via `&&`.** Pods need minutes after
creation to get a public IP, and a proxy config generated before the IPs settle
silently leaves those pods out. `ready_pods` waits for each pod's IP first.

### Reference

**`create_pods`** — RunPod. Selection (one of): `-n N` makes sure the first N names
in `MACHINE_NAME_LIST` have pods (creating only the missing ones); `-a N` creates N
more on unused names; or positional names, bare (`alder`) or prefixed (`gpu-alder`).
A selection is required. Retries continuously, in parallel, rotating through every
NVIDIA GPU type at or under `--max-price` (per GPU, default 0.50), with
`RUNPOD_GPU_TYPE` tried first. Options: `--gpu-types "NVIDIA A40,NVIDIA L40S"` (only
these, still capped by `--max-price`), `--gpu-count N`, `--cloud COMMUNITY|SECURE`, `--image`, `--disk N`,
`--volume N`, `--data-centers US-NE-1,US-CA-2`, `--timeout` (default 1800s).
Defaults come from `config.env`. No prompt. Only names in `MACHINE_NAME_LIST` are
accepted. Prints `[OK] <pod> <- <gpu>` as each lands and a progress line every 15s;
expect hundreds of silent retries when capacity is scarce. Capacity errors, rate
limits and network/server errors are retried (after a lost response it checks
whether the pod got created before retrying); a request RunPod rejects outright
(bad field, auth, no credit) prints `[FAILED]` at once. If listing existing pods
fails it creates nothing. Exits 1 unless every pod landed. Ctrl-C stops all
workers and lists what was already created. Don't pipe its output through
anything that closes stdout early (`head`).

**`ready_pods [names...]`** — takes new pods to "ready", each independently and in
parallel: waits for a public IP, runs `update_proxy` (serialized), runs `check_pods`,
then `deploy_keys` if `keys/` has CSVs (`WITH_KEYS=0` to skip). No names = every pod
that exists. Prints `[name] READY` or `[name] FAILED <step>` per pod, plus a summary.
A FAILED pod is almost always a bad host: `destroy_pods <name> && create_pods <name>`,
then `ready_pods <name>`.

**`check_pods [names...]`** — SSH + CUDA check through the proxy. No args = every pod
that exists. Accepts bare names or single-letter shorthand for the first name with
that initial (`check_pods a m`). `cuda: NO` means a bad host.

**`destroy_pods`** — destroys pods on both providers, whatever state they're in.
`destroy_pods alder cedar`, or `--all` for every `<prefix>-*` pod (others on the
account are never touched), with `--exclude name...`. Lists what it will destroy
and asks first; `--yes` skips the prompt (for `at` jobs and scripts). Verifies
against the provider afterwards. Everything on a destroyed pod is lost; network
volumes survive.

**`list_pods`** — table of every pod on both providers, with total hourly spend
(running pods only). Rows follow `MACHINE_NAME_LIST` order, which is also the proxy
port order and the order of participants' SSH config.

**`pod_costs`** — per-pod uptime and estimated spend so far, from each provider's
real start time. est$ = rate × uptime (approximate).

**`create_vast_pods`** — Vast.ai (needs `VASTAI_API_KEY` in `config.env`). Same
`-n`/`-a`/names selection as `create_pods`, but tries once per name and asks
`(y/N)` before creating (pipe `printf 'y\n' |` when scripting). `--gpu-name RTX_3090`
(underscores), `--num-gpus`, `--disk`, `--image`, `--max-price` (default $0.60/hr).

**`create_volume_pod <name>`** — a SECURE pod on a persistent network volume; see
`NETWORK_VOLUMES.md`. A new pod needs `--gpu-type` (and `--data-center` for a new
volume); re-running with just the name rebuilds it from `network_volumes.json` on the
same volume. Retries while its datacenter has no capacity (`--timeout`, default 600s).

**`pair_fleet`** — one network-volume pod per *pair* of participants: N attendees
→ `ceil(N/2)` pods on the first names in `MACHINE_NAME_LIST`, each via
`create_volume_pod` (so each slot keeps `<prefix>-<name>-vol` across meetings).
Count from `--participants N`, or `--from-airtable` (rows in `AIRTABLE_VIEW_ID` with
`AIRTABLE_ATTENDED_FIELD` checked; `--planned` counts every row). `--assign` writes
each person's pod into `AIRTABLE_POD_FIELD`, two per pod in view order. Skips pods
that already exist, shows the plan and asks before creating (`--yes` skips,
`--dry-run` stops after the plan). GPU / datacenter / volume size default to
`PAIR_GPU_TYPE`, `PAIR_DATA_CENTER`, `PAIR_VOLUME_SIZE_GB`. Then `ready_pods`.
Volumes are never deleted by any script; see NETWORK_VOLUMES.md.

**`deploy_keys`** (→ `management/copy_api_keys.py`) — see below. `--pod <name>`
(bare or prefixed, repeatable) restricts it; `--max-parallel N` (default 30).
Exits 1 if any requested pod isn't online or any deploy failed.

**`update_proxy`** — regenerates the nginx config from live pod IPs and reloads nginx.
If the new config fails `nginx -t`, the old one stays in place.

**`ssh_config`** — prints the `~/.ssh/config` block participants install (one `Host`
per name, each pinned to its proxy port; `IdentityFile ~/.ssh/<key filename>`, and
`ServerAliveInterval 60` so idle sessions stay up). The proxy also sets
`proxy_timeout 24h`, since nginx's default would cut sessions idle for 10 minutes.

**`show_key`** — prints the shared private SSH key to hand to participants.

Scripts without a shortcut, run directly: `management/run_cmd.sh <cmd>` (run a
command on every pod), `management/gpu-top.sh` (live GPU use across the fleet),
`management/tree_relay.sh` (copy a model cache pod-to-pod),
`management/fleet_status.py` (one machine-readable line per name, for scripts),
`management/generate_openrouter_keys.py` and `management/openrouter_spend_report.py`
(per-pod OpenRouter keys and their spend). Stopping a pod (rarely useful, since it
keeps no data) is `runpod_compat.stop_pod(<id>)`.

---

## `deploy_keys`

Reads `keys/{openrouter,openai,anthropic,huggingface}_api_keys.csv` (two columns:
full pod name, key), finds each running pod's IP/port through the provider APIs, and
writes on each pod:

- `POD_ENV_FILE` (default `/root/.env`) — `KEY="value"` lines, atomic, mode 600.
- `~/.bashrc` and `~/.zshrc` — a marker-guarded block that sources it (idempotent).
- `/root/.ipython/profile_default/startup/00-load-env.py` — loads it into
  `os.environ` on every Jupyter/IPython kernel start.

The HF token is written as both `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN`. Offline pods
and missing CSVs are skipped silently.

- **"My notebook says `KeyError: OPENROUTER_API_KEY`"** → was `deploy_keys` run after
  the pod was created, and was the kernel restarted since? Check the pod has the file:
  `ssh <prefix>-<name> 'cut -c1-20 /root/.env'`.
- **Shared keys (one key for everyone)** → a CSV with one row per name, all the same
  key, saved as `keys/<provider>_api_keys.csv`, then `deploy_keys`. Ask the organizer
  for the key string — never invent one.
- **Per-pod OpenRouter keys** → `management/generate_openrouter_keys.py` mints one
  capped key per name in `MACHINE_NAME_LIST` into `keys/openrouter_api_keys.csv`
  (needs the provisioning key in `keys/.openrouter_provisioning_key`; `--limit` sets
  the $ cap per key, default 10). It checks the provisioning key first and won't
  replace an existing CSV without `--force`.

---

## Provisioning when availability is tight

This is the **normal** case, not the exception.

**What you'll see.** Individual create attempts fail with errors like:
- `There are no longer any instances available with the requested specifications.`
- `This machine does not have the resources to deploy your pod.`

Both mean "no capacity for that GPU type right now" — not a bug, not a config
problem. Capacity churns second to second, so the fix is to **retry hard and spread
across GPU types and providers**, which is what `create_pods` does.

**Why aggressive retrying is safe:**
1. **A failed RunPod create costs nothing and leaves no pod.** There is no
   half-created or billed-but-stuck state.
2. **Community prices are fixed per GPU type.** To stay under $X/hr, restrict which
   types you try. Roughly a dozen NVIDIA types (A4000, A5000, 3090, 4090, A6000, A40,
   …) are usually under $0.50/hr on community cloud, and `create_pods` reads the
   live price list itself. Don't pin a single type when supply is short. Skip AMD and
   MIG slices; the images are CUDA-only.
3. **Vast.ai is a second source**, but its cheap supply is often thin, and its query
   syntax is unreliable for filtering; filter offers in Python instead. Don't burn
   time forcing Vast for one marginal pod if RunPod is delivering.

Run `create_pods` in the background and watch it. Expect minutes and hundreds of
silent retries per pod when capacity is scarce — let it grind. If a type keeps landing
on bad hosts, steer around it with `--gpu-types`.

---

## Hard rules

- **Never** stop, delete or destroy pods without the organizer naming them. Pods can
  hold participants' active work, and a stopped pod keeps nothing.
- **Never** commit API keys. `config.env` and `keys/` are gitignored; keep it that way.
- **Never** hardcode a key in any script or doc.
- **Never** leave a destructive command waiting on an approval prompt in an unattended
  window: a pending prompt freezes the whole session, including anything scheduled.
- Before any command that could create or destroy pods in bulk, know how you would
  undo it.
