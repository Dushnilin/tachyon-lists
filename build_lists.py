#!/usr/bin/env python3
"""
Tachyon Lists Builder (build_lists.py)
======================================
Automated multi-source reparser, deduplicator, and compiler for Tachyon routing lists.

Pulls from upstream sources:
- hydraponique/roscomvpn-geosite (v2ray domain-list-community `data/*` + `.srs`)
- hydraponique/roscomvpn-geoip (`data/*.txt` + `.srs`)
- runetfreedom/russia-v2ray-rules-dat (`.srs`)
- runetfreedom/russia-blocked-geosite (`domains.txt`)
- runetfreedom/russia-blocked-geoip (`cidr.txt`)
- itdoginfo/allow-domains (`.srs` + `Subnets/IPv4/*.lst` + `Subnets/IPv6/*.lst`)
- MetaCubeX/meta-rules-dat (`sing` branch `.json` / `.srs`)
- zxc-rv/ad-filter (`adlist.srs`)
- ushan0v/sing-box-supercell-ruleset (`supercell.srs`)

Outputs:
- release/sing-box/<id>.srs       (compiled sing-box binary rule-set)
- release/json/<id>.json          (sing-box source rule-set v3)
- release/domains/<id>.lst        (flat domain list for steer / zapret / dnsmasq)
- release/subnets/<id>.lst        (combined collapsed IPv4 + IPv6 CIDRs for Tachyon nftables)
- release/subnets/ipv4/<id>.lst   (collapsed IPv4 CIDRs)
- release/subnets/ipv6/<id>.lst   (collapsed IPv6 CIDRs)
- release/catalog.json            (metadata, descriptions, counts, SHA-256 hashes)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
SOURCES_FILE = ROOT_DIR / "sources.json"
CACHE_DIR = ROOT_DIR / ".cache"
DOWNLOAD_CACHE_DIR = CACHE_DIR / "downloads"
DECOMPILED_CACHE_DIR = CACHE_DIR / "decompiled"
BIN_CACHE_DIR = CACHE_DIR / "bin"
CUSTOM_DIR = ROOT_DIR / "custom"
RELEASE_DIR = ROOT_DIR / "release"

USER_AGENT = "TachyonListsBuilder/1.0 (+https://github.com/Dushnilin/tachyon-lists)"
SING_BOX_VERSION = "1.11.15"


@dataclass
class RuleBucket:
    domain: set[str] = field(default_factory=set)
    domain_suffix: set[str] = field(default_factory=set)
    domain_keyword: set[str] = field(default_factory=set)
    domain_regex: set[str] = field(default_factory=set)
    ip_cidr: set[str] = field(default_factory=set)

    def merge(self, other: "RuleBucket") -> None:
        self.domain.update(other.domain)
        self.domain_suffix.update(other.domain_suffix)
        self.domain_keyword.update(other.domain_keyword)
        self.domain_regex.update(other.domain_regex)
        self.ip_cidr.update(other.ip_cidr)


def log(msg: str) -> None:
    print(f"[tachyon-lists] {msg}", flush=True)


def ensure_sing_box_binary() -> Path:
    """Locate sing-box on PATH or download official binary into .cache/bin/."""
    env_bin = os.environ.get("SING_BOX_BIN")
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)

    which_bin = shutil.which("sing-box")
    if which_bin:
        return Path(which_bin)

    BIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported architecture for auto-downloading sing-box: {machine}")

    exe_name = "sing-box.exe" if sys_name == "windows" else "sing-box"
    cached_bin = BIN_CACHE_DIR / exe_name
    if cached_bin.is_file():
        return cached_bin

    ver = SING_BOX_VERSION
    if sys_name == "windows":
        archive_ext = "zip"
        os_tag = "windows"
    elif sys_name == "darwin":
        archive_ext = "tar.gz"
        os_tag = "darwin"
    else:
        archive_ext = "tar.gz"
        os_tag = "linux"

    folder_name = f"sing-box-{ver}-{os_tag}-{arch}"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{ver}/{folder_name}.{archive_ext}"
    archive_path = BIN_CACHE_DIR / f"{folder_name}.{archive_ext}"

    log(f"Downloading sing-box v{ver} ({os_tag}-{arch}) for SRS compilation...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        archive_path.write_bytes(resp.read())

    if archive_ext == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith(f"/{exe_name}") or member == exe_name:
                    with zf.open(member) as src:
                        cached_bin.write_bytes(src.read())
                    break
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith(f"/{exe_name}") or member.name == exe_name:
                    extracted = tf.extractfile(member)
                    if extracted:
                        cached_bin.write_bytes(extracted.read())
                    break

    if not cached_bin.is_file():
        raise RuntimeError(f"Failed to extract {exe_name} from {archive_path}")

    if sys_name != "windows":
        cached_bin.chmod(cached_bin.stat().st_mode | stat.S_IEXEC)

    return cached_bin


def url_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    tail = url.rstrip("/").split("/")[-1]
    safe_tail = "".join(c if c.isalnum() or c in "._-" else "_" for c in tail)[:48]
    return DOWNLOAD_CACHE_DIR / f"{digest}_{safe_tail}"


def fetch_url_bytes(url: str, retries: int = 3) -> bytes:
    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = url_cache_path(url)
    if cpath.is_file() and cpath.stat().st_size > 0:
        return cpath.read_bytes()

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
                if data:
                    cpath.write_bytes(data)
                    return data
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(0.8 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def clean_domain(raw: str) -> str:
    d = raw.strip().lower().strip(".")
    if not d or " " in d or "/" in d or ":" in d:
        return ""
    if d.startswith("*."):
        d = d[2:]
    return d


def parse_dlc_entry(
    base_url: str,
    entry: str,
    required_attr: str | None = None,
    visited: set[tuple[str, str | None]] | None = None,
) -> RuleBucket:
    """Recursively parse a v2ray domain-list-community data/<entry> file."""
    if visited is None:
        visited = set()
    key = (entry, required_attr)
    if key in visited:
        return RuleBucket()
    visited.add(key)

    url = f"{base_url.rstrip('/')}/{entry}"
    try:
        text = fetch_url_bytes(url).decode("utf-8", errors="replace")
    except Exception as exc:
        log(f"WARN: Could not fetch DLC entry {entry} ({url}): {exc}")
        return RuleBucket()

    bucket = RuleBucket()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        parts = line.split()
        token = parts[0]
        attrs = {p[1:].lower() for p in parts[1:] if p.startswith("@")}

        if token.startswith("include:"):
            inc_target = token[len("include:") :].strip()
            # Filter attributes on include line: e.g. `include:category-ru @ru-available-only-inside`
            inc_attr = required_attr
            for a in attrs:
                if not a.startswith("!"):
                    inc_attr = a
            sub_bucket = parse_dlc_entry(base_url, inc_target, inc_attr, visited)
            bucket.merge(sub_bucket)
            continue

        if required_attr and required_attr.lower() not in attrs:
            continue

        if token.startswith("full:"):
            dom = clean_domain(token[len("full:") :])
            if dom:
                bucket.domain.add(dom)
        elif token.startswith("domain:"):
            dom = clean_domain(token[len("domain:") :])
            if dom:
                bucket.domain_suffix.add(dom)
        elif token.startswith("keyword:"):
            kw = token[len("keyword:") :].strip().lower()
            if kw:
                bucket.domain_keyword.add(kw)
        elif token.startswith("regexp:"):
            rx = token[len("regexp:") :].strip()
            if rx:
                bucket.domain_regex.add(rx)
        else:
            dom = clean_domain(token)
            if dom:
                bucket.domain_suffix.add(dom)

    return bucket


def parse_singbox_json_bytes(raw_bytes: bytes) -> RuleBucket:
    bucket = RuleBucket()
    obj = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    rules = obj.get("rules", [])
    if not isinstance(rules, list):
        return bucket

    def collect_rule(rule: dict[str, Any]) -> None:
        for k, target in (
            ("domain", bucket.domain),
            ("domain_suffix", bucket.domain_suffix),
            ("domain_keyword", bucket.domain_keyword),
            ("domain_regex", bucket.domain_regex),
            ("ip_cidr", bucket.ip_cidr),
        ):
            val = rule.get(k)
            if isinstance(val, str) and val.strip():
                if k in ("domain", "domain_suffix"):
                    cleaned = clean_domain(val)
                    if cleaned:
                        target.add(cleaned)
                else:
                    target.add(val.strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        if k in ("domain", "domain_suffix"):
                            cleaned = clean_domain(item)
                            if cleaned:
                                target.add(cleaned)
                        else:
                            target.add(item.strip())
        if isinstance(rule.get("rules"), list):
            for child in rule["rules"]:
                if isinstance(child, dict):
                    collect_rule(child)

    for r in rules:
        if isinstance(r, dict):
            collect_rule(r)
    return bucket


def parse_srs_url(url: str, sing_box_bin: Path) -> RuleBucket:
    DECOMPILED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw_bytes = fetch_url_bytes(url)
    digest = hashlib.sha256(raw_bytes).hexdigest()[:24]
    json_cache = DECOMPILED_CACHE_DIR / f"{digest}.json"

    if not json_cache.is_file():
        srs_tmp = DECOMPILED_CACHE_DIR / f"{digest}.srs"
        srs_tmp.write_bytes(raw_bytes)
        res = subprocess.run(
            [str(sing_box_bin), "rule-set", "decompile", str(srs_tmp), "-o", str(json_cache)],
            capture_output=True,
            text=True,
        )
        srs_tmp.unlink(missing_ok=True)
        if res.returncode != 0 or not json_cache.is_file():
            raise RuntimeError(f"sing-box rule-set decompile failed for {url}: {res.stderr.strip()}")

    return parse_singbox_json_bytes(json_cache.read_bytes())


def parse_plain_domains(text: str) -> RuleBucket:
    bucket = RuleBucket()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("||") and line.endswith("^"):
            line = line[2:-1]
        dom = clean_domain(line)
        if dom:
            bucket.domain_suffix.add(dom)
    return bucket


def parse_plain_subnets(text: str) -> RuleBucket:
    bucket = RuleBucket()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
            bucket.ip_cidr.add(str(net))
        except ValueError:
            continue
    return bucket


def optimize_bucket(
    bucket: RuleBucket,
    kind: str,
    exclude_domains: set[str] | None = None,
) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    """
    Deduplicate, prune redundant subdomains covered by parent domain_suffix,
    and collapse overlapping IPv4/IPv6 CIDRs.
    Returns (domain, domain_suffix, domain_keyword, domain_regex, ipv4_cidrs, ipv6_cidrs).
    """
    ex = exclude_domains or set()

    raw_suffixes = {d for d in bucket.domain_suffix if d and d not in ex}
    raw_domains = {d for d in bucket.domain if d and d not in ex}

    # Prune child suffixes whose parent is already in raw_suffixes
    pruned_suffixes: set[str] = set()
    for d in raw_suffixes:
        parts = d.split(".")
        covered = False
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            if parent in raw_suffixes:
                covered = True
                break
        if not covered:
            pruned_suffixes.add(d)

    # Prune exact domains that are already matched by pruned_suffixes
    pruned_domains: set[str] = set()
    for d in raw_domains:
        if d in pruned_suffixes:
            continue
        parts = d.split(".")
        covered = False
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            if parent in pruned_suffixes:
                covered = True
                break
        if not covered:
            pruned_domains.add(d)

    v4_nets: list[ipaddress.IPv4Network] = []
    v6_nets: list[ipaddress.IPv6Network] = []
    for cidr in bucket.ip_cidr:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                v4_nets.append(net)
            else:
                v6_nets.append(net)
        except ValueError:
            continue

    collapsed_v4 = [str(n) for n in ipaddress.collapse_addresses(v4_nets)]
    collapsed_v6 = [str(n) for n in ipaddress.collapse_addresses(v6_nets)]

    return (
        sorted(pruned_domains),
        sorted(pruned_suffixes),
        sorted(bucket.domain_keyword),
        sorted(bucket.domain_regex),
        collapsed_v4,
        collapsed_v6,
    )


def format_url(template: str, upstreams: dict[str, str]) -> str:
    res = template
    for k, v in upstreams.items():
        res = res.replace("{" + k + "}", v.rstrip("/"))
    return res


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def update_readme_table(catalog_items: list[dict[str, Any]]) -> None:
    readme_path = ROOT_DIR / "README.md"
    if not readme_path.is_file():
        return
    content = readme_path.read_text(encoding="utf-8")
    start_marker = "<!-- CATALOG_TABLE_START -->"
    end_marker = "<!-- CATALOG_TABLE_END -->"
    if start_marker not in content or end_marker not in content:
        return

    action_badges = {
        "proxy": "🟣 `Proxy`",
        "direct": "🟢 `Direct`",
        "block": "🔴 `Block`",
    }

    lines = [
        start_marker,
        "| ID | Категория и описание | Режим | Домены | Подсети (IPv4/IPv6) | Форматы |",
        "|:---|:---|:---:|---:|---:|:---|",
    ]
    for item in catalog_items:
        if item.get("group") == "internal":
            continue
        lid = item["id"]
        title = item["title"]
        desc = item["description"]
        act = action_badges.get(item["recommended_action"], item["recommended_action"])
        dom_total = item["counts"]["domain"] + item["counts"]["domain_suffix"]
        cidr_total = item["counts"]["ipv4_cidr"] + item["counts"]["ipv6_cidr"]
        formats = [f"[`.srs`](release/sing-box/{lid}.srs)", f"[`.json`](release/json/{lid}.json)"]
        if dom_total > 0:
            formats.append(f"[`.lst`](release/domains/{lid}.lst)")
        if cidr_total > 0:
            formats.append(f"[`subnets`](release/subnets/{lid}.lst)")
        lines.append(
            f"| `{lid}` | **{title}**<br><sub>{desc}</sub> | {act} | `{dom_total:,}` | `{cidr_total:,}` | {' · '.join(formats)} |"
        )
    lines.append(end_marker)

    before = content.split(start_marker, 1)[0]
    after = content.split(end_marker, 1)[1]
    readme_path.write_text(before + "\n".join(lines) + after, encoding="utf-8")


def build_all(only_ids: set[str] | None = None) -> int:
    t0 = time.time()
    cfg = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    upstreams: dict[str, str] = cfg["upstreams"]
    lists_cfg: list[dict[str, Any]] = cfg["lists"]
    if only_ids:
        lists_cfg = [item for item in lists_cfg if item["id"] in only_ids]

    sing_box_bin = ensure_sing_box_binary()
    log(f"Using sing-box compiler: {sing_box_bin}")

    # Pre-fetch direct HTTP URLs in parallel
    urls_to_prefetch: set[str] = set()
    for item in lists_cfg:
        for src in item.get("sources", []):
            if "url" in src:
                urls_to_prefetch.add(format_url(src["url"], upstreams))

    log(f"Prefetching {len(urls_to_prefetch)} upstream resources in parallel...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        future_map = {pool.submit(fetch_url_bytes, u): u for u in urls_to_prefetch}
        for fut in concurrent.futures.as_completed(future_map):
            u = future_map[fut]
            try:
                fut.result()
            except Exception as exc:
                log(f"WARN: Failed prefetching {u}: {exc}")

    # Prepare output directories
    for sub in ("sing-box", "json", "domains", "subnets", "subnets/ipv4", "subnets/ipv6"):
        (RELEASE_DIR / sub).mkdir(parents=True, exist_ok=True)

    catalog_entries: list[dict[str, Any]] = []
    compile_jobs: list[tuple[str, Path, Path]] = []

    for item in lists_cfg:
        lid = item["id"]
        kind = item["kind"]
        bucket = RuleBucket()

        for src in item.get("sources", []):
            stype = src["type"]
            try:
                if stype == "dlc":
                    base_url = upstreams[src["repo"]]
                    sub = parse_dlc_entry(base_url, src["entry"], src.get("attr"))
                    bucket.merge(sub)
                elif stype == "srs":
                    url = format_url(src["url"], upstreams)
                    sub = parse_srs_url(url, sing_box_bin)
                    bucket.merge(sub)
                elif stype == "singbox_json":
                    url = format_url(src["url"], upstreams)
                    sub = parse_singbox_json_bytes(fetch_url_bytes(url))
                    bucket.merge(sub)
                elif stype == "plain_domains":
                    url = format_url(src["url"], upstreams)
                    sub = parse_plain_domains(fetch_url_bytes(url).decode("utf-8", errors="replace"))
                    bucket.merge(sub)
                elif stype == "plain_subnets":
                    url = format_url(src["url"], upstreams)
                    sub = parse_plain_subnets(fetch_url_bytes(url).decode("utf-8", errors="replace"))
                    bucket.merge(sub)
            except Exception as exc:
                log(f"WARN: [{lid}] source {src} failed: {exc}")

        # Merge optional local overrides from custom/
        custom_dom_file = CUSTOM_DIR / "domains" / f"{lid}.lst"
        if custom_dom_file.is_file():
            bucket.merge(parse_plain_domains(custom_dom_file.read_text(encoding="utf-8")))

        custom_sub_file = CUSTOM_DIR / "subnets" / f"{lid}.lst"
        if custom_sub_file.is_file():
            bucket.merge(parse_plain_subnets(custom_sub_file.read_text(encoding="utf-8")))

        exclude_domains: set[str] = set()
        custom_exc_file = CUSTOM_DIR / "exclude" / f"{lid}.lst"
        if custom_exc_file.is_file():
            for line in custom_exc_file.read_text(encoding="utf-8").splitlines():
                d = clean_domain(line.split("#", 1)[0])
                if d:
                    exclude_domains.add(d)

        domains, suffixes, keywords, regexes, v4_cidrs, v6_cidrs = optimize_bucket(
            bucket, kind, exclude_domains
        )

        # Build sing-box source rule-set v3
        # IMPORTANT: For kind == "domains", never include ip_cidr in the .srs/.json rule-set
        # so sing-box 1.14+ DNS rules without match_response accept the rule-set.
        rule_obj: dict[str, Any] = {}
        if domains:
            rule_obj["domain"] = domains
        if suffixes:
            rule_obj["domain_suffix"] = suffixes
        if keywords:
            rule_obj["domain_keyword"] = keywords
        if regexes:
            rule_obj["domain_regex"] = regexes
        if kind in ("subnets", "mixed") and (v4_cidrs or v6_cidrs):
            rule_obj["ip_cidr"] = v4_cidrs + v6_cidrs

        # Ensure non-empty rule so sing-box compiles a valid populated .srs
        if not rule_obj:
            rule_obj["domain"] = [f"empty-{lid}.invalid"]

        srs_json_doc = {
            "version": 3,
            "rules": [rule_obj],
        }

        json_path = RELEASE_DIR / "json" / f"{lid}.json"
        srs_path = RELEASE_DIR / "sing-box" / f"{lid}.srs"
        json_path.write_text(json.dumps(srs_json_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        compile_jobs.append((lid, json_path, srs_path))

        # Write flat domain list for steer / zapret / dnsmasq
        all_flat_domains = sorted(set(domains) | set(suffixes))
        if all_flat_domains:
            (RELEASE_DIR / "domains" / f"{lid}.lst").write_text(
                "\n".join(all_flat_domains) + "\n", encoding="utf-8"
            )

        # Write subnet CIDR lists for nftables / steer
        if v4_cidrs:
            (RELEASE_DIR / "subnets" / "ipv4" / f"{lid}.lst").write_text(
                "\n".join(v4_cidrs) + "\n", encoding="utf-8"
            )
        if v6_cidrs:
            (RELEASE_DIR / "subnets" / "ipv6" / f"{lid}.lst").write_text(
                "\n".join(v6_cidrs) + "\n", encoding="utf-8"
            )
        if v4_cidrs or v6_cidrs:
            (RELEASE_DIR / "subnets" / f"{lid}.lst").write_text(
                "\n".join(v4_cidrs + v6_cidrs) + "\n", encoding="utf-8"
            )

        catalog_entries.append(
            {
                "id": lid,
                "group": item.get("group", "other"),
                "kind": kind,
                "recommended_action": item.get("recommended_action", "proxy"),
                "title": item["title"],
                "description": item["description"],
                "fallback_srs": item.get("fallback_srs", ""),
                "counts": {
                    "domain": len(domains),
                    "domain_suffix": len(suffixes),
                    "domain_keyword": len(keywords),
                    "domain_regex": len(regexes),
                    "ipv4_cidr": len(v4_cidrs),
                    "ipv6_cidr": len(v6_cidrs),
                },
                "files": {
                    "srs": f"release/sing-box/{lid}.srs",
                    "json": f"release/json/{lid}.json",
                    "domains": f"release/domains/{lid}.lst" if all_flat_domains else None,
                    "subnets": f"release/subnets/{lid}.lst" if (v4_cidrs or v6_cidrs) else None,
                },
            }
        )

    # Compile all .json -> .srs in parallel
    log(f"Compiling {len(compile_jobs)} binary .srs rule-sets...")

    def compile_one(job: tuple[str, Path, Path]) -> tuple[str, str]:
        lid, jpath, spath = job
        res = subprocess.run(
            [str(sing_box_bin), "rule-set", "compile", str(jpath), "-o", str(spath)],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0 or not spath.is_file():
            raise RuntimeError(f"Failed compiling {lid}.srs: {res.stderr.strip()}")
        return lid, sha256_file(spath)

    srs_hashes: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for lid, h in pool.map(compile_one, compile_jobs):
            srs_hashes[lid] = h

    for entry in catalog_entries:
        lid = entry["id"]
        entry["sha256_srs"] = srs_hashes.get(lid, "")

    catalog_doc = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": cfg["repository"],
        "total_lists": len(catalog_entries),
        "lists": catalog_entries,
    }
    (RELEASE_DIR / "catalog.json").write_text(
        json.dumps(catalog_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    update_readme_table(catalog_entries)

    elapsed = time.time() - t0
    log(f"Successfully built {len(catalog_entries)} lists in {elapsed:.1f}s -> {RELEASE_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Tachyon unified routing lists and SRS rule-sets.")
    parser.add_argument("--only", nargs="*", help="Build only specific list IDs")
    args = parser.parse_args()
    only = set(args.only) if args.only else None
    return build_all(only)


if __name__ == "__main__":
    sys.exit(main())
