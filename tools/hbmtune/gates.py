#!/usr/bin/env python3
"""hbmtune gates: does the memory still tell the truth at this clock?

A memory overclock fails silently: nothing crashes, the numbers are just wrong. These gates look for wrong numbers on
every visible GPU at once (the cards heat each other, as in production) and report a verdict PER CARD:

  1. pattern sweep   nearly all of VRAM in 1 GiB chunks: seeded random, its complement, an address pattern and a
                     checkerboard. Every chunk is written, left alone while the others are written, then compared
                     with a regenerated copy. Catches stuck, weak and mis-addressed cells.
  2. hammer          the chunks are rotated through each other at full memory bandwidth for the rest of the time,
                     with bit-exact GEMMs in between for heat and for the compute path. Wrapping checksums follow
                     the data; at the end every chunk is compared bit for bit with what it must hold after that
                     many rotations. Copy bandwidth is measured here: it has to go UP with the clock, a flat or
                     falling number means the controller is retrying.
  3. bit-exact GEMM  the same seeded bf16 and fp16 products must give the same bits every time, and (with --ref)
                     the same bits as the reference file written at a known-good clock with --make-ref.

`--self-test` proves the detector: it flips ONE bit in one chunk during each phase and must catch both.

Needs PyTorch with CUDA. Prints one JSON document on the last line of stdout (and to --json). Exit code 0 = every
tested card passed, 1 = at least one failed, 2 = the gates could not run, 3 = stopped for temperature (no verdict).
"""
import argparse, hashlib, json, os, subprocess, sys, time

GIB = 1 << 30
GOLD = -7046029254386353131            # 0x9E3779B97F4A7C15 as int64
CHECKER = (0x5555555555555555, -0x5555555555555556)   # 0x5555.., 0xAAAA.. as int64


def smi(fields, gpus=None):
    cmd = ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"]
    if gpus is not None:
        cmd.insert(1, "-i"); cmd.insert(2, ",".join(map(str, gpus)))
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    return [[c.strip() for c in l.split(",")] for l in out.strip().splitlines() if l.strip()]


def fnum(x):
    try:
        return float(x)
    except ValueError:
        return None


