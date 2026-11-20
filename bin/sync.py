#!/usr/bin/env python3
"""sync.py - mirror the curated smolBSD package closure + NetBSD 11 binary
sets from the NetBSD CDN into the pkg.smolbsd.org S3 bucket.

Layout served (keys match cdn.netbsd.org paths):
  pub/pkgsrc/packages/NetBSD/<x86_64|aarch64>/11.0/All/*.tgz
  pub/pkgsrc/packages/NetBSD/<arch>/11.0/All/pkg_summary.{gz,bz2}  (trimmed)
  pub/NetBSD-daily/netbsd-11/latest/<amd64|evbarm-aarch64>/binary/sets/*
Every directory under pub/ gets a generated index.html (+ a slash-less
redirect stub), so https://pkg.smolbsd.org/ browses like a NetBSD mirror.

Modes:
  sync.py packages --arch amd64|evbarm-aarch64|all --bucket B [--dry-run]
  sync.py sets     --arch amd64|evbarm-aarch64|all --bucket B [--dry-run]

Mirror source (when cdn.netbsd.org times out): --source URL, env
PKG_SMOLBSD_SOURCE, or a repo `source` file holding a NetBSD mirror root
(https://www.netbsd.org/mirrors/) that carries both pub/pkgsrc/packages and
pub/NetBSD-daily; per-mode --base overrides everything.

Failures (timeouts included) exit with a one-line message, never a
backtrace; re-run the same command -- already-uploaded files are skipped.
Streaming downloads transparently resume interrupted sources with HTTP
Range requests, so one flaky connection no longer costs a whole file.

pkg_summary SIZE is treated as a hint only: every PUT is sized from the
source's own Content-Length (HEAD), so a stale/odd summary cannot stall
the upload, and republished files re-enter the mirror automatically.

Quarterly trees churn in place (an nb-bump deletes the old .tgz): files
the manifest names but the source 404s are skipped with a WARNING and
kept out of index.html/pkg_summary. Regenerate the manifest when that
happens (bin/needs.py + bin/closure.py) to pick up the new versions.
Once a replaced version is truly superseded in the mirror, its stale
.tgz is pruned from the bucket (--no-cleanup disables). The mirror
tracks ONE release: after switching the release file (11.0 -> 11.1),
the next sync mirrors the new release and deletes the old release's
entire subtree -- but only once the new release has landed, so a typo
cannot wipe the mirror.
"""

import argparse
import bz2
import configparser
import datetime
import gzip
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "pkg.smolbsd.org sync.py"}
SRC_TIMEOUT = 180  # source-side socket timeout: detects mid-transfer stalls
PKG_SRC_BASE = "https://cdn.netbsd.org/pub/pkgsrc/packages/NetBSD"
SETS_SRC_BASE = "https://nycdn.netbsd.org/pub/NetBSD-daily"
PKG_ARCH = {"amd64": "x86_64", "x86_64": "x86_64",
            "evbarm-aarch64": "aarch64", "aarch64": "aarch64"}
CONTENT_TYPES = {
    ".tgz": "application/x-gzip",
    ".gz": "application/x-gzip",
    ".bz2": "application/x-bzip2",
    ".xz": "application/x-xz",
    ".html": "text/html",
}
# FILE_NAME split: everything up to the last -<digit...> segment is the
# package name (handles at-spi2-core, gtk3+, libXft, LuaJIT2, ...)
PKG_NAME_RE = re.compile(r"^(.*)-([0-9][^-]*)\.tgz$")


class SourceMissing(Exception):
    """Source no longer serves this file (HTTP 404) - quarterly churn."""


def content_type(key):
    for suffix, ctype in CONTENT_TYPES.items():
        if key.endswith(suffix):
            return ctype
    return "application/octet-stream"


def log(msg):
    print(f"sync.py: {msg}", flush=True)


def _mib(nbytes):
    """Human-readable size for progress output."""
    for unit, div in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if nbytes >= div:
            return f"{nbytes / div:.1f}{unit}"
    return f"{nbytes}B"


