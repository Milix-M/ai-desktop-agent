#!/bin/bash
# Read the boot files from this instance's own ext4 disk, without mounting it.
set -euo pipefail

image=$(realpath -e -- "$1")
boot_dir="${image}.boot"
work=$(mktemp -d "${boot_dir}.tmp.XXXXXXXX")
trap 'rm -rf --one-file-system -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$work/next"
cd "$work/next"

# Never bypass QEMU's image locks. The source may contain saved user work.
qemu-img convert -f qcow2 -O raw -S 4k "$image" disk.raw
status=0
e2fsck -p disk.raw || status=$?
if [ "$status" -gt 1 ]; then
    echo "ERROR: VM filesystem needs recovery; original disk was not changed: $image" >&2
    exit 1
fi
debugfs -R 'ls -p /lib/modules' disk.raw > modules.txt
versions=$(awk -F/ '$3 ~ /^04/ && $6 ~ /^[[:alnum:]][[:alnum:]._+-]*$/ { print $6 }' modules.txt | sort -rV)
selected=""
while IFS= read -r version; do
    [ -n "$version" ] || continue
    rm -f vmlinuz initrd.img
    debugfs -R "dump /boot/vmlinuz-$version vmlinuz" disk.raw
    debugfs -R "dump /boot/initrd.img-$version initrd.img" disk.raw
    if [ -s vmlinuz ] && [ -s initrd.img ]; then
        selected="$version"
        break
    fi
done <<< "$versions"
if [ -z "$selected" ]; then
    echo "ERROR: no kernel and initrd matching /lib/modules in $image" >&2
    exit 1
fi
debugfs -R 'dump /etc/ai-desktop-agent/cmdline cmdline.txt' disk.raw
if [ ! -s cmdline.txt ]; then
    # Images produced before cmdline was stored in the guest use this root layout.
    echo 'console=tty0 console=ttyS0 root=/dev/vda rw' > cmdline.txt
fi
printf '%s\n' "$selected" > version
rm -f disk.raw modules.txt
cd "$work"
if [ -e "$boot_dir" ] || [ -L "$boot_dir" ]; then
    mv -T -- "$boot_dir" "$work/previous"
fi
mv -T -- "$work/next" "$boot_dir"
echo "Prepared VM boot files from $image (kernel $selected)"
