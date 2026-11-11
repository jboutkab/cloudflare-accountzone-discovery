#!/usr/bin/env python3
"""Regression tests for load-balancing migration reference generation."""

import contextlib
import importlib.util
import io
from pathlib import Path
import stat
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load_module(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the defining module during class construction.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


refs = load_module("load-balancing-refs.py", "load_balancing_refs")

MONITOR_ID = "a" * 32
POOL_ONE_ID = "b" * 32
POOL_TWO_ID = "c" * 32
LB_ID = "d" * 32
HEALTHCHECK_ID = "e" * 32

MONITOR_NAME = "terraform_managed_resource_%s_0" % MONITOR_ID
POOL_ONE_NAME = "terraform_managed_resource_%s_0" % POOL_ONE_ID
POOL_TWO_NAME = "terraform_managed_resource_%s_1" % POOL_TWO_ID
LB_NAME = "terraform_managed_resource_%s_0" % LB_ID
HEALTHCHECK_NAME = "terraform_managed_resource_%s_0" % HEALTHCHECK_ID
MONITOR_REF = "cloudflare_load_balancer_monitor.%s.id" % MONITOR_NAME
POOL_ONE_REF = "cloudflare_load_balancer_pool.%s.id" % POOL_ONE_NAME
POOL_TWO_REF = "cloudflare_load_balancer_pool.%s.id" % POOL_TWO_NAME

ACCOUNT_TF = """\
# monitor = \"{monitor}\" in a comment must not be changed
resource \"cloudflare_load_balancer_monitor\" \"{monitor_name}\" {{
  account_id  = \"source-account\"
  description = \"{monitor}\"
  type        = \"https\"
}}

resource \"cloudflare_load_balancer_pool\" \"{pool_one_name}\" {{
  account_id = \"source-account\"
  monitor    = \"{monitor}\"
  name       = \"primary\"
  origins = [{{
    address        = \"192.0.2.10\"
    enabled        = false
    disabled_at    = \"2024-01-01T00:00:00Z\"
    healthy        = false
    failure_reason = \"source runtime state\"
    weight         = 1
  }}]
}}

resource \"cloudflare_load_balancer_pool\" \"{pool_two_name}\" {{
  account_id = \"source-account\"
  monitor    = \"{monitor}\"
  name       = \"secondary\"
}}

resource \"cloudflare_ruleset\" \"unrelated\" {{
  account_id = \"leave-this-alone\"
  rules = [{{
    default_pools = [\"{pool_one}\"]
  }}]
}}
""".format(
    monitor=MONITOR_ID,
    monitor_name=MONITOR_NAME,
    pool_one=POOL_ONE_ID,
    pool_one_name=POOL_ONE_NAME,
    pool_two_name=POOL_TWO_NAME,
)

ZONE_TF = """\
resource \"cloudflare_load_balancer\" \"{lb_name}\" {{
  zone_id       = \"source-zone\"
  default_pools = [\"{pool_one}\", \"{pool_two}\"]
  fallback_pool = \"{pool_two}\"
  description   = \"{pool_one}\"
  region_pools = {{
    \"WEU\" = [\"{pool_one}\"]
    \"EEU\" = [\"{pool_two}\", \"{pool_one}\"]
  }}
  country_pools = {{
    US = [\"{pool_two}\"]
  }}
  pop_pools = {{
    \"SJC\" = [\"{pool_one}\"]
  }}
  random_steering = {{
    default_weight = 1
    pool_weights = {{
      \"{pool_one}\" = 0.75
      \"{pool_two}\" = 0.25
    }}
  }}
  rules = [{{
    name = \"fail over in a rule\"
    overrides = {{
      default_pools = [\"{pool_two}\"]
      fallback_pool = \"{pool_one}\"
      region_pools = {{
        \"WNAM\" = [\"{pool_two}\"]
      }}
      random_steering = {{
        pool_weights = {{
          \"{pool_two}\" = 1
        }}
      }}
    }}
  }}]
}}

resource \"cloudflare_healthcheck\" \"{healthcheck_name}\" {{
  zone_id        = \"source-zone\"
  name           = \"standalone-healthcheck\"
  status         = \"healthy\"
  failure_reason = \"source runtime state\"
}}
""".format(
    lb_name=LB_NAME,
    pool_one=POOL_ONE_ID,
    pool_two=POOL_TWO_ID,
    healthcheck_name=HEALTHCHECK_NAME,
)


def documents(account=ACCOUNT_TF, zone=ZONE_TF):
    return [("account.tf", account), ("zone.tf", zone)]


class TransformTests(unittest.TestCase):
    def transformed(self):
        transformed, stats, errors = refs.transform_documents(documents())
        self.assertEqual([], errors)
        return transformed, stats

    def test_monitor_references_and_shared_monitors(self):
        transformed, stats = self.transformed()
        account = transformed["account.tf"]
        self.assertEqual(2, account.count("monitor    = " + MONITOR_REF))
        self.assertEqual(2, stats["monitor_references"])
        self.assertIn('description = "%s"' % MONITOR_ID, account)
        self.assertIn('# monitor = "%s"' % MONITOR_ID, account)

    def test_load_balancer_pool_references_everywhere(self):
        transformed, stats = self.transformed()
        zone = transformed["zone.tf"]
        self.assertIn("default_pools = [%s, %s]" % (POOL_ONE_REF, POOL_TWO_REF), zone)
        self.assertIn("fallback_pool = %s" % POOL_TWO_REF, zone)
        self.assertIn('"WEU" = [%s]' % POOL_ONE_REF, zone)
        self.assertIn("US = [%s]" % POOL_TWO_REF, zone)
        self.assertIn('"SJC" = [%s]' % POOL_ONE_REF, zone)
        self.assertIn("(%s) = 0.75" % POOL_ONE_REF, zone)
        self.assertIn("(%s) = 1" % POOL_TWO_REF, zone)
        self.assertIn("default_pools = [%s]" % POOL_TWO_REF, zone)
        self.assertIn("fallback_pool = %s" % POOL_ONE_REF, zone)
        self.assertIn('description   = "%s"' % POOL_ONE_ID, zone)
        self.assertEqual(14, stats["pool_references"])

    def test_scope_ids_become_variables_without_touching_unrelated_resources(self):
        transformed, stats = self.transformed()
        account = transformed["account.tf"]
        zone = transformed["zone.tf"]
        self.assertEqual(3, account.count("= var.account_id"))
        self.assertIn('account_id = "leave-this-alone"', account)
        self.assertIn('default_pools = ["%s"]' % POOL_ONE_ID, account)
        self.assertEqual(2, zone.count("= var.zone_id"))
        self.assertEqual(3, stats["account_ids"])
        self.assertEqual(2, stats["zone_ids"])

    def test_read_only_runtime_fields_are_removed(self):
        transformed, stats = self.transformed()
        account = transformed["account.tf"]
        zone = transformed["zone.tf"]
        self.assertNotIn("disabled_at", account)
        self.assertNotIn("healthy        =", account)
        self.assertNotIn("failure_reason", account)
        self.assertNotIn("status         =", zone)
        self.assertNotIn("failure_reason", zone)
        self.assertIn("enabled        = false", account)
        self.assertEqual(5, stats["read_only_fields_removed"])

    def test_transform_is_idempotent(self):
        first, first_stats, errors = refs.transform_documents(documents())
        self.assertEqual([], errors)
        second, second_stats, errors = refs.transform_documents(
            [("account.tf", first["account.tf"]), ("zone.tf", first["zone.tf"])]
        )
        self.assertEqual([], errors)
        self.assertEqual(first, second)
        self.assertEqual(0, second_stats["monitor_references"])
        self.assertEqual(0, second_stats["pool_references"])
        self.assertEqual(0, second_stats["account_ids"])
        self.assertEqual(0, second_stats["zone_ids"])
        self.assertEqual(0, second_stats["read_only_fields_removed"])

    def test_unresolved_pool_fails_without_returning_partial_output(self):
        zone = ZONE_TF.replace(POOL_ONE_ID, "f" * 32)
        transformed, _stats, errors = refs.transform_documents(documents(zone=zone))
        self.assertEqual({}, transformed)
        self.assertTrue(any("cannot resolve cloudflare_load_balancer_pool ID" in e for e in errors))

    def test_unresolved_monitor_fails(self):
        account = ACCOUNT_TF.replace('monitor    = "%s"' % MONITOR_ID,
                                     'monitor    = "%s"' % ("f" * 32))
        transformed, _stats, errors = refs.transform_documents(documents(account=account))
        self.assertEqual({}, transformed)
        self.assertTrue(any("cannot resolve cloudflare_load_balancer_monitor ID" in e for e in errors))

    def test_heredoc_braces_and_ids_are_ignored(self):
        zone = ZONE_TF.replace(
            '  description   = "%s"' % POOL_ONE_ID,
            '  description = <<-EOT\n  } %s\n  EOT' % POOL_ONE_ID,
        )
        transformed, _stats, errors = refs.transform_documents(documents(zone=zone))
        self.assertEqual([], errors)
        self.assertIn("  } %s" % POOL_ONE_ID, transformed["zone.tf"])

    def test_cli_check_and_in_place_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "account.tf").write_text(ACCOUNT_TF, encoding="utf-8")
            (root / "zone.tf").write_text(ZONE_TF, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, refs.main([directory, "--check"]))
            self.assertEqual(ACCOUNT_TF, (root / "account.tf").read_text(encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, refs.main([directory]))
                self.assertEqual(0, refs.main([directory]))
            self.assertNotIn('monitor    = "%s"' % MONITOR_ID,
                             (root / "account.tf").read_text(encoding="utf-8"))

    def test_cli_error_leaves_all_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "account.tf").write_text(ACCOUNT_TF, encoding="utf-8")
            missing_pool_zone = ZONE_TF.replace(POOL_ONE_ID, "f" * 32)
            (root / "zone.tf").write_text(missing_pool_zone, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, refs.main([directory]))
            self.assertEqual(ACCOUNT_TF, (root / "account.tf").read_text(encoding="utf-8"))
            self.assertEqual(missing_pool_zone, (root / "zone.tf").read_text(encoding="utf-8"))


