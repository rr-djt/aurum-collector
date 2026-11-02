#!/usr/bin/env python3
"""Aurum Terminal rate collector.

Reads today's gold and silver rates straight from each jeweller's own website
(the rate the jeweller itself publishes), checks every number, and writes one
JSON document. Runs hourly on a free GitHub Actions schedule and sends the
result to the licence server; can also run on any computer for testing:

    python3 collector.py --out rates.json
    python3 collector.py --post https://<server>/bullion/ingest   (token in AURUM_INGEST_TOKEN)

Rules it keeps:
- Only the jeweller's own site. It says who it is (User-Agent), follows
  robots.txt, and never disguises itself or gets around a block: a site that
  refuses automatic reading is reported as "blocked".
- Never invents a number: a purity the jeweller doesn't publish stays empty.
- A reading that fails a sense check, or is more than STALE_DAYS old, is not
  used; the server keeps showing the last good one with its age.
Standard library only, Python 3.9+.
"""
import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser

UA = "AurumTerminalRateReader/1.0 (hourly public gold-rate check)"
TIMEOUT = 25
STALE_DAYS = 3
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# The 15 cities of the city picker → state, as written by most jeweller sites.
CITIES = {
    "Mumbai": "Maharashtra", "Delhi": "Delhi", "Bengaluru": "Karnataka", "Hyderabad": "Telangana",
    "Ahmedabad": "Gujarat", "Chennai": "Tamil Nadu", "Kolkata": "West Bengal", "Surat": "Gujarat",
    "Pune": "Maharashtra", "Jaipur": "Rajasthan", "Lucknow": "Uttar Pradesh", "Kanpur": "Uttar Pradesh",
    "Nagpur": "Maharashtra", "Indore": "Madhya Pradesh", "Thane": "Maharashtra",
}
# Other spellings jeweller sites use for the same city / state, and the
# neighbourhoods some list their stores by (Kalyan: "Camac Street" = Kolkata).
ALIASES = {
    "mumbai": ["bombay", "borivalli", "borivali", "ghatkopar", "andheri", "dadar", "malad", "kandivali", "mulund",
               "chembur", "goregaon", "santacruz", "bandra", "vile parle", "matunga", "kurla", "powai", "vashi"],
    "delhi": ["new delhi", "ncr", "karol bagh", "dwarka", "pitampura", "preet vihar", "south extension",
              "defence colony", "vegas mall", "kamla nagar", "kamala nagar", "rajouri garden", "janakpuri",
              "lajpat nagar", "rohini", "saket"],
    "kolkata": ["calcutta", "camac street", "gariahat", "salt lake", "behala", "park street", "new town"],
    "bengaluru": ["bangalore", "jayanagar", "koramangala", "malleswaram", "marathahalli", "phoenix mall",
                  "lulu bengaluru", "kammanahalli", "whitefield", "indiranagar", "rajajinagar", "basavanagudi"],
    "hyderabad": ["secunderabad", "kukatpally", "dilsukhnagar", "ameerpet", "banjara hills", "himayatnagar",
                  "kphb", "madhapur", "abids"],
    "chennai": ["madras", "t nagar", "t.nagar", "anna nagar", "velachery", "adyar", "purasawalkam", "tambaram",
                "mylapore", "porur"],
    "pune": ["hadapsar", "kothrud", "pimpri", "chinchwad", "aundh", "camp"],
    "ahmedabad": ["cg road", "maninagar", "satellite"],
    "tamil nadu": ["tamilnadu"],
}


def now_iso():
    return dt.datetime.now(IST).isoformat(timespec="seconds")


def norm(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def names_for(word):
    w = word.lower()
    return {norm(w)} | {norm(a) for a in ALIASES.get(w, [])}


def num(v):
    try:
        x = float(str(v).replace(",", "").strip())
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ polite fetching
class Blocked(Exception):
    """The site refuses automatic reading (robots.txt or HTTP 401/403/429)."""


_robots = {}


def allowed(url):
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            body = urllib.request.urlopen(urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA}),
                                          timeout=TIMEOUT).read().decode("utf-8", "replace")
            rp.parse(body.splitlines())
        except Exception:
            rp.parse([])          # no robots.txt (or unreachable) = no rules
        _robots[base] = rp
    return _robots[base].can_fetch(UA, url)


