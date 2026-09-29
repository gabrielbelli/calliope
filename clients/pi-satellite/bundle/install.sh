#!/bin/sh
# Run as root by calliope-root (and by the first boot) from a release's own
# directory, each time a release is installed or put back. Idempotent: it
# brings the system to what this release needs and changes nothing else.
set -eu
R="${CALLIOPE_RELEASE:-$(cd "$(dirname "$0")" && pwd)}"

# Packages, only the missing ones (an update that needs none installs none).
PKGS="pipewire pipewire-pulse pipewire-alsa pipewire-bin wireplumber python3-websockets python3-cryptography"
missing=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing
fi

# The user the agent runs as: made by cloud-init on a card set up with
# prepare_sd.py, made here otherwise. `audio` lets its PipeWire open the
# sound cards without a login seat.
id calliope >/dev/null 2>&1 || useradd -m -s /usr/sbin/nologin calliope
usermod -aG audio calliope
UID_=$(id -u calliope)
install -d -o calliope -g calliope -m 700 /var/lib/calliope
install -d -m 755 /etc/calliope

# The one command the agent may run as root, and the rule that lets it.
install -m 755 "$R/bin/calliope-root" /usr/local/sbin/calliope-root
install -m 440 "$R/sudoers.d/calliope" /etc/sudoers.d/calliope.new
visudo -cqf /etc/sudoers.d/calliope.new && mv /etc/sudoers.d/calliope.new /etc/sudoers.d/calliope

# The services and timers, with the agent's user id filled in.
for f in "$R"/systemd/*; do
  sed "s/@UID@/$UID_/g" "$f" > "/etc/systemd/system/$(basename "$f")"
done
# PipeWire runs in calliope's own session, started at boot without a login.
loginctl enable-linger calliope
systemctl daemon-reload
systemctl enable calliope-agent.service calliope-rollback.timer calliope-netcheck.timer
systemctl start calliope-rollback.timer calliope-netcheck.timer
