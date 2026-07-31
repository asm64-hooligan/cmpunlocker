#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mapfile -t SUPPORTED_VERSIONS < <(grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' "${SCRIPT_DIR}/driver/VERSION")
SUPPORTED_VERSIONS_CSV="$(IFS=', '; echo "${SUPPORTED_VERSIONS[*]}")"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/install_$(date +%Y%m%d_%H%M%S).log"

CONFIGURE_IOMMU=0
MCLK_NDIV=""
MCLK_TIMINGS=""
for arg in "$@"; do
    case "${arg}" in
        --iommu) CONFIGURE_IOMMU=1 ;;
        --mclk-ndiv=*) MCLK_NDIV="${arg#*=}" ;;
        --mclk-timings=*) MCLK_TIMINGS="${arg#*=}" ;;
        -h|--help)
            cat <<'EOF'
Usage: sudo ./install.sh [--iommu] [--mclk-ndiv=N] [--mclk-timings=N]

  --iommu         Add iommu=pt to the kernel command line (see README for details)
  --mclk-ndiv=N   HBM memory clock: set PLL multiplier (30-80), N * 27 MHz.
                  Works on any VBIOS, on both 0x20C2 and 0x2082. Stock is 64 on
                  8GB 300W, 54 on 8GB 250W, 45 on 10GB. Without this flag the
                  overclock is not applied at all.
  --mclk-timings=N
                  Scale DRAM timings by N percent, -50 to +50. Positive loosens,
                  negative tightens (--mclk-timings=-10). Applied before the
                  clock is raised. Timings are cycle counts, so a higher clock
                  tightens them in real time; loosening gives that margin back
                  and can make an otherwise unstable --mclk-ndiv hold.
                  Tightening is the risky direction: too small a value corrupts
                  data silently or wedges the memory controller.
                  Scaled: tRC tRFC tRAS tRP tRCD tWR tFAW tRRD.
                  Never touched: CL, WL, tCCD.

Memory geometry is selected automatically from PCI device ID:
  10de:20c2 → 8GB card → 64GB unlock
  10de:2082 → 10GB card → 40GB unlock

Multi-GPU and mixed 8GB+10GB systems are supported.
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            echo "Try: sudo ./install.sh --help" >&2
            exit 1
            ;;
    esac
done

exec > >(tee -a "${LOG_FILE}") 2>&1

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; NC=""
fi

info() { echo -e "${CYAN}==>${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*" >&2; }
die()  { err "$*"; exit 1; }

step() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}$*${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

#
# Ask a yes/no question. Reads from the terminal rather than stdin, because
# stdout is piped through tee and the script may be run from a pipe.
# Returns non-zero when the answer is no or when there is nobody to ask.
#
confirm() {
    local reply="" prompt
    prompt="$(echo -e "${YELLOW}?${NC} $* [y/N] ")"
    #
    # -r is not enough: /dev/tty exists but fails to open when there is no
    # controlling terminal, so probe it by actually opening it.
    #
    if { : < /dev/tty; } 2>/dev/null; then
        read -r -p "${prompt}" reply < /dev/tty || return 1
    elif [[ -t 0 ]]; then
        read -r -p "${prompt}" reply || return 1
    else
        warn "Not running interactively — cannot ask, assuming no"
        return 1
    fi
    [[ "${reply}" =~ ^[Yy]([Ee][Ss])?$ ]]
}

normalize_bus_id() {
    local raw="$1"
    raw="$(echo "${raw}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    if [[ "${raw}" =~ ^([0-9a-f]+):([0-9a-f]{2}):([0-9a-f]{2})\.([0-9a-f])$ ]]; then
        printf '%04x:%s:%s.%s\n' "$((16#${BASH_REMATCH[1]}))" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"
    elif [[ "${raw}" =~ ^[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$ ]]; then
        echo "0000:${raw}"
    else
        echo "${raw}"
    fi
}

profile_from_devid() {
    case "$1" in
        20c2) echo "8gb" ;;
        2082) echo "10gb" ;;
        *) echo "unsupported" ;;
    esac
}

expected_mib_for_profile() {
    case "$1" in
        8gb) echo "65536" ;;
        10gb) echo "40960" ;;
        *) echo "" ;;
    esac
}

smi_memory_for_bus() {
    local want="$1"
    local line bus mem
    [[ -n "${SMI_MEM_CACHE:-}" ]] || return 0
    while IFS= read -r line; do
        [[ -n "${line}" ]] || continue
        bus="$(normalize_bus_id "$(echo "${line}" | cut -d, -f1)")"
        mem="$(echo "${line}" | cut -d, -f2 | tr -d '[:space:]')"
        if [[ "${bus}" == "${want}" ]]; then
            echo "${mem}"
            return 0
        fi
    done <<< "${SMI_MEM_CACHE}"
}

