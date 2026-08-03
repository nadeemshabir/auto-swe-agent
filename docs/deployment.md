# Deployment — Azure VM

How this system is deployed, why it ended up this shape, what went wrong getting
there, and how to change it afterwards.

Written 2026-08-03, from the actual deployment rather than from a plan.

---

## 1. What is running

| | |
|---|---|
| **URL** | `https://auto-swe-nadeem.centralindia.cloudapp.azure.com` |
| **Host** | Azure VM `auto-swe-vm`, Standard **B2pls_v2** — 2 vCPU, 4 GB, **arm64** |
| **Region** | Central India |
| **OS** | Ubuntu Server 24.04 LTS (arm64), Docker 29.7.1 |
| **Cost** | ~$16/mo VM + ~$5/mo disk, less whatever auto-shutdown saves |
| **Auto-shutdown** | 11:59 PM IST daily |

Five containers, from `docker-compose.yml` plus a VM-only
`docker-compose.override.yml`:

```
caddy      TLS termination, Let's Encrypt, :80 + :443 -> api:8000
api        FastAPI gateway — webhooks, read API, manual trigger
worker     Celery — the orchestrator (Planner -> Coder -> Reviewer)
postgres   system of record (runs, run_steps, webhook_events)
redis      Celery broker
```

Azure resources — everything else was deleted:

```
auto-swe-vm              the VM
auto-swe-vm-ip           public IP (the DNS label points here)
auto-swe-vm-nsg          firewall: 22, 80, 443 ONLY
auto-swe-vm-vnet         network
auto-swe-vm361           NIC
auto-swe-vm_key          SSH public key
auto-swe-vm_OsDisk_...   64 GB disk — holds the database
```

> **Do not delete the NSG or the disk.** The NSG is the only network protection
> on a host whose worker mounts the Docker socket. The disk holds Postgres.

---

## 2. Why a VM and not a container platform

The original plan targeted Azure Container Apps. It was abandoned partway, and
the reasoning is worth keeping because it is the sort of thing that looks like a
mistake later if undocumented.

**The sandbox needs a Docker daemon.** `agent/sandbox.py` doesn't just *run in* a
container — it *creates* them (`docker run`, `docker exec`) to give each run a
network-isolated box with a read-only host FS and `.git` masked. Container Apps,
ACI and App Service give you no daemon. On those platforms the sandbox has to be
abandoned, and untrusted repository code then executes right next to your GitHub
token and API keys.

**A multi-service stack with state is cheaper on one box.** Container Apps pushed
toward *managed* Postgres and *managed* Redis, since container platforms are
built for stateless workloads. Those two together cost more than the entire VM.
The "modern" path was the expensive one here.

**The count of moving parts.** The VM runs the same `docker-compose.yml` already
tested locally. One resource, one bill, no ingress configuration, no cascade
deletes.

The trade accepted: **you own the machine** — patching, firewall, TLS, backups.
`unattended-upgrades` handles security patches; Caddy handles TLS; backups are
still open (§7).

### Where the sandbox stands

`USE_SANDBOX=false` today. Runs execute repo tests **inside the worker
container**, alongside `GITHUB_TOKEN` and the LLM keys. Fine for first-party
repos behind `AGENT_REPO_ALLOWLIST`; **not fine** for code you did not write.

Turning it on is a config flip, not a code change — but see §6.4, because the
sandbox image must also carry the target repo's test dependencies.

---

## 3. Problems hit, and what they actually were

Every one of these cost real time. None would have shown up in local testing.

### 3.1 Azure Cache for Redis was retired mid-setup

```
Azure Cache for Redis is retiring, create Azure Managed Redis instead.
```

The replacement quoted **$25/month** — for something `plan2.md §7.2` describes as
"a pure broker with no correctness responsibility." Postgres is the system of
record; losing Redis loses in-flight queue state and nothing else.

**Resolution:** deleted it. On the VM, Redis is a container costing nothing extra.
*Lesson: check what a component actually guarantees before paying for guarantees.*

### 3.2 Container Apps: the wizard silently reused the wrong image

A Container App named `redis` was created running `auto-swe-api:v1`. The form had
retained "Azure Container Registry" from the previous app, and **Deployment
source** had defaulted to *Source code or artifact* rather than *Container image*.

It reported **Running**, because the api image starts uvicorn happily without a
database — it only fails when a request arrives. So "Running" proved nothing.

**Resolution:** read the app's JSON (`image`, `targetPort`, `transport`,
`external`, `minReplicas`) instead of trusting the status column.
*Lesson: a green status is not evidence the right thing is running.*

### 3.3 Deleting a Container Apps Environment deleted every app in it

```
ManagedEnvironmentNotReadyForAppCreation ... state 'ScheduledForDelete'
```

Removing the misconfigured app took the environment, and the environment took the
working api app with it.
*Lesson: delete the app, never the environment.*

