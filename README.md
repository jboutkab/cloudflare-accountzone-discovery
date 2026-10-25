# cf-discovery

Scans a Cloudflare **account + zone** for configured resources and prints the exact
[`cf-terraforming`](https://github.com/cloudflare/cf-terraforming#v5) commands needed to
export them — the discovery phase of a migration, automated.

## Requirements

- Python 3.8+ (**stdlib only** — no pip installs)
- A Cloudflare API token with read access to the account/zone
  (see [Token scopes](#token-scopes))
- [`cf-terraforming`](https://github.com/cloudflare/cf-terraforming) installed if you want
  to run the generated commands

## Usage

```bash
# Fully interactive — prompts for account ID, zone ID, token (token hidden)
python3 cf-discovery.py

# From a config file (default ~/.cloudflare-discovery.conf, or pass your own)
python3 cf-discovery.py
python3 cf-discovery.py --config prod.ini

# From CLI flags; --save persists them (mode 600) for next time
python3 cf-discovery.py -a <ACCOUNT_ID> -z <ZONE_ID> -t <TOKEN> -c prod.ini --save

# Also write the generated commands to a runnable shell script
python3 cf-discovery.py --write-sh export.sh

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
| `-q, --quiet` | Suppress probe progress on stderr |

## `--services` — one terraform state per product area

Normal generation is flat (`zone.tf` + `account.tf`). With `--services DIR`, the same
findings are split into **independent workspaces**, one per product area — every
directory holds its own `versions.tf`, a scoped `export.sh` (with a lazy
`terraform init` so it works standalone), and eventually its own `terraform.tfstate`.
That means you can apply, destroy and drift-check per service, and keep a smaller
blast radius:

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
├── networking/     # spectrum, tunnels, magic wan — apply LAST
│                   #   (spectrum needs an ACTIVE zone → post-cutover only)
└── export-all.sh   # runs the workspaces in dependency order
```

```bash
# one target account/zone pre-wired into every workspace:
python3 cf-discovery.py --services services/ \
    --target-account-id <acct2> --target-zone-id <zone2>

# per workspace:
cd services/dns && export CF_SOURCE_TOKEN=<source token> && bash setup.sh && terraform plan
```

Every workspace directory is self-contained — `provider.tf` + `variables.tf` +
`versions.tf`, a `terraform.tfvars` scaffold pre-filled with the target ids,
`.gitignore` (incl. `terraform.tfvars`, state, binaries), `export.sh`, and
`setup.sh` which runs the whole prepare sequence:

1. copies a `terraform` binary in (cf-terraforming needs it in the CWD)
2. runs `export.sh` the first time (lazy init; `setup.sh --fresh` re-exports)
3. remaps **SOURCE -> TARGET** zone/account ids inside the exported `*.tf`
4. (`security/` only) composes `imports.tf` by phase-matching the source zone's
   default ruleset entrypoints to the target zone's (`rulesets-import.py`)
5. `terraform init`

Token convention: `CF_SOURCE_TOKEN` = source account (exports + mappings),
`CLOUDFLARE_API_TOKEN` = target account (imports lookup + `terraform apply`).

Rule: any resource type that isn't explicitly mapped lands in a `misc/` directory,
so a new/unmapped service is never silently dropped.

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

```bash
python3 cf-discovery.py > out.txt            # progress goes to the terminal anyway
```

## What it does

1. **Verifies credentials** — calls `GET /accounts/:id` and `GET /zones/:id` first,
   fails early with the account/zone name if the token is bad.
2. **Probes ~40 endpoints** (zone-scoped and account-scoped) with **pagination**
   (per-endpoint page caps are handled — see below).
3. **Prints a mapping table** — every endpoint → finding → **cf-terraforming v5
   resource type** → action:

   ```
   endpoint               finding  v5 resource type                        action
   ---------------------  -------  --------------------------------------  ------------------------
   dns_records            327      cloudflare_dns_record                   export
   settings               56       cloudflare_zone_setting                 export via --resource-id
   waiting_rooms          5        cloudflare_waiting_room, ...settings    export (+ events via --resource-id)
   custom_pages           10       (no cf-terraforming v5 support)         manual — dashboard/API or v4-pin provider
   page_rules             HTTP 400 cloudflare_page_rule                    skip (HTTP 400) — legacy API may 400
   logpush/jobs           HTTP 403 cloudflare_logpush_job                  skip (HTTP 403) — needs Log Push perm
   ```

   Actions: `export` = included in the commands below, `export via --resource-id` =
   generated with a `--resource-id` flag, `manual` = no v5 support (listed at the end
   for the manual-migration checklist), `not configured` / `skip (err)` = omitted.
4. **Emits cf-terraforming commands** for every action marked `export`:
   - one **zone batch** command (`> zone.tf`)
   - a **zone settings** command with `--resource-id` **auto-built from the zone's
     editable settings**
   - **waiting-room events** and **list items** commands with `--resource-id`
     auto-filled from the discovered IDs
   - one **account batch** command (`> account.tf`)
5. Lists resources that need **manual migration** (cf-terraforming cannot export them):
   workers scripts (wrangler), worker routes, custom pages, custom SSL certs, managed
   headers, Magic IPsec tunnels.

`cloudflare_zone` is deliberately **not** included: cf-terraforming's zone generator
pulls **every** zone the token can see. Create the target zone manually and import it
(`terraform import cloudflare_zone.this <zone_id>`).

> The mapping table is useful on its own as a port of the official
> [cf-terraforming v5 supported resources](https://github.com/cloudflare/cf-terraforming#v5)
> to *this specific account* — it shows exactly which v5 resources your exports will produce.

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

Verified empirically against a live account (2026-10) so the commands actually
complete end-to-end:

1. **Batches abort mid-run on `--resource-id`-only resources.** If a bulk
   `--resource-type "a,b,c..."` batch includes a resource type that requires
   explicit ids, cf-terraforming dies with
   `No resource IDs defined in Terraform for resource X` — types sorted *after*
   the offender are silently missing from the output file. The script bans all 19
   such resource types from batches and emits dedicated commands instead.
2. **Comma-joined `--resource-id` values break.** cf-terraforming does *not*
   split `--resource-id "type=id1,id2"` — it puts the whole comma list into a
   single GET URL path segment (`/queues/<id1>,<id2>/consumers -> 400`).
   Commands therefore repeat `--resource-id "type=id"` **once per value**.
3. **A 404 on one id quietly skips the remaining ids** in a `--resource-id`
   run (e.g. tunnels without a config object). The script pre-filters such ids
   (probes `/cfd_tunnel/{id}/configurations` and only emits tunnels that have one).
4. **`cloudflare_origin_ca_certificate`'s generator is broken** (requests
   `/certificates` without the zone param) — banned from batches; export those
   certs via the API call shown in the mapping row instead.
5. Some resource-id values are **names, not uuids** (`cloudflare_pages_domain`
   takes the Pages *project name*; cron triggers / deployments / script
   subdomains take *script names*) — the script feeds the right identifier type.

## Pagination & wrapped responses

Counts are exact regardless of per-endpoint page caps (LB pools cap at 1000, rulesets
at 50, Access at 100, KV at 20, DNS at 100). A few endpoints (`pages/projects` and
Stream-era APIs) **reject `per_page` entirely** (400 / 8000024) — the script detects
this and automatically falls back to `?page=N`-only pagination. Wrapped list
responses (`r2/buckets` → `{buckets:[...]}`, `magic/routes` → `{routes:[...]}`,
`devices/dex_tests` → `{dex_tests:[...]}`) are unwrapped before counting. Settings
endpoints that return a single object are reported as `present`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 404 on **every** endpoint | `account_id`/`zone_id` empty or wrong | set the exports or the config file |
| `api_token invalid` / 400 | wrong/expired token | regenerate the token |
| 403 only on `logpush/jobs` | token lacks `Log Push` | add Log Push:Read (skip otherwise) |
| rulesets shows more than cf-terraforming exports | zone API returns zone-kind **+ managed** rulesets; only zone-kind is migratable | expected — cf-terraforming filters correctly |

## Exit codes

- `0` — discovery completed (some endpoints may still report errors inline; those are skipped)
- `1` — missing credentials, invalid token/account/zone, or `--non-interactive` with missing inputs