import os
import sqlite3
import requests
import logging
import threading
import time
import random

from flask import Flask, render_template, jsonify
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DB_FILE            = "houses.db"
META_TABLE         = "meta"
HOUSES_TABLE       = "houses"
NOTIFS_TABLE       = "notifications"
LAST_UPDATE_HOURS  = 12
DB_LOCK            = threading.Lock()

IGNORED_HOUSES = [
    "Ankardia Guildhall I",
    "Ankardia Guildhall II",
    "Ankardia Guildhall III"
]

FETCH_DELAY       = 1.5
JITTER            = 1.5
REQUEST_TIMEOUT   = 15
SESSION_HEADERS   = {
    "User-Agent": "MedievalDynasty-House-Scraper/1.0 (+kontakt)",
    "From":      "twoj_email@przyklad.com"
}

SCHEDULER_SLEEP   = 60 * 30
NOTIFIER_INTERVAL = 5 * 60  # co 5 minut tylko sprawdzanie progów

DEFAULT_PLACEHOLDER = "/static/no-image.png"

DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/"
    "1461076804034105559/YflKwPorOxSTviZAal_7PwjqPb2suseTP4F6D9E46yum0c1zBdBeB1BysrzhZd9DDhMu"
)
ENABLE_DISCORD      = bool(DISCORD_WEBHOOK_URL)
NOTIFY_CITIES       = {"cyleria city", "boss room", "ankardia"}
NOTIFY_24H          = 24 * 3600
NOTIFY_1H           = 60 * 60
COLOR_24H           = 0xFFA500
COLOR_1H            = 0xFF0000

sent_houses = set()

