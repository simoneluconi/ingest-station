from ingest.common import efiboot

OUTPUT = """BootCurrent: 0002
Timeout: 0 seconds
BootOrder: 0003,0002,0000
Boot0000* UiApp	FvVol(7cb8bdc9-f8eb-4f34-aaea-3ee4af6516a1)/FvFile(462caa21-7614-4503-836e-8ab6f4662331)
Boot0002* UEFI: Kingston DataTraveler 3.0	PciRoot(0x0)/Pci(0x14,0x0)/USB(3,0)
Boot0003* Ingest Station	HD(1,GPT,903fc9f3-a70e-4851-b19e-eaacf453dca3,0x800,0x300000)/\\EFI\\BOOT\\BOOTX64.EFI
Boot000a  Ingest Station	HD(1,GPT,11111111-a70e-4851-b19e-eaacf453dca3,0x800,0x300000)/\\EFI\\BOOT\\BOOTX64.EFI
Boot0004* Ingest Station old	HD(1,GPT,...)
"""


def test_entries_with_label():
    assert efiboot.entries_with_label(OUTPUT) == ["0003", "000A"]
    assert efiboot.entries_with_label("") == []


def test_create_command():
    cmd = efiboot.create_command("mmcblk0")
    assert cmd[:2] == ["efibootmgr", "--create"]
    assert cmd[cmd.index("--disk") + 1] == "/dev/mmcblk0"
    assert cmd[cmd.index("--loader") + 1] == "\\EFI\\BOOT\\BOOTX64.EFI"
