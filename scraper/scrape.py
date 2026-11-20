"""
Houston cash tracker - reads every Houston-area room's live cash games from
PokerAtlas, saves the reading, and rebuilds the summary the website charts use.

Runs on GitHub Actions every 15 minutes. Needs two secrets:
  PA_USERNAME  - PokerAtlas username (or email) for a dedicated account
  PA_PASSWORD  - that account's password

Files it writes (all under data/):
  latest.json            the newest reading, room by room, game by game
  rooms.json             the room list (refreshed from PokerAtlas once a day)
  history/YYYY-MM.csv    every game seen running or with a wait list, every run
  totals/YYYY-MM.csv     each room's table/wait totals, every run (drives charts)
  summary.json           heat maps, daily share and 7-day ranking for the site

Exit code 0 = good reading. Exit code 2 = reading failed (details in latest.json).
"""

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.pokeratlas.com"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CENTRAL = ZoneInfo("America/Chicago")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 HoustonCashTracker/1.0")

# Used only if PokerAtlas's room list can't be read and no saved list exists yet.
# (Houston-area rooms PokerAtlas listed in November 2026.)
SEED_ROOMS = [
    ("101-poker-club-katy", "101 Poker Club", "9973", "Katy"),
    ("9-dragons-social-club-houston", "9 Dragons Social Club", "11538", "Houston"),
    ("capri-poker-room-webster", "Capri Poker Room", "11073", "Webster"),
    ("cbet-poker-room-houston", "CBET Poker Room", "11627", "Houston"),
    ("champions-club-houston", "Champions Club", "11230", "Houston"),
    ("doghouse-poker-club-cypress", "Doghouse Poker Club", "10984", "Cypress"),
    ("empire-poker-club-houston", "Empire Poker Club", "10750", "Houston"),
    ("fortune-poker-club-houston", "Fortune Poker Club", "11537", "Houston"),
    ("houston-social-cardroom", "Houston Social Cardroom", "11543", "Houston"),
    ("katy-poker", "Katy Poker", "11385", "Katy"),
    ("legends-poker-room-houston", "Legends Poker Room", "10937", "Houston"),
    ("lucky-j-social-club-houston", "Lucky J Social Club", "11019", "Houston"),
    ("ny-poker-club-katy", "NY Poker Club", "11586", "Katy"),
    ("prime-time-social-houston", "Prime Time Social", "11641", "Houston"),
    ("spades-poker-house-webster", "Spades Poker House", "10683", "Webster"),
    ("spades-poker-house-baytown", "Spades Poker House Baytown", "11269", "Webster"),
    ("texas-card-house-houston", "Texas Card House Houston", "11194", "Houston"),
    ("texas-card-house-spring", "Texas Card House Spring", "9959", "Spring"),
    ("the-hangar-poker-house-humble", "The Hangar Poker House", "11020", "Spring"),
    ("the-river-poker-club-spring", "The River Poker Club", "11500", "Spring"),
    ("the-wheel-social-club-houston", "The Wheel Social Club", "11521", "Spring"),
]

COLUMNS = ["$1/2 NLH", "$1/3 NLH", "$2/5+ NLH", "$1/2 PLO", "$1/3 PLO",
           "$5/5+ PLO / Big O", "ROE / Mixed", "Other"]

DAY_WORDS = r"\b(MON|TUE|WED|THU|FRI|SAT|SUN)(DAY|S|SDAY|NESDAY|RSDAY|URDAY)?\b"
# Not "real" public cash action: private games, tournaments, promo/jackpot/info lines, set-day or
# set-time games, and house-banked casino games. ("PROMO" on its own can tag a real game, e.g.
# "$1/2 Call-$5 NLH+1 PROMO", so only lines that are just a promotions list are dropped.)
EXCLUDE_RE = re.compile(
    r"PRIVATE|TOURNAMENT|^\s*(DAILY\s+)?PROMO(TION)?S?\s*$|DAILY PROMOS|JACKPOT|LIVE\s?STREAM|BONUS|^\s*INFO"
    r"|ULTIMATE|PAI\s?GOW|BLACKJACK|BACCARAT|THREE CARD|3 CARD|@|" + DAY_WORDS, re.I)


class ReadError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def to_int(text):
    t = (text or "").strip().replace(",", "")
    return int(t) if re.fullmatch(r"\d+", t) else None


