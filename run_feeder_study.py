"""
IEEE 13-node feeder study with OpenDSS + Python
================================================
One script, five studies, the same kinds a utility planner runs in CYME:

  0. Check     - prove the model is right by matching the official IEEE answers
  1. Load flow - voltage at every bus and phase (base case)
  2. What-ifs  - turn the voltage regulators off, then the capacitors off,
                 and see which buses fall outside the 0.95-1.05 pu band
  3. Faults    - short-circuit current at every bus (what protection is set from)
  4. New load  - how big a new 3-phase load (EV hub, small data centre) bus 675
                 can take before voltage, regulators or the substation run out

Run it from this folder:   py run_feeder_study.py
Results land in the "results" folder (3 charts + 4 CSV tables).

Things you can change (then run again) are in the SETTINGS block below.
"""

import sys
from pathlib import Path

try:
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")  # save charts to files, no pop-up windows
    import matplotlib.pyplot as plt
    import opendssdirect as dss
except ImportError as e:
    sys.exit(f"\nMissing package ({e.name}). Run this, then try again:\n"
             "    py -m pip install opendssdirect.py matplotlib pandas\n")

# ----------------------------------------------------------------------------
# SETTINGS - change these and re-run
# ----------------------------------------------------------------------------
PHASE_C_CAP_KVAR = 0         # addition: extra 1-phase capacitor on 675 phase C (0 = off)
V_LOW, V_HIGH = 0.95, 1.05     # planning voltage band, per unit
NEW_LOAD_BUS = "675"           # where the new large load connects (3-phase bus)
NEW_LOAD_PF = 0.98             # its power factor
NEW_LOAD_STEP_KW = 100         # sweep step
NEW_LOAD_MAX_KW = 4000         # sweep stops here
SUB_KVA = 5000                 # substation transformer rating in the circuit file
REG_MAX_STEPS = 16             # regulators run out at +/-16 steps (10%)
# ----------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
CIRCUIT = HERE / "IEEE13Nodeckt.dss"
OUT = HERE / "results"
OUT.mkdir(exist_ok=True)

# Buses roughly in order from the substation outward (650 is the substation).
BUS_ORDER = ["650", "rg60", "632", "670", "671", "680", "692", "675",
             "684", "611", "652", "633", "634", "645", "646"]
PHASE = {"1": "A", "2": "B", "3": "C"}
REGS = ["reg1", "reg2", "reg3"]

# Published IEEE answers (Kersting, IEEE 13 Node Test Feeder), per unit, A/B/C.
IEEE_PUBLISHED = {
    "650": (1.0000, 1.0000, 1.0000), "rg60": (1.0625, 1.0500, 1.0687),
    "632": (1.0210, 1.0420, 1.0174), "633": (1.0180, 1.0401, 1.0148),
    "634": (0.9940, 1.0218, 0.9960), "645": (None, 1.0329, 1.0155),
    "646": (None, 1.0311, 1.0134),   "671": (0.9900, 1.0529, 0.9778),
    "680": (0.9900, 1.0529, 0.9778), "684": (0.9881, None, 0.9758),
    "611": (None, None, 0.9738),     "652": (0.9825, None, None),
    "692": (0.9900, 1.0529, 0.9777), "675": (0.9835, 1.0553, 0.9758),
}

# Chart colours: blue / orange / aqua, plus different markers so colour isn't
# the only thing telling the cases apart.
CASE_STYLE = {
    "Base case":      dict(color="#2a78d6", marker="o"),
    "Regulators off": dict(color="#eb6834", marker="s"),
    "Capacitors off": dict(color="#1baf7a", marker="^"),
}


# ---------------------------------------------------------------- helpers ---
def cmd(text):
    """Send one line of OpenDSS script, exactly as you'd type it in OpenDSS."""
    dss.Text.Command(text)


def load_feeder():
    """Fresh copy of the feeder (the circuit file also runs a first solve)."""
    cmd("clear")
    cmd(f'compile "{CIRCUIT}"')


def solve(must_converge=True):
    try:
        dss.Solution.Solve()
        ok = dss.Solution.Converged()
    except Exception:
        if must_converge:
            raise
        ok = False
    if must_converge and not ok:
        raise RuntimeError("OpenDSS did not converge for this case.")
    return ok


