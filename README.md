# Shark Etcher

Shark Etcher is an open-source disk imaging utility inspired by Balena Etcher, rebuilt with a clean Python stack and zero telemetry.

## Features

- Lightweight `tkinter` GUI with modern styling, live logging, drive details and safety prompts
- Device list with size, model, bus and mount points pulled from `lsblk`
- Automatic unmounting of mounted targets before flashing
- Supports `.img`, `.iso`, `.zip`, `.gz`, `.xz`, `.bz2` images (ZIP is auto-extracted)
- Streaming writes with configurable chunk size, optional verification pass and privileged helper
- Console mode with live progress output plus graphical privilege escalation (`pkexec`) fallback
- Dry-run mode to exercise the workflow without touching the device

## Requirements

- Python 3.10+
- Linux environment with the `lsblk` command available
- `pkexec` (polkit) or root privileges to flash and unmount devices

## Quick Start (GUI)

```bash
./shark-etcher.sh
```

Alternatively you can launch the package directly:

```bash
python3 -m shark_etcher
```

Pick the source image, select the destination drive, then click `Write image`. The app will prompt for privileges (via `pkexec`) when needed, unmount the drive automatically and stream the image. Enable `Verify after write` or `Dry run` as needed before starting.

## Console Mode

List devices:

```bash
python3 -m shark_etcher --list
```

Flash directly from the terminal:

```bash
sudo python3 -m shark_etcher --cli --image /path/to/image.iso --device /dev/sdX --verify
```

Useful options:

- `--dry-run` runs the pipeline without writing to the device
- `--chunk-size` sets the block size in bytes (default 4 MiB)

## Safety Notes

- Double-check the destination path: flashing will irreversibly erase the drive
- The app unmounts the target automatically, but make sure nothing critical is using it
- Internal drives may show up in the list; avoid overwriting them accidentally

## Current Limitations

- Device enumeration is Linux-only (macOS and Windows backends still pending)
- ZIP archives are extracted to a temporary directory before writing
- `pkexec` is required for GUI elevation; CLI falls back to sudo/pkexec instructions
- No analytics or telemetry are collected or transmitted

Contributions and pull requests are welcome.
