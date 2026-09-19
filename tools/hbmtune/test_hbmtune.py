#!/usr/bin/env python3
"""Simulator tests for hbmtune: a fake machine with four cards of different quality, driver loads, reboots, crashes."""
import json, os, re, sys, tempfile, unittest

TMP = tempfile.mkdtemp(prefix="hbmtune-test-")
os.environ["HBMTUNE_ROOT"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hbmtune as H  # noqa: E402

UU = {k: f"GPU-{k * 8}" for k in "abcd"}
BDF = {"a": "0000:01:00.0", "b": "0000:41:00.0", "c": "0000:81:00.0", "d": "0000:c4:00.0"}


class MachineDown(Exception):
    pass


class FakeBox(H.Backend):
    def __init__(self, conf, limit, live=66, crash_at=None, nolock_at=None, soak_limit=None, reload_works=True,
                 reboot_applies=True, thermal_once=False, missing_at=None):
        super().__init__(conf)
        self.limit, self.crash_at, self.nolock_at, self.soak_limit = limit, crash_at or {}, nolock_at or {}, soak_limit or {}
        self.missing_at = missing_at or {}
        self.live = {k: live for k in limit}; self.text = ""; self.boot = 1; self.safe_boot = False
        self.reload_works, self.reboot_applies, self.thermal_once = reload_works, reboot_applies, thermal_once
        self.gate_runs = []; self.reloads = 0; self.reboots = 0

    # --- what the driver would do at load time
    def load_driver(self):
        if self.safe_boot:
            return
        for k in self.limit:
            m = re.search(re.escape(f"pci={BDF[k]};") + r"([^\"]*?)(?=;pci=|\")", self.text)
            if m and "cmpMclkSafe=1" not in m.group(1):
                n = re.search(r"cmpMclkNdiv=(\d+)", m.group(1))
                if n:
                    self.live[k] = int(n.group(1))

    def power_cycle(self, safe=False):
        self.boot += 1; self.safe_boot = safe; self.load_driver()

    # --- Backend interface
    def discover(self):
        return [{"index": i, "uuid": UU[k], "bdf": BDF[k], "name": "CMP 170HX", "mhz": 27.0 * self.live[k], "mem_mib": "65536"}
                for i, k in enumerate(sorted(self.limit)) if not (k in self.missing_at and self.live[k] >= self.missing_at[k])]

    def driver_results(self):
        res = {}
        for k in self.limit:
            nolock = k in self.nolock_at and self.live[k] >= self.nolock_at[k]
            res[BDF[k]] = {"ndiv": 32 if self.live[k] == 0 else self.live[k], "mhz": 27 * self.live[k], "lock": 0 if nolock else 1, "timings_pct": 100,
                           "source": "safe" if self.safe_boot else ("stock" if self.live[k] == 0 else "percard")}
        return res, []

    def write_modprobe(self, text):
        self.text = text

    def other_perdevice_files(self):
        return []

    def preflight(self):
        return True, ""

    def boot_id(self):
        return f"boot-{self.boot}"

    def reload_driver(self):
        self.reloads += 1
        if self.reload_works:
            self.load_driver()
        return True

    def reboot(self):
        self.reboots += 1; self.boot += 1
        if self.reboot_applies:
            self.load_driver()

    def gates(self, minutes, self_test=False):
        self.gate_runs.append(dict(self.live))
        if self.thermal_once:
            self.thermal_once = False
            return {"thermal_stop": "GPU 0 reached memory 89 C", "cards": []}
        if any(self.live[k] >= v for k, v in self.crash_at.items()):
            self.boot += 1; self.load_driver(); raise MachineDown()
        cards = []
        for k in sorted(self.limit):
            lim = self.soak_limit.get(k, self.limit[k]) if minutes >= 30 else self.limit[k]
            ok = self.live[k] <= lim
            cards.append({"uuid": UU[k], "pci": BDF[k], "verdict": "pass" if ok else "fail", "pattern_errors": 0 if ok else 17,
                          "hammer_errors": 0, "gemm_mismatch": 0, "ref_mismatch": 0, "hbm_max": 68, "copy_gbps": 700 + self.live[k], "gib": 60})
        return {"cards": cards, "all_pass": all(c["verdict"] == "pass" for c in cards)}


def fresh(conf_over=None, **box):
    for f in (H.STATE, H.MODPROBE):
        if os.path.exists(f):
            os.remove(f)
    conf = dict(H.DEFAULTS, baseline_ndiv=66, **(conf_over or {}))
    return conf, FakeBox(conf, **box)


def run_auto(conf, be, max_steps=400, on_wait=None):
    st = H.load_state(); auto = H.Auto(be, conf, st); auto.start(); out = None
    for _ in range(max_steps):
        try:
            out = auto.step()
        except MachineDown:
            auto = H.Auto(be, conf, H.load_state()); continue          # the process died; the resume unit starts a new one
        if out == "wait":
            (on_wait or be.power_cycle)(); auto = H.Auto(be, conf, H.load_state()); continue
        if out == "reboot":
            auto = H.Auto(be, conf, H.load_state()); continue
        if out in ("done", "stopped"):
            break
    st = H.load_state()
    return out, {k: (st["cards"][UU[k]].get("auto") or {}).get("chosen") for k in be.limit}, st


class SearchLogic(unittest.TestCase):
    P = {"baseline": 66, "min_ndiv": 54, "max_ndiv": 74, "step": 2, "margin": 1}

    def walk(self, limit):
        cs = H.new_search(); seen = []
        while True:
            n = H.next_candidate(cs, self.P)
            if n is None:
                break
            seen.append(n); H.record(cs, n, n <= limit, self.P)
            self.assertLess(len(seen), 30)
        return seen, cs, H.chosen(cs, self.P)

    def test_every_limit(self):
        for limit in range(50, 80):
            seen, cs, val = self.walk(limit)
            self.assertEqual(len(seen), len(set(seen)), f"limit {limit}: a value was tested twice: {seen}")
            if limit >= 54:
                self.assertIsNotNone(val); self.assertLessEqual(val, min(limit, 74))
                self.assertLessEqual(val, min(limit, 74) - (1 if limit < 74 else 0) if limit > 54 else 54)
                if 55 <= limit < 74:
                    self.assertEqual(val, limit - 1, f"limit {limit}: tested {seen}")     # one step of tested headroom
            else:
                self.assertIsNone(val, f"limit {limit}: nothing may be chosen when even min_ndiv fails")

    def test_never_failed_still_gets_margin(self):
        self.assertEqual(self.walk(79)[2], 73)


class AutoSession(unittest.TestCase):
    def test_four_different_cards(self):
        conf, be = fresh(limit={"a": 70, "b": 66, "c": 71, "d": 79}, conf_over={"apply_mode": "reload"})
        out, got, st = run_auto(conf, be)
        self.assertEqual(out, "done"); self.assertEqual(got, {"a": 69, "b": 65, "c": 70, "d": 73})
        for k, v in got.items():
            self.assertIn(f"pci={BDF[k]};cmpMclkNdiv={v}", be.text)
        self.assertEqual(be.gate_runs[-1], {"a": 69, "b": 65, "c": 70, "d": 73}, "the soak must run on the final values")
        self.assertTrue(all(be.gate_runs[i][k] <= 74 for i in range(len(be.gate_runs)) for k in "abcd"))

    def test_crash_with_several_cards_raised_goes_solo(self):
        conf, be = fresh(limit={"a": 70, "b": 66, "c": 71, "d": 71}, crash_at={"d": 72}, conf_over={"apply_mode": "reboot"})
        out, got, st = run_auto(conf, be)
        self.assertEqual(out, "done"); self.assertEqual(got, {"a": 69, "b": 65, "c": 70, "d": 70})
        crashes = [r for r in be.gate_runs if r["d"] >= 72]
        self.assertEqual(len(crashes), 2, "one crash with several cards raised, then one that can be blamed on a single card")
        self.assertEqual(sum(1 for k in "abcd" if crashes[0][k] == 72), 3)
        self.assertEqual(crashes[1], {"a": 70, "b": 66, "c": 70, "d": 72}, "the suspects move one at a time, the rest at known-good values")

    def test_reload_that_does_not_apply_escalates_to_reboot(self):
        conf, be = fresh(limit={"a": 70, "b": 66}, reload_works=False, conf_over={"apply_mode": "reload"})
        out, got, st = run_auto(conf, be)
        self.assertEqual((out, got), ("done", {"a": 69, "b": 65})); self.assertGreater(be.reboots, 0)
        self.assertEqual(st["session"]["params"]["apply_mode"], "reboot")

    def test_needs_power_cycle(self):
        conf, be = fresh(limit={"a": 70, "b": 66}, reboot_applies=False, conf_over={"apply_mode": "reboot"})
        waits = []
        out, got, st = run_auto(conf, be, on_wait=lambda: (waits.append(1), be.power_cycle()))
        self.assertEqual((out, got), ("done", {"a": 69, "b": 65})); self.assertGreater(len(waits), 0)
        self.assertEqual(st["session"]["params"]["apply_mode"], "manual")

    def test_pll_that_does_not_lock_is_a_failure_without_gates(self):
        conf, be = fresh(limit={"a": 79}, nolock_at={"a": 72}, conf_over={"apply_mode": "reload"})
        out, got, st = run_auto(conf, be)
        self.assertEqual((out, got), ("done", {"a": 70})); self.assertTrue(all(r["a"] < 72 for r in be.gate_runs))

    def test_baseline_that_fails_goes_down(self):
        conf, be = fresh(limit={"a": 64, "b": 70}, conf_over={"apply_mode": "reload"})
        out, got, st = run_auto(conf, be)
        self.assertEqual((out, got), ("done", {"a": 63, "b": 69}))

    def test_soak_failure_steps_down_and_soaks_again(self):
        conf, be = fresh(limit={"a": 70, "b": 72}, soak_limit={"a": 68}, conf_over={"apply_mode": "reload"})
        out, got, st = run_auto(conf, be)
        self.assertEqual((out, got), ("done", {"a": 67, "b": 71}))
        self.assertEqual(be.gate_runs[-1], {"a": 67, "b": 71})

    def test_safe_boot_rolls_back(self):
        conf, be = fresh(limit={"a": 70, "b": 70}, reboot_applies=True, conf_over={"apply_mode": "manual"})
        st = H.load_state(); auto = H.Auto(be, conf, st); auto.start()
        self.assertEqual(auto.step(), "continue")                        # baseline vector is already live
        self.assertEqual(auto.step(), "continue")                        # baseline gates
        self.assertEqual(auto.step(), "wait")                            # 68/68 written, waiting for the power cycle
        be.power_cycle(safe=True)                                        # the human booted with cmpMclkSafe=1
        auto = H.Auto(be, conf, H.load_state()); self.assertEqual(auto.step(), "continue")
        self.assertIn("cmpMclkNdiv=66", be.text); self.assertNotIn("cmpMclkNdiv=68", be.text)
        self.assertEqual(H.load_state()["session"]["solo"], [UU["a"], UU["b"]])

    def test_thermal_stop_records_nothing(self):
        conf, be = fresh(limit={"a": 70}, thermal_once=True, conf_over={"apply_mode": "reload"})
        st = H.load_state(); auto = H.Auto(be, conf, st); auto.start()
        self.assertEqual(auto.step(), "continue"); self.assertEqual(auto.step(), "stopped")
        self.assertEqual(H.load_state()["cards"][UU["a"]]["history"], [])
        auto = H.Auto(be, conf, H.load_state()); out = None
        for _ in range(100):
            out = auto.step()
            if out == "done":
                break
        self.assertEqual(out, "done"); self.assertEqual(H.load_state()["cards"][UU["a"]]["auto"]["chosen"], 69)

    def test_missing_card_is_blamed(self):
        conf, be = fresh(limit={"a": 79, "b": 70}, missing_at={"a": 72}, conf_over={"apply_mode": "reload"})
        out, got, st = run_auto(conf, be)
        self.assertEqual((out, got), ("done", {"a": 70, "b": 69}))


class ManualOverride(unittest.TestCase):
    def test_set_lock_force_and_safe(self):
        conf, be = fresh(limit={"a": 70, "b": 66})
        open_conf = os.path.join(TMP, "etc/hbmtune"); os.makedirs(open_conf, exist_ok=True)
        open(H.CONF, "w").write("baseline_ndiv = 66\napply_mode = reload\n")
        H.main(["set", "1", "--ndiv", "64", "--lock", "--note", "fails the replay at 68"], be=be)
        self.assertIn(f"pci={BDF['b']};cmpMclkNdiv=64", be.text); self.assertIn(f"pci={BDF['a']};cmpMclkNdiv=66", be.text)
        out = H.main(["auto", "start"], be=be)                           # the locked card is left alone
        st = H.load_state(); self.assertEqual(out, "done")
        self.assertIsNone(st["cards"][UU["b"]].get("auto")); self.assertEqual(st["cards"][UU["a"]]["auto"]["chosen"], 69)
        self.assertIn(f"pci={BDF['b']};cmpMclkNdiv=64", be.text); self.assertIn(f"pci={BDF['a']};cmpMclkNdiv=69", be.text)
        with self.assertRaises(SystemExit):                              # 71 failed in the search: refused without --force
            H.main(["set", "0", "--ndiv", "72"], be=be)
        H.main(["set", "0", "--ndiv", "72", "--force"], be=be); self.assertIn(f"pci={BDF['a']};cmpMclkNdiv=72", be.text)
        H.main(["set", "0", "--timings", "110"], be=be); self.assertIn("cmpMclkNdiv=72;cmpMclkTimingsPct=110", be.text)
        with self.assertRaises(SystemExit):
            H.main(["set", "0", "--timings", "90"], be=be)               # tightening needs --force
        H.main(["unset", "0"], be=be); self.assertIn(f"pci={BDF['a']};cmpMclkNdiv=69", be.text)
        H.main(["safe", "on"], be=be); self.assertIn(f"pci={BDF['a']};cmpMclkSafe=1", be.text); self.assertNotIn("cmpMclkNdiv", be.text.split("options")[1])
        H.main(["safe", "off"], be=be); self.assertIn("cmpMclkNdiv=69", be.text)

    def test_card_pinned_to_the_vbios_clock(self):
        conf, be = fresh(limit={"a": 70, "b": 66})
        os.makedirs(os.path.dirname(H.CONF), exist_ok=True); open(H.CONF, "w").write("baseline_ndiv = 66\napply_mode = reload\n")
        H.main(["set", "1", "--ndiv", "0", "--lock"], be=be)
        self.assertIn(f"pci={BDF['b']};cmpMclkNdiv=0", be.text)
        be.power_cycle(); self.assertEqual(be.live["b"], 0)
        res, _ = be.driver_results()
        self.assertTrue(H.is_live(res[BDF["b"]], 0), "source=stock proves the PLL was left alone even when the register reads 32")
        self.assertFalse(H.is_live({"ndiv": 32, "lock": 0, "source": "build"}, 0), "source=build means the key was not read")
        out = H.main(["auto", "start"], be=be)                             # the pinned card stays out of the search
        self.assertEqual(out, "done"); self.assertIn(f"pci={BDF['b']};cmpMclkNdiv=0", be.text)

    def test_broadcast_flag_is_a_card_property(self):
        conf, be = fresh(limit={"a": 70, "b": 70})
        os.makedirs(os.path.dirname(H.CONF), exist_ok=True); open(H.CONF, "w").write("baseline_ndiv = 66\napply_mode = reload\n")
        H.main(["set", "1", "--ndiv", "66", "--broadcast", "on"], be=be)
        self.assertIn(f"pci={BDF['b']};cmpMclkNdiv=66;cmpMclkBroadcast=1", be.text)
        self.assertNotIn("cmpMclkBroadcast", be.text.split(f"pci={BDF['a']}")[1].split(";pci=")[0])
        H.main(["unset", "1"], be=be)                                       # the value goes, the card property stays
        self.assertIn(f"pci={BDF['b']};cmpMclkNdiv=66;cmpMclkBroadcast=1", be.text)
        H.main(["set", "1", "--broadcast", "off"], be=be); self.assertNotIn("cmpMclkBroadcast", be.text)

    def test_pci_address_forms(self):
        self.assertEqual(H.norm_bdf("00000000:41:00.0"), "0000:41:00.0"); self.assertEqual(H.norm_bdf("0000:C4:00"), "0000:c4:00.0")


if __name__ == "__main__":
    unittest.main(verbosity=1)