class DiscoveryIntegrationTests(unittest.TestCase):
    def test_services_mode_installs_and_runs_transformer(self):
        discovery = load_module("cf-discovery.py", "cf_discovery_test")
        commands = [
            {
                "types": [refs.POOL, refs.MONITOR],
                "out": "account.tf",
                "cmd": "cf-terraforming generate --account source-account",
            },
            {
                "types": [refs.LOAD_BALANCER, refs.HEALTHCHECK],
                "out": "zone.tf",
                "cmd": "cf-terraforming generate --zone source-zone",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            discovery.write_services(
                directory,
                commands,
                [],
                "source-account",
                "source-zone",
                target_aid="target-account",
                target_zid="target-zone",
                include={"loadbalancing"},
                include_types={refs.POOL, refs.MONITOR, refs.LOAD_BALANCER, refs.HEALTHCHECK},
            )
            workspace = Path(directory) / "loadbalancing"
            helper = workspace / "load-balancing-refs.py"
            setup = (workspace / "setup.sh").read_text(encoding="utf-8")
            self.assertEqual((HERE / "load-balancing-refs.py").read_text(encoding="utf-8"),
                             helper.read_text(encoding="utf-8"))
            self.assertTrue(helper.stat().st_mode & stat.S_IXUSR)
            self.assertIn("python3 load-balancing-refs.py .", setup)
            self.assertLess(setup.index("load-balancing-refs.py ."), setup.index("terraform init"))
            self.assertIn("references", (Path(directory) / "README.md").read_text(encoding="utf-8"))
            self.assertTrue((workspace / "terraform.tfvars").exists())
            self.assertFalse((workspace / "rulesets-import.py").exists())


if __name__ == "__main__":
    unittest.main()
