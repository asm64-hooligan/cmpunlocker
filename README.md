# cmpunlocker

Unlock tool for the NVIDIA CMP 170HX (GA100). Restores full SM compute, unlocked HBM2e memory geometry and PCIe Gen2 that are restricted in firmware/OTP configuration.

**[Join our Discord community](https://discord.gg/CdHSakKSFv)** for support and discussions.

---
## Proof of Concept

Below are memory and performance results after applying the unlock:

### Unlock Results

<img width="777" height="973" alt="image" src="https://github.com/user-attachments/assets/026c767e-66dc-46e9-bbfa-14b1f445f146" />

---

## Requirements

- Linux (x86-64 or aarch64)
- Root access
- NVIDIA CMP 170HX (8GB or 10GB)
- **nvidia-open 610.43.0x already installed** (libs + firmware)
- Kernel headers matching the running kernel (`linux-headers-$(uname -r)` / `kernel-devel`)
- Secure Boot disabled (patched modules are unsigned)
- Network access on first install (downloads matching stock `open-gpu-kernel-modules` sources)

---

## Install

```bash
sudo ./install.sh
```

Then perform a cold reboot (full power off, then boot). The correct memory geometry is selected automatically from the PCI device ID (`0x20C2` = 8GB -> 64GB, `0x2082` = 10GB -> 40GB).

### HBM Memory Clock

`--mclk-ndiv=N` sets the FBPA PLL multiplier; the resulting clock is `N * 27` MHz. Any VBIOS works, on both `0x20C2` (8GB) and `0x2082` (10GB).

```bash
sudo ./install.sh --mclk-ndiv=70   # 1890 MHz
```

| NDIV | Frequency | Notes                           |
|------|-----------|---------------------------------|
| 45   | 1215 MHz  | Stock 10gb                      |
| 60   | 1620 MHz  | Works on ~60% of 10gb cards     |
| 54   | 1458 MHz  | Stock 8gb 250w vbios            |
| 64   | 1728 MHz  | Stock 8gb 300w vbios            |
| 70   | 1890 MHz  | Works on ~60% of 8gb cards      |
| 73   | 1971 MHz  | Usually only on lucky 8gb cards |

Values below stock downclock the card, which is the way to stabilise a card that fails at stock.

Without the flag the overclock is compiled out entirely. The multiplier is compiled into the modules, so changing it means re-running `install.sh`. In a mixed 8GB+10GB system the same multiplier lands on every card.

If a value turns out to be unstable - reinstall without `--mclk-ndiv` (or run `./remove.sh`) from a working state.

### IOMMU

NVIDIA recommends `iommu=pt` (passthrough) for all GPUs. The installer does **not** touch the kernel command line by default:

```bash
sudo ./install.sh --iommu
```

Or add `iommu=pt` to your kernel cmdline manually. IOMMU must also be enabled in BIOS (VT-d on Intel, AMD-Vi / SVM on AMD).

---

## Verify & Benchmark

After install and cold reboot, run the built-in GPU benchmark:

```bash
./benchmark/nvidia_bench
```

This measures memory bandwidth, tensor core throughput, PCIe speed, SM clock, and reports hardware features. An unlocked 8GB card should show ~64 GiB total memory and 56/70 SMs; a 10GB card should show ~40 GiB and 54/70 SMs.

```bash
./benchmark/nvidia_bench 0           # test GPU 0 (default)
./benchmark/nvidia_bench --csv       # machine-readable output
```

A pre-built x86-64 binary is included. On aarch64 (or to rebuild), install the CUDA toolkit and build from source:

```bash
cd benchmark && nvcc -O3 -o nvidia_bench nvidia_bench.cu -lnvidia-ml -ldl \
  -gencode arch=compute_80,code=sm_80 && strip nvidia_bench
```

## What Gets Unlocked

| Feature | Status |
|---|---|
| Full SM compute throughput (SS0/SS1) | Working |
| Memory geometry (64GB on 8GB cards, 40GB on 10GB cards) | Working |
| PCIe Gen 2 speeds | Working |
| HBM2 memory overclock/downclock | Working |
| Persistence across reboot (patched modules) | Working |

---

## Uninstall

```bash
sudo ./remove.sh --yes
```

Then perform a cold reboot (full power off, then boot).

## Community

Join our [Discord community](https://discord.gg/CdHSakKSFv) to discuss with other users.