def column_for(game):
    """Which grid column a game belongs in (index into COLUMNS)."""
    s = game.upper()
    m = re.search(r"(\d+)\s*[/-]\s*\$?(\d+)", s)
    big = int(m.group(2)) if m else 0
    if re.search(r"ROE|MIX|STUD|LIMIT|\bHORSE\b|DEALER", s) and not re.search(r"POT LIMIT|NO LIMIT", s):
        return 6
    if "BIG O" in s:
        return 5
    nlh = s.find("NLH") if "NLH" in s else s.find("HOLD")
    plo_m = re.search(r"PLO|OMAHA", s)
    plo = plo_m.start() if plo_m else -1
    if nlh >= 0 and (plo < 0 or nlh < plo):
        if not m:
            return 7
        return 0 if big <= 2 else 1 if big == 3 else 2
    if plo >= 0:
        if not m:
            return 7
        return 3 if big <= 2 else 4 if big == 3 else 5
    if re.search(r"ROE", s):
        return 6
    return 7


def is_excluded(game):
    """Private games, tournaments, promos and set-day games aren't 'real' cash action."""
    return bool(EXCLUDE_RE.search(game))


# ---------------------------------------------------------------- parsing

def parse_room_list(html, cfg):
    """Read PokerAtlas's Texas room list and keep the Houston-area rooms."""
    soup = BeautifulSoup(html, "html.parser")
    rooms = {}
    for card in soup.select("div.main-content"):
        name_el = card.select_one(".venue-name")
        link = card.select_one(".venue a[href^='/poker-room/']")
        area_el = card.select_one(".area-name")
        if not (name_el and link and area_el):
            continue
        slug = link["href"].split("/")[2]
        vid = ""
        for a in card.select(".venue-meta a[href^='/poker-room/']"):
            part = a["href"].split("/")[2]
            if part.isdigit():
                vid = part
                break
        area = area_el.get_text(strip=True)
        if not vid or area not in cfg["areas"]:
            continue
        if any(w in slug for w in cfg.get("exclude_slug_words", [])):
            continue
        rooms[slug] = {"slug": slug, "name": name_el.get_text(" ", strip=True), "vid": vid, "area": area}
    return rooms


def age_minutes(text):
    """'Last updated: 5 minutes ago' -> 5. Returns None if it can't tell."""
    t = (text or "").lower()
    if not t:
        return None
    if "less than a minute" in t or "just now" in t or "second" in t:
        return 0
    m = re.search(r"(\d+|an?|one)\s+(minute|hour|day|week|month|year)", t)
    if not m:
        return None
    n = 1 if not m.group(1).isdigit() else int(m.group(1))
    mult = {"minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200, "year": 525600}[m.group(2)]
    return n * mult


def parse_last_update(html):
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".last-update")
    if not el:
        return ""
    return re.sub(r"^\s*Last updated:\s*", "", el.get_text(" ", strip=True), flags=re.I)


def parse_live_games(html):
    """Read the 'Games in Progress' box. Returns list of {game, tables, waiting}.
    Raises ReadError if PokerAtlas is showing the signed-out placeholder."""
    if "blanked-live-games" in html or "Sign In to View" in html:
        raise ReadError("PokerAtlas showed the signed-out view - the login didn't stick.")
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup.select(".modal-overlay"):
        junk.decompose()
    games = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c != ""]
        if len(cells) < 2 or any(c.lower() in ("tables", "waiting", "poker games", "game") for c in cells):
            continue
        nums = [to_int(c) for c in cells[1:]]
        nums = [n for n in nums if n is not None]
        if not nums or to_int(cells[0]) is not None:
            continue
        games.append({"game": re.sub(r"\s+", " ", cells[0]), "tables": nums[0],
                      "waiting": nums[1] if len(nums) > 1 else 0})
    if games:
        return games
    # Fallback for a non-table layout: a game name followed by two numbers.
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    i = 0
    while i + 2 < len(lines):
        a, b, c = lines[i], to_int(lines[i + 1]), to_int(lines[i + 2])
        if to_int(a) is None and b is not None and c is not None and a.lower() not in ("tables", "waiting"):
            games.append({"game": a, "tables": b, "waiting": c})
            i += 3
        else:
            i += 1
    return games


# ---------------------------------------------------------------- network

def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    return s


