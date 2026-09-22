/*
 * fbpa_dump - READ-ONLY dump of the HBM controller (FBPA) state of every CMP 170HX in the machine.
 *
 * Why: the clock sequence walks FBPA 0..11 and touches the ones whose PLL registers answer. On most 8GB cards that is 8
 * FBPAs; one card answers on 4 and returns a PRI error (0xbadf2010) for the rest, and that card does not hold any
 * overclock. This tool shows, per card and per FBPA, whether the block answers, its PLL state (NDIV, lock), its gates
 * and its geometry, plus the broadcast view, so cards can be compared side by side.
 *
 * It maps BAR0 read-only and never writes. Reads of absent blocks return 0xbadfXXXX; that is the expected answer, not a
 * fault. Build and run as root:  cc -O2 -o fbpa_dump fbpa_dump.c && ./fbpa_dump
 */
#include <dirent.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define BAR0_SIZE 0x1000000UL
#define PCI "/sys/bus/pci/devices"
#define FBPA_BASE 0x900000U
#define FBPA_STRIDE 0x4000U
#define FBPA_COUNT 12
#define BCAST 0x9a0000U
#define IS_PRI_ERR(v) ((((v) & 0xFFF00000U) == 0xBAD00000U))

static unsigned hexfile(const char *bdf, const char *f)
{
    char p[320], b[32]; unsigned v = 0; FILE *fp;
    snprintf(p, sizeof p, "%s/%s/%s", PCI, bdf, f);
    if ((fp = fopen(p, "r"))) { if (fgets(b, sizeof b, fp)) v = (unsigned)strtoul(b, NULL, 16); fclose(fp); }
    return v;
}

static void show(const volatile uint32_t *r, const char *label, uint32_t base)
{
    uint32_t plm0 = r[(base + 0x3c7c) / 4], cfg = r[(base + 0x3c90) / 4], coeff = r[(base + 0x3c98) / 4];
    uint32_t gate = r[(base + 0x0148) / 4], gmem = r[(base + 0x0168) / 4], cfg1 = r[(base + 0x0204) / 4];
    uint32_t t0 = r[(base + 0x0290) / 4], gen0 = r[(base + 0x02b0) / 4];
    int live = !(IS_PRI_ERR(cfg) || IS_PRI_ERR(coeff) || cfg == 0 || coeff == 0);

    printf("  %-9s %s  PLLCFG=%08x COEFF=%08x", label, live ? "answers" : "silent ", cfg, coeff);
    if (live) printf(" NDIV=%-2u (%4u MHz) lock=%u", (coeff >> 8) & 0xff, 27 * ((coeff >> 8) & 0xff), (cfg >> 5) & 1);
    else      printf("                          ");
    printf("  PLLPLM=%08x FBPAPLM=%08x MEMPLM=%08x CFG1=%08x CONFIG0=%08x TIMING0=%08x\n", plm0, gate, gmem, cfg1, t0, gen0);
}

int main(void)
{
    DIR *d = opendir(PCI); struct dirent *e; int n = 0;
    if (!d) { perror(PCI); return 1; }
    while ((e = readdir(d))) {
        char path[320]; int fd, i, live = 0; volatile uint32_t *r;
        if (e->d_name[0] == '.' || hexfile(e->d_name, "vendor") != 0x10de) continue;
        unsigned dev = hexfile(e->d_name, "device");
        if (dev != 0x20c2 && dev != 0x2082) continue;
        snprintf(path, sizeof path, "%s/%s/resource0", PCI, e->d_name);
        if ((fd = open(path, O_RDONLY | O_SYNC)) < 0) { perror(path); continue; }
        r = mmap(NULL, BAR0_SIZE, PROT_READ, MAP_SHARED, fd, 0);
        if (r == MAP_FAILED) { perror("mmap"); close(fd); continue; }
        printf("== %s  device %04x  BOOT_0=%08x\n", e->d_name, dev, r[0]);
        for (i = 0; i < FBPA_COUNT; i++) {
            char l[16]; uint32_t base = FBPA_BASE + (uint32_t)i * FBPA_STRIDE;
            uint32_t cfg = r[(base + 0x3c90) / 4], coeff = r[(base + 0x3c98) / 4];
            snprintf(l, sizeof l, "FBPA%-2d", i); show(r, l, base);
            if (!(IS_PRI_ERR(cfg) || IS_PRI_ERR(coeff) || cfg == 0 || coeff == 0)) live++;
        }
        show(r, "broadcast", BCAST);
        printf("  answering FBPAs: %d   DDLL_CAL=%08x status=%08x %08x  SELF_REFRESH=%08x  FBIO_BROADCAST=%08x  MMU_LMR=%08x\n\n",
               live, r[0x9a11dc / 4], r[0x9a0674 / 4], r[0x9a0678 / 4], r[0x9a031c / 4], r[0x9a0590 / 4], r[0x100ce0 / 4]);
        munmap((void *)r, BAR0_SIZE); close(fd); n++;
    }
    closedir(d);
    if (!n) { fprintf(stderr, "no CMP 170HX found (run as root)\n"); return 1; }
    return 0;
}
