/*
 * cmpunlock build configuration.
 *
 * driver/build.sh overwrites this file in the extracted source tree. The copy
 * kept in the repository is the default: no HBM overclock, stock timings,
 * no P2P override.
 *
 * CMPUNLOCK_MCLK_NDIV = N compiles in the HBM PLL overclock and targets
 * N * 27 MHz. Undefined compiles both halves of the overclock out entirely.
 *
 * CMPUNLOCK_MCLK_TIMINGS = N scales the DRAM timings by N percent before the
 * clock is raised. Positive loosens, negative tightens. The timing registers
 * hold cycle counts, so a higher clock shortens every one of them in real
 * time; scaling them back up restores the margin. Undefined leaves the VBIOS
 * timing table untouched.
 *
 * CMPUNLOCK_ENABLE_P2P compiles in mailbox P2P support (--p2p): forces P2P
 * caps to OK and arms a PRI decode trap so the mailbox setup writes land on
 * CMP. Uses a 512 KB window inside the stock 64 MB BAR1 — no REBAR, no
 * kernel patches, works on any host with PCIe reach between GPUs. Undefined
 * leaves P2P as GSP reports it (disabled), which is the safe default.
 *
 * CMPUNLOCK_DISABLE_GEN2 compiles out the directed PCIe Gen2 speed change
 * (--no-gen2). Undefined keeps the retrain, which is the default. Define it on
 * hosts where the link does not train reliably at Gen2: a card that does not
 * come back from the retrain stays gone until a cold power cycle.
 * CMPUNLOCK_ENABLE_LATE_PMA compiles in the late PMA extension, which offers
 * the highest reserved FB region to PMA after init. On this hardware that
 * region is WPR plus GSP heap and is reserved end to end, so publishing it
 * yields no capacity and eventually faults the GPU with an Xid 31 region
 * violation followed by Xid 154. Undefined skips the extension, which is the
 * safe default; the unlocked capacity does not depend on it.
 */

#ifndef CMPUNLOCK_CONFIG_H
#define CMPUNLOCK_CONFIG_H

/* #define CMPUNLOCK_MCLK_NDIV 70 */
/* #define CMPUNLOCK_MCLK_TIMINGS (20) */
/* #define CMPUNLOCK_ENABLE_P2P 1 */
/* #define CMPUNLOCK_DISABLE_GEN2 1 */
/* #define CMPUNLOCK_ENABLE_LATE_PMA 1 */

#endif /* CMPUNLOCK_CONFIG_H */
