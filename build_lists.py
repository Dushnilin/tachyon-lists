#!/usr/bin/env python3
"""
Tachyon Lists Builder (build_lists.py)
======================================
Automated multi-source reparser, deduplicator, and compiler for Tachyon routing lists.

Pulls from upstream sources:
- runetfreedom/russia-v2ray-rules-dat (`.srs`)
- runetfreedom/russia-blocked-geosite / russia-blocked-geoip
- MetaCubeX/meta-rules-dat (`sing` branch `.json` / `.srs` and `meta/asn/*.list`)
- hydraponique/roscomvpn-geosite & roscomvpn-geoip (`.srs` / `.txt`)
- 1andrevich/Re-filter-lists & dartraiden/no-russia-hosts & antifilter.download
- hagezi/dns-blocklists, firehol/blocklist-ipsets, zxc-rv/ad-filter, StevenBlack/hosts
- Official ASN/IP feeds (RIPE Stat, AWS, Google, Cloudflare, DigitalOcean, Telegram)

Outputs:
- release/tachyon/<id>.trs        (compiled Tachyon zero-copy mmap rule-set v2)
- release/sing-box/<id>.srs       (compiled sing-box binary rule-set)
- release/json/<id>.json          (sing-box source rule-set v3)
- release/domains/<id>.lst        (flat domain list for dnsmasq / routing engines)
- release/subnets/<id>.lst        (combined collapsed IPv4 + IPv6 CIDRs for nftables)
- release/subnets/ipv4/<id>.lst   (collapsed IPv4 CIDRs)
- release/subnets/ipv6/<id>.lst   (collapsed IPv6 CIDRs)
- release/catalog.json            (metadata, descriptions, counts, SHA-256 hashes)
- release/sha256sums.txt          (deterministic SHA-256 checksums for release artifacts)
"""

from __future__ import annotations

import argparse
import bisect
import concurrent.futures
import csv
import email.utils
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse
import uuid
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
TACHYON_CORE_FALLBACK_TAG = "v0.5.7"
DEFAULT_CACHE_TTL_SECONDS = int(os.environ.get("TACHYON_CACHE_TTL", "3600"))
ADS_ALL_JSON_URL = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/geosite/category-ads-all.json"

BUILTIN_TELEMETRY_EXACT_DOMAINS: frozenset[str] = frozenset({
    "openai.qualtrics.com",
    "ubisoft.siteintercept.qualtrics.com",
    "browser-intake-datadoghq.com",
    "logs.datadoghq.com",
    "ads.vk.ru",
    "ads.tiktok.com",
    "ads.reddit.com",
    "ads.x.com",
})

BUILTIN_TELEMETRY_SUFFIX_DOMAINS: frozenset[str] = frozenset({
    "crashlytics.com",
    "app-measurement.com",
    "app-measurement-cn.com",
    "lgsmartad.com",
    "samsungacr.com",
    "samsungads.com",
    "yandexadexchange.net",
})

PROTECTED_PUSH_DOMAINS: frozenset[str] = frozenset({
    "alt1-mtalk.google.com",
    "alt2-mtalk.google.com",
    "alt3-mtalk.google.com",
    "alt4-mtalk.google.com",
    "alt5-mtalk.google.com",
    "alt6-mtalk.google.com",
    "alt7-mtalk.google.com",
    "alt8-mtalk.google.com",
    "android.clients.google.com",
    "connectivitycheck.android.com",
    "connectivitycheck.gstatic.com",
    "device-provisioning.googleapis.com",
    "fcm.googleapis.com",
    "firebaseinstallations.googleapis.com",
    "mtalk.google.com",
    "mtalk4.google.com",
    "push.apple.com",
    "time.android.com",
})


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
    """Locate tachyon-core on PATH, sibling repo, or download latest official release binary into .cache/bin/."""
    env_bin = os.environ.get("TACHYON_CORE_BIN")
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)

    which_bin = shutil.which("tachyon-core")
    if which_bin:
        return Path(which_bin)

    exe_name = "tachyon-core.exe" if platform.system().lower() == "windows" else "tachyon-core"
    for profile in ("release", "debug"):
        sibling_bin = ROOT_DIR.parent / "tachyon-core" / "target" / profile / exe_name
        if sibling_bin.is_file():
            return sibling_bin

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


_CACHE_LOCK = threading.Lock()
_URL_LOCKS: dict[str, threading.Lock] = {}
_SESSION_VERIFIED_URLS: set[str] = set()
_SESSION_FETCH_ERRORS: dict[str, str] = {}


def reset_fetch_cache() -> None:
    with _CACHE_LOCK:
        _SESSION_VERIFIED_URLS.clear()
        _SESSION_FETCH_ERRORS.clear()


def _get_url_lock(url: str) -> threading.Lock:
    with _CACHE_LOCK:
        lock = _URL_LOCKS.get(url)
        if lock is None:
            lock = threading.Lock()
            _URL_LOCKS[url] = lock
        return lock


def url_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    tail = url.rstrip("/").split("/")[-1]
    safe_tail = "".join(c if c.isalnum() or c in "._-" else "_" for c in tail)[:48]
    return DOWNLOAD_CACHE_DIR / f"{digest}_{safe_tail}"


def _url_meta_path(cpath: Path) -> Path:
    return cpath.with_name(f"{cpath.name}.meta.json")


