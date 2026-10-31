# gpu-fleet

Get a room full of people onto GPUs in minutes.

> **Based on [nickypro/arena-infra](https://github.com/nickypro/arena-infra)**, the
> infrastructure behind the [ARENA](https://www.arena.education) bootcamps. The whole
> design comes from it — pre-provisioned pods, one shared SSH key, and an nginx proxy
> that gives every pod name a permanent port — and so do many of the scripts here,
> some of them nearly unchanged. gpu-fleet is that project with the ARENA-specific
> parts removed and changes from running it for later programs added: see
> [Credits](#credits).

An organizer provisions one GPU pod per person (or pair) before a session starts.
Participants do a five-minute setup **once** and then connect with `ssh gpu-alder`,
or with VS Code / Cursor Remote-SSH, every time after that. No provider accounts, no
credit cards and no per-person dashboards for them. The organizer creates, checks and
tears down the whole fleet with a handful of commands.

It's built for programs with scheduled sessions: bootcamps, workshops, weekly
upskilling or research sprints. The trade-off is that participants don't manage
their own GPUs. The organizer (or Claude, running on the proxy box) does that for
them.

## How it works

![Participants connect through an nginx proxy to their GPU pods](docs/architecture.png)

- **Pods** run on [RunPod](https://www.runpod.io) (optionally also
  [Vast.ai](https://vast.ai)), named `<prefix>-<name>`: `gpu-alder`, `gpu-birch`, …
  from `MACHINE_NAME_LIST` in `config.env`.
- **One shared SSH key** is trusted by every pod, and every participant gets a copy.
- **An nginx proxy** on a small always-on server gives each *name* a fixed port
  (`gpu-alder` → port 16000, `gpu-birch` → 16001, …). Pods come and go with new IP
  addresses every time; `update_proxy` re-points the ports at whatever is live.
- So each participant's `~/.ssh/config` **never changes**: it maps each name to the
  proxy host and that name's port. Whoever sits at "alder" today runs `ssh gpu-alder`.

## What you need

- A **RunPod account** with credit and an API key (Settings → API Keys). Accounts are
  prepaid: turn on auto-top-up, or an empty balance stops every pod mid-session.
- An **always-on Linux server** with a public IP to be the proxy: any small VPS
  (1 vCPU / 1 GB is plenty) running Ubuntu 22.04 or 24.04. It needs inbound TCP open for one port per name, starting
  at `SSH_PROXY_STARTING_PORT` (16000–16029 for the default 30 names).
- Optional: a **Vast.ai** account and API key, as a second source of GPUs.

## One-time setup (organizer)

Run everything below on the proxy server **as root** (`sudo -i` first if you log in as another user).

```bash
# 1. Get the code
git clone <this repo's URL> ~/gpu-fleet && cd ~/gpu-fleet

# 2. Python environment for the scripts (standard library only, plus optional vastai)
bash proxy/install_python_venv.sh && source ~/.bashrc
pip install vastai                      # only if you'll use Vast.ai

# 3. nginx, set up as the SSH proxy
bash proxy/setup_nginx.sh

# 4. Settings
cp config.env.example config.env        # then edit it — see "Configuration" below

# 5. A new shared SSH key for this program (never reuse one from elsewhere)
ssh-keygen -t ed25519 -N "" -C "program shared key" -f ~/.ssh/program_shared
#    ...and make SHARED_SSH_KEY_PATH in config.env match that path.

# 6. Put the commands on your PATH (create_pods, ready_pods, list_pods, ...)
./install.sh

# 7. Let this server reach the pods the same way participants do
ssh_config >> ~/.ssh/config
```

Open the proxy port range in your server's firewall (for example
`ufw allow 16000:16029/tcp`, and the same in your cloud provider's firewall panel).

**Test it end to end with one pod** before inviting anyone:
```bash
create_pods alder --max-price 0.30
ready_pods alder         # want:  [alder] READY   (takes ~5 min)
destroy_pods alder
```

## One-time setup (participants)

Send each participant two things: the private key (`show_key` prints it) and the
config block (`ssh_config` prints it). Then they:

1. Save the key as `~/.ssh/<same filename as SHARED_SSH_KEY_PATH>`, e.g.
   `~/.ssh/program_shared`, and restrict it: `chmod 600 ~/.ssh/program_shared`
   (on Windows, the file must be readable only by their own user).
2. Paste the config block at the end of `~/.ssh/config` (create the file if needed).
3. Connect to the pod they're assigned: `ssh gpu-alder`. In VS Code or Cursor:
   *Remote-SSH: Connect to Host…* → `gpu-alder`.

That's it for the whole program. If the key or proxy host ever changes, they redo
steps 1–2.

A good way to catch setup problems early: a few days before the first session, start
one cheap pod (`create_pods alder --max-price 0.20`, then `ready_pods alder`) and ask
everyone to connect to it once. Destroy it afterwards; one cheap pod costs about
$4–5 a day.

## Each session

**Before people arrive** (pods take 5–20 minutes to become usable, so start early):
```bash
create_pods -n 12 --max-price 0.50   # 12 pods, any GPU type up to $0.50/hr; retries until all land
ready_pods                           # for each pod: wait for its IP, update the proxy,
                                     # check SSH + GPU, deploy API keys (if any in keys/)
```
`ready_pods` prints `[name] READY` or `[name] FAILED …` for each pod. A failed pod is
almost always a bad machine: `destroy_pods <name>`, `create_pods <name>`, then
`ready_pods <name>`. If one GPU type keeps failing, steer around it with `create_pods
--gpu-types`.

**After the session:**
```bash
destroy_pods --all     # lists every pod and asks before destroying them
```

> **Pods keep nothing when they're stopped or destroyed.** Tell participants to push
> their work to GitHub or HuggingFace before the session ends. For work that must
> persist between sessions, use a network volume (`create_volume_pod`, see
> `NETWORK_VOLUMES.md`).

## Commands

| Command | What it does |
|---|---|
| `create_pods` | Create pods, retrying across GPU types until each one lands. `-n N` (make sure the first N names have pods), `-a N` (N more), or names. `--max-price` caps $/hr per GPU; `--gpu-types`, `--gpu-count`, `--cloud SECURE`, `--disk` for bigger machines. |
| `ready_pods [names]` | For each new pod: wait for its IP, update the proxy, check SSH + GPU, deploy API keys. No names = all pods. |
| `check_pods [names]` | Check pods are reachable through the proxy and their GPU works. No names = all pods. |
| `destroy_pods` | Destroy pods (running or stopped) on both providers: `destroy_pods alder cedar` or `--all`. Asks first; `--yes` to skip. |
| `list_pods` | Every pod on both providers, with IPs and total hourly cost. |
| `pod_costs` | How long each pod has been up and roughly what it has cost. |
| `update_proxy` | Point the proxy at the current pod IPs (`ready_pods` runs it for you). |
| `deploy_keys` | Copy API keys from `keys/` onto every running pod (`ready_pods` runs it for you). |
| `ssh_config` | Print the SSH config block for participants. |
| `show_key` | Print the shared private key for participants. |
| `create_volume_pod` | Create a pod on a persistent network volume (see `NETWORK_VOLUMES.md`). |
| `pair_fleet` | One network-volume pod per pair of participants: `ceil(N/2)` pods, count from `--participants N` or Airtable (see `CLAUDE.md`). |
| `create_vast_pods` | Create pods on Vast.ai, as a second source of GPUs. |

Names can be bare (`alder`) or prefixed (`gpu-alder`). `CLAUDE.md` has the full option
reference, and a few less common scripts live in `management/`.

## API keys on the pods (optional)

To give each pod API keys (OpenRouter, OpenAI, Anthropic, HuggingFace), put
two-column CSVs in `keys/` — full pod name, then key:

```
gpu-alder,sk-or-v1-...
gpu-birch,sk-or-v1-...
```

The files are `keys/openrouter_api_keys.csv`, `openai_api_keys.csv`,
`anthropic_api_keys.csv` and `huggingface_api_keys.csv`. Any you don't need can be
left out. Then run `deploy_keys`. On each pod the keys land in `/root/.env`, are
loaded into every new shell, and are set in `os.environ` in every Jupyter kernel.

For per-pod OpenRouter keys with individual spending caps,
`management/generate_openrouter_keys.py` mints one per name from an OpenRouter
provisioning key (saved as `keys/.openrouter_provisioning_key`; `--limit` sets the cap),
and `management/openrouter_spend_report.py` reports their spend (and can
disable them at the end of the program).

## Configuration (`config.env`)

| Setting | Meaning |
|---|---|
| `RUNPOD_API_KEY`, `VASTAI_API_KEY` | Provider keys. Leave Vast blank to use RunPod only. |
| `SHARED_SSH_KEY_PATH` | The shared key (the `.pub` next to it is put on every pod). |
| `SSH_USER`, `SSH_PROXY_USER` | The login user on the pods (`root` for RunPod and Vast images); used by the tools and in participants' SSH config. |
| `MACHINE_NAME_PREFIX` | Pods are named `<prefix>-<name>`. Something short and specific to your program. |
| `MACHINE_NAME_LIST` | The names, in order. **Only ever add names at the end:** each name's position decides its proxy port, and participants' configs depend on it. |
| `RUNPOD_GPU_TYPE`, `RUNPOD_CLOUD_TYPE`, `RUNPOD_NUM_GPUS` | Defaults for `create_pods` (`RUNPOD_GPU_TYPE` is the type it tries first). |
| `RUNPOD_DOCKER_IMAGE` | Pod image. The default, `nickypro/arena-env`, is RunPod's PyTorch image with a conda environment (`arena-env`) of common interpretability and ML libraries preinstalled (`transformer_lens`, `transformers`, `datasets`, `einops`, `peft`, …), so pods are ready to work the moment they boot. It also contains a copy of the ARENA course in `/root/ARENA_3.0`, which is harmless if unused. For a smaller, plain image use `runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04` with `POD_PYTHON="python3"`. Any image works if it starts sshd from the `PUBLIC_KEY` environment variable, as RunPod's official images do. |
| `RUNPOD_DISK_SPACE_IN_GB`, `RUNPOD_VOLUME_SPACE_IN_GB` | Container disk and persistent pod volume sizes. |
| `POD_SETUP_CMD` | Optional shell command run at every pod start (e.g. `cd /root/course && git pull`). |
| `POD_PYTHON` | Python on the pods, used by `check_pods`' GPU check. Must match the image (`/opt/conda/envs/arena-env/bin/python` for the default). |
| `POD_ENV_FILE` | Where `deploy_keys` writes keys on each pod. |
| `KEY_COHORT` | Label for per-pod OpenRouter keys; change it each program. |
| `SSH_PROXY_HOST`, `SSH_PROXY_STARTING_PORT` | The proxy server's address, and the port for the first name. |

## Costs

You pay the provider per second for each running pod, plus a little for storage.
Cheap cards (RTX A4000 / A5000 / 3090) cost about $0.17–0.30/hr on RunPod's community
cloud, so 15 pods for a 5-hour session come to about $15–20. A100s and H100s cost
several times that per GPU, so they dominate any bill. Create them only when the work
needs them, and never leave them idle. `list_pods` shows the current hourly total;
`pod_costs` shows the cost so far. The proxy server is a few dollars a month.

## Security

- **Use a new shared SSH key for each program**, and never use it for anything else.
  Everyone holds it, and every pod trusts it.
- **Keys stay out of git**: `config.env` and `keys/` are gitignored.
- Prefer RunPod over Vast.ai's community hosts for anything sensitive. Community hosts
  are third-party machines.

## Files

```
bin/            the shortcut commands (install.sh symlinks these onto your PATH)
management/     the scripts behind them; runpod_compat.py is the RunPod API client
proxy/          nginx proxy: config generator, update script, one-time setup
docs/           diagram
config.env.example   copy to config.env
OPS_PLAYBOOK.md      how to run sessions well, and the costly mistakes to avoid
NETWORK_VOLUMES.md   persistent storage for pods
CLAUDE.md            notes for Claude (and a full command reference)
```

The scripts talk to RunPod's REST API (v2) directly through
`management/runpod_compat.py`. They don't need the `runpod` pip package.

## Credits

gpu-fleet is derived from **[nickypro/arena-infra](https://github.com/nickypro/arena-infra)**
by Nicky Pochinkov and contributors, the infrastructure behind the [ARENA](https://www.arena.education)
bootcamps. From arena-infra come the architecture (pods named from a fixed list, a shared
SSH key, and an nginx stream proxy mapping each name to a permanent port), the diagram, and
the core scripts: creating, listing, stopping and destroying pods, generating SSH configs,
and the nginx proxy configuration.

Changes since then, made while running it for later programs:
- `create_pods` retrying across GPU types when capacity is tight, and one
  `destroy_pods` for every pod state
- CUDA health checks (`check_pods`) and per-pod bring-up (`ready_pods`)
- Vast.ai support, network-volume pods, `deploy_keys`, per-pod OpenRouter keys, and cost
  reports
- a RunPod REST API v2 client (`runpod_compat.py`) replacing the pip SDK, whose GraphQL API
  RunPod is retiring
- the ARENA-specific parts taken out (the ARENA image and curriculum hooks, notebook
  patches, cohort tooling), and settings and docs made program-neutral

arena-infra has no license, so ask its author before sharing or reusing this code.
