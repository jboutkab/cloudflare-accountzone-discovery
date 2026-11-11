#!/usr/bin/env python3
"""Make a cf-terraforming load-balancing export portable across accounts.

The Cloudflare resource IDs emitted by cf-terraforming are recovered from its
standard terraform_managed_resource_<32 hex id>_<index> resource labels. Only
reference-bearing attributes are changed; all other exported configuration is
preserved byte-for-byte.

Usage (from a generated loadbalancing workspace):
    python3 load-balancing-refs.py .
    python3 load-balancing-refs.py account.tf zone.tf --check

Pass every account.tf/zone.tf file in the same invocation so references can be
resolved across files. By default files are updated in place; --check only
validates and reports whether changes are needed. Python 3.8+, stdlib only.
"""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile

MONITOR = "cloudflare_load_balancer_monitor"
POOL = "cloudflare_load_balancer_pool"
LOAD_BALANCER = "cloudflare_load_balancer"
HEALTHCHECK = "cloudflare_healthcheck"
SUPPORTED = {MONITOR, POOL, LOAD_BALANCER, HEALTHCHECK}

CLOUDFLARE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
ID_IN_RESOURCE_NAME = re.compile(
    r"^terraform_managed_resource_([0-9a-fA-F]{32})(?:_[0-9]+)?$"
)
POOL_REFERENCE_FIELDS = {
    "default_pools",
    "fallback_pool",
    "region_pools",
    "pop_pools",
    "country_pools",
    "pool_weights",
}
# cf-terraforming can include API status/computed fields that the v5 provider
# correctly rejects in configuration. Operational fields such as enabled=false
# are deliberately not in these sets and are retained.
TOP_LEVEL_READ_ONLY = {
    MONITOR: {"created_on", "id", "modified_on"},
    POOL: {"created_on", "disabled_at", "id", "modified_on", "networks"},
    LOAD_BALANCER: {"created_on", "id", "modified_on", "zone_name"},
    HEALTHCHECK: {"created_on", "failure_reason", "id", "modified_on", "status"},
}
POOL_ORIGIN_READ_ONLY = {"disabled_at", "failure_reason", "healthy"}
OPENERS = {"{": "}", "[": "]", "(": ")"}
CLOSERS = {value: key for key, value in OPENERS.items()}


class TransformError(Exception):
    """Invalid or incomplete Terraform input."""


@dataclass
class Token:
    kind: str
    start: int
    end: int


@dataclass
class Resource:
    rtype: str
    name: str
    body_open: int
    body_close: int


def raw(text, token):
    return text[token.start:token.end]


def quoted_value(text, token):
    """Decode a normal quoted HCL string (JSON escapes are compatible)."""
    value = raw(text, token)
    if not value.startswith('"'):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise TransformError("invalid quoted string at offset %d" % token.start) from exc


def lex(text):
    """Small, non-validating HCL lexer sufficient for cf-terraforming output.

    Strings, heredocs, and comments are skipped as units so braces or IDs inside
    them cannot be mistaken for HCL structure or resource references.
    """
    tokens = []
    i = 0
    size = len(text)
    while i < size:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char == "#" or text.startswith("//", i):
            newline = text.find("\n", i)
            i = size if newline < 0 else newline + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise TransformError("unterminated block comment at offset %d" % i)
            i = end + 2
            continue
        if text.startswith("<<", i):
            match = re.match(r"<<(-?)([A-Za-z_][A-Za-z0-9_-]*)\r?\n", text[i:])
            if match:
                tag = match.group(2)
                body_start = i + match.end()
                closing = re.compile(
                    r"^[ \t]*%s[ \t]*(?:\r?$)" % re.escape(tag), re.MULTILINE
                )
                end_match = closing.search(text, body_start)
                if not end_match:
                    raise TransformError("unterminated heredoc %s at offset %d" % (tag, i))
                end = end_match.end()
                tokens.append(Token("STRING", i, end))
                i = end
                continue
        if char == '"':
            start = i
            i += 1
            closed = False
            while i < size:
                if text[i] == "\\":
                    i += 2
                elif text[i] == '"':
                    i += 1
                    closed = True
                    break
                else:
                    i += 1
            if not closed:
                raise TransformError("unterminated quoted string at offset %d" % start)
            tokens.append(Token("STRING", start, i))
            continue
        ident = re.match(r"[A-Za-z_][A-Za-z0-9_-]*", text[i:])
        if ident:
            end = i + ident.end()
            tokens.append(Token("IDENT", i, end))
            i = end
            continue
        if char in "{}[]()=,:.":
            tokens.append(Token("PUNCT", i, i + 1))
        else:
            tokens.append(Token("OTHER", i, i + 1))
        i += 1
    return tokens


