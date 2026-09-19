#!/usr/bin/env python3
"""hbmtune: per-card HBM clock tuning for cmpunlocker builds with --mclk-percard.

Every card gets its own PLL multiplier (clock = NDIV x 27 MHz) and, optionally, its own timing percentage, written to
/etc/modprobe.d/cmp-hbmtune.conf as per-device registry keys. The value of a card comes from, in this order:

  1. a MANUAL override   hbmtune set <card> --ndiv N [--timings P] [--lock]   (you know better than the test)
  2. the AUTO result     hbmtune auto start                                   (searched and soaked by the gates)
  3. the baseline        baseline_ndiv in /etc/hbmtune/hbmtune.conf           (0 = leave the VBIOS clock)

A card is named by index (1), PCI address (0000:41:00.0) or UUID. State is keyed by UUID, so a card keeps its values
when it moves to another slot.

Commands (root):
  status                         what each card runs now, what is configured, where it came from, last test
  set <card> [--ndiv N] [--timings P] [--lock] [--note ..] [--force]      unset | lock | unlock <card>
  apply                          write the modprobe file from the effective values (takes effect at the next driver load)
  test [--minutes M]             run the gates at the clocks that are live now and record the result per card
  auto start|resume|status|abort automatic search: baseline check, climb, bisect, margin, soak; survives reboots
  safe on|off                    next driver load ignores every memory tunable (same as cmpMclkSafe=1 at the boot loader)
  selfcheck                      prove that the gates catch a single flipped bit on this machine

If a clock ever stops the machine from booting: add  nvidia.NVreg_RegistryDwords=cmpMclkSafe=1  to the kernel command
line for one boot, then run `hbmtune auto abort` or `hbmtune set`.
"""
import argparse, json, os, re, shlex, subprocess, sys, time

ROOT = os.environ.get("HBMTUNE_ROOT", "")                 # tests point this at a scratch directory
CONF = ROOT + "/etc/hbmtune/hbmtune.conf"
STATE = ROOT + "/var/lib/hbmtune/state.json"
MODPROBE = ROOT + "/etc/modprobe.d/cmp-hbmtune.conf"
HERE = os.path.dirname(os.path.abspath(__file__))
NDIV_MIN, NDIV_MAX = 30, 80

DEFAULTS = {
    "baseline_ndiv": 0,          # value for cards without manual or auto value; 0 = leave the VBIOS clock
    "min_ndiv": 54, "max_ndiv": 74, "step": 2, "margin": 1,
    "quick_minutes": 6.0, "soak_minutes": 45.0, "test_minutes": 10.0, "max_soak_rounds": 4,
    "apply_mode": "reboot",      # reload | reboot | manual   (how a new value becomes live during `auto`)
    "gate_cmd": f"python3 {HERE}/gates.py",
    "gate_ref": "",              # GEMM reference file written at a known-good clock (gates.py --make-ref)
    "extra_gate_cmd": "",        # optional second gate (your own workload); prints the same JSON, a card must pass both
    "preflight_cmd": "",         # must exit 0 before any test (production stopped, fans fine, ...)
    "pre_reload_cmd": "systemctl stop nvidia-persistenced", "post_reload_cmd": "systemctl start nvidia-persistenced",
    # The driver usually loads from the initramfs, which carries its own copy of /etc/modprobe.d. A value that is not in
    # the boot image is not read at boot. auto = update-initramfs / dracut / mkinitcpio, whichever exists; none = skip.
    "initramfs_cmd": "auto",
    "expected_cards": 0,         # 0 = whatever is present at `auto start`
}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_conf():
    c = dict(DEFAULTS)
    try:
        for line in open(CONF):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if k in c:
                    c[k] = type(DEFAULTS[k])(v) if not isinstance(DEFAULTS[k], str) else v
    except FileNotFoundError:
        pass
    return c


def norm_bdf(x):
    """'00000000:41:00.0' | '0000:41:00' -> '0000:41:00.0'"""
    m = re.search(r"([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})(?:\.([0-7]))?$", x.strip())
    if not m:
        raise ValueError(f"not a PCI address: {x}")
    return f"{m.group(1)[-4:]}:{m.group(2)}:{m.group(3)}.{m.group(4) or 0}".lower()


# ================================================================================================ pure search logic
def new_search(lo=None):
    return {"lo": lo, "hi": None, "done": False}


def next_candidate(cs, p):
    """The NDIV this card should try next, or None when its search is finished."""
    if cs["done"]:
        return None
    lo, hi = cs["lo"], cs["hi"]
    if lo is None:                                   # nothing has passed yet
        if hi is None:
            return p["baseline"]
        cand = max(hi - p["step"], p["min_ndiv"])
        return cand if cand < hi else None           # min_ndiv itself failed: nothing left to try
    if hi is None:
        cand = min(lo + p["step"], p["max_ndiv"])
        if p.get("start") and lo == p["baseline"]:    # first move of a search started with --start: jump there
            cand = min(max(cand, p["start"]), p["max_ndiv"])
        return cand if cand > lo else None           # reached the ceiling without a failure
    return (lo + hi) // 2 if hi - lo > 1 else None