class Progress:
    """Current-file + overall transfer progress.

    On a terminal: in-place bar redrawn on stderr. When redirected (cron
    log): one plain line per completed file via log().
    """

    def __init__(self, total_bytes, total_files):
        self.total_bytes = max(total_bytes, 0)
        self.total_files = max(total_files, 1)
        self.done_bytes = 0
        self.done_files = 0
        self.name = ""
        self.cur_size = 0
        self.cur_bytes = 0
        self.t_file = time.time()
        self.t_draw = 0.0
        self.tty = sys.stderr.isatty()

    def start(self, name, size):
        self.name, self.cur_size = name, size
        self.cur_bytes = 0
        self.t_file = time.time()
        if self.tty:
            self._draw()

    def update(self, n):
        self.cur_bytes += n
        self.done_bytes += n
        if self.tty and time.time() - self.t_draw >= 0.2:
            self._draw()

    def finish(self):
        dt = max(time.time() - self.t_file, 1e-6)
        rate = self.cur_bytes / dt / (1 << 20)
        if self.tty:
            self._draw()
            sys.stderr.write("\n")
            sys.stderr.flush()
        else:
            log(f"  [ok] {self.name} {_mib(self.cur_size or self.cur_bytes)}"
                f" in {dt:.0f}s ({rate:.1f} MB/s),"
                f" file {self.done_files + 1}/{self.total_files},"
                f" {_mib(self.done_bytes)} moved")
        self.done_files += 1

    def _draw(self):
        self.t_draw = time.time()
        rate = self.cur_bytes / max(self.t_draw - self.t_file, 1e-6) / (1 << 20)
        if self.total_bytes:
            pct = min(self.done_bytes * 100 // self.total_bytes, 100)
            filled = 20 * pct // 100
            head = (f"{pct:3d}% [{'#' * filled}{'-' * (20 - filled)}]"
                    f"{_mib(self.done_bytes)}/{_mib(self.total_bytes)}")
        else:
            head = (f"file {self.done_files + 1}/{self.total_files}"
                    f" [{_mib(self.done_bytes)}]")
        cur = (f"{_mib(self.cur_bytes)}/{_mib(self.cur_size)}"
               if self.cur_size else _mib(self.cur_bytes))
        tail = f"{cur} {rate:.1f}MB/s"
        # stay inside the terminal: a wrapped bar redraws as new lines
        width = shutil.get_terminal_size((80, 24)).columns
        room = max(width - len(head) - len(tail) - 4, 4)
        name = self.name if len(self.name) <= room else self.name[:room - 1] + "~"
        sys.stderr.write("\r" + f"{head}  {name}  {tail}"[:width - 2] + " ")
        sys.stderr.flush()


class ResumableStream:
    """Resilient HTTP body for streaming (no-disk) source -> S3 PUTs.

    http.client pulls read() from this object while the signed PUT streams
    to S3. If the source stalls or closes early (the classic cdn.netbsd.org
    timeout), the remainder is refetched with an HTTP Range request and
    fed onwards: the S3 body stays contiguous and neither transfer
    restarts. read() never returns b'' before expect bytes have moved --
    a short body would leave the S3 PUT hanging -- an unresumable
    truncation raises instead.
    """

    def __init__(self, url, progress, expect=0, tries=8):
        self.p = progress
        self.tries = tries
        self.resp = http_stream(url)
        self.url = self.resp.geturl()
        got = int(self.resp.headers.get("Content-Length", 0))
        if expect and got and got != expect:
            self.close()
            raise RuntimeError(f"{progress.name}: source serves {got} "
                               f"bytes, expected {expect}")
        self.expect = expect or got
        if self.expect <= 0:
            self.close()
            raise RuntimeError(f"{progress.name}: source gave no "
                               "Content-Length; re-run or drop --no-cache")

    def close(self):
        try:
            self.resp.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass

    def _resume(self):
        """Open a Range connection to the first missing byte (or None)."""
        start = self.p.cur_bytes
        hdrs = dict(UA, Range=f"bytes={start}-")
        last = None
        for attempt in range(self.tries):
            try:
                req = urllib.request.Request(self.url, headers=hdrs)
                resp = urllib.request.urlopen(req, timeout=SRC_TIMEOUT)
                if resp.status != 206:
                    resp.close()
                    raise RuntimeError(
                        f"{self.p.name}: source refuses Range resume "
                        f"(HTTP {resp.status}); re-run or drop --no-cache")
                rng = resp.headers.get("Content-Range", "")
                m = re.match(r"bytes (\d+)-(\d+)/(\d+)", rng)
                if m and (int(m.group(1)) != start
                          or int(m.group(3)) != self.expect):
                    resp.close()
                    raise RuntimeError(
                        f"{self.p.name}: bad resume window {rng!r}")
                return resp
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    raise SourceMissing(self.url) from None
                if err.code == 416:  # asked past EOF
                    if self.p.cur_bytes >= self.expect:
                        return None
                    raise RuntimeError(
                        f"{self.p.name}: source rejects offset {start}"
                    ) from None
                last = err
            except RuntimeError:
                raise  # refusals are deterministic, not transient
            except Exception as err:  # noqa: BLE001 - retry transient
                last = err
            time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"{self.p.name}: resume failed after "
                           f"{self.tries} tries: {last}")

    def read(self, n=-1):
        while True:
            try:
                chunk = self.resp.read(n)
            except Exception as err:  # noqa: BLE001 - resume if incomplete
                if self.p.cur_bytes >= self.expect:
                    return b""  # complete; a dying tail is irrelevant
                log(f"  [resume] {self.p.name}: {str(err)[:50]} at "
                    f"{_mib(self.p.cur_bytes)}/{_mib(self.expect)}")
                resp = self._resume()
                if resp is None:
                    return b""
                self.resp = resp
                continue
            if chunk:
                self.p.update(len(chunk))
                return chunk
            if self.p.cur_bytes >= self.expect:
                return b""
            log(f"  [resume] {self.p.name}: source closed early at "
                f"{_mib(self.p.cur_bytes)}/{_mib(self.expect)}")
            resp = self._resume()
            if resp is None:
                return b""
            self.resp = resp


