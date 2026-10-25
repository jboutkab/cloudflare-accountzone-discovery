#!/usr/bin/env python3
"""
cf-discovery — scan a Cloudflare account/zone for EVERY configured resource, print the
endpoint -> cf-terraforming v5 resource mapping, and emit the cf-terraforming
commands needed to export everything supported.

Coverage: built from the official cf-terraforming v5 supported-resources table
(github.com/cloudflare/cf-terraforming → Supported Resources → v5), ~140 resource
types. Every probe path was verified against a live account (2026-10).

Config resolution order (later overrides earlier):
  environment variables  ->  INI config file  ->  CLI flags  ->  interactive prompt

  ENV:   CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_ZONE_ID
  INI:   ~/.cloudflare-discovery.conf (or --config <path>)
         [cloudflare]
         api_token  = ...
         account_id = ...
         zone_id    = ...

Examples:
  python3 cf-discovery.py                        # fully interactive
  python3 cf-discovery.py --config prod.ini
  python3 cf-discovery.py -a <AID> -z <ZID> -t <TOKEN> --save
  python3 cf-discovery.py --write-sh export.sh

Only resource types that exist in the target AND are supported by
cf-terraforming v5 end up in the generated commands.
"""

import argparse
import configparser
import getpass
import json
import math
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
DEFAULT_CONFIG = os.path.expanduser("~/.cloudflare-discovery.conf")

# v5 resource types that MUST be exported individually via --resource-id
# (cf-terraforming aborts the whole batch with "No resource IDs defined in
# Terraform for resource X" if any of these lands in a plain generate batch.
# Source: official v5 table CLI-flags column + verified empirically 2026-10.)
RESOURCE_ID_REQUIRED = {
    "cloudflare_zone_setting",
    "cloudflare_list_item",
    "cloudflare_waiting_room_event",
    "cloudflare_waiting_room_rules",
    "cloudflare_authenticated_origin_pulls",            # needs origin hostname
    "cloudflare_hostname_tls_setting",                  # literal ciphers,min_tls_version
    "cloudflare_observatory_scheduled_test",            # needs hostname/page
    "cloudflare_web_analytics_rule",                    # needs site id
    "cloudflare_api_shield_operation_schema_validation_settings",  # operation ids
    "cloudflare_pages_domain",                          # project names
    "cloudflare_queue_consumer",                        # queue ids
    "cloudflare_r2_custom_domain",                      # bucket,domain pairs
    "cloudflare_r2_managed_domain",                     # bucket,domain pairs
    "cloudflare_workers_cron_trigger",                  # script names
    "cloudflare_workers_deployment",                    # script names
    "cloudflare_workers_script_subdomain",              # script names
    "cloudflare_origin_ca_certificate",                 # generator hits /certificates w/o zone param -> 400 (verified)
    "cloudflare_zero_trust_dlp_custom_profile",         # profile ids
    "cloudflare_zero_trust_dlp_predefined_profile",     # profile ids
    "cloudflare_zero_trust_tunnel_cloudflared_config",  # tunnel ids
}

