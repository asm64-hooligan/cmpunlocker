/*
 * cmpunlock build configuration.
 *
 * driver/build.sh overwrites this file in the extracted source tree. The copy
 * kept in the repository is the default: no HBM overclock, stock timings.
 *
 * CMPUNLOCK_MCLK_NDIV = N compiles in the HBM PLL overclock and targets
 * N * 27 MHz. Undefined compiles both halves of the overclock out entirely.
 *
 * CMPUNLOCK_MCLK_TIMINGS = N scales the DRAM timings by N percent before the
 * clock is raised. Positive loosens, negative tightens. The timing registers
 * hold cycle counts, so a higher clock shortens every one of them in real
 * time; scaling them back up restores the margin. Undefined leaves the VBIOS
 * timing table untouched.
 */

#ifndef CMPUNLOCK_CONFIG_H
#define CMPUNLOCK_CONFIG_H

/* #define CMPUNLOCK_MCLK_NDIV 70 */
/* #define CMPUNLOCK_MCLK_TIMINGS (20) */

#endif /* CMPUNLOCK_CONFIG_H */