class CountingStream:
    """Wrap a readable stream so every read() feeds a Progress.

    When expect > 0, a premature EOF raises instead of returning b'':
    http.client would otherwise send a short body under a signed
    Content-Length and the S3 PUT would hang waiting for the rest.
    """

    def __init__(self, stream, progress, expect=0):
        self._stream = stream
        self._progress = progress
        self._expect = expect

    def read(self, n=-1):
        chunk = self._stream.read(n)
        if chunk:
            self._progress.update(len(chunk))
            return chunk
        if self._expect and self._progress.cur_bytes < self._expect:
            raise RuntimeError(
                f"{self._progress.name}: source closed at "
                f"{self._progress.cur_bytes} of {self._expect} bytes")
        return b""

    def close(self):
        self._stream.close()


def default_version():
    """NetBSD release this mirror tracks: env > repo release file > 11.0."""
    env = os.environ.get("PKG_SMOLBSD_VERSION")
    if env:
        return env
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "release")) as f:
            return f.read().strip()
    except OSError:
        return "11.0"


def default_source():
    """NetBSD mirror root (or None -> built-in per-mode defaults).

    env PKG_SMOLBSD_SOURCE > repo `source` file. The root must carry both
    /pub/pkgsrc/packages/NetBSD and /pub/NetBSD-daily; pick a fast one at
    https://www.netbsd.org/mirrors/ when cdn.netbsd.org times out.
    """
    env = os.environ.get("PKG_SMOLBSD_SOURCE")
    if env:
        return env.rstrip("/")
    try:
        with open(os.path.join(os.path.dirname(__file__), "..",
                               "source")) as f:
            return f.read().strip().rstrip("/")
    except OSError:
        return None


def http_fetch(url, tries=4):
    """GET a URL to bytes, with backoff. HTTP redirects are followed."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=SRC_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise SourceMissing(url) from None
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
        except Exception as err:  # noqa: BLE001 - retry anything transient
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def http_fetch_to(url, dest, tries=4, progress=None):
    """Stream a URL to a file (small-memory path for big packages)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=SRC_TIMEOUT) as resp, \
                    open(dest + ".part", "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    if progress:
                        progress.update(len(chunk))
            os.replace(dest + ".part", dest)  # cache never holds partials
            return
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise SourceMissing(url) from None
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
        except Exception as err:  # noqa: BLE001 - retry overwrites from start
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def file_sha512(path):
    digest = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_stream(url, tries=4):
    """Open a URL as a readable stream (caller uploads and closes it).

    Retries connection-phase failures with backoff; a failure mid-transfer
    surfaces to the caller and a later re-run skips completed objects.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=SRC_TIMEOUT)
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise SourceMissing(url) from None
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
        except Exception as err:  # noqa: BLE001 - retry anything transient
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def http_size(url, tries=4):
    """Authoritative file size via HEAD (0 when the server omits it).

    pkg_summary SIZE is a hint and can be off by a few bytes; sizing a
    signed S3 PUT from it stalls the upload when the stream ends early.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=SRC_TIMEOUT) as resp:
                return int(resp.headers.get("Content-Length", 0))
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return None  # file gone from the source tree
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
        except Exception as err:  # noqa: BLE001 - retry anything transient
            last = err
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"HEAD {url} failed after {tries} tries: {last}")