# ---------------------------------------------------------------------------
# Service grouping for --services mode: one terraform state per product area.
# Every v5 resource type maps to exactly one service dir; unknown types land
# in "misc" so nothing can ever be dropped silently.
# ---------------------------------------------------------------------------
SERVICES = {
    "account":      ["cloudflare_account", "cloudflare_account_member",
                     "cloudflare_account_subscription", "cloudflare_user"],
    "dns":          ["cloudflare_dns_record", "cloudflare_zone_dnssec",
                     "cloudflare_registrar_domain", "cloudflare_dns_firewall",
                     "cloudflare_web3_hostname",
                     "cloudflare_dns_zone_transfers_acl",
                     "cloudflare_dns_zone_transfers_peer",
                     "cloudflare_dns_zone_transfers_tsig",
                     "cloudflare_dns_zone_transfers_incoming",
                     "cloudflare_dns_zone_transfers_outgoing"],
    "loadbalancing": ["cloudflare_load_balancer", "cloudflare_load_balancer_pool",
                      "cloudflare_load_balancer_monitor", "cloudflare_healthcheck"],
    "security":     ["cloudflare_ruleset", "cloudflare_zone_lockdown",
                     "cloudflare_filter", "cloudflare_rate_limit", "cloudflare_page_rule",
                     "cloudflare_bot_management", "cloudflare_managed_transforms",
                     "cloudflare_content_scanning_expression",
                     "cloudflare_leaked_credential_check",
                     "cloudflare_leaked_credential_check_rule",
                     "cloudflare_page_shield_policy",
                     "cloudflare_api_shield_operation",
                     "cloudflare_api_shield_schema",
                     "cloudflare_api_shield_discovery_operation",
                     "cloudflare_api_shield_schema_validation_settings",
                     "cloudflare_api_shield_operation_schema_validation_settings",
                     "cloudflare_list", "cloudflare_list_item",
                     "cloudflare_custom_pages"],
    "ssl-tls":      ["cloudflare_certificate_pack", "cloudflare_total_tls",
                     "cloudflare_hostname_tls_setting", "cloudflare_mtls_certificate",
                     "cloudflare_custom_hostname",
                     "cloudflare_custom_hostname_fallback_origin",
                     "cloudflare_origin_ca_certificate",
                     "cloudflare_keyless_certificate",
                     "cloudflare_authenticated_origin_pulls",
                     "cloudflare_authenticated_origin_pulls_certificate",
                     "cloudflare_custom_certificates"],
    "networking":   ["cloudflare_spectrum_application",
                     "cloudflare_zero_trust_tunnel_cloudflared",
                     "cloudflare_zero_trust_tunnel_cloudflared_route",
                     "cloudflare_zero_trust_tunnel_cloudflared_virtual_network",
                     "cloudflare_zero_trust_tunnel_cloudflared_config",
                     "cloudflare_magic_wan_static_route",
                     "cloudflare_regional_hostname"],
    "cdn":          ["cloudflare_argo_smart_routing", "cloudflare_argo_tiered_caching",
                     "cloudflare_tiered_cache", "cloudflare_regional_tiered_cache",
                     "cloudflare_zone_cache_reserve", "cloudflare_zone_cache_variants",
                     "cloudflare_url_normalization_settings",
                     "cloudflare_zone_setting",
                     "cloudflare_snippets", "cloudflare_snippet_rules"],
    "workers":      ["cloudflare_workers_kv_namespace",
                     "cloudflare_workers_custom_domain",
                     "cloudflare_workers_for_platforms_dispatch_namespace",
                     "cloudflare_workers_script_subdomain",
                     "cloudflare_workers_cron_trigger", "cloudflare_workers_deployment",
                     "cloudflare_queue", "cloudflare_queue_consumer",
                     "cloudflare_d1_database",
                     "cloudflare_r2_bucket", "cloudflare_r2_custom_domain",
                     "cloudflare_r2_managed_domain",
                     "cloudflare_pages_project", "cloudflare_pages_domain",
                     "cloudflare_stream", "cloudflare_stream_key",
                     "cloudflare_stream_live_input", "cloudflare_stream_watermark",
                     "cloudflare_stream_webhook",
                     "cloudflare_turnstile_widget",
                     "cloudflare_web_analytics_site", "cloudflare_web_analytics_rule",
                     "cloudflare_workers_scripts", "cloudflare_workers_routes"],
    "zero-trust":   ["cloudflare_zero_trust_access_application",
                     "cloudflare_zero_trust_access_group",
                     "cloudflare_zero_trust_access_identity_provider",
                     "cloudflare_zero_trust_access_service_token",
                     "cloudflare_zero_trust_access_policy",
                     "cloudflare_zero_trust_access_tag",
                     "cloudflare_zero_trust_access_custom_page",
                     "cloudflare_zero_trust_access_key_configuration",
                     "cloudflare_zero_trust_access_short_lived_certificate",
                     "cloudflare_zero_trust_access_mtls_certificate",
                     "cloudflare_zero_trust_access_mtls_hostname_settings",
                     "cloudflare_zero_trust_organization",
                     "cloudflare_zero_trust_access_infrastructure_target",
                     "cloudflare_zero_trust_gateway_policy",
                     "cloudflare_zero_trust_gateway_certificate",
                     "cloudflare_zero_trust_gateway_proxy_endpoint",
                     "cloudflare_zero_trust_gateway_settings",
                     "cloudflare_zero_trust_list", "cloudflare_zero_trust_gateway_list",
                     "cloudflare_zero_trust_dns_location",
                     "cloudflare_zero_trust_device_default_profile",
                     "cloudflare_zero_trust_device_default_profile_certificates",
                     "cloudflare_zero_trust_device_default_profile_local_domain_fallback",
                     "cloudflare_zero_trust_device_custom_profile",
                     "cloudflare_zero_trust_device_managed_networks",
                     "cloudflare_zero_trust_device_posture_rule",
                     "cloudflare_zero_trust_device_posture_integration",
                     "cloudflare_zero_trust_dex_test",
                     "cloudflare_zero_trust_risk_behavior",
                     "cloudflare_zero_trust_risk_scoring_integration",
                     "cloudflare_zero_trust_dlp_dataset",
                     "cloudflare_zero_trust_dlp_custom_profile",
                     "cloudflare_zero_trust_dlp_predefined_profile"],
    "observability": ["cloudflare_notification_policy",
                      "cloudflare_notification_policy_webhooks",
                      "cloudflare_logpush_job", "cloudflare_logpull_retention",
                      "cloudflare_observatory_scheduled_test"],
    "waiting-rooms": ["cloudflare_waiting_room", "cloudflare_waiting_room_settings",
                      "cloudflare_waiting_room_event", "cloudflare_waiting_room_rules"],
    "email":        ["cloudflare_email_routing_address",
                     "cloudflare_email_routing_settings",
                     "cloudflare_email_routing_dns",
                     "cloudflare_email_routing_rule",
                     "cloudflare_email_routing_catch_all",
                     "cloudflare_email_security_block_sender",
                     "cloudflare_email_security_impersonation_registry",
                     "cloudflare_email_security_trusted_domains"],
    "ai":           ["cloudflare_ai_gateway", "cloudflare_ai_search_instance",
                     "cloudflare_ai_search_namespace", "cloudflare_ai_search_token",
                     "cloudflare_calls_sfu_app", "cloudflare_calls_turn_app"],
}
TYPE_TO_SERVICE = {}
for _svc, _types in SERVICES.items():
    for _t in _types:
        TYPE_TO_SERVICE.setdefault(_t, _svc)