### 3.4 VM creation looked blocked — it was an unregistered provider

The quota page was empty with a small banner: *"The selected provider is not
registered."* **`Microsoft.Compute` had never been registered** on the
subscription. Not a student-tier restriction, not a quota — a switch that had
never been flipped.

Registering it took two minutes and revealed **6 vCPUs available, 0 used**.
*Lesson: read the banner before believing the empty table.*

### 3.5 Every cheap VM size appeared unavailable

The size picker grouped them: *Unsupported availability zone*, *Incompatible with
Trusted launch virtual machines*. Two defaults were filtering everything cheap:

- **Security type: Trusted launch** — B-series is older hardware and doesn't
  support it → set to **Standard**
- **A pinned availability zone** → set **No infrastructure redundancy required**

Only `D2s_v3` at **$76.65/month** remained selectable. After both changes,
`B2pls_v2` at **~$16/month** appeared — a quarter of the price.

### 3.6 The 9.7 GB worker image — twice

**First time (x86):** the image carried **2.7 GB of NVIDIA CUDA packages**.
`requirements.txt` pins `sentence-transformers`, which pulls the default GPU
build of torch. Nothing here uses a GPU: the only local model is the MiniLM
embedder, on CPU, and every LLM call is a remote API request.

Fixed by installing torch from PyTorch's CPU index first: **9.74 GB → 3.19 GB**.

**Second time (arm64) — a wrong assumption in the fix itself.** The fix was
guarded to x86 only, on the reasoning that ARM Linux torch is CPU-only anyway.
That is outdated: PyPI publishes CUDA builds for aarch64 too (Grace/GH200-class
servers). The ARM build produced `torch 2.13.0+cu130`, 2.9 GB of nvidia, 652 MB
of triton — **9.75 GB**.

Verified empirically instead of reasoning again: `pip download` against the CPU
index on the ARM host returned a **148 MB aarch64 wheel**. Guard removed.
**9.75 GB → 3.03 GB.**

*Lesson: when a platform assumption is cheap to test, test it.*

### 3.7 A credential nearly reached GitHub

Push protection rejected a push: a timestamped `.env.bak` — created before
editing `.env` — had been swept in by `git add -A`. `.gitignore` covered `.env`
but not `.env.bak.*`.

Nothing leaked. `.gitignore` now covers `.env`, `.env.*` and `*.env`, verified
against `.env.bak.123`, `.env.local` and `prod.env`.

### 3.8 A healthcheck that could never pass

The worker sat "unhealthy" while working perfectly. The check pinged
`celery@$$HOSTNAME` — and in `sh`, `$$` is the **shell's PID**, so it resolved to
`celery@131HOSTNAME`, a node that has never existed.

It had been failing since it was written. Fixed by dropping `--destination`.

*Lesson: a healthcheck that can only fail is worse than none — it teaches you to
ignore container status, so the day the worker really dies you won't notice.*

### 3.9 WSL could not use the downloaded SSH key

The `.pem` landed in `/mnt/c/Users/.../Downloads`. Windows filesystems in WSL
can't hold Unix permissions, so `chmod 600` silently does nothing and SSH refuses
the key as "too open." Copy it into the WSL filesystem first:

```bash
cp "/mnt/c/Users/<you>/Downloads/auto-swe-vm_key.pem" ~/.ssh/
chmod 600 ~/.ssh/auto-swe-vm_key.pem
```

### 3.10 The VM built stale code

The VM clones from GitHub — which was two commits behind everything built that
day. It would have deployed code without the sandbox flag, the provider config,
or the image fix.

*Lesson: on a git-based deploy, "it works locally" is irrelevant. Push first.*

---

## 4. Making changes

### 4.1 Config only — `.env`

Models, budgets, allowlist, sandbox flag.

```bash
ssh -i ~/.ssh/auto-swe-vm_key.pem azureuser@auto-swe-nadeem.centralindia.cloudapp.azure.com
cd ~/auto-swe-agent
nano .env
docker compose up -d --force-recreate api worker
```

⚠️ **The restart is mandatory.** `AGENT_REPO_ALLOWLIST`, `USE_SANDBOX` and the
model settings are read **once at process start**. Editing `.env` alone changes
nothing, and the symptom is silent: webhooks return 204 and are ignored, looking
exactly like a broken webhook.

Confirm it took effect:

```bash
curl -s https://auto-swe-nadeem.centralindia.cloudapp.azure.com/readyz
# {"status":"ready","redis":"ok","db":"ok","sandbox":"off"}
docker exec auto-swe-api printenv AGENT_REPO_ALLOWLIST
```

### 4.2 Code changes

```bash
# laptop
python -m pytest tests/ -q          # 50 tests
git push origin master

# VM
cd ~/auto-swe-agent
git pull origin master
docker compose build                # only if deps/Dockerfile changed
docker compose up -d
```

