"""UEFI boot entries (efibootmgr). The Wyse firmware does not reliably pick
up removable or fallback boot paths by itself, so the installer registers
an explicit entry in addition to installing \\EFI\\BOOT\\BOOTX64.EFI."""

from __future__ import annotations

import re

LABEL = "Ingest Station"
LOADER = "\\EFI\\BOOT\\BOOTX64.EFI"

_ENTRY = re.compile(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*?)(?:\t|\s{2,}|$)")


def entries_with_label(efibootmgr_output: str, label: str = LABEL) -> list[str]:
    """Boot numbers (e.g. '0003') of the entries named `label`."""
    found = []
    for line in efibootmgr_output.splitlines():
        m = _ENTRY.match(line.strip())
        if m and m.group(2).strip() == label:
            found.append(m.group(1).upper())
    return found


def create_command(disk: str, partition: int = 1) -> list[str]:
    """efibootmgr command that adds the entry first in BootOrder."""
    return ["efibootmgr", "--create", "--disk", f"/dev/{disk}", "--part", str(partition),
            "--label", LABEL, "--loader", LOADER]