# ---------------------------------------------------------------------------
# Endpoint maps: (path, [v5 resource types] | [] | None, note)
#   []       => no cf-terraforming v5 support -> manual migration
#   None     => special-cased in scan():  SPECIAL:<key>[:<wrap_key>]
#   "note:"  => no API call possible (no collection list endpoint); informational.
#   "@..."   => absolute path under the API root (not account/zone-relative);
#              may contain {zone_id} / {account_id} placeholders.
# ---------------------------------------------------------------------------
ZONE_ENDPOINTS = [
    ("dns_records",            ["cloudflare_dns_record"],                ""),
    ("rulesets",               ["cloudflare_ruleset"],                   "zone-kind only (managed rulesets are system-owned)"),
    ("load_balancers",         ["cloudflare_load_balancer"],             ""),
    ("spectrum/apps",          ["cloudflare_spectrum_application"],      ""),
    ("custom_hostnames",       ["cloudflare_custom_hostname"],           ""),
    ("custom_hostnames/fallback_origin", ["cloudflare_custom_hostname_fallback_origin"], ""),
    ("healthchecks",           ["cloudflare_healthcheck"],               ""),
    ("settings",               None,                                     "SPECIAL:zone_settings"),
    ("dnssec",                 ["cloudflare_zone_dnssec"],               ""),
    ("waiting_rooms",          None,                                     "SPECIAL:waiting_rooms"),
    ("ssl/certificate_packs",  ["cloudflare_certificate_pack"],          ""),
    ("@certificates?zone_id={zone_id}", ["cloudflare_origin_ca_certificate"], "user-level Origin CA certs for this zone (note: cf-terraforming's generate for this type mis-requests /certificates without zone param — export via API instead)"),
    ("bot_management",         ["cloudflare_bot_management"],            ""),
    ("url_normalization",      ["cloudflare_url_normalization_settings"], ""),
    ("argo/tiered_caching",    ["cloudflare_argo_tiered_caching", "cloudflare_tiered_cache"], "tiered_cache = newer resource name for the same setting"),
    ("argo/smart_routing",     ["cloudflare_argo_smart_routing"],        ""),
    ("snippets",               ["cloudflare_snippets", "cloudflare_snippet_rules"], ""),
    # ---- additional v5 coverage (verified against live API, 2026-10) ----
    ("content-upload-scan/payloads",    ["cloudflare_content_scanning_expression"], ""),
    ("managed_headers",                 ["cloudflare_managed_transforms"],            ""),
    ("email/routing/settings",          ["cloudflare_email_routing_settings"],  "404 = email routing disabled on zone"),
    ("email/routing/dns",               ["cloudflare_email_routing_dns"],       ""),
    ("email/routing/rules",             ["cloudflare_email_routing_rule"],      ""),
    ("email/routing/rules/catch_all",   ["cloudflare_email_routing_catch_all"], ""),
    ("acm/total_tls",                   ["cloudflare_total_tls"],               ""),
    ("cache/cache_reserve",             ["cloudflare_zone_cache_reserve"],      ""),
    ("cache/regional_tiered_cache",     ["cloudflare_regional_tiered_cache"],   ""),
    ("firewall/lockdowns",              ["cloudflare_zone_lockdown"],           ""),
    ("api_gateway/operations",          ["cloudflare_api_shield_operation"],    ""),
    ("api_gateway/schemas",             ["cloudflare_api_shield_schema"],       ""),
    ("api_gateway/discovery/operations",["cloudflare_api_shield_discovery_operation"], ""),
    ("api_gateway/settings/schema_validation",
        ["cloudflare_api_shield_schema_validation_settings",
         "cloudflare_api_shield_operation_schema_validation_settings"], ""),
    ("origin_tls_client_auth",          ["cloudflare_authenticated_origin_pulls"], ""),
    ("origin_tls_client_auth/hostnames/certificates", ["cloudflare_authenticated_origin_pulls_certificate"], ""),
    ("keyless_certificates",            ["cloudflare_keyless_certificate"],     "keyless SSL"),
    ("leaked-credential-checks",        ["cloudflare_leaked_credential_check"], ""),
    ("leaked-credential-checks/detections", ["cloudflare_leaked_credential_check_rule"], ""),
    ("logs/control/retention/flag",     ["cloudflare_logpull_retention"],       ""),
    ("page_shield/policies",            ["cloudflare_page_shield_policy"],      ""),
    ("web3/hostnames",                  ["cloudflare_web3_hostname"],           ""),
    ("filters",                         ["cloudflare_filter"],                  "legacy API may 410 — rulesets supersede filters"),
    ("rate_limits",                     ["cloudflare_rate_limit"],              "legacy API may 410 — ruleset http_ratelimit supersedes"),
    ("page_rules",                      ["cloudflare_page_rule"],               "legacy API may 400"),
    ("logpush/jobs",                    ["cloudflare_logpush_job"],             "needs Log Push perm"),
    # ---- v5 resources with no collection-list endpoint (derivation notes) ----
    ("note:", ["cloudflare_zone_cache_variants"],
        "zone setting 'cache_variants' — covered by the settings probe above"),
    ("note:", ["cloudflare_hostname_tls_setting"],
        "no collection list — per-hostname setting (dashboard: SSL/TLS -> Hostname)"),
    ("note:", ["cloudflare_observatory_scheduled_test"],
        "no account-wide list — scheduled per page URL (dashboard: Observatory)"),
    ("note:", ["cloudflare_regional_hostname"],
        "data-localization/regional-hostnames has no list endpoint — check dashboard (DNS -> Data Localization)"),
    ("note:", ["cloudflare_dns_zone_transfers_incoming", "cloudflare_dns_zone_transfers_outgoing"],
        "per-zone secondary DNS state (see account secondary_dns ACL/peer/TSIG probes)"),
    # ---- present but NOT supported by cf-terraforming v5 -> manual ----
    ("custom_pages",           [],     "dashboard/API or v4-pin provider"),
    ("custom_certificates",    [],     "re-upload PEM/key (no cloudflare_custom_ssl in v5 table)"),
    ("workers/routes",         [],     "cloudflare_worker_route not in v5"),
]