def _read_cache_meta(meta_path: Path) -> dict[str, Any]:
    if not meta_path.is_file():
        return {}
    try:
        obj = json.loads(meta_path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_atomic_bytes(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}.{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def _parse_retry_after(header_val: str | None) -> float | None:
    if not header_val:
        return None
    val = header_val.strip()
    try:
        secs = float(val)
        return min(max(secs, 0.5), 30.0)
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(delta, 0.5), 30.0)
    except Exception:
        return None


def _validate_fetched_payload(url: str, data: bytes, content_type: str, expected_format: str | None) -> None:
    if not data:
        raise ValueError(f"Empty response body from {url}")

    head = data[:512].lstrip()
    head_lower = head.lower()
    if head_lower.startswith((b"<!doctype", b"<html", b"<head")) or (
        "text/html" in content_type.lower() and head_lower.startswith(b"<")
    ):
        raise ValueError(f"Unexpected HTML response instead of rule data from {url}")

    fmt = expected_format
    if fmt is None:
        clean_path = url.lower().split("?", 1)[0]
        if clean_path.endswith(".srs"):
            fmt = "srs"

    if fmt == "srs":
        if len(data) < 4 or data[:3] != b"SRS" or not (1 <= data[3] <= 10):
            raise ValueError(f"Invalid .srs magic bytes from {url}")
    elif fmt == "json":
        stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
        if not stripped.startswith((b"{", b"[")):
            raise ValueError(f"Invalid JSON payload header from {url}")


def fetch_url_bytes(
    url: str,
    retries: int = 3,
    *,
    refresh: bool = False,
    cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
    expected_format: str | None = None,
) -> bytes:
    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = url_cache_path(url)
    meta_path = _url_meta_path(cpath)

    with _CACHE_LOCK:
        prev_err = _SESSION_FETCH_ERRORS.get(url)
    if prev_err is not None:
        raise RuntimeError(prev_err)

    url_lock = _get_url_lock(url)
    with url_lock:
        with _CACHE_LOCK:
            prev_err = _SESSION_FETCH_ERRORS.get(url)
            already_verified = url in _SESSION_VERIFIED_URLS
        if prev_err is not None:
            raise RuntimeError(prev_err)

        has_cached = cpath.is_file() and cpath.stat().st_size > 0
        meta = _read_cache_meta(meta_path) if has_cached else {}

        if has_cached and already_verified:
            cached_bytes = cpath.read_bytes()
            _validate_fetched_payload(url, cached_bytes, "", expected_format)
            return cached_bytes

        if has_cached and not refresh and cache_ttl > 0:
            fetched_at = float(meta.get("fetched_at", 0.0) or 0.0)
            if fetched_at > 0 and (time.time() - fetched_at) < cache_ttl:
                try:
                    cached_bytes = cpath.read_bytes()
                    _validate_fetched_payload(url, cached_bytes, "", expected_format)
                    with _CACHE_LOCK:
                        _SESSION_VERIFIED_URLS.add(url)
                    return cached_bytes
                except ValueError:
                    pass

        last_err: Exception | None = None
        for attempt in range(1, max(1, retries) + 1):
            headers = {"User-Agent": USER_AGENT}
            if has_cached:
                if meta.get("etag"):
                    headers["If-None-Match"] = str(meta["etag"])
                if meta.get("last_modified"):
                    headers["If-Modified-Since"] = str(meta["last_modified"])
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                    _validate_fetched_payload(url, data, ctype, expected_format)
                    _write_atomic_bytes(cpath, data)
                    new_meta = {
                        "url": url,
                        "etag": resp.headers.get("ETag", ""),
                        "last_modified": resp.headers.get("Last-Modified", ""),
                        "content_type": ctype,
                        "fetched_at": time.time(),
                    }
                    _write_atomic_bytes(meta_path, json.dumps(new_meta, ensure_ascii=False).encode("utf-8"))
                    with _CACHE_LOCK:
                        _SESSION_VERIFIED_URLS.add(url)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and has_cached:
                    cached_bytes = cpath.read_bytes()
                    _validate_fetched_payload(url, cached_bytes, "", expected_format)
                    new_meta = dict(meta)
                    new_meta["url"] = url
                    new_meta["fetched_at"] = time.time()
                    if exc.headers and exc.headers.get("ETag"):
                        new_meta["etag"] = exc.headers.get("ETag")
                    if exc.headers and exc.headers.get("Last-Modified"):
                        new_meta["last_modified"] = exc.headers.get("Last-Modified")
                    _write_atomic_bytes(meta_path, json.dumps(new_meta, ensure_ascii=False).encode("utf-8"))
                    with _CACHE_LOCK:
                        _SESSION_VERIFIED_URLS.add(url)
                    return cached_bytes

                last_err = exc
                # Do not retry permanent 4xx client errors (404, 410, etc.) except 408/429
                if 400 <= exc.code < 500 and exc.code not in (408, 429):
                    break
                if attempt < retries:
                    delay = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                    time.sleep(delay if delay is not None else 0.8 * attempt)
            except ValueError as exc:
                last_err = exc
                # Invalid payload (empty/HTML/bad magic)
                if attempt < retries:
                    time.sleep(0.8 * attempt)
            except Exception as exc:
                last_err = exc
                if attempt < retries:
                    time.sleep(0.8 * attempt)

        err_msg = f"Failed to fetch {url}: {last_err}"
        with _CACHE_LOCK:
            _SESSION_FETCH_ERRORS[url] = err_msg
        raise RuntimeError(err_msg)


_DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_DOMAIN_BAD_CHARS = set("/:\\$^*|,;?&=!@#[](){}<>\"'`~+\t\r\n")

_BLOCKED_SINGLE_LABEL_TLDS: frozenset[str] = frozenset({
    "ua", "psk", "onedrive", "onedriveupdater", "exit", "tor", "onion",
    "localhost", "local", "lan", "home", "internal", "invalid", "test",
    "example", "arpa", "corp", "intranet", "localdomain",
})

_RU_SINGLE_LABEL_TLDS: frozenset[str] = frozenset({
    "ru", "su", "moscow", "tatar", "yandex", "by", "kz",
})

_ALLOWED_BRAND_TLDS: dict[str, frozenset[str]] = {
    "apple": frozenset({"apple"}),
    "google_services": frozenset({
        "android", "chrome", "dclk", "gbiz", "gle", "gmail", "goog",
        "google", "guge", "hangout", "nexus", "xn--flw351e", "xn--qcka1pmc",
    }),
    "youtube": frozenset({"youtube"}),
    "microsoft": frozenset({
        "azure", "bing", "hotmail", "microsoft", "office", "skype", "windows", "xbox",
    }),
    "playstation": frozenset({"playstation"}),
    "xbox": frozenset({"xbox"}),
    "games_all": frozenset({"playstation", "xbox"}),
    "porn": frozenset({"adult", "porn", "sex", "xxx"}),
    "shopping_global": frozenset({
        "amazon", "aws", "imdb", "kindle", "nike",
        "xn--cckwcxetd", "xn--jlq480n2rg", "yamaxun", "zappos",
    }),
    "smart_cars": frozenset({"bmw", "mini"}),
    "smarttv_apps": frozenset({"samsung", "xn--cg4bki"}),
    "streaming_west": frozenset({"hbo"}),
    "tmdb_metadata": frozenset({"imdb"}),
}


def _is_ip_literal(value: str) -> bool:
    candidate = value.strip()
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def clean_domain(raw: str) -> str:
    d = raw.strip().lower()
    if not d:
        return ""
    if d.startswith("*."):
        d = d[2:]
    if d.endswith(".") and not d.endswith(".."):
        d = d[:-1]
    if not d or d.startswith(".") or ".." in d:
        return ""
    if any(c.isspace() for c in d) or any(c in _DOMAIN_BAD_CHARS for c in d):
        return ""
    if _is_ip_literal(d):
        return ""

    if not d.isascii():
        encoded_labels: list[str] = []
        for lbl in d.split("."):
            if not lbl:
                return ""
            if lbl.isascii():
                encoded_labels.append(lbl)
            else:
                try:
                    encoded_labels.append(lbl.encode("idna").decode("ascii").lower())
                except UnicodeError:
                    return ""
        d = ".".join(encoded_labels)
        if d.endswith(".") and not d.endswith(".."):
            d = d[:-1]

    if not d or len(d) > 253 or not d.isascii() or d.startswith(".") or ".." in d:
        return ""

    labels = d.split(".")
    for lbl in labels:
        if not _DOMAIN_LABEL_RE.match(lbl):
            return ""
    tld = labels[-1]
    if len(tld) < 2 or tld.isdigit() or "_" in tld:
        return ""
    return d


def _is_allowed_domain(dom: str, list_id: str = "", *, is_suffix: bool = True) -> bool:
    if not dom:
        return False
    if "." in dom:
        return True
    if not is_suffix:
        return False
    if dom in _BLOCKED_SINGLE_LABEL_TLDS:
        return False
    if list_id in ("ru_domains", "geosite_ru"):
        return dom in _RU_SINGLE_LABEL_TLDS or dom.startswith("xn--")
    allowed = _ALLOWED_BRAND_TLDS.get(list_id)
    return bool(allowed and dom in allowed)


def _parse_attr_filters(attr_spec: Any) -> tuple[frozenset[str], frozenset[str]]:
    if not attr_spec:
        return frozenset(), frozenset()
    if isinstance(attr_spec, str):
        tokens = [t for part in attr_spec.split(",") for t in part.split() if t.strip()]
    elif isinstance(attr_spec, (list, tuple, set, frozenset)):
        tokens = [str(t).strip() for item in attr_spec for t in str(item).replace(",", " ").split() if t.strip()]
    else:
        return frozenset(), frozenset()

    pos: set[str] = set()
    neg: set[str] = set()
    for tok in tokens:
        t = tok.strip().lower()
        if not t:
            continue
        if t.startswith(("-@", "@-")):
            name = t[2:]
            if name:
                neg.add(name)
        elif t.startswith("-"):
            name = t[1:].lstrip("@!")
            if name:
                neg.add(name)
        elif t.startswith(("@!", "!@")):
            name = t[2:]
            if name:
                neg.add(name)
        elif t.startswith("!"):
            name = t[1:]
            if name:
                neg.add(name)
        else:
            name = t.lstrip("@")
            if name:
                pos.add(name)
    return frozenset(pos), frozenset(neg)


def parse_dlc_entry(
    base_url: str,
    entry: str,
    required_attr: str | list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
    visited: set[tuple[str, tuple[str, ...], tuple[str, ...]]] | None = None,
    exclude_attrs: set[str] | list[str] | tuple[str, ...] | frozenset[str] | None = None,
    *,
    refresh: bool = False,
) -> RuleBucket:
    """Recursively parse a v2ray domain-list-community data/<entry> file."""
    if visited is None:
        visited = set()

    req_pos, req_neg = _parse_attr_filters(required_attr)
    if exclude_attrs:
        req_neg = req_neg | frozenset(str(a).strip().lstrip("@!").lower() for a in exclude_attrs if str(a).strip())

    if not req_pos.isdisjoint(req_neg):
        return RuleBucket()

    key = (entry, tuple(sorted(req_pos)), tuple(sorted(req_neg)))
    if key in visited:
        return RuleBucket()
    visited.add(key)

    url = f"{base_url.rstrip('/')}/{entry}"
    try:
        text = fetch_url_bytes(url, refresh=refresh).decode("utf-8", errors="replace")
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
        raw_line_attrs = [p for p in parts[1:] if p.startswith(("@", "-@", "!@"))]

        if token.startswith("include:"):
            inc_target = token[len("include:") :].strip()
            if not inc_target:
                continue
            inc_pos, inc_neg = _parse_attr_filters(raw_line_attrs)
            child_pos = req_pos | inc_pos
            child_neg = req_neg | inc_neg
            if not child_pos.isdisjoint(child_neg):
                continue
            child_filters = [f"@{a}" for a in sorted(child_pos)] + [f"-@{a}" for a in sorted(child_neg)]
            sub_bucket = parse_dlc_entry(
                base_url,
                inc_target,
                child_filters or None,
                visited,
                refresh=refresh,
            )
            bucket.merge(sub_bucket)
            continue

        line_attrs = {p[1:].lower() for p in raw_line_attrs if len(p) > 1}
        if req_pos and not req_pos.issubset(line_attrs):
            continue
        if req_neg and not req_neg.isdisjoint(line_attrs):
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


def _add_network_to_bucket(bucket: RuleBucket, token: str) -> None:
    try:
        net = ipaddress.ip_network(token.strip(), strict=False)
        if not _is_bogon_or_private(net):
            bucket.ip_cidr.add(str(net))
    except ValueError:
        pass


def parse_singbox_json_bytes(raw_bytes: bytes) -> RuleBucket:
    bucket = RuleBucket()
    obj = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    rules = obj.get("rules", [])
    if not isinstance(rules, list):
        return bucket

    def collect_rule(rule: dict[str, Any]) -> None:
        if rule.get("invert"):
            return
        for k, target in (
            ("domain", bucket.domain),
            ("domain_suffix", bucket.domain_suffix),
            ("domain_keyword", bucket.domain_keyword),
            ("domain_regex", bucket.domain_regex),
            ("ip_cidr", bucket.ip_cidr),
        ):
            val = rule.get(k)
            items = [val] if isinstance(val, str) else val if isinstance(val, list) else []
            for item in items:
                if not isinstance(item, str) or not item.strip():
                    continue
                if k in ("domain", "domain_suffix"):
                    cleaned = clean_domain(item)
                    if cleaned:
                        target.add(cleaned)
                elif k == "ip_cidr":
                    _add_network_to_bucket(bucket, item)
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


def parse_srs_url(url: str, sing_box_bin: Path, *, refresh: bool = False) -> RuleBucket:
    DECOMPILED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    raw_bytes = fetch_url_bytes(url, refresh=refresh, expected_format="srs")
    if len(raw_bytes) < 4 or raw_bytes[:3] != b"SRS" or not (1 <= raw_bytes[3] <= 10):
        raise RuntimeError(f"Invalid .srs magic bytes for {url}")
    digest = hashlib.sha256(raw_bytes).hexdigest()[:24]
    json_cache = DECOMPILED_CACHE_DIR / f"{digest}.json"

    if not json_cache.is_file():
        unique_tag = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
        srs_tmp = DECOMPILED_CACHE_DIR / f"{digest}.{unique_tag}.srs.tmp"
        json_tmp = DECOMPILED_CACHE_DIR / f"{digest}.{unique_tag}.json.tmp"
        try:
            srs_tmp.write_bytes(raw_bytes)
            res = subprocess.run(
                [str(sing_box_bin), "rule-set", "decompile", str(srs_tmp), "-o", str(json_tmp)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0 or not json_tmp.is_file():
                raise RuntimeError(f"sing-box rule-set decompile failed for {url}: {res.stderr.strip()}")
            json_tmp.replace(json_cache)
        finally:
            srs_tmp.unlink(missing_ok=True)
            json_tmp.unlink(missing_ok=True)

    return parse_singbox_json_bytes(json_cache.read_bytes())


_HOSTS_IGNORED_NAMES: frozenset[str] = frozenset({
    "localhost",
    "localhost.localdomain",
    "local",
    "broadcasthost",
    "ip6-localhost",
    "ip6-loopback",
    "ip6-localnet",
    "ip6-mcastprefix",
    "ip6-allnodes",
    "ip6-allrouters",
    "ip6-allhosts",
})


def parse_plain_domains(text: str) -> RuleBucket:
    bucket = RuleBucket()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("!", "[", "@@")):
            continue

        parts = line.split()
        if len(parts) >= 2:
            if not _is_ip_literal(parts[0]):
                continue
            for host_tok in parts[1:]:
                if (
                    host_tok in _HOSTS_IGNORED_NAMES
                    or _is_ip_literal(host_tok)
                    or any(ch in host_tok for ch in "$*/|^,?=")
                ):
                    continue
                dom = clean_domain(host_tok)
                if dom and "." in dom:
                    bucket.domain_suffix.add(dom)
            continue

        if line.startswith("||"):
            rest = line[2:]
            if any(ch in rest for ch in "$*/|?=,"):
                continue
            if "^" in rest:
                dom_part, after = rest.split("^", 1)
                if after:
                    continue
                line = dom_part
            else:
                line = rest
        else:
            if line.startswith("*."):
                line = line[2:]
            if any(ch in line for ch in "$*/|^,?="):
                continue

        dom = clean_domain(line)
        if dom and "." in dom:
            bucket.domain_suffix.add(dom)
    return bucket


_IPV4_EXTRA_BOGONS: tuple[ipaddress.IPv4Network, ...] = (
    ipaddress.IPv4Network("100.64.0.0/10"),   # CGNAT (RFC 6598)
    ipaddress.IPv4Network("192.88.99.0/24"),  # Deprecated 6to4 relay (RFC 7526)
    ipaddress.IPv4Network("198.18.0.0/15"),   # Benchmarking / FakeIP (RFC 2544)
)

_IPV6_EXTRA_BOGONS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("2001:20::/28"),   # ORCHIDv2 (RFC 7343)
    ipaddress.IPv6Network("2001:db8::/32"),  # Documentation (RFC 3849)
    ipaddress.IPv6Network("fc00::/7"),       # ULA / FakeIPv6 (RFC 4193)
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped IPv6 addresses
    ipaddress.IPv6Network("64:ff9b::/96"),   # NAT64 well-known prefix (RFC 6052)
    ipaddress.IPv6Network("64:ff9b:1::/48"), # Local-use IPv4/IPv6 translation (RFC 8215)
    ipaddress.IPv6Network("::/96"),          # Deprecated IPv4-compatible IPv6 (RFC 4291)
    ipaddress.IPv6Network("100::/64"),       # Discard-only prefix (RFC 6666)
)


def _is_bogon_or_private(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    if (
        net.prefixlen == 0
        or net.is_private
        or net.is_loopback
        or net.is_link_local
        or net.is_multicast
        or net.is_reserved
        or net.is_unspecified
    ):
        return True
    if isinstance(net, ipaddress.IPv4Network):
        return any(net.overlaps(bogon) for bogon in _IPV4_EXTRA_BOGONS)
    return any(net.overlaps(bogon) for bogon in _IPV6_EXTRA_BOGONS)


def _is_too_broad_network(net: ipaddress.IPv4Network | ipaddress.IPv6Network, list_id: str = "") -> bool:
    if list_id.startswith("geoip_"):
        if isinstance(net, ipaddress.IPv4Network):
            return net.prefixlen < 7
        return net.prefixlen < 16
    if isinstance(net, ipaddress.IPv4Network):
        return net.prefixlen < 10
    return net.prefixlen < 20


def parse_plain_subnets(text: str) -> RuleBucket:
    bucket = RuleBucket()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        _add_network_to_bucket(bucket, token)
    return bucket


def _normalize_filter_set(val: Any, *, upper: bool = False) -> set[str] | None:
    if val is None:
        return None
    items = [val] if isinstance(val, str) else list(val) if isinstance(val, (list, tuple, set)) else []
    out = {str(x).strip().upper() if upper else str(x).strip() for x in items if str(x).strip()}
    return out or None


def parse_aws_ip_ranges(
    raw_bytes: bytes,
    service: str | list[str] | None = None,
    region: str | list[str] | None = None,
) -> RuleBucket:
    """Parse AWS ip-ranges.json with optional service/region filters."""
    bucket = RuleBucket()
    obj = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    svc_filter = _normalize_filter_set(service, upper=True)
    reg_filter = _normalize_filter_set(region, upper=True)

    for key_list, key_cidr in (("prefixes", "ip_prefix"), ("ipv6_prefixes", "ipv6_prefix")):
        entries = obj.get(key_list, [])
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, dict):
                continue
            if svc_filter and str(item.get("service", "")).upper() not in svc_filter:
                continue
            if reg_filter and str(item.get("region", "")).upper() not in reg_filter:
                continue
            cidr = item.get(key_cidr, "")
            if isinstance(cidr, str) and cidr:
                _add_network_to_bucket(bucket, cidr)
    return bucket


def parse_geofeed_csv(
    text: str,
    country: str | list[str] | None = None,
    region: str | list[str] | None = None,
) -> RuleBucket:
    """Parse RFC 8805 geofeed CSV (ip_prefix,country,region,city,postal)."""
    bucket = RuleBucket()
    country_filter = _normalize_filter_set(country, upper=True)
    region_filter = _normalize_filter_set(region, upper=True)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for row in csv.reader(lines):
        if not row:
            continue
        cidr = row[0].strip()
        if not cidr or cidr.startswith("#"):
            continue
        if country_filter and (len(row) < 2 or row[1].strip().upper() not in country_filter):
            continue
        if region_filter and (len(row) < 3 or row[2].strip().upper() not in region_filter):
            continue
        _add_network_to_bucket(bucket, cidr)
    return bucket


def parse_ripe_stat_asn(raw_bytes: bytes) -> RuleBucket:
    """Parse RIPE Stat announced-prefixes JSON response."""
    bucket = RuleBucket()
    obj = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    data = obj.get("data", {})
    if not isinstance(data, dict):
        return bucket
    query_endtime = data.get("query_endtime")
    prefixes = data.get("prefixes", [])
    if not isinstance(prefixes, list):
        return bucket
    for item in prefixes:
        if not isinstance(item, dict):
            continue
        prefix = item.get("prefix", "")
        timelines = item.get("timelines")
        if query_endtime and isinstance(timelines, list) and timelines:
            if not any(isinstance(t, dict) and t.get("endtime") == query_endtime for t in timelines):
                continue
        if isinstance(prefix, str) and prefix:
            _add_network_to_bucket(bucket, prefix)
    return bucket


def parse_google_ip_ranges(
    raw_bytes: bytes,
    service: str | list[str] | None = None,
    scope: str | list[str] | None = None,
) -> RuleBucket:
    """Parse Google IP ranges JSON (goog.json / cloud.json)."""
    bucket = RuleBucket()
    obj = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    svc_filter = _normalize_filter_set(service, upper=True)
    scope_filter = _normalize_filter_set(scope, upper=True)
    prefixes = obj.get("prefixes", [])
    if not isinstance(prefixes, list):
        return bucket
    for item in prefixes:
        if not isinstance(item, dict):
            continue
        if svc_filter and str(item.get("service", "")).upper() not in svc_filter:
            continue
        if scope_filter and str(item.get("scope", "")).upper() not in scope_filter:
            continue
        cidr = item.get("ipv4Prefix") or item.get("ipv6Prefix") or ""
        if isinstance(cidr, str) and cidr:
            _add_network_to_bucket(bucket, cidr)
    return bucket


def parse_cloudflare_ips(raw_bytes: bytes, include_jdcloud: bool = False) -> RuleBucket:
    """Parse official Cloudflare IP ranges (JSON v4 API or plain-text ips-v4/ips-v6)."""
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        bucket = RuleBucket()
        obj = json.loads(text)
        if obj.get("success") is False:
            raise ValueError(f"Cloudflare IP API returned success=False: {obj.get('errors')}")
        result = obj.get("result", {})
        if not isinstance(result, dict):
            return bucket
        keys = ["ipv4_cidrs", "ipv6_cidrs"]
        if include_jdcloud:
            keys.append("jdcloud_cidrs")
        for key in keys:
            cidrs = result.get(key, [])
            if isinstance(cidrs, list):
                for cidr in cidrs:
                    if isinstance(cidr, str) and cidr:
                        _add_network_to_bucket(bucket, cidr)
        return bucket
    return parse_plain_subnets(text)


def parse_clash_yaml(text: str) -> RuleBucket:
    """Parse Clash / Mihomo YAML rule-set payload (classical, domain, or ipcidr)."""
    bucket = RuleBucket()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line == "payload:":
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        line = line.strip("'\"")
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("DOMAIN-SUFFIX,"):
            tok = line.split(",", 2)[1].strip()
            if tok.startswith(".") and not tok.startswith(".."):
                tok = tok[1:]
            dom = clean_domain(tok)
            if dom:
                bucket.domain_suffix.add(dom)
        elif upper.startswith("DOMAIN,"):
            dom = clean_domain(line.split(",", 2)[1])
            if dom:
                bucket.domain.add(dom)
        elif upper.startswith("DOMAIN-KEYWORD,"):
            kw = line.split(",", 2)[1].strip().lower()
            if kw:
                bucket.domain_keyword.add(kw)
        elif upper.startswith(("IP-CIDR,", "IP-CIDR6,")):
            cidr_str = line.split(",", 2)[1].strip()
            _add_network_to_bucket(bucket, cidr_str)
        elif line.startswith("+.") or line.startswith("*."):
            dom = clean_domain(line[2:])
            if dom:
                bucket.domain_suffix.add(dom)
        else:
            try:
                net = ipaddress.ip_network(line, strict=False)
                if not _is_bogon_or_private(net):
                    bucket.ip_cidr.add(str(net))
            except ValueError:
                dom = clean_domain(line)
                if dom and "." in dom:
                    bucket.domain_suffix.add(dom)
    return bucket


def _exclude_networks_by_version(nets: list[Any], excludes: list[Any], addr_cls: Any) -> list[str]:
    if not nets:
        return []
    collapsed_nets = list(ipaddress.collapse_addresses(nets))
    if not excludes:
        return [str(n) for n in collapsed_nets]
    collapsed_ex = list(ipaddress.collapse_addresses(excludes))

    net_ranges = [(int(n.network_address), int(n.broadcast_address)) for n in collapsed_nets]
    ex_ranges = [(int(e.network_address), int(e.broadcast_address)) for e in collapsed_ex]

    remaining: list[tuple[int, int]] = []
    i = 0
    j = 0
    n_len = len(net_ranges)
    e_len = len(ex_ranges)
    cur_s, cur_e = net_ranges[0]

    while i < n_len:
        if j >= e_len:
            remaining.append((cur_s, cur_e))
            i += 1
            if i < n_len:
                cur_s, cur_e = net_ranges[i]
            continue

        ex_s, ex_e = ex_ranges[j]
        if ex_e < cur_s:
            j += 1
        elif ex_s > cur_e:
            remaining.append((cur_s, cur_e))
            i += 1
            if i < n_len:
                cur_s, cur_e = net_ranges[i]
        else:
            if cur_s < ex_s:
                remaining.append((cur_s, ex_s - 1))
            if ex_e < cur_e:
                cur_s = ex_e + 1
                j += 1
            else:
                i += 1
                if i < n_len:
                    cur_s, cur_e = net_ranges[i]

    out: list[str] = []
    for s, e in remaining:
        for net in ipaddress.summarize_address_range(addr_cls(s), addr_cls(e)):
            out.append(str(net))
    return out


def _exclude_networks(
    nets: list[Any],
    excludes: list[Any],
) -> list[str]:
    v4_nets = [n for n in nets if isinstance(n, ipaddress.IPv4Network)]
    v6_nets = [n for n in nets if isinstance(n, ipaddress.IPv6Network)]
    v4_ex = [n for n in excludes if isinstance(n, ipaddress.IPv4Network)]
    v6_ex = [n for n in excludes if isinstance(n, ipaddress.IPv6Network)]
    return (
        _exclude_networks_by_version(v4_nets, v4_ex, ipaddress.IPv4Address)
        + _exclude_networks_by_version(v6_nets, v6_ex, ipaddress.IPv6Address)
    )


def _is_domain_excluded(d: str, exact_ex: set[str], suffix_ex: set[str]) -> bool:
    if d in exact_ex or d in suffix_ex:
        return True
    if suffix_ex:
        parts = d.split(".")
        for i in range(1, len(parts)):
            if ".".join(parts[i:]) in suffix_ex:
                return True
    return False


def optimize_bucket(
    bucket: RuleBucket,
    kind: str,
    exclude_domains: set[str] | None = None,
    exclude_cidrs: set[str] | None = None,
    exclude_exact_domains: set[str] | None = None,
    list_id: str = "",
) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    """
    Deduplicate, prune redundant subdomains covered by parent domain_suffix,
    and collapse overlapping IPv4/IPv6 CIDRs.
    Returns (domain, domain_suffix, domain_keyword, domain_regex, ipv4_cidrs, ipv6_cidrs).
    """
    ex_suffix = {clean_domain(x) for x in (exclude_domains or set()) if clean_domain(x)}
    ex_exact = {clean_domain(x) for x in (exclude_exact_domains or set()) if clean_domain(x)}
    ex_cidrs = exclude_cidrs or set()

    raw_suffixes: set[str] = set()
    for d in bucket.domain_suffix:
        dc = clean_domain(d)
        if dc and _is_allowed_domain(dc, list_id, is_suffix=True) and not _is_domain_excluded(dc, ex_exact, ex_suffix):
            raw_suffixes.add(dc)

    raw_domains: set[str] = set()
    for d in bucket.domain:
        dc = clean_domain(d)
        if dc and _is_allowed_domain(dc, list_id, is_suffix=False) and not _is_domain_excluded(dc, ex_exact, ex_suffix):
            raw_domains.add(dc)

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

    # Prune exact domains covered ONLY by pruned_suffixes (never prune under exact domain)
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
            if _is_bogon_or_private(net) or _is_too_broad_network(net, list_id):
                continue
            if isinstance(net, ipaddress.IPv4Network):
                v4_nets.append(net)
            else:
                v6_nets.append(net)
        except ValueError:
            continue

    ex_v4: list[ipaddress.IPv4Network] = []
    ex_v6: list[ipaddress.IPv6Network] = []
    for cidr in ex_cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            if isinstance(net, ipaddress.IPv4Network):
                ex_v4.append(net)
            else:
                ex_v6.append(net)
        except ValueError:
            continue

    collapsed_v4 = (
        _exclude_networks_by_version(v4_nets, ex_v4, ipaddress.IPv4Address)
        if ex_v4
        else [str(n) for n in ipaddress.collapse_addresses(v4_nets)]
    )
    collapsed_v6 = (
        _exclude_networks_by_version(v6_nets, ex_v6, ipaddress.IPv6Address)
        if ex_v6
        else [str(n) for n in ipaddress.collapse_addresses(v6_nets)]
    )

    keywords = sorted({k.strip().lower() for k in bucket.domain_keyword if k.strip()})
    regexes = sorted({r.strip() for r in bucket.domain_regex if r.strip()})

    return (
        sorted(pruned_domains),
        sorted(pruned_suffixes),
        keywords,
        regexes,
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
        d_clean = clean_domain(d)
        if d_clean and len(d_clean) <= 253:
            try:
                rev = d_clean.encode("ascii")[::-1].replace(b".", b"\x01")
            except UnicodeEncodeError:
                continue
            if rev:
                rev_map[rev] = rev_map.get(rev, 0) | DOM_FLAG_EXACT
    for s in suffixes:
        s_clean = clean_domain(s)
        if s_clean and len(s_clean) <= 253:
            try:
                rev = s_clean.encode("ascii")[::-1].replace(b".", b"\x01")
            except UnicodeEncodeError:
                continue
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


def decode_tsrs_v2_summary(data: bytes) -> dict[str, Any]:
    """Decode TSRS v2 binary header and domain blocks for verification and --validate."""
    HEADER_FMT = "<4sIIIQ18I"
    HEADER_SIZE = 96
    HEADER_FLAG_DOM_DICT = 0x02

    if len(data) < HEADER_SIZE:
        raise ValueError(f"TSRS v2 file too short: {len(data)} bytes")
    unpacked = struct.unpack_from(HEADER_FMT, data, 0)
    (
        magic,
        version,
        header_flags,
        total_rules,
        _kw_bloom,
        dom_blocks_offset,
        dom_blocks_count,
        dom_data_offset,
        dom_data_len,
        _keywords_offset,
        keywords_count,
        _v4_dir_offset,
        _v4_intervals_offset,
        v4_count,
        _v6_dir_offset,
        _v6_intervals_offset,
        v6_count,
        _dest_ports_offset,
        dest_ports_count,
        _string_table_offset,
        _string_table_len,
        _extra_offset,
        extra_len,
    ) = unpacked

    if magic != b"TSRS":
        raise ValueError(f"Invalid TSRS magic: {magic!r}")
    if version != 2:
        raise ValueError(f"Unsupported TSRS version: {version}")
    if dom_blocks_offset + dom_blocks_count * 8 > len(data):
        raise ValueError("TSRS dom_blocks extend past EOF")
    if dom_data_offset + dom_data_len > len(data):
        raise ValueError("TSRS dom_data extends past EOF")

    domains: list[tuple[str, int]] = []
    if dom_blocks_count > 0 and dom_data_len > 0:
        dom_data = data[dom_data_offset : dom_data_offset + dom_data_len]
        pos = 0
        code_to_tok: dict[int, bytes] = {}
        if header_flags & HEADER_FLAG_DOM_DICT:
            dict_count = dom_data[pos]
            pos += 1
            for _ in range(dict_count):
                if pos + 2 > dom_data_len:
                    raise ValueError("Truncated TSRS domain dictionary header")
                code = dom_data[pos]
                tok_len = dom_data[pos + 1]
                pos += 2
                if pos + tok_len > dom_data_len:
                    raise ValueError("Truncated TSRS domain dictionary token")
                code_to_tok[code] = dom_data[pos : pos + tok_len]
                pos += tok_len

        block_offsets: list[int] = []
        for bidx in range(dom_blocks_count):
            _, block_off = struct.unpack_from("<II", data, dom_blocks_offset + bidx * 8)
            if block_off >= dom_data_len:
                raise ValueError(f"TSRS block offset out of bounds: {block_off}")
            block_offsets.append(block_off)

        for bidx, block_start in enumerate(block_offsets):
            block_end = block_offsets[bidx + 1] if bidx + 1 < len(block_offsets) else dom_data_len
            if block_end < block_start:
                raise ValueError("TSRS block offsets not monotonic")
            cursor = block_start
            prev_key = b""
            while cursor < block_end:
                meta = dom_data[cursor]
                cursor += 1
                flags = (meta >> 6) & 0x03
                shared = meta & 0x3F
                if shared == 63:
                    if cursor >= block_end:
                        raise ValueError("Truncated TSRS shared prefix length")
                    shared += dom_data[cursor]
                    cursor += 1
                if shared > len(prev_key):
                    raise ValueError(f"TSRS shared length {shared} > prev_key {len(prev_key)}")
                suffix = bytearray()
                while True:
                    if cursor >= block_end:
                        raise ValueError("Unterminated TSRS domain suffix")
                    b = dom_data[cursor]
                    cursor += 1
                    is_last = bool(b & 0x80)
                    ch = b & 0x7F
                    tok = code_to_tok.get(ch)
                    if tok is not None:
                        suffix.extend(tok)
                    else:
                        suffix.append(ch)
                    if is_last:
                        break
                key = prev_key[:shared] + bytes(suffix)
                prev_key = key
                dom_bytes = key.replace(b"\x01", b".")[::-1]
                dom_str = dom_bytes.decode("ascii")
                domains.append((dom_str, flags))

    return {
        "version": version,
        "header_flags": header_flags,
        "total_rules": total_rules,
        "domains": domains,
        "keywords_count": keywords_count,
        "v4_count": v4_count,
        "v6_count": v6_count,
        "dest_ports_count": dest_ports_count,
        "extra_len": extra_len,
    }


def decode_tsrs_v2_domains(data: bytes) -> list[tuple[str, int]]:
    """Decode and return `(domain, flags)` pairs from a TSRS v2 rule-set."""
    return list(decode_tsrs_v2_summary(data)["domains"])


def _replace_marker_block(content: str, start_marker: str, end_marker: str, lines: list[str]) -> str:
    if start_marker not in content or end_marker not in content:
        return content
    before = content.split(start_marker, 1)[0]
    after = content.split(end_marker, 1)[1]
    return before + "\n".join(lines) + after


def _format_source_label(src: dict[str, Any]) -> str:
    stype = str(src.get("type", "") or "").strip()
    upstream = src.get("upstream") or src.get("repo")
    entry = src.get("entry")
    entries = src.get("entries")
    if upstream and entry:
        return f"`{upstream}/{entry}`"
    if upstream and isinstance(entries, list) and entries:
        joined = ",".join(str(x) for x in entries)
        return f"`{upstream}/{{{joined}}}`"
    if stype == "ripe_stat_asn":
        if src.get("asn"):
            asn = str(src["asn"]).strip().upper()
            if not asn.startswith("AS"):
                asn = f"AS{asn}"
            return f"`stat.ripe.net/{asn}`"
        if isinstance(src.get("asns"), list) and src["asns"]:
            joined = ",".join(
                str(a).strip().upper() if str(a).strip().upper().startswith("AS") else f"AS{str(a).strip()}"
                for a in src["asns"]
            )
            return f"`stat.ripe.net/{{{joined}}}`"
    url = src.get("url")
    if url:
        raw_url = str(url).strip()
        if raw_url.startswith("{") and "}/" in raw_url:
            label = raw_url[1:].replace("}/", "/", 1)
        else:
            parsed = urlparse(raw_url)
            label = f"{parsed.netloc}{parsed.path}"
            if stype == "ripe_stat_asn" and "resource=" in parsed.query:
                m_res = re.search(r"resource=([^&]+)", parsed.query)
                if m_res:
                    label = f"stat.ripe.net/{m_res.group(1)}"
            elif parsed.query and stype not in ("aws_ip_ranges", "geofeed_csv", "google_ip_ranges"):
                label = f"{label}?{parsed.query}"
        if stype == "aws_ip_ranges":
            extra = [str(src[k]) for k in ("service", "region") if src.get(k)]
            if extra:
                label = f"{label} ({', '.join(extra)})"
        elif stype == "geofeed_csv":
            extra = [str(src[k]) for k in ("country", "region") if src.get(k)]
            if extra:
                label = f"{label} ({', '.join(extra)})"
        elif stype == "google_ip_ranges":
            extra = [str(src[k]) for k in ("service", "scope") if src.get(k)]
            if extra:
                label = f"{label} ({', '.join(extra)})"
        return f"`{label}`"
    return f"`{stype or 'unknown'}`"


def _format_item_sources(item_cfg: dict[str, Any], *, include_excludes: bool = True) -> str:
    lid = str(item_cfg["id"])
    parts = [_format_source_label(src) for src in item_cfg.get("sources", [])]
    if (CUSTOM_DIR / "domains" / f"{lid}.lst").is_file():
        parts.append(f"`custom/domains/{lid}.lst`")
    if (CUSTOM_DIR / "subnets" / f"{lid}.lst").is_file():
        parts.append(f"`custom/subnets/{lid}.lst`")
    if include_excludes and item_cfg.get("exclude_lists"):
        exc_joined = ", ".join(f"`{x}`" for x in item_cfg["exclude_lists"])
        parts.append(f"exclude_lists: {exc_joined}")
    return " + ".join(parts) if parts else "—"


def _format_fallback_label(fallback_srs: Any) -> str:
    if not fallback_srs:
        return "—"
    if isinstance(fallback_srs, dict):
        upstream = fallback_srs.get("upstream", "")
        entry = fallback_srs.get("entry", "")
        if upstream and entry:
            return f"`{upstream}` → `{entry}`"
        return f"`{fallback_srs}`"
    url = str(fallback_srs)
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if parsed.netloc == "raw.githubusercontent.com" and len(path_parts) >= 3:
        repo = f"{path_parts[0]}/{path_parts[1]}"
        return f"`{repo}` → `{path_parts[-1]}`"
    if parsed.netloc == "github.com" and len(path_parts) >= 3:
        repo = f"{path_parts[0]}/{path_parts[1]}"
        return f"`{repo}` → `{path_parts[-1]}`"
    return f"`{parsed.netloc}` → `{path_parts[-1] if path_parts else parsed.path}`"


def update_readme_table(catalog_items: list[dict[str, Any]], cfg: dict[str, Any] | None = None) -> None:
    readme_path = ROOT_DIR / "README.md"
    if not readme_path.is_file():
        return
    if cfg is None and SOURCES_FILE.is_file():
        cfg = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    cfg_by_id: dict[str, dict[str, Any]] = {}
    lists_cfg: list[dict[str, Any]] = []
    if cfg is not None:
        lists_cfg = list(cfg.get("lists", []))
        cfg_by_id = {str(entry["id"]): entry for entry in lists_cfg}

    content = readme_path.read_text(encoding="utf-8")
    updated = content

    action_badges = {
        "proxy": "`Proxy`",
        "direct": "`Direct`",
        "block": "`Block`",
    }

    main_items = [item for item in catalog_items if item.get("group") != "internal"]
    internal_items = [item for item in catalog_items if item.get("group") == "internal"]
    total_count = len(catalog_items)
    main_count = len(main_items)
    internal_count = len(internal_items)

    # Update main catalog table
    cat_lines = [
        "<!-- CATALOG_TABLE_START -->",
        "| ID | Категория и описание | Режим | Домены | Подсети (IPv4/IPv6) | Форматы |",
        "|:---|:---|:---:|---:|---:|:---|",
    ]
    for item in main_items:
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
        cat_lines.append(
            f"| `{lid}` | **{title}**<br><sub>{desc}</sub> | {act} | `{dom_total:,}` | `{cidr_total:,}` | {' · '.join(formats)} |"
        )
    cat_lines.append("<!-- CATALOG_TABLE_END -->")
    updated = _replace_marker_block(
        updated, "<!-- CATALOG_TABLE_START -->", "<!-- CATALOG_TABLE_END -->", cat_lines
    )

    # Update optional internal catalog table
    if "<!-- INTERNAL_CATALOG_START -->" in updated and "<!-- INTERNAL_CATALOG_END -->" in updated:
        int_lines = [
            "<!-- INTERNAL_CATALOG_START -->",
            "| ID | Категория и назначение | Режим | Источники (`sources.json`) | Форматы |",
            "|:---|:---|:---:|:---|:---|",
        ]
        for item in internal_items:
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
            src_cell = _format_item_sources(cfg_by_id[lid]) if lid in cfg_by_id else "—"
            int_lines.append(
                f"| `{lid}` | **{title}** — {desc} | {act} | {src_cell} | {' · '.join(formats)} |"
            )
        int_lines.append("<!-- INTERNAL_CATALOG_END -->")
        updated = _replace_marker_block(
            updated, "<!-- INTERNAL_CATALOG_START -->", "<!-- INTERNAL_CATALOG_END -->", int_lines
        )

    # Update optional sources matrix table
    if "<!-- SOURCES_MATRIX_START -->" in updated and "<!-- SOURCES_MATRIX_END -->" in updated and lists_cfg:
        mat_lines = [
            "<!-- SOURCES_MATRIX_START -->",
            "| ID | Группа | `kind` | Входные источники (`sources` в `sources.json` + `custom/`) | Резервный `fallback_srs` |",
            "|:---|:---:|:---:|:---|:---|",
        ]
        for item_cfg in lists_cfg:
            lid = str(item_cfg["id"])
            grp = str(item_cfg.get("group", ""))
            kind = str(item_cfg.get("kind", ""))
            src_cell = _format_item_sources(item_cfg)
            fb_cell = _format_fallback_label(item_cfg.get("fallback_srs"))
            mat_lines.append(f"| `{lid}` | `{grp}` | `{kind}` | {src_cell} | {fb_cell} |")
        mat_lines.append("<!-- SOURCES_MATRIX_END -->")
        updated = _replace_marker_block(
            updated, "<!-- SOURCES_MATRIX_START -->", "<!-- SOURCES_MATRIX_END -->", mat_lines
        )

    # Update category counters in badges and headings
    updated = re.sub(r"lists-\d+(%20|_)categories", f"lists-{total_count}\\1categories", updated)
    updated = re.sub(r'alt="\d+\s+Categories"', f'alt="{total_count} Categories"', updated)
    updated = re.sub(
        r"Пофайловая матрица источников для всех\s+\d+\s+категорий",
        f"Пофайловая матрица источников для всех {total_count} категорий",
        updated,
    )
    updated = re.sub(
        r"###\s+Полный каталог\s+\(\d+\s+категорий\)",
        f"### Полный каталог ({total_count} категорий)",
        updated,
    )
    updated = re.sub(
        r"Полная таблица\s+\d+\s+основн(ого|ых)\s+списк(а|ов)",
        f"Полная таблица {main_count} основных списков",
        updated,
    )
    updated = re.sub(
        r"Системные и гео-базы ядра\s+\(\d+\s+категори[йяи]",
        f"Системные и гео-базы ядра ({internal_count} категорий",
        updated,
    )

    if updated != content:
        readme_path.write_text(updated, encoding="utf-8", newline="\n")


def _parse_exclude_file(path: Path) -> tuple[set[str], set[str], set[str]]:
    """
    Parse an exclude file supporting:
      - IPv4/IPv6 addresses and CIDRs -> cidr_excludes
      - `full:<domain>` / `exact:<domain>` / `domain:<domain>` -> exact_excludes
      - `domain_suffix:<domain>` / `suffix:<domain>` / plain `<domain>` -> suffix_excludes
    """
    suffix_excludes: set[str] = set()
    exact_excludes: set[str] = set()
    cidr_excludes: set[str] = set()
    if not path.is_file():
        return suffix_excludes, exact_excludes, cidr_excludes

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        entry = raw_line.split("#", 1)[0].split(";", 1)[0].strip()
        if not entry:
            continue
        lower = entry.lower()
        if lower.startswith(("full:", "exact:", "domain:")):
            raw_dom = entry.split(":", 1)[1]
            dom = clean_domain(raw_dom)
            if dom:
                exact_excludes.add(dom)
            continue
        if lower.startswith(("domain_suffix:", "suffix:")):
            raw_dom = entry.split(":", 1)[1]
            dom = clean_domain(raw_dom)
            if dom:
                suffix_excludes.add(dom)
            continue
        try:
            net = ipaddress.ip_network(entry, strict=False)
            cidr_excludes.add(str(net))
            continue
        except ValueError:
            pass
        dom = clean_domain(entry)
        if dom:
            suffix_excludes.add(dom)
    return suffix_excludes, exact_excludes, cidr_excludes


def load_builtin_ads_excludes(*, refresh: bool = False) -> tuple[set[str], set[str]]:
    """
    Load built-in @ads and telemetry domains subtracted from non-block categories.
    Returns `(exact_domains, suffix_domains)`.
    """
    exact: set[str] = set()
    suffixes: set[str] = set()
    for d in BUILTIN_TELEMETRY_EXACT_DOMAINS:
        cd = clean_domain(d)
        if cd:
            exact.add(cd)
    for s in BUILTIN_TELEMETRY_SUFFIX_DOMAINS:
        cs = clean_domain(s)
        if cs:
            suffixes.add(cs)

    try:
        raw = fetch_url_bytes(ADS_ALL_JSON_URL, refresh=refresh, expected_format="json")
        ads_bucket = parse_singbox_json_bytes(raw)
        for d in ads_bucket.domain:
            cd = clean_domain(d)
            if cd:
                exact.add(cd)
        for s in ads_bucket.domain_suffix:
            cs = clean_domain(s)
            if cs:
                suffixes.add(cs)
    except Exception as exc:
        log(f"WARN: Failed loading built-in @ads exclude list ({ADS_ALL_JSON_URL}): {exc}; using static telemetry set")

    return exact, suffixes


def _counts_total(counts: dict[str, Any] | None) -> int:
    if not isinstance(counts, dict):
        return 0
    return sum(
        int(counts.get(k, 0) or 0)
        for k in ("domain", "domain_suffix", "domain_keyword", "domain_regex", "ipv4_cidr", "ipv6_cidr")
    )


def _source_urls_with_formats(src: dict[str, Any], upstreams: dict[str, str]) -> list[tuple[str, str | None]]:
    stype = str(src.get("type", "") or "").strip()
    fmt_map = {
        "srs": "srs",
        "singbox_json": "json",
        "aws_ip_ranges": "json",
        "google_ip_ranges": "json",
        "ripe_stat_asn": "json",
    }
    fmt = fmt_map.get(stype)
    out: list[tuple[str, str | None]] = []
    if "url" in src and src["url"]:
        out.append((format_url(str(src["url"]), upstreams), fmt))
    elif stype == "ripe_stat_asn":
        asns: list[Any] = []
        if src.get("asn"):
            asns.append(src["asn"])
        if isinstance(src.get("asns"), list):
            asns.extend(src["asns"])
        for a in asns:
            asn_str = str(a).strip().upper()
            if not asn_str:
                continue
            if not asn_str.startswith("AS"):
                asn_str = f"AS{asn_str}"
            out.append((f"https://stat.ripe.net/data/announced-prefixes/data.json?resource={asn_str}", "json"))
    return out


def _existing_artifacts_valid(lid: str, prev_entry: dict[str, Any] | None) -> bool:
    if not prev_entry:
        return False
    jpath = RELEASE_DIR / "json" / f"{lid}.json"
    spath = RELEASE_DIR / "sing-box" / f"{lid}.srs"
    tpath = RELEASE_DIR / "tachyon" / f"{lid}.trs"
    if not (jpath.is_file() and spath.is_file() and tpath.is_file()):
        return False
    try:
        srs_bytes = spath.read_bytes()
        if len(srs_bytes) < 4 or srs_bytes[:3] != b"SRS":
            return False
        trs_bytes = tpath.read_bytes()
        decode_tsrs_v2_summary(trs_bytes)
        if prev_entry.get("sha256_trs") and hashlib.sha256(trs_bytes).hexdigest() != prev_entry["sha256_trs"]:
            return False
        if prev_entry.get("sha256_srs") and hashlib.sha256(srs_bytes).hexdigest() != prev_entry["sha256_srs"]:
            return False
        jdoc = json.loads(jpath.read_text(encoding="utf-8"))
        rules = jdoc.get("rules", []) if isinstance(jdoc, dict) else []
        if not rules or not isinstance(rules[0], dict) or not rules[0]:
            return False
        if f"empty-{lid}.invalid" in rules[0].get("domain", []):
            return False
    except Exception:
        return False
    return True


def _write_if_changed_text(path: Path, text: str) -> None:
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except Exception:
            pass
    path.write_text(text, encoding="utf-8", newline="\n")


def _write_if_changed_bytes(path: Path, data: bytes) -> None:
    if path.is_file():
        try:
            if path.read_bytes() == data:
                return
        except Exception:
            pass
    path.write_bytes(data)


def write_sha256sums(release_dir: Path = RELEASE_DIR) -> Path:
    """Write deterministic POSIX-sorted SHA-256 checksums for all release artifacts."""
    sums_path = release_dir / "sha256sums.txt"
    entries: list[tuple[str, str]] = []
    for path in release_dir.rglob("*"):
        if not path.is_file() or path == sums_path or path.name.endswith(".tmp"):
            continue
        rel = path.relative_to(release_dir).as_posix()
        entries.append((rel, sha256_file(path)))
    entries.sort(key=lambda item: item[0])
    body = "".join(f"{digest}  {rel}\n" for rel, digest in entries)
    _write_if_changed_text(sums_path, body)
    return sums_path


def validate_release() -> int:
    """Validate all generated artifacts in release/ against sources.json and invariants."""
    errors: list[str] = []

    if not SOURCES_FILE.is_file():
        log(f"ERROR: Missing sources file: {SOURCES_FILE}")
        return 1
    cfg = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    cfg_lists: list[dict[str, Any]] = cfg.get("lists", [])
    cfg_by_id = {str(item["id"]): item for item in cfg_lists}

    catalog_file = RELEASE_DIR / "catalog.json"
    if not catalog_file.is_file():
        log(f"ERROR: Missing catalog file: {catalog_file}")
        return 1
    try:
        catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"ERROR: Invalid catalog.json: {exc}")
        return 1

    if not isinstance(catalog, dict) or not str(catalog.get("generated_at", "")).strip():
        errors.append("catalog.json missing non-empty generated_at")

    cat_lists = catalog.get("lists", [])
    if not isinstance(cat_lists, list):
        errors.append("catalog.json 'lists' must be an array")
        cat_lists = []

    if int(catalog.get("total_lists", -1)) != len(cat_lists):
        errors.append(f"catalog.json total_lists={catalog.get('total_lists')} != len(lists)={len(cat_lists)}")

    cat_ids = [str(e.get("id", "")) for e in cat_lists if isinstance(e, dict)]
    if set(cat_ids) != set(cfg_by_id.keys()) or len(cat_ids) != len(cfg_by_id):
        missing = sorted(set(cfg_by_id) - set(cat_ids))
        extra = sorted(set(cat_ids) - set(cfg_by_id))
        if missing:
            errors.append(f"catalog.json missing IDs from sources.json: {missing}")
        if extra:
            errors.append(f"catalog.json contains unknown IDs not in sources.json: {extra}")

    sums_path = RELEASE_DIR / "sha256sums.txt"
    sum_map: dict[str, str] = {}
    if not sums_path.is_file():
        errors.append("Missing release/sha256sums.txt")
    else:
        raw_lines = [line for line in sums_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        seen_paths: list[str] = []
        for line in raw_lines:
            if "  " not in line:
                errors.append(f"Malformed line in sha256sums.txt: {line}")
                continue
            digest, rel = line.split("  ", 1)
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                errors.append(f"Invalid SHA-256 hex in sha256sums.txt for {rel}")
                continue
            target = RELEASE_DIR / rel
            if not target.is_file():
                errors.append(f"Missing file listed in sha256sums.txt: release/{rel}")
                continue
            actual = sha256_file(target)
            if actual != digest:
                errors.append(f"SHA-256 mismatch in sha256sums.txt for release/{rel}")
            sum_map[rel] = digest
            seen_paths.append(rel)
        if seen_paths != sorted(seen_paths):
            errors.append("release/sha256sums.txt is not sorted in POSIX order")
        for actual_file in RELEASE_DIR.rglob("*"):
            if actual_file.is_file() and actual_file != sums_path and not actual_file.name.endswith(".tmp"):
                rel = actual_file.relative_to(RELEASE_DIR).as_posix()
                if rel not in sum_map:
                    errors.append(f"Artifact missing from sha256sums.txt: release/{rel}")

    for entry in cat_lists:
        if not isinstance(entry, dict):
            continue
        lid = str(entry.get("id", ""))
        kind = str(entry.get("kind", ""))
        counts = entry.get("counts", {})
        if _counts_total(counts) <= 0:
            errors.append(f"[{lid}] catalog counts total is 0")

        jpath = RELEASE_DIR / "json" / f"{lid}.json"
        spath = RELEASE_DIR / "sing-box" / f"{lid}.srs"
        tpath = RELEASE_DIR / "tachyon" / f"{lid}.trs"
        for req_path in (jpath, spath, tpath):
            if not req_path.is_file():
                errors.append(f"[{lid}] missing artifact {req_path.relative_to(ROOT_DIR).as_posix()}")

        if jpath.is_file():
            try:
                jdoc = json.loads(jpath.read_text(encoding="utf-8"))
                rules = jdoc.get("rules", []) if isinstance(jdoc, dict) else []
                if not rules or not isinstance(rules[0], dict) or not rules[0]:
                    errors.append(f"[{lid}] {jpath.name} has empty rules")
                else:
                    r0 = rules[0]
                    for dom in r0.get("domain", []):
                        if dom == f"empty-{lid}.invalid" or dom.endswith(".invalid"):
                            errors.append(f"[{lid}] placeholder domain in json: {dom}")
                        cd = clean_domain(dom)
                        if cd != dom or not _is_allowed_domain(cd, lid, is_suffix=False):
                            errors.append(f"[{lid}] invalid domain in json: {dom!r}")
                    for suf in r0.get("domain_suffix", []):
                        if suf == f"empty-{lid}.invalid" or suf.endswith(".invalid"):
                            errors.append(f"[{lid}] placeholder suffix in json: {suf}")
                        cs = clean_domain(suf)
                        if cs != suf or not _is_allowed_domain(cs, lid, is_suffix=True):
                            errors.append(f"[{lid}] invalid domain_suffix in json: {suf!r}")
                    if kind == "domains" and r0.get("ip_cidr"):
                        errors.append(f"[{lid}] kind=domains contains ip_cidr in json")
                    for cidr in r0.get("ip_cidr", []):
                        try:
                            net = ipaddress.ip_network(cidr, strict=True)
                            if _is_bogon_or_private(net) or _is_too_broad_network(net, lid):
                                errors.append(f"[{lid}] disallowed ip_cidr in json: {cidr}")
                        except ValueError:
                            errors.append(f"[{lid}] invalid ip_cidr in json: {cidr!r}")
            except Exception as exc:
                errors.append(f"[{lid}] failed reading json: {exc}")

        if spath.is_file():
            sbytes = spath.read_bytes()
            if len(sbytes) < 4 or sbytes[:3] != b"SRS" or not (1 <= sbytes[3] <= 10):
                errors.append(f"[{lid}] invalid .srs magic bytes")
            elif entry.get("sha256_srs") and hashlib.sha256(sbytes).hexdigest() != entry["sha256_srs"]:
                errors.append(f"[{lid}] sha256_srs mismatch vs catalog.json")

        if tpath.is_file():
            tbytes = tpath.read_bytes()
            if entry.get("sha256_trs") and hashlib.sha256(tbytes).hexdigest() != entry["sha256_trs"]:
                errors.append(f"[{lid}] sha256_trs mismatch vs catalog.json")
            try:
                summary = decode_tsrs_v2_summary(tbytes)
                if kind == "domains" and (summary["v4_count"] > 0 or summary["v6_count"] > 0):
                    errors.append(f"[{lid}] kind=domains contains IP intervals in .trs")
                for dom_str, flags in summary["domains"]:
                    if dom_str == f"empty-{lid}.invalid" or dom_str.endswith(".invalid"):
                        errors.append(f"[{lid}] placeholder domain in .trs: {dom_str}")
                    if clean_domain(dom_str) != dom_str or not _is_allowed_domain(
                        dom_str, lid, is_suffix=bool(flags & 0x02)
                    ):
                        errors.append(f"[{lid}] invalid decoded domain in .trs: {dom_str!r} (flags={flags})")
            except Exception as exc:
                errors.append(f"[{lid}] invalid .trs binary: {exc}")

        dom_count = int(counts.get("domain", 0) or 0) + int(counts.get("domain_suffix", 0) or 0)
        v4_count = int(counts.get("ipv4_cidr", 0) or 0)
        v6_count = int(counts.get("ipv6_cidr", 0) or 0)
        dom_lst = RELEASE_DIR / "domains" / f"{lid}.lst"
        sub_lst = RELEASE_DIR / "subnets" / f"{lid}.lst"
        v4_lst = RELEASE_DIR / "subnets" / "ipv4" / f"{lid}.lst"
        v6_lst = RELEASE_DIR / "subnets" / "ipv6" / f"{lid}.lst"

        if dom_count > 0 and not dom_lst.is_file():
            errors.append(f"[{lid}] missing release/domains/{lid}.lst")
        elif dom_count == 0 and dom_lst.is_file():
            errors.append(f"[{lid}] stale release/domains/{lid}.lst exists with 0 domain count")

        if (v4_count + v6_count) > 0 and not sub_lst.is_file():
            errors.append(f"[{lid}] missing release/subnets/{lid}.lst")
        elif (v4_count + v6_count) == 0 and sub_lst.is_file():
            errors.append(f"[{lid}] stale release/subnets/{lid}.lst exists with 0 subnet count")

        if v4_count > 0 and not v4_lst.is_file():
            errors.append(f"[{lid}] missing release/subnets/ipv4/{lid}.lst")
        elif v4_count == 0 and v4_lst.is_file():
            errors.append(f"[{lid}] stale release/subnets/ipv4/{lid}.lst exists")

        if v6_count > 0 and not v6_lst.is_file():
            errors.append(f"[{lid}] missing release/subnets/ipv6/{lid}.lst")
        elif v6_count == 0 and v6_lst.is_file():
            errors.append(f"[{lid}] stale release/subnets/ipv6/{lid}.lst exists")

    if errors:
        for err in errors[:50]:
            log(f"VALIDATE ERROR: {err}")
        if len(errors) > 50:
            log(f"VALIDATE ERROR: ... and {len(errors) - 50} more errors")
        return 1

    log(f"Validation passed for {len(cat_lists)} categories in {RELEASE_DIR}")
    return 0


def build_all(only_ids: set[str] | None = None, *, refresh: bool = False, allow_shrink: bool = False) -> int:
    t0 = time.time()
    reset_fetch_cache()

    cfg = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    upstreams: dict[str, str] = cfg["upstreams"]
    all_lists_cfg: list[dict[str, Any]] = cfg["lists"]
    cfg_by_id: dict[str, dict[str, Any]] = {str(item["id"]): item for item in all_lists_cfg}

    target_lists_cfg = (
        [item for item in all_lists_cfg if item["id"] in only_ids]
        if only_ids
        else list(all_lists_cfg)
    )
    if only_ids and not target_lists_cfg:
        log(f"ERROR: No matching list IDs found in {SOURCES_FILE} for {sorted(only_ids)}")
        return 1

    tachyon_bin = ensure_tachyon_core_binary()
    log(f"Using tachyon-core compiler: {tachyon_bin}")

    # Resolve transitive exclude_lists dependencies so --only still resolves exclusions accurately
    needed_ids: set[str] = set()
    queue = [str(item["id"]) for item in target_lists_cfg]
    while queue:
        cur = queue.pop()
        if cur in needed_ids:
            continue
        needed_ids.add(cur)
        item_cfg = cfg_by_id.get(cur)
        if item_cfg:
            for dep in item_cfg.get("exclude_lists", []):
                dep_id = str(dep).strip()
                if dep_id and dep_id not in cfg_by_id:
                    raise RuntimeError(f"[{cur}] unknown exclude_lists target: {dep_id}")
                if dep_id and dep_id not in needed_ids:
                    queue.append(dep_id)

    # Read existing catalog.json upfront for shrink-guard and conditional generated_at
    catalog_file = RELEASE_DIR / "catalog.json"
    prev_doc: dict[str, Any] = {}
    prev_by_id: dict[str, dict[str, Any]] = {}
    if catalog_file.is_file():
        try:
            loaded = json.loads(catalog_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_doc = loaded
                for e in loaded.get("lists", []):
                    if isinstance(e, dict) and "id" in e:
                        prev_by_id[str(e["id"])] = e
        except Exception as exc:
            log(f"WARN: Failed reading previous catalog.json: {exc}")

    # Pre-fetch direct HTTP URLs in parallel (including exclude_lists dependencies & built-in ads list)
    prefetch_formats: dict[str, str | None] = {}
    need_ads_list = False
    for lid in needed_ids:
        item_cfg = cfg_by_id.get(lid)
        if not item_cfg:
            continue
        if item_cfg.get("group", "other") != "block" and lid != "twitch_ads":
            need_ads_list = True
        for src in item_cfg.get("sources", []):
            for url, fmt in _source_urls_with_formats(src, upstreams):
                if url not in prefetch_formats or prefetch_formats[url] is None:
                    prefetch_formats[url] = fmt
    if need_ads_list:
        prefetch_formats[ADS_ALL_JSON_URL] = "json"

    log(f"Prefetching {len(prefetch_formats)} upstream resources in parallel (refresh={refresh})...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        future_map = {
            pool.submit(fetch_url_bytes, url, refresh=refresh, expected_format=fmt): url
            for url, fmt in prefetch_formats.items()
        }
        for fut in concurrent.futures.as_completed(future_map):
            url = future_map[fut]
            try:
                fut.result()
            except Exception as exc:
                log(f"WARN: Failed prefetching {url}: {exc}")

    global_exc_file = CUSTOM_DIR / "exclude" / "domains.lst"
    global_exc_sufs, global_exc_exact, global_exc_cidrs = _parse_exclude_file(global_exc_file)
    ads_exact, ads_suffixes = load_builtin_ads_excludes(refresh=refresh) if need_ads_list else (set(), set())

    resolved_memo: dict[
        str, tuple[list[str], list[str], list[str], list[str], list[str], list[str], int, int]
    ] = {}

    def _try_fallback_srs(item_cfg: dict[str, Any]) -> RuleBucket | None:
        fb = item_cfg.get("fallback_srs")
        if not fb:
            return None
        if isinstance(fb, dict) and fb.get("upstream") and fb.get("entry"):
            fb_url = f"{upstreams[fb['upstream']].rstrip('/')}/{fb['entry']}"
        elif isinstance(fb, str) and fb.strip():
            fb_url = format_url(fb.strip(), upstreams)
        else:
            return None
        try:
            log(f"WARN: [{item_cfg['id']}] falling back to fallback_srs: {fb_url}")
            return parse_srs_url(fb_url, tachyon_bin, refresh=refresh)
        except Exception as exc:
            log(f"WARN: [{item_cfg['id']}] fallback_srs failed ({fb_url}): {exc}")
            return None

    def resolve_list_items(
        lid: str, stack: tuple[str, ...]
    ) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str], int, int]:
        if lid in stack:
            raise RuntimeError(f"Cyclic exclude_lists dependency: {' -> '.join(stack + (lid,))}")
        if lid in resolved_memo:
            return resolved_memo[lid]

        item_cfg = cfg_by_id[lid]
        kind = str(item_cfg["kind"])
        group = str(item_cfg.get("group", "other"))
        bucket = RuleBucket()
        failed_sources = 0
        total_sources = 0

        for src in item_cfg.get("sources", []):
            stype = str(src["type"])
            total_sources += 1
            try:
                if stype == "dlc":
                    base_key = src.get("repo") or src["upstream"]
                    base_url = upstreams[base_key]
                    exc_attrs = {"ads", "telemetry"} if (group != "block" and lid != "twitch_ads") else None
                    entries = src.get("entries") if isinstance(src.get("entries"), list) else [src["entry"]]
                    for ent in entries:
                        bucket.merge(
                            parse_dlc_entry(
                                base_url,
                                str(ent),
                                src.get("attr"),
                                exclude_attrs=exc_attrs,
                                refresh=refresh,
                            )
                        )
                elif stype == "srs":
                    url = format_url(str(src["url"]), upstreams)
                    bucket.merge(parse_srs_url(url, tachyon_bin, refresh=refresh))
                elif stype == "singbox_json":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh, expected_format="json")
                    bucket.merge(parse_singbox_json_bytes(raw))
                elif stype == "plain_domains":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh)
                    bucket.merge(parse_plain_domains(raw.decode("utf-8", errors="replace")))
                elif stype == "plain_subnets":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh)
                    bucket.merge(parse_plain_subnets(raw.decode("utf-8", errors="replace")))
                elif stype == "clash_yaml":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh)
                    bucket.merge(parse_clash_yaml(raw.decode("utf-8", errors="replace")))
                elif stype == "aws_ip_ranges":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh, expected_format="json")
                    bucket.merge(
                        parse_aws_ip_ranges(raw, service=src.get("service"), region=src.get("region"))
                    )
                elif stype == "geofeed_csv":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh)
                    bucket.merge(
                        parse_geofeed_csv(
                            raw.decode("utf-8", errors="replace"),
                            country=src.get("country"),
                            region=src.get("region"),
                        )
                    )
                elif stype == "ripe_stat_asn":
                    for url, fmt in _source_urls_with_formats(src, upstreams):
                        raw = fetch_url_bytes(url, refresh=refresh, expected_format=fmt)
                        bucket.merge(parse_ripe_stat_asn(raw))
                elif stype == "google_ip_ranges":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh, expected_format="json")
                    bucket.merge(
                        parse_google_ip_ranges(raw, service=src.get("service"), scope=src.get("scope"))
                    )
                elif stype == "cloudflare_ips":
                    url = format_url(str(src["url"]), upstreams)
                    raw = fetch_url_bytes(url, refresh=refresh)
                    bucket.merge(
                        parse_cloudflare_ips(raw, include_jdcloud=bool(src.get("include_jdcloud", False)))
                    )
                else:
                    raise ValueError(f"Unsupported source type {stype!r} in [{lid}]")
            except ValueError as val_exc:
                if "Unsupported source type" in str(val_exc):
                    raise
                failed_sources += 1
                log(f"WARN: [{lid}] source {src} failed: {val_exc}")
            except Exception as exc:
                failed_sources += 1
                log(f"WARN: [{lid}] source {src} failed: {exc}")

        if total_sources > 0 and failed_sources == total_sources:
            fb_bucket = _try_fallback_srs(item_cfg)
            if fb_bucket is not None:
                bucket.merge(fb_bucket)

        custom_dom_file = CUSTOM_DIR / "domains" / f"{lid}.lst"
        if custom_dom_file.is_file():
            bucket.merge(parse_plain_domains(custom_dom_file.read_text(encoding="utf-8")))

        custom_sub_file = CUSTOM_DIR / "subnets" / f"{lid}.lst"
        if custom_sub_file.is_file():
            bucket.merge(parse_plain_subnets(custom_sub_file.read_text(encoding="utf-8")))

        exc_suf, exc_exact, exc_cidr = _parse_exclude_file(CUSTOM_DIR / "exclude" / f"{lid}.lst")

        if group != "block" and lid != "twitch_ads":
            g_suf = global_exc_sufs - PROTECTED_PUSH_DOMAINS if lid == "push" else global_exc_sufs
            g_exact = global_exc_exact - PROTECTED_PUSH_DOMAINS if lid == "push" else global_exc_exact
            a_suf = ads_suffixes - PROTECTED_PUSH_DOMAINS if lid == "push" else ads_suffixes
            a_exact = ads_exact - PROTECTED_PUSH_DOMAINS if lid == "push" else ads_exact
            exc_suf |= g_suf | a_suf
            exc_exact |= g_exact | a_exact
            exc_cidr |= global_exc_cidrs

        for dep in item_cfg.get("exclude_lists", []):
            dep_id = str(dep).strip()
            if not dep_id:
                continue
            d_exact, d_sufs, _dkw, _drx, d_v4, d_v6, _df, _dt = resolve_list_items(dep_id, stack + (lid,))
            exc_exact.update(d_exact)
            exc_suf.update(d_sufs)
            exc_cidr.update(d_v4)
            exc_cidr.update(d_v6)

        domains, suffixes, keywords, regexes, v4_cidrs, v6_cidrs = optimize_bucket(
            bucket,
            kind,
            exclude_domains=exc_suf,
            exclude_cidrs=exc_cidr,
            exclude_exact_domains=exc_exact,
            list_id=lid,
        )
        res = (domains, suffixes, keywords, regexes, v4_cidrs, v6_cidrs, failed_sources, total_sources)
        resolved_memo[lid] = res
        return res

    # Phase 1: resolve and verify shrink-guard / empty-guard before mutating release/
    effective_allow_shrink = allow_shrink or (
        os.environ.get("TACHYON_ALLOW_SHRINK", "").strip().lower() in ("1", "true", "yes")
    )
    staged_items: list[dict[str, Any]] = []
    abort_errors: list[str] = []

    for item in target_lists_cfg:
        lid = str(item["id"])
        kind = str(item["kind"])
        domains, suffixes, keywords, regexes, v4_cidrs, v6_cidrs, failed_sources, total_sources = (
            resolve_list_items(lid, ())
        )

        # IMPORTANT: For kind == "domains", never include ip_cidr in .trs/.srs/.json
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

        counts = {
            "domain": len(domains),
            "domain_suffix": len(suffixes),
            "domain_keyword": len(keywords),
            "domain_regex": len(regexes),
            "ipv4_cidr": len(v4_cidrs),
            "ipv6_cidr": len(v6_cidrs),
        }
        new_total = _counts_total(counts)
        prev_entry = prev_by_id.get(lid)
        prev_total = _counts_total(prev_entry.get("counts")) if prev_entry else 0

        if not rule_obj or new_total == 0:
            if failed_sources > 0 and _existing_artifacts_valid(lid, prev_entry):
                log(
                    f"WARN: [{lid}] resolved 0 rules ({failed_sources}/{total_sources} sources failed); "
                    "keeping previous valid artifacts"
                )
                staged_items.append({"id": lid, "keep_prev": prev_entry})
                continue
            abort_errors.append(
                f"[{lid}] resolved 0 rules ({failed_sources}/{total_sources} failed sources); refusing empty rule-set"
            )
            continue

        if prev_total > 20 and new_total < prev_total * 0.5:
            if effective_allow_shrink:
                log(
                    f"WARN: [{lid}] count reduced >50% ({prev_total} -> {new_total}); "
                    "accepted via allow-shrink"
                )
            elif failed_sources > 0 and _existing_artifacts_valid(lid, prev_entry):
                log(
                    f"WARN: [{lid}] count dropped >50% ({prev_total} -> {new_total}) with "
                    f"{failed_sources}/{total_sources} failed sources; keeping previous valid artifacts"
                )
                staged_items.append({"id": lid, "keep_prev": prev_entry})
                continue
            else:
                abort_errors.append(
                    f"[{lid}] shrink-guard triggered ({prev_total} -> {new_total}, >50% drop); "
                    "pass --allow-shrink or set TACHYON_ALLOW_SHRINK=1"
                )
                continue

        staged_items.append(
            {
                "id": lid,
                "keep_prev": None,
                "item": item,
                "kind": kind,
                "rule_obj": rule_obj,
                "counts": counts,
                "domains": domains,
                "suffixes": suffixes,
                "v4_cidrs": v4_cidrs,
                "v6_cidrs": v6_cidrs,
            }
        )

    if abort_errors:
        for err in abort_errors:
            log(f"ERROR: {err}")
        log(f"Build aborted with {len(abort_errors)} validation/shrink-guard error(s); release/ left untouched.")
        return 1

    # Phase 2: Prepare output directories and write validated artifacts
    for sub in ("tachyon", "sing-box", "json", "domains", "subnets", "subnets/ipv4", "subnets/ipv6"):
        (RELEASE_DIR / sub).mkdir(parents=True, exist_ok=True)

    catalog_entries: list[dict[str, Any]] = []
    compile_jobs: list[tuple[str, Path, Path]] = []

    for rec in staged_items:
        lid = str(rec["id"])
        if rec["keep_prev"] is not None:
            catalog_entries.append(dict(rec["keep_prev"]))
            continue

        item = rec["item"]
        kind = rec["kind"]
        rule_obj = rec["rule_obj"]
        counts = rec["counts"]
        domains = rec["domains"]
        suffixes = rec["suffixes"]
        v4_cidrs = rec["v4_cidrs"]
        v6_cidrs = rec["v6_cidrs"]

        srs_json_doc = {
            "version": 3,
            "rules": [rule_obj],
        }

        json_path = RELEASE_DIR / "json" / f"{lid}.json"
        srs_path = RELEASE_DIR / "sing-box" / f"{lid}.srs"
        trs_path = RELEASE_DIR / "tachyon" / f"{lid}.trs"
        _write_if_changed_text(json_path, json.dumps(srs_json_doc, indent=2, ensure_ascii=False) + "\n")
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
        _write_if_changed_bytes(trs_path, trs_bytes)
        trs_sha256 = hashlib.sha256(trs_bytes).hexdigest()

        # Write flat domain list for dnsmasq / client integrations, or unlink stale file if empty
        all_flat_domains = sorted(set(domains) | set(suffixes))
        dom_lst_path = RELEASE_DIR / "domains" / f"{lid}.lst"
        if all_flat_domains:
            _write_if_changed_text(dom_lst_path, "\n".join(all_flat_domains) + "\n")
        else:
            dom_lst_path.unlink(missing_ok=True)

        # Write subnet CIDR lists for nftables / ipset, or unlink stale files if empty
        v4_lst_path = RELEASE_DIR / "subnets" / "ipv4" / f"{lid}.lst"
        v6_lst_path = RELEASE_DIR / "subnets" / "ipv6" / f"{lid}.lst"
        sub_lst_path = RELEASE_DIR / "subnets" / f"{lid}.lst"
        if v4_cidrs:
            _write_if_changed_text(v4_lst_path, "\n".join(v4_cidrs) + "\n")
        else:
            v4_lst_path.unlink(missing_ok=True)
        if v6_cidrs:
            _write_if_changed_text(v6_lst_path, "\n".join(v6_cidrs) + "\n")
        else:
            v6_lst_path.unlink(missing_ok=True)
        if v4_cidrs or v6_cidrs:
            _write_if_changed_text(sub_lst_path, "\n".join(v4_cidrs + v6_cidrs) + "\n")
        else:
            sub_lst_path.unlink(missing_ok=True)

        catalog_entries.append(
            {
                "id": lid,
                "group": item.get("group", "other"),
                "kind": kind,
                "recommended_action": item.get("recommended_action", "proxy"),
                "title": item["title"],
                "description": item["description"],
                "fallback_srs": item.get("fallback_srs", ""),
                "counts": counts,
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
        tmp_spath = spath.with_name(f".{lid}.{os.getpid()}_{uuid.uuid4().hex[:8]}.srs.tmp")
        try:
            res = subprocess.run(
                [str(tachyon_bin), "rule-set", "compile", str(jpath), "-o", str(tmp_spath)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if res.returncode != 0 or not tmp_spath.is_file():
                raise RuntimeError(f"Failed compiling {lid}.srs: {res.stderr.strip()}")
            compiled_bytes = tmp_spath.read_bytes()
            if len(compiled_bytes) < 4 or compiled_bytes[:3] != b"SRS":
                raise RuntimeError(f"Compiled {lid}.srs has invalid magic bytes")
            tmp_spath.replace(spath)
        finally:
            tmp_spath.unlink(missing_ok=True)
        return lid, sha256_file(spath)

    srs_hashes: dict[str, str] = {}
    if compile_jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for lid, h in pool.map(compile_one, compile_jobs):
                srs_hashes[lid] = h

    for entry in catalog_entries:
        lid = str(entry["id"])
        if lid in srs_hashes:
            entry["sha256_srs"] = srs_hashes[lid]

    # Prune orphaned artifacts removed from sources.json during a full build
    if only_ids is None:
        valid_ids = {str(item["id"]) for item in all_lists_cfg}
        for sub in ("tachyon", "sing-box", "json", "domains", "subnets", "subnets/ipv4", "subnets/ipv6"):
            subdir = RELEASE_DIR / sub
            if not subdir.is_dir():
                continue
            for fpath in subdir.iterdir():
                if fpath.is_file() and fpath.stem not in valid_ids:
                    fpath.unlink()

    full_entries = catalog_entries
    if only_ids and prev_doc:
        by_id = {str(e["id"]): e for e in prev_doc.get("lists", []) if isinstance(e, dict) and "id" in e}
        for e in catalog_entries:
            by_id[str(e["id"])] = e
        full_entries = [by_id[item["id"]] for item in all_lists_cfg if item["id"] in by_id]

    prev_generated_at = str(prev_doc.get("generated_at", "")).strip()
    unchanged_catalog = (
        bool(prev_generated_at)
        and prev_doc.get("version") == 1
        and prev_doc.get("repository") == cfg["repository"]
        and prev_doc.get("total_lists") == len(full_entries)
        and prev_doc.get("lists") == full_entries
    )
    generated_at = (
        prev_generated_at
        if unchanged_catalog
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    catalog_doc = {
        "version": 1,
        "generated_at": generated_at,
        "repository": cfg["repository"],
        "total_lists": len(full_entries),
        "lists": full_entries,
    }
    _write_if_changed_text(catalog_file, json.dumps(catalog_doc, indent=2, ensure_ascii=False) + "\n")

    write_sha256sums(RELEASE_DIR)
    update_readme_table(full_entries, cfg)

    elapsed = time.time() - t0
    log(f"Successfully built {len(catalog_entries)} lists in {elapsed:.1f}s -> {RELEASE_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Tachyon unified routing lists and SRS rule-sets.")
    parser.add_argument("--only", nargs="*", help="Build only specific list IDs")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass local download TTL cache and revalidate upstream resources",
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="Allow >50%% rule count reduction relative to previous release/catalog.json",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate generated artifacts in release/ against sources.json without rebuilding",
    )
    args = parser.parse_args()
    if args.validate:
        return validate_release()
    only = {x.strip() for item in (args.only or []) for x in item.split(",") if x.strip()} or None
    return build_all(only, refresh=args.refresh, allow_shrink=args.allow_shrink)


if __name__ == "__main__":
    sys.exit(main())