echo ""
echo -e "${CYAN}╔════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║               cmpunlocker              ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════╝${NC}"
echo ""

step "Verifying root privileges"
[[ "${EUID}" -eq 0 ]] || die "Run as root: sudo ./install.sh"
ok "Running as root"

step "Detecting CMP 170HX GPU(s)"
mapfile -t PCI_LINES < <(lspci -nn 2>/dev/null | grep -iE '10de:20b0|10de:20c2|10de:2082' || true)
ALLOW_NO_GPU=0
if [[ ${#PCI_LINES[@]} -eq 0 ]]; then
    warn "No CMP 170HX found on the PCI bus (10de:20b0 / 10de:20c2 / 10de:2082)"
    echo ""
    echo "  The card may be out of the machine, or an unstable overclock may have"
    echo "  left it unable to initialise. Building without it is how you install a"
    echo "  corrected driver before putting it back."
    echo ""
    echo "  The unlock reads its geometry from the device ID at runtime, so the"
    echo "  modules built here work on either variant once a card is present."
    echo ""
    confirm "Continue without a GPU?" || die "Aborted — no GPU detected"
    ALLOW_NO_GPU=1
    echo ""
fi

SMI_MEM_CACHE=""
if command -v nvidia-smi &>/dev/null; then
    SMI_MEM_CACHE="$(nvidia-smi --query-gpu=pci.bus_id,memory.total --format=csv,noheader,nounits 2>/dev/null || true)"
fi

GPU_COUNT=0
COUNT_8GB=0
COUNT_10GB=0

for PCI_LINE in "${PCI_LINES[@]}"; do
    PCI="$(echo "${PCI_LINE}" | awk '{print $1}')"
    PCI_FULL="$(normalize_bus_id "${PCI}")"
    DEVID="$(echo "${PCI_LINE}" | grep -oE '10de:[0-9a-fA-F]{4}' | head -1 | cut -d: -f2 | tr '[:upper:]' '[:lower:]')"
    PROF="$(profile_from_devid "${DEVID}")"
    CUR_MEM="$(smi_memory_for_bus "${PCI_FULL}" || true)"
    [[ -n "${CUR_MEM}" ]] || CUR_MEM="?"

    if [[ "${PROF}" == "unsupported" ]]; then
        warn "GPU ${PCI_FULL} (10de:${DEVID}) — not a supported device ID; skipping"
        continue
    fi

    EXP="$(expected_mib_for_profile "${PROF}")"
    GPU_COUNT=$((GPU_COUNT + 1))

    if [[ "${PROF}" == "8gb" ]]; then
        COUNT_8GB=$((COUNT_8GB + 1))
    else
        COUNT_10GB=$((COUNT_10GB + 1))
    fi

    if [[ "${CUR_MEM}" != "?" ]]; then
        ok "GPU ${PCI_FULL} (10de:${DEVID}) → ${PROF} (current ${CUR_MEM} MiB, expect ~${EXP} MiB unlocked)"
    else
        ok "GPU ${PCI_FULL} (10de:${DEVID}) → ${PROF} (expect ~${EXP} MiB unlocked)"
    fi
done

if [[ "${GPU_COUNT}" -eq 0 ]]; then
    if (( ALLOW_NO_GPU == 0 )); then
        warn "No unlockable CMP 170HX found (need 10de:20c2 and/or 10de:2082)"
        echo ""
        confirm "Continue anyway?" || die "Aborted — no unlockable GPU detected"
        ALLOW_NO_GPU=1
        echo ""
    fi
    warn "Building with no GPU to check against"
else
    info "Found ${GPU_COUNT} unlockable GPU(s): ${COUNT_8GB}x 8gb, ${COUNT_10GB}x 10gb"
fi

if [[ -n "${MCLK_NDIV}" ]]; then
    if ! [[ "${MCLK_NDIV}" =~ ^[0-9]+$ ]] || [[ "${MCLK_NDIV}" -lt 30 || "${MCLK_NDIV}" -gt 80 ]]; then
        die "--mclk-ndiv must be between 30 and 80 (got: ${MCLK_NDIV})"
    fi
    ok "MCLK set: NDIV=${MCLK_NDIV} ($((MCLK_NDIV * 27)) MHz) on every unlockable card"
    if (( GPU_COUNT == 0 )); then
        warn "No card present to check this against — NDIV ${MCLK_NDIV} is being"
        warn "compiled in blind. Stock is 64 on 8gb 300W, 54 on 8gb 250W, 45 on 10gb."
    elif (( COUNT_8GB > 0 && COUNT_10GB > 0 )); then
        warn "Mixed inventory: the multiplier is compiled in once and applies to both"
        warn "variants, but stock differs (8gb 54/64 vs 10gb 45). NDIV ${MCLK_NDIV} is"
        warn "$((MCLK_NDIV * 27)) MHz on all of them — verify each card in dmesg."
    elif (( COUNT_10GB > 0 )); then
        info "Stock for 10gb is NDIV 45 (1215 MHz)"
    else
        info "Stock for 8gb is NDIV 54 (1458 MHz, 250W VBIOS) or 64 (1728 MHz, 300W VBIOS)"
    fi
else
    info "MCLK overclock disabled (use --mclk-ndiv=N to enable)"
fi
export CMPUNLOCKER_MCLK_NDIV="${MCLK_NDIV}"

if [[ -n "${MCLK_TIMINGS}" ]]; then
    if ! [[ "${MCLK_TIMINGS}" =~ ^[+-]?[0-9]+$ ]] || \
       [[ "${MCLK_TIMINGS}" -lt -50 || "${MCLK_TIMINGS}" -gt 50 ]]; then
        die "--mclk-timings must be between -50 and 50 (got: ${MCLK_TIMINGS})"
    fi
    if [[ "${MCLK_TIMINGS}" -lt 0 ]]; then
        ok "DRAM timings tightened by ${MCLK_TIMINGS#-}% (tRC/tRFC/tRAS/tRP/tRCD/tWR/tFAW/tRRD)"
        warn "Tightening can corrupt data silently or wedge the memory controller."
        warn "Validate with a long gpu-burn run before trusting it — see overclocking/"
    elif [[ "${MCLK_TIMINGS}" -eq 0 ]]; then
        info "--mclk-timings=0 is a no-op; timings left at stock"
    else
        ok "DRAM timings loosened by ${MCLK_TIMINGS#+}% (tRC/tRFC/tRAS/tRP/tRCD/tWR/tFAW/tRRD)"
    fi
    info "CL, WL and tCCD are left at stock on purpose — see overclocking/timings/"
else
    info "DRAM timings left at stock (use --mclk-timings=N to scale)"
fi
export CMPUNLOCKER_MCLK_TIMINGS="${MCLK_TIMINGS}"

step "Verifying nvidia-open (${SUPPORTED_VERSIONS_CSV})"
[[ ${#SUPPORTED_VERSIONS[@]} -gt 0 ]] || die "No supported versions listed in driver/VERSION"
if [[ -d /sys/firmware/efi ]] && command -v mokutil &>/dev/null; then
    if mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; then
        die "Secure Boot is enabled. Disable it before installing unsigned patched modules."
    fi
fi

version_supported() {
    local v="$1"
    local s
    for s in "${SUPPORTED_VERSIONS[@]}"; do
        [[ "${v}" == "${s}" ]] && return 0
    done
    return 1
}

detected=""
if [[ -r /proc/driver/nvidia/version ]]; then
    detected="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' /proc/driver/nvidia/version | head -1 || true)"
fi
if [[ -z "${detected}" ]] && command -v nvidia-smi &>/dev/null; then
    #
    # nvidia-smi prints its "couldn't communicate with the driver" error on
    # stdout and not stderr, so match the version shape rather than taking
    # whatever came back.
    #
    detected="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
        | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)"
fi
if [[ -z "${detected}" ]]; then
    for cand in "${SUPPORTED_VERSIONS[@]}"; do
        if [[ -d "/lib/firmware/nvidia/${cand}" ]]; then
            detected="${cand}"
            break
        fi
    done
    if [[ -z "${detected}" ]]; then
        fw="$(ls -d /lib/firmware/nvidia/*/ 2>/dev/null | sed 's|.*/nvidia/||;s|/||' | sort -rV | head -1 || true)"
        detected="${fw}"
    fi
fi

[[ -n "${detected}" ]] || die "Could not detect an installed NVIDIA driver. Install nvidia-open ${SUPPORTED_VERSIONS_CSV} first."
version_supported "${detected}" || die "Installed driver is ${detected}, but cmpunlocker requires one of: ${SUPPORTED_VERSIONS_CSV}."
ok "NVIDIA driver ${detected} is supported"

[[ -d "/lib/modules/$(uname -r)/build" ]] || die "Kernel headers missing for $(uname -r). Install linux-headers-$(uname -r) or kernel-devel."
ok "Kernel headers present for $(uname -r)"

step "Building and installing patched modules"
chmod +x "${SCRIPT_DIR}/driver/build.sh"
CMPUNLOCKER_DRIVER_VERSION="${detected}" \
CMPUNLOCKER_MCLK_NDIV="${MCLK_NDIV}" \
CMPUNLOCKER_MCLK_TIMINGS="${MCLK_TIMINGS}" \
    "${SCRIPT_DIR}/driver/build.sh"
ok "Patched modules installed"

step "Configuring IOMMU (passthrough)"
IOMMU_STATUS="skipped"
IOMMU_PARAMS=""

iommu_params_for_cpu() {
    local vendor=""
    vendor="$(awk -F': ' '/^vendor_id/{print $2; exit}' /proc/cpuinfo 2>/dev/null || true)"
    case "${vendor}" in
        GenuineIntel) echo "intel_iommu=on iommu=pt" ;;
        AuthenticAMD) echo "amd_iommu=on iommu=pt" ;;
        *) echo "" ;;
    esac
}

cmdline_merge() {
    local current="$1"
    local token out=()
    for token in ${current}; do
        case "${token}" in
            intel_iommu=*|amd_iommu=*|iommu=*) continue ;;
            *) out+=("${token}") ;;
        esac
    done
    for token in ${IOMMU_PARAMS}; do
        out+=("${token}")
    done
    echo "${out[*]}"
}

