#!/bin/sh
# Clean-install test for one Linux artifact. Run as root in a fresh container:
#   sh packaging/linux/install_test.sh <file.deb|file.rpm|file.AppImage>
# Stops at the first failed step. It never enables or changes a firewall.
set -eu

here=$(cd "$(dirname "$0")" && pwd)

# The host libraries the bundle needs are the package dependencies; the
# AppImage needs the same set, installed by hand.
depends() {
    sed -n "/^  $1:\$/,/^  [a-z]*:\$/s/^      - \([^ ]*\).*/\1/p" "$here/nfpm.yaml"
}
APPIMAGE_BASE_DEB=$(depends deb)
APPIMAGE_BASE_RPM=$(depends rpm)

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

step() {
    echo "== $*"
}

[ $# -eq 1 ] || fail "usage: $0 <artifact>"
[ -f "$1" ] || fail "no such artifact: $1"
[ "$(id -u)" -eq 0 ] || fail "run as root"
[ -n "$APPIMAGE_BASE_DEB" ] && [ -n "$APPIMAGE_BASE_RPM" ] || fail "no dependencies in nfpm.yaml"
artifact=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")

if command -v apt-get >/dev/null 2>&1; then
    pm=apt
elif command -v dnf >/dev/null 2>&1; then
    pm=dnf
elif command -v zypper >/dev/null 2>&1; then
    pm=zypper
else
    fail "needs apt-get, dnf or zypper"
fi

case $artifact in
    *.deb) kind=deb; [ $pm = apt ] || fail "a .deb needs apt-get" ;;
    *.rpm) kind=rpm; [ $pm != apt ] || fail "an .rpm needs dnf or zypper" ;;
    *.AppImage) kind=appimage ;;
    *) fail "unknown artifact type: $artifact" ;;
esac

# Recommended and weak dependencies stay out, so only the declared ones count.
pkg_install() {
    case $pm in
        apt) DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@" ;;
        dnf) dnf install -y -q --setopt=install_weak_deps=False "$@" ;;
        zypper) zypper --non-interactive --quiet install --no-recommends "$@" ;;
    esac
}

if [ $pm = apt ]; then
    apt-get update -q
fi
command -v setpriv >/dev/null 2>&1 || pkg_install util-linux
command -v find >/dev/null 2>&1 || pkg_install findutils

step "1: install the $kind"
case $kind in
    deb)
        pkg_install "$artifact"
        app=/usr/bin/sdc-testing-toolbox
        ;;
    rpm)
        if [ $pm = dnf ]; then
            pkg_install "$artifact"
        else
            zypper --non-interactive --quiet install --no-recommends --allow-unsigned-rpm "$artifact"
        fi
        app=/usr/bin/sdc-testing-toolbox
        ;;
    appimage)
        if [ $pm = apt ]; then
            pkg_install $APPIMAGE_BASE_DEB
        else
            pkg_install $APPIMAGE_BASE_RPM
        fi
        mkdir -p "/tmp/with space"
        app="/tmp/with space/$(basename "$artifact")"
        cp "$artifact" "$app"
        chmod 755 "$app"
        export APPIMAGE_EXTRACT_AND_RUN=1
        ;;
esac
[ -x "$app" ] || fail "$app is not executable"

# Everything runs as an unprivileged user from a writable directory.
work=$(mktemp -d)
mkdir -m 700 "$work/runtime"
chown -R 65534:65534 "$work"
as_user() {
    (cd "$work" && setpriv --reuid=65534 --regid=65534 --clear-groups \
        env HOME="$work" XDG_RUNTIME_DIR="$work/runtime" "$@")
}

smoke() {
    rm -f "$work/smoke-test-error.txt"
    if ! as_user "$@" "$app" --name packaged-smoke --ip 127.0.0.1 --smoke-test >"$work/smoke.log" 2>&1; then
        tail -n 40 "$work/smoke.log" >&2
        [ ! -f "$work/smoke-test-error.txt" ] || cat "$work/smoke-test-error.txt" >&2
        fail "smoke test failed: $*"
    fi
}

