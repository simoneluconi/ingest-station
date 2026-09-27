"""Build ingest-initrd.gz: a small cpio archive that GRUB loads after the
official Alpine initramfs. The kernel unpacks both into the same rootfs, so
files here add to or replace the official ones:

- /init, patched with a one-line hook that sources /ingest/early.sh
- /ingest/early.sh (slot selection + watchdog + background driver loading)
- /init also waits for the background drivers before switch_root
- extra kernel modules and the full modules.* index from the modloop
- /etc/modprobe.d/ingest-wyse.conf (dw_dmac blacklist)
"""

from __future__ import annotations

import posixpath
import stat

from . import cpio
from .squashfs import SquashFS

HOOK_ANCHOR = '\t$repoopts -a "$ROOT"/tmp/apkovls\neend $?\n'
# nlplug-findfs fails when it finds no apkovl or .boot_repository at the
# places it searches (our slots are deeper on purpose), and eend would then
# drop to the recovery shell. The hook runs before eend and the scan counts
# as successful when the hook located the slot.
HOOK_REPLACEMENT = (
    '\t$repoopts -a "$ROOT"/tmp/apkovls\n'
    "ingest_rc=$?\n"
    "# ingest-station: slot-aware boot media and early watchdog\n"
    "[ -r /ingest/early.sh ] && . /ingest/early.sh\n"
    '[ -n "$ingest_slot_ok" ] && ingest_rc=0\n'
    "eend $ingest_rc\n"
)


# the final switch_root (an indented one, for another boot mode, comes first)
SWITCH_ANCHOR = '\nexec switch_root $switch_root_opts $sysroot $chart_init "$KOPT_init" $KOPT_init_args\n'
# early.sh loads slow drivers (i915) in the background while /init installs
# the packages; they must be loaded before switch_root frees the initramfs
# holding the module files.
SWITCH_REPLACEMENT = (
    "\n# ingest-station: wait for the drivers loaded in the background\n"
    '[ -n "$ingest_bg_pid" ] && wait "$ingest_bg_pid"'
    + SWITCH_ANCHOR
)


class InitrdError(Exception):
    pass


def patch_init(init_text: str) -> str:
    for anchor in (HOOK_ANCHOR, SWITCH_ANCHOR):
        if init_text.count(anchor) != 1:
            raise InitrdError(
                "Alpine /init changed: hook anchor not found exactly once; "
                "review buildlib/initrd.py for this Alpine release")
    init_text = init_text.replace(HOOK_ANCHOR, HOOK_REPLACEMENT, 1)
    return init_text.replace(SWITCH_ANCHOR, SWITCH_REPLACEMENT, 1)


def module_closure(modules_dep: str, wanted: list[str]) -> list[str]:
    """Return relative module paths (kernel/...) for `wanted` module names
    plus all their dependencies, from a modules.dep text."""
    deps: dict[str, list[str]] = {}
    by_name: dict[str, str] = {}
    for line in modules_dep.splitlines():
        if ":" not in line:
            continue
        mod, rest = line.split(":", 1)
        deps[mod] = rest.split()
        name = posixpath.basename(mod).split(".ko")[0]
        by_name[name.replace("-", "_")] = mod
    out: list[str] = []
    stack = []
    for w in wanted:
        key = w.replace("-", "_")
        if key not in by_name:
            raise InitrdError(f"module {w} not found in modloop")
        stack.append(by_name[key])
    while stack:
        m = stack.pop()
        if m in out:
            continue
        out.append(m)
        stack.extend(deps.get(m, []))
    return sorted(out)


def build(base_entries: list[cpio.Entry], modloop: SquashFS, kver: str,
          extra_modules: list[str], early_sh: bytes,
          modprobe_conf: bytes,
          background_modules: list[str] = ()) -> tuple[bytes, list[str]]:
    """Return (gzip'd cpio, list of modules added). `background_modules` are
    added too, and listed in /ingest/background-modules for early.sh."""
    names = {e.name: e for e in base_entries}
    init = names.get("init")
    if init is None:
        raise InitrdError("official initramfs has no /init")
    patched = patch_init(init.data.decode("utf-8"))

    # the official initramfs is usr-merged (lib -> usr/lib)
    moddir = f"usr/lib/modules/{kver}"
    if f"{moddir}/modules.dep" not in names:
        raise InitrdError(f"official initramfs has no {moddir}/modules.dep")

    src = f"/modules/{kver}"
    modules_dep = modloop.read_file(modloop.lookup(f"{src}/modules.dep")).decode()

    w = cpio.Writer()
    w.file("init", patched.encode(), 0o755)
    w.file("ingest/early.sh", early_sh, 0o644)
    w.file("etc/modprobe.d/ingest-wyse.conf", modprobe_conf, 0o644)
    if background_modules:
        w.file("ingest/background-modules",
               "".join(f"{m}\n" for m in background_modules).encode(), 0o644)
        # the boot media scan (nlplug-findfs) loads the drivers of every
        # device it sees, synchronously: keep it off these, early.sh loads
        # them by name (which ignores the blacklist). Initramfs only.
        w.file("etc/modprobe.d/ingest-background.conf",
               "".join(f"blacklist {m}\n" for m in background_modules).encode(), 0o644)

    added = []
    for rel in module_closure(modules_dep, list(extra_modules) + list(background_modules)):
        path = f"{moddir}/{rel}"
        if path in names:
            continue
        w.file(path, modloop.read_file(modloop.lookup(f"{src}/{rel}")))
        added.append(rel)

    # replace the module index with the full one from the modloop so that
    # modprobe can resolve the modules we added
    for name, ref in modloop.listdir(modloop.lookup(src)).items():
        if name.startswith("modules.") and not name.endswith(".modinfo"):
            ino = modloop.inode(ref)
            if ino.is_file:
                w.file(f"{moddir}/{name}", modloop.read_file(ino))

    return w.finish_gzip(), added


def initramfs_kernel_version(entries: list[cpio.Entry]) -> str:
    kvers = {e.name.split("/")[3] for e in entries
             if e.name.startswith("usr/lib/modules/") and e.name.count("/") >= 4}
    if len(kvers) != 1:
        raise InitrdError(f"cannot determine kernel version from initramfs: {kvers}")
    return kvers.pop()


def is_regular(entry: cpio.Entry) -> bool:
    return stat.S_ISREG(entry.mode)
