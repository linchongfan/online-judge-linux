#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run as root on the judge VM." >&2
    exit 1
fi
if [[ ${1:-} == "--yes" ]]; then
    shift
else
    echo "This creates /var/lib/xinbaoyan-judge/rootfs and /var/lib/xinbaoyan-judge/work." >&2
    echo "Run only on the disposable judge VM: $0 --yes" >&2
    exit 1
fi

BASE_DIR=/var/lib/xinbaoyan-judge
ROOTFS="$BASE_DIR/rootfs"
WORK_ROOT="$BASE_DIR/work"
META_ROOT="$BASE_DIR/meta"

mkdir -p "$BASE_DIR" "$WORK_ROOT" "$META_ROOT"
# Task artifacts are relaxed for cross-UID case reuse; keep the roots
# non-world-traversable so other local users cannot read or tamper with them.
chmod 750 "$WORK_ROOT" "$META_ROOT"
if [[ -e $ROOTFS && ${1:-} != "--rebuild" ]]; then
    echo "Rootfs already exists. Use --rebuild after removing the old rootfs from the trusted manifest." >&2
    exit 1
fi
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"

copy_tree() {
    local source=$1
    local destination=$2
    mkdir -p "$destination"
    rsync -a --links --perms --times --chown=root:root "$source/" "$destination/"
}

copy_tree /usr/lib/x86_64-linux-gnu "$ROOTFS/usr/lib/x86_64-linux-gnu"
if [[ -d /usr/lib/gcc/x86_64-linux-gnu/13 ]]; then
    mkdir -p "$ROOTFS/usr/lib/gcc/x86_64-linux-gnu"
    copy_tree /usr/lib/gcc/x86_64-linux-gnu/13 "$ROOTFS/usr/lib/gcc/x86_64-linux-gnu/13"
fi
if [[ -d /usr/lib/jvm/java-17-openjdk-amd64 ]]; then
    mkdir -p "$ROOTFS/usr/lib/jvm"
    copy_tree /usr/lib/jvm/java-17-openjdk-amd64 "$ROOTFS/usr/lib/jvm/java-17-openjdk-amd64"
fi
# Copy the exact interpreter the toolchain points at (Python 3.12 baseline)
# plus its version-specific stdlib. Copying /usr/lib/python3 would ship the
# wrong stdlib on hosts where the system python3 is not 3.12.
PYTHON_REAL="$(readlink -f /opt/judge-toolchains/python312/bin/python3)"
[[ -x $PYTHON_REAL ]] || { echo "Toolchain python3 does not resolve to a binary: $PYTHON_REAL" >&2; exit 1; }
PYTHON_STDLIB="/usr/lib/${PYTHON_REAL##*/}"
[[ -d $PYTHON_STDLIB ]] || { echo "Missing Python stdlib directory: $PYTHON_STDLIB" >&2; exit 1; }
mkdir -p "$ROOTFS/usr/lib"
copy_tree "$PYTHON_STDLIB" "$ROOTFS${PYTHON_STDLIB}"
if [[ -d /usr/lib/python3/dist-packages ]]; then
    copy_tree /usr/lib/python3/dist-packages "$ROOTFS/usr/lib/python3/dist-packages"
fi
mkdir -p "$ROOTFS/lib/x86_64-linux-gnu" "$ROOTFS/lib64" "$ROOTFS/etc" "$ROOTFS/tmp" "$ROOTFS/bin" "$ROOTFS/usr/bin"
if [[ -d /lib/x86_64-linux-gnu ]]; then
    copy_tree /lib/x86_64-linux-gnu "$ROOTFS/lib/x86_64-linux-gnu"
fi
if [[ -d /lib64 ]]; then
    copy_tree /lib64 "$ROOTFS/lib64"
fi
copy_tree /etc/ld.so.conf.d "$ROOTFS/etc/ld.so.conf.d"
cp -a /etc/ld.so.conf /etc/nsswitch.conf "$ROOTFS/etc/"
# Minimal synthetic accounts: never leak the host's real user/group names
# into the sandbox where untrusted programs can read them.
cat > "$ROOTFS/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin
EOF
cat > "$ROOTFS/etc/group" <<'EOF'
root:x:0:
nogroup:x:65534:
EOF
copy_tree /opt/judge-toolchains "$ROOTFS/opt/judge-toolchains"

for tool in sh bash env cat cp mv rm mkdir rmdir ls chmod chown true false; do
    source_path="/usr/bin/$tool"
    [[ -e $source_path ]] || source_path="/bin/$tool"
    cp -a "$source_path" "$ROOTFS/usr/bin/$tool"
done
for tool in gcc g++ gcov cpp; do
    ln -sfn "/opt/judge-toolchains/gcc13/bin/$tool" "$ROOTFS/usr/bin/$tool"
done
for tool in javac java jar; do
    ln -sfn "/opt/judge-toolchains/jdk17/bin/$tool" "$ROOTFS/usr/bin/$tool"
done
# Ship the real interpreter binary and a relative symlink. The toolchain
# symlink points at /usr/bin/python3.12 (absolute), which must exist inside
# the box; linking /usr/bin/python3 back into the toolchain would create a
# symlink cycle (ELOOP).
cp -a "$PYTHON_REAL" "$ROOTFS/usr/bin/${PYTHON_REAL##*/}"
ln -sfn "${PYTHON_REAL##*/}" "$ROOTFS/usr/bin/python3"
cat > "$ROOTFS/etc/os-release" <<'EOF'
NAME="Xinbaoyan Judge Rootfs"
ID=xinbaoyan-judge
EOF

find "$ROOTFS" -type d -exec chmod 755 {} +
chmod 1777 "$ROOTFS/tmp"
python3 - <<'PY'
import hashlib
import os
from pathlib import Path
root = Path('/var/lib/xinbaoyan-judge/rootfs')
entries = []
for path in sorted(root.rglob('*')):
    if path.is_symlink():
        digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        kind = 'symlink'
    elif path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        kind = 'file'
    else:
        continue
    entries.append(f"{digest}  {kind}:{path.relative_to(root)}")
manifest = Path('/var/lib/xinbaoyan-judge/rootfs.SHA256SUMS')
manifest.write_text('\n'.join(entries) + '\n', encoding='utf-8')
manifest.chmod(0o444)
PY
chown -R root:root "$ROOTFS"
chmod -R a-w "$ROOTFS"

cat <<EOF
Rootfs ready: $ROOTFS
Manifest: $BASE_DIR/rootfs.SHA256SUMS
Work root: $WORK_ROOT
Meta root: $META_ROOT
EOF