# ----------------------------------------------------------------------------------------------- worker (one per GPU)
def worker(gpu, a, q):
    import torch
    res = {"gpu": gpu, "pattern_errors": 0, "hammer_errors": 0, "gemm_mismatch": 0, "ref_mismatch": 0, "examples": [],
           "chunks": 0, "gib": 0.0, "rotations": 0, "copy_gbps": None, "gemm_runs": 0, "gemm_sha": {}, "injected_caught": {},
           "phase": "init", "error": None}
    try:
        torch.cuda.set_device(gpu); dev = torch.device("cuda", gpu)
        n = int(a.chunk_gib * GIB) // 8
        free, total = torch.cuda.mem_get_info(dev)
        want = int((free - a.reserve_gib * GIB) // (n * 8)) - 2            # two more chunk-sized buffers: scratch copy, index ramp
        if a.max_chunks:
            want = min(want, a.max_chunks)
        if want < 2:
            raise RuntimeError(f"not enough free memory: {free / GIB:.1f} GiB free")
        scratch = torch.empty(n, dtype=torch.int64, device=dev)
        chunks = [torch.empty(n, dtype=torch.int64, device=dev) for _ in range(want)]
        res["chunks"], res["gib"] = want, want * a.chunk_gib
        g = torch.Generator(device=dev)
        idx = torch.arange(n, dtype=torch.int64, device=dev)

        def expect(kind, c, out):
            """Regenerate what chunk c must hold for pattern `kind` into `out`."""
            if kind in ("random", "complement"):
                g.manual_seed(a.seed * 1000003 + c); out.random_(generator=g)
                if kind == "complement":
                    out.bitwise_not_()
            elif kind == "address":
                torch.mul(idx, GOLD, out=out); out.add_(c * n)
            else:                                                       # checkerboard, phase alternates per chunk
                out.fill_(CHECKER[c % 2])

        def compare(kind, c, buf, tag):
            expect(kind, c, scratch)
            if torch.equal(buf, scratch):                       # the common case needs no mask at all
                return 0
            mask = buf != scratch
            bad = int(torch.count_nonzero(mask).item())         # not mask.sum(): that upcasts the mask to int64 (8x its size)
            if bad:
                pos = mask.nonzero()[:4].flatten().tolist()
                for p_ in pos:
                    got, exp = int(buf[p_].item()), int(scratch[p_].item())
                    if len(res["examples"]) < 12:
                        res["examples"].append({"phase": tag, "pattern": kind, "chunk": c, "offset": p_,
                                                "xor": f"{(got ^ exp) & 0xFFFFFFFFFFFFFFFF:016x}"})
            return bad

        def flip_one_bit(buf):
            v = buf[n // 3].item(); buf[n // 3] = v ^ (1 << 17)

        # ---- 1. pattern sweep
        res["phase"] = "pattern"
        kinds = ["random", "complement", "address", "checker"]
        for k_i, kind in enumerate(kinds):
            for c, buf in enumerate(chunks):
                expect(kind, c, buf)
            if a.self_test and k_i == 0:
                flip_one_bit(chunks[want // 2])
            errs = sum(compare(kind, c, buf, "pattern") for c, buf in enumerate(chunks))
            if a.self_test and k_i == 0:
                res["injected_caught"]["pattern"] = errs == 1; errs = 0; res["examples"].clear()
            res["pattern_errors"] += errs
            torch.cuda.synchronize(dev)

        # ---- 2. hammer + 3. GEMM, until the time is up
        res["phase"] = "hammer"
        for c, buf in enumerate(chunks):
            expect("random", c, buf)
        sums = [int(buf.sum().item()) for buf in chunks]
        m = a.gemm_n
        gg = torch.Generator(device=dev); gg.manual_seed(a.seed + 7)
        mats = {}
        for name, dt in (("bf16", torch.bfloat16), ("fp16", torch.float16)):
            x = (torch.rand(m, m, generator=gg, device=dev, dtype=torch.float32) - 0.5).to(dt)
            y = (torch.rand(m, m, generator=gg, device=dev, dtype=torch.float32) - 0.5).to(dt)
            first = x @ y
            mats[name] = (x, y, first)
            res["gemm_sha"][name] = hashlib.sha256(first.view(torch.int16).cpu().numpy().tobytes()).hexdigest()
            if a.ref and a.ref.get(name) and a.ref[name] != res["gemm_sha"][name]:
                res["ref_mismatch"] += 1
        torch.cuda.synchronize(dev)
        t_end = time.time() + a.minutes * 60.0
        copied, t_copy, injected = 0, 0.0, False
        while True:
            t0 = time.time()
            scratch.copy_(chunks[0])
            for c in range(want - 1):
                chunks[c].copy_(chunks[c + 1])
            chunks[want - 1].copy_(scratch)
            torch.cuda.synchronize(dev)
            t_copy += time.time() - t0; copied += (want + 1) * n * 8; res["rotations"] += 1
            sums = sums[1:] + sums[:1]
            if a.self_test and not injected:
                flip_one_bit(chunks[1]); injected = True
            for name, (x, y, first) in mats.items():
                for _ in range(a.gemm_burst):
                    if not torch.equal(x @ y, first):
                        res["gemm_mismatch"] += 1
                    res["gemm_runs"] += 1
            if res["rotations"] % 8 == 0:                               # cheap running check: wrapping sums follow the data
                bad = sum(1 for c, buf in enumerate(chunks) if int(buf.sum().item()) != sums[c])
                if a.self_test and bad:
                    pass                                                # counted by the exact compare below
                elif bad:
                    res["hammer_errors"] += bad
            if time.time() >= t_end:
                break
        res["copy_gbps"] = round(copied / t_copy / 1e9, 1) if t_copy else None
        res["phase"] = "final-compare"
        mats.clear(); del x, y, first; torch.cuda.empty_cache()
        r = res["rotations"] % want
        exact = sum(compare("random", (c + r) % want, buf, "hammer") for c, buf in enumerate(chunks))
        if a.self_test:
            res["injected_caught"]["hammer"] = exact == 1; exact = 0; res["examples"].clear(); res["hammer_errors"] = 0
        res["hammer_errors"] += exact
        res["phase"] = "done"
    except Exception as e:  # noqa: BLE001  (a CUDA fault on a marginal card lands here, or kills the process)
        res["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    q.put(res)


# ----------------------------------------------------------------------------------------------- parent
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gpus", default="all", help="all, or a comma list of nvidia-smi indices, PCI addresses or UUIDs")
    ap.add_argument("--minutes", type=float, default=5.0, help="length of the hammer phase")
    ap.add_argument("--chunk-gib", type=float, default=1.0); ap.add_argument("--reserve-gib", type=float, default=3.0)
    ap.add_argument("--max-chunks", type=int, default=0); ap.add_argument("--gemm-n", type=int, default=8192)
    ap.add_argument("--gemm-burst", type=int, default=2); ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--ref", help="JSON file with GEMM reference hashes from a known-good clock")
    ap.add_argument("--make-ref", help="write the GEMM hashes of this run to this file (only when every card passed and agrees)")
    ap.add_argument("--json", help="also write the result document here"); ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--max-hbm-c", type=float, default=88.0, help="stop the run when any tested card's memory gets this hot")
    ap.add_argument("--max-core-c", type=float, default=85.0)
    a = ap.parse_args()
    try:
        import torch, torch.multiprocessing as mp
        ngpu = torch.cuda.device_count()
        if ngpu == 0:
            raise RuntimeError("no CUDA device visible")
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"gates cannot run: {e}"})); return 2
    # CUDA ordinals are NOT nvidia-smi indices: a faulted card drops out of CUDA's list and the ones after it move up.
    # Identity therefore comes from the UUID CUDA reports for each ordinal; nvidia-smi rows are matched by UUID.
    cuda = {}
    for i in range(ngpu):
        try:
            cuda[i] = "GPU-" + str(torch.cuda.get_device_properties(i).uuid)
        except Exception:  # noqa: BLE001  (ordinal that CUDA counts but cannot open)
            pass
    rows = {r[1]: r for r in smi("index,uuid,pci.bus_id,name,clocks.mem")}
    want = None if a.gpus == "all" else {x.strip() for x in a.gpus.split(",")}       # smi indices, PCI addresses or UUIDs
    gpus = [o for o, u in cuda.items() if want is None or u in want or (u in rows and (rows[u][0] in want or rows[u][2][-12:].lower() in {w.lower()[-12:] for w in want}))]
    if not gpus:
        print(json.dumps({"error": f"no usable CUDA device matches --gpus {a.gpus}; CUDA sees {list(cuda.values())}"})); return 2
    a.ref = json.load(open(a.ref)).get("gemm_sha") if a.ref else None
    if a.self_test:
        a.minutes, a.max_chunks = min(a.minutes, 0.3), a.max_chunks or 4
    ctx = mp.get_context("spawn"); q = ctx.Queue()
    procs = {g: ctx.Process(target=worker, args=(g, a, q), daemon=True) for g in gpus}
    ident = {o: {"uuid": cuda[o], "smi_index": int(rows[cuda[o]][0]) if cuda[o] in rows else None,
                 "pci": rows[cuda[o]][2][-12:].lower() if cuda[o] in rows else None, "name": rows[cuda[o]][3] if cuda[o] in rows else None,
                 "mem_clock_mhz": fnum(rows[cuda[o]][4]) if cuda[o] in rows else None} for o in gpus}
    smi_to_ord = {v["smi_index"]: o for o, v in ident.items() if v["smi_index"] is not None}
    absent = [r for u, r in rows.items() if u not in cuda.values()]
    t0 = time.time()
    for p in procs.values():
        p.start()
    temps = {g: {"core_max": None, "hbm_max": None, "w_max": None} for g in gpus}
    results, deadline = {}, t0 + a.minutes * 60 + 900 + 40 * 60 * (0 if a.max_chunks else 1)
    thermal = None
    while len(results) < len(gpus) and time.time() < deadline:
        try:
            r = q.get(timeout=5); results[r["gpu"]] = r
        except Exception:  # noqa: BLE001  (queue.Empty)
            pass
        for row in smi("index,temperature.gpu,temperature.memory,power.draw"):
            g = smi_to_ord.get(int(row[0]))
            if g in temps:
                for key, v in (("core_max", fnum(row[1])), ("hbm_max", fnum(row[2])), ("w_max", fnum(row[3]))):
                    if v is not None and (temps[g][key] is None or v > temps[g][key]):
                        temps[g][key] = v
                if (fnum(row[2]) or 0) >= a.max_hbm_c or (fnum(row[1]) or 0) >= a.max_core_c:
                    thermal = f"GPU {g} reached core {row[1]} C / memory {row[2]} C (limits {a.max_core_c:.0f} / {a.max_hbm_c:.0f})"
        if thermal:
            break
        for g, p in procs.items():
            if g not in results and not p.is_alive() and p.exitcode is not None:
                time.sleep(1)
                if q.empty():
                    results[g] = {"gpu": g, "error": f"worker died, exit code {p.exitcode}", "phase": "crashed"}
    for g, p in procs.items():
        if g not in results:
            results[g] = {"gpu": g, "error": ("thermal stop" if thermal else "timeout: worker did not finish"),
                          "phase": ("thermal" if thermal else "hung")}
        if p.is_alive():
            p.kill()
    cards = []
    for g in gpus:
        r = results[g]; r.update(ident.get(g, {})); r.update(temps[g])
        errs = sum(r.get(k, 0) or 0 for k in ("pattern_errors", "hammer_errors", "gemm_mismatch", "ref_mismatch"))
        if a.self_test:
            r["verdict"] = "pass" if (not r.get("error") and r.get("injected_caught") == {"pattern": True, "hammer": True}
                                      and errs == 0) else "fail"
        else:
            r["verdict"] = "pass" if (not r.get("error") and errs == 0 and r.get("phase") == "done") else "fail"
        cards.append(r)
    if thermal:                      # too hot says nothing about the clock: no card gets a pass OR a fail from this run
        for c in cards:
            c["verdict"] = "invalid"
    for r in absent:                       # listed by nvidia-smi but not usable by CUDA (for example "GPU requires reset")
        cards.append({"gpu": None, "smi_index": int(r[0]), "uuid": r[1], "pci": r[2][-12:].lower(), "name": r[3], "verdict": "fail",
                      "error": "card is not usable by CUDA (faulted or needs a reset)", "phase": "absent"})
    doc = {"tool": "hbmtune-gates", "version": 1, "thermal_stop": thermal, "self_test": a.self_test, "minutes": a.minutes, "seconds": round(time.time() - t0, 1),
           "when_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "cards": cards,
           "all_pass": all(c["verdict"] == "pass" for c in cards)}
    if a.make_ref and doc["all_pass"] and not a.self_test:
        shas = [json.dumps(c["gemm_sha"], sort_keys=True) for c in cards]
        if len(set(shas)) == 1:
            json.dump({"gemm_sha": cards[0]["gemm_sha"], "gemm_n": a.gemm_n, "seed": a.seed, "made_utc": doc["when_utc"],
                       "mem_clock_mhz": [c.get("mem_clock_mhz") for c in cards], "gpu": cards[0].get("name")}, open(a.make_ref, "w"), indent=1)
            doc["ref_written"] = a.make_ref
        else:
            doc["ref_written"] = None; doc["ref_note"] = "cards disagree on the GEMM bits; no reference written"
    for c in cards:
        print(f"GPU {c.get('smi_index')} {c.get('pci', '?')} {c.get('mem_clock_mhz')} MHz: {c['verdict'].upper()}  "
              f"{c.get('gib', 0):.0f} GiB swept, pattern {c.get('pattern_errors')}, hammer {c.get('hammer_errors')} "
              f"({c.get('rotations')} rot, {c.get('copy_gbps')} GB/s), gemm {c.get('gemm_mismatch')}/{c.get('gemm_runs')}, "
              f"ref {c.get('ref_mismatch')}, HBM max {c.get('hbm_max')} C{'  ERROR ' + c['error'] if c.get('error') else ''}", flush=True)
    if a.json:
        json.dump(doc, open(a.json, "w"), indent=1)
    print(json.dumps(doc))
    return 3 if thermal else (0 if doc["all_pass"] else 1)


if __name__ == "__main__":
    sys.exit(main())