def delimiter_pairs(text, tokens):
    pairs = {}
    stack = []
    for index, token in enumerate(tokens):
        if token.kind != "PUNCT":
            continue
        char = raw(text, token)
        if char in OPENERS:
            stack.append((char, index, token.start))
        elif char in CLOSERS:
            if not stack or stack[-1][0] != CLOSERS[char]:
                raise TransformError("unmatched %s at offset %d" % (char, token.start))
            _opener, opened_index, _offset = stack.pop()
            pairs[opened_index] = index
            pairs[index] = opened_index
    if stack:
        char, _index, offset = stack[-1]
        raise TransformError("unclosed %s at offset %d" % (char, offset))
    return pairs


def parse_resources(text):
    tokens = lex(text)
    pairs = delimiter_pairs(text, tokens)
    resources = []
    i = 0
    while i + 3 < len(tokens):
        if (
            tokens[i].kind == "IDENT"
            and raw(text, tokens[i]) == "resource"
            and tokens[i + 1].kind == "STRING"
            and tokens[i + 2].kind == "STRING"
            and tokens[i + 3].kind == "PUNCT"
            and raw(text, tokens[i + 3]) == "{"
        ):
            close = pairs[i + 3]
            resources.append(
                Resource(
                    quoted_value(text, tokens[i + 1]),
                    quoted_value(text, tokens[i + 2]),
                    i + 3,
                    close,
                )
            )
            i = close + 1
        else:
            i += 1
    return tokens, pairs, resources


def source_id(resource):
    match = ID_IN_RESOURCE_NAME.match(resource.name)
    return match.group(1).lower() if match else None


def expression_end(tokens, pairs, start, limit):
    if start >= limit:
        return start
    token = tokens[start]
    if token.kind == "PUNCT":
        # delimiter_pairs is keyed by token index; find the value without
        # embedding the text in Token just for this one operation.
        return pairs.get(start, start) + 1
    return start + 1


def line_number(text, offset):
    return text.count("\n", 0, offset) + 1


class Transformer:
    def __init__(self):
        self.references = {MONITOR: {}, POOL: {}}
        self.errors = []
        self.stats = {
            "account_ids": 0,
            "zone_ids": 0,
            "monitor_references": 0,
            "pool_references": 0,
            "read_only_fields_removed": 0,
        }

    def collect(self, documents):
        for path, text in documents:
            try:
                _tokens, _pairs, resources = parse_resources(text)
            except TransformError as exc:
                self.errors.append("%s: %s" % (path, exc))
                continue
            for resource in resources:
                if resource.rtype not in self.references:
                    continue
                resource_id = source_id(resource)
                if not resource_id:
                    continue
                reference = "%s.%s.id" % (resource.rtype, resource.name)
                previous = self.references[resource.rtype].get(resource_id)
                if previous and previous != reference:
                    self.errors.append(
                        "%s: source ID %s is claimed by both %s and %s"
                        % (path, resource_id, previous, reference)
                    )
                else:
                    self.references[resource.rtype][resource_id] = reference

    def add_replacement(self, replacements, token, value, stat):
        replacements.append((token.start, token.end, value))
        self.stats[stat] += 1

    def reference_for(self, rtype, value, path, text, token):
        reference = self.references[rtype].get(value.lower())
        if reference is None:
            self.errors.append(
                "%s:%d: cannot resolve %s ID %s; export the referenced resource "
                "in the same loadbalancing workspace"
                % (path, line_number(text, token.start), rtype, value)
            )
        return reference

    def remove_read_only_attribute(self, text, tokens, pairs, index, rhs, replacements):
        """Remove one scalar attribute assignment, normally its whole source line."""
        token = tokens[index]
        end = expression_end(tokens, pairs, rhs, len(tokens))
        value_end = tokens[end - 1].end if end > rhs else tokens[rhs].end
        line_start = text.rfind("\n", 0, token.start) + 1
        line_end = text.find("\n", value_end)
        if line_end < 0:
            line_end = len(text)
        only_assignment = not text[line_start:token.start].strip()
        trailing = text[value_end:line_end].strip()
        if only_assignment and trailing in ("", ","):
            remove_end = line_end + 1 if line_end < len(text) else line_end
            replacements.append((line_start, remove_end, ""))
        else:
            remove_end = value_end + 1 if text[value_end:value_end + 1] == "," else value_end
            replacements.append((token.start, remove_end, ""))
        self.stats["read_only_fields_removed"] += 1

    def transform(self, path, text):
        tokens, pairs, resources = parse_resources(text)
        replacements = []
        for resource in resources:
            if resource.rtype not in SUPPORTED:
                continue
            depth = 0
            index = resource.body_open + 1
            while index < resource.body_close:
                token = tokens[index]
                value = raw(text, token)
                if token.kind == "PUNCT" and value in OPENERS:
                    depth += 1
                    index += 1
                    continue
                if token.kind == "PUNCT" and value in CLOSERS:
                    depth -= 1
                    index += 1
                    continue
                if (
                    token.kind != "IDENT"
                    or index + 2 >= resource.body_close
                    or raw(text, tokens[index + 1]) != "="
                ):
                    index += 1
                    continue

                rhs = index + 2
                read_only = (
                    depth == 0 and value in TOP_LEVEL_READ_ONLY[resource.rtype]
                ) or (
                    resource.rtype == POOL
                    and depth > 0
                    and value in POOL_ORIGIN_READ_ONLY
                )
                if read_only:
                    self.remove_read_only_attribute(
                        text, tokens, pairs, index, rhs, replacements
                    )
                    index += 2
                    continue

                if depth == 0 and value in ("account_id", "zone_id"):
                    if tokens[rhs].kind == "STRING":
                        variable = "var.account_id" if value == "account_id" else "var.zone_id"
                        stat = "account_ids" if value == "account_id" else "zone_ids"
                        self.add_replacement(replacements, tokens[rhs], variable, stat)
                    index += 2
                    continue

                if resource.rtype == POOL and value == "monitor":
                    if tokens[rhs].kind == "STRING":
                        monitor_id = quoted_value(text, tokens[rhs])
                        reference = self.reference_for(MONITOR, monitor_id, path, text, tokens[rhs])
                        if reference:
                            self.add_replacement(
                                replacements, tokens[rhs], reference, "monitor_references"
                            )
                    index += 2
                    continue

                if resource.rtype == LOAD_BALANCER and value in POOL_REFERENCE_FIELDS:
                    end = expression_end(tokens, pairs, rhs, resource.body_close)
                    for ref_index in range(rhs, end):
                        ref_token = tokens[ref_index]
                        if ref_token.kind != "STRING":
                            continue
                        pool_id = quoted_value(text, ref_token)
                        if not CLOUDFLARE_ID.match(pool_id):
                            continue
                        reference = self.reference_for(
                            POOL, pool_id, path, text, ref_token
                        )
                        if reference:
                            # HCL requires parentheses around an expression used
                            # as an object key (random_steering.pool_weights).
                            replacement = "(%s)" % reference if value == "pool_weights" else reference
                            self.add_replacement(
                                replacements, ref_token, replacement, "pool_references"
                            )
                    index = max(index + 2, end)
                    continue
                index += 2

        for start, end, value in sorted(replacements, reverse=True):
            text = text[:start] + value + text[end:]
        return text


