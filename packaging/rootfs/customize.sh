#!/bin/sh
# Called only by mmdebstrap inside the isolated build namespace.
set -eu
root=$1
test -d "$root/etc"
mkdir -p "$root/usr/share/maa-build"
chroot "$root" dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\n' \
    > "$root/usr/share/maa-build/packages.unsorted.tsv"
LC_ALL=C sort "$root/usr/share/maa-build/packages.unsorted.tsv" \
    > "$root/usr/share/maa-build/packages.tsv"
chroot "$root" python3 -c 'import sys, tomllib; assert sys.version_info >= (3, 11)'
# Never export the builder hostname, DNS configuration, or a generated host ID.
printf 'maa-runtime\n' > "$root/etc/hostname"
printf '127.0.0.1 localhost maa-runtime\n::1 localhost ip6-localhost\n' > "$root/etc/hosts"
: > "$root/etc/resolv.conf"
: > "$root/etc/machine-id"
printf 'en_US.UTF-8 UTF-8\n' > "$root/etc/locale.gen"
chroot "$root" locale-gen
# DNS, timezone and Android mounts are runtime-driver responsibilities.
# No game launch, ADB connection, cron start, SSH server or device helpers here.