configure_iommu_grub() {
    local grub_file="/etc/default/grub"
    local key="GRUB_CMDLINE_LINUX_DEFAULT"
    local current merged

    grep -q "^${key}=" "${grub_file}" || key="GRUB_CMDLINE_LINUX"
    if grep -q "^${key}=" "${grub_file}"; then
        current="$(sed -n "s/^${key}=\"\(.*\)\"$/\1/p" "${grub_file}" | head -1)"
    else
        current=""
    fi
    merged="$(cmdline_merge "${current}")"

    if [[ "${current}" == "${merged}" ]]; then
        ok "GRUB already has ${IOMMU_PARAMS} (${key})"
        IOMMU_STATUS="already-set"
        return 0
    fi

    cp -a "${grub_file}" "${grub_file}.cmpunlocker.bak"
    if grep -q "^${key}=" "${grub_file}"; then
        local escaped="${merged//\//\\/}"
        sed -i "s/^${key}=.*/${key}=\"${escaped}\"/" "${grub_file}"
    else
        printf '%s="%s"\n' "${key}" "${merged}" >> "${grub_file}"
    fi
    ok "Set ${key}=\"${merged}\" (backup: ${grub_file}.cmpunlocker.bak)"

    local regen_ok=1
    if command -v update-grub &>/dev/null; then
        update-grub || regen_ok=0
    elif command -v grub2-mkconfig &>/dev/null; then
        #
        # On Fedora/RHEL /boot/efi/EFI/*/grub.cfg is a stub that chainloads
        # /boot/grub2/grub.cfg, and grub2-mkconfig refuses to overwrite it.
        # Prefer the real config; the EFI path is only it on older layouts.
        #
        local cfg=""
        if [[ -f /boot/grub2/grub.cfg ]]; then
            cfg="/boot/grub2/grub.cfg"
        else
            cfg="$(ls /boot/efi/EFI/*/grub.cfg 2>/dev/null | head -1 || true)"
        fi
        if [[ -n "${cfg}" ]]; then
            grub2-mkconfig -o "${cfg}" || regen_ok=0
        else
            regen_ok=0
        fi
    elif command -v grub-mkconfig &>/dev/null; then
        grub-mkconfig -o /boot/grub/grub.cfg || regen_ok=0
    else
        warn "No grub config generator found — regenerate grub.cfg manually"
        IOMMU_STATUS="needs-grub-regen"
        return 0
    fi

    if (( regen_ok == 0 )); then
        warn "Could not regenerate grub.cfg — ${grub_file} is staged but inactive"
        warn "Regenerate it yourself, or restore ${grub_file}.cmpunlocker.bak"
        IOMMU_STATUS="needs-grub-regen"
        return 0
    fi
    ok "Regenerated GRUB config"

    #
    # BLS entries carry their own cmdline, and regenerating grub.cfg does not
    # rewrite the ones that already exist — only new kernels would pick the
    # parameters up. grubby patches the existing entries.
    #
    if [[ -d /boot/loader/entries ]] && command -v grubby &>/dev/null; then
        if grubby --update-kernel=ALL --args="${IOMMU_PARAMS}"; then
            ok "Updated existing boot entries via grubby"
        else
            warn "grubby could not update existing boot entries — only new kernels get ${IOMMU_PARAMS}"
            IOMMU_STATUS="needs-grub-regen"
            return 0
        fi
    fi
    IOMMU_STATUS="configured"
}

