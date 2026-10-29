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
- release/tachyon/<id>.trs        (compiled Tachyon zero-copy mmap rule-set v2)
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
import bisect
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import platform
import shutil
import stat
import struct
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

USER_AGENT = "TachyonListsBuilder/2.0 (+https://github.com/Dushnilin/tachyon-lists)"
TACHYON_CORE_REPO = "Dushnilin/tachyon-core"
TACHYON_CORE_FALLBACK_TAG = "v0.2.4"


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


def resolve_latest_tachyon_tag() -> str:
    """Resolve the latest release tag from Dushnilin/tachyon-core via GitHub redirect."""
    env_tag = os.environ.get("TACHYON_CORE_TAG")
    if env_tag:
        return env_tag if env_tag.startswith("v") else f"v{env_tag}"
    url = f"https://github.com/{TACHYON_CORE_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            final_url = resp.geturl().rstrip("/")
            if "/releases/tag/" in final_url:
                return final_url.split("/releases/tag/")[-1]
    except Exception as exc:
        log(f"WARN: Failed to resolve latest tachyon-core tag ({exc}), falling back to {TACHYON_CORE_FALLBACK_TAG}")
    return TACHYON_CORE_FALLBACK_TAG


def ensure_tachyon_core_binary() -> Path:
    """Locate tachyon-core on PATH or download latest official release binary into .cache/bin/."""
    env_bin = os.environ.get("TACHYON_CORE_BIN")
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)

    which_bin = shutil.which("tachyon-core")
    if which_bin:
        return Path(which_bin)

    BIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if sys_name == "windows":
        if machine not in ("x86_64", "amd64"):
            raise RuntimeError(f"Unsupported Windows architecture for tachyon-core: {machine}")
        target_name = "windows-amd64"
        archive_ext = "zip"
        exe_suffix = ".exe"
    elif sys_name == "darwin":
        target_name = "darwin-arm64" if machine in ("aarch64", "arm64") else "darwin-amd64"
        archive_ext = "tar.gz"
        exe_suffix = ""
    else:
        if machine in ("x86_64", "amd64"):
            target_name = "linux-amd64"
        elif machine in ("aarch64", "arm64"):
            target_name = "aarch64"
        else:
            raise RuntimeError(f"Unsupported Linux architecture for tachyon-core: {machine}")
        archive_ext = "tar.gz"
        exe_suffix = ""

    tag = resolve_latest_tachyon_tag()
    cached_bin = BIN_CACHE_DIR / f"tachyon-core-{tag}{exe_suffix}"
    if cached_bin.is_file():
        return cached_bin

    asset_stem = f"tachyon-core-{target_name}-{tag}"
    url = f"https://github.com/{TACHYON_CORE_REPO}/releases/download/{tag}/{asset_stem}.{archive_ext}"
    archive_path = BIN_CACHE_DIR / f"{asset_stem}.{archive_ext}"

    log(f"Downloading tachyon-core {tag} ({target_name}) from {TACHYON_CORE_REPO}...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        archive_path.write_bytes(resp.read())

    if archive_ext == "zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith("tachyon-core.exe"):
                    with zf.open(member) as src:
                        cached_bin.write_bytes(src.read())
                    break
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.isfile() and "tachyon-core" in Path(member.name).name:
                    extracted = tf.extractfile(member)
                    if extracted:
                        cached_bin.write_bytes(extracted.read())
                        break

    if not cached_bin.is_file():
        raise RuntimeError(f"Failed to extract tachyon-core from {archive_path}")

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


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals.sort()
    merged: list[tuple[int, int]] = []
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e + 1:
            if e > cur_e:
                cur_e = e
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    return merged


def compile_tsrs_v2(
    domains: list[str],
    suffixes: list[str],
    keywords: list[str],
    regexes: list[str],
    ip_cidrs: list[str],
    total_rules: int = 1,
) -> bytes:
    """
    Compile rule-set directly into Tachyon Memory-Mapped Rule-Set v2 (TSRS v2) binary format.
    Byte-for-byte identical to `core_runtime::route::mmap_set::compiler::RuleSetCompiler`.
    """
    DOM_FLAG_EXACT = 0x01
    DOM_FLAG_SUFFIX = 0x02
    DOMAIN_BLOCK_SIZE = 128
    HEADER_SIZE = 96
    HEADER_FLAG_HAS_EXTRA = 0x01
    HEADER_FLAG_DOM_DICT = 0x02
    HEADER_FLAG_V6_64BIT = 0x04
    UNUSED_7BIT_CODES = [
        2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26,
        27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 47, 58, 59, 60, 61,
        62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84,
        85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 96, 123, 124, 125, 126, 127,
    ]

    # 1. Build merged reversed-key map (`rev_key -> flags`)
    rev_map: dict[bytes, int] = {}
    for d in domains:
        d_clean = d.strip().strip(".").lower()
        if d_clean and len(d_clean) <= 255:
            rev = d_clean.encode("ascii", errors="ignore")[::-1].replace(b".", b"\x01")
            if rev:
                rev_map[rev] = rev_map.get(rev, 0) | DOM_FLAG_EXACT
    for s in suffixes:
        s_clean = s.strip().strip(".").lower()
        if s_clean and len(s_clean) <= 255:
            rev = s_clean.encode("ascii", errors="ignore")[::-1].replace(b".", b"\x01")
            if rev:
                rev_map[rev] = rev_map.get(rev, 0) | DOM_FLAG_SUFFIX

    # Prune redundant subdomain keys covered by an ancestor with DOM_FLAG_SUFFIX
    kept_keys: list[tuple[bytes, int]] = []
    active_suffix: bytes | None = None
    for rev_key, flags in sorted(rev_map.items()):
        if active_suffix is not None:
            if (
                len(rev_key) > len(active_suffix)
                and rev_key[len(active_suffix)] == 1
                and rev_key.startswith(active_suffix)
            ):
                continue
            active_suffix = None
        if flags & DOM_FLAG_SUFFIX:
            active_suffix = rev_key
        kept_keys.append((rev_key, flags))

    # Pack `kept_keys` into Front-Coded Blocks of 128 keys with 87-token dictionary
    dom_blocks_buf = bytearray()
    dom_data = bytearray()
    use_dom_dict = False

    if kept_keys:
        use_dom_dict = True
        raw_suffixes: list[bytes] = []
        for i in range(0, len(kept_keys), DOMAIN_BLOCK_SIZE):
            chunk = kept_keys[i : i + DOMAIN_BLOCK_SIZE]
            prev_key = b""
            for idx, (key, _) in enumerate(chunk):
                if idx == 0:
                    shared = 0
                else:
                    max_common = min(len(prev_key), len(key), 255)
                    shared = 0
                    while shared < max_common and prev_key[shared] == key[shared]:
                        shared += 1
                raw_suffixes.append(key[shared:])
                prev_key = key

        dict_tokens: list[tuple[int, bytes]] = []
        if len(raw_suffixes) >= 32:
            step = max(1, len(raw_suffixes) // 20000)
            counts: dict[bytes, int] = {}
            for s in raw_suffixes[::step]:
                slen = len(s)
                for l in (2, 3, 4, 5, 6, 7, 8, 10, 12, 15):
                    if slen >= l:
                        for j in range(slen - l + 1):
                            sub = s[j : j + l]
                            counts[sub] = counts.get(sub, 0) + 1
            candidates: list[tuple[int, bytes]] = []
            for sub, cnt in counts.items():
                gain = cnt * (len(sub) - 1) * step
                if gain > 64:
                    candidates.append((gain, sub))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            chosen: list[bytes] = []
            for _, sub in candidates:
                if len(chosen) >= len(UNUSED_7BIT_CODES):
                    break
                if any((c in sub or sub in c) for c in chosen):
                    continue
                chosen.append(sub)
            dict_tokens = [(UNUSED_7BIT_CODES[idx], sub) for idx, sub in enumerate(chosen)]

        dom_data.append(len(dict_tokens))
        for code, tok in dict_tokens:
            dom_data.append(code)
            dom_data.append(len(tok))
            dom_data.extend(tok)

        by_first: list[list[tuple[bytes, int]]] = [[] for _ in range(128)]
        for code, tok in dict_tokens:
            by_first[tok[0]].append((tok, code))
        for bucket_list in by_first:
            bucket_list.sort(key=lambda item: -len(item[0]))

        for i in range(0, len(kept_keys), DOMAIN_BLOCK_SIZE):
            chunk = kept_keys[i : i + DOMAIN_BLOCK_SIZE]
            block_offset = len(dom_data)
            first_key = chunk[0][0]
            first_prefix_be = int.from_bytes(first_key[:4].ljust(4, b"\x00"), "big")
            prev_key = b""
            for idx, (key, flags) in enumerate(chunk):
                if idx == 0:
                    shared = 0
                else:
                    max_common = min(len(prev_key), len(key), 255)
                    shared = 0
                    while shared < max_common and prev_key[shared] == key[shared]:
                        shared += 1
                suffix_bytes = key[shared:]
                if shared < 63:
                    dom_data.append((flags << 6) | shared)
                else:
                    dom_data.append((flags << 6) | 63)
                    dom_data.append(shared - 63)
                tok_bytes = bytearray()
                p = 0
                slen = len(suffix_bytes)
                while p < slen:
                    b0 = suffix_bytes[p]
                    matched_code = 0
                    matched_len = 0
                    if b0 < 128 and by_first[b0]:
                        rem = suffix_bytes[p:]
                        for tok, code in by_first[b0]:
                            if rem.startswith(tok):
                                matched_code = code
                                matched_len = len(tok)
                                break
                    if matched_len > 0:
                        tok_bytes.append(matched_code)
                        p += matched_len
                    else:
                        tok_bytes.append(b0)
                        p += 1
                tok_bytes[-1] |= 0x80
                dom_data.extend(tok_bytes)
                prev_key = key
            dom_blocks_buf.extend(struct.pack("<II", first_prefix_be, block_offset))
    dom_blocks_count = (len(kept_keys) + DOMAIN_BLOCK_SIZE - 1) // DOMAIN_BLOCK_SIZE if kept_keys else 0

    # 2. Keywords + 64-bit trigram Bloom filter
    sorted_keywords = sorted({k.strip().lower() for k in keywords if k.strip()})
    string_table = bytearray()
    keywords_buf = bytearray()
    kw_bloom = 0
    has_short_kw = False
    for k in sorted_keywords:
        kb = k.encode("utf-8")
        offset = len(string_table)
        if len(kb) < 3:
            has_short_kw = True
        else:
            h = (((kb[0] * 31) & 0xFFFFFFFF) + kb[1]) & 0xFFFFFFFF
            h = (((h * 31) & 0xFFFFFFFF) + kb[2]) & 0xFFFFFFFF
            kw_bloom |= 1 << (h & 63)
        string_table.extend(kb)
        keywords_buf.extend(struct.pack("<IHH", offset, len(kb), 0))
    if has_short_kw:
        kw_bloom = 0xFFFFFFFFFFFFFFFF
    keywords_count = len(sorted_keywords)

    # 3. IPv4 & IPv6 merged intervals + 257-entry L1 directories
    v4_raw: list[tuple[int, int]] = []
    v6_raw: list[tuple[int, int]] = []
    for cidr in ip_cidrs:
        try:
            net = ipaddress.ip_network(cidr.strip(), strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                v4_raw.append((int(net.network_address), int(net.broadcast_address)))
            else:
                v6_raw.append((int(net.network_address), int(net.broadcast_address)))
        except ValueError:
            continue

    merged_v4 = _merge_intervals(v4_raw)
    v4_dir_buf = bytearray()
    v4_intervals_buf = bytearray()
    if merged_v4:
        v4_first_octets = [s >> 24 for s, _ in merged_v4]
        for octet in range(256):
            idx = bisect.bisect_left(v4_first_octets, octet)
            v4_dir_buf.extend(struct.pack("<I", idx))
        v4_dir_buf.extend(struct.pack("<I", len(merged_v4)))
        for s, e in merged_v4:
            v4_intervals_buf.extend(struct.pack("<II", s, e))
    v4_count = len(merged_v4)

    merged_v6 = _merge_intervals(v6_raw)
    v6_dir_buf = bytearray()
    v6_intervals_buf = bytearray()
    v6_is_64bit = bool(merged_v6) and all(
        (s & 0xFFFFFFFFFFFFFFFF) == 0 and (e & 0xFFFFFFFFFFFFFFFF) == 0xFFFFFFFFFFFFFFFF
        for s, e in merged_v6
    )
    if merged_v6:
        v6_first_bytes = [s >> 120 for s, _ in merged_v6]
        for byte0 in range(256):
            idx = bisect.bisect_left(v6_first_bytes, byte0)
            v6_dir_buf.extend(struct.pack("<I", idx))
        v6_dir_buf.extend(struct.pack("<I", len(merged_v6)))
        if v6_is_64bit:
            for s, e in merged_v6:
                v6_intervals_buf.extend(struct.pack("<QQ", s >> 64, e >> 64))
        else:
            for s, e in merged_v6:
                v6_intervals_buf.extend(s.to_bytes(16, "big"))
                v6_intervals_buf.extend(e.to_bytes(16, "big"))
    v6_count = len(merged_v6)

    # 4. Extra fallback rules blob (for domain_regex if present)
    if regexes:
        extra_bytes = json.dumps(
            [{"domain_regex": list(regexes)}], separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    else:
        extra_bytes = b""

    def align_8(pos: int) -> int:
        return (pos + 7) & ~7

    pos = align_8(HEADER_SIZE)
    dom_blocks_offset = pos
    pos += len(dom_blocks_buf)

    pos = align_8(pos)
    dom_data_offset = pos
    dom_data_len = len(dom_data)
    pos += dom_data_len

    pos = align_8(pos)
    keywords_offset = pos
    pos += len(keywords_buf)

    pos = align_8(pos)
    if v4_count == 0:
        v4_dir_offset = 0
    else:
        v4_dir_offset = pos
        pos += len(v4_dir_buf)

    pos = align_8(pos)
    v4_intervals_offset = pos
    pos += len(v4_intervals_buf)

    pos = align_8(pos)
    if v6_count == 0:
        v6_dir_offset = 0
    else:
        v6_dir_offset = pos
        pos += len(v6_dir_buf)

    pos = align_8(pos)
    v6_intervals_offset = pos
    pos += len(v6_intervals_buf)

    pos = align_8(pos)
    dest_ports_offset = pos
    dest_ports_count = 0

    pos = align_8(pos)
    string_table_offset = pos
    string_table_len = len(string_table)
    pos += string_table_len

    pos = align_8(pos)
    extra_offset = pos
    extra_len = len(extra_bytes)
    pos += extra_len

    header_flags = HEADER_FLAG_HAS_EXTRA if extra_bytes else 0
    if use_dom_dict:
        header_flags |= HEADER_FLAG_DOM_DICT
    if v6_is_64bit:
        header_flags |= HEADER_FLAG_V6_64BIT
    header = struct.pack(
        "<4sIIIQ18I",
        b"TSRS",
        2,
        header_flags,
        total_rules,
        kw_bloom,
        dom_blocks_offset,
        dom_blocks_count,
        dom_data_offset,
        dom_data_len,
        keywords_offset,
        keywords_count,
        v4_dir_offset,
        v4_intervals_offset,
        v4_count,
        v6_dir_offset,
        v6_intervals_offset,
        v6_count,
        dest_ports_offset,
        dest_ports_count,
        string_table_offset,
        string_table_len,
        extra_offset,
        extra_len,
    )

    out = bytearray(pos)
    out[0:HEADER_SIZE] = header
    if dom_blocks_buf:
        out[dom_blocks_offset : dom_blocks_offset + len(dom_blocks_buf)] = dom_blocks_buf
    if dom_data:
        out[dom_data_offset : dom_data_offset + dom_data_len] = dom_data
    if keywords_buf:
        out[keywords_offset : keywords_offset + len(keywords_buf)] = keywords_buf
    if v4_dir_buf:
        out[v4_dir_offset : v4_dir_offset + len(v4_dir_buf)] = v4_dir_buf
    if v4_intervals_buf:
        out[v4_intervals_offset : v4_intervals_offset + len(v4_intervals_buf)] = v4_intervals_buf
    if v6_dir_buf:
        out[v6_dir_offset : v6_dir_offset + len(v6_dir_buf)] = v6_dir_buf
    if v6_intervals_buf:
        out[v6_intervals_offset : v6_intervals_offset + len(v6_intervals_buf)] = v6_intervals_buf
    if string_table:
        out[string_table_offset : string_table_offset + string_table_len] = string_table
    if extra_bytes:
        out[extra_offset : extra_offset + extra_len] = extra_bytes
    return bytes(out)


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
        formats = [
            f"[`.trs`](release/tachyon/{lid}.trs)",
            f"[`.srs`](release/sing-box/{lid}.srs)",
            f"[`.json`](release/json/{lid}.json)",
        ]
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

    tachyon_bin = ensure_tachyon_core_binary()
    log(f"Using tachyon-core compiler: {tachyon_bin}")

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
    for sub in ("tachyon", "sing-box", "json", "domains", "subnets", "subnets/ipv4", "subnets/ipv6"):
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
                    sub = parse_srs_url(url, tachyon_bin)
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
        trs_path = RELEASE_DIR / "tachyon" / f"{lid}.trs"
        json_path.write_text(json.dumps(srs_json_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        compile_jobs.append((lid, json_path, srs_path))

        # Compile native zero-copy TRS v2 directly in Python
        trs_bytes = compile_tsrs_v2(
            domains=rule_obj.get("domain", []),
            suffixes=rule_obj.get("domain_suffix", []),
            keywords=rule_obj.get("domain_keyword", []),
            regexes=rule_obj.get("domain_regex", []),
            ip_cidrs=rule_obj.get("ip_cidr", []),
            total_rules=1,
        )
        trs_path.write_bytes(trs_bytes)
        trs_sha256 = hashlib.sha256(trs_bytes).hexdigest()

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
                    "trs": f"release/tachyon/{lid}.trs",
                    "srs": f"release/sing-box/{lid}.srs",
                    "json": f"release/json/{lid}.json",
                    "domains": f"release/domains/{lid}.lst" if all_flat_domains else None,
                    "subnets": f"release/subnets/{lid}.lst" if (v4_cidrs or v6_cidrs) else None,
                },
                "sha256_trs": trs_sha256,
            }
        )

    # Compile all .json -> .srs in parallel using tachyon-core
    log(f"Compiling {len(compile_jobs)} binary .srs rule-sets via tachyon-core...")

    def compile_one(job: tuple[str, Path, Path]) -> tuple[str, str]:
        lid, jpath, spath = job
        res = subprocess.run(
            [str(tachyon_bin), "rule-set", "compile", str(jpath), "-o", str(spath)],
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
