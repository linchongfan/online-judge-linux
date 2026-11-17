#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run as root on an authorized disposable Ubuntu 22.04/24.04 judge host." >&2
    exit 1
fi
if [[ ${1:-} == "--yes" ]]; then
    shift
else
    echo "This mutates the current VM, installs pinned packages and writes /opt/judge-toolchains." >&2
    echo "Run only on an authorized disposable judge VM: $0 --yes" >&2
    exit 1
fi

source /etc/os-release
if [[ ${ID:-} != ubuntu ]]; then
    echo "Supported baseline: Ubuntu only." >&2
    exit 1
fi
case ${VERSION_ID:-} in
    24.04)
        # Noble ships Python 3.12 as the system interpreter.
        PYTHON_PKGS=(python3 python3-venv python3-minimal)
        PYTHON_BIN=python3
        ;;
    22.04)
        # Jammy ships Python 3.10; the production baseline is 3.12, so install
        # it explicitly from deadsnakes instead of silently judging with 3.10.
        PYTHON_PKGS=(python3.12 python3.12-venv)
        PYTHON_BIN=python3.12
        if ! apt-cache show python3.12 >/dev/null 2>&1; then
            apt-get install -y software-properties-common
            add-apt-repository -y ppa:deadsnakes/ppa
            apt-get update
        fi
        ;;
    *)
        echo "Supported baseline: Ubuntu 22.04/24.04 LTS only (got ${PRETTY_NAME:-unknown})." >&2
        exit 1
        ;;
esac
if [[ ! -f /sys/fs/cgroup/cgroup.controllers ]]; then
    echo "cgroup v2 unified hierarchy is required." >&2
    exit 1
fi
for controller in cpu memory pids io; do
    if ! grep -qw "$controller" /sys/fs/cgroup/cgroup.controllers; then
        echo "Missing cgroup v2 controller: $controller" >&2
        exit 1
    fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl gcc-13 g++-13 git gzip \
    libcap-dev libseccomp-dev locales openjdk-17-jdk-headless pkg-config \
    "${PYTHON_PKGS[@]}" rsync sudo tar ufw util-linux

# The judge host executes untrusted code: default-deny all inbound traffic
# except SSH (allowed before enabling to avoid remote lockout).
ufw allow OpenSSH
ufw default deny incoming
ufw default allow outgoing
ufw --force enable

# Build the pinned IOI isolate from source. This deliberately avoids a floating
# distro package. isolate becomes the setuid-root trust anchor of the whole
# sandbox, so the tarball integrity is verified against a pinned checksum
# before anything is compiled or installed.
ISOLATE_VERSION="2.0"
ISOLATE_SHA256="17364f1b0f5d3d59fc917e1520f9a16d2b85bcb175b2615ab0dbfce09ab893f1"
ISOLATE_SOURCE_DIR="$(mktemp -d)"
trap 'rm -rf "$ISOLATE_SOURCE_DIR"' EXIT
curl -fsSL "https://github.com/ioi/isolate/archive/refs/tags/v${ISOLATE_VERSION}.tar.gz" \
    -o "$ISOLATE_SOURCE_DIR/isolate.tar.gz"
echo "${ISOLATE_SHA256}  $ISOLATE_SOURCE_DIR/isolate.tar.gz" | sha256sum -c -
tar -xzf "$ISOLATE_SOURCE_DIR/isolate.tar.gz" -C "$ISOLATE_SOURCE_DIR"
make -C "$ISOLATE_SOURCE_DIR/isolate-${ISOLATE_VERSION}" isolate
install -m 4755 -o root -g root "$ISOLATE_SOURCE_DIR/isolate-${ISOLATE_VERSION}/isolate" /usr/bin/isolate
/usr/bin/isolate --version

# isolate v2 refuses to run without an explicit configuration. Prepare the
# box/lock roots and a delegated cgroup v2 subtree for sandbox accounting.
install -d -m 755 -o root -g root /var/local/lib/isolate /var/lib/xinbaoyan-judge/locks
install -d -m 755 -o root -g root /sys/fs/cgroup/isolate
for controller in cpu memory pids io; do
    echo "+$controller" > /sys/fs/cgroup/cgroup.subtree_control 2>/dev/null || true
    echo "+$controller" > /sys/fs/cgroup/isolate/cgroup.subtree_control 2>/dev/null || true
done
cat > /usr/local/etc/isolate <<'EOF'
box_root = /var/local/lib/isolate
lock_root = /var/lib/xinbaoyan-judge/locks
cg_root = /sys/fs/cgroup/isolate
first_uid = 60000
first_gid = 60000
num_boxes = 1000
EOF
for controller in cpu memory pids io; do
    if ! grep -qw "$controller" /sys/fs/cgroup/isolate/cgroup.controllers; then
        echo "WARNING: cgroup controller '$controller' not delegated to /sys/fs/cgroup/isolate; isolate --cg will fail." >&2
    fi
done

install -d -m 755 -o root -g root /opt/judge-toolchains /opt/judge-toolchains/gcc13 /opt/judge-toolchains/jdk17 /opt/judge-toolchains/python312
ln -sfn /usr/bin/gcc-13 /opt/judge-toolchains/gcc13/bin-gcc-link-target
mkdir -p /opt/judge-toolchains/gcc13/bin
for tool in gcc g++ gcov cpp; do
    ln -sfn "/usr/bin/${tool}-13" "/opt/judge-toolchains/gcc13/bin/${tool}"
done
mkdir -p /opt/judge-toolchains/jdk17/bin /opt/judge-toolchains/python312/bin
for tool in javac java jar; do
    ln -sfn "/usr/bin/${tool}" "/opt/judge-toolchains/jdk17/bin/${tool}"
done
# Point at the resolved real binary (e.g. /usr/bin/python3.12), not the
# /usr/bin/python3 symlink: inside the sandbox rootfs /usr/bin/python3 links
# back into the toolchain, and a symlink-to-symlink cycle would fail with
# ELOOP. build-rootfs.sh copies this exact binary into the rootfs.
PYTHON_REAL="$(readlink -f "$(command -v "$PYTHON_BIN")")"
case "$PYTHON_REAL" in
    */python3.12) ;;
    *) echo "Expected a Python 3.12 interpreter, got: $PYTHON_REAL" >&2; exit 1 ;;
esac
ln -sfn "$PYTHON_REAL" /opt/judge-toolchains/python312/bin/python3

python3 - <<'PY'
import hashlib
import os
from pathlib import Path

root = Path("/opt/judge-toolchains")
entries = []
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        digest = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        kind = "symlink"
    elif path.is_file():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        kind = "file"
    else:
        continue
    entries.append(f"{digest}  {kind}:{path}")
manifest = root / "SHA256SUMS"
manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
manifest.chmod(0o444)
PY

cat <<'EOF'
Toolchain bootstrap complete.
- GCC 13 via /opt/judge-toolchains/gcc13/bin
- OpenJDK 17 via /opt/judge-toolchains/jdk17/bin
- Python 3.12 via /opt/judge-toolchains/python312/bin
Next: run build-rootfs.sh and then run-acceptance.sh on this VM.
EOF