step "2: offscreen smoke test as an unprivileged user"
smoke QT_QPA_PLATFORM=offscreen

if [ $kind != appimage ]; then
    step "3: every bundled file finds its libraries"
    missing=$(find /opt/sdc-testing-toolbox -type f \
        -exec env LD_LIBRARY_PATH=/opt/sdc-testing-toolbox ldd {} + 2>/dev/null |
        grep 'not found' | sort -u || true)
    [ -z "$missing" ] || fail "unresolved libraries: $missing"
fi

step "4: X11 smoke test with the GTK theme"
case $pm in
    apt) pkg_install xvfb xauth ;;
    zypper) pkg_install xvfb-run ;;
    dnf) pkg_install xorg-x11-server-Xvfb xorg-x11-xauth || pkg_install xwayland-run ;;
esac
# Some xvfb-run defaults use an 8-bit screen, which Qt cannot paint on, and
# xwfb-run drops the child's stderr, so the child merges stderr into stdout.
headless="$work/headless"
cat >"$headless" <<'EOF'
#!/bin/sh
if command -v xvfb-run >/dev/null 2>&1; then
    exec xvfb-run -a -s "-screen 0 1280x1024x24" sh -c 'exec "$@" 2>&1' sh "$@"
fi
exec xwfb-run -- sh -c 'exec "$@" 2>&1' sh "$@"
EOF
chmod 755 "$headless"
smoke QT_QPA_PLATFORM=xcb QT_QPA_PLATFORMTHEME=gtk3 QT_DEBUG_PLUGINS=1 "$headless"
grep 'libqgtk3\.so.*loaded library' "$work/smoke.log" || {
    grep -i gtk "$work/smoke.log" | tail -n 20 >&2
    fail "the GTK theme plugin did not load"
}

step "5: a launch without arguments waits in the startup dialog"
rc=0
as_user QT_QPA_PLATFORM=xcb QT_QPA_PLATFORMTHEME=gtk3 "$headless" timeout 15 "$app" \
    >"$work/launch.log" 2>&1 || rc=$?
if [ $rc -ne 124 ]; then
    tail -n 40 "$work/launch.log" >&2
    fail "exit code $rc, expected 124 from timeout"
fi

if [ $kind = appimage ]; then
    echo "PASS: $kind"
    exit 0
fi

step "6: firewall definitions are installed but not enabled"
if [ $kind = deb ]; then
    pkg_install ufw
    ufw app info SDC-Testing-Toolbox >"$work/firewall.txt"
    ports="3702/udp 21073:21172/tcp 21073:21172/udp"
    grep -qx 'ENABLED=no' /etc/ufw/ufw.conf || fail "ufw is enabled"
    for script in preinst postinst prerm postrm; do
        [ ! -e "/var/lib/dpkg/info/sdc-testing-toolbox.$script" ] || fail "package has a $script script"
    done
else
    pkg_install firewalld
    firewall-offline-cmd --info-service=sdc-testing-toolbox >"$work/firewall.txt"
    ports="3702/udp 21073-21172/tcp 21073-21172/udp"
    if firewall-cmd --state >/dev/null 2>&1; then
        fail "firewalld is running"
    fi
    [ -z "$(rpm -q --scripts sdc-testing-toolbox)" ] || fail "package has scriptlets"
fi
for port in $ports; do
    grep -q "$port" "$work/firewall.txt" || fail "firewall definition lacks $port"
done

step "7: uninstall"
case $pm in
    apt) DEBIAN_FRONTEND=noninteractive apt-get purge -y -q sdc-testing-toolbox ;;
    dnf) dnf remove -y -q sdc-testing-toolbox ;;
    zypper) zypper --non-interactive --quiet remove sdc-testing-toolbox ;;
esac
for path in /opt/sdc-testing-toolbox /usr/bin/sdc-testing-toolbox; do
    if [ -e "$path" ] || [ -L "$path" ]; then
        fail "$path is still there"
    fi
done

echo "PASS: $kind"
