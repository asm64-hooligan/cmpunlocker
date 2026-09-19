# hbmtune: a memory clock for each card

`--mclk-ndiv` gives every card the same HBM clock, so the weakest card sets the clock for all of them.
In one four-card 170HX box, three cards hold NDIV 70 (1890 MHz) and the fourth corrupts data at NDIV 68.
With `--mclk-percard` the driver reads a multiplier for each card from the per-device registry.
`hbmtune` finds that multiplier for each card, tests it, writes it, and lets you overrule it.

## Install

```bash
sudo ./install.sh --p2p --mclk-ndiv=64 --mclk-percard   # your usual flags plus --mclk-percard; then cold boot once
sudo tools/hbmtune/install-hbmtune.sh                   # root-owned copies, /etc/hbmtune/hbmtune.conf, resume unit
sudo hbmtune selfcheck                                  # the gates must catch a single flipped bit on this machine
sudo hbmtune status
```

`--mclk-ndiv` is now only the default for cards that have no value of their own.
Without it, such a card stays at the clock the VBIOS programmed, which is what a mixed 8GB + 10GB system needs.

## Where a card's value comes from

1. A **manual override**: `hbmtune set`. It always wins.
2. The **automatic result**: `hbmtune auto start`.
3. The **baseline**: `baseline_ndiv` in `/etc/hbmtune/hbmtune.conf` (0 = leave the VBIOS clock).

Cards are named by index (`1`), PCI address (`0000:41:00.0`) or UUID.
The state file uses the UUID, so a card keeps its value when it moves to another slot.
The values go to `/etc/modprobe.d/cmp-hbmtune.conf` and become live at the next driver load.
The driver usually loads from the initramfs, which carries its own copy of `/etc/modprobe.d`, so hbmtune rebuilds the boot image after every change (`initramfs_cmd`, default `auto`). A value that is not in the boot image is simply not read at boot; `hbmtune status` says so when the loaded driver received no per-device options.

```
options nvidia NVreg_RegistryDwordsPerDevice="pci=0000:01:00.0;cmpMclkNdiv=69;pci=0000:41:00.0;cmpMclkNdiv=65"
```

## Manual control

```bash
sudo hbmtune set 1 --ndiv 66 --lock --note "fails the replay at 68"   # pin card 1; the search leaves locked cards alone
sudo hbmtune set 0 --timings 110                                       # timing table at 110 % of stock for card 0
sudo hbmtune set 0 --ndiv 72 --force                                   # overrule a recorded failure (your risk)
sudo hbmtune unset 1          # back to the automatic result or the baseline
sudo hbmtune test             # run the gates at the clocks that are live now, record the result per card
sudo hbmtune safe on          # next driver load ignores every memory tunable; `safe off` brings the values back
```

`set` refuses a multiplier at or above one that failed the gates on that card, and any tightening of the timings, unless you add `--force`.

### If the machine does not boot any more

Add this to the kernel command line for one boot (GRUB: press `e`, append to the `linux` line):

```
nvidia.NVreg_RegistryDwords=cmpMclkSafe=1
```

The unlock (memory size, PCIe, P2P) still runs; only the memory clock and timings are skipped.
Then run `sudo hbmtune auto abort` or `sudo hbmtune set ...`. Upstream's `cmpSafe=1` also still works.

## The automatic search

```bash
sudo hbmtune auto start       # all cards that are not locked
sudo hbmtune auto status      # the log so far
sudo hbmtune auto abort       # stop; the modprobe file goes back to the manual/automatic/baseline values
```

1. **Baseline.** The values that are live now are tested first. They count as known good only if they pass.
2. **Climb.** Every card goes up by `step` until it fails or reaches `max_ndiv`. All cards move in the same driver load, so the search costs as many loads as the best card needs.
3. **Bisect** between the best pass and the first failure, down to one step.
4. **Margin.** The result is `margin` steps under the lowest failure. A card that never failed is treated as failing at `max_ndiv + 1`.
5. **Soak.** The final values of all cards run `soak_minutes` together. A card that fails steps down and the soak runs again.

