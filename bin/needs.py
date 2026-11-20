#!/usr/bin/env python3
"""needs.py - list the pkgsrc binary packages smolBSD services need.

Scans a smolBSD checkout for every package pulled at build time:
  - ADDPKGS= in service/*/options.mk
  - LABEL smolbsd.addpkgs="..." in smolerfiles/*
  - "pkgin [-y] in|install <pkgs>" in smolerfiles/* and service/*/postinst/*.sh
    (backslash continuations joined, in-file VAR=value expanded)
  - the implicit bootstrap set from service/common/pkgin

Prints one package name per line, sorted and deduplicated, on stdout.
Anything that looks like a package token but is rejected goes to stderr.

Usage: needs.py [smolBSD-repo]   (default: ../smolBSD)
"""

import argparse
import glob
import os
import re
import sys

# packages service/common/pkgin installs unconditionally at first boot
BOOTSTRAP = "ca-certificates curl pkg_install pkg_tarup pkgin rsync"

MK_ADDPKGS = re.compile(r"^ADDPKGS[ \t]*[?+]?=[ \t]*(.*)$")
VAR_ASSIGN = re.compile(
    r'^(?:(?:ARG|ENV)[ \t]+)?([A-Z][A-Z0-9_]*)(?:[ \t]*=[ \t]*|[ \t]+)'
    r"\"?([A-Za-z0-9._-]+)\"?[ \t]*$")
LABEL_ADDPKGS = re.compile(r'smolbsd\.addpkgs[ \t]*=[ \t]*"([^"]*)"')
PKGIN_IN = re.compile(r"pkgin\b[^|;&]*?[ \t](?:in|install)[ \t]+(.*)$")
PKG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~:-]*$")
VERBS = {"in", "install", "up", "ur", "ui", "ii", "update"}
OPERATORS = ("&&", "||", ";", "|")


def logical_lines(text):
    """Yield lines with backslash continuations joined."""
    buf = None
    for line in text.splitlines():
        if buf is not None:
            line = buf + " " + line
        if line.endswith("\\"):
            buf = line[:-1]
        else:
            buf = None
            yield line


def expand(tok, variables):
    for var, val in variables.items():
        tok = tok.replace("${" + var + "}", val).replace("$" + var, val)
    return tok


def cut_at_operator(args):
    for op in OPERATORS:
        pos = args.find(op)
        if pos >= 0:
            args = args[:pos]
    return args


def tokenize(args, variables, warnings, origin):
    for tok in args.replace('"', " ").split():
        tok = expand(tok, variables)
        if not tok or tok in OPERATORS or tok.startswith("-") or tok in VERBS:
            continue
        if PKG_OK.match(tok):
            yield tok
        elif "$" in tok:
            # value comes from outside this file (INCLUDE chain, --build-arg);
            # the generated service postinst carries the literal expansion
            warnings.append(
                f"needs.py: NOTE {origin}: unexpanded token: {tok}")
        else:
            warnings.append(f"needs.py: WARN {origin}: rejected token: {tok}")


def scan_shell_file(path, names, warnings, global_vars=None):
    try:
        with open(path, errors="replace") as f:
            text = f.read()
    except OSError:
        return
    variables = dict(global_vars or {})
    for line in text.splitlines():
        m = VAR_ASSIGN.match(line)
        if m:
            variables[m.group(1)] = m.group(2)
    for line in logical_lines(text):
        for m in LABEL_ADDPKGS.finditer(line):
            names.update(tokenize(m.group(1), variables, warnings, path))
        for line_part in re.split(r"&&|\|\||;", line):
            m = PKGIN_IN.search(line_part)
            if m:
                args = cut_at_operator(m.group(1))
                names.update(tokenize(args, variables, warnings, path))


def scan_mk_file(path, names, warnings):
    try:
        with open(path, errors="replace") as f:
            for line in f:
                m = MK_ADDPKGS.match(line.rstrip("\n"))
                if m:
                    names.update(tokenize(m.group(1), {}, warnings, path))
    except OSError:
        pass


def collect_global_vars(paths):
    """ARG/ENV VAR=literal pool (e.g. PYVERS in python.inc) for cross-file
    expansion; per-file definitions still take precedence. A variable defined
    with two different values is dropped (too ambiguous to guess)."""
    pool = {}
    for path in paths:
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = VAR_ASSIGN.match(line.rstrip("\n"))
                    if m:
                        var, val = m.groups()
                        if pool.get(var, val) != val:
                            pool[var] = None
                        else:
                            pool[var] = val
        except OSError:
            pass
    return {k: v for k, v in pool.items() if v is not None}


def scan_repo(repo):
    names, warnings = set(), []
    names.update(BOOTSTRAP.split())
    smolerfiles = [p for p in glob.glob(os.path.join(repo, "smolerfiles", "*"))
                   if os.path.isfile(p)]
    global_vars = collect_global_vars(smolerfiles)
    for path in glob.glob(os.path.join(repo, "service", "*", "options.mk")):
        scan_mk_file(path, names, warnings)
    for path in smolerfiles:
        scan_shell_file(path, names, warnings, global_vars)
    for path in glob.glob(os.path.join(repo, "service", "*", "postinst", "*.sh")):
        scan_shell_file(path, names, warnings, global_vars)
    return names, warnings


def load_addpkgs(paths):
    """Extra seed names from addpkgs-style files; strict on junk.

    One bare package name per line; # comments allowed. Versioned tokens
    are refused - the mirror always tracks the newest build in the tree.
    """
    names = set()
    for path in paths:
        try:
            f = open(path)
        except OSError as err:
            sys.exit(f"needs.py: cannot read --addpkgs file: {err}")
        with f:
            for lineno, line in enumerate(f, 1):
                line = line.split("#", 1)[0]
                for tok in line.split():
                    if not PKG_OK.match(tok):
                        sys.exit(f"needs.py: {path}:{lineno}: "
                                 f"invalid package name: {tok}")
                    if re.search(r"-(?=[0-9])", tok):
                        bare = re.split(r"-(?=[0-9])", tok)[0]
                        sys.exit(f"needs.py: {path}:{lineno}: no versions "
                                 f"in addpkgs: {tok} -> use {bare}")
                    names.add(tok)
    return names


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", nargs="?", default="../smolBSD",
                    help="smolBSD checkout (default: %(default)s)")
    ap.add_argument("--addpkgs", action="append", default=[], metavar="FILE",
                    help="file of extra package names merged into the seed "
                         "set (bare names, # comments; repeatable)")
    args = ap.parse_args()
    if not (os.path.isdir(os.path.join(args.repo, "service"))
            and os.path.isdir(os.path.join(args.repo, "smolerfiles"))):
        sys.exit(f"needs.py: not a smolBSD repo: {args.repo}")

    names, warnings = scan_repo(args.repo)
    names.update(load_addpkgs(args.addpkgs))
    for w in warnings:
        print(w, file=sys.stderr)
    for name in sorted(names):
        print(name)


if __name__ == "__main__":
    main()
