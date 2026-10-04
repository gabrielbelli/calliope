#!/bin/sh
# Run as root by calliope-root (and by the first boot) from a release's own
# directory, each time a release is installed or put back. Idempotent: it
# brings the system to what this release needs and changes nothing else.
set -eu
R="${CALLIOPE_RELEASE:-$(cd "$(dirname "$0")" && pwd)}"

# Packages, only the missing ones (an update that needs none installs none).
PKGS="pipewire pipewire-pulse pipewire-alsa pipewire-bin wireplumber pulseaudio-utils python3-websockets python3-cryptography shairport-sync"
missing=""
for p in $PKGS; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing
fi

# Shairport Sync's own system service plays straight to ALSA as its own
# user; the satellite runs it as calliope instead, through PipeWire, under
# the name the hub gives it (calliope-airplay.service, a user unit).
if systemctl list-unit-files shairport-sync.service >/dev/null 2>&1; then
  systemctl disable --now shairport-sync.service >/dev/null 2>&1 || true
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
# Audio at the best the hardware takes (bundle/pipewire, bundle/wireplumber):
# installed for every PipeWire client and the PulseAudio server, and PipeWire
# restarted once in calliope's session when they changed, not on every update.
before=$(cat /etc/pipewire/*.conf.d/calliope-quality.conf /etc/wireplumber/wireplumber.conf.d/calliope-quality.conf 2>/dev/null | sha256sum)
for d in pipewire.conf.d client.conf.d pipewire-pulse.conf.d; do
  install -d "/etc/pipewire/$d"
  install -m 644 "$R/pipewire/calliope-quality.conf" "/etc/pipewire/$d/calliope-quality.conf"
done
install -d /etc/wireplumber/wireplumber.conf.d
install -m 644 "$R/wireplumber/calliope-quality.conf" /etc/wireplumber/wireplumber.conf.d/calliope-quality.conf
after=$(cat /etc/pipewire/*.conf.d/calliope-quality.conf /etc/wireplumber/wireplumber.conf.d/calliope-quality.conf | sha256sum)
AUDIO_CHANGED=0
[ "$before" = "$after" ] || AUDIO_CHANGED=1

# The units calliope's own session runs (AirPlay).
install -d /etc/systemd/user
for f in "$R"/systemd-user/*; do
  install -m 644 "$f" "/etc/systemd/user/$(basename "$f")"
done
# PipeWire runs in calliope's own session, started at boot without a login.
loginctl enable-linger calliope
systemctl daemon-reload
systemctl enable calliope-agent.service calliope-rollback.timer calliope-netcheck.timer
systemctl start calliope-rollback.timer calliope-netcheck.timer
if [ "$AUDIO_CHANGED" = 1 ] && systemctl is-active --quiet "user@$UID_.service"; then
  systemctl --user -M calliope@ restart wireplumber.service pipewire.service pipewire-pulse.service || true
  systemctl --user -M calliope@ try-restart calliope-airplay.service || true
fi
