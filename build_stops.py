#!/usr/bin/env python3
"""
build_stops.py  -  busdata.json  ->  stops.csv  +  route_stops.csv

stops.csv        one row per UNIQUE bus stop
                 stop_id, location, latitude, longitude, route_count, routes, aliases
route_stops.csv  one row per (route, position)  -> which stop comes after which, per route
                 route_id, code, kind, scope, towards, seq, stop_id, location

Usage:  python3 build_stops.py busdata.json [output_folder]
Nothing can be missed: the script stops with an error if a stop used by any route is not in stops.csv.
"""
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def natural_key(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(text))]


def make_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 build_stops.py busdata.json [output_folder]")

    src = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")
    out_dir.mkdir(parents=True, exist_ok=True)

    data = json.loads(src.read_text(encoding="utf-8"))
    routes = data["routes"]
    stops_list = data.get("stops", [])
    aliases = data.get("aliases", {})

    # ---- 1. every stop name that exists anywhere (exact text, only trimmed) ----
    all_names = set()
    for s in stops_list:
        all_names.add(s["name"].strip())
    for r in routes:
        for n in r["stops"]:
            all_names.add(n.strip())

    # coordinates that already exist in the file are kept
    known = {}
    for s in stops_list:
        if s.get("lat") is not None and s.get("lng") is not None:
            known[s["name"].strip()] = (s["lat"], s["lng"])

    # ---- 2. unique, stable id for each stop ----
    #  id = lower-case name with every non a-z0-9 replaced by "-"   ("Jadavpur 8B" -> "jadavpur-8b")
    #  two different names that would give the same id get -2, -3 ...  (so ids never clash)
    ordered = sorted(all_names, key=natural_key)
    stop_id = {}
    used = defaultdict(int)
    for name in ordered:
        base = make_slug(name) or "stop"
        used[base] += 1
        stop_id[name] = base if used[base] == 1 else f"{base}-{used[base]}"
    assert len(set(stop_id.values())) == len(stop_id), "stop ids are not unique!"

    # ---- 3. routes that pass each stop ----
    codes_of = defaultdict(set)
    count_of = defaultdict(int)
    for r in routes:
        for n in r["stops"]:
            n = n.strip()
            codes_of[n].add(r["code"])
            count_of[n] += 1

    alias_of = defaultdict(list)  # stop -> other names people use for it
    for alias, target in aliases.items():
        alias_of[target.strip()].append(alias)

    # ---- 4. stops.csv ----
    stops_csv = out_dir / "stops.csv"
    with stops_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["stop_id", "location", "latitude", "longitude", "route_count", "routes", "aliases"])
        for name in ordered:
            lat, lng = known.get(name, ("", ""))
            w.writerow([
                stop_id[name],
                name,
                lat,
                lng,
                count_of[name],
                "|".join(sorted(codes_of[name], key=natural_key)),
                "|".join(sorted(alias_of[name], key=natural_key)),
            ])

    # ---- 5. route_stops.csv ----
    rs_csv = out_dir / "route_stops.csv"
    with rs_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["route_id", "code", "kind", "scope", "towards", "seq", "stop_id", "location"])
        for i, r in enumerate(routes, start=1):
            for seq, n in enumerate(r["stops"], start=1):
                n = n.strip()
                w.writerow([i, r["code"], r["kind"], r["scope"], r.get("towards", ""), seq, stop_id[n], n])

    # ---- 6. checks: nothing may be missing ----
    with stops_csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    in_csv = {row["location"] for row in rows}
    in_routes = {n.strip() for r in routes for n in r["stops"]}
    missing = in_routes - in_csv
    mentions = sum(len(r["stops"]) for r in routes)
    with rs_csv.open(encoding="utf-8") as f:
        rs_rows = sum(1 for _ in f) - 1

    assert not missing, f"MISSING STOPS: {sorted(missing)[:10]}"
    assert rs_rows == mentions, f"route_stops rows {rs_rows} != stop mentions {mentions}"
    assert len(rows) == len(in_csv) == len(set(stop_id.values())), "duplicate rows in stops.csv"

    with_xy = sum(1 for row in rows if row["latitude"] != "")
    print(f"routes                      : {len(routes)}")
    print(f"stop mentions in routes     : {mentions}")
    print(f"unique stops (stops.csv)    : {len(rows)}")
    print(f"  already have lat/lng      : {with_xy}")
    print(f"  still need lat/lng        : {len(rows) - with_xy}")
    print(f"route_stops.csv rows        : {rs_rows}")
    print("CHECK OK - no stop missed, every stop_id is unique")

    clash = [a for a in aliases if a in in_csv]
    if clash:
        print(f"NOTE: these alias names are ALSO real stops (same place under two names?): {clash}")


if __name__ == "__main__":
    main()