def imds_creds():
    """Short-lived credentials from EC2 instance role via IMDSv2 (or None)."""
    base = "http://169.254.169.254/latest"
    try:
        req = urllib.request.Request(
            base + "/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})
        with urllib.request.urlopen(req, timeout=1) as resp:
            token = resp.read().decode()
        headers = {"X-aws-ec2-metadata-token": token}
        req = urllib.request.Request(
            base + "/meta-data/iam/security-credentials/", headers=headers)
        with urllib.request.urlopen(req, timeout=1) as resp:
            role = resp.read().decode().strip().splitlines()[0]
        req = urllib.request.Request(
            f"{base}/meta-data/iam/security-credentials/{role}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=2) as resp:
            cred = json.loads(resp.read().decode())
        return (cred["AccessKeyId"], cred["SecretAccessKey"],
                cred["Token"])
    except Exception:
        return None


def creds():
    """AWS credentials: env vars, then EC2 instance role, then
    ~/.aws/credentials (AWS_PROFILE honored)."""
    if all(os.environ.get(k) for k in
           ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")):
        return (os.environ["AWS_ACCESS_KEY_ID"],
                os.environ["AWS_SECRET_ACCESS_KEY"],
                os.environ.get("AWS_SESSION_TOKEN"))
    imds = imds_creds()
    if imds:
        return imds
    profile = os.environ.get("AWS_PROFILE", "default")
    parser = configparser.ConfigParser()
    parser.read(os.path.expanduser("~/.aws/credentials"))
    if parser.has_section(profile):
        return (parser[profile]["aws_access_key_id"],
                parser[profile]["aws_secret_access_key"],
                parser[profile].get("aws_session_token"))
    sys.exit("sync.py: no AWS credentials (env or ~/.aws/credentials)")


class S3:
    """Minimal S3 client with SigV4: list, head, get, put. No dependencies."""

    def __init__(self, bucket, region, dry=False):
        self.bucket = bucket
        self.region = region
        self.dry = dry
        self.access_key, self.secret_key, self.token = creds()
        # path-style: dotted bucket names (pkg.smolbsd.org) break the
        # *.s3.<region>.amazonaws.com wildcard TLS certificate
        self.host = f"s3.{region}.amazonaws.com"

    def _path(self, key):
        return "/" + self.bucket + ("/" + self._uri_quote(key, safe="/")
                                    if key else "")

    # -- signing --------------------------------------------------------
    @staticmethod
    def _uri_quote(s, safe=""):
        return urllib.parse.quote(s, safe=safe + "~")

    def _signed_headers(self, method, key, query, extra, payload,
                        stream=False):
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        if stream:
            payload_hash = "UNSIGNED-PAYLOAD"  # HTTPS + streamed file body
        elif payload is not None:
            payload_hash = hashlib.sha256(payload).hexdigest()
        else:
            payload_hash = hashlib.sha256(b"").hexdigest()
        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if self.token:
            headers["x-amz-security-token"] = self.token
        for k, v in (extra or {}).items():
            headers[k.lower()] = v
        signed_names = sorted(headers)
        canonical_headers = "".join(
            f"{k}:{headers[k].strip()}\n" for k in signed_names)
        canonical_query = "&".join(
            f"{self._uri_quote(k)}={self._uri_quote(v)}"
            for k, v in sorted((query or {}).items()))
        canonical_request = "\n".join([
            method,
            self._path(key),
            canonical_query,
            canonical_headers,
            ";".join(signed_names),
            payload_hash,
        ])
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])

        def hmac(key, msg):
            import hmac as _hmac
            return _hmac.new(key, msg.encode(), hashlib.sha256).digest()

        signing_key = hmac(hmac(hmac(hmac(
            ("AWS4" + self.secret_key).encode(), date_stamp),
            self.region), "s3"), "aws4_request")
        signature = hmac(signing_key, string_to_sign).hex()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={';'.join(signed_names)}, Signature={signature}")
        return headers

    def _request(self, method, key="", query=None, extra=None, payload=None):
        headers = self._signed_headers(method, key, query, extra, payload)
        url = f"https://{self.host}{self._path(key)}"
        if query:
            url += "?" + "&".join(
                f"{self._uri_quote(k)}={self._uri_quote(v)}"
                for k, v in sorted(query.items()))
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=payload)
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            body = err.read()
            if err.code == 404 and b"NoSuchBucket" in body:
                raise NoSuchBucket(self.bucket) from None
            return err.code, dict(err.headers), body

    # -- operations ------------------------------------------------------
    def exists(self):
        try:
            # probe with pub/: the sync IAM role is scoped to that prefix and
            # an empty-prefix ListObjects would be denied by its condition
            self.list_prefix("pub/", limit=1)
            return True
        except NoSuchBucket:
            return False

    def list_prefix(self, prefix, limit=None):
        """Return {key: size} for a prefix (paginated ListObjectsV2)."""
        out, token = {}, None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            status, _, body = self._request("GET", "", query)
            if status != 200:
                raise RuntimeError(f"list {prefix}: HTTP {status}: {body[:200]}")
            root = ET.fromstring(body)
            ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
            for contents in root.iter(ns + "Contents"):
                out[contents.findtext(ns + "Key")] = int(
                    contents.findtext(ns + "Size"))
            if limit and len(out) >= limit:
                break
            if root.findtext(ns + "IsTruncated") == "true":
                token = root.findtext(ns + "NextContinuationToken")
            else:
                break
        return out

    def meta(self, key):
        status, headers, _ = self._request("HEAD", key)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"head {key}: HTTP {status}")
        return headers

    def get(self, key):
        status, headers, body = self._request("GET", key)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"get {key}: HTTP {status}")
        return body

    def put(self, key, data, redirect=None, metadata=None, planned_size=None):
        if self.dry:
            size = planned_size if planned_size is not None else len(data)
            log(f"  [dry-run] would PUT {key} ({size} bytes)")
            return
        extra = {"content-type": content_type(key)}
        if redirect:
            extra["x-amz-website-redirect-location"] = redirect
        for mk, mv in (metadata or {}).items():
            extra[f"x-amz-meta-{mk}"] = mv
        status, headers, body = self._request("PUT", key, extra=extra,
                                              payload=data)
        if status != 200:
            raise RuntimeError(f"put {key}: HTTP {status}: {body[:200]}")
        log(f"  [PUT] {key} ({len(data)} bytes)")

    def delete(self, key):
        if self.dry:
            log(f"  [dry-run] would DELETE {key}")
            return
        status, _, body = self._request("DELETE", key)
        if status not in (200, 204):
            raise RuntimeError(f"delete {key}: HTTP {status}: {body[:200]}")
        log(f"  [DEL] {key}")

    def put_file(self, key, path, size, metadata=None):
        """Stream-upload a local file with SigV4 UNSIGNED-PAYLOAD."""
        if self.dry:
            log(f"  [dry-run] would PUT {key} ({size} bytes)")
            return
        extra = {"content-type": content_type(key),
                 "content-length": str(size)}
        for mk, mv in (metadata or {}).items():
            extra[f"x-amz-meta-{mk}"] = mv
        last = None
        for attempt in range(4):
            try:
                headers = self._signed_headers("PUT", key, None, extra,
                                               None, stream=True)
                conn = http.client.HTTPSConnection(self.host, timeout=600)
                with open(path, "rb") as body:
                    conn.request("PUT", self._path(key), body=body,
                                 headers=headers)
                    resp = conn.getresponse()
                    resp_body = resp.read()
                    conn.close()
                if resp.status == 200:
                    log(f"  [PUT] {key} ({size} bytes)")
                    return
                last = f"HTTP {resp.status}: {resp_body[:200]}"
            except Exception as err:  # noqa: BLE001
                last = str(err)
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"put_file {key} failed: {last}")

    def put_stream(self, key, stream, size, metadata=None):
        """Stream an open readable object (e.g. HTTP response) straight to S3:
        no local disk. Integrity = HTTPS + enforced Content-Length; S3 rejects
        truncated or oversize bodies and a failed PUT stores nothing."""
        if self.dry:
            log(f"  [dry-run] would PUT {key} ({size} bytes)")
            return
        extra = {"content-type": content_type(key),
                 "content-length": str(size)}
        for mk, mv in (metadata or {}).items():
            extra[f"x-amz-meta-{mk}"] = mv
        headers = self._signed_headers("PUT", key, None, extra,
                                       None, stream=True)
        conn = http.client.HTTPSConnection(self.host, timeout=600)
        try:
            conn.request("PUT", self._path(key), body=stream, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise RuntimeError(f"put_stream {key}: HTTP {resp.status}: "
                               f"{body[:200]}")
        log(f"  [PUT] {key} ({size} bytes)")


class NoSuchBucket(Exception):
    pass


# ---------------------------------------------------------------------------
# packages mode
# ---------------------------------------------------------------------------

def read_manifest(path):
    """Return [(file_name, size)] from a manifest produced by closure.py."""
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fname, size = line.split("\t")
            entries.append((fname, int(size)))
    return entries


def trimmed_summary(source_text, file_names):
    """Filter raw pkg_summary blocks down to the manifest's FILE_NAMEs."""
    wanted = set(file_names)
    kept = []
    for block in re.split(r"\n\s*\n", source_text):
        m = re.search(r"^FILE_NAME=(.+)$", block, re.MULTILINE)
        if m and m.group(1).strip() in wanted:
            kept.append(block.rstrip("\n"))
    return ("\n\n".join(kept) + "\n\n").encode()


def index_html(files, title, base="", show_parent=True):
    lines = [f"<html><head><title>Index of {title}</title>"]
    if base:
        lines.append(f'<base href="{base}">')
    lines.append("</head><body>")
    lines.append(f"<h1>Index of {title}</h1>")
    if show_parent:
        lines.append('<a href="../">../</a><br />')
    for f in sorted(files):
        lines.append(f'<a href="{f}">{f}</a><br />')
    lines.append("</body></html>")
    return "\n".join(lines).encode()


def prune_replaced(s3, prefix, bucket_files, manifest_files, avail_files):
    """Delete stale .tgz versions that the mirror has replaced.

    A bucket file name-ver.tgz is deleted only when BOTH hold:
      - the manifest no longer lists that exact version, AND
      - the mirror now offers a different version of the same package.
    A package that is merely absent from the manifest is never pruned,
    and neither is the last version the mirror has. Versioned bucket +
    lifecycle rules make the deletion recoverable and self-cleaning.
    """
    man, avail = {}, {}
    for coll, src in ((man, manifest_files), (avail, avail_files)):
        for f in src:
            m = PKG_NAME_RE.match(f)
            if m:
                coll.setdefault(m.group(1), set()).add(m.group(2))
    doomed = []
    for f in bucket_files:
        m = PKG_NAME_RE.match(f) if f.endswith(".tgz") else None
        if not m:
            continue
        name, ver = m.group(1), m.group(2)
        if ver not in man.get(name, ()) and any(
                v != ver for v in avail.get(name, ())):
            doomed.append(f)
    for f in sorted(doomed):
        s3.delete(f"{prefix}/{f}")
    return doomed


def update_index_pages(s3):
    """bozohttpd-style autoindex: an index.html per directory under pub/,
    plus a zero-byte redirect stub so slash-less paths resolve like on
    cdn.netbsd.org. Runs after uploads/pruning so pages match real
    objects; only changed pages are re-PUT. CloudFront serves
    pub/index.html at / via DefaultRootObject (<base> keeps links right).
    """
    if not s3.exists():
        log("  (bucket empty: directory pages will come with the data)")
        return
    keys = sorted(s3.list_prefix("pub/"))
    dirs = set()
    for key in keys:
        parts = key.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]) + "/")
    tree = {d: set() for d in dirs}
    for key in keys:
        if key.endswith("/index.html") or "/" not in key:
            continue
        parent, name = key.rsplit("/", 1)
        parent += "/"
        entry = name + "/" if key + "/" in dirs else name
        tree.setdefault(parent, set()).add(entry)
    updated = 0
    for d in sorted(tree):
        page = d + "index.html"
        html = index_html(tree[d], "/" + d, base="/" + d,
                          show_parent=d.count("/") > 1)
        try:
            if s3.get(page) != html:
                s3.put(page, html)
                updated += 1
            stub = d.rstrip("/")
            if stub and s3.get(stub) is None:
                s3.put(stub, b"", redirect="/" + d)
                updated += 1
        except Exception as err:  # noqa: BLE001 - housekeeping never fatal
            log(f"  [warn] page/stub for {d}: {err}")
    log(f"  directory pages: {len(tree)} dir(s), {updated} updated")


