# Legion-Go-S-DSU

A tiny DSU (cemuhook) motion server for the **Lenovo Legion Go S on SteamOS**.

It reads the Steam-Deck-format HID reports that InputPlumber exposes for the Legion Go S and serves the gyro and accelerometer over the DSU protocol on `127.0.0.1:26760`, so emulators such as Eden, Ryubing/Ryujinx and Cemu can use motion controls.

- Single file, Python 3 standard library only. Nothing to install or compile.
- Only motion data is served (no buttons or sticks).
- Auto-finds the controller by USB ID and reconnects if InputPlumber restarts.

## Why this exists

Tools like [iio-dsu-bridge](https://github.com/Sebalvarez97/iio-dsu-bridge) read the IMU through Linux IIO (`/sys/bus/iio/devices`). On recent SteamOS builds the Legion Go S exposes only the ambient light sensor (`als`) there, so those tools fail with `no gyro/accel channels found`, even though gyro still works in Steam.

This script takes a different route: it reads the virtual Deck-style controller (`28DE:12FF`, named "Legion Go S Controller") that InputPlumber creates, which carries the IMU data.

## Requirements

- Legion Go S running SteamOS (tested on SteamOS 3.9.5, kernel 7.2.7)
- InputPlumber running (default on SteamOS)
- Python 3 (`/usr/bin/python3`, included with SteamOS)
- Read access to the virtual controller's hidraw device (udev rule below)
- An emulator with DSU / cemuhook motion support

Check that the virtual controller exists:

```bash
grep -l 12FF /sys/class/hidraw/hidraw*/device/uevent
```

You should see at least one match. If you don't, this script won't work on your setup.

`iio-dsu-bridge` is **not** needed. Both use port 26760, so disable it first if you have it installed:

```bash
systemctl --user disable --now iio-dsu-bridge
```

## Quick install

In Desktop Mode, open Konsole and run (as your normal user, not with sudo; it asks for your sudo password when needed):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/aitcfriatcsu-hub/Legion-Go-S-DSU/main/install.sh)
```

The installer checks that the controller is present, installs the script, adds the udev rule, creates and starts the user service, and enables auto-start. Set a sudo password first with `passwd` if you have never set one.

To uninstall:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/aitcfriatcsu-hub/Legion-Go-S-DSU/main/install.sh) --uninstall
```

## Manual install

Prefer to do it step by step? Do this in Desktop Mode, in a terminal (Konsole).

**1. Copy the script**

```bash
mkdir -p ~/.local/bin
cp legion-dsu.py ~/.local/bin/ && chmod +x ~/.local/bin/legion-dsu.py
```

**2. Test it** (tilt and rotate the device while it runs, Ctrl+C to stop)

```bash
sudo python3 ~/.local/bin/legion-dsu.py --probe
```

Gyro values should change when you rotate the device, and the accelerometer should show about ±16384 on the axis pointing down when the device is still.

**3. Give your user access to the device** (replace `deck` if your username differs)

```bash
echo 'SUBSYSTEM=="hidraw", KERNELS=="0003:28DE:12FF.*", OWNER="deck", MODE="0660"' | sudo tee /etc/udev/rules.d/99-legion-dsu.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw --action=add
```

**4. Run it as a service**

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/legion-dsu.service <<'EOF'
[Unit]
Description=Legion Go S DSU motion server

[Service]
ExecStart=/usr/bin/python3 %h/.local/bin/legion-dsu.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now legion-dsu
```

Check that it is running:

```bash
journalctl --user -u legion-dsu --no-pager | tail
```

You should see `reading /dev/hidrawN` and `DSU server listening on 127.0.0.1:26760`. If the service does not start in Gaming Mode after a reboot, also run:

```bash
sudo loginctl enable-linger deck
```

## Emulator setup

In the emulator's controller settings, enable motion from the DSU / cemuhook server with:

- Server: `127.0.0.1`
- Port: `26760`

Tested with Eden and Ryubing. When an emulator connects, the log shows `client subscribed`.

## Options

Add options to the `ExecStart=` line of the service file (then `systemctl --user daemon-reload && systemctl --user restart legion-dsu`).

| Option | Default | Description |
|---|---|---|
| `--probe` | off | Print live raw accel/gyro values instead of serving |
| `--invert-pitch` / `--invert-yaw` / `--invert-roll` | off | Flip an axis if motion feels backwards |
| `--gyro-scale N` | 1.0 | Multiply gyro rates (sensitivity) |
| `--deadzone N` | 8 | Gyro deadzone in raw units (16 = 1°/s) |
| `--bind ADDR` | 127.0.0.1 | UDP bind address |
| `--port N` | 26760 | UDP port |
| `--slot N` | 0 | DSU controller slot (0-3) |
| `--device PATH` | auto | Explicit `/dev/hidrawN` instead of auto-detect |
| `--vid HEX` / `--pid HEX` | 28de / 12ff | USB ID used for auto-detect |

## Troubleshooting

- **Service not running after boot:** `sudo loginctl enable-linger deck`, then reboot.
- **`cannot open /dev/hidrawN`:** the udev rule has not applied. Re-run the `udevadm trigger` line, or reboot.
- **`controller 28de:12ff not found`:** InputPlumber is not running, or your device presents a different ID. Check `cat /sys/class/hidraw/hidraw*/device/uevent | grep HID_`.
- **Server running but no `client subscribed`:** check the emulator's motion server settings and that Steam Input is not interfering with the controller.
- **Motion works but is backwards:** use the `--invert-*` options.

Logs: `journalctl --user -u legion-dsu -f`

## Uninstall

```bash
systemctl --user disable --now legion-dsu
rm ~/.config/systemd/user/legion-dsu.service ~/.local/bin/legion-dsu.py
sudo rm /etc/udev/rules.d/99-legion-dsu.rules
```

## Credits

- [SteamDeckGyroDSU](https://github.com/kmicki/SteamDeckGyroDSU) by kmicki (MIT). The Deck HID report layout and scaling values used here are documented in its source.
- [iio-dsu-bridge](https://github.com/Sebalvarez97/iio-dsu-bridge) for the original idea of a DSU server for handhelds on SteamOS.
- The DSU/cemuhook protocol, as implemented by Cemu and its clients.
- Written with Claude (Anthropic) and tested on a real Legion Go S.

## License

MIT, see [LICENSE](LICENSE).
