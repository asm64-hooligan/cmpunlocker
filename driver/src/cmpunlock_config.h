/*
 * cmpunlock build configuration.
 *
 * driver/build.sh overwrites this file in the extracted source tree. The copy
 * kept in the repository is the default: no HBM overclock.
 *
 * Defining CMPUNLOCK_MCLK_NDIV to N compiles in the HBM PLL overclock and
 * targets N * 27 MHz. Leaving it undefined compiles both halves of the
 * overclock out entirely.
 */

#ifndef CMPUNLOCK_CONFIG_H
#define CMPUNLOCK_CONFIG_H

/* #define CMPUNLOCK_MCLK_NDIV 70 */

#endif /* CMPUNLOCK_CONFIG_H */