def sync_packages(arch, s3, manifest_dir, version, base, dry, cache_dir,
                  no_cache=False, cleanup=True):
    pkgarch = PKG_ARCH[arch]
    manifest = os.path.join(manifest_dir, f"needed-{pkgarch}.txt")
    if not os.path.isfile(manifest):
        sys.exit(f"sync.py: manifest not found: {manifest}")
    entries = read_manifest(manifest)
    prefix = f"pub/pkgsrc/packages/NetBSD/{pkgarch}/{version}/All"
    log(f"packages {pkgarch}: {len(entries)} files from {manifest}")

    existing = s3.list_prefix(prefix + "/") if s3.exists() else {}
    existing = {k.rsplit("/", 1)[1]: v for k, v in existing.items()}

    # 1. .tgz files: upload missing / size-changed
    todo = [(f, size) for f, size in entries
            if existing.get(f) is None or existing[f] != size]
    skipped = len(entries) - len(todo)
    mb = sum(s for _, s in todo) / 1024 / 1024
    log(f"  {skipped} up to date, {len(todo)} to upload ({mb:.0f} MB)")
    prog = Progress(sum(s for _, s in todo), len(todo))
    gone = []  # manifest names the source no longer serves (quarterly churn)

    def drop(fname, why):
        if existing.get(fname):
            log(f"  note {fname}: off source, keeping mirror copy")
        else:
            gone.append(fname)
            log(f"  [MISSING] {fname}: {why}, skipped")

    if dry and not todo:
        log("  (dry-run: nothing queued for download)")
    for fname, size in todo:
        url = f"{base}/{pkgarch}/{version}/All/{fname}"
        if dry:
            s3.put(f"{prefix}/{fname}", b"", planned_size=size)
            continue
        # pkg_summary SIZE is a hint; HEAD is authoritative
        real = http_size(url)
        if real is None:
            drop(fname, "HTTP 404 on HEAD")
            continue
        real = real or size
        if real != size:
            log(f"  note {fname}: manifest {size}, source serves {real}")
        if existing.get(fname) == real:
            log(f"  up to date {fname}: S3 already has {real} bytes")
            continue
        prog.start(fname, real)
        path = os.path.join(cache_dir, pkgarch, fname)
        try:
            if os.path.isfile(path) and os.path.getsize(path) == real:
                log(f"  cache hit {fname}")
                s3.put_file(f"{prefix}/{fname}", path, real)
            elif no_cache:
                log(f"  streaming {url}")
                rs = ResumableStream(url, prog, real)
                try:
                    s3.put_stream(f"{prefix}/{fname}", rs, rs.expect)
                finally:
                    rs.close()
            else:
                log(f"  downloading {url}")
                http_fetch_to(url, path)
                got = os.path.getsize(path)
                if real and got != real:
                    raise RuntimeError(
                        f"{fname}: downloaded {got}, source serves {real}")
                s3.put_file(f"{prefix}/{fname}", path, got)
        except SourceMissing:
            drop(fname, "vanished mid-transfer")
            continue
        prog.finish()
    if gone:
        log(f"  WARNING: {len(gone)} manifest file(s) no longer on the "
            "source tree:")
        for f in gone:
            log(f"    - {f}")
        log("  quarterly trees churn in place: regenerate the manifest "
            "(bin/needs.py + bin/closure.py) and re-sync")

    # gone files are excluded from listing sources and pkg_summary, so
    # pkgin/fetch.sh only ever see objects that actually exist
    absent = set(gone)
    present = [f for f, _ in entries if f not in absent]

    # 2. trimmed pkg_summary last: pkgin only ever sees mirrored files
    log(f"  fetching source pkg_summary ({base}/{pkgarch}/{version})")
    if dry and not s3.exists():
        log("  (dry-run: would rebuild trimmed pkg_summary.gz/.bz2")
        return
    source = gzip.decompress(
        http_fetch(f"{base}/{pkgarch}/{version}/All/pkg_summary.gz")
    ).decode("utf-8", "replace")
    blob = trimmed_summary(source, present)
    gz, bz = gzip.compress(blob, 9), bz2.compress(blob)
    if s3.get(f"{prefix}/pkg_summary.gz") != gz:
        s3.put(f"{prefix}/pkg_summary.gz", gz)
    if s3.get(f"{prefix}/pkg_summary.bz2") != bz:
        s3.put(f"{prefix}/pkg_summary.bz2", bz)
    log(f"  trimmed summary: {blob.count(b'PKGNAME=')} packages kept")

    # 3. prune stale versions the manifest no longer wants (churn cleanup).
    #    Best-effort housekeeping: never fail an otherwise-good sync run
    #    (e.g. role lacks s3:DeleteObject until the stack is redeployed).
    if cleanup:
        try:
            doomed = prune_replaced(s3, prefix, sorted(existing),
                                    [f for f, _ in entries], present)
            if doomed:
                log(f"  pruned {len(doomed)} replaced version(s)")
        except Exception as err:  # noqa: BLE001 - cleanup is optional
            log(f"  WARNING: cleanup skipped: {err}")
            log("  (grant the sync role s3:DeleteObject to enable pruning)")