def login(s, username, password):
    r = s.get(f"{BASE}/login", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.select_one("form[action='/sessions']")
    if not form:
        raise ReadError("Couldn't find the PokerAtlas sign-in form (the page may have changed).")
    data = {i.get("name"): i.get("value", "") for i in form.select("input[name]") if i.get("type") != "checkbox"}
    data.update({"user[username]": username, "user[password]": password, "user[remember_me]": "1"})
    r = s.post(f"{BASE}/sessions", data=data, timeout=30, headers={"Referer": f"{BASE}/login"})
    r.raise_for_status()
    if "/login" in r.url and "user[password]" in r.text:
        raise ReadError("PokerAtlas rejected the username or password.")


def fetch_room_list(s, cfg):
    r = s.get(f"{BASE}/poker-rooms/texas", timeout=30)
    r.raise_for_status()
    return parse_room_list(r.text, cfg)


def fetch_games(s, room):
    r = s.get(f"{BASE}/boxes/live_cash_games", params={"from": "main", "vid": room["vid"]}, timeout=30,
              headers={"X-Requested-With": "XMLHttpRequest",
                       "Referer": f"{BASE}/poker-room/{room['slug']}/cash-games"})
    if r.status_code == 404:
        return None, ""
    r.raise_for_status()
    return parse_live_games(r.text), parse_last_update(r.text)


# ---------------------------------------------------------------- room list

def get_rooms(s, cfg):
    saved = load_json(DATA / "rooms.json", {})
    fresh_enough = False
    if saved.get("refreshed_utc"):
        age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["refreshed_utc"])
        fresh_enough = age < timedelta(hours=24)
    rooms = saved.get("rooms") or {}
    note = ""
    if not fresh_enough or not rooms:
        try:
            found = fetch_room_list(s, cfg)
            if len(found) >= 5:
                rooms = found
                saved = {"refreshed_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "rooms": rooms}
                save_json(DATA / "rooms.json", saved)
            else:
                note = "Room list looked wrong; kept the saved one."
        except Exception as e:  # keep going with what we have
            note = f"Couldn't refresh the room list ({e}); used the saved one."
    if not rooms:
        rooms = {sl: {"slug": sl, "name": n, "vid": v, "area": a} for sl, n, v, a in SEED_ROOMS}
    for extra in cfg.get("extra_rooms", []):
        if extra.get("slug") and extra.get("vid"):
            rooms[extra["slug"]] = {"slug": extra["slug"], "name": extra.get("name", extra["slug"]),
                                    "vid": str(extra["vid"]), "area": extra.get("area", "Houston")}
    return rooms, note


# ---------------------------------------------------------------- saving

def append_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def build_summary(cfg, highlight_name):
    """Recompute chart data from the totals files."""
    days = cfg.get("history_days_for_charts", 56)
    now_local = datetime.now(CENTRAL)
    since = now_local - timedelta(days=days)
    runs = defaultdict(dict)  # ts_utc -> {room: (tables, waiting)}
    run_local = {}
    for f in sorted((DATA / "totals").glob("*.csv")):
        with f.open() as fh:
            for row in csv.DictReader(fh):
                ts = row["ts_utc"]
                t_local = datetime.fromisoformat(ts).astimezone(CENTRAL)
                if t_local < since:
                    continue
                runs[ts][row["room"]] = (int(row["tables"]), int(row["waiting"]))
                run_local[ts] = t_local
    rooms = sorted({r for v in runs.values() for r in v})
    heat_sum = defaultdict(lambda: [[0.0] * 24 for _ in range(7)])
    heat_n = [[0] * 24 for _ in range(7)]
    daily = defaultdict(lambda: {"champ": 0.0, "all": 0.0, "n": 0})
    week_ago = now_local - timedelta(days=7)
    rank = defaultdict(lambda: {"t": 0.0, "w": 0.0, "n": 0, "peak": 0})
    for ts, per_room in runs.items():
        tl = run_local[ts]
        d, h = tl.weekday(), tl.hour
        heat_n[d][h] += 1
        total = 0
        for room, (t, w) in per_room.items():
            heat_sum[room][d][h] += t
            total += t
            if tl >= week_ago:
                rk = rank[room]
                rk["t"] += t; rk["w"] += w; rk["n"] += 1; rk["peak"] = max(rk["peak"], t)
        heat_sum["__all"][d][h] += total
        day = daily[tl.date().isoformat()]
        day["champ"] += per_room.get(highlight_name, (0, 0))[0]
        day["all"] += total
        day["n"] += 1

    def avg_grid(key):
        return [[round(heat_sum[key][d][h] / heat_n[d][h], 1) if heat_n[d][h] else None
                 for h in range(24)] for d in range(7)]

    heat = {"All Houston": avg_grid("__all")}
    for room in rooms:
        heat[room] = avg_grid(room)
    daily_list = []
    for day in sorted(daily):
        v = daily[day]
        champ, allt = v["champ"] / v["n"], v["all"] / v["n"]
        daily_list.append({"date": day, "champ": round(champ, 2), "all": round(allt, 2),
                           "share": round(100 * champ / allt, 1) if allt else None, "readings": v["n"]})
    ranking = sorted(({"room": r, "avg_tables": round(v["t"] / v["n"], 1), "avg_waiting": round(v["w"] / v["n"], 1),
                       "peak_tables": v["peak"]} for r, v in rank.items() if v["n"]),
                     key=lambda x: -x["avg_tables"])
    months = sorted(p.stem for p in (DATA / "history").glob("*.csv"))
    save_json(DATA / "summary.json", {
        "built_local": now_local.strftime("%b %-d, %-I:%M %p CT"),
        "readings": len(runs),
        "first_reading_local": min(run_local.values()).strftime("%b %-d, %Y") if run_local else None,
        "highlight": highlight_name, "heat": heat, "daily": daily_list,
        "ranking_7d": ranking, "months": months,
    })


# ---------------------------------------------------------------- main

def main():
    cfg = load_json(ROOT / "config.json", {})
    cfg.setdefault("areas", ["Houston", "Katy", "Cypress", "Webster", "Spring"])
    user, pw = os.environ.get("PA_USERNAME", ""), os.environ.get("PA_PASSWORD", "")
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    now_local = now_utc.astimezone(CENTRAL)
    stamp = now_local.strftime("%b %-d, %-I:%M %p CT")
    previous = load_json(DATA / "latest.json", {})

    def fail(msg):
        out = dict(previous) if previous.get("rooms") else {"rooms": []}
        streak = int(previous.get("fail_streak") or 0) + 1
        out.update({"ok": False, "error": msg, "last_attempt_local": stamp,
                    "last_attempt_utc": now_utc.isoformat(), "fail_streak": streak})
        save_json(DATA / "latest.json", out)
        print("FAILED:", msg)
        # Mark the GitHub run as failed (which emails the owner) on the first failure,
        # then only every 4 hours while it keeps failing - not every 15 minutes.
        sys.exit(2 if streak == 1 or streak % 16 == 0 else 0)

    if not user or not pw:
        fail("The PA_USERNAME / PA_PASSWORD secrets aren't set on GitHub.")

    s = new_session()
    try:
        login(s, user, pw)
    except ReadError as e:
        fail(str(e))
    except Exception as e:
        fail(f"Couldn't reach PokerAtlas to sign in ({e.__class__.__name__}).")

    rooms, note = get_rooms(s, cfg)
    highlight_slug = cfg.get("highlight_room", "champions-club-houston")
    out_rooms, hist_rows, total_rows, problems = [], [], [], []
    for i, room in enumerate(sorted(rooms.values(), key=lambda r: r["name"].lower())):
        if i:
            time.sleep(cfg.get("seconds_between_rooms", 1.5))
        try:
            games, updated = fetch_games(s, room)
        except ReadError as e:
            fail(str(e))
        except Exception as e:
            problems.append(f"{room['name']}: {e.__class__.__name__}")
            continue
        status = "ok"
        if games is None:
            games, status = [], "not found on PokerAtlas"
        if status == "ok" and not games and not updated:
            status = "doesn't post live games to PokerAtlas"
        age = age_minutes(updated)
        stale = age is not None and age > cfg.get("stale_after_minutes", 360)
        if stale:
            status = f"PokerAtlas not updated in {updated.replace(' ago', '')} - not counted"
        kept = []
        for g in games:
            if g["tables"] <= 0 and g["waiting"] <= 0:
                continue
            excluded = is_excluded(g["game"])
            g = dict(g, col=column_for(g["game"]), counted=not excluded and not stale and g["tables"] > 0)
            kept.append(g)
            hist_rows.append([now_utc.isoformat(), now_local.strftime("%Y-%m-%d %H:%M"), room["name"], room["area"],
                              g["game"], g["tables"], g["waiting"], int(g["counted"])])
        t = sum(g["tables"] for g in kept if g["counted"])
        w = sum(g["waiting"] for g in kept if g["counted"])
        out_rooms.append({"name": room["name"], "slug": room["slug"], "area": room["area"], "status": status,
                          "pa_updated": updated, "stale": stale,
                          "highlight": room["slug"] == highlight_slug, "tables": t, "waiting": w, "games": kept})
        total_rows.append([now_utc.isoformat(), now_local.strftime("%Y-%m-%d %H:%M"), room["name"], t, w])

    if not out_rooms:
        fail("Every room failed to load: " + "; ".join(problems[:5]))

    month = now_local.strftime("%Y-%m")
    append_csv(DATA / "history" / f"{month}.csv",
               ["ts_utc", "local_time", "room", "area", "game", "tables", "waiting", "counted"], hist_rows)
    append_csv(DATA / "totals" / f"{month}.csv",
               ["ts_utc", "local_time", "room", "tables", "waiting"], total_rows)

    highlight_name = next((r["name"] for r in out_rooms if r["highlight"]), "Champions Club")
    save_json(DATA / "latest.json", {
        "ok": True, "error": None, "fail_streak": 0, "checked_local": stamp, "checked_utc": now_utc.isoformat(),
        "last_attempt_local": stamp, "last_attempt_utc": now_utc.isoformat(),
        "columns": COLUMNS, "problems": problems + ([note] if note else []), "rooms": out_rooms,
    })
    build_summary(cfg, highlight_name)
    print(f"OK {stamp}: {len(out_rooms)} rooms, {sum(r['tables'] for r in out_rooms)} tables running.")


if __name__ == "__main__":
    main()
