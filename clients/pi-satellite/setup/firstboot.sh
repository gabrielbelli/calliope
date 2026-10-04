#!/bin/sh
# Run once by cloud-init, from the boot partition: unpack the release the card
# was prepared with and start the first boot (calliope_pi/firstboot.py), which
# finds a network, installs the packages and starts the agent.
set -eu
B=/boot/firmware/calliope
V=$(tar -xzOf "$B/bundle.tar.gz" manifest.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
D=/opt/calliope/releases/$V
rm -rf "$D"
mkdir -p "$D"
tar -xzf "$B/bundle.tar.gz" -C "$D" --no-same-owner
ln -sfn "$D" /opt/calliope/current
cp "$D/systemd/calliope-firstboot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable calliope-firstboot.service
systemctl start --no-block calliope-firstboot.service