# ---------------------------------------------------------------------------
# sets mode
# ---------------------------------------------------------------------------

def prune_stale_versions(s3, mode, version, branch, dry):
    """Drop subtrees of other releases/branches after a release switch.

    The mirror tracks exactly ONE release: once 11.1 is in the bucket,
    11.0's whole subtree (packages and/or sets) is deleted. Guard: never
    prunes while the current release has no objects yet, so a mistyped
    --version cannot wipe the mirror. Only keys whose release/branch
    segment matches a recognizable shape are candidates; stubs and
    index pages of the kept release are untouched.
    """
    if mode == "packages":
        root, seg, keep = "pub/pkgsrc/packages/NetBSD/", 5, version
        shape = re.compile(r"\d+\.\d")  # 11.0, 11.1, ...
    else:
        root, seg, keep = "pub/NetBSD-daily/", 2, branch
        shape = re.compile(r"netbsd-")  # netbsd-10, netbsd-11, ...
    if not s3.exists():
        return
    keys = sorted(s3.list_prefix(root))

    def seg_of(k):
        p = k.split("/")
        return p[seg] if len(p) > seg else ""

    stale = [k for k in keys
             if seg_of(k) != keep and shape.match(seg_of(k))]
    if not stale:
        return
    if not dry and not any(seg_of(k) == keep for k in keys):
        log(f"  keeping stale subtree(s): bucket has no {keep} data yet")
        return
    gone = sorted({seg_of(k) for k in stale})
    log(f"  release switch: pruning {' '.join(gone)} "
        f"({len(stale)} object(s))")
    try:
        for k in stale:
            s3.delete(k)
    except Exception as err:  # noqa: BLE001 - remaining keys go next run
        log(f"  WARNING: release prune incomplete: {err}")