def voltages(case):
    """Per-unit voltage at every bus/phase as a table."""
    rows = []
    for name, v in zip(dss.Circuit.AllNodeNames(), dss.Circuit.AllBusMagPu()):
        bus, node = name.lower().split(".")
        if bus == "sourcebus" or node not in PHASE:
            continue
        rows.append({"case": case, "bus": bus, "phase": PHASE[node],
                     "v_pu": round(float(v), 4)})
    return pd.DataFrame(rows)


def reg_steps():
    """Regulator tap positions in steps (each step = 0.625%)."""
    steps = {}
    for r in REGS:
        dss.Transformers.Name(r)
        dss.Transformers.Wdg(2)
        steps[r] = int(round((dss.Transformers.Tap() - 1.0) / 0.00625))
    return steps


def feeder_summary(case, v):
    p_kw, q_kvar = (-x for x in dss.Circuit.TotalPower()[:2])
    losses_kw = dss.Circuit.Losses()[0] / 1000
    low, high = v.loc[v.v_pu.idxmin()], v.loc[v.v_pu.idxmax()]
    out_of_band = v[(v.v_pu < V_LOW) | (v.v_pu > V_HIGH)]
    return {
        "case": case,
        "feeder_kW": round(p_kw, 1),
        "feeder_kvar": round(q_kvar, 1),
        "losses_kW": round(losses_kw, 1),
        "lowest_V": f"{low.v_pu:.4f} at {low.bus}.{low.phase}",
        "highest_V": f"{high.v_pu:.4f} at {high.bus}.{high.phase}",
        "nodes_outside_band": len(out_of_band),
        "which": ", ".join(f"{r.bus}.{r.phase}" for r in out_of_band.itertuples()),
        "reg_steps": reg_steps(),
    }


# ---------------------------------------------------------------- studies ---
def study0_ieee_check():
    """Lock the regulators at the published taps; compare with IEEE answers."""
    load_feeder()
    cmd("Transformer.Reg1.Taps=[1.0 1.0625]")
    cmd("Transformer.Reg2.Taps=[1.0 1.0500]")
    cmd("Transformer.Reg3.Taps=[1.0 1.06875]")
    cmd("Set ControlMode=OFF")
    solve()
    v = voltages("check").set_index(["bus", "phase"]).v_pu
    rows = []
    for bus, vals in IEEE_PUBLISHED.items():
        for ph, ref in zip("ABC", vals):
            if ref is not None and (bus, ph) in v.index:
                rows.append({"bus": bus, "phase": ph, "IEEE": ref,
                             "yours": v[(bus, ph)],
                             "diff": round(abs(v[(bus, ph)] - ref), 4)})
    return pd.DataFrame(rows)


def study1_and_2_load_flow():
    """Base case, regulators off, capacitors off."""
    cases = {}

    load_feeder()                       # regulators adjust themselves
    solve()
    cases["Base case"] = voltages("Base case")
    summaries = [feeder_summary("Base case", cases["Base case"])]

    load_feeder()
    for r in REGS:                      # lock all 3 regulators at neutral
        cmd(f"RegControl.{r}.Enabled=No")
        cmd(f"Transformer.{r}.Taps=[1.0 1.0]")
    solve()
    cases["Regulators off"] = voltages("Regulators off")
    summaries.append(feeder_summary("Regulators off", cases["Regulators off"]))

    load_feeder()
    cmd("Capacitor.Cap1.Enabled=No")    # 600 kvar bank at 675
    cmd("Capacitor.Cap2.Enabled=No")    # 100 kvar bank at 611
    solve()
    cases["Capacitors off"] = voltages("Capacitors off")
    summaries.append(feeder_summary("Capacitors off", cases["Capacitors off"]))

    return pd.concat(cases.values(), ignore_index=True), pd.DataFrame(summaries)


def fault_current_kA(bus, nodes):
    """Bolted fault from the listed phases to ground at one bus; returns kA."""
    load_feeder()
    cmd("Set ControlMode=OFF")          # freeze regulator taps where they settled
    # The IEEE file shrinks the substation transformer's impedance 1000x so the
    # load flow starts at exactly 1.0 pu. That is fine for voltages, but it makes
    # fault currents near the substation unrealistically huge, so put the real
    # nameplate impedance back (8% reactance, 0.5% resistance per winding).
    cmd("Transformer.Sub.XHL=8")
    cmd("Transformer.Sub.%Rs=[0.5 0.5]")
    bus1 = bus + "." + ".".join(str(n) for n in nodes)
    cmd(f"New Fault.F phases={len(nodes)} bus1={bus1} r=0.0001")
    solve(must_converge=False)
    dss.Circuit.SetActiveElement("Fault.F")
    mags = dss.CktElement.CurrentsMagAng()[0:2 * len(nodes):2]
    return round(max(mags) / 1000, 2)