def record(cs, ndiv, passed, p):
    if passed:
        cs["lo"] = ndiv if cs["lo"] is None else max(cs["lo"], ndiv)
    else:
        cs["hi"] = ndiv if cs["hi"] is None else min(cs["hi"], ndiv)
        if cs["lo"] is not None and cs["lo"] >= cs["hi"]:
            cs["lo"] = None                          # an earlier pass at or above a failing value does not count any more
    cs["done"] = next_candidate(dict(cs, done=False), p) is None


def chosen(cs, p):
    """Final value for a card: at least `margin` tested-good steps of headroom under the lowest failure, never above
    the best pass. A card that never failed up to max_ndiv is treated as failing at max_ndiv + 1. None = nothing held."""
    if cs["lo"] is None:
        return None
    hi = cs["hi"] if cs["hi"] is not None else p["max_ndiv"] + 1
    return max(min(cs["lo"], hi - 1 - p["margin"]), min(cs["lo"], p["min_ndiv"]))


# ================================================================================================ backend (real machine)
class Backend:
    def __init__(self, conf):
        self.c = conf

    def sh(self, cmd, timeout=None):
        try:
            return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:          # a wedged driver blocks nvidia-smi for ever: report it, do not hang with it
            return subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout} s")

    def discover(self):
        r = self.sh("nvidia-smi --query-gpu=index,uuid,pci.bus_id,name,clocks.mem,memory.total --format=csv,noheader,nounits", 60)
        cards = []
        for l in r.stdout.strip().splitlines():
            f = [x.strip() for x in l.split(",")]
            if len(f) >= 6 and f[1].startswith("GPU-"):
                cards.append({"index": int(f[0]), "uuid": f[1], "bdf": norm_bdf(f[2]), "name": f[3],
                              "mhz": float(f[4]) if f[4].replace(".", "").isdigit() else None, "mem_mib": f[5]})
        return cards

    def driver_results(self):
        """Last 'HBMPLL_OC: RESULT' line per card from this boot's kernel log, plus trouble lines."""
        r = self.sh("journalctl -k -b -o cat 2>/dev/null || dmesg", 60)
        res, trouble = {}, []
        for l in r.stdout.splitlines():
            m = re.search(r"HBMPLL_OC: RESULT pci=(\S+) ndiv=(\d+) mhz=(\d+) lock=(\d) timings_pct=(-?\d+) source=(\w+)", l)
            if m:
                res[norm_bdf(m.group(1))] = {"ndiv": int(m.group(2)), "mhz": int(m.group(3)), "lock": int(m.group(4)),
                                             "timings_pct": int(m.group(5)), "source": m.group(6)}
            elif re.search(r"FBPAs failed to lock|PLL lock timeout|HBMPLL_OC: aborted|NVRM: Xid|fallen off the bus", l):
                trouble.append(l.strip()[-200:])
        return res, trouble[-12:]

    def write_modprobe(self, text):
        os.makedirs(os.path.dirname(MODPROBE), exist_ok=True)
        tmp = MODPROBE + ".tmp"
        with open(tmp, "w") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, MODPROBE)
        self.refresh_boot_image()

    def refresh_boot_image(self):
        """Put the new modprobe file into the boot image. Without this the driver keeps reading the old values at boot."""
        cmd = self.c["initramfs_cmd"].strip()
        if ROOT or cmd == "none":
            return True
        if cmd == "auto":
            cmd = next((c for t, c in (("update-initramfs", "update-initramfs -u"), ("dracut", "dracut --force"),
                                       ("mkinitcpio", "mkinitcpio -P")) if self.sh(f"command -v {t}", 20).returncode == 0), "")
        if not cmd:
            print("note: no initramfs tool found; if your driver loads from the boot image, rebuild it by hand"); return False
        print(f"rebuilding the boot image ({cmd}) so the driver reads the new values at boot ...", flush=True)
        r = self.sh(cmd, 1200)
        if r.returncode != 0:
            print(f"WARNING: '{cmd}' failed (exit {r.returncode}): {(r.stderr or r.stdout)[-300:]}\n"
                  "The new values are NOT in the boot image."); return False
        return True

    def received_perdevice(self):
        """The per-device option string the LOADED driver got (empty = the boot image did not carry our file)."""
        try:
            for l in open("/proc/driver/nvidia/params"):
                if l.startswith("RegistryDwordsPerDevice:"):
                    return l.split(":", 1)[1].strip().strip('"')
        except OSError:
            pass
        return None

    def other_perdevice_files(self):
        d = os.path.dirname(MODPROBE); out = []
        for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            p = os.path.join(d, fn)
            if p != MODPROBE and fn.endswith(".conf") and "NVreg_RegistryDwordsPerDevice" in open(p, errors="replace").read():
                out.append(p)
        return out

    def preflight(self):
        if not self.c["preflight_cmd"]:
            return True, ""
        r = self.sh(self.c["preflight_cmd"], 120)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[-400:]

    def gates(self, minutes, self_test=False):
        cmd = f"{self.c['gate_cmd']} --minutes {minutes}" + (f" --ref {shlex.quote(self.c['gate_ref'])}" if self.c["gate_ref"] and not self_test else "") \
              + (" --self-test" if self_test else "")
        doc = self._gate_doc(cmd, int(minutes * 60 + 4200))
        if self.c["extra_gate_cmd"] and not self_test and not doc.get("error") and not doc.get("thermal_stop"):
            extra = self._gate_doc(self.c["extra_gate_cmd"], 3600)
            if extra.get("error"):
                return {"error": "extra gate: " + extra["error"]}
            verdict = {c.get("uuid"): c for c in extra.get("cards", [])}
            for c in doc.get("cards", []):
                e = verdict.get(c.get("uuid"))
                c["extra_gate"] = e.get("verdict") if e else "missing"
                if c["verdict"] == "pass" and c["extra_gate"] != "pass":
                    c["verdict"] = "fail"; c["error"] = "extra gate: " + str((e or {}).get("error") or c["extra_gate"])
            doc["all_pass"] = all(c["verdict"] == "pass" for c in doc.get("cards", []))
        return doc

    def _gate_doc(self, cmd, timeout):
        try:
            r = self.sh(cmd, timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"timeout after {timeout} s: {cmd[:80]}"}
        for l in reversed(r.stdout.strip().splitlines()):
            if l.startswith("{"):
                try:
                    return json.loads(l)
                except ValueError:
                    break
        return {"error": f"no result document (exit {r.returncode}): {(r.stderr or r.stdout)[-300:]}"}

    def reload_driver(self):
        """Unload and load the driver so the new registry keys are read. False = could not, use a reboot."""
        had = [m for m in ("nvidia_peermem", "nvidia_uvm", "nvidia_drm", "nvidia_modeset", "nvidia") if os.path.isdir(f"/sys/module/{m}")]
        if self.c["pre_reload_cmd"]:
            self.sh(self.c["pre_reload_cmd"], 120)
        ok = False
        for _ in range(10):
            for m in had:
                if os.path.isdir(f"/sys/module/{m}"):
                    self.sh(f"rmmod {m}", 60)
            ok = not os.path.isdir("/sys/module/nvidia")
            if ok:
                break
            time.sleep(3)
        if ok:
            ok = self.sh("modprobe nvidia", 300).returncode == 0
            for m in had:
                if m not in ("nvidia", "nvidia_peermem"):
                    self.sh(f"modprobe {m}", 120)
        if self.c["post_reload_cmd"]:
            self.sh(self.c["post_reload_cmd"], 120)
        time.sleep(5)
        return ok

    def reboot(self):
        self.sh("systemctl reboot", 60)

    def boot_id(self):
        try:
            return open("/proc/sys/kernel/random/boot_id").read().strip()
        except OSError:
            return "?"


