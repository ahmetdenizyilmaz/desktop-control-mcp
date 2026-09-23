"""Running and checking scripts from the command line.

    python -m script list
    python -m script check migros/weekly-shop
    python -m script run   migros/weekly-shop --dry
    python -m script run   migros/add-product --set product=süt
    python -m script pin   https://raw.githubusercontent.com/you/s/main/x.json

`check` is the one to reach for first: it does everything `run` does
except touch the screen — resolves the call graph, verifies every pin,
refuses cycles, and confirms every parameter is passed. A script that
passes `check` can still fail on a page that looks different from the one
it was written for, but it cannot fail because of a typo.
"""

from __future__ import annotations

import argparse
import sys

from . import Library, Runner, ScriptError, to_json
from .library import pin as pin_url


def _confirm(word: str, el) -> bool:
    print(f"\n  !! {el.name!r} looks like a commitment ({word!r}) at "
          f"{el.center}")
    try:
        return input("     go ahead? [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="script", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="every script in the library")

    for name, help_text in (("check", "resolve and validate, touching nothing"),
                            ("show", "print the parsed script")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("script")

    p = sub.add_parser("run", help="run a script")
    p.add_argument("script")
    p.add_argument("--set", action="append", default=[], metavar="K=V",
                   help="a parameter, repeatable")
    p.add_argument("--dry", action="store_true",
                   help="resolve every target and print what WOULD happen")
    p.add_argument("--fetch", action="store_true",
                   help="allow pinned remote scripts to be downloaded")
    p.add_argument("--host", action="append", default=[],
                   help="restrict --fetch to these hosts, repeatable")

    p = sub.add_parser("pin", help="print a URL's sha256, after you read it")
    p.add_argument("url")

    args = ap.parse_args(argv)

    if args.cmd == "pin":
        print(pin_url(args.url))
        return 0

    lib = Library(allow_fetch=getattr(args, "fetch", False),
                  allow_hosts=tuple(getattr(args, "host", []) or ()))

    if args.cmd == "list":
        known = lib.local()
        if not known:
            print("no scripts in", lib.roots[0])
            return 0
        width = max(len(n) for n in known)
        for name in sorted(known):
            s = known[name]
            params = ", ".join(s.declared_params())
            print(f"  {name:<{width}}  {s.trust.value:<10} "
                  f"{len(s.steps):>2} steps"
                  + (f"  ({params})" if params else ""))
        return 0

    try:
        run = lib.resolve(args.script)
    except ScriptError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "show":
        import json
        print(json.dumps(to_json(run.entry), indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "check":
        print(f"{run.entry.name}: {len(run.entry.steps)} steps, "
              f"trust {run.trust.value}")
        for name in sorted(run.scripts):
            mark = "imported" if name in run.imported else "local"
            print(f"  {name:<30} {mark:<9} {run.origins.get(name, '')}")
        print("ok — resolves, no cycles, every pin verified, "
              "every parameter passed")
        return 0

    values = {}
    for pair in args.set:
        if "=" not in pair:
            print(f"--set needs K=V, got {pair!r}", file=sys.stderr)
            return 2
        k, v = pair.split("=", 1)
        values[k] = v

    # The import is deferred so `list`, `check` and `show` work on a machine
    # with no screen, and so a typo never gets as far as starting UIA.
    from vision.elements import Watcher

    from .runner import DesktopActuator
    runner = Runner(run, Watcher(), DesktopActuator(), confirm=_confirm,
                    dry_run=args.dry,
                    on_step=lambda o: print(
                        f"  {'ok ' if o.ok else 'FAIL'} {o.do:<12} "
                        f"{o.detail[:100]}"))
    report = runner.run(values)
    print()
    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