A card that fails at its baseline is searched downward to `min_ndiv`.

Each new value needs a driver load. `apply_mode` says how: `reload` (unload and load the modules), `reboot`, or `manual` (the tool asks for a power cycle and continues by itself after the boot, through `hbmtune-resume.service`).
When a value is not live after the load, the tool moves from `reload` to `reboot` to `manual` by itself.
The live value is read from the driver's own report, one line per card:

```
HBMPLL_OC: RESULT pci=0000:41:00 ndiv=66 mhz=1782 lock=1 timings_pct=100 source=percard fbpas=8
```

`ndiv` and `lock` are read from the first FBPA that answers, and `fbpas` is how many answer. `source` is `percard`, `build`, `stock` (this card was told to keep the VBIOS clock, its PLL was not touched) or `safe`. For a card pinned to the VBIOS clock (`--ndiv 0`), `source=stock` is the proof that it is live.

What counts as a failure for a card: any wrong bit in the gates, a PLL that does not lock, a card that is missing after the load, a value that does not become live.
When the whole machine goes down with several cards raised, nobody is blamed: those cards are then raised one at a time.
A boot with `cmpMclkSafe=1` during a search writes the known-good values back first.
A run that is stopped for temperature gives no verdict at all.

## The gates (`gates.py`)

A memory overclock fails silently, so the gates look for wrong numbers, on all cards at once, and give a verdict per card:

| Gate | What it does |
|---|---|
| pattern sweep | nearly all VRAM in 1 GiB chunks: seeded random, complement, address pattern, checkerboard; written, left alone, compared with a regenerated copy |
| hammer | the chunks rotate through each other at full memory bandwidth, with GEMMs in between for heat; at the end every chunk is compared bit for bit; reports copy bandwidth |
| bit-exact GEMM | seeded bf16 and fp16 products must give the same bits every time, and the same bits as a reference written at a known-good clock (`--make-ref`, `gate_ref`) |

Copy bandwidth has to go up with the clock. A flat or falling number means the controller is retrying.
`extra_gate_cmd` adds your own workload as a second gate (same JSON on the last line of stdout); a card must pass both.
`--max-hbm-c` (88) and `--max-core-c` (85) stop a run that gets too hot.

## Settings (`/etc/hbmtune/hbmtune.conf`)

| Key | Default | Meaning |
|---|---|---|
| `baseline_ndiv` | 0 | value for cards without a manual or automatic value; 0 = VBIOS clock |
| `min_ndiv` / `max_ndiv` | 54 / 74 | search range |
| `step` / `margin` | 2 / 1 | climb step; tested headroom under the lowest failure |
| `quick_minutes` / `soak_minutes` / `test_minutes` | 6 / 45 / 10 | hammer time per search step, for the final soak, for `hbmtune test` |
| `apply_mode` | reboot | `reload`, `reboot` or `manual` |
| `initramfs_cmd` | auto | command that rebuilds the boot image after a change; `auto` = update-initramfs / dracut / mkinitcpio; `none` = skip |
| `gate_cmd` | `python3 .../gates.py` | where PyTorch with CUDA lives, for example `docker exec mybox python3 /work/gates.py` |
| `gate_ref` | | GEMM reference file, as `gate_cmd` sees it |
| `extra_gate_cmd` | | optional second gate |
| `preflight_cmd` | | must exit 0 before any test (production stopped, fans fine) |
| `expected_cards` | 0 | cards that must be present after every driver load; 0 = the number at `auto start` |

## Tests

`python3 tools/hbmtune/test_hbmtune.py` runs the search against a simulated machine: four cards of different quality, driver loads that do not apply, a PLL that does not lock, a card that takes the machine down, a soak failure, a safe boot, a thermal stop, manual overrides.
`python3 tools/hbmtune/gates.py --self-test` flips one bit in each phase on the real GPU and must catch both.