# ================================================================================================ state
def load_state():
    try:
        return json.load(open(STATE))
    except FileNotFoundError:
        return {"version": 1, "cards": {}, "session": None, "safe": False}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, STATE)


def sync_cards(st, found):
    for c in found:
        e = st["cards"].setdefault(c["uuid"], {"manual": None, "auto": None, "history": []})
        e.update(bdf=c["bdf"], index=c["index"], name=c["name"])
    return st


def find_card(st, key):
    key = key.strip()
    if not st["cards"]:
        raise SystemExit("no card is known yet and nvidia-smi gave no list (driver not loaded, or wedged by a faulted card). "
                         "Try again after the next boot.")
    for uuid, e in st["cards"].items():
        if key == uuid or key == str(e.get("index")) or (":" in key and norm_bdf(key) == e.get("bdf")):
            return uuid
    raise SystemExit(f"no card '{key}' (use an index, a PCI address or a UUID from `hbmtune status`)")


def effective(st, conf, uuid, vector=None):
    """-> (ndiv, timings_pct, source). A search vector beats everything for the cards it names."""
    e = st["cards"][uuid]; man = e.get("manual") or {}
    tp = man.get("timings_pct") or 100
    if vector and uuid in vector:
        return vector[uuid], tp, "search"
    if man.get("ndiv") is not None:
        return man["ndiv"], tp, "manual"
    if e.get("auto") and e["auto"].get("chosen") is not None:
        return e["auto"]["chosen"], tp, "auto"
    return conf["baseline_ndiv"], tp, "baseline"


def is_live(result, want):
    """Does the driver's RESULT line say this card runs what we asked for? 0 = "leave the VBIOS clock": then the proof is
    source=stock (the PLL was not touched); the ndiv field is not used, because on some cards the first FBPA's PLL
    register does not read back."""
    if not result:
        return False
    if want == 0:
        return result.get("source") in ("stock", "safe", "skipped")
    return result.get("ndiv") == want and result.get("source") not in ("stock", "safe", "skipped")