configure_iommu_kernel_cmdline() {
    local file="/etc/kernel/cmdline"
    local current merged
    current="$(tr -d '\n' < "${file}")"
    merged="$(cmdline_merge "${current}")"

    if [[ "${current}" == "${merged}" ]]; then
        ok "${file} already has ${IOMMU_PARAMS}"
        IOMMU_STATUS="already-set"
        return 0
    fi

    cp -a "${file}" "${file}.cmpunlocker.bak"
    printf '%s\n' "${merged}" > "${file}"
    ok "Set ${file} to \"${merged}\" (backup: ${file}.cmpunlocker.bak)"

    if command -v kernel-install &>/dev/null && [[ -d /boot/loader/entries ]]; then
        for kdir in /lib/modules/*/; do
            kver="$(basename "${kdir}")"
            [[ -f "${kdir}/vmlinuz" ]] || continue
            kernel-install add "${kver}" "${kdir}/vmlinuz" 2>/dev/null || true
        done
        ok "Refreshed systemd-boot entries"
        IOMMU_STATUS="configured"
    else
        warn "Update your boot entries so ${file} takes effect"
        IOMMU_STATUS="needs-boot-refresh"
    fi
}

if (( CONFIGURE_IOMMU == 0 )); then
    info "IOMMU: not requested (use --iommu to configure passthrough)"
else
    IOMMU_PARAMS="$(iommu_params_for_cpu)"
    if [[ -z "${IOMMU_PARAMS}" ]]; then
        warn "Unrecognized CPU vendor — cannot pick IOMMU kernel parameters; skipping"
    elif [[ -f /etc/default/grub ]]; then
        info "Target: ${IOMMU_PARAMS} (GRUB)"
        configure_iommu_grub
    elif [[ -f /etc/kernel/cmdline ]]; then
        info "Target: ${IOMMU_PARAMS} (systemd-boot)"
        configure_iommu_kernel_cmdline
    else
        warn "No /etc/default/grub or /etc/kernel/cmdline found"
        warn "Add these to your kernel command line manually: ${IOMMU_PARAMS}"
        IOMMU_STATUS="manual"
    fi

    if grep -qw iommu=pt /proc/cmdline 2>/dev/null && [[ -d /sys/class/iommu ]] && [[ -n "$(ls -A /sys/class/iommu 2>/dev/null)" ]]; then
        ok "IOMMU is already active in passthrough mode on the running kernel"
    elif [[ "${IOMMU_STATUS}" != "skipped" ]]; then
        info "IOMMU passthrough takes effect after the next reboot"
        warn "IOMMU must also be enabled in BIOS/UEFI (VT-d / AMD-Vi / SVM)"
    fi
fi

echo ""
echo -e "${CYAN}╔════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║               cmpunlocker              ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════╝${NC}"
echo ""
if (( GPU_COUNT > 0 )); then
    echo "Installed for ${GPU_COUNT} GPU(s): ${COUNT_8GB}x 8gb, ${COUNT_10GB}x 10gb"
else
    echo "Installed with no GPU present — put the card back, then cold boot"
fi
if [[ -n "${MCLK_NDIV}" ]]; then
    echo "MCLK: NDIV=${MCLK_NDIV} ($((MCLK_NDIV * 27)) MHz)"
fi
if [[ -n "${MCLK_TIMINGS}" ]]; then
    echo "DRAM timings: ${MCLK_TIMINGS}% (verify: sudo dmesg | grep TIMING_SCALE)"
fi
echo ""
echo "Next:"
echo -e "  1. Cold reboot: ${CYAN}sudo shutdown -h now${NC}  (then power on)"
echo -e "  2. Benchmark: ${CYAN}./benchmark/nvidia_bench${NC}"
echo -e "  3. Unlock logs: ${CYAN}sudo dmesg | grep CMPUNLOCK${NC}"
if [[ -n "${IOMMU_PARAMS}" && "${IOMMU_STATUS}" != "skipped" ]]; then
    echo -e "  4. Verify IOMMU: ${CYAN}cat /proc/cmdline${NC}"
fi
echo ""
echo "Log: ${LOG_FILE}"
echo ""
