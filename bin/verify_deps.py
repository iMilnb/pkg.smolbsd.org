#!/usr/bin/env python3
"""verify_deps.py - independently audit a manifest for missing dependencies.

Reuses ONLY closure.load_summary (fetch/parse pkg_summary.gz) and
closure.strip_name. Deliberately does NOT reuse closure.resolve, so this is
a second pair of eyes on the result, not a rerun of the same logic.

Correctness property checked (what pkgin/pkg_add needs at install time):
  for every package P in the manifest, every DEPENDS of P must be satisfied
  by some OTHER package also present in the manifest (name match, and if the
  DEPENDS carries a version constraint, that constraint must actually be
  met). The trimmed pkg_summary we serve keeps DEPENDS lines verbatim, so an
  unsatisfiable DEPENDS = a broken install even if the tree itself is fine.

Exit 0 = manifest self-consistent; exit 3 = at least one unsatisfied dep.
"""
import os
import re
import sys
import argparse

sys.path.insert(0, os.path.dirname(__file__))
import closure  # noqa: E402


def pkg_base_and_ver(pkgname):
    """fontconfig-2.18.3nb1 -> ('fontconfig', '2.18.3nb1')."""
    m = re.match(r"^(.*?)-([0-9][^-]*)$", pkgname)
    if m:
        return m.group(1), m.group(2)
    return closure.strip_name(pkgname), ""


def vtok(v):
    return re.findall(r"\d+|\D+", v or "")


def cmpv(a, b):
    ta, tb = vtok(a), vtok(b)
    for x, y in zip(ta, tb):
        nx, ny = x.isdigit(), y.isdigit()
        if nx and ny:
            if int(x) != int(y):
                return -1 if int(x) < int(y) else 1
        elif x != y:
            return -1 if x < y else 1
    if len(ta) != len(tb):
        return -1 if len(ta) < len(tb) else 1
    return 0


def parse_dep(dep):
    """'gtk+>=3.24<4' -> ('gtk+', [('>=','3.24'),('<','4')]);  no ops -> []."""
    m = re.search(r"(>=|<=|==|!=|=|<|>)", dep)
    if not m:
        return closure.strip_name(dep), []
    name = dep[:m.start()]
    cons = re.findall(r"(>=|<=|==|!=|=|<|>)([^<>!=]+)", dep)
    return name, cons


def satisfied(cons, ver):
    for op, want in cons:
        c = cmpv(ver, want)
        ok = {">=": c >= 0, ">": c > 0, "<=": c <= 0, "<": c < 0,
              "=": c == 0, "==": c == 0, "!=": c != 0}.get(op)
        if ok is None or not ok:
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--arch", required=True)
    ap.add_argument("--version", help="default: release file/env")
    ap.add_argument("--base", default=closure.BASE)
    a = ap.parse_args()

    ver = a.version or closure.default_version()
    arch = {"amd64": "x86_64", "evbarm-aarch64": "aarch64"}.get(a.arch, a.arch)
    url = f"{a.base}/{arch}/{ver}/All/pkg_summary.gz"
    entries = closure.load_summary(url)  # name -> (pkgname,file,size,deps)
    closure.log_url(url)

    manifest_files = set()
    with open(a.manifest) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                manifest_files.add(line.split("\t")[0])

    # map: base name -> list of (pkgver) actually mirrored
    mirrored = {}
    for name, (pkgname, fname, _sz, _d) in entries.items():
        if fname in manifest_files:
            base, v = pkg_base_and_ver(pkgname)
                # fall back to entries' own base key
            mirrored.setdefault(base or name, []).append((v, pkgname))
    # sanity: every manifest FILE_NAME should exist in the live summary
    known = {e[1] for e in entries.values()}
    stale = sorted(manifest_files - known)

    problems, checked = [], 0
    for name, (pkgname, fname, _sz, deps) in entries.items():
        if fname not in manifest_files:
            continue
        for dep in deps:
            checked += 1
            dbase, cons = parse_dep(dep)
            cands = mirrored.get(dbase)
            if not cands:
                problems.append((pkgname, dep, "not in manifest"))
            elif cons and not any(satisfied(cons, v) for v, _ in cands):
                have = ",".join(v for v, _ in cands) or "?"
                problems.append((pkgname, dep, f"version unmet (have {have})"))

    print(f"arch={arch} version={ver}")
    print(f"manifest packages: {len(manifest_files)}"
          f"   DEPENDS edges checked: {checked}")
    if stale:
        print(f"\nSTALE FILE_NAMES (in manifest, gone from live tree): "
              f"{len(stale)}")
        for s in stale:
            print(f"  - {s}")
    if problems:
        print(f"\nUNSATISFIED DEPENDENCIES: {len(problems)}")
        for pkg, dep, why in sorted(set(problems)):
            print(f"  - {pkg}: {dep}  ->  {why}")
        print("\nfix: regenerate -> python3 bin/needs.py | "
              "python3 bin/closure.py --arch <arch> --out manifest/...")
        sys.exit(3)
    print("\nOK: every mirrored package's DEPENDS resolves inside the mirror.")
    if stale:
        sys.exit(3)


if __name__ == "__main__":
    main()
