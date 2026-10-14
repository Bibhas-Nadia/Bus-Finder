#!/usr/bin/env python3
"""
geocode_stops.py  -  find latitude / longitude of every West Bengal bus stop from OpenStreetMap (free, no key).

Run once on your computer to BUILD the data file. Your app then only uses the finished CSV/JSON + the
phone's own GPS, so the app itself needs no web service at all.

STEP 1  python3 geocode_stops.py fetch  --email you@example.com
        asks OpenStreetMap "Nominatim" for each stop that has no coordinates (1 request / second,
        about 40 minutes for 2139 stops).  Everything is saved in nominatim_cache.json,
        so you can stop (Ctrl+C) and start again - it continues where it stopped.

STEP 2  python3 geocode_stops.py pick
        offline. A name like "Barasat" may match many places. For every stop the candidate that sits
        closest to the neighbouring stops of its bus routes is chosen. Writes stops_geocoded.csv and
        needs_review.csv (the stops you should look at on a map).

STEP 3  (optional, makes it exact)  python3 geocode_stops.py fetch-osm  --email you@example.com
        python3 geocode_stops.py snap
        downloads every real OSM "bus stop" point of West Bengal (one request) and moves each stop
        to the real bus-stop pole if one with a matching name is within 500 m.
"""
import argparse
import csv
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# West Bengal bounding box (lat/lon). Anything outside is rejected.
WB_SOUTH, WB_NORTH = 21.45, 27.25
WB_WEST, WB_EAST = 85.80, 89.90

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"

LOCAL_LIMIT_KM = 6.0      # a stop on a city route must be this close to its neighbours (median)
REGIONAL_LIMIT_KM = 45.0  # stops only on long-distance routes
SNAP_RADIUS_KM = 0.5
NAME_MATCH = 0.78


# ----------------------------------------------------------------- helpers
def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def in_west_bengal(lat, lon):
    return WB_SOUTH <= lat <= WB_NORTH and WB_WEST <= lon <= WB_EAST