def render_modprobe(st, conf, vector=None):
    parts, lines = [], []
    for uuid, e in sorted(st["cards"].items(), key=lambda kv: kv[1].get("bdf", "")):
        if not e.get("bdf"):
            continue
        ndiv, tp, src = effective(st, conf, uuid, vector)
        keys = ["cmpMclkSafe=1"] if st.get("safe") else [f"cmpMclkNdiv={ndiv}"] + ([f"cmpMclkTimingsPct={tp}"] if tp != 100 else []) \
            + (["cmpMclkBroadcast=1"] if (e.get("manual") or {}).get("broadcast") or e.get("broadcast") else [])
        parts.append(f"pci={e['bdf']};" + ";".join(keys))
        lines.append(f"#   {e['bdf']}  NDIV {ndiv:>2} = {27 * ndiv:>4} MHz  timings {tp:>3} %  ({src})  {uuid}")
    head = ["# Written by hbmtune. Do not edit: use `hbmtune set`, `hbmtune auto`, `hbmtune safe`.",
            f"# {now()}" + ("   SAFE: every memory tunable is ignored at the next driver load" if st.get("safe") else "")] + lines
    return "\n".join(head) + "\n" + (f'options nvidia NVreg_RegistryDwordsPerDevice="{";".join(parts)}"\n' if parts else "")


