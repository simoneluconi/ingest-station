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
# Without ingest_slot= on the command line this file does nothing.

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
	modprobe -q lpc_ich
	modprobe -q iTCO_wdt heartbeat="$ingest_wdt" || { ingest_log "no iTCO watchdog"; return 0; }
	_t=$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null)
	# the driver silently falls back to 30 s on an out-of-range heartbeat:
	# that would reset us in the middle of the package install
	if [ "$_t" != "$ingest_wdt" ]; then
		ingest_log "watchdog timeout is '$_t', not arming"
		return 0
	fi
	# any byte but 'V' starts the timer; closing without the magic
	# character leaves it running until the ingest-watchdog service takes over
	[ -c /dev/watchdog ] && printf 1 > /dev/watchdog && ingest_log "watchdog armed (${_t}s)"
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

if [ -n "$ingest_slot" ]; then
	ingest_arm_watchdog
	if _dev=$(ingest_find_media); then
		_mnt=/media/${_dev##*/}
		if ! grep -q " $_mnt " /proc/mounts; then
			mkdir -p "$_mnt"
			mount -t vfat -o ro "$_dev" "$_mnt" || ingest_log "cannot mount $_dev"
		fi
		_slotdir="$_mnt/slots/$ingest_slot"
		if [ -f "$_slotdir/ingest.apkovl.tar.gz" ]; then
			KOPT_apkovl="$_slotdir/ingest.apkovl.tar.gz"
			ALPINE_REPO="$_slotdir/apks/main,$_slotdir/apks/community"
			ingest_log "booting slot $ingest_slot from $_dev"
		else
			ingest_log "slot $ingest_slot missing on $_dev"
		fi
	else
		ingest_log "boot media $ingest_media not found"
	fi
fi
