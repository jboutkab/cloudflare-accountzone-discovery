# cf-discovery

Scans a Cloudflare **account + zone** for every configured resource and produces ready-to-run [`cf-terraforming`](https://github.com/cloudflare/cf-terraforming#v5) exports — the **discovery phase of a zone/account migration, automated**.

## What it does

1. Probes **~130 API endpoints** (~50 zone + ~80 account, read-only **GET**s) to build a
   complete inventory of everything configured: DNS records, rulesets, load balancers,
   spectrum apps, tunnels, workers, KV, queues, Pages, R2, Access, Gateway policies, DLP,
   and ~25 more product areas.
2. Prints an **endpoint → cf-terraforming v5 mapping table** — for every finding: which
   v5 Terraform resource it becomes, and whether cf-terraforming can export it
   (`export`, `export via --resource-id`, `manual`, `derive`, `not configured`).
   Rows needing **manual migration** (workers scripts, custom pages, Magic IPsec, PAC
   files, …) are listed at the end so they show up on your checklist.
3. Emits **working cf-terraforming commands** — either flat (`zone.tf` + `account.tf`) or
   — with `--services DIR` — split into **independent Terraform workspaces, one per
   product area**, each self-contained and carrying its own state file.

Every command it generates is known to **complete successfully against a live account** —
repeated `--resource-id` flags instead of comma-joining, dedicated commands for the ~19
generator-quirk resource types, wrapped-response handling, batch abort prevention —
all handled for you (see [quirks](#cf-terraforming-quirks-the-generated-commands-already-handle)).

---

## Migrating to a new account / migrated zone — full workflow

Scenario: your zone lives on **source account A**, and you want it running on
**target account B** as a new (`pending`) zone that already exists there. Terraform
becomes the source of truth afterwards.

### 0. Prerequisites

- Python 3.8+ (**stdlib only**), `cf-terraforming` and `terraform` installed
- A **source token** with read access to account A (+ zone, see [Token scopes](#token-scopes))
- A **target token** with admin on account B + the new zone
- The new zone created on account B (status `pending` is fine for everything **except
  spectrum** — see step 6)

### 1. Discover + generate the workspaces

```bash
python3 cf-discovery.py \
    --account-id <ACCOUNT_A> --zone-id <ZONE_A> \
    --services services/ \
    --target-account-id <ACCOUNT_B> --target-zone-id <ZONE_B>
```

Two-stage picker (interactive): select the services you want, then the resource
types inside each. Non-interactive equivalents: `--services-only`, `--resources-only`,
`--non-interactive` (= everything).

This writes, per product area (e.g. `services/dns/`, `services/security/`):

| file | purpose |
|---|---|
| `export.sh` | service-scoped `cf-terraforming generate` commands |
| `setup.sh` | the one-shot prepare workflow (below) |
| `provider.tf` / `variables.tf` / `versions.tf` | provider reads the token from the env |
| `terraform.tfvars` | pre-filled with the **target** account/zone IDs (git-ignored) |
| `.gitignore` | state, plans, binaries, `terraform.tfvars` |
| `rulesets-import.py` | (`security/` only) builds `imports.tf` |
| `README.md` / `export-all.sh` | at the tree root |

### 2. Set the two tokens

```bash
export CF_SOURCE_TOKEN=<token for ACCOUNT_A>     # used for exports + import mapping
export CLOUDFLARE_API_TOKEN=<token for ACCOUNT_B> # used for terraform plan/apply
```

### 3. Prepare each workspace — `setup.sh`

```bash
cd services/dns && bash setup.sh        # or: bash services/export-all.sh  (all, in order)
```

`setup.sh` does, in order:

1. copies a `terraform` binary into the dir (cf-terraforming needs it in the CWD)
2. runs `export.sh` — **once** (`setup.sh --fresh` re-exports); cf-terraforming gets
   `CF_SOURCE_TOKEN`, i.e. it reads from the **source** account
3. **remaps** account/zone IDs inside the exported `*.tf` from source → target
4. only in `security/`: runs `rulesets-import.py` → writes `imports.tf`
5. `terraform init`

### 4. Import what already exists on the target

Some objects exist on every zone/account and must be **imported, never created** —
most importantly the default ruleset phase entrypoints. `security/import.tf` (generated
in step 3.4) pairs each exported ruleset block with the **target zone's** ruleset ID,
matched by phase:

```hcl
import {
  to = cloudflare_ruleset.terraform_managed_resource_<src_ruleset_id>_<n>   # ddos_l7
  id = "<ZONE_B>/<target_ruleset_id>"
}
```

Just review it and let `terraform apply` pick up the imports. Any later **409**
(conflict) means the object already exists on the target — add the matching `import`
block and re-run.

### 5. Plan + apply in dependency order

```text
account → dns → loadbalancing → ssl-tls → cdn → security → workers →
email → ai → observability → waiting-rooms → zero-trust → networking (later!)
```

```bash
# from the services/ root:
bash export-all.sh            # prepares every workspace in that order

cd services/dns          && terraform init && terraform plan && terraform apply
cd ../loadbalancing      && terraform init && terraform plan && terraform apply
# …through zero-trust
```

Terrform state grows **one file per service** (`services/<svc>/terraform.tfstate`) — a
broken apply in one area never touches the others, plans stay fast, and changing one
product re-plans only that directory.

### 6. Cut over DNS, then apply networking

`spectrum` applications **cannot be created on a pending zone** — Cloudflare refuses
(HTTP 400, error 16001 *"DNS configuration does not match authenticated zone"*). So:

1. point the domain's NS at the **target** zone (registrar change)
2. wait for the zone to become `active` (dashboard / `GET /zones/<ZONE_B>`)
3. then bring up the last workspace:

```bash
cd services/networking && terraform init && terraform plan && terraform apply
```

4. verify traffic/records parity, then decommission the source zone.

### 7. Manual items (cf-terraforming can't do these)

The run prints them explicitly; typically:

- **workers scripts** — `wrangler deploy` / API (never supported by cf-terraforming)
- worker **routes**, account-level **workers.dev subdomain** name
- **custom pages**, **custom (uploaded) SSL certs** — dashboard/API, or re-upload PEM
- **Magic IPsec tunnels**, **PAC files** — dashboard only
- R2 **custom/managed domains** — composite `--resource-id "<bucket>,<domain>"` hints
  are printed per bucket

---

## Requirements

- Python 3.8+ (**stdlib only** — no pip installs)
- A Cloudflare API token with read access to the account/zone
  (see [Token scopes](#token-scopes))
- [`cf-terraforming`](https://github.com/cloudflare/cf-terraforming) installed if you want
  to run the generated commands; `terraform` for the workspace workflow

## Usage

```bash
# Fully interactive — prompts for account ID, zone ID, token (token hidden)
python3 cf-discovery.py

# From a config file (default ~/.cloudflare-discovery.conf, or pass your own)
python3 cf-discovery.py
python3 cf-discovery.py --config prod.ini

# From CLI flags; --save persists them (mode 600) for next time
python3 cf-discovery.py -a <ACCOUNT_ID> -z <ZONE_ID> -t <TOKEN> -c prod.ini --save

# Flat output (the classic mode) — prints commands, optionally to a file
python3 cf-discovery.py --write-sh export.sh

# Per-service workspaces
python3 cf-discovery.py --services services/ \
    --target-account-id <ACCOUNT_B> --target-zone-id <ZONE_B>
python3 cf-discovery.py --services services/ --services-only dns,security
python3 cf-discovery.py --services services/ --resources-only cloudflare_ruleset,cloudflare_dns_record

# CI / scripted mode — fail instead of prompting when inputs are missing
python3 cf-discovery.py --non-interactive
```

### Inputs & resolution order

Each of `account_id`, `zone_id`, `api_token` is resolved in this order
(later sources win):

1. **Environment variables** — `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`
2. **INI config file** — `--config <path>` or `~/.cloudflare-discovery.conf`:

   ```ini
   [cloudflare]
   api_token  = YOUR_API_TOKEN
   account_id = YOUR_ACCOUNT_ID
   zone_id    = YOUR_ZONE_ID
   ```
3. **CLI flags** — `--account-id`, `--zone-id`, `--token`
4. **Interactive prompt** — anything still missing is asked (token via hidden input)

`--save` writes the resolved inputs back to the config file with `chmod 600`.

### Flags

| Flag | Purpose |
|---|---|
| `-a, --account-id` | Cloudflare account ID |
| `-z, --zone-id` | Cloudflare zone ID |
| `-t, --token` | API token |
| `-c, --config` | Config file path (default `~/.cloudflare-discovery.conf`) |
| `--save` | Persist resolved inputs to the config file (mode 600) |
| `--non-interactive` | Never prompt; fail if inputs are missing |
| `--write-sh FILE` | Write the generated commands to a runnable shell script |
| `--services DIR` | Write **one terraform workspace per product area** under `DIR/` (each with its own state) |
| `--target-account-id`, `--target-zone-id` | Pre-fill workspace tfvars + drive the source→target ID remap in `setup.sh` |
| `--services-only LIST` | Comma-separated services to generate — skips the picker (`dns,security`) |
| `--resources-only LIST` | Comma-separated resource **types** to include — skips the per-service type picker (`cloudflare_dns_record,cloudflare_ruleset`) |
| `-q, --quiet` | Suppress probe progress on stderr |

## What the output looks like

### The mapping table

```
endpoint                        finding          v5 resource type                      action
------------------------------------------------------------------------------------------------------------
dns_records                     327              cloudflare_dns_record                 export
rulesets                        24               cloudflare_ruleset                    export (zone-kind only)
spectrum/apps                   12               cloudflare_spectrum_application       export
tunnels                         50               cloudflare_zero_trust_tunnel_cloudflared ... export
pages/projects                  22 (+38 domains) cloudflare_pages_project,             export
                                                    cloudflare_pages_domain
logpush/jobs                    HTTP 403         cloudflare_logpush_job                skip (HTTP 403)
workers/scripts                 303              (no cf-terraforming v5 support)       manual — wrangler/API
```

Actions: `export` = in the generated commands, `export via --resource-id` = generated
with `--resource-id`, `manual` = no v5 support (printed in the manual list), 
`derive` = no collection list endpoint, `not configured` / `skip` = omitted.

### `--services` layout & picker

```text
services/
├── account/        # account, members, subscriptions
├── dns/            # DNS records, DNSSEC, transfers
├── loadbalancing/  # LBs + pools/monitors (+ healthchecks)
├── ssl-tls/        # cert packs, custom hostnames, Total TLS, mTLS …
├── cdn/            # caching, zone settings, snippets
├── security/       # rulesets, lists, API Shield, bot management …
├── workers/        # KV, queues, Pages, R2, Stream, cron/deployments …
├── email/          # Email Routing, Area 1
├── ai/             # AI Gateway, AI Search, Calls
├── observability/  # logpush, notifications …
├── waiting-rooms/
├── zero-trust/     # Access, Gateway, DLP, device posture …
├── networking/     # spectrum, tunnels, magic wan — apply LAST (post-cutover)
└── export-all.sh   # runs the workspaces in dependency order
```

In an interactive terminal a **two-stage point-and-shoot picker** asks:

1. **which services** — one checkbox list of every *configured* service
   (toggle with **space**; `a` = all, `n` = none, `enter` = confirm, `q` = abort)
2. **which resource types**, per selected service — a sequence of screens,
   `RESOURCE TYPES — <svc> · service i of N` (`enter` accepts the current
   checkmarks and moves to the next service). Batches are presented per type,
   dedicated-id exports show id counts.

Without a terminal it falls back to a numbered prompt; `--non-interactive` /
`--services-only` / `--resources-only` / `CF_DISCOVERY_PICKER=none` skip the UI.
Selecting zero types in a service skips its workspace. And any resource type
that isn't explicitly mapped lands in a `misc/` dir — never silently dropped.

## Progress tracking

During the ~130 endpoint probes (plus inner loops for tunnel configs, R2 bucket
domains, queue consumers, waiting-room rules — a full run takes 1-3 minutes)
a live progress line is written to **stderr** in-place:

```
[.. accounts  42/78] tunnels -> probing configs 17/50 ...      (in-flight)
[OK accounts  42/78] tunnels                                    50 (+41 configs)
[DONE accounts] 78 endpoints probed
```

stdout stays clean (mapping table + commands), so you can do:
`python3 cf-discovery.py -q > migration-plan.md`. `--quiet` silences stderr too.

## Token scopes

Minimum for discovery — read access everywhere you want scanned:

| Scope | Permissions |
|---|---|
| Zone | DNS, Config (rulesets), Load Balancing, Spectrum, SSL & Certificates, Waiting Rooms, Workers, Zone Settings — **Read** |
| Account | Load Balancing, Workers, Zero Trust (Access/Gateway), Rules, Notifications, DNS Firewall, Turnstile, mTLS — **Read** |
| Log Push | **Read** (optional — without it, `logpush/jobs` probes return 403 and are skipped) |
| Area 1 / WARP risk-scoring / Pages for Platforms | optional — unentitled probes return 400/403 and are shown as `skip` in the mapping table so you know what to ask Cloudflare about |

The script only performs **GET** requests; nothing is modified.

## cf-terraforming quirks the generated commands already handle

Verified empirically against a live account so the commands actually
complete end-to-end:

- **batch abort on resource-id-required types** — a batch containing
  `cloudflare_zone_setting`, `cloudflare_waiting_room_event`, tunnel configs,
  DLP profiles, … dies mid-output with `No resource IDs defined…`; these all go
  to their own **dedicated commands** (batch contains only safe bulk types)
- **`--resource-id` does not iterate comma-separated ids** — repeated flags are
  used instead (`--resource-id x=1 --resource-id x=2`); for >3 ids a tiny
  shell loop keeps the command one line
- **setting-list must include only editable settings** — `zone_setting` gets its
  `--resource-id` list from the zone's *editable* settings response (script-queried)
- **pages project listing rejects `per_page=1000`** after ~page 16 (`error 8000024`)
  → adaptive pagination fallback
- **`cloudflare_origin_ca_certificate` generator mis-includes `/certificates`
  without a zone param** (400) → surfaced as a note; export via API instead
- **`cloudflare_zero_trust_device_default_profile_certificates` soft-fails**
  (missing bindings on its only GET) → dashboard fallback note
- **tunnels may lack a config** (cURL install-only) → `tunnels/*/configurations`
  is pre-probed per tunnel before emitting `--resource-id` flags
- **`cloudflare_list_item` is not a safe batch type** → hint text only
- **`cloudflare_zone` is deliberately not exported** — the generator pulls every
  visible zone; create the target zone yourself: `terraform import cloudflare_zone.this <zone_id>`

## Distribution

Single file, stdlib Python — copy `cf-discovery.py` (+ `service_templates.py` for
`--services`) anywhere and run. Suggested: `pipx run`, `uv tool run`, or a small
wrapper script in your tooling repo.