def norm_name(text):
    out = "".join(ch.lower() if ch.isalnum() else " " for ch in text)
    return " ".join(out.split())


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_json(path, default):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- STEP 1: ask Nominatim
def nominatim_candidates(session, name, email):
    """Up to 5 places inside West Bengal for this name. Returns list of dicts (best-ranked first)."""
    queries = [f"{name}, West Bengal, India", f"{name}, Kolkata, West Bengal, India"]
    for q in queries:
        params = {
            "q": q,
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "in",
            "viewbox": f"{WB_WEST},{WB_NORTH},{WB_EAST},{WB_SOUTH}",
            "bounded": 1,
            "accept-language": "en",
        }
        for attempt in range(5):
            resp = session.get(NOMINATIM, params=params, timeout=40)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 502, 503, 504):
                wait = 30 * (attempt + 1)
                print(f"   server busy ({resp.status_code}), waiting {wait}s ...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
        else:
            raise RuntimeError("Nominatim keeps refusing - try again later")

        found = []
        for item in resp.json():
            lat, lon = to_float(item.get("lat")), to_float(item.get("lon"))
            if lat is None or lon is None or not in_west_bengal(lat, lon):
                continue
            found.append({
                "lat": lat,
                "lon": lon,
                "display": item.get("display_name", ""),
                "class": item.get("category") or item.get("class", ""),
                "type": item.get("type", ""),
            })
        time.sleep(1.1)  # Nominatim rule: max 1 request per second
        if found:
            return found
    return []


def cmd_fetch(args):
    import requests

    if "@" not in args.email:
        sys.exit("Please give your real e-mail: --email you@example.com (OpenStreetMap asks for it)")
    stops = read_csv(args.stops)
    cache = load_json(args.cache, {})
    todo = [s for s in stops if not s["latitude"] and s["location"] not in cache]
    print(f"{len(stops)} stops, {len(cache)} already in cache, {len(todo)} to ask now")

    session = requests.Session()
    session.headers["User-Agent"] = f"WestBengalBusTracker/1.0 ({args.email})"
    done = 0
    try:
        for s in todo:
            cache[s["location"]] = nominatim_candidates(session, s["location"], args.email)
            done += 1
            if done % 20 == 0:
                save_json(args.cache, cache)
                print(f"   {done}/{len(todo)} done")
    except KeyboardInterrupt:
        print("\nstopped - progress is saved, run the same command again to continue")
    finally:
        save_json(args.cache, cache)
    print("cache saved:", args.cache)


# ----------------------------------------------------------------- STEP 2: choose the right candidate
def build_neighbours(route_rows):
    """stop_id -> set of stop_ids next to it in any route; stop_id -> 'local' / 'regional' """
    by_route = defaultdict(list)
    scope_of = defaultdict(set)
    for r in route_rows:
        by_route[r["route_id"]].append((int(r["seq"]), r["stop_id"]))
        scope_of[r["stop_id"]].add(r["scope"])
    neigh = defaultdict(set)
    for items in by_route.values():
        items.sort()
        ids = [i for _, i in items]
        for a, b in zip(ids, ids[1:]):
            if a != b:
                neigh[a].add(b)
                neigh[b].add(a)
    return neigh, scope_of


def pick_best(candidates, anchors):
    """candidate closest (median distance) to the anchor points -> (candidate, median_km)"""
    best, best_d = None, None
    for c in candidates:
        d = statistics.median(haversine_km(c["lat"], c["lon"], a[0], a[1]) for a in anchors)
        if best_d is None or d < best_d:
            best, best_d = c, d
    return best, best_d


def choose_all(stops, route_rows, cache):
    """Returns dict stop_id -> result dict. Pure function (no network)."""
    neigh, scope_of = build_neighbours(route_rows)
    by_id = {s["stop_id"]: s for s in stops}

    pos = {}     # stop_id -> (lat, lon)
    res = {}     # stop_id -> result
    for s in stops:
        lat, lon = to_float(s["latitude"]), to_float(s["longitude"])
        if lat is not None and lon is not None:
            pos[s["stop_id"]] = (lat, lon)
            res[s["stop_id"]] = {"source": "existing", "confidence": "high", "osm_name": "", "neighbour_km": ""}

    def limit_for(sid):
        return LOCAL_LIMIT_KM if "local" in scope_of[sid] or "metro" in scope_of[sid] else REGIONAL_LIMIT_KM

    def anchors_of(sid):
        return [pos[n] for n in neigh[sid] if n in pos]

    todo = [s["stop_id"] for s in stops if s["stop_id"] not in res]
    with_cand = [sid for sid in todo if cache.get(by_id[sid]["location"])]

    # pass A: resolve stops that already have located neighbours, most-connected first, repeat while progress
    progress = True
    while progress:
        progress = False
        for sid in sorted(with_cand, key=lambda i: -len(anchors_of(i))):
            if sid in res:
                continue
            anchors = anchors_of(sid)
            if not anchors:
                continue
            cand, dist = pick_best(cache[by_id[sid]["location"]], anchors)
            pos[sid] = (cand["lat"], cand["lon"])
            res[sid] = {
                "source": "osm",
                "confidence": "high" if dist <= limit_for(sid) else "review",
                "osm_name": cand["display"],
                "neighbour_km": round(dist, 2),
            }
            progress = True

    # pass B: stops with no located neighbour at all -> take OpenStreetMap's first answer (low confidence)
    for sid in with_cand:
        if sid not in res:
            cand = cache[by_id[sid]["location"]][0]
            pos[sid] = (cand["lat"], cand["lon"])
            res[sid] = {"source": "osm", "confidence": "low", "osm_name": cand["display"], "neighbour_km": ""}

    # pass C: re-check every OSM choice now that all neighbours are known (2 rounds)
    for _ in range(2):
        for sid in with_cand:
            anchors = [pos[n] for n in neigh[sid] if n in pos]
            if not anchors:
                continue
            cand, dist = pick_best(cache[by_id[sid]["location"]], anchors)
            pos[sid] = (cand["lat"], cand["lon"])
            res[sid].update({
                "confidence": "high" if dist <= limit_for(sid) else "review",
                "osm_name": cand["display"],
                "neighbour_km": round(dist, 2),
            })

    for sid in todo:
        if sid not in res:
            res[sid] = {"source": "not_found", "confidence": "none", "osm_name": "", "neighbour_km": ""}
    return pos, res


def cmd_pick(args):
    stops = read_csv(args.stops)
    routes = read_csv(args.routes)
    cache = load_json(args.cache, {})
    pos, res = choose_all(stops, routes, cache)

    fields = list(stops[0].keys())
    for extra in ("source", "confidence", "neighbour_km", "osm_name"):
        if extra not in fields:
            fields.append(extra)
    out, review = [], []
    for s in stops:
        sid = s["stop_id"]
        row = dict(s)
        if sid in pos:
            row["latitude"], row["longitude"] = f"{pos[sid][0]:.6f}", f"{pos[sid][1]:.6f}"
        row.update(res[sid])
        out.append(row)
        if res[sid]["confidence"] != "high":
            review.append(row)

    write_csv(args.out, out, fields)
    write_csv(args.review, review, fields)
    counts = defaultdict(int)
    for r in out:
        counts[r["confidence"]] += 1
    print(f"written {args.out}  ({len(out)} stops)")
    print("  confidence:", dict(counts))
    print(f"written {args.review}  ({len(review)} stops to check on a map: low / review / none)")


# ----------------------------------------------------------------- STEP 3: snap to real bus-stop poles
def cmd_fetch_osm(args):
    import requests

    if "@" not in args.email:
        sys.exit("Please give your real e-mail: --email you@example.com")
    query = (
        "[out:json][timeout:300];"
        f"(node[\"highway\"=\"bus_stop\"]({WB_SOUTH},{WB_WEST},{WB_NORTH},{WB_EAST});"
        f"node[\"public_transport\"=\"platform\"][\"bus\"=\"yes\"]({WB_SOUTH},{WB_WEST},{WB_NORTH},{WB_EAST}););"
        "out body;"
    )
    print("downloading all OSM bus stops of West Bengal (one request, may take a few minutes) ...")
    resp = requests.post(
        OVERPASS,
        data={"data": query},
        headers={"User-Agent": f"WestBengalBusTracker/1.0 ({args.email})"},
        timeout=600,
    )
    resp.raise_for_status()
    nodes = []
    for el in resp.json().get("elements", []):
        name = el.get("tags", {}).get("name")
        if name:
            nodes.append({"name": name, "lat": el["lat"], "lon": el["lon"]})
    save_json(args.osm, nodes)
    print(f"saved {len(nodes)} named bus stops -> {args.osm}")


def name_score(a, b):
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


def snap_rows(rows, osm_nodes):
    """moves a stop to a real OSM bus-stop node with matching name within SNAP_RADIUS_KM. Pure function."""
    # grid index (0.01 degree ~ 1 km) so 2000 x 20000 comparisons stay fast
    grid = defaultdict(list)
    for n in osm_nodes:
        grid[(round(n["lat"], 2), round(n["lon"], 2))].append(n)
    snapped = 0
    for row in rows:
        lat, lon = to_float(row.get("latitude")), to_float(row.get("longitude"))
        row["snapped"] = "no"
        row["osm_stop_name"] = ""
        if lat is None or lon is None:
            continue
        best, best_key = None, None
        for dy in (-0.01, 0, 0.01):
            for dx in (-0.01, 0, 0.01):
                for n in grid.get((round(lat + dy, 2), round(lon + dx, 2)), []):
                    d = haversine_km(lat, lon, n["lat"], n["lon"])
                    if d > SNAP_RADIUS_KM:
                        continue
                    sc = name_score(row["location"], n["name"])
                    if sc < NAME_MATCH:
                        continue
                    key = (sc, -d)
                    if best_key is None or key > best_key:
                        best, best_key = n, key
        if best:
            row["latitude"], row["longitude"] = f"{best['lat']:.6f}", f"{best['lon']:.6f}"
            row["snapped"] = "yes"
            row["osm_stop_name"] = best["name"]
            snapped += 1
    return snapped


def cmd_snap(args):
    rows = read_csv(args.out)
    nodes = load_json(args.osm, None)
    if nodes is None:
        sys.exit(f"{args.osm} not found - run:  python3 geocode_stops.py fetch-osm --email you@example.com")
    n = snap_rows(rows, nodes)
    fields = list(rows[0].keys())
    write_csv(args.final, rows, fields)
    print(f"{n} of {len(rows)} stops moved to a real OSM bus-stop point -> {args.final}")


# ----------------------------------------------------------------- command line
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "pick", "fetch-osm", "snap"])
    ap.add_argument("--email", default="")
    ap.add_argument("--stops", default="stops.csv")
    ap.add_argument("--routes", default="route_stops.csv")
    ap.add_argument("--cache", default="nominatim_cache.json")
    ap.add_argument("--out", default="stops_geocoded.csv")
    ap.add_argument("--review", default="needs_review.csv")
    ap.add_argument("--osm", default="osm_bus_stops.json")
    ap.add_argument("--final", default="stops_final.csv")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "pick": cmd_pick, "fetch-osm": cmd_fetch_osm, "snap": cmd_snap}[args.command](args)


if __name__ == "__main__":
    main()