def sync_sets(arch, s3, branch, base, dry, exclude=("kern-",), cache_dir="",
              no_cache=False, cleanup=True):
    prefix = f"pub/NetBSD-daily/{branch}/latest/{arch}/binary/sets"
    src = f"{base}/{branch}/latest/{arch}/binary/sets"
    log(f"sets {arch}: reading source manifest {src}/SHA512")
    sha_text = http_fetch(f"{src}/SHA512").decode()
    want = re.findall(r"SHA512 \((.+?)\) = ([0-9a-f]{128})", sha_text)
    if not want:
        raise RuntimeError(f"no entries parsed from {src}/SHA512")
    dropped = [n for n, _ in want if n.startswith(exclude)]
    want = [(n, d) for n, d in want if not n.startswith(exclude)]
    log(f"  {len(dropped)} excluded ({' '.join(dropped)})")

    existing = {}
    if s3.exists():
        for key in s3.list_prefix(prefix + "/"):
            headers = s3.meta(key) or {}
            existing[key.rsplit("/", 1)[1]] = headers.get("x-amz-meta-sha512")

    todo = [name for name, digest in want if existing.get(name) != digest]
    log(f"  {len(want) - len(todo)} up to date, {len(todo)} to mirror")
    prog = Progress(0, len(todo))  # byte total unknown: per-file sizes only
    set_gone = []
    for name in todo:
        url = f"{src}/{name}"
        if dry:
            s3.put(f"{prefix}/{name}", b"", metadata={"sha512": "dry-run"})
            continue
        digest = dict(want)[name]
        path = os.path.join(cache_dir, "sets", arch, name)
        prog.start(name, 0)
        try:
            if os.path.isfile(path) and file_sha512(path) == digest:
                log(f"  cache hit {name}")
                s3.put_file(f"{prefix}/{name}", path, os.path.getsize(path),
                            metadata={"sha512": digest})
            elif no_cache:
                log(f"  streaming {url}")
                rs = ResumableStream(url, prog)
                try:
                    prog.start(name, rs.expect)
                    s3.put_stream(f"{prefix}/{name}", rs, rs.expect,
                                  metadata={"sha512": digest})
                finally:
                    rs.close()
            else:
                log(f"  downloading {url}")
                http_fetch_to(url, path, progress=prog)
                if file_sha512(path) != digest:
                    raise RuntimeError(
                        f"{name}: sha512 mismatch vs source SHA512")
                s3.put_file(f"{prefix}/{name}", path, os.path.getsize(path),
                            metadata={"sha512": digest})
        except SourceMissing:
            set_gone.append(name)
            log(f"  [MISSING] {name}: in SHA512 but 404 on source, skipped")
            continue
        prog.finish()
    if set_gone:
        log(f"  WARNING: {len(set_gone)} set file(s) 404 on source: "
            f"{' '.join(set_gone)}")
    # the SHA512 manifest itself, last (mirrors cdn layout)
    s3.put(f"{prefix}/SHA512", sha_text.encode(),
           metadata={"sha512": hashlib.sha512(sha_text.encode()).hexdigest()})
    # set files that disappeared from the source manifest are not mirrored
    if cleanup:
        keep = {n for n, _ in want} | {"SHA512", "index.html"}
        extra = sorted(n for n in existing if n not in keep)
        try:
            for n in extra:
                s3.delete(f"{prefix}/{n}")
            if extra:
                log(f"  pruned {len(extra)} set file(s) no longer in SHA512")
        except Exception as err:  # noqa: BLE001 - cleanup never fatal
            log(f"  WARNING: set cleanup incomplete: {err}")