def fetch(url, data=None, json_body=None, form=None, headers=None):
    if not allowed(url):
        raise Blocked("robots.txt asks automatic readers not to open this page")
    h = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}
    h.update(headers or {})
    if json_body is not None:
        data, h["Content-Type"] = json.dumps(json_body).encode(), "application/json"
    elif form is not None:
        data, h["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429):
            raise Blocked("the site refused automatic reading (HTTP %d)" % e.code)
        raise


def page_text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)).split())


def find_rate(text, label_regex):
    """First '₹ 12,730' / 'Rs. 12730.00' that follows the label within a few words."""
    m = re.search(label_regex + r"[^₹\d]{0,30}(?:₹|Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)", text, re.I)
    return num(m.group(1)) if m else None


def reading(k24=None, k22=None, silver=None, as_of=None, city="*", area=None):
    return {"city": city, "area": area, "k24": k24, "k22": k22, "silver": silver, "as_of": as_of}


# ------------------------------------------------------------------ one reader per jeweller
# Each returns (scope, [readings]). scope: "national" (one rate for all India),
# "state", "store" (per store location) or "region" (one published rate for its home region).

def malabar():
    q = ("query getMetalRate($filter: MetalRateFilterInput) { getMetalRate(filter: $filter) "
         "{ items { entry_date entry_time purity unit rate country state } } }")
    out = {}
    for metal in ("gold", "silver"):
        url = "https://www.malabargoldanddiamonds.com/graphql-magento?" + urllib.parse.urlencode(
            {"query": q, "variables": json.dumps({"filter": {"country": "India", "metal_type": metal}})})
        for it in json.loads(fetch(url))["data"]["getMetalRate"]["items"]:
            out[(metal, it["purity"])] = (num(it["rate"]), "%sT%s" % (it["entry_date"], it["entry_time"]))
    g24, g22, s = out.get(("gold", "24k")), out.get(("gold", "22k")), out.get(("silver", "1000"))
    as_of = (g22 or g24 or (None, None))[1]
    return "national", [reading(g24 and g24[0], g22 and g22[0], s and s[0], as_of)]


def lalithaa():
    base = "https://api.lalithaajewellery.com/public/"
    states = json.loads(fetch(base + "states"))["data"]["items"]
    rows = []
    for st in states:
        p = json.loads(fetch(base + "pricings/latest?state_id=" + st["id"]))["data"]["prices"]
        def val(key):
            e = p.get(key) or {}
            return num(e.get("price")), e.get("rate_datetime")
        (k24, t24), (k22, t22), (sv, ts) = val("gold_24kt"), val("gold_22kt"), val("silver")
        for city, state in CITIES.items():
            if norm(state) in names_for(st["name"]) or norm(st["name"]) in names_for(state):
                rows.append(reading(k24, k22, sv, t22 or t24, city=city))
    return "state", rows


def kalyan():
    ajax = "https://www.kalyanjewellers.net/kalyan_gold_rates/ajax/"
    hdr = {"X-Requested-With": "XMLHttpRequest"}
    opts = lambda html: re.findall(r'value="([^"]+)"[^>]*>\s*([^<]+?)\s*<', html)
    states = opts(fetch(ajax + "get_state_ajax", form={"country": "1"}, headers=hdr))
    rows, store_cache = [], {}
    for city, state in CITIES.items():
        sid = next((v for v, t in states if norm(t) in names_for(state)), None)
        if not sid:
            continue
        if sid not in store_cache:
            store_cache[sid] = opts(fetch(ajax + "get_city_ajax", form={"state": sid}, headers=hdr))
        stores = store_cache[sid]
        pick = next(((v, t) for v, t in stores if norm(t) in names_for(city)), None)
        if not pick:
            continue                                   # no Kalyan store listed for this city
        res = json.loads(fetch(ajax + "get_rate", form={"countryId": "1", "stateId": sid, "cityId": pick[0]}, headers=hdr))
        t = page_text(res.get("html", ""))
        k22 = num((re.search(r"22\s*kt\s*:\s*INR\s*([\d,.]+)", t, re.I) or [None, None])[1])
        k24 = num((re.search(r"24\s*kt\s*:\s*INR\s*([\d,.]+)", t, re.I) or [None, None])[1])
        rows.append(reading(k24, k22, None, None, city=city, area=pick[1].title()))
        time.sleep(1)                                  # gentle: one request a second
    return "store", rows


