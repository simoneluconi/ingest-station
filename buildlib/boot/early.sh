# Ingest Station initramfs hook. Sourced by Alpine's /init (busybox ash)
# right after the boot media scan; build.py patches /init to do so.
#
# 1. Arms the hardware watchdog (iTCO) so a kernel/initramfs that cannot
#    finish booting gets reset, letting GRUB count the attempt and roll back.
# 2. Finds the boot partition by ingest_media=UUID=..., mounts it read-only
#    under /media/<dev> and points the apkovl and the package repositories at
#    /slots/<ingest_slot>/ on it. Slots are deeper than Alpine's automatic
#    .boot_repository search, so the other slot is never picked up.
#
# 3. Starts loading the slow drivers listed in /ingest/background-modules
#    (i915) in the background, while /init installs the packages; the
#    patched /init waits for $ingest_bg_pid before switch_root.
#
# Sets ingest_slot_ok=1 when the slot was found; /init then treats the boot
# media scan as successful. Without ingest_slot= on the command line this
# file does nothing.

ingest_slot_ok=
ingest_slot=
ingest_media=
ingest_wdt=0
for _o in $(cat /proc/cmdline); do
	case "$_o" in
	ingest_slot=*) ingest_slot=${_o#*=} ;;
	ingest_media=*) ingest_media=${_o#*=} ;;
	ingest_wdt=*) ingest_wdt=${_o#*=} ;;
	esac
done

ingest_log() {
	echo "ingest: $*" > /dev/kmsg
	[ "$KOPT_quiet" = yes ] || echo "ingest: $*"
}

ingest_arm_watchdog() {
	[ "$ingest_wdt" -gt 0 ] 2>/dev/null || return 0
	# timeouts come from the command line (wdat_wdt.timeout=, iTCO_wdt.heartbeat=)
	modprobe -q wdat_wdt
	modprobe -q lpc_ich
	modprobe -q iTCO_wdt
	_t=$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null)
	if [ -z "$_t" ]; then
		ingest_log "no hardware watchdog"
		return 0
	fi
	# drivers silently fall back to their default (e.g. 30 s) on an
	# out-of-range value: that would reset us during the package install
	if [ "$_t" != "$ingest_wdt" ]; then
		ingest_log "watchdog timeout is '$_t', not arming"
		return 0
	fi
	# any byte but 'V' starts the timer; closing without the magic
	# character leaves it running until the ingest-watchdog service takes over
	[ -c /dev/watchdog ] && printf 1 > /dev/watchdog &&
		ingest_log "watchdog armed (${_t}s, $(cat /sys/class/watchdog/watchdog0/identity))"
}

ingest_find_media() {
	# GRUB prints FAT serials in lower case, Linux tools in upper case
	_raw=${ingest_media#UUID=}
	_want=$(echo "$_raw" | tr 'a-z' 'A-Z')
	_low=$(echo "$_raw" | tr 'A-Z' 'a-z')
	_i=0
	while [ $_i -lt 50 ]; do
		_dev=$(findfs "UUID=$_want" 2>/dev/null || findfs "UUID=$_low" 2>/dev/null)
		[ -n "$_dev" ] && { echo "$_dev"; return 0; }
		_dev=$(blkid 2>/dev/null | while IFS= read -r _l; do
			case "$(echo "$_l" | tr 'a-z' 'A-Z')" in
			*" UUID=\"$_want\""*) echo "${_l%%:*}"; break ;;
			esac
		done)
		[ -n "$_dev" ] && { echo "$_dev"; return 0; }
		sleep 0.2
		_i=$((_i + 1))
	done
	return 1
}

ingest_bg_pid=
if [ -n "$ingest_slot" ]; then
	ingest_arm_watchdog
	if [ -s /ingest/background-modules ]; then
		( for _m in $(cat /ingest/background-modules); do modprobe -q "$_m"; done ) &
		ingest_bg_pid=$!
	fi
	if _dev=$(ingest_find_media); then
		_mnt=/media/${_dev##*/}
		if ! grep -q " $_mnt " /proc/mounts; then
			mkdir -p "$_mnt"
			mount -t vfat -o ro "$_dev" "$_mnt" || ingest_log "cannot mount $_dev"
		fi
		# nlplug-findfs leaves mounted every device carrying the /slots marker
		# (e.g. the installed eMMC when booting the USB installer, or the
		# stick when booting the eMMC). Unmount them: the modloop service
		# takes /slots/<X>/modloop-lts from the first mount that has it.
		for _m in $(awk '$2 ~ "^/media/" { print $2 }' /proc/mounts); do
			[ "$_m" = "$_mnt" ] && continue
			umount "$_m" 2>/dev/null && ingest_log "released $_m"
		done
		_slotdir="$_mnt/slots/$ingest_slot"
		if [ -f "$_slotdir/ingest.apkovl.tar.gz" ]; then
			KOPT_apkovl="$_slotdir/ingest.apkovl.tar.gz"
			ALPINE_REPO="$_slotdir/apks/main,$_slotdir/apks/community"
			ingest_slot_ok=1
			# nlplug-findfs listed the /slots marker here; with this file
			# present /init would move the boot media mount according to
			# its fstab (e.g. to /media/usb) and break the paths above.
			# /init regenerates it from ALPINE_REPO.
			rm -f "$repofile"
			ingest_log "booting slot $ingest_slot from $_dev"
		else
			ingest_log "slot $ingest_slot missing on $_dev"
		fi
	else
		ingest_log "boot media $ingest_media not found"
	fi
fi