# ================================================================================================ automatic search
class Auto:
    """One step per call of step(): 'continue' (call again), 'reboot' (the machine goes down; the resume unit calls
    step() again after boot), 'wait' (a human has to power-cycle), 'done', or 'stopped'."""

    def __init__(self, be, conf, st):
        self.be, self.c, self.st = be, conf, st

    def log(self, msg):
        self.st["session"]["log"].append(f"{now()} {msg}"); self.st["session"]["log"] = self.st["session"]["log"][-200:]
        print(msg, flush=True)

    def params(self):
        return self.st["session"]["params"]

    def start(self, cards_arg=None, start_at=None):
        found = self.be.discover(); sync_cards(self.st, found)
        if not found:
            raise SystemExit("no GPU visible")
        res, _ = self.be.driver_results()
        if not res:
            raise SystemExit("the driver logged no 'HBMPLL_OC: RESULT' line: install the fork with --mclk-percard first")
        p = {k: self.c[k] for k in ("min_ndiv", "max_ndiv", "step", "margin", "quick_minutes", "soak_minutes", "max_soak_rounds", "apply_mode")}
        p["start"] = start_at
        sess = {"id": now(), "phase": "baseline", "iteration": 0, "params": p, "cards": {}, "pending": None, "solo": [],
                "known_good": {}, "soak_rounds": 0, "log": [], "expected": self.c["expected_cards"] or len(found)}
        want = [find_card(self.st, k) for k in cards_arg] if cards_arg else [c["uuid"] for c in found]
        for c in found:
            e = self.st["cards"][c["uuid"]]; live = res.get(c["bdf"], {}).get("ndiv")
            stock = res.get(c["bdf"], {}).get("source") in ("stock", "safe")
            sess["known_good"][c["uuid"]] = 0 if stock else (live if live else effective(self.st, self.c, c["uuid"])[0])
            if c["uuid"] in want and not (e.get("manual") or {}).get("locked"):
                p_card = dict(p, baseline=sess["known_good"][c["uuid"]])
                # --start N: the live value is taken as proven (it was tested before this session), so the first step goes
                # straight to N instead of re-testing the baseline and climbing one step at a time.
                sess["cards"][c["uuid"]] = dict(new_search(lo=p_card["baseline"] if start_at else None), baseline=p_card["baseline"])
        if not sess["cards"]:
            raise SystemExit("every card is locked: nothing to search")
        if start_at:
            sess["phase"] = "search"
        self.st["session"] = sess
        self.log(f"auto session for {len(sess['cards'])} card(s); the values live now count as known good: "
                 + ", ".join(f"{self.st['cards'][u]['bdf']}={v}" for u, v in sess["known_good"].items()))
        save_state(self.st)

    def card_p(self, uuid):
        return dict(self.params(), baseline=self.st["session"]["cards"][uuid]["baseline"])

    def plan_vector(self):
        """Next value per searching card. In solo mode only the head of the solo list moves."""
        s = self.st["session"]; vec, raised = dict(s["known_good"]), []
        movers = [u for u in s["cards"] if not s["cards"][u]["done"]]
        if s["solo"]:
            movers = [u for u in s["solo"] if u in movers][:1]
        for u in movers:
            cand = next_candidate(s["cards"][u], self.card_p(u))
            if cand is not None:
                vec[u] = cand; raised.append(u)
        return vec, raised

    def fail_card(self, uuid, ndiv, why):
        s = self.st["session"]; record(s["cards"][uuid], ndiv, False, self.card_p(uuid))
        self.st["cards"][uuid]["history"].append({"when": now(), "ndiv": ndiv, "result": "fail", "why": why})
        self.log(f"  {self.st['cards'][uuid]['bdf']} NDIV {ndiv} ({27 * ndiv} MHz): FAIL ({why})")

    def pass_card(self, uuid, ndiv, info):
        s = self.st["session"]; record(s["cards"][uuid], ndiv, True, self.card_p(uuid)); s["known_good"][uuid] = max(s["known_good"].get(uuid, 0), ndiv)
        self.st["cards"][uuid]["history"].append(dict({"when": now(), "ndiv": ndiv, "result": "pass"}, **info))
        self.log(f"  {self.st['cards'][uuid]['bdf']} NDIV {ndiv} ({27 * ndiv} MHz): pass {info}")

    def unattributed_failure(self, why):
        """The machine (or the whole gate run) died with several cards raised: nobody can be blamed yet."""
        s = self.st["session"]; pend = s["pending"]; raised = pend["raised"]
        if len(raised) == 1:
            self.fail_card(raised[0], pend["vector"][raised[0]], why)
            s["solo"] = [u for u in s["solo"] if u != raised[0]]      # its solo turn is over
        elif len(raised) > 1:
            s["solo"] = list(raised)
            self.log(f"  {why} with {len(raised)} cards raised: testing them one at a time from here")
        s["solo"] = [u for u in s["solo"] if not s["cards"][u]["done"]]

    def step(self):
        s = self.st["session"]
        if not s or s["phase"] in ("done", "aborted"):
            return "done"
        found = self.be.discover(); sync_cards(self.st, found); res, trouble = self.be.driver_results()
        pend = s["pending"]

        # ---------- A. nothing in flight: decide the next vector and make it live
        if pend is None:
            if s["phase"] == "soak":
                vec, raised = {u: self.final_value(u) for u in s["cards"]}, []
                vec = dict(s["known_good"], **{u: v for u, v in vec.items() if v is not None})
            else:
                vec, raised = self.plan_vector()
                if not raised and s["phase"] in ("baseline", "search"):
                    if s["phase"] == "baseline":
                        pass                                  # baseline vector = known good, still has to be tested once
                    else:
                        s["phase"] = "soak"; save_state(self.st); return "continue"
            s["iteration"] += 1
            s["pending"] = {"vector": vec, "raised": raised, "stage": "applied", "boots": 0, "when": now(), "boot_id": self.be.boot_id()}
            self.be.write_modprobe(render_modprobe(self.st, self.c, vec)); save_state(self.st)
            self.log(f"iteration {s['iteration']} ({s['phase']}): " + ", ".join(f"{self.st['cards'][u]['bdf']}={v}" for u, v in sorted(vec.items())))
            live_ok = all(is_live(res.get(self.st["cards"][u]["bdf"], {}), v) for u, v in vec.items() if self.st["cards"][u].get("bdf"))
            if live_ok:
                return "continue"                             # already live (first baseline iteration)
            return self.make_live()

        # ---------- B. a vector is in flight
        if pend["boot_id"] != self.be.boot_id():
            pend["boots"] += 1; pend["boot_id"] = self.be.boot_id(); save_state(self.st)
        if self.st.get("safe") or any(r.get("source") == "safe" for r in res.values()):
            self.unattributed_failure("booted in safe mode"); return self.rollback("safe mode: back to the known-good values")
        if pend["stage"] == "testing":                        # we were inside the gates when the machine went away
            self.unattributed_failure("machine went down during the gates"); return self.rollback(None)
        if len(found) < s["expected"]:
            missing = [u for u in pend["vector"] if u not in {c["uuid"] for c in found}]
            for u in [m for m in missing if m in pend["raised"]]:
                self.fail_card(u, pend["vector"][u], "card missing after the driver load")
            if not any(m in pend["raised"] for m in missing):
                self.unattributed_failure(f"{s['expected'] - len(found)} card(s) missing after the driver load")
            return self.rollback(None)
        not_live = [u for u, v in pend["vector"].items() if not is_live(res.get(self.st["cards"][u]["bdf"], {}), v)]
        if not_live:
            unlocked = [u for u in not_live if res.get(self.st["cards"][u]["bdf"], {}).get("lock") == 0]
            if pend["boots"] >= 1 and s["params"]["apply_mode"] != "manual" and not unlocked:
                s["params"]["apply_mode"] = {"reload": "reboot", "reboot": "manual"}[s["params"]["apply_mode"]]
                self.log(f"  values not live after {pend['boots']} driver load(s); escalating to apply_mode={s['params']['apply_mode']}")
                save_state(self.st); return self.make_live()
            if pend["boots"] >= 2 or unlocked:
                for u in [x for x in not_live if x in pend["raised"]]:
                    self.fail_card(u, pend["vector"][u], "PLL did not lock" if u in unlocked else "value did not become live")
                return self.rollback(None)
            return self.make_live()
        nolock = [u for u in pend["raised"] if res.get(self.st["cards"][u]["bdf"], {}).get("lock") == 0]
        if nolock:                                            # a clock that is not locked is not worth testing: plan again
            for u in nolock:
                self.fail_card(u, pend["vector"][u], "PLL did not lock")
            return self.rollback(None)
        ok, why = self.be.preflight()
        if not ok:
            self.log(f"preflight refused: {why}"); save_state(self.st); return "stopped"
        pend["stage"] = "testing"; save_state(self.st)
        minutes = self.params()["soak_minutes"] if s["phase"] == "soak" else self.params()["quick_minutes"]
        doc = self.be.gates(minutes)
        if doc.get("thermal_stop"):
            pend["stage"] = "applied"; self.log(f"gates stopped for temperature ({doc['thermal_stop']}): no verdict; fix the cooling, then `hbmtune auto resume`")
            save_state(self.st); return "stopped"
        if doc.get("error") or not doc.get("cards"):
            pend["stage"] = "applied"; self.unattributed_failure(f"gates could not run: {doc.get('error')}"); return self.rollback(None)
        by_uuid = {c.get("uuid"): c for c in doc["cards"]}
        for u, v in pend["vector"].items():
            g = by_uuid.get(u)
            if u not in s["cards"]:
                continue
            if g is None:
                self.fail_card(u, v, "card not seen by the gates"); continue
            info = {"minutes": minutes, "hbm_max": g.get("hbm_max"), "copy_gbps": g.get("copy_gbps"), "gib": g.get("gib")}
            if g["verdict"] == "pass":
                if s["phase"] == "soak":
                    self.st["cards"][u]["history"].append(dict({"when": now(), "ndiv": v, "result": "pass", "why": "soak"}, **info))
                    self.log(f"  {self.st['cards'][u]['bdf']} NDIV {v}: soak pass {info}")
                else:
                    self.pass_card(u, v, info)
            else:
                why = g.get("error") or f"pattern={g.get('pattern_errors')} hammer={g.get('hammer_errors')} gemm={g.get('gemm_mismatch')} ref={g.get('ref_mismatch')}"
                if s["phase"] == "soak":
                    cs = s["cards"][u]; cs["hi"] = v if cs["hi"] is None else min(cs["hi"], v)
                    cs["lo"] = None if cs["lo"] is None else min(cs["lo"], v - 1)
                    if cs["lo"] is not None and cs["lo"] < self.params()["min_ndiv"]:
                        cs["lo"] = None
                    s["known_good"][u] = min(s["known_good"][u], cs["lo"]) if cs["lo"] else s["cards"][u]["baseline"]
                    self.st["cards"][u]["history"].append({"when": now(), "ndiv": v, "result": "fail", "why": "soak: " + why})
                    self.log(f"  {self.st['cards'][u]['bdf']} NDIV {v}: SOAK FAIL ({why})")
                else:
                    self.fail_card(u, v, why)
        s["pending"] = None
        if s["solo"]:
            s["solo"] = [u for u in s["solo"][1:] if not s["cards"][u]["done"]]
        if s["phase"] == "baseline":
            s["phase"] = "search"
        elif s["phase"] == "soak":
            failed = [u for u, v in pend["vector"].items() if u in s["cards"] and by_uuid.get(u, {}).get("verdict") != "pass"]
            s["soak_rounds"] += 1
            if not failed or s["soak_rounds"] >= self.params()["max_soak_rounds"]:
                return self.finish(failed)
        save_state(self.st)
        return "continue"

    def final_value(self, uuid):
        return chosen(self.st["session"]["cards"][uuid], self.card_p(uuid))

    def finish(self, still_failing):
        s = self.st["session"]
        for u, cs in s["cards"].items():
            val = None if u in still_failing else self.final_value(u)
            self.st["cards"][u]["auto"] = {"chosen": val if val is not None else s["cards"][u]["baseline"], "best_pass": cs["lo"], "first_fail": cs["hi"],
                                           "margin": self.params()["margin"], "when": now(), "soak_minutes": self.params()["soak_minutes"],
                                           "note": "" if val is not None else "nothing held: left at the value it had before the search"}
        s["phase"] = "done"; s["pending"] = None
        self.be.write_modprobe(render_modprobe(self.st, self.c)); save_state(self.st)
        self.log("search finished: " + ", ".join(f"{self.st['cards'][u]['bdf']}={self.st['cards'][u]['auto']['chosen']}" for u in s["cards"]))
        return "done"

    def rollback(self, why):
        """Forget the vector in flight; the next step plans again from what is known."""
        s = self.st["session"]
        if why:
            self.log(why)
        s["pending"] = None; self.st["safe"] = False
        self.be.write_modprobe(render_modprobe(self.st, self.c, dict(s["known_good"]))); save_state(self.st)
        return "continue"

    def make_live(self):
        mode = self.params()["apply_mode"]; save_state(self.st)
        if mode == "reload":
            if self.be.reload_driver():
                self.st["session"]["pending"]["boots"] += 1; save_state(self.st); return "continue"
            self.log("  driver reload did not work; using a reboot"); self.params()["apply_mode"] = "reboot"; save_state(self.st)
            mode = "reboot"
        if mode == "reboot":
            self.log("  rebooting to make the values live"); save_state(self.st); self.be.reboot(); return "reboot"
        self.log("  POWER-CYCLE NEEDED: shut down, wait 30 s, power on. The search continues by itself after the boot.")
        return "wait"

    def abort(self):
        s = self.st["session"]
        if s:
            s["phase"] = "aborted"; s["pending"] = None
            self.be.write_modprobe(render_modprobe(self.st, self.c)); save_state(self.st)