def jos_alukkas():
    d = json.loads(fetch("https://backend.josalukkasonline.com/api/Master/GetLatestGoldRate/", json_body={}))["Data"]
    return "national", [reading(num(d.get("R24KT")), num(d.get("R22KT")), num(d.get("RS925")) if d.get("RS925Show") else None,
                                d.get("LastUpdated"))]


def reliance():
    d = json.loads(fetch("https://www.reliancejewels.com/api/stores/getGoldRateList"))["data"]["goldRate"][0]
    v = lambda k: num((d.get(k) or {}).get("$numberDecimal"))
    return "national", [reading(v("amount_one"), v("amount_two"), None, d.get("created_on"))]


def thangamayil():
    t = page_text(fetch("https://www.thangamayil.com/"))
    return "region", [reading(find_rate(t, r"24\s*k\b"), find_rate(t, r"22\s*k\b"), find_rate(t, r"silver"), None, city="Chennai")]


def khazana():
    t = page_text(fetch("https://www.khazanajewellery.com/"))
    return "region", [reading(find_rate(t, r"24\s*KT\b"), find_rate(t, r"22\s*KT\b"), find_rate(t, r"silver"), None, city="Chennai")]


def grt():
    """GRT's page carries its whole rate list as data ("gold_rate": 24/22/18 KT, silver)."""
    html = fetch("https://www.grtjewels.com/").replace('\\"', '"')
    m = re.search(r'"gold_rate":(\[.*?\])', html)
    rates = json.loads(m.group(1)) if m else []
    get = lambda typ, pur: next((num(r.get("amount")) for r in rates if r.get("type") == typ and
                                 (pur is None or re.sub(r"\s+", "", str(r.get("purity")).upper()) == pur.replace(" ", "").upper()) and r.get("weight") == 1), None)
    return "region", [reading(get("GOLD", "24 KT"), get("GOLD", "22 KT"), get("SILVER", None), None, city="Chennai")]


def bangalore_refinery():
    """A refinery: it publishes prices of its bars, not a jewellery rate. 24K = its 10 g bar ÷ 10,
    silver = its 1 kg bar ÷ 1000. The file is cached by their CDN, so ask for a fresh copy and
    take the date from its own "Update At" line (a stale copy then fails the age check)."""
    rows = json.loads(fetch("https://www.bangalorerefinery.com/cdn/shop/files/rates.txt?v=%d" % time.time()))
    price = {re.sub(r"\s+", " ", a).strip().lower(): b for a, b in rows if a}
    bar10 = next((num(v) for k, v in price.items() if k.startswith("24k") and "10 g" in k and "bar" in k), None)
    kg = next((num(v) for k, v in price.items() if k == "silver 1 kg bar"), None)
    as_of = None
    stamp = price.get("update at", "")
    try:
        as_of = dt.datetime.strptime(stamp.strip(), "%m/%d/%Y %I:%M:%S %p").isoformat()
    except ValueError:
        pass
    return "national", [reading(bar10 and round(bar10 / 10), None, kg and round(kg / 1000, 2), as_of)]


