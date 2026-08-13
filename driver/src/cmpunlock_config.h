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
 * CMPUNLOCK_ENABLE_P2P compiles in the GPU-to-GPU P2P capability override
 * (--p2p). Undefined leaves the caps as GSP reported them, which is the safe
 * default: forcing them on a host that cannot carry P2P turns a clean
 * "unsupported" into transfers that time out.
 *
 * CMPUNLOCK_DISABLE_GEN2 compiles out the directed PCIe Gen2 speed change
 * (--no-gen2). Undefined keeps the retrain, which is the default. Define it on
 * hosts where the link does not train reliably at Gen2: a card that does not
 * come back from the retrain stays gone until a cold power cycle.
 */

#ifndef CMPUNLOCK_CONFIG_H
#define CMPUNLOCK_CONFIG_H

/* #define CMPUNLOCK_MCLK_NDIV 70 */
/* #define CMPUNLOCK_MCLK_TIMINGS (20) */
/* #define CMPUNLOCK_ENABLE_P2P 1 */
/* #define CMPUNLOCK_DISABLE_GEN2 1 */

#endif /* CMPUNLOCK_CONFIG_H */
