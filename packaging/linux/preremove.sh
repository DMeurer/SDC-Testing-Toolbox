#!/bin/sh
# Runs before uninstall (deb prerm "remove", rpm preun "0"), never on upgrade. firewalld
# rejects its whole user configuration while a zone or policy names a service whose file
# is gone, so take ours out of every zone and policy first. Never fails the uninstall.
case "$1" in remove | 0) ;; *) exit 0 ;; esac

# $1: the command, $2/$3: its remove option for zones/policies (firewall-offline-cmd
# reads a bare --remove-service as a lokkit option and refuses it next to --zone).
drop() {
    for zone in $($1 --get-zones 2>/dev/null); do
        $1 --zone="$zone" "$2=sdc-testing-toolbox" >/dev/null 2>&1
    done
    for policy in $($1 --get-policies 2>/dev/null); do
        $1 --policy="$policy" "$3=sdc-testing-toolbox" >/dev/null 2>&1
    done
}

if firewall-cmd --state >/dev/null 2>&1; then
    drop "firewall-cmd --permanent" --remove-service --remove-service
    drop firewall-cmd --remove-service --remove-service
elif command -v firewall-offline-cmd >/dev/null 2>&1; then
    drop firewall-offline-cmd --remove-service-from-zone --remove-service-from-policy
fi
exit 0