# ---------------------------------------------------------------------------

def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bucket", required=True)
    common.add_argument("--region",
                        default=os.environ.get("AWS_REGION", "eu-west-1"))
    common.add_argument("--arch", default="all",
                        help="amd64|x86_64|evbarm-aarch64|aarch64|all")
    common.add_argument("--source",
                        help="NetBSD mirror root for when cdn.netbsd.org "
                             "times out (env PKG_SMOLBSD_SOURCE or repo "
                             "`source` file also work); must carry both "
                             "pub/pkgsrc/packages and pub/NetBSD-daily, "
                             "see https://www.netbsd.org/mirrors/")
    common.add_argument("--dry-run", action="store_true",
                        help="read S3, plan and log uploads, write nothing")
    common.add_argument("--cache",
                        default=os.path.join(tempfile.gettempdir(),
                                             "pkg-smolbsd-cache"),
                        help="persistent download cache (default: %(default)s)")

    common.add_argument("--no-cleanup", action="store_true",
                        help="keep superseded .tgz versions in the bucket "
                             "instead of pruning them after a version bump")
    common.add_argument("--no-cache", action="store_true",
                        help="stream source to S3 without local copies "
                             "(for small-root-disk  hosts)")

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)

    pk = sub.add_parser("packages", parents=[common],
                        help="mirror the curated pkgsrc closure")
    pk.add_argument("--manifest-dir",
                    default=os.path.join(os.path.dirname(__file__), "..",
                                         "manifest"))
    pk.add_argument("--version",
                    help=f"pkgsrc OS version (default: release file / env, "
                         f"currently {default_version()})")
    pk.add_argument("--base",
                    help="packages source base URL, overrides --source "
                         f"(default: {PKG_SRC_BASE})")

    st = sub.add_parser("sets", parents=[common],
                        help="mirror NetBSD-daily binary sets")
    st.add_argument("--branch",
                    help="NetBSD-daily branch (default: netbsd-<major> of "
                         "the release version)")
    st.add_argument("--base",
                    help="sets source base URL, overrides --source "
                         f"(default: {SETS_SRC_BASE})")
    st.add_argument("--exclude", default=["kern-"], action="append",
                    help="filename prefix to skip (repeatable; default kern-, "
                         "smolBSD ships its own kernels)")

    args = ap.parse_args()
    version = getattr(args, "version", None) or default_version()
    branch = getattr(args, "branch", None) or f"netbsd-{version.split('.')[0]}"
    source = args.source or default_source()
    if args.mode == "packages":
        base = args.base or (f"{source}/pub/pkgsrc/packages/NetBSD"
                             if source else PKG_SRC_BASE)
    else:
        base = args.base or (f"{source}/pub/NetBSD-daily"
                             if source else SETS_SRC_BASE)
    log(f"source: {base}")
    if args.arch == "all":
        arches = (["amd64", "evbarm-aarch64"] if args.mode == "sets"
                  else ["x86_64", "aarch64"])
    else:
        arches = [args.arch] if args.mode == "sets" else [PKG_ARCH[args.arch]]

    for arch in arches:
        s3 = S3(args.bucket, args.region, dry=args.dry_run)
        if not s3.exists():
            log(f"bucket s3://{args.bucket} does not exist yet: "
                f"planning a cold mirror")
        if args.mode == "packages":
            sync_packages(arch, s3, args.manifest_dir, version,
                          base, args.dry_run, args.cache, args.no_cache,
                          cleanup=not args.no_cleanup)
        else:
            sync_sets(arch, s3, branch, base, args.dry_run,
                      tuple(args.exclude), args.cache, args.no_cache,
                      cleanup=not args.no_cleanup)

    s3 = S3(args.bucket, args.region, dry=args.dry_run)
    if not args.no_cleanup:
        prune_stale_versions(s3, args.mode, version, branch, args.dry_run)
    update_index_pages(s3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
    except (OSError, http.client.HTTPException, RuntimeError,
            SyntaxError) as err:
        # URLError/timeout/ConnectionReset are OSError subclasses; SyntaxError
        # covers a mirror serving garbage where XML/summary was expected.
        log(f"sync stopped: {err}")
        log("re-run the same command: already-uploaded files are skipped. "
            "If the source keeps timing out, try a faster mirror: "
            "--source https://<mirror> (https://www.netbsd.org/mirrors/)")
        sys.exit(1)