ACCOUNT_ENDPOINTS = [
    ("load_balancers/pools",       ["cloudflare_load_balancer_pool"],    ""),
    ("load_balancers/monitors",    ["cloudflare_load_balancer_monitor"], ""),
    ("tunnels",                    ["cloudflare_zero_trust_tunnel_cloudflared",
                                    "cloudflare_zero_trust_tunnel_cloudflared_virtual_network",
                                    "cloudflare_zero_trust_tunnel_cloudflared_route"], ""),
    ("workers/domains",            ["cloudflare_workers_custom_domain"], ""),
    ("storage/kv/namespaces",      ["cloudflare_workers_kv_namespace"],  ""),
    ("workers/dispatch/namespaces",["cloudflare_workers_for_platforms_dispatch_namespace"],
                                       "enabled on entprise plans"),
    ("workers/subdomain",          [],  "account workers.dev subdomain name — no dedicated v5 export resource (per-script subdomains are exported below)"),
    ("access/apps",                ["cloudflare_zero_trust_access_application"], ""),
    ("access/groups",              ["cloudflare_zero_trust_access_group"], ""),
    ("access/identity_providers",  ["cloudflare_zero_trust_access_identity_provider"], ""),
    ("access/service_tokens",      ["cloudflare_zero_trust_access_service_token"], ""),
    ("access/policies",            ["cloudflare_zero_trust_access_policy"], ""),
    ("access/tags",                ["cloudflare_zero_trust_access_tag"], ""),
    ("access/custom_pages",        ["cloudflare_zero_trust_access_custom_page"], ""),
    ("access/keys",                ["cloudflare_zero_trust_access_key_configuration"], ""),
    ("access/apps/ca",             ["cloudflare_zero_trust_access_short_lived_certificate"], ""),
    ("access/certificates",        ["cloudflare_zero_trust_access_mtls_certificate"], ""),
    ("access/certificates/settings", ["cloudflare_zero_trust_access_mtls_hostname_settings"], ""),
    ("access/organization",        ["cloudflare_zero_trust_organization"], "404 = ZT org not provisioned"),
    ("infrastructure/targets",     ["cloudflare_zero_trust_access_infrastructure_target"], ""),
    ("rules/lists",                None,                                 "SPECIAL:lists"),
    ("rulesets",                   ["cloudflare_ruleset"],               "account kind"),
    ("alerting/v3/policies",       ["cloudflare_notification_policy"],   ""),
    ("alerting/v3/destinations/webhooks", ["cloudflare_notification_policy_webhooks"], ""),
    ("logpush/jobs",               ["cloudflare_logpush_job"],           "needs Log Push perm"),
    ("dns_firewall",               ["cloudflare_dns_firewall"],          ""),
    ("subscriptions",              ["cloudflare_account_subscription"],  ""),
    ("addressing/address_maps",    ["cloudflare_address_map"],           ""),
    ("ai-gateway/gateways",        ["cloudflare_ai_gateway"],            ""),
    ("ai-search/instances",        ["cloudflare_ai_search_instance"],    "AutoRAG"),
    ("ai-search/namespaces",       ["cloudflare_ai_search_namespace"],   ""),
    ("ai-search/tokens",           ["cloudflare_ai_search_token"],       ""),
    ("calls/apps",                 ["cloudflare_calls_sfu_app", "cloudflare_calls_turn_app"], "combined SFU/TURN list"),
    ("d1/database",                ["cloudflare_d1_database"],           ""),
    ("secondary_dns/acls",         ["cloudflare_dns_zone_transfers_acl"],   ""),
    ("secondary_dns/peers",        ["cloudflare_dns_zone_transfers_peer"],  ""),
    ("secondary_dns/tsigs",        ["cloudflare_dns_zone_transfers_tsig"],  ""),
    ("email/routing/addresses",    ["cloudflare_email_routing_address"], ""),
    ("email_security/block_senders",          ["cloudflare_email_security_block_sender"],          "Area 1 entitlement"),
    ("email_security/impersonation_registry", ["cloudflare_email_security_impersonation_registry"], ""),
    ("email_security/trusted_domains",        ["cloudflare_email_security_trusted_domains"],       ""),
    ("magic/routes",               None, "SPECIAL:wrap:routes->cloudflare_magic_wan_static_route"),
    ("pages/projects",             None, "SPECIAL:pages"),
    ("queues",                     None, "SPECIAL:queues"),
    ("r2/buckets",                 None, "SPECIAL:r2"),
    ("registrar/domains",          ["cloudflare_registrar_domain"],      ""),
    ("stream",                     ["cloudflare_stream"],                "video count"),
    ("stream/keys",                ["cloudflare_stream_key"],            ""),
    ("stream/live_inputs",         ["cloudflare_stream_live_input"],     ""),
    ("stream/watermarks",          ["cloudflare_stream_watermark"],      ""),
    ("devices/policy",             ["cloudflare_zero_trust_device_default_profile",
                                    "cloudflare_zero_trust_device_default_profile_local_domain_fallback",
                                    "cloudflare_zero_trust_device_default_profile_certificates"], "default WARP profile (certificates subresource may soft-fail on export — dashboard fallback)"),
    ("devices/networks",           ["cloudflare_zero_trust_device_managed_networks"], ""),
    ("devices/posture",            ["cloudflare_zero_trust_device_posture_rule"], ""),
    ("devices/posture/integration",["cloudflare_zero_trust_device_posture_integration"], ""),
    ("devices/dex_tests",          None, "SPECIAL:wrap:dex_tests->cloudflare_zero_trust_dex_test"),
    ("risk-score-behaviors",       ["cloudflare_zero_trust_risk_behavior"], "needs WARP risk-scoring entitlement"),
    ("dlp/profiles",               ["cloudflare_zero_trust_dlp_custom_profile",
                                    "cloudflare_zero_trust_dlp_predefined_profile"], "combined list, split by profile type"),
    ("dlp/datasets",               ["cloudflare_zero_trust_dlp_dataset"], ""),
    ("gateway/locations",          ["cloudflare_zero_trust_dns_location"], ""),
    ("gateway/certificates",       ["cloudflare_zero_trust_gateway_certificate"], ""),
    ("gateway/proxy_endpoints",    ["cloudflare_zero_trust_gateway_proxy_endpoint"], ""),
    ("gateway/configuration",      ["cloudflare_zero_trust_gateway_settings"], ""),
    ("gateway/rules",              ["cloudflare_zero_trust_gateway_policy"], ""),
    ("gateway/lists",              ["cloudflare_zero_trust_list"],       ""),
    ("mtls_certificates",          ["cloudflare_mtls_certificate"],      ""),
    ("challenges/widgets",         ["cloudflare_turnstile_widget"],      ""),
    ("rum/site_info/list",         ["cloudflare_web_analytics_site"],    ""),
    ("members",                    ["cloudflare_account_member"],        "imports permissions"),
    ("@user",                      ["cloudflare_user"],                  "403 = account-scoped token cannot read user"),
    # ---- v5 resources with no collection-list endpoint (derivation notes) ----
    ("note:", ["cloudflare_zero_trust_device_custom_profile"],
        "no list endpoint found in API — custom WARP profiles via dashboard"),
    ("note:", ["cloudflare_zero_trust_risk_scoring_integration"],
        "risk-scoring integrations set via dashboard (Events/Logs)"),
    ("note:", ["cloudflare_waiting_room_rules"],
        "rules read per waiting room — aggregated in zone waiting_rooms probe"),
    ("note:", ["cloudflare_web_analytics_rule"],
        "rules per RUM site — POST-list endpoint; derive from site count above"),
    ("note:", ["cloudflare_r2_custom_domain", "cloudflare_r2_managed_domain"],
        "export per bucket: --resource-id 'cloudflare_r2_custom_domain=<bucket_name>,<domain_slug>' "
        "(composite ids — see probe row r2/buckets above for which buckets have domains)"),
    ("note:", ["cloudflare_stream_webhook"],
        "Stream webhook — no stable list endpoint; check Notifications dashboard"),
    # ---- present but NOT supported by cf-terraforming v5 -> manual ----
    ("workers/scripts",            [],  "wrangler/API — never supported by cf-terraforming"),
    ("magic/ipsec_tunnels",        [],  "dashboard/API export"),
    ("gateway/pacfiles",           [],  "PAC files — dashboard only"),
]