def init_db():
    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS {HOUSES_TABLE} (
                name TEXT PRIMARY KEY,
                city TEXT,
                size TEXT,
                owner TEXT,
                image TEXT,
                days INTEGER,
                available TEXT,
                status TEXT
            )
        """)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS {META_TABLE} (
                id INTEGER PRIMARY KEY,
                last_update TEXT
            )
        """)
        c.execute(f"""
            CREATE TABLE IF NOT EXISTS {NOTIFS_TABLE} (
                name TEXT,
                available TEXT,
                notified_at TEXT
            )
        """)
        conn.commit()

        # dodaj kolumnę 'type' w notifications
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({NOTIFS_TABLE})")
        cols = [r[1] for r in cur.fetchall()]
        if 'type' not in cols:
            logging.info("[MIGRATE] Dodaję kolumnę 'type' do notifications")
            try:
                cur.execute(f"ALTER TABLE {NOTIFS_TABLE} ADD COLUMN type TEXT")
            except sqlite3.OperationalError:
                pass
            cur.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_notifications_unique ON {NOTIFS_TABLE}(name, available, type)
            """)
            conn.commit()

        # inicjalizacja meta
        c.execute(f"SELECT COUNT(*) FROM {META_TABLE}")
        if c.fetchone()[0] == 0:
            past = (datetime.now() - timedelta(hours=LAST_UPDATE_HOURS+1))\
                   .strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                f"INSERT INTO {META_TABLE}(id, last_update) VALUES(1, ?)",
                (past,)
            )
        conn.commit()

init_db()

def parse_available(available_str):
    txt = available_str.strip().lower()
    if txt.startswith("już") or "teraz" in txt:
        return datetime.now()
    try:
        return datetime.strptime(available_str, "%d.%m.%Y %H:%M")
    except ValueError:
        return None

def get_last_update():
    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            f"SELECT last_update FROM {META_TABLE} WHERE id=1"
        ).fetchone()
    return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") if row else None

def set_last_update(ts):
    s = ts.strftime("%Y-%m-%d %H:%M:%S")
    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            f"UPDATE {META_TABLE} SET last_update=? WHERE id=1", (s,)
        )
        conn.commit()

def get_house_info(session=None):
    url = "https://cyleria.pl/?subtopic=houses&length=1000"
    logging.info(f"[SCRAPE] Pobieram domki: {url}")
    sess = session or requests.Session()
    sess.headers.update(SESSION_HEADERS)
    r = sess.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    soup, data, cities = BeautifulSoup(r.text, "html.parser"), [], set()
    rows = soup.select("table.table-striped tbody tr")

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue
        name = cols[0].get_text(strip=True)
        if any(name.startswith(x) for x in IGNORED_HOUSES):
            continue

        size, owner = cols[1].get_text(strip=True), cols[2].get_text(strip=True)
        city, img = "Nieznane", None

        span = cols[0].find("span", {"data-bs-content": True})
        if span:
            inner = BeautifulSoup(span["data-bs-content"], "html.parser")
            i = inner.find("img")
            if i and i.get("src"):
                src = i["src"].strip()
                if src.startswith("//"):
                    img = "https:" + src
                elif src.startswith("/"):
                    img = "https://cyleria.pl" + src
                elif src.startswith("http"):
                    img = src
                else:
                    img = "https://cyleria.pl/" + src
            cdiv = inner.find("div", class_="mt-2 fw-bold")
            if cdiv:
                city = cdiv.get_text(strip=True)

        if name.lower().startswith("unnamed house") and city.lower() == "cyleria city":
            continue

        data.append({
            "name":  name,
            "city":  city,
            "size":  size,
            "owner": owner,
            "image": img or DEFAULT_PLACEHOLDER
        })
        cities.add(city)

    return data, cities

def fetch_login(owner, session):
    if not owner or owner.strip().lower() == "none":
        return None
    time.sleep(FETCH_DELAY + random.uniform(0, JITTER))
    url = f"https://cyleria.pl/?subtopic=characters&name={owner.replace(' ', '+')}"
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for li in soup.select("li.list-group-item.d-flex.justify-content-between"):
            if "Logowanie:" in li.text:
                strong = li.find("strong")
                if strong:
                    return datetime.strptime(
                        strong.text.strip(), "%d.%m.%Y (%H:%M)"
                    )
    except Exception:
        pass
    return None

def send_discord_embed(session, title, description, fields, color, image_url=None, mention=False):
    """
    Wysyła embed do Discorda. Jeśli mention=True, doda content: "@everyone"
    oraz dopisze " @everyone" do opisu (żeby było widoczne).
    """
    if not ENABLE_DISCORD:
        return False

    # jeśli chcemy pingować, dopiszemy widoczny dopisek w opisie i dodamy content
    if mention:
        description = (description or "") + " @everyone"

    payload = {
        "embeds": [{
            "title":       title,
            "description": description,
            "color":       color,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "fields":      fields
        }]
    }
    if image_url:
        payload["embeds"][0]["image"] = {"url": image_url}

    if mention:
        # content z @everyone powoduje prawdziwy ping
        payload["content"] = "@everyone"

    try:
        resp = session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        logging.info(f"[DISCORD] Embed wysłany: {title}")
        return True
    except Exception as e:
        logging.error(f"[DISCORD] Błąd przy wysyłce: {e}")
        return False

def already_notified(name, available, ntype):
    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        row = conn.execute(
            f"SELECT 1 FROM {NOTIFS_TABLE} WHERE name=? AND available=? AND type=?",
            (name, available, ntype)
        ).fetchone()
    return bool(row)

def mark_notified(name, available, ntype):
    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        conn.execute(f"""
            INSERT OR REPLACE INTO {NOTIFS_TABLE}
            (name, available, type, notified_at) VALUES(?,?,?,?)
        """, (
            name, available, ntype,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        conn.commit()

def format_timedelta(td):
    total = int(td.total_seconds())
    if total <= 0:
        return "mniej niż minuta"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} {'dzień' if days==1 else 'dni'}")
    if hours:
        parts.append(
            f"{hours} {'godzina' if hours==1 else 'godziny' if 2<=hours<=4 else 'godzin'}"
        )
    if minutes and not days:
        parts.append(
            f"{minutes} {'minuta' if minutes==1 else 'minuty' if 2<=minutes<=4 else 'minut'}"
        )
    return ' '.join(parts) if parts else "mniej niż minuta"

def _do_update():
    global sent_houses
    logging.info("[UPDATE] Rozpoczynam odświeżanie bazy…")
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    data, _ = get_house_info(session=session)
    now = datetime.now()

    for h in data:
        dt = None
        try:
            dt = fetch_login(h["owner"], session)
        except Exception:
            pass

        if dt:
            avail_dt    = dt + timedelta(days=14)
            days_passed = (now - dt).days
            h["days"]      = max(0, 14 - days_passed)
            h["available"] = (
                avail_dt.strftime("%d.%m.%Y %H:%M") if days_passed < 14 else "Już teraz"
            )
            h["status"]    = "Aktywny" if days_passed < 14 else "Nieaktywny"
        else:
            h.update(days=0, available="Nieznane", status="Nieaktywny")

    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(f"DELETE FROM {HOUSES_TABLE}")
        for h in data:
            c.execute(f"""
                INSERT OR REPLACE INTO {HOUSES_TABLE}
                (name,city,size,owner,image,days,available,status)
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                h["name"], h["city"], h["size"], h["owner"],
                h.get("image", DEFAULT_PLACEHOLDER),
                h["days"], h["available"], h["status"]
            ))
        conn.commit()

    set_last_update(now)
    logging.info("[UPDATE] Zakończono odświeżanie.")

