# cmpunlocker

Unlock tool for the NVIDIA CMP 170HX (GA100) mining card. Restores full SM compute throughput and unlocked HBM2e memory geometry that are restricted in firmware/OTP configuration.


**[Join our Discord community](https://discord.gg/CdHSakKSFv)** for support and discussions.

---
## Proof of Concept

Below are memory and performance results after applying the unlock:

### Memory Unlock Results

<img alt="memory unlock" src="https://github.com/user-attachments/assets/ae062bd8-e3a7-4e73-b9a4-fbcde53f3c7b" width="100%" style="max-width: 900px;" />

### Performance Benchmarks ([OpenCL-Benchmark](https://github.com/ProjectPhysX/OpenCL-Benchmark))

<img alt="performance benchmarks" src="https://github.com/user-attachments/assets/2501506d-420f-4014-9574-b1bd0290eb60" width="100%" style="max-width: 900px;" />

---

## Requirements

- Linux (x86-64)
- Root access
- NVIDIA CMP 170HX
- **nvidia-open 610.43.0x already installed** (libs + firmware)
- Kernel headers matching the running kernel (`linux-headers-$(uname -r)` / `kernel-devel`)
- Secure Boot disabled (patched modules are unsigned)
- Network access on first install (downloads matching stock `open-gpu-kernel-modules` sources)
- Python 3 (used at build time to select 8GB/10GB geometry)

---

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

Then perform a cold reboot (full power off, then boot).

### HBM Memory Clock

`--mclk-ndiv=N` sets the FBPA PLL multiplier; the resulting clock is `N × 27` MHz. Any VBIOS works, on both `0x20C2` (8GB) and `0x2082` (10GB) — the driver reads the stock NDIV out of the PLL and rewrites only that field, leaving MDIV/PDIV as the VBIOS programmed them.

```bash
sudo ./install.sh --mclk-ndiv=70   # → 1890 MHz
```

| NDIV | Frequency | Notes                           |
|------|-----------|---------------------------------|
| 45   | 1215 MHz  | Stock 10gb                      |
| 54   | 1458 MHz  | Stock 8gb 250w vbios            |
| 64   | 1728 MHz  | Stock 8gb 300w vbios            |
| 70   | 1890 MHz  | Works on ~60% of 8gb cards      |
| 73   | 1971 MHz  | Usually only on lucky 8gb cards |

Values below stock downclock the card, which is the way to cut memory power or stabilise a card that fails at stock.

Without the flag, patches `0009` and `0010` are not applied at all and nothing touches the PLL. The multiplier is compiled into the modules, so changing it means re-running `install.sh`. In a mixed 8GB+10GB box the same multiplier lands on every card, so check each one with `sudo ./verify.sh` before trusting it.

If a value turns out to be unstable, boot is what breaks — reinstall without `--mclk-ndiv` (or run `./remove.sh`) from a working state.

## What Gets Unlocked

| Feature | Status |
|---|---|
| Full SM compute throughput (SS0/SS1) | Working ✓ |
| Memory geometry (64GB on 8GB cards, 40GB on 10GB cards) | Working ✓ |
| PCIe Gen 2 speeds | Working ✓ |
| HBM2 memory overclock/downclock | Working ✓ |
| Persistence across reboot (patched modules) | Working ✓ |

---

## Uninstall

To uninstall cmpunlocker, run the following command:

```bash
sudo ./uninstall.sh --yes
```

Then perform a cold reboot (full power off, then boot).

## Support & Community

Having issues? Need help? Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with other users and get support.