# ================================================================================================ commands
def cmd_status(be, conf, st):
    found = be.discover(); sync_cards(st, found); res, trouble = be.driver_results()
    live = {c["uuid"]: c for c in found}
    print(f"{'idx':>3} {'pci':<13} {'live':>12} {'configured':>16} {'source':<9} {'lock':<5} last test")
    for uuid, e in sorted(st["cards"].items(), key=lambda kv: kv[1].get("index", 99)):
        ndiv, tp, src = effective(st, conf, uuid)
        r = res.get(e.get("bdf"), {}); c = live.get(uuid)
        livetxt = f"{r['ndiv']}={r['mhz']}MHz" if r else (f"{c['mhz']:.0f}MHz" if c and c["mhz"] else "absent")
        h = e["history"][-1] if e["history"] else None
        print(f"{e.get('index', '?'):>3} {e.get('bdf', '?'):<13} {livetxt:>12} {f'{ndiv}={27 * ndiv}MHz t{tp}%':>16} {src:<9} "
              f"{'yes' if (e.get('manual') or {}).get('locked') else '-':<5} " + ("[broadcast] " if e.get("broadcast") else "")
              + (f"{h['when']} NDIV {h['ndiv']} {h['result']} {h.get('why', '')}" if h else "-"))
    if st.get("safe"):
        print("SAFE is on: the next driver load ignores every memory tunable")
    got = be.received_perdevice() if hasattr(be, "received_perdevice") else None
    if got == "" and os.path.exists(MODPROBE):
        print("WARNING: the loaded driver received NO per-device options, so the values above were not read at this boot. "
              "The boot image is older than the modprobe file: run `hbmtune apply`, then reboot.")
    s = st.get("session")
    if s:
        print(f"auto session {s['id']}: phase {s['phase']}, iteration {s['iteration']}, mode {s['params']['apply_mode']}"
              + (f", in flight: {s['pending']['stage']}" if s.get("pending") else ""))
    for t in trouble:
        print("kernel:", t)
    other = be.other_perdevice_files()
    if other:
        print("WARNING: NVreg_RegistryDwordsPerDevice is also set in", ", ".join(other), "- the last file read wins")
    save_state(st)


