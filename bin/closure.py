#!/usr/bin/env python3
"""closure.py - resolve the transitive dependency closure of package names
against a pkgsrc 11.0 binary tree (pkg_summary.gz), as served by
cdn.netbsd.org (and, later, mirrored on pkg.smolbsd.org).

Input : list of bare package names (one per line, # comments allowed),
        from stdin or --list FILE.
Output: manifest on stdout (--out FILE) - "FILE_NAME<TAB>FILE_SIZE" lines,
        sorted; stats and unresolved deps on stderr.

stdlib only (urllib, gzip) so it runs locally and in Lambda/Fargate alike.
"""

import argparse
import gzip
import os
import re
import sys
import urllib.request

BASE = "https://cdn.netbsd.org/pub/pkgsrc/packages/NetBSD"
ARCH_MAP = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "evbarm-aarch64": "aarch64",
    "aarch64": "aarch64",
    "i386": "i386",
}
UA = {"User-Agent": "pkg.smolbsd.org closure.py"}


def log_url(url):
    sys.stderr.write(f"closure.py: fetching {url}\n")


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


def strip_name(s):
    """Reduce a PKGNAME or dependency string to its bare package name.

    Handles: foo-1.2nb3, foo>=1.2, foo>=1.2<2, foo-1.2{,nb*}, foo-[0-9]*,
    and names containing digits or dashes (py314-pip, p5-JSON-XS).
    The version part is the first '-' (or operator) followed by a digit or
    a glob/brace pattern.
    """
    # cut at first version constraint operator
    m = re.search(r"(>=|<=|==|!=|=|<|>)", s)
    if m:
        s = s[: m.start()]
    # cut at first '-<digit-or-glob>'
    m = re.search(r"-(?=[0-9])|-\[(?=[0-9])|-\{", s)
    if m:
        s = s[: m.start()]
    return s


def load_summary(url):
    """Return {name: (pkgname, file_name, file_size)} from a pkg_summary.gz."""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp:
        text = gzip.decompress(resp.read()).decode("utf-8", "replace")
    entries = {}
    name = fname = None
    size = 0
    deps = []
    for line in text.splitlines():
        if not line.strip():
            if name:
                entries[name] = (pkgname, fname or pkgname + ".tgz", size, tuple(deps))
            name = pkgname = fname = None
            size = 0
            deps = []
            continue
        if line.startswith("PKGNAME="):
            pkgname = line[8:].strip()
            name = strip_name(pkgname)
        elif line.startswith("FILE_NAME="):
            fname = line[10:].strip()
        elif line.startswith("FILE_SIZE="):
            try:
                size = int(line[10:].strip())
            except ValueError:
                size = 0
        elif line.startswith("DEPENDS="):
            deps.append(line[8:].strip())
    if name:
        entries[name] = (pkgname, fname or pkgname + ".tgz", size, tuple(deps))
    return entries


def resolve(seeds, entries):
    """BFS over DEPENDS; returns (ordered list of names, sorted unresolved)."""
    order = []
    seen = set()
    unresolved = set()
    queue = list(seeds)
    while queue:
        dep = queue.pop(0)
        name = strip_name(dep)
        if not name:
            continue
        if name not in entries:
            unresolved.add(dep)
            continue
        if name in seen:
            continue
        seen.add(name)
        order.append(name)
        queue.extend(entries[name][3])
    return order, sorted(unresolved)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", help="file of package names (default: stdin)")
    ap.add_argument("--arch", required=True,
                    help="amd64|x86_64|evbarm-aarch64|aarch64|i386")
    ap.add_argument("--version",
                    help="pkgsrc OS version (default: release file / env)")
    ap.add_argument("--base", default=BASE, help="repository base URL")
    ap.add_argument("--out", help="write manifest here (default: stdout)")
    args = ap.parse_args()
    version = args.version or default_version()

    arch = ARCH_MAP.get(args.arch)
    if not arch:
        sys.exit(f"closure.py: unknown arch {args.arch!r}")

    if args.list:
        with open(args.list) as f:
            raw = f.read()
    else:
        raw = sys.stdin.read()
    seeds = [l.split("#", 1)[0].strip() for l in raw.splitlines()]
    seeds = sorted({s for s in seeds if s})

    url = f"{args.base}/{arch}/{version}/All/pkg_summary.gz"
    sys.stderr.write(f"closure.py: fetching {url}\n")
    entries = load_summary(url)
    sys.stderr.write(f"closure.py: tree has {len(entries)} packages\n")

    order, unresolved = resolve(seeds, entries)
    total = sum(entries[n][2] for n in order)

    lines = [f"{entries[n][1]}\t{entries[n][2]}" for n in sorted(order)]
    header = [
        f"# pkg.smolbsd.org manifest - pkgsrc {version} / {arch}",
        f"# {len(seeds)} seeds -> {len(order)} packages, "
        f"{total / 1024 / 1024:.0f} MB compressed",
    ]
    out = "\n".join(header + lines) + "\n"
    if args.out:
        with open(args.out, "w") as f:
            f.write(out)
        sys.stderr.write(f"closure.py: wrote {args.out}\n")
    else:
        sys.stdout.write(out)

    for dep in unresolved:
        sys.stderr.write(f"closure.py: UNRESOLVED: {dep}\n")
    if unresolved:
        sys.exit(3)


if __name__ == "__main__":
    main()