def dp_jewellers():
    """D.P. Jewellers (D.P. Abhushan): the metal_rates data its own online store loads."""
    rows = json.loads(fetch("https://prod-dpj.acmepadm.com/api/v1/metal_rates?locale=en"))["data"]
    def pick(metal, purity):
        r = next((x for x in rows if x.get("metal_type") == metal and
                  (purity is None or str(x.get("purity_description", "")).upper() == purity)), None)
        return (num(r.get("sale_rate")), r.get("rate_datetime")) if r else (None, None)
    (k24, t), (k22, _), (sv, _) = pick("gold", "24KT"), pick("gold", "22KT"), pick("silver", None)
    return "national", [reading(k24, k22, sv, t)]


CANDERE_CITY = {"Mumbai": "mumbai", "Delhi": "delhi", "Bengaluru": "bangalore", "Hyderabad": "hyderabad",
                "Chennai": "chennai", "Pune": "pune", "Lucknow": "lucknow", "Thane": "kalyan-thane"}


def candere():
    """Candere (Kalyan's online store): one page per city with a dated daily table."""
    rows = []
    for city, slug in CANDERE_CITY.items():
        t = page_text(fetch("https://www.candere.com/gold-rate-today/" + slug)).replace("&#8377;", "₹")
        m = re.search(r"Last\s*10\s*Days\s*\(1g\)\s*Date\s*24K\s*22K\s*18K\s*14K\s*([A-Z][a-z]{2}\s*\d{1,2},\s*\d{4})"
                      r"\s*₹\s*([\d,]+)\s*₹\s*([\d,]+)", t)
        if m:
            day = dt.datetime.strptime(re.sub(r"\s+", " ", m.group(1)), "%b %d, %Y").date().isoformat()
            r = reading(num(m.group(2)), num(m.group(3)), None, day, city=city)
            table = t[m.start():m.start() + 1500]
            r["history"] = [{"day": dt.datetime.strptime(re.sub(r"\s+", " ", h.group(1)), "%b %d, %Y").date().isoformat(),
                             "k24": num(h.group(2)), "k22": num(h.group(3))}
                            for h in re.finditer(r"([A-Z][a-z]{2}\s*\d{1,2},\s*\d{4})\s*₹\s*([\d,]+)\s*₹\s*([\d,]+)", table)]
            rows.append(r)
        time.sleep(1)
    return "city", rows


CARATLANE_CITY = {"Mumbai": "maharashtra/mumbai", "Delhi": "delhi/new-delhi", "Bengaluru": "karnataka/bangalore",
                  "Hyderabad": "telangana/hyderabad", "Ahmedabad": "gujarat/ahmedabad", "Chennai": "tamil-nadu/chennai",
                  "Kolkata": "west-bengal/kolkata", "Pune": "maharashtra/pune"}


def caratlane():
    rows = []
    for city, path in CARATLANE_CITY.items():
        t = page_text(fetch("https://www.caratlane.com/gold-rate/%s-gold-rate-today" % path))
        m = re.search(r"₹\s*([\d,]+)\s*/\s*Gram\s*·\s*24KT\s*22KT\s*₹\s*([\d,]+)", t)
        if m:
            rows.append(reading(num(m.group(1)), num(m.group(2)), None, None, city=city))
        time.sleep(1)
    return "city", rows


def augmont():
    t = page_text(fetch("https://augmont.com/live-rates"))
    k24 = num((re.search(r"24K\s*Gold/g\s*[+-]?[\d.]+%\s*₹\s*([\d,]+)", t) or [None, None])[1])
    k22 = num((re.search(r"22K\s*Gold/g\s*[+-]?[\d.]+%\s*₹\s*([\d,]+)", t) or [None, None])[1])
    m = re.search(r"last refreshed on (\d{1,2} [A-Z][a-z]{2} \d{4}) at (\d{1,2}:\d{2} [ap]m)", t, re.I)
    as_of = None
    if m:
        try:
            as_of = dt.datetime.strptime(m.group(1) + " " + m.group(2).upper(), "%d %b %Y %I:%M %p").isoformat()
        except ValueError:
            pass
    return "national", [reading(k24, k22, None, as_of)]


