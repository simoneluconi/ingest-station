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

# -- boot cache ---------------------------------------------------------------
# Installing the packages into the RAM root takes ~4 s at every boot. The
# first boot of a system version saves the freshly installed root (before
# anything ran in it) as an uncompressed tar on the data partition; later
# boots extract it instead (~1.3 s). The key covers what the root is made
# of: the apkovl, the slot's package indexes and the kernel. Installer and
# rescue boots never use it. Any problem means a normal package install.

ingest_cache_mnt=/ingest/data
ingest_data=
for _o in $(cat /proc/cmdline); do
	case "$_o" in
	ingest_data=*) ingest_data=${_o#*=} ;;
	esac
done

ingest_cache_usable() {
	[ -n "$ingest_slot_ok" ] && [ -n "$ingest_data" ] || return 1
	case " $(cat /proc/cmdline) " in *" ingest_mode="*) return 1 ;; esac
	return 0
}

# OpenRC reads every script in /etc/init.d at boot to build its dependency
# tree (~10 ms each, ~75 installed by the packages): the ones the station
# never uses are moved away, after the package install and before the cache
# is saved. The list comes from the build (/ingest/keep-services).
ingest_prune_services() {
	[ -s /ingest/keep-services ] || return 0
	_keep=" $(tr '\n' ' ' < /ingest/keep-services) "
	_away="$sysroot/usr/share/ingest/unused-init.d"
	mkdir -p "$_away"
	for _f in "$sysroot"/etc/init.d/*; do
		case "$_keep" in
		*" ${_f##*/} "*) ;;
		*) mv "$_f" "$_away"/ ;;
		esac
	done
}

ingest_cache_key() {
	{
		sha1sum "$KOPT_apkovl"
		cat /ingest/keep-services 2>/dev/null | sha1sum
		for _r in $(echo "$ALPINE_REPO" | tr ',' ' '); do
			cat "$_r"/*/APKINDEX.tar.gz 2>/dev/null | sha1sum
		done
		uname -r
	} | sha1sum | cut -c1-16
}

# mount the data partition (ro or rw) on $ingest_cache_mnt
ingest_cache_mount() {
	_dev=$(findfs "${ingest_data}" 2>/dev/null) || return 1
	mkdir -p "$ingest_cache_mnt"
	mount -t ext4 -o "$1" "$_dev" "$ingest_cache_mnt" 2>/dev/null
}

# extract the cached root into $sysroot; fails when there is none
ingest_cache_restore() {
	ingest_cache_usable || return 1
	ingest_cache_file="rootfs-$ingest_slot-$(ingest_cache_key).tar"
	ingest_cache_mount ro || return 1
	_ok=1
	if [ -f "$ingest_cache_mnt/cache/$ingest_cache_file" ]; then
		tar -xf "$ingest_cache_mnt/cache/$ingest_cache_file" -C "$sysroot" && _ok=0
	fi
	umount "$ingest_cache_mnt"
	[ $_ok = 0 ] && ingest_log "system restored from the boot cache"
	return $_ok
}

# save $sysroot (packages just installed) for the next boots of this version
ingest_cache_save() {
	ingest_cache_usable || return 0
	[ -n "$ingest_cache_file" ] || ingest_cache_file="rootfs-$ingest_slot-$(ingest_cache_key).tar"
	ingest_cache_mount rw || return 0
	_dir="$ingest_cache_mnt/cache"
	mkdir -p "$_dir"
	# older caches of this slot (the other slot keeps its own, for rollbacks)
	rm -f "$_dir"/rootfs-"$ingest_slot"-*.tar "$_dir"/.saving
	if (cd "$sysroot" && tar -cf "$_dir/.saving" .) && sync; then
		mv "$_dir/.saving" "$_dir/$ingest_cache_file" && sync
		ingest_log "boot cache saved ($ingest_cache_file)"
	else
		rm -f "$_dir/.saving"
	fi
	umount "$ingest_cache_mnt"
	return 0
}

ingest_bg_pid=${ingest_bg_pid:-}
if [ -n "$ingest_slot" ]; then
	ingest_arm_watchdog
	# slow drivers (i915) in the background, while the boot media is found
	# and the root is installed (the FAT charset module is loaded before:
	# mounting must not wait for a module behind i915)
	if [ -z "$ingest_bg_pid" ] && [ -s /ingest/background-modules ]; then
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