Rebuild is only needed for `requirements.txt` or Dockerfile changes. Python source
is copied into the image, so a source change still needs a rebuild — but it's fast
because the dependency layer is cached.

### 4.3 Database schema changes

```bash
# laptop — create the migration
alembic revision -m "description"
# edit it, then test against SQLite
DATABASE_URL="sqlite:///$(pwd)/scratch.db" alembic upgrade head
git push origin master

# VM — the migrate container applies it on next start
git pull origin master && docker compose up -d
docker logs auto-swe-migrate
```

⚠️ **SQLite and PostgreSQL take different code paths.** Migration
`c4d1e88a5f27` uses `op.drop_constraint` on Postgres and `batch_alter_table`
elsewhere — the Postgres branch was untested until the first real deploy. Read
the `auto-swe-migrate` logs on the first run after any migration.

### 4.4 Adding a repository — three steps, in this order

```bash
# 1. allowlist, on the VM
nano ~/auto-swe-agent/.env      # append to AGENT_REPO_ALLOWLIST, comma-separated

# 2. restart — the step people miss
docker compose up -d --force-recreate api

# 3. webhook (laptop or VM)
bash scripts/setup_webhook.sh --repo owner/name \
  --url https://auto-swe-nadeem.centralindia.cloudapp.azure.com
bash scripts/setup_webhook.sh --repo owner/name --ping     # expect 204
```

The allowlist and the webhook do different jobs. The webhook is how GitHub
*tells* the server; the allowlist is the server deciding whether to *accept*.
Both are required — the allowlist alone does nothing.

Your `GITHUB_TOKEN` also needs **write** access, since the agent pushes a branch
and opens a PR.

### 4.5 Rotating secrets

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # AGENT_API_TOKEN
python3 -c "import secrets; print(secrets.token_hex(32))"       # GITHUB_WEBHOOK_SECRET
```

`GITHUB_WEBHOOK_SECRET` must match **wherever `setup_webhook.sh` runs from**, or
every delivery 403s. Simplest: change it on the VM and run the script there.

---

## 5. Daily operation

```bash
# start / stop (portal, or az CLI)
# stopping DEALLOCATES — compute billing stops, disk persists

# containers auto-start on boot (restart: unless-stopped), so
# starting the VM is the only step. ~2 minutes to healthy.

docker compose ps                       # status
docker compose logs -f worker           # watch a run
curl https://.../readyz                 # from anywhere
curl https://.../runs                   # run history
curl https://.../runs/<id>/steps        # full trace incl. tool calls
```

**Auto-shutdown at 11:59 PM IST** stops the VM. The webhook is dead until you
start it again. If a run is in flight it dies — `_reap_stale_runs()` marks the
row `stale` on the next run so the issue isn't blocked forever.

---

## 6. Known gaps

### 6.1 No database backups
Postgres lives in a Docker volume on one disk. Losing the VM loses all run
history. Fix: `pg_dump` on a cron to Azure Blob Storage, or managed Postgres.

### 6.2 Webhook runs are not rate limited
`RUNS_RATE_LIMIT` guards `POST /runs` only. **Anyone who can open an issue on an
allowlisted repo can start a run costing up to `MAX_USD`.** On a public repo that
is a real spending risk. Options: lower `MAX_USD`, or gate on a label by
restricting `_ACTIONABLE_ISSUE_ACTIONS` in `agent/github.py`.

### 6.3 Rate limiting is per-process
In-memory, so it does not survive a restart and would not be shared across
replicas. Fine for one API container.

### 6.4 Enabling the sandbox needs more than the flag
The sandbox has **no network** and runs a bare `python -m pytest` against the
*image's* site-packages. `docker/sandbox.Dockerfile` contains only pytest, so any
repo whose tests import anything will fail inside it. The image must carry the
target repo's dependencies — `plan2.md` DECISION D9, still open.

### 6.5 Docker socket = root on the host
`agent/sandbox.py` needs it, so the worker mounts it. A container escape is root
on this VM. Acceptable **only** because: the VM runs nothing else, the token is
scoped to the allowlist, and the NSG allows 22/80/443. If any stops being true,
revisit.

---

## 7. Rebuilding from scratch

1. VM: Ubuntu 24.04 **arm64**, B2pls_v2, Security type **Standard**, **no zone**,
   ports 22/80/443, 64 GB disk. Set a **DNS label** — free, survives IP changes,
   and Let's Encrypt will issue against it.
2. `curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER`
3. 2 GB swap, `unattended-upgrades`
4. `git clone`, create `.env` with **fresh** secrets
5. `Caddyfile` with the DNS name, and `docker-compose.override.yml` adding caddy
   and `--concurrency=1`
6. `docker compose up -d`, check `docker logs auto-swe-migrate`
7. `scripts/setup_webhook.sh --repo ... --url https://<dns>` then `--ping`

Roughly 30 minutes, most of it the ARM image build.