def indriya():
    """Indriya publishes a 30-day rate history as data for its main store (Indore)."""
    today = dt.datetime.now(IST).date()
    url = ("https://www.indriya.com/content/noveljewels/in/en/gold-rate-today/jcr:content/root/container/container/"
           "container_copy_copy__1497933197/chartcomponent.goldChart.json?storeId=NS0001&fromDate=%s&toDate=%s"
           % (today - dt.timedelta(days=30), today))
    data = json.loads(json.loads(fetch(url))["ratesJson"])
    latest, hist = {}, {}
    for p in data.get("purities", []):
        items = ((p.get("data") or {}).get("getMetalRateData") or {}).get("items") or []
        key = {"24KT": "k24", "22KT": "k22"}.get(p.get("purity", "")[:4].upper())
        for it in items:
            if key and it.get("event_date"):
                hist.setdefault(it["event_date"], {"day": it["event_date"]})[key] = round(num(it.get("metal_rate")) or 0, 2) or None
        if items:
            it = max(items, key=lambda i: i.get("event_date", ""))
            latest[p.get("purity", "")[:4].upper()] = (num(it.get("metal_rate")), it.get("event_date"), it.get("city"))
    k24, k22 = latest.get("24KT"), latest.get("22KT")
    city = (k22 or k24 or (None, None, "Indore"))[2] or "Indore"
    r = reading(k24 and round(k24[0], 2), k22 and round(k22[0], 2), None,
                (k22 or k24 or (None, None))[1], city=city if city in CITIES else "Indore")
    r["history"] = sorted(hist.values(), key=lambda h: h["day"])
    return "store", [r]


# id, display name, own website, reader (None = refuses automatic reading), kind
JEWELLERS = [
    ("malabar", "Malabar Gold & Diamonds", "https://www.malabargoldanddiamonds.com", malabar, "jeweller"),
    ("kalyan", "Kalyan Jewellers", "https://www.kalyanjewellers.net", kalyan, "jeweller"),
    ("lalithaa", "Lalithaa Jewellery", "https://www.lalithaajewellery.com", lalithaa, "jeweller"),
    ("josalukkas", "Jos Alukkas", "https://www.josalukkasonline.com", jos_alukkas, "jeweller"),
    ("reliance", "Reliance Jewels", "https://www.reliancejewels.com", reliance, "jeweller"),
    ("thangamayil", "Thangamayil Jewellery", "https://www.thangamayil.com", thangamayil, "jeweller"),
    ("khazana", "Khazana Jewellery", "https://www.khazanajewellery.com", khazana, "jeweller"),
    ("grt", "GRT Jewellers", "https://www.grtjewels.com", grt, "jeweller"),
    ("dpjewellers", "D.P. Jewellers", "https://www.dpjewellers.com", dp_jewellers, "jeweller"),
    ("candere", "Candere by Kalyan", "https://www.candere.com", candere, "jeweller"),
    ("caratlane", "CaratLane", "https://www.caratlane.com", caratlane, "jeweller"),
    ("indriya", "Indriya", "https://www.indriya.com", indriya, "jeweller"),
    ("augmont", "Augmont", "https://www.augmont.com", augmont, "bullion"),
    ("bangalorerefinery", "Bangalore Refinery", "https://www.bangalorerefinery.com", bangalore_refinery, "bullion"),
    # These refuse server readers; the apps read them in the user's own Chrome (AURUM_READER on the server).
    ("tanishq", "Tanishq", "https://www.tanishq.co.in", None, "jeweller"),
    ("bhima", "Bhima Jewellers", "https://www.bhimagold.com", None, "jeweller"),
    ("mmtcpamp", "MMTC-PAMP", "https://www.mmtcpamp.com", None, "bullion"),
]


