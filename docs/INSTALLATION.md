# Installation

Here are the steps to install cmpunlocker on your system.

---

## Requirements

- NVIDIA CMP 170HX (8GB or 10GB)
- Linux operating system (Ubuntu, Debian, Fedora, etc.)
- Kernel headers matching the running kernel (linux-headers-$(uname -r) / kernel-devel)
- Python 3
- **nvidia-open 610.43.0x already installed** (libs + firmware)
- Root access to the system (sudo privileges)
- Secure Boot disabled
- Network access on first install (downloads matching stock open-gpu-kernel-modules sources)

## Install

To install cmpunlocker, run the following command:

```bash
sudo ./install.sh
```

To force a certain memory profile, use the `--profile` option:

```bash
sudo ./install.sh --profile=8gb    # 8GB card → 64GB unlock
sudo ./install.sh --profile=10gb   # 10GB card → 40GB unlock
```

To change the HBM memory clock, use `--mclk-ndiv` (multiplier × 27 MHz, any VBIOS,
both `0x20C2` and `0x2082`):

```bash
sudo ./install.sh --mclk-ndiv=70   # → 1890 MHz
```

Stock is NDIV 64 on 8GB 300W VBIOS, 54 on 8GB 250W, 45 on 10GB. Without the flag
the overclock patches are not applied at all. See the README for the full table
and the recovery path if a value turns out unstable.

Then perform a cold reboot (full power off, then boot).

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./uninstall.sh --yes
```

Then perform a cold reboot (full power off, then boot).