def study3_faults():
    rows = []
    for bus in BUS_ORDER:
        load_feeder()
        dss.Circuit.SetActiveBus(bus)
        nodes = [int(n) for n in dss.Bus.Nodes() if int(n) in (1, 2, 3)]
        kv_ll = dss.Bus.kVBase() * np.sqrt(3)
        rows.append({
            "bus": bus,
            "kV_LL": round(kv_ll, 2),
            "phases": "".join(PHASE[str(n)] for n in nodes),
            "all_phases_to_ground_kA": fault_current_kA(bus, nodes),
            "one_phase_to_ground_kA": fault_current_kA(bus, nodes[:1]),
        })
    return pd.DataFrame(rows)


def study4_new_load():
    rows = []
    for kw in range(0, NEW_LOAD_MAX_KW + 1, NEW_LOAD_STEP_KW):
        load_feeder()
        if PHASE_C_CAP_KVAR > 0:
            cmd(f"New Capacitor.CapC phases=1 bus1={NEW_LOAD_BUS}.3 kV=2.4 kvar={PHASE_C_CAP_KVAR}")

        if kw > 0:
            cmd(f"New Load.NewLoad phases=3 bus1={NEW_LOAD_BUS}.1.2.3 conn=wye "
                f"kV=4.16 kW={kw} pf={NEW_LOAD_PF} model=1")
        converged = solve(must_converge=False)
        v = voltages("sweep")
        p, q = (-x for x in dss.Circuit.TotalPower()[:2])
        worst = v.loc[v.v_pu.idxmin()]
        steps = max(abs(s) for s in reg_steps().values())
        sub_pct = 100 * np.hypot(p, q) / SUB_KVA
        problems = []
        if not converged:
            problems.append("did not converge")
        if worst.v_pu < V_LOW:
            problems.append(f"low voltage at {worst.bus}.{worst.phase}")
        if steps >= REG_MAX_STEPS:
            problems.append("regulator out of taps")
        if sub_pct > 100:
            problems.append("substation transformer overloaded")
        rows.append({"new_load_kW": kw, "lowest_V": worst.v_pu,
                     "lowest_at": f"{worst.bus}.{worst.phase}",
                     "max_reg_steps": steps, "substation_loading_pct": round(sub_pct, 1),
                     "ok": not problems, "problem": "; ".join(problems)})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- charts ---
def chart_voltage_profile(v):
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), sharey=True)
    for ax, ph in zip(axes, "ABC"):
        ax.axhspan(V_LOW, V_HIGH, color="#f0efec", zorder=0)
        for case, style in CASE_STYLE.items():
            d = v[(v.case == case) & (v.phase == ph)].set_index("bus")
            d = d.reindex([b for b in BUS_ORDER if b in d.index])
            ax.plot(range(len(d)), d.v_pu, linewidth=2, markersize=7,
                    label=case, **style)
            ax.set_xticks(range(len(d)), d.index, rotation=60, fontsize=8)
        ax.axhline(V_LOW, color="#52514e", linewidth=1, linestyle="--")
        ax.axhline(V_HIGH, color="#52514e", linewidth=1, linestyle="--")
        ax.set_title(f"Phase {ph}")
        ax.grid(axis="y", color="#e5e4e0", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].set_ylabel("Voltage (per unit)")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False)
    fig.suptitle("IEEE 13-node feeder: bus voltages (dashed lines = 0.95-1.05 pu band)")
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    fig.savefig(OUT / "1_voltage_profile.png", dpi=150)
    plt.close(fig)