def cmd_set(be, conf, st, a):
    sync_cards(st, be.discover()); uuid = find_card(st, a.card); e = st["cards"][uuid]
    man = e.get("manual") or {"ndiv": None, "timings_pct": None, "locked": False, "note": ""}
    if a.ndiv is not None:
        if a.ndiv != 0 and not NDIV_MIN <= a.ndiv <= NDIV_MAX:
            raise SystemExit(f"--ndiv must be 0 (VBIOS clock) or {NDIV_MIN}..{NDIV_MAX}")
        fails = [h["ndiv"] for h in e["history"] if h["result"] == "fail"]
        if a.ndiv and fails and a.ndiv >= min(fails) and not a.force:
            raise SystemExit(f"this card FAILED the gates at NDIV {min(fails)}; add --force if you are sure the test was wrong")
        if a.ndiv > conf["max_ndiv"] and not a.force:
            raise SystemExit(f"above max_ndiv={conf['max_ndiv']}; add --force if you mean it")
        man["ndiv"] = a.ndiv
    if a.timings is not None:
        if not 50 <= a.timings <= 150:
            raise SystemExit("--timings is a percentage of the stock table: 50..150 (100 = stock, 120 = 20 % looser)")
        if a.timings < 100 and not a.force:
            raise SystemExit("tightening can hang the card until a reboot; add --force if you mean it")
        man["timings_pct"] = a.timings
    if a.broadcast is not None:
        # card-level property, kept when the manual value is unset: its populated HBM sites do not answer unicast access
        e["broadcast"] = (a.broadcast == "on")
    if a.lock:
        man["locked"] = True
    if a.note:
        man["note"] = a.note
    man["when"] = now(); e["manual"] = man; save_state(st)
    be.write_modprobe(render_modprobe(st, conf))
    ndiv, tp, src = effective(st, conf, uuid)
    print(f"{e['bdf']}: NDIV {ndiv} ({27 * ndiv} MHz), timings {tp} %, source {src}{', locked' if man['locked'] else ''}"
          f"{', broadcast clock sequence' if e.get('broadcast') else ''}. "
          f"Written to {MODPROBE}; live at the next driver load (reboot).")