# ---------------------------------------------------------------------------


def fetch_all(base, token):
    """Paginated GET. Returns (list | object | None, error_str).

    A few endpoints (e.g. pages/projects) reject per_page entirely; after the
    first such 400 we switch to ?page=N-only requests for the remaining pages.
    """
    page, total, items = 1, 1, []
    no_per_page = False
    while page <= total:
        sep = "&" if "?" in base else "?"
        qs = f"page={page}" if no_per_page else f"per_page=50&page={page}"
        req = urllib.request.Request(
            f"{base}{sep}{qs}",
            headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 400 and not no_per_page and b"per_page" in body:
                no_per_page = True          # retry same page without per_page
                continue
            return None, f"HTTP {e.code}"
        except Exception as e:
            return None, str(e)
        if not d.get("success"):
            return None, str(d.get("errors"))
        res = d.get("result")
        if not isinstance(res, list):
            return res, None
        items.extend(res)
        ri = d.get("result_info") or {}
        per = 10 if no_per_page else 50
        total = int(ri.get("total_pages")
                    or math.ceil(int(ri.get("total_count", 0)) / per) or 1)
        page += 1
    return items, None


# ---------------------------------------------------------------------------


def resolve_config(args):
    cfg = {
        "api_token":  os.environ.get("CLOUDFLARE_API_TOKEN"),
        "account_id": os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
        "zone_id":    os.environ.get("CLOUDFLARE_ZONE_ID"),
    }
    path = args.config or DEFAULT_CONFIG
    if os.path.exists(path):
        cp = configparser.ConfigParser()
        cp.read(path)
        cfg.update({k: cp["cloudflare"][k]
                    for k in ("api_token", "account_id", "zone_id")
                    if cp.has_option("cloudflare", k)})
        cfg["_config_path"] = path
    if args.account_id: cfg["account_id"] = args.account_id
    if args.zone_id:    cfg["zone_id"]    = args.zone_id
    if args.token:      cfg["api_token"]  = args.token

    if args.non_interactive and not all(cfg.values()):
        fail("missing credentials and --non-interactive was set")
    if not cfg["account_id"]:
        cfg["account_id"] = input("Account ID: ").strip() or None
    if not cfg["zone_id"]:
        cfg["zone_id"] = input("Zone ID: ").strip() or None
    if not cfg["api_token"]:
        cfg["api_token"] = getpass.getpass("API token: ").strip() or None
    if not all(cfg.values()):
        fail("account_id, zone_id and api_token are all required")
    return cfg


def save_config(cfg, path=None):
    path = path or DEFAULT_CONFIG
    cp = configparser.ConfigParser()
    cp["cloudflare"] = {k: cfg[k] for k in ("api_token", "account_id", "zone_id")}
    with open(path, "w") as fh:
        cp.write(fh)
    os.chmod(path, 0o600)
    print(f"config saved to {path} (mode 600)")


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


QUIET = False   # set by --quiet in main()


def progress(msg, end="\n"):
    """Progress output goes to stderr so stdout stays redirectable."""
    if QUIET:
        return
    sys.stderr.write(msg + end)
    sys.stderr.flush()



# ---------------------------------------------------------------------------


def scan(endpoint_map, base_info, token):
    """Probe every endpoint, returning (rows, specials):
      rows      = (endpoint | 'note:', finding, v5_resource_type, action)
      specials  = resolved IDs for --resource-id commands + extra notes
    base_info   = (api_root, url_base, ctx)  where ctx fills {zone_id}/{account_id}
    """
    api_root, url_base, ctx = base_info
    rows, specials = [], {}
    total_eps = len(endpoint_map)
    scope = url_base.rsplit("/", 2)[1]
    for idx, (ep, rtypes, note) in enumerate(endpoint_map, 1):
        special = note.replace("SPECIAL:", "") if note.startswith("SPECIAL:") else None
        note_opt = "" if special else note
        wrap_key = wrap_rtype = None
        if special and "->" in special:
            special, wrap = special.split("->", 1)
            if ":" in special:  # wrap:key form
                special, wrap_key = special.split(":", 1)
            wrap_key = wrap_key or special
            wrap_rtype = wrap

        if ep == "note:":
            if note_opt:
                rows.append(("(no API list)", "-", ", ".join(rtypes), f"derive — {note_opt}"))
            continue

        full = ep.format(**ctx) if "{" in ep else ep
        url = f"{api_root}/{full[1:]}" if full.startswith("@") else f"{url_base}/{full}"
        progress(f"[.. {scope:>5} {idx:3d}/{total_eps}] {ep} ...", end="")
        items, err = fetch_all(url, token)


        # collect ids needed by the dedicated --resource-id commands
        if not err and isinstance(items, list) and items:
            if ep == "tunnels":
                specials["tunnel_ids"] = [t["id"] for t in items if isinstance(t, dict)]
                # cf-terraforming silently aborts the remaining ids on the FIRST
                # 404 (tunnels without a config object) — pre-filter to tunnels
                # that actually have one (skip probing when several hundred)
                if len(specials["tunnel_ids"]) <= 50:
                    cfg_ids = []
                    for j, tid in enumerate(specials["tunnel_ids"], 1):
                        progress(f"[.. {scope:>5} {idx:3d}/{total_eps}] {ep} -> probing configs {j}/{len(specials['tunnel_ids'])} ...", end="\r")
                        _, e = fetch_all(f"{url_base}/cfd_tunnel/{tid}/configurations", token)
                        if not e:
                            cfg_ids.append(tid)
                    specials["tunnel_config_ids"] = cfg_ids
            elif ep == "workers/scripts":
                specials["script_ids"] = [s.get("id") for s in items if s.get("id")]
            elif ep == "api_gateway/operations":
                specials["shield_op_ids"] = [o["operation_id"] for o in items if o.get("operation_id")]
            elif ep == "dlp/profiles":
                specials["dlp_custom_ids"] = [p["id"] for p in items if p.get("type") == "custom"]
                specials["dlp_predef_ids"]  = [p["id"] for p in items if p.get("type") == "predefined"]

        if err:
            result = err
        elif special == "wrap" and isinstance(items, dict):
            vals = items.get(wrap_key) or []
            result = str(len(vals))
        elif isinstance(items, list):
            result = str(len(items))
        elif items:
            result = "present"
        else:
            result = "0"

        v5 = ", ".join(wrap_rtype.split(",")) if wrap_rtype else (
             ", ".join(rtypes) if rtypes else "-")
        if wrap_rtype:
            v5 = wrap_rtype

        if err:
            action = f"skip ({err})" + (f" — {note_opt}" if note_opt else "")
        elif (isinstance(items, list) and not items) or items is None or \
             (special == "wrap" and result == "0"):
            action = "not configured"
        elif special == "zone_settings":
            settings = [s["id"] for s in items if s.get("editable")]
            specials["settings"] = settings
            result = f"{result} ({len(settings)} editable)"
            v5, action = "cloudflare_zone_setting", "export via --resource-id"
        elif special == "waiting_rooms":
            specials["wr_ids"] = [w["id"] for w in items]
            rules = 0
            for j, wid in enumerate(specials["wr_ids"][:50], 1):
                progress(f"[.. {scope:>5} {idx:3d}/{total_eps}] {ep} -> rules {j}/{min(len(specials['wr_ids']),50)} ...", end="\r")
                rr, _e = fetch_all(f"{url_base}/waiting_rooms/{wid}/rules", token)
                if isinstance(rr, dict):
                    rules += len(rr.get("rules") or [])
                elif isinstance(rr, list):
                    rules += len(rr)
            result = f"{result} (+{rules} rules)"
            v5 = "cloudflare_waiting_room, cloudflare_waiting_room_settings"
            action = "export (+ events/rules via --resource-id)"
        elif special == "lists":
            specials["list_ids"] = [l["id"] for l in items]
            v5 = "cloudflare_list, cloudflare_list_item"
            action = "export (+ items via --resource-id)"
        elif special == "pages":
            doms = sum(len(p.get("domains") or []) for p in items)
            specials["pages_names"] = [p["name"] for p in items if p.get("name")]
            result = f"{result} (+{doms} domains)"
            v5 = "cloudflare_pages_project, cloudflare_pages_domain"
            action = "export"
        elif special == "queues":
            if items:
                specials["queue_ids"] = [q["queue_id"] for q in items if q.get("queue_id")]
            cons = 0
            for j, q in enumerate(items[:25], 1):
                progress(f"[.. {scope:>5} {idx:3d}/{total_eps}] {ep} -> consumers {j}/{min(len(items),25)} ...", end="\r")
                cc, _e = fetch_all(f"{url_base}/queues/{q['queue_id']}/consumers", token)
                if isinstance(cc, list):
                    cons += len(cc)
            result = f"{result} (+{cons} consumers)"
            v5 = "cloudflare_queue, cloudflare_queue_consumer"
            action = "export"
        elif special == "r2":
            buckets = items.get("buckets") or []
            cd, mg, pairs = 0, 0, []
            probe = buckets[:25]
            for j, b in enumerate(probe, 1):
                progress(f"[.. {scope:>5} {idx:3d}/{total_eps}] {ep} -> bucket domains {j}/{len(probe)} ...", end="\r")
                cc, _e = fetch_all(f"{url_base}/r2/buckets/{b['name']}/domains/custom", token)
                if isinstance(cc, dict):
                    for dd in cc.get("domains") or []:
                        cd += 1
                        pairs.append(f"{b['name']},{dd.get('domain')}")
                mm, _e = fetch_all(f"{url_base}/r2/buckets/{b['name']}/domains/managed", token)
                if isinstance(mm, dict) and mm.get("enabled"):
                    mg += 1
                    pairs.append(f"{b['name']},{mm.get('domain')}")
            specials["r2_pairs"] = pairs
            result = f"{len(buckets)} (+{cd} custom domains, {mg} managed)"
            v5 = "cloudflare_r2_bucket, cloudflare_r2_custom_domain, cloudflare_r2_managed_domain"
            action = "export"
        elif special == "wrap":
            action = "export" + (f" ({note_opt})" if note_opt else "")
        elif rtypes:
            action = "export" + (f" ({note_opt})" if note_opt else "")
        else:
            v5 = "(no cf-terraforming v5 support)"
            action = f"manual — {note_opt}"
        rows.append((ep, result, v5, action))
        pad = " " * max(0, 68 - len(ep))
        progress(f"\r[OK {scope:>5} {idx:3d}/{total_eps}] {ep}{pad}{result}")
    progress(f"\r[DONE {scope:>4}] {total_eps} endpoints probed" + " " * 40)
    return rows, specials


def print_mapping(rows):
    w_ep  = max(len(r[0]) for r in rows) + 2
    w_res = max(len(r[1]) for r in rows) + 2
    w_v5  = max(len(r[2]) for r in rows) + 2
    print("\n=== Endpoint -> cf-terraforming v5 resource mapping ===")
    print(f"{'endpoint':<{w_ep}}{'finding':<{w_res}}{'v5 resource type':<{w_v5}}action")
    print("-" * (w_ep + w_res + w_v5 + 30))
    for ep, result, v5, action in rows:
        print(f"{ep:<{w_ep}}{result:<{w_res}}{v5:<{w_v5}}{action}")
    print()


def build_commands(zone_rows, acct_rows, z_special, a_special, aid, zid):
    """Group findings into cf-terraforming generate commands.

    Batches contain ONLY types safe to bulk-generate; anything in
    RESOURCE_ID_REQUIRED goes to dedicated --resource-id commands
    (a batch containing them aborts mid-run — verified empirically).
    """
    zone_batch, acct_batch, extras = set(), set(), []

    def batch_cmd(scope_flag, scope_val, types, out):
        types = sorted(types - RESOURCE_ID_REQUIRED)
        if types:
            extras.append({"types": types, "out": out, "cmd":
                f'cf-terraforming generate {scope_flag} {scope_val} '
                f'--resource-type "{",".join(types)}" > {out}'})

    def rid_cmd(scope_flag, scope_val, rtype, ids, out):
        # IMPORTANT: --resource-id must be passed ONCE PER VALUE — cf-terraforming
        # does NOT iterate comma-separated value lists (it would put them all
        # into one URL path segment: /queues/<id1,id2,id3>/consumers -> 400).
        # For >3 ids we generate the flags inline with a tiny shell loop so the
        # ids appear once as a plain list instead of repeating the flag reword.
        if len(ids) <= 3:
            flags = " ".join(f'--resource-id "{rtype}={i}"' for i in ids)
        else:
            flags = (f'$(for id in {" ".join(ids)}; do '
                     f'printf -- \'--resource-id "{rtype}=%s" \' "$id"; done)')
        extras.append({"types": [rtype], "out": out, "cmd":
            f'cf-terraforming generate {scope_flag} {scope_val} '
            f'--resource-type {rtype} {flags} > {out}'})

    # ---------------- zone ----------------
    for ep, rtypes, _n in ZONE_ENDPOINTS:
        row = next((r for r in zone_rows if r[0] == ep), None)
        if row and row[3].startswith("export"):
            zone_batch.update(rtypes or row[2].split(", "))
    if "wr_ids" in z_special:
        zone_batch.update({"cloudflare_waiting_room",
                           "cloudflare_waiting_room_settings"})
    batch_cmd("--zone", zid, zone_batch, "zone.tf")
    if z_special.get("settings"):
        rid_cmd("--zone", zid, "cloudflare_zone_setting", z_special["settings"], "zone-settings.tf")
    if z_special.get("wr_ids"):
        rid_cmd("--zone", zid, "cloudflare_waiting_room_event", z_special["wr_ids"], "waiting-room-events.tf")
        rid_cmd("--zone", zid, "cloudflare_waiting_room_rules", z_special["wr_ids"], "waiting-room-rules.tf")
    if z_special.get("shield_op_ids"):
        rid_cmd("--zone", zid,
                "cloudflare_api_shield_operation_schema_validation_settings",
                z_special["shield_op_ids"], "shield-op-validation.tf")
    # literal resource-id specifiers (no per-hostname override => output may be empty)
    rid_cmd("--zone", zid, "cloudflare_hostname_tls_setting",
            ["ciphers", "min_tls_version"], "hostname-tls.tf")

    # ---------------- account ----------------
    for ep, rtypes, _n in ACCOUNT_ENDPOINTS:
        row = next((r for r in acct_rows if r[0] == ep), None)
        if row and row[3].startswith("export"):
            acct_batch.update(rtypes or row[2].split(", "))
    if "list_ids" in a_special:
        acct_batch.add("cloudflare_list")
    acct_batch.add("cloudflare_account")              # account itself always exists
    acct_batch.discard("-")
    batch_cmd("--account", aid, acct_batch, "account.tf")

    if a_special.get("list_ids"):
        rid_cmd("--account", aid, "cloudflare_list_item", a_special["list_ids"], "list-items.tf")
    if a_special.get("tunnel_config_ids"):
        rid_cmd("--account", aid, "cloudflare_zero_trust_tunnel_cloudflared_config",
                a_special["tunnel_config_ids"], "tunnel-configs.tf")
    if a_special.get("queue_ids"):
        rid_cmd("--account", aid, "cloudflare_queue_consumer", a_special["queue_ids"], "queue-consumers.tf")
    if a_special.get("dlp_custom_ids"):
        rid_cmd("--account", aid, "cloudflare_zero_trust_dlp_custom_profile",
                a_special["dlp_custom_ids"], "dlp-custom-profiles.tf")
    if a_special.get("dlp_predef_ids"):
        rid_cmd("--account", aid, "cloudflare_zero_trust_dlp_predefined_profile",
                a_special["dlp_predef_ids"], "dlp-predefined-profiles.tf")
    if a_special.get("pages_names"):
        rid_cmd("--account", aid, "cloudflare_pages_domain", a_special["pages_names"], "pages-domains.tf")
    # r2 custom/managed domains: composite "bucket,domain" ids — cf-terraforming
    # parses --resource-id by comma splitting, so pairs go to separate one-off
    # commands; surfaced as a derive note row instead (see mapping above).
    if a_special.get("script_ids"):
        ids = a_special["script_ids"]
        rid_cmd("--account", aid, "cloudflare_workers_cron_trigger", ids, "workers-cron.tf")
        rid_cmd("--account", aid, "cloudflare_workers_deployment", ids, "workers-deployments.tf")
        rid_cmd("--account", aid, "cloudflare_workers_script_subdomain", ids, "workers-subdomains.tf")
    return extras


SERVICE_ORDER = ["account", "dns", "loadbalancing", "ssl-tls", "cdn", "security",
                 "workers", "email", "ai", "observability", "waiting-rooms",
                 "zero-trust", "networking", "misc"]   # networking LAST (spectrum = post-cutover)


def write_services(outdir, commands, manual_rows, aid, zid):
    """--services mode: one terraform workspace (dir + own state) per product
    area. Bulk zone/account.tf batches are SPLIT by service — every dir only
    gets the resource types that belong to it."""
    svc_group = {}
    for c in commands:
        if c["out"] in ("zone.tf", "account.tf"):          # bulk batch — split per service
            scope_flag, scope_val = (("--zone", zid) if c["cmd"].startswith("cf-terraforming generate --zone")
                                     else ("--account", aid))
            for t in c["types"]:
                svc = TYPE_TO_SERVICE.get(t, "misc")
                buckets = svc_group.setdefault(svc, {"batches": {}, "cmds": []})
                buckets["batches"].setdefault((scope_flag, scope_val, c["out"]), set()).add(t)
        else:                                                # dedicated --resource-id cmd
            svc = TYPE_TO_SERVICE.get(c["types"][0], "misc")
            svc_group.setdefault(svc, {"batches": {}, "cmds": []})["cmds"].append(c["cmd"])

    os.makedirs(outdir, exist_ok=True)
    present = [s for s in SERVICE_ORDER if s in svc_group]

    VERSIONS = """terraform {
  required_version = ">= 1.5"
  required_providers {
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 5.0" }
  }
}
"""
    for svc in present:
        d = os.path.join(outdir, svc)
        os.makedirs(d, exist_ok=True)
        if not os.path.exists(os.path.join(d, "versions.tf")):
            with open(os.path.join(d, "versions.tf"), "w") as fh:
                fh.write(VERSIONS)

        parts = []
        info = svc_group[svc]
        for (scope_flag, scope_val, out), types in sorted(info["batches"].items()):
            parts.append(
                f'cf-terraforming generate {scope_flag} {scope_val} '
                f'--resource-type "{",".join(sorted(types))}" > {out}')
        parts.extend(c for c in dict.fromkeys(info["cmds"]))   # dedupe, keep order
        with open(os.path.join(d, "export.sh"), "w") as fh:
            fh.write("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\n\n")
            fh.write("[ -d .terraform ] || terraform init -input=false -upgrade >/dev/null\n\n")
            fh.write("\n\n".join(parts) + "\n")
        os.chmod(os.path.join(d, "export.sh"), 0o755)

    with open(os.path.join(outdir, "export-all.sh"), "w") as fh:
        fh.write("#!/usr/bin/env bash\nset -euo pipefail\ncd \"$(dirname \"$0\")\"\n\n")
        fh.write("# ordered: dependencies first; networking LAST (spectrum needs\n# an ACTIVE zone — run its export/apply only after the NS cutover).\n\n")
        for svc in present:
            fh.write(
                f'echo "== exporting {svc} =="\n'
                f'(cd {svc} && terraform init -input=false -upgrade >/dev/null && bash export.sh)\n')
    os.chmod(os.path.join(outdir, "export-all.sh"), 0o755)

    with open(os.path.join(outdir, "README.md"), "w") as fh:
        fh.write("# Per-service terraform workspaces\n\n"
                 "One directory = one product area = one `terraform.tfstate`.\n\n"
                 "| Order | Service | Applies when |\n|---|---|---|\n")
        for i, svc in enumerate(present, 1):
            cut = "**post-cutover** (spectrum needs an active zone)" if svc == "networking" else "pre-cutover"
            fh.write(f"| {i} | {svc}/ | {cut} |\n")
        fh.write("\nRun everything:\n\n```bash\nbash export-all.sh\n```\n\n"
                 "Run one service:\n\n```bash\ncd <service> && terraform init && bash export.sh\n```\n\n"
                 f"Exports read from the source account/zone: `{aid}` / `{zid}`.\n")
        manual_lines = sorted({f"- **{TYPE_TO_SERVICE.get(v.split(',')[0].strip(), 'misc')}** — `{v}`"
                               for _e, _f, v, a in manual_rows})
        if manual_lines:
            fh.write("\n## Manual / no-list-endpoint items\n\n" + "\n".join(manual_lines) + "\n")

    print(f"== services written to {outdir}/ ({len(present)} workspaces)")
    for svc in present:
        info = svc_group[svc]
        n_cmds = len(info["batches"]) + len(info["cmds"])
        n_types = sum(len(t) for t in info["batches"].values())
        print(f"   {svc:<16} {n_cmds:>2} invocations, {n_types} resource types")
    return present


# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description="cf-terraforming discovery + mapping + command generator")
    p.add_argument("-a", "--account-id", help="Cloudflare account ID")
    p.add_argument("-z", "--zone-id",    help="Cloudflare zone ID")
    p.add_argument("-t", "--token",      help="Cloudflare API token")
    p.add_argument("-c", "--config",     help=f"INI config path (default {DEFAULT_CONFIG})")
    p.add_argument("--save", action="store_true", help="persist inputs to the config file")
    p.add_argument("--non-interactive", action="store_true", help="never prompt")
    p.add_argument("--write-sh", metavar="FILE", help="also write the commands to a shell script")
    p.add_argument("--services", metavar="DIR",
                   help="write one terraform workspace per product area under DIR/ (each with its own export.sh and terraform state)")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress probe progress on stderr")
    args = p.parse_args()
    QUIET = args.quiet
    globals()["QUIET"] = QUIET

    cfg = resolve_config(args)
    token, aid, zid = cfg["api_token"], cfg["account_id"], cfg["zone_id"]

    acct, err = fetch_all(f"{API}/accounts/{aid}", token)
    if err or not isinstance(acct, dict):
        fail(f"cannot access account {aid}: {err}")
    zone, err = fetch_all(f"{API}/zones/{zid}", token)
    if err or not isinstance(zone, dict):
        fail(f"cannot access zone {zid}: {err}")
    print(f"== account: {acct.get('name', '?')} ({aid})")
    print(f"== zone:    {zone.get('name', '?')} ({zid})")

    ctx = {"zone_id": zid, "account_id": aid}
    zone_rows, z_special  = scan(ZONE_ENDPOINTS,    (API, f"{API}/zones/{zid}",    ctx), token)
    acct_rows, a_special  = scan(ACCOUNT_ENDPOINTS, (API, f"{API}/accounts/{aid}", ctx), token)

    print_mapping(zone_rows + acct_rows)

    commands = build_commands(zone_rows, acct_rows, z_special, a_special, aid, zid)

    if args.services:
        present = write_services(args.services, commands,
                                 [r for r in zone_rows + acct_rows
                                  if r[3].startswith(("manual", "derive"))],
                                 aid, zid)
    else:
        print("=== cf-terraforming commands ===\n")
        print("  # NOTE: cloudflare_zone is NOT included — cf-terraforming's zone")
        print("  # generator pulls EVERY zone the token can see. Create the target")
        print("  # zone via dashboard/API, then: terraform import cloudflare_zone.this <zone_id>\n")
        for c in commands:
            print(c["cmd"] + "\n")

    manual = [r for r in zone_rows + acct_rows if r[3].startswith("manual")]
    if manual:
        print("== requires manual migration (cf-terraforming cannot export):")
        for ep, _r, _v, action in manual:
            print(f"  {ep:<30} {action}")
        print()

    derive = [v for _e, _f, v, a in zone_rows + acct_rows if a.startswith("derive")]
    if derive:
        print("== supported but no list endpoint — migrate by reading individually:")
        for v in derive:
            print(f"  {v}")
        print()

    if args.write_sh and not args.services:
        with open(args.write_sh, "w") as fh:
            fh.write("#!/usr/bin/env bash\nset -euo pipefail\n\n")
            fh.write("\n\n".join(c["cmd"] for c in commands) + "\n")
        print(f"commands written to {args.write_sh}")

    if args.save:
        save_config(cfg, args.config)


if __name__ == "__main__":
    main()