def chart_faults(f):
    fig, ax = plt.subplots(figsize=(9, 5))
    y = np.arange(len(f))
    ax.barh(y - 0.2, f.all_phases_to_ground_kA, height=0.38, color="#2a78d6",
            label="All phases to ground")
    ax.barh(y + 0.2, f.one_phase_to_ground_kA, height=0.38, color="#eb6834",
            label="One phase to ground")
    ax.set_yticks(y, [f"{b} ({k} kV)" for b, k in zip(f.bus, f.kV_LL)], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("Fault current (kA)")
    ax.set_title("Short-circuit current at each bus")
    ax.legend(frameon=False)
    ax.grid(axis="x", color="#e5e4e0", linewidth=0.8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "2_fault_currents.png", dpi=150)
    plt.close(fig)


def chart_new_load(s, limit_kw):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    ax1.plot(s.new_load_kW, s.lowest_V, color="#2a78d6", linewidth=2, marker="o",
             markersize=4)
    ax1.axhline(V_LOW, color="#52514e", linewidth=1, linestyle="--")
    ax1.set_ylabel("Lowest voltage on feeder (pu)")
    ax2.plot(s.new_load_kW, s.substation_loading_pct, color="#2a78d6", linewidth=2,
             marker="o", markersize=4)
    ax2.axhline(100, color="#52514e", linewidth=1, linestyle="--")
    ax2.set_ylabel("Substation transformer loading (%)")
    ax2.set_xlabel(f"New 3-phase load at bus {NEW_LOAD_BUS} (kW)")
    for ax in (ax1, ax2):
        if limit_kw is not None:
            ax.axvline(limit_kw, color="#eb6834", linewidth=1.5)
        ax.grid(color="#e5e4e0", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    title = f"How much new load can bus {NEW_LOAD_BUS} take?"
    if limit_kw is not None:
        title += f"  (orange line = limit, about {limit_kw} kW)"
    ax1.set_title(title)
    fig.tight_layout()
    fig.savefig(OUT / "3_new_load_capacity.png", dpi=150)
    plt.close(fig)


# ------------------------------------------------------------------- main ---
def main():
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    print(f"OpenDSS engine: {dss.Basic.Version().splitlines()[0]}\n")

    print("=" * 72)
    print("STUDY 0 - Is the model right? (compare with official IEEE answers)")
    print("=" * 72)
    check = study0_ieee_check()
    worst = check["diff"].max()
    print(check.to_string(index=False))
    verdict = "PASS - matches IEEE" if worst <= 0.005 else "CHECK - differs from IEEE"
    print(f"\nLargest difference: {worst:.4f} pu  ->  {verdict}\n")

    print("=" * 72)
    print("STUDIES 1 & 2 - Load flow: base case and what-ifs")
    print("=" * 72)
    v, summary = study1_and_2_load_flow()
    for row in summary.to_dict("records"):
        print(f"\n[{row['case']}]")
        print(f"  Feeder draws {row['feeder_kW']} kW / {row['feeder_kvar']} kvar, "
              f"losses {row['losses_kW']} kW")
        print(f"  Lowest {row['lowest_V']}   Highest {row['highest_V']}")
        print(f"  Regulator taps (steps): {row['reg_steps']}")
        print(f"  Outside {V_LOW}-{V_HIGH} pu: {row['nodes_outside_band']} "
              f"{'(' + row['which'] + ')' if row['which'] else ''}")
    v.to_csv(OUT / "voltages_all_cases.csv", index=False)
    summary.to_csv(OUT / "case_summary.csv", index=False)
    chart_voltage_profile(v)

    print("\n" + "=" * 72)
    print("STUDY 3 - Fault (short-circuit) currents")
    print("=" * 72)
    f = study3_faults()
    print(f.to_string(index=False))
    f.to_csv(OUT / "fault_currents.csv", index=False)
    chart_faults(f)

    print("\n" + "=" * 72)
    print(f"STUDY 4 - New large load at bus {NEW_LOAD_BUS}")
    print("=" * 72)
    s = study4_new_load()
    s.to_csv(OUT / "new_load_sweep.csv", index=False)
    if not s.ok.iloc[0]:
        print("Note: the feeder already has a problem before any new load: "
              + s.problem.iloc[0])
    bad = s[~s.ok & (s.new_load_kW > 0)]
    if bad.empty:
        print(f"No limit reached up to {NEW_LOAD_MAX_KW} kW. Raise NEW_LOAD_MAX_KW.")
        limit = None
    else:
        first = bad.iloc[0]
        limit = int(first.new_load_kW - NEW_LOAD_STEP_KW)
        print(f"Bus {NEW_LOAD_BUS} can take about {limit} kW of new load.")
        print(f"At {int(first.new_load_kW)} kW the first problem is: {first.problem}")
    chart_new_load(s, limit)

    print(f"\nDone. Charts and tables are in: {OUT}")


if __name__ == "__main__":
    main()