def transform_documents(documents):
    """Return {path: transformed_text}, stats, and validation errors."""
    transformer = Transformer()
    transformer.collect(documents)
    if transformer.errors:
        return {}, transformer.stats, transformer.errors
    transformed = {}
    for path, text in documents:
        try:
            transformed[path] = transformer.transform(path, text)
        except TransformError as exc:
            transformer.errors.append("%s: %s" % (path, exc))
    if transformer.errors:
        return {}, transformer.stats, transformer.errors
    return transformed, transformer.stats, transformer.errors


def input_files(paths):
    files = []
    for value in paths or ["."]:
        path = Path(value)
        if path.is_dir():
            files.extend(sorted(path.glob("*.tf")))
        else:
            files.append(path)
    # Preserve order but avoid transforming a file twice (e.g. '.' plus a file).
    return list(dict.fromkeys(files))


def atomic_write(path, content):
    path = Path(path)
    mode = path.stat().st_mode
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths",
        nargs="*",
        help="exported .tf files or directories (default: current directory)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report changes without updating files",
    )
    args = parser.parse_args(argv)
    files = input_files(args.paths)
    if not files:
        parser.error("no .tf files found")

    documents = []
    for path in files:
        try:
            documents.append((path, path.read_text(encoding="utf-8")))
        except OSError as exc:
            print("error: %s: %s" % (path, exc), file=sys.stderr)
            return 1

    transformed, stats, errors = transform_documents(documents)
    if errors:
        for error in errors:
            print("error: " + error, file=sys.stderr)
        print(
            "error: no files changed. Re-export cloudflare_load_balancer_monitor, "
            "cloudflare_load_balancer_pool, and cloudflare_load_balancer together.",
            file=sys.stderr,
        )
        return 1

    changed = 0
    for path, original in documents:
        updated = transformed[path]
        if updated == original:
            continue
        changed += 1
        if not args.check:
            try:
                atomic_write(path, updated)
            except OSError as exc:
                print("error: %s: %s" % (path, exc), file=sys.stderr)
                return 1
        print("%s: %s" % (path, "would update" if args.check else "updated"))

    print(
        "load-balancing references: %d monitor, %d pool; "
        "parameterized %d account IDs and %d zone IDs; removed %d read-only "
        "fields; %d file(s) %s"
        % (
            stats["monitor_references"],
            stats["pool_references"],
            stats["account_ids"],
            stats["zone_ids"],
            stats["read_only_fields_removed"],
            changed,
            "need changes" if args.check else "changed",
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