# ------------------------------------------------------------------ sense checks
def check(rows, kind, ref22):
    """Drop numbers that can't be right. Returns (kept_rows, notes)."""
    kept, notes = [], []
    today = dt.datetime.now(IST).date()
    for r in rows:
        bad = []
        if r["as_of"]:
            try:
                day = dt.date.fromisoformat(str(r["as_of"])[:10])
                if (today - day).days > STALE_DAYS:
                    bad.append("published %s, older than %d days" % (day, STALE_DAYS))
            except ValueError:
                pass
        k24, k22 = r["k24"], r["k22"]
        if k24 and k22 and not (0.88 <= k22 / k24 <= 0.95):
            bad.append("22K is %.1f%% of 24K (should be about 91.6%%)" % (100 * k22 / k24))
        if kind == "jeweller" and ref22 and k22 and abs(k22 / ref22 - 1) > 0.12:
            bad.append("22K ₹%d is more than 12%% away from other jewellers" % k22)
        for key, lo, hi in (("k24", 3000, 60000), ("k22", 3000, 60000), ("silver", 20, 2000)):
            if r[key] is not None and not (lo <= r[key] <= hi):
                bad.append("%s value %s out of range" % (key, r[key]))
        if not (k24 or k22 or r["silver"]):
            bad.append("no rate found on the page")
        if bad:
            notes.append("%s: %s" % (r["area"] or r["city"], "; ".join(bad)))
        else:
            kept.append(r)
    return kept, notes


def collect(only=None, progress=None):
    """Read every jeweller once. progress(done, total, name) is called as it goes
    (the app's Refresh button shows it)."""
    started = now_iso()
    results = []
    todo = [j for j in JEWELLERS if not only or j[0] in only]
    for n, (jid, name, site, reader, kind) in enumerate(todo):
        if progress:
            progress(n, len(todo), name)
        entry = {"id": jid, "name": name, "site": site, "kind": kind, "read_at": now_iso()}
        if reader is None:
            entry.update(status="blocked", note="This jeweller's website doesn't allow automatic rate reading.")
        else:
            try:
                scope, rows = reader()
                entry.update(status="ok", scope=scope, readings=rows)
            except Blocked as e:
                entry.update(status="blocked", note=str(e))
            except Exception as e:
                entry.update(status="error", note="%s: %s" % (type(e).__name__, str(e)[:200]))
        results.append(entry)
    # middle 22K across jewellers = the reference for the "too far away" check
    all22 = [r["k22"] for e in results if e.get("kind") == "jeweller" for r in e.get("readings", []) if r["k22"]]
    ref22 = statistics.median(all22) if all22 else None
    for e in results:
        if e["status"] == "ok":
            e["readings"], notes = check(e["readings"], e["kind"], ref22)
            if notes:
                e["rejected"] = notes
            if not e["readings"]:
                e["status"] = "error"
                e["note"] = "No reading passed the checks"
    return {"format": 1, "started_at": started, "finished_at": now_iso(), "cities": CITIES,
            "reference_22k": ref22, "jewellers": results}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="write the JSON here")
    ap.add_argument("--post", help="send the JSON to this server address")
    ap.add_argument("--only", help="comma-separated jeweller ids, for testing")
    a = ap.parse_args()
    doc = collect(set(a.only.split(",")) if a.only else None)
    body = json.dumps(doc, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(body)
    if a.post:
        token = os.environ.get("AURUM_INGEST_TOKEN", "")
        if not token:
            sys.exit("AURUM_INGEST_TOKEN is not set")
        req = urllib.request.Request(a.post, data=body.encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA,
                                              "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            print("server:", r.status, r.read().decode()[:200])
    for e in doc["jewellers"]:
        rs = e.get("readings", [])
        first = rs[0] if rs else {}
        print("%-24s %-8s %-9s %2d reading(s)  22K=%s 24K=%s silver=%s %s" % (
            e["name"], e["status"], e.get("scope", ""), len(rs), first.get("k22"), first.get("k24"), first.get("silver"),
            ("| " + e.get("note", "")) if e["status"] != "ok" else ("| rejected: %s" % e["rejected"] if e.get("rejected") else "")))


if __name__ == "__main__":
    main()