def check_and_notify():
    session = requests.Session()
    session.headers.update(SESSION_HEADERS)
    now = datetime.now()

    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"""
            SELECT name, city, owner, image, available
            FROM {HOUSES_TABLE}
            WHERE status='Aktywny'
        """).fetchall()

    for r in rows:
        avail_dt = parse_available(r["available"])
        if not avail_dt or r["city"].strip().lower() not in NOTIFY_CITIES:
            continue

        remaining = avail_dt - now
        rem_sec   = int(remaining.total_seconds())

        for ntype, threshold, color in [
            ("24h", NOTIFY_24H, COLOR_24H),
            ("1h",  NOTIFY_1H,  COLOR_1H)
        ]:
            # nie wyślij "24h", gdy zostało już <= 1h
            if ntype == "24h" and rem_sec <= NOTIFY_1H:
                continue

            if rem_sec <= threshold and not already_notified(r["name"], r["available"], ntype):
                time_until = format_timedelta(remaining)
                title = f"[{ntype}] Domek: {r['name']}"
                desc  = f"Za {time_until} ({r['available']}) będzie do przejęcia."
                fields = [
                    {"name":"Miasto",     "value":r["city"],    "inline":True},
                    {"name":"Właściciel", "value":r["owner"],   "inline":True},
                    {"name":"Pozostało",  "value":time_until,   "inline":True}
                ]
                # tu ustawiamy mention=True, żeby dodać @everyone (zarówno w content, jak i w opisie)
                if send_discord_embed(session, title, desc, fields, color, image_url=r["image"], mention=True):
                    mark_notified(r["name"], r["available"], ntype)

def background_scheduler():
    while True:
        try:
            if not get_last_update() or \
               (datetime.now() - get_last_update()) >= timedelta(hours=LAST_UPDATE_HOURS):
                sent_houses.clear()
                _do_update()
        except Exception as e:
            logging.error(f"[SCHEDULER] {e}")
        time.sleep(SCHEDULER_SLEEP)

def notifier_scheduler():
    while True:
        try:
            check_and_notify()
        except Exception as e:
            logging.error(f"[NOTIFIER] {e}")
        time.sleep(NOTIFIER_INTERVAL)

threading.Thread(target=background_scheduler, daemon=True).start()
threading.Thread(target=notifier_scheduler,  daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/houses')
def houses():
    with DB_LOCK, sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT * FROM {HOUSES_TABLE}").fetchall()
    data   = [dict(r) for r in rows]
    cities = sorted({r["city"] for r in rows})
    return jsonify(data=data, cities=cities, statuses=["Aktywny","Nieaktywny"])

@app.route('/refresh')
def refresh():
    _do_update()
    return "Odświeżono bazę na żądanie"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

