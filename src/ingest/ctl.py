"""ingestctl: command line control of the daemon (tests over SSH, scripts)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

from ingest.common.client import Client, DaemonError


def human(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000
    return str(n)


def eta(sec) -> str:
    if sec is None:
        return "-"
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else \
        f"{sec // 60}:{sec % 60:02d}"


def print_state(st: dict) -> None:
    sh = st["share"]
    print(f"Ingest Station {st['version']}  slot {st['slot']}  IP {st['ip'] or '-'}")
    where = f"{sh['type']}://{sh['server']}{'' if sh['path'].startswith('/') else '/'}{sh['path']}"
    extra = f"  free {human(sh.get('free'))}" if sh.get("state") == "online" else \
        f"  ({sh.get('reason', '')})"
    print(f"Share: {sh['state']}  {where if sh['server'] else ''}"
          f"{'/' + sh['subdir'] if sh.get('subdir') else ''}{extra}")
    if not st["sources"]:
        print("Sources: none")
    for s in st["sources"]:
        line = f"  [{s['id']}] {s['name']}  {s['status']}"
        if s["label"] or s["fs"]:
            line += f"  '{s['label']}' {s['fs']} {human(s['capacity'])}"
        if s.get("countdown"):
            line += f"  auto-import in {s['countdown']} s"
        if s["message"]:
            line += f"  - {s['message']}"
        print(line)
        sm = s.get("summary")
        if sm and s["status"] in ("ready", "countdown", "queued"):
            print(f"      {sm['files']} files {human(sm['bytes'])}: {sm['new_files']} new "
                  f"({human(sm['new_bytes'])}), {sm['known_files']} already imported")
            for p in sm["previous"]:
                when = dt.datetime.fromtimestamp(p["date"]).strftime("%Y-%m-%d %H:%M")
                print(f"        {p['files']} files imported {when} -> {p['folder']}")
        if s.get("result"):
            r = s["result"]
            print(f"      last import: {r['status']} {r['files']} files {human(r['bytes'])} "
                  f"in {r['seconds']} s ({r['mbps']} MB/s) -> {r['folder']} {r['error']}")
    a = st.get("active")
    if a:
        print_progress(a)
    if st.get("decision"):
        print(f"  PAUSED: {st['decision']['error']}  (ingestctl retry | ingestctl giveup)")
    r = st.get("last_result")
    if r:
        print(f"Last result: {r['status'].upper()}  {r['name']}: {r['files']} files "
              f"{human(r['bytes'])} -> {r['folder']}{'  ' + r['error'] if r['error'] else ''}"
              f"  (ingestctl dismiss)")


def print_progress(a: dict, end: str = "\n") -> None:
    if "bytes_total" not in a:
        print(f"  import {a.get('phase', '')}...", end=end)
        return
    print(f"  {a['phase']:6} {a['percent']:5.1f}%  {a['files_done']}/{a['files_total']} files  "
          f"{human(a['bytes_done'])}/{human(a['bytes_total'])}  "
          f"{human(a['speed_now'])}/s (avg {human(a['speed_avg'])}/s)  ETA {eta(a['eta'])}  "
          f"{a.get('current_file', '')[-40:]}", end=end)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingestctl", description="Control the Ingest Station daemon")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("watch", help="follow the state live")
    i = sub.add_parser("import")
    i.add_argument("source", nargs="?", default="")
    i.add_argument("--all", action="store_true", help="copy everything, even known files")
    i.add_argument("--verify", choices=["none", "hash", "full"])
    for name in ("cancel", "eject", "rescan"):
        sub.add_parser(name).add_argument("source", nargs="?", default="")
    sub.add_parser("retry", help="retry after a pause")
    sub.add_parser("giveup", help="cancel a paused import")
    sub.add_parser("dismiss", help="clear the last result")
    sh = sub.add_parser("share", help="destination settings")
    ssub = sh.add_subparsers(dest="action", required=True)
    s_set = ssub.add_parser("set")
    s_set.add_argument("--type", choices=["nfs", "smb"])
    s_set.add_argument("--server")
    s_set.add_argument("--path", help="NFS export or SMB share name")
    s_set.add_argument("--subdir", help="destination folder inside the share")
    s_set.add_argument("--user")
    s_set.add_argument("--password")
    ssub.add_parser("test")
    s_ls = ssub.add_parser("ls")
    s_ls.add_argument("path", nargs="?", default="")
    s_mk = ssub.add_parser("mkdir")
    s_mk.add_argument("path")
    st = sub.add_parser("settings", help="show or change settings: key=value (JSON values)")
    st.add_argument("pairs", nargs="*")
    sub.add_parser("rebuild-index", help="rebuild the index from the manifests on the share")
    h = sub.add_parser("history")
    h.add_argument("query", nargs="?", default="")
    b = sub.add_parser("beep")
    b.add_argument("pattern", nargs="?", default="ok", choices=["start", "ok", "error"])
    b.add_argument("--backend", choices=["none", "pcspkr"])
    r = sub.add_parser("raw")
    r.add_argument("command")
    r.add_argument("json", nargs="?", default="{}")
    a = p.parse_args(argv)

    try:
        with Client() as c:
            if a.cmd == "status":
                print_state(c.call("state"))
            elif a.cmd == "watch":
                for state in c.events():
                    print("\033[2J\033[H", end="")
                    print_state(state)
            elif a.cmd == "import":
                args = {"source": a.source, "mode": "all" if a.all else "new"}
                if a.verify:
                    args["verify"] = a.verify
                print(c.call("import", **args))
            elif a.cmd in ("cancel", "eject", "rescan"):
                c.call(a.cmd, source=a.source)
            elif a.cmd == "retry":
                c.call("retry")
            elif a.cmd == "giveup":
                c.call("decide", choice="cancel")
            elif a.cmd == "dismiss":
                c.call("dismiss")
            elif a.cmd == "share":
                if a.action == "set":
                    changes = {k: v for k, v in {"type": a.type, "server": a.server,
                                                 "path": a.path, "subdir": a.subdir,
                                                 "username": a.user}.items() if v is not None}
                    args = {"changes": {"share": changes}}
                    if a.password is not None:
                        args["smb_password"] = a.password
                    print(json.dumps(c.call("settings.set", **args)["share"], indent=2))
                    time.sleep(0.5)
                    print_state(c.call("state"))
                elif a.action == "test":
                    print(json.dumps(c.call("share.test"), indent=2))
                elif a.action == "ls":
                    for d in c.call("share.list", path=a.path):
                        print(d)
                elif a.action == "mkdir":
                    c.call("share.mkdir", path=a.path)
            elif a.cmd == "settings":
                if not a.pairs:
                    print(json.dumps(c.call("settings.get"), indent=2))
                else:
                    changes: dict = {}
                    for pair in a.pairs:
                        key, _, value = pair.partition("=")
                        try:
                            val = json.loads(value)
                        except ValueError:
                            val = value
                        node = changes
                        parts = key.split(".")
                        for k in parts[:-1]:
                            node = node.setdefault(k, {})
                        node[parts[-1]] = val
                    print(json.dumps(c.call("settings.set", changes=changes), indent=2))
            elif a.cmd == "rebuild-index":
                print(json.dumps(c.call("index.rebuild"), indent=2))
            elif a.cmd == "history":
                for imp in c.call("history", query=a.query):
                    when = dt.datetime.fromtimestamp(imp["started"]).strftime("%Y-%m-%d %H:%M")
                    print(f"{when}  {imp['status']:11} {imp['files_done']:>5} files "
                          f"{human(imp['bytes_done']):>10}  {imp['dest_folder']}")
            elif a.cmd == "beep":
                args = {"pattern": a.pattern}
                if a.backend:
                    args["backend"] = a.backend
                print(c.call("beep", **args))
            elif a.cmd == "raw":
                print(json.dumps(c.call(a.command, **json.loads(a.json)), indent=2, default=str))
    except DaemonError as e:
        print(f"ingestctl: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