def main(argv=None, be=None):
    ap = argparse.ArgumentParser(description="per-card HBM clock tuning for cmpunlocker --mclk-percard", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status"); sub.add_parser("apply"); sub.add_parser("selfcheck")
    p = sub.add_parser("set"); p.add_argument("card"); p.add_argument("--ndiv", type=int); p.add_argument("--timings", type=int)
    p.add_argument("--lock", action="store_true"); p.add_argument("--note", default=""); p.add_argument("--force", action="store_true")
    p.add_argument("--broadcast", choices=["on", "off"], help="clock sequence through the FBPA broadcast aperture for this card "
                   "(for a card whose first FBPA does not answer; `fbpa_dump` shows it)")
    for name in ("unset", "lock", "unlock"):
        sub.add_parser(name).add_argument("card")
    p = sub.add_parser("test"); p.add_argument("--minutes", type=float)
    p = sub.add_parser("auto"); p.add_argument("action", choices=["start", "resume", "status", "abort"]); p.add_argument("--cards", nargs="*")
    p.add_argument("--once", action="store_true", help="one step only (used by tests)")
    p.add_argument("--start", type=int, help="the live values are already proven: skip the baseline test and try this NDIV first")
    p = sub.add_parser("safe"); p.add_argument("state", choices=["on", "off"])
    a = ap.parse_args(argv)
    conf = load_conf(); be = be or Backend(conf); st = load_state()
    if a.cmd != "status" and not ROOT and os.geteuid() != 0:
        raise SystemExit("run with sudo")
    if a.cmd == "status":
        return cmd_status(be, conf, st)
    if a.cmd == "set":
        return cmd_set(be, conf, st, a)
    if a.cmd in ("unset", "lock", "unlock"):
        sync_cards(st, be.discover()); uuid = find_card(st, a.card); e = st["cards"][uuid]
        if a.cmd == "unset":
            e["manual"] = None
        else:
            e["manual"] = dict(e.get("manual") or {"ndiv": None, "timings_pct": None, "note": ""}, locked=(a.cmd == "lock"), when=now())
        save_state(st); be.write_modprobe(render_modprobe(st, conf)); print(f"{e['bdf']}: {a.cmd} done; {MODPROBE} rewritten"); return
    if a.cmd == "apply":
        sync_cards(st, be.discover()); save_state(st); text = render_modprobe(st, conf); be.write_modprobe(text); print(text, end="")
        print(f"written to {MODPROBE}; live at the next driver load (reboot)"); return
    if a.cmd == "safe":
        st["safe"] = a.state == "on"; save_state(st); be.write_modprobe(render_modprobe(st, conf))
        print("safe " + a.state + f": {MODPROBE} rewritten; live at the next driver load"); return
    if a.cmd == "selfcheck":
        doc = be.gates(0.3, self_test=True); ok = bool(doc.get("all_pass"))
        print("gates self-test:", "PASS (a single flipped bit is caught in both phases)" if ok else f"FAIL {doc.get('error', '')}"); return 0 if ok else 1
    if a.cmd == "test":
        sync_cards(st, be.discover()); ok, why = be.preflight()
        if not ok:
            raise SystemExit(f"preflight refused: {why}")
        res, _ = be.driver_results(); minutes = a.minutes or conf["test_minutes"]; doc = be.gates(minutes)
        for g in doc.get("cards", []):
            e = st["cards"].get(g.get("uuid"))
            if e and g["verdict"] in ("pass", "fail"):
                ndiv = res.get(e["bdf"], {}).get("ndiv") or round((g.get("mem_clock_mhz") or 0) / 27)
                e["history"].append({"when": now(), "ndiv": ndiv, "result": g["verdict"], "minutes": minutes, "hbm_max": g.get("hbm_max"),
                                     "copy_gbps": g.get("copy_gbps"), "why": g.get("error") or "manual test"})
                print(f"{e['bdf']} NDIV {ndiv}: {g['verdict'].upper()}  copy {g.get('copy_gbps')} GB/s, HBM max {g.get('hbm_max')} C")
        save_state(st); return 0 if doc.get("all_pass") else 1
    auto = Auto(be, conf, st)
    if a.action == "status":
        s = st.get("session")
        print("\n".join(s["log"][-40:]) if s else "no auto session"); return
    if a.action == "abort":
        auto.abort(); print("auto session aborted; modprobe file rewritten from the manual/auto/baseline values"); return
    if a.action == "start":
        if st.get("session") and st["session"]["phase"] not in ("done", "aborted"):
            raise SystemExit("an auto session is already active: `hbmtune auto resume` or `hbmtune auto abort`")
        ok, why = be.preflight()
        if not ok:
            raise SystemExit(f"preflight refused: {why}")
        if a.start is not None and not (NDIV_MIN <= a.start <= conf["max_ndiv"]):
            raise SystemExit(f"--start must be between {NDIV_MIN} and max_ndiv={conf['max_ndiv']}")
        auto.start(a.cards, a.start)
    while True:
        r = auto.step()
        if r != "continue" or a.once:
            print(f"[{r}]"); return r


if __name__ == "__main__":
    r = main()
    sys.exit(r if isinstance(r, int) else 0)
