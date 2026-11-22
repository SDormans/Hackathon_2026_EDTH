"""Search tunables in one object; defaults are the tuned values."""
from dataclasses import dataclass


@dataclass
class Config:
    tour_share: float = 0.1        # share of LNS steps using "tour" (without --adaptive)
    tour_rebuild: float = 0.5      # share of "tour" steps that clear the whole tour instead of k visits
    exact_reinsert: int = 5      # repairs placing at most this many contacts try other insertion orders
    exact_orders: int = 120      # at most this many orders per repair
    exact_reinsert_s: float = 0.5  # time cap for the extra orders per LNS step
    # Scout candidate generation.
    beam_diverse: int = 3        # beam slots kept for states with an unrepresented used/served set
    beam_diverse_extra: bool = False  # diverse slots come on top of the width (else they replace its tail)
    beam_bound: float = 0.0      # beam ranks by score + this * free-effector share * points still to come
    scout_fanout: int = 4        # scouts tried first for a contact
    scout_fallback: int = 8      # scouts tried when none of the first SCOUT_FANOUT gives a valid child
    full_insert_tasks: int = 4   # tours up to this long try every insertion position, not only 3
    reorder_tasks: int = 6       # tours up to this long also try deadline- and start-ordered tours
    # Rebuilt tours keep earlier visits within a few seconds of the parent's (other altitude or
    # departure shift); the reachability bound subtracts this much from the parent's times.
    reach_slack_s: float = 20.0
    # Scout cruise speed. The kit flies scouts at min(0.95 v_h, best-range speed). Energy per
    # metre is flat near the best-range speed (on L4, 0.98 v_h costs +0-1.5% per metre) while
    # airtime, hence hover-free p0 energy, R1 (home by the horizon) and window reach, improve
    # by 5-35%. For scouts whose best-range speed exceeds 0.95 v_h the kit speed is both slower
    # and dearer. R2 allows v_h exactly; 0.995 keeps a 0.5% margin (waypoint times are
    # rounded to 1 ms, legs last tens of seconds).
    scout_cruise: float = 0.995
    scout_speed_mode: str = "fast"
    # Contacts are on the ground; the 3D observation range allows a horizontal offset of
    # sqrt(R_obs^2 - z^2): ~133 m at 70 m, ~102 m at 110 m. The kit always uses 100 m.
    obs_margin_m: float = 2.0
    # Set per run by Search(obs_stretch=...); planner.py --obs-stretch / --no-obs-stretch.
    obs_stretch: bool = True
    # Climb along the first leg and descend along the last one instead of vertically over the
    # pad. R2 times a leg by max(horizontal / v_h, climb / v_up), so the climb overlaps the
    # cruise: no hover power during a vertical climb (p0 for ~5-25 s per aircraft) and, for
    # scouts leaving at t = 0, every visit that many seconds earlier. Falls back to the
    # vertical climb when the diagonal leg would cross a zone or protected area, or is too
    # short to hide the climb. Off: the kit's vertical climb and descent.
    diagonal_climb: bool = True
    # Effector cruise speed as a share of v_h. The kit flies min(0.95 v_h, best-range speed),
    # but on L3/L4 the best-range speed of nearly every effector is above 0.95 v_h, so
    # flying closer to v_h is both earlier and cheaper per metre. R2 allows v_h exactly.
    effector_cruise: float = 0.995
    # Ground-level strikes. R2 bounds the altitude of waypoints after the first and before the
    # last only, and R10 needs the engagement within R_eng (3D) of the contact on the ground,
    # so a two-waypoint effector route (home, engagement) may fly the straight line at z = 0:
    # no climb energy (e_climb x 30 m: 0.3-0.6 Wh per light, 0.9-1.5 Wh per heavy), no climb
    # time and no approach detour. Tried first (layer GROUND_Z, angle 0 only) for every team;
    # a member whose straight ground line enters a zone or protected area (R6) flies the 30 m
    # route instead. Off: the 30 m team first, as before.
    ground_strikes: bool = True
    ground_z: float = 0.0
    # Clearance (m) kept from zone and protected-area cylinders by a ground route: waypoints
    # are rounded to 1 mm.
    ground_clearance_m: float = 1.0
    # Pads are about 11 m apart and separation is 40 m: two aircraft climbing from neighbouring
    # pads at the same time break R8. Most scout R8 rejections were exactly that.
    pad_takeoffs: bool = True
    # Strike arrival delays after confirmation; finer early steps give pad-separated takeoffs
    # more chances before the window closes.
    strike_delays: tuple = (0.0, 4.0, 8.0, 14.0, 20.0, 30.0, 45.0, 90.0)
    # Rank scouts by added cruise energy (added distance x Wh per metre at the tour speed)
    # instead of added distance. Scout efficiency varies by about +-30% within a scenario
    # (L2-202: 7.9 vs 10.4-10.7 J/m), and ranking by distance alone often leaves the
    # cheapest scout idle; hand-built plans with the efficient scouts score +10 on L2-202.
    scout_energy_rank: bool = True
    # Scout options are ranked and filtered by a straight-leg replay of the whole tour
    # (tour_estimate): every window, strike confirmations, landing before the horizon and
    # battery. Off: the distance ranking and scout_reachable bound only.
    time_rank: bool = True
    # Scout trip label search (LNS operator "trip"): one scout's whole sortie rebuilt as the
    # best-value chain of contacts it can observe in their windows and still get home within
    # battery and horizon. Off: the LNS never picks the "trip" operator.
    trip_search: bool = True
    trip_share: float = 0.2        # share of LNS steps using "trip" (without --adaptive)
    trip_labels: int = 6         # labels kept per contact (best by value)
    trip_battery: float = 0.995    # usable battery share the straight-leg estimate may use
    trip_strike_slack: float = 3.0  # seconds kept after confirm_margin + 2 s before a strike window end
    # Trip plan warm start: top trips per scout from the label search, at most one per scout
    # and no contact twice (weighted set packing), realised before the beam passes.
    trip_plan: bool = True
    trip_plan_trips: int = 40     # candidate trips per scout and layer
    trip_plan_share: float = 0.2    # at most this share of the beam phase (start to beam end)
    trip_plan_rounds: int = 2     # re-plan each scout around the others' chosen trips, re-pack
    trip_plan_labels: int = 4     # labels kept per contact while generating plan trips
    trip_plan_exact: bool = False  # plan trips use straight legs (detours are ~95% of the label time)
    trip_plan_drops: int = 2      # extra searches per scout without one of its best trip's contacts
    # Joint re-plan of n scouts (LNS operator "group"): pool the scouts' contacts and the
    # unserved positive-net contacts near them, generate each scout's top trips over that pool
    # with the label search, choose the best combination of disjoint trips and realise it when
    # its estimated value beats the scouts' current trips. Empty: the operator is off.
    group_sizes: tuple = (2, 3, 4)  # random n per move; () turns the operator off
    group_share: float = 0.2        # share of LNS steps using "group" (without --adaptive)
    group_trips: int = 40         # trips kept per scout (plus the cover trips)
    group_radius_m: float = 2000.0  # unserved contacts this close to a chosen scout's tour join the pool
    group_pool: int = 40          # at most this many pool contacts
    group_move_s: float = 0.8       # realisation time cap per move
    group_nodes: int = 200000     # search nodes cap in best_disjoint
    # Joint strike repair.  Unlike a normal destroy/repair step this deliberately removes
    # the aircraft which actually violate R8 against an unserved target's candidate team,
    # then retries that target and the displaced strikes together.  Keep the switch here:
    # it makes an A/B run possible without changing the rest of LNS.
    strike_group: bool = True
    strike_share: float = 0.2
    strike_move_s: float = 0.8
    strike_conflict_routes: int = 18
    strike_remove_sizes: tuple = (1, 2, 3)  # random count of conflicting hostile strikes to displace
    fragility_tight_s: float = 10.0      # engage with less confirm slack than this is a tight coupling
    fragility_sep_margin_m: float = 20.0  # pairs closer than d_sep_m + this are near pairs
    fragility_gap_cap_s: float = 120.0    # confirm->strike gaps are capped here in the score
    # Default LNS share of the budget: larger scenarios gain more from repair than from more beam passes.
    lns_share: float = 0.4
    lns_share_large: float = 0.6
    lns_large_contacts: int = 40  # hostile + unknown contacts


CONFIG = Config()
