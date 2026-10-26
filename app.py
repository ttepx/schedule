import base64
import datetime
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, make_response, redirect, render_template_string, request

from teachers import ASSIGNMENTS, TEACHERS

# ───────────────────────── Налаштування ─────────────────────────
BASE_URL = os.getenv("LCLOUD_BASE_URL", "https://rcnubip.lcloud.in.ua/")
TZ = ZoneInfo("Europe/Kyiv")
COOKIE_NAME = "lc_session"
SESSION_SECONDS = 12 * 3600
CACHE_SECONDS = 120

# SECRET_KEY потрібен для шифрування cookie. На Vercel задай його в Environment Variables.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = os.urandom(32).hex()
    print("⚠️ SECRET_KEY не задано: сесії діятимуть лише до перезапуску сервера.")
fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
}
DAY_NAMES = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]

# ── Суботи: пари «за інший день» ──
# Суботній розклад повторює розклад одного з буднів і щотижня зсувається:
# Пн → Вт → Ср → Чт → Пт → знову Пн. Опорна субота й її день задають весь цикл.
SATURDAY_ANCHOR = datetime.date(2026, 10, 31)   # ця субота: пари за СЕРЕДУ
SATURDAY_ANCHOR_WEEKDAY = 2                      # 0=Пн, 1=Вт, 2=Ср, 3=Чт, 4=Пт
# Якщо якась субота випадає з циклу (свята, перенесення), вкажи її тут:
#   {datetime.date(2026, 12, 26): 0}   # ця субота за понеділок
SATURDAY_OVERRIDES = {}
# Дзвінок на суботу (пари по 1 годині). ПЕРЕВІР і виправ під свій коледж!
# Використовується лише тоді, коли в lCloud на суботу своїх пар немає.
SATURDAY_BELLS = [("08:00", "09:00"), ("09:10", "10:10"), ("10:20", "11:20"),
                  ("11:30", "12:30"), ("12:40", "13:40")]
DAY_ACC = ["понеділок", "вівторок", "середу", "четвер", "п'ятницю"]

app = Flask(__name__)


class SessionExpired(Exception):
    pass


# ───────────────────────── Робота з lCloud ─────────────────────────
def lcloud_login(login, password):
    """Вхід як у login.js. Пароль нікуди не зберігається і не логується.
    Повертає (ok, message, cookies)."""
    try:
        s = requests.Session()
        s.get(BASE_URL, headers=HEADERS, timeout=8)
        res = s.get(
            BASE_URL,
            params={"answer": "json", "do": "users", "func": "login",
                    "login": login, "password": password},
            headers=HEADERS, timeout=8,
        )
        data = res.json()
    except Exception as exc:
        # Лише тип помилки: текст міг би містити URL із паролем.
        print(f"Вхід: помилка зв'язку з lCloud ({type(exc).__name__})")
        return False, "Не вдалося зв'язатися з lCloud. Спробуйте пізніше.", None

    ok = data.get("st") in (True, 1, "1", "true")
    if not ok:
        print(f"Вхід відхилено lCloud (st={data.get('st')})")
    msg = BeautifulSoup(str(data.get("text", "")), "html.parser").get_text(" ", strip=True)
    if not ok:
        return False, msg or "Невірний логін або пароль.", None
    return True, "", s.cookies.get_dict()


def _same_site(url):
    """Адреса аудиторії має бути на сайті lCloud (щоб cookie не пішли кудись інде)."""
    return url if urlparse(url).netloc == urlparse(BASE_URL).netloc else None


def safe_link(url):
    if not url:
        return None
    return url if urlparse(url).scheme in ("http", "https") else None


TEACHER_BY_SHORT = {t["short"]: t for t in TEACHERS}
TEACHERS_BY_ID = {}
for _t in TEACHERS:
    for _i in _t["ids"]:
        TEACHERS_BY_ID.setdefault(_i, []).append(_t)


def zoom_meeting_id(url):
    m = re.search(r"/(?:j|jc|wc/join)/(\d+)", url or "")
    return m.group(1) if m else None


def find_assigned(card):
    group = (card["group"].split() or [""])[0]
    subject = card["lesson"].lower()
    for part, short in ASSIGNMENTS.get(group, {}).items():
        if part.lower() in subject:
            return TEACHER_BY_SHORT.get(short)
    return None


def _norm(text):
    text = text.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")
    return re.sub(r"[^а-яіїєґ'a-z\s]", " ", text)


def match_pdf_teacher(raw):
    """Шукає викладача зі списку PDF за прізвищем (і ініціалом, якщо прізвище повторюється)."""
    toks = _norm(raw or "").split()
    cands = [t for t in TEACHERS if t["name"].split()[0].lower() in toks]
    if len(cands) <= 1:
        return cands[0] if cands else None
    best = [t for t in cands
            if t["name"].split()[1].lower() in toks or t["name"].split()[1][0].lower() in toks]
    return best[0] if len(best) == 1 else None


LABEL_RE = re.compile(
    r"(?i:викладач|педагог)[^:\n]{0,20}:?\s*"
    r"([А-ЯІЇЄҐ][а-яіїєґ']+(?:\s+[А-ЯІЇЄҐ][а-яіїєґ'.]*){1,2})")


def extract_teacher_text(html):
    """Дістає прізвище викладача зі сторінки віртуальної аудиторії."""
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    for line in text.split("\n"):                    # 1) збіг із викладачами з PDF
        if len(line) <= 160:
            t = match_pdf_teacher(line)
            if t:
                return t["name"]
    m = LABEL_RE.search(text)                        # 2) за підписом «Викладач: ...»
    return m.group(1).strip() if m else None


_TEACHER_CACHE = {}


def fetch_teacher_name(cookies, url):
    hit = _TEACHER_CACHE.get(url)
    if hit and time.time() - hit[0] < (6 * 3600 if hit[1] else 600):
        return hit[1]
    name = None
    try:
        s = requests.Session()
        s.cookies.update(cookies)
        res = s.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=8)
        name = extract_teacher_text(res.text)
        if not name:
            print(f"Викладача не знайдено на сторінці {urlparse(url).path}")
    except Exception:
        name = None
    if len(_TEACHER_CACHE) > 2000:
        _TEACHER_CACHE.clear()
    _TEACHER_CACHE[url] = (time.time(), name)
    return name


def fetch_teacher_names(cookies, urls):
    urls = [u for u in urls if u]
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        return dict(zip(urls, pool.map(lambda u: fetch_teacher_name(cookies, u), urls)))


def enrich(card):
    """Додає викладача, посилання й код доступу.
    Порядок: збіг номера Zoom з PDF → прізвище зі сторінки аудиторії → налаштування ASSIGNMENTS."""
    card.update(teacher=None, teacher_short=None, code=None, backup_url=None, backup_code=None)
    raw = card.get("teacher_raw")
    cands = TEACHERS_BY_ID.get(zoom_meeting_id(card["zoom_link"]) or "", [])
    teacher = cands[0] if len(cands) == 1 else None
    teacher = teacher or match_pdf_teacher(raw) or find_assigned(card)
    ref = teacher or (cands[0] if cands else None)
    if teacher:
        card["teacher"], card["teacher_short"] = teacher["name"], teacher["short"]
    elif raw:                                         # викладача немає в PDF, показуємо як є
        card["teacher"], card["teacher_short"] = raw, raw[:40]
    if not ref:
        return card
    if ref in cands:                      # та сама кімната, що й в lCloud
        card["zoom_link"] = safe_link(ref["url"]) or card["zoom_link"]
        card["code"] = ref["code"]
    elif not card["zoom_link"]:           # в lCloud посилання немає: беремо з PDF
        card["zoom_link"] = safe_link(ref["url"])
        card["code"] = ref["code"]
    else:                                 # в lCloud інша кімната: запасний варіант
        card["backup_url"] = safe_link(ref["url"])
        card["backup_code"] = ref["code"]
    return card


CARD_TIME_RE = re.compile(r"№\s*(\d+)\s*\(\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*\)")


def parse_cards(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.schedule-card"):
        date_el = li.select_one(".schedule-date span")
        time_el = li.select_one(".schedule-time span")
        subj_el = li.select_one(".schedule-subject")
        if not (date_el and time_el and subj_el):
            continue
        m = CARD_TIME_RE.search(time_el.get_text(" ", strip=True))
        if not m:
            continue
        try:
            date = datetime.datetime.strptime(date_el.get_text(strip=True), "%d.%m.%Y").date()
        except ValueError:
            continue
        group_el = li.select_one(".schedule-group")
        zoom_el = li.select_one("a.webinar_link")
        room_el = li.select_one('.schedule-actions a[href^="/va-"]')
        out.append({
            "date": date,
            "pair": int(m.group(1)),
            "start": m.group(2),
            "end": m.group(3),
            "time": f"{m.group(2)} - {m.group(3)}",
            "lesson": subj_el.get_text(" ", strip=True),
            "group": group_el.get_text(" ", strip=True) if group_el else "",
            "room": room_el.get_text(strip=True) if room_el else "",
            "va_url": _same_site(urljoin(BASE_URL, room_el["href"])) if room_el else None,
            "zoom_link": safe_link(zoom_el.get("href")) if zoom_el else None,
        })
    return out


def fetch_day(cookies, day):
    s = requests.Session()
    s.cookies.update(cookies)
    res = s.get(
        BASE_URL,
        params={"do": "nav_plan", "answer": "json", "func": "task_rozklad",
                "to_date": day.isoformat()},
        headers=HEADERS, timeout=8,
    )
    try:
        data = res.json()
    except ValueError:
        print(f"lCloud: відповідь розкладу не JSON (статус {res.status_code})")
        raise SessionExpired()
    if not isinstance(data, dict):
        raise SessionExpired()
    if "text" not in data:
        if data.get("st") in (False, 0, "0", "false"):
            print(f"lCloud: розклад відхилено (st={data.get('st')})")
            raise SessionExpired()
        return []                          # день без пар: відповідь без тексту
    return parse_cards(data["text"] or "")


_CACHE = {}


def get_schedule(cookies, monday, extra_day=None):
    """Повертає {date: [картки]} для тижня Пн–Сб (+ додатковий день)."""
    days = [monday + datetime.timedelta(days=i) for i in range(6)]
    if extra_day and extra_day not in days:
        days.append(extra_day)
    key = (hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).hexdigest(),
           tuple(d.isoformat() for d in days))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    with ThreadPoolExecutor(max_workers=len(days)) as pool:
        results = list(pool.map(lambda d: fetch_day(cookies, d), days))

    wanted = set(days)
    by_date, seen = {d: [] for d in days}, set()
    for cards in results:
        for c in cards:
            k = (c["date"], c["pair"], c["lesson"])
            if c["date"] in wanted and k not in seen:
                seen.add(k)
                by_date[c["date"]].append(c)
    for lst in by_date.values():
        lst.sort(key=lambda c: c["pair"])

    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[key] = (time.time(), by_date)
    return by_date


# ───────────────────────── Сесія в cookie ─────────────────────────
def read_session():
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        raw = fernet.decrypt(token.encode(), ttl=SESSION_SECONDS)
        data = json.loads(raw)
        return data if isinstance(data, dict) and data else None
    except (InvalidToken, ValueError):
        return None


def set_session(resp, cookies):
    token = fernet.encrypt(json.dumps(cookies).encode()).decode()
    resp.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
        samesite="Lax", secure=bool(os.getenv("VERCEL")) or request.is_secure,
    )


def clear_session(resp):
    resp.delete_cookie(COOKIE_NAME)


# Обмеження НЕВДАЛИХ спроб входу (в межах одного інстанса сервера).
# Вдалі входи не рахуються, щоб клас в одній мережі не блокував сам себе.
_FAILED = {}
MAX_FAILED = 20


def _client_ip():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "?")).split(",")[0].strip()


def is_blocked():
    now = time.time()
    hits = [t for t in _FAILED.get(_client_ip(), []) if now - t < 600]
    return len(hits) >= MAX_FAILED


def record_failure():
    now = time.time()
    ip = _client_ip()
    _FAILED[ip] = [t for t in _FAILED.get(ip, []) if now - t < 600] + [now]
    if len(_FAILED) > 1000:
        _FAILED.clear()


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ───────────────────────── Допоміжні ─────────────────────────
def step_day(d, direction):
    """Наступний/попередній день, неділі пропускаємо."""
    d += datetime.timedelta(days=direction)
    while d.weekday() == 6:
        d += datetime.timedelta(days=direction)
    return d


def pairs_word(n):
    if n == 0:
        return "—"
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} пара"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} пари"
    return f"{n} пар"


def saturday_weekday(sat):
    """За який будній день (0=Пн ... 4=Пт) ця субота."""
    if sat in SATURDAY_OVERRIDES:
        return SATURDAY_OVERRIDES[sat]
    weeks = (sat - SATURDAY_ANCHOR).days // 7
    return (SATURDAY_ANCHOR_WEEKDAY + weeks) % 5


def apply_saturday(by_date, monday):
    """Субота: якщо в lCloud на цю дату пар немає, беремо пари відповідного
    буднього дня цього ж тижня й ставимо їх на суботній дзвінок."""
    sat = monday + datetime.timedelta(days=5)
    if sat not in by_date:
        return by_date, None
    wd = saturday_weekday(sat)
    info = {"weekday": wd, "from_site": bool(by_date[sat])}
    if by_date[sat]:
        return by_date, info
    cards = []
    for i, c in enumerate(by_date.get(monday + datetime.timedelta(days=wd), [])):
        start, end = SATURDAY_BELLS[min(i, len(SATURDAY_BELLS) - 1)]
        cards.append({**c, "date": sat, "pair": i + 1, "start": start, "end": end,
                      "time": f"{start} - {end}"})
    result = dict(by_date)
    result[sat] = cards
    return result, info


def _bounds(d, c):
    st = datetime.datetime.combine(d, datetime.datetime.strptime(c["start"], "%H:%M").time(), tzinfo=TZ)
    en = datetime.datetime.combine(d, datetime.datetime.strptime(c["end"], "%H:%M").time(), tzinfo=TZ)
    return st, en


def _decorate(c, d, now):
    st, en = _bounds(d, c)
    diff = (d - now.date()).days
    if diff == 0:
        when = "Сьогодні"
    elif diff == 1:
        when = "Завтра"
    else:
        when = f"{DAY_NAMES[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"
    return {**c, "start_iso": st.isoformat(), "end_iso": en.isoformat(), "when": when}


def find_hero_lessons(by_date, now):
    """Повертає (пара, що йде зараз; найближча наступна пара)."""
    current = upcoming = None
    for d in sorted(by_date):
        for c in by_date[d]:
            st, en = _bounds(d, c)
            if st <= now < en and current is None:
                current = _decorate(c, d, now)
            elif st > now and upcoming is None:
                upcoming = _decorate(c, d, now)
    return current, upcoming


# ───────────────────────── Сторінки ─────────────────────────
@app.route("/login", methods=["POST"])
def login():
    if is_blocked():
        return render_template_string(LOGIN_TEMPLATE, error="Забагато спроб. Спробуйте за 10 хвилин."), 429
    login_value = (request.form.get("login") or "").strip()
    password = request.form.get("password") or ""
    if not login_value or not password:
        return render_template_string(LOGIN_TEMPLATE, error="Введіть логін і пароль."), 400
    ok, msg, cookies = lcloud_login(login_value, password)
    password = None  # пароль більше не потрібен
    if not ok:
        record_failure()
        return render_template_string(LOGIN_TEMPLATE, error=msg), 401
    resp = make_response(redirect("/"))
    set_session(resp, cookies)
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(redirect("/"))
    clear_session(resp)
    return resp


@app.route("/")
def home():
    cookies = read_session()
    if not cookies:
        return render_template_string(LOGIN_TEMPLATE, error=None)

    now = datetime.datetime.now(TZ)
    today = now.date()
    try:
        selected = datetime.date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        selected = today if today.weekday() <= 5 else step_day(today, 1)
    monday = selected - datetime.timedelta(days=selected.weekday())

    try:
        by_date = get_schedule(cookies, monday, extra_day=selected)
    except SessionExpired:
        resp = make_response(render_template_string(
            LOGIN_TEMPLATE, error="Сесія завершилась. Увійдіть ще раз."))
        clear_session(resp)
        return resp
    except Exception as exc:
        print(f"Розклад: помилка ({type(exc).__name__})")
        return render_template_string(
            ERROR_TEMPLATE, message="Не вдалося отримати розклад. Спробуйте оновити сторінку."), 502

    by_date, sat_info = apply_saturday(by_date, monday)

    week_days = []
    for i in range(6):
        d = monday + datetime.timedelta(days=i)
        week_days.append({
            "iso": d.isoformat(), "label": DAY_SHORT[i], "num": d.day,
            "count": pairs_word(len(by_date.get(d, []))),
            "selected": d == selected, "today": d == today,
        })

    # Блок зверху: пара, що йде зараз, і наступна пара
    raw_now = raw_next = None
    if monday - datetime.timedelta(days=2) <= today <= monday + datetime.timedelta(days=5):
        raw_now, raw_next = find_hero_lessons(by_date, now)

    # Додатковий крок: відкриваємо сторінку аудиторії й дізнаємось викладача
    selected_cards = by_date.get(selected, [])
    urls = {c["va_url"] for c in selected_cards if c.get("va_url")}
    for raw in (raw_now, raw_next):
        if raw and raw.get("va_url"):
            urls.add(raw["va_url"])
    names = fetch_teacher_names(cookies, urls)

    def finish(card):
        card = dict(card)
        card["teacher_raw"] = names.get(card.get("va_url"))
        return enrich(card)

    hero_now = finish(raw_now) if raw_now else None
    hero_next = finish(raw_next) if raw_next else None

    lessons = []
    for c in selected_cards:
        state = "upcoming"
        if selected == today:
            st, en = _bounds(selected, c)
            state = "past" if en <= now else ("current" if st <= now else "upcoming")
        elif selected < today:
            state = "past"
        lessons.append({**finish(c), "state": state})

    day_summary = ""
    if lessons:
        day_summary = f"{pairs_word(len(lessons))}, {lessons[0]['start']} – {lessons[-1]['end']}"

    sat_note = ""
    if selected.weekday() == 5 and sat_info:
        sat_note = f"Субота за {DAY_ACC[sat_info['weekday']]}."
        if not sat_info["from_site"]:
            sat_note += " Пари взято з цього дня тижня, час за суботнім дзвінком."

    year_part = f" {selected.year}" if selected.year != today.year else ""
    return render_template_string(
        MAIN_TEMPLATE,
        lessons=lessons,
        week_days=week_days,
        day_title=f"{DAY_NAMES[selected.weekday()]}, {selected.day} {MONTHS[selected.month - 1]}{year_part}",
        day_summary=day_summary,
        sat_note=sat_note,
        is_today=selected == today,
        prev_date=step_day(selected, -1).isoformat(),
        next_date=step_day(selected, 1).isoformat(),
        today_iso=today.isoformat(),
        hero_now=hero_now,
        hero_next=hero_next,
    )


STYLE = """
:root {
  --bg: #0b0f1a; --card: rgba(255,255,255,.045); --card-line: rgba(255,255,255,.09);
  --text: #f1f4fa; --muted: #98a2b8;
  --blue: #4f8cff; --blue-2: #2b5cff; --green: #34d399; --amber: #fbbf24; --red: #f87171;
}
* { box-sizing: border-box; }
html { color-scheme: dark; }
body {
  margin: 0; color: var(--text); min-height: 100vh;
  font-family: "Onest", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 16px; line-height: 1.5; -webkit-font-smoothing: antialiased;
  background:
    radial-gradient(900px 500px at 8% -8%, rgba(60, 100, 220, .28), transparent 60%),
    radial-gradient(700px 420px at 100% 0%, rgba(20, 184, 166, .18), transparent 55%),
    var(--bg);
  background-attachment: fixed;
}
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 8px; }
.wrap { max-width: 680px; margin: 0 auto; padding: 22px 16px 64px; }

.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; }
.title {
  margin: 0; font-size: 24px; font-weight: 800; letter-spacing: -.02em;
  background: linear-gradient(90deg, #8ab8ff, #7ff0c8); -webkit-background-clip: text;
  background-clip: text; color: transparent;
}
.logout {
  background: var(--card); border: 1px solid var(--card-line); color: var(--muted);
  padding: 8px 14px; border-radius: 12px; font: inherit; font-size: 14px; cursor: pointer;
}
.logout:hover { color: var(--text); border-color: rgba(255,255,255,.25); }

/* ── Наступна пара ── */
.hero {
  border-radius: 24px; padding: 22px; margin-bottom: 22px; color: #fff;
  background: linear-gradient(135deg, #2b5cff 0%, #1f8fd1 55%, #14b8a6 100%);
  box-shadow: 0 18px 40px rgba(20, 60, 160, .35);
}
.hero .tag {
  display: inline-block; font-size: 13px; font-weight: 600; padding: 4px 12px; border-radius: 999px;
  background: rgba(255,255,255,.2);
}
.countdown {
  font-size: clamp(42px, 13vw, 60px); font-weight: 800; line-height: 1.05; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; margin: 14px 0 0;
}
.hero .caption { font-size: 14px; opacity: .9; margin-bottom: 16px; }
.hero h2 { margin: 0 0 10px; font-size: 21px; font-weight: 700; line-height: 1.3; }
.hero .chips { display: flex; flex-wrap: wrap; gap: 8px; }
.hero .chip { background: rgba(255,255,255,.18); color: #fff; }
.hero-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.btn-white {
  background: #fff; color: #12306e; border: 0; border-radius: 14px; padding: 12px 18px;
  font: inherit; font-size: 15px; font-weight: 700; text-decoration: none; cursor: pointer;
}
.btn-white:hover { background: #eef4ff; }
.btn-ghost {
  background: rgba(255,255,255,.14); color: #fff; border: 1px solid rgba(255,255,255,.4);
  border-radius: 14px; padding: 12px 18px; font: inherit; font-size: 15px; cursor: pointer;
}
.btn-ghost:hover { background: rgba(255,255,255,.22); }
.btn-ghost:disabled { opacity: .75; cursor: default; }
.switch {
  display: flex; gap: 12px; align-items: flex-start; margin-top: 18px; padding-top: 16px;
  border-top: 1px solid rgba(255,255,255,.28); cursor: pointer;
}
.switch input {
  -webkit-appearance: none; appearance: none; flex: 0 0 auto; width: 46px; height: 28px; margin: 1px 0 0;
  border-radius: 14px; background: rgba(255,255,255,.3); position: relative; cursor: pointer;
  transition: background .2s;
}
.switch input::after {
  content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px;
  border-radius: 50%; background: #fff; transition: transform .2s;
}
.switch input:checked { background: #10b981; }
.switch input:checked::after { transform: translateX(18px); }
.switch b { display: block; font-weight: 700; font-size: 15px; }
.switch span { display: block; font-size: 13px; opacity: .9; line-height: 1.4; }

/* ── Дні тижня ── */
.week { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin-bottom: 22px; }
.day {
  display: flex; flex-direction: column; align-items: center; gap: 1px; padding: 10px 2px 9px;
  border-radius: 16px; background: var(--card); border: 1px solid var(--card-line);
  color: var(--text); text-decoration: none; transition: background .15s, border-color .15s;
}
.day:hover { background: rgba(255,255,255,.08); }
.day .wd { font-size: 13px; color: var(--muted); }
.day .dn { font-size: 21px; font-weight: 800; line-height: 1.25; font-variant-numeric: tabular-nums; }
.day .cnt { font-size: 11.5px; color: var(--muted); white-space: nowrap; }
.day.today { border-color: rgba(251, 191, 36, .65); }
.day.today .wd { color: var(--amber); }
.day.sel { background: linear-gradient(160deg, #3b6dff, #2b5cff); border-color: transparent; box-shadow: 0 8px 20px rgba(43, 92, 255, .4); }
.day.sel .wd, .day.sel .cnt { color: rgba(255,255,255,.85); }

/* ── Заголовок дня ── */
.dayhead { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.dayhead h2 { margin: 0; font-size: 19px; font-weight: 800; line-height: 1.3; }
.badge-today {
  display: inline-block; margin-left: 8px; white-space: nowrap; padding: 3px 10px; border-radius: 999px; vertical-align: 2px;
  font-size: 12.5px; font-weight: 700; color: var(--green); background: rgba(52, 211, 153, .15);
}
.steps { display: flex; gap: 8px; align-items: center; }
.step {
  width: 40px; height: 40px; display: grid; place-items: center; border-radius: 12px;
  background: var(--card); border: 1px solid var(--card-line); color: var(--text);
  text-decoration: none; font-size: 18px;
}
.step:hover { background: rgba(255,255,255,.09); }
.jump { font-size: 14px; color: var(--blue); text-decoration: none; padding: 0 4px; }
.jump:hover { text-decoration: underline; }

/* ── Пари ── */
.lessons { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }
.lesson {
  display: grid; grid-template-columns: 58px 1fr; gap: 14px; padding: 16px; border-radius: 20px;
  background: var(--card); border: 1px solid var(--card-line);
}
.lesson.past { opacity: .55; }
.lesson.current { border-color: rgba(52, 211, 153, .6); box-shadow: 0 0 0 3px rgba(52, 211, 153, .12); }
.num {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 58px; border-radius: 16px; background: rgba(79, 140, 255, .16); color: #a9c7ff;
}
.num b { font-size: 24px; font-weight: 800; line-height: 1; }
.num small { font-size: 11.5px; margin-top: 2px; }
.lesson.current .num { background: rgba(52, 211, 153, .18); color: var(--green); }
.name { font-size: 18px; font-weight: 700; line-height: 1.3; }
.status { display: inline-block; font-size: 12.5px; font-weight: 700; padding: 2px 10px; border-radius: 999px; margin-bottom: 6px; }
.status.live { color: var(--green); background: rgba(52, 211, 153, .15); }
.status.done { color: var(--muted); background: rgba(255,255,255,.07); }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.chip { font-size: 14px; padding: 4px 10px; border-radius: 10px; background: rgba(255,255,255,.07); color: #d5dbe8; }
.group { font-size: 13.5px; color: var(--muted); margin-top: 8px; }
.zoom, .nolink { grid-column: 1 / -1; text-align: center; }
.zoom {
  display: block; padding: 12px; border-radius: 14px; font-size: 15px; font-weight: 700;
  color: #fff; text-decoration: none; background: linear-gradient(135deg, #3b6dff, #2b8cff);
  box-shadow: 0 6px 16px rgba(43, 92, 255, .35); transition: transform .15s, box-shadow .15s;
}
.zoom:hover { transform: translateY(-1px); box-shadow: 0 10px 22px rgba(43, 92, 255, .5); }
.nolink { font-size: 13px; color: var(--muted); }
.teacher { font-size: 14.5px; font-weight: 600; color: #d5dbe8; margin-top: 8px; }
.code { font-size: 14px; margin-top: 8px; color: #d5dbe8; }
.code b, .hero-code b, .mono { font-family: ui-monospace, Menlo, Consolas, monospace; letter-spacing: .03em; }
.hero-code { margin-top: 16px; font-size: 14.5px; }
.copy {
  margin-left: 6px; padding: 3px 10px; border-radius: 8px; font: inherit; font-size: 13px; cursor: pointer;
  color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--card-line);
}
.copy:hover { background: rgba(255,255,255,.16); }
.hero .copy { color: #fff; background: rgba(255,255,255,.16); border-color: rgba(255,255,255,.45); }
.alt { color: var(--blue); }
@media (min-width: 560px) {
  .lesson { grid-template-columns: 58px 1fr auto; align-items: center; }
  .zoom, .nolink { grid-column: auto; }
  .zoom { padding: 11px 18px; white-space: nowrap; }
}
.empty { text-align: center; padding: 36px 20px; border-radius: 20px; background: var(--card); border: 1px solid var(--card-line); color: var(--muted); }
.empty .big { font-size: 34px; margin-bottom: 6px; }
.empty a { color: var(--blue); }

/* ── Верхній блок: перемикач, прогрес ── */
.seg { display: inline-flex; background: rgba(255,255,255,.18); border-radius: 12px; padding: 3px; margin-bottom: 14px; }
.seg-btn {
  border: 0; background: transparent; color: #fff; font: inherit; font-size: 14px; font-weight: 600;
  padding: 7px 14px; border-radius: 9px; cursor: pointer; opacity: .85;
}
.seg-btn.on { background: #fff; color: #12306e; opacity: 1; }
.view.hidden { display: none; }
.progress { height: 6px; border-radius: 3px; background: rgba(255,255,255,.25); margin: 0 0 16px; overflow: hidden; }
.progress i { display: block; height: 100%; width: 0; background: #fff; border-radius: 3px; transition: width 1s linear; }
.hero .countdown { margin-top: 4px; }
.countdown.long { font-size: clamp(28px, 8.5vw, 40px); }
.summary { margin: 2px 0 0; color: var(--muted); font-size: 14px; }
.satnote {
  margin: 0 0 14px; padding: 10px 14px; border-radius: 14px; font-size: 14px;
  background: rgba(251,191,36,.12); border: 1px solid rgba(251,191,36,.35); color: #fde68a;
}
.dayhead { margin-bottom: 14px; }

/* ── Вхід ── */
.login { max-width: 420px; margin: 0 auto; padding: 48px 16px; }
.panel { border-radius: 24px; padding: 26px 22px; background: var(--card); border: 1px solid var(--card-line); box-shadow: 0 20px 40px rgba(0,0,0,.35); }
.logo { font-size: 40px; line-height: 1; margin-bottom: 10px; }
.panel h1 { margin: 0 0 6px; font-size: 26px; font-weight: 800; letter-spacing: -.02em; }
.lead { color: var(--muted); margin: 0 0 22px; font-size: 15px; }
.f { display: block; font-size: 14px; font-weight: 600; margin: 0 0 6px; }
.field {
  width: 100%; padding: 13px 14px; margin-bottom: 16px; border-radius: 14px; font: inherit;
  border: 1px solid var(--card-line); background: rgba(255,255,255,.06); color: var(--text);
}
.field:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(79,140,255,.25); }
.submit {
  width: 100%; padding: 14px; border: 0; border-radius: 14px; color: #fff; font: inherit; font-weight: 700;
  font-size: 16px; cursor: pointer; background: linear-gradient(135deg, #3b6dff, #2b8cff);
  box-shadow: 0 8px 20px rgba(43, 92, 255, .4);
}
.submit:hover { filter: brightness(1.08); }
.note { display: flex; gap: 10px; margin-top: 20px; padding: 12px 14px; border-radius: 14px; font-size: 13px; line-height: 1.5; color: var(--muted); background: rgba(255,255,255,.04); }
.err { padding: 12px 14px; margin-bottom: 18px; border-radius: 14px; font-size: 14.5px; color: #fecaca; background: rgba(248,113,113,.12); border: 1px solid rgba(248,113,113,.35); }
"""

HEAD = """<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0b0f1a">
  <title>Мій розклад</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>__STYLE__</style>
</head>
""".replace("__STYLE__", STYLE)

LOGIN_TEMPLATE = HEAD + """<body>
<main class="login">
  <div class="panel">
    <div class="logo">📅</div>
    <h1>Мій розклад</h1>
    <p class="lead">Увійдіть своїм акаунтом lCloud, і ми покажемо розклад вашої групи.</p>
    {% if error %}<div class="err" role="alert">{{ error }}</div>{% endif %}
    <form method="POST" action="/login">
      <label class="f" for="login">Логін або телефон</label>
      <input class="field" id="login" type="text" name="login" autocomplete="username" maxlength="200" required autofocus>
      <label class="f" for="password">Пароль</label>
      <input class="field" id="password" type="password" name="password" autocomplete="current-password" maxlength="100" required>
      <button class="submit" type="submit">Увійти</button>
    </form>
    <div class="note"><span>🔒</span><span>Сайт неофіційний і не пов'язаний з коледжем чи lCloud. Пароль потрібен один раз, щоб увійти в lCloud, і ніде не зберігається. У браузері лишається зашифрована сесія на 12 годин, кнопка «Вийти» її видаляє.</span></div>
  </div>
</main>
</body>
</html>
"""

ERROR_TEMPLATE = HEAD + """<body>
<main class="login">
  <div class="panel">
    <h1>Не вдалося завантажити</h1>
    <div class="err" role="alert">{{ message }}</div>
    <a class="submit" style="display:block; text-align:center; text-decoration:none" href="/">Спробувати ще раз</a>
  </div>
</main>
</body>
</html>
"""

MAIN_TEMPLATE = HEAD + """<body>
<div class="wrap">

  <header class="topbar">
    <h1 class="title">📅 Мій розклад</h1>
    <form method="POST" action="/logout"><button class="logout" type="submit">Вийти</button></form>
  </header>

  {% if hero_now or hero_next %}
  {% set both = hero_now and hero_next %}
  {% set default_view = 'now' if hero_now else 'next' %}
  <section class="hero" id="hero" aria-label="Поточна та наступна пара">
    {% if both %}
    <div class="seg" role="group" aria-label="Що показати зверху">
      <button type="button" class="seg-btn on" data-view="now">Зараз іде</button>
      <button type="button" class="seg-btn" data-view="next">Наступна пара</button>
    </div>
    {% endif %}

    {% for key, item in [('now', hero_now), ('next', hero_next)] %}
    {% if item %}
    <div class="view{% if key != default_view %} hidden{% endif %}" data-view="{{ key }}"
         data-start="{{ item.start_iso }}" data-end="{{ item.end_iso }}"
         data-title="{{ item.lesson }}" data-zoom="{{ item.zoom_link or '' }}">
      {% if not both %}<span class="tag">{{ 'Зараз іде' if key == 'now' else 'Наступна пара' }}</span>{% endif %}
      <div class="countdown" data-role="cd">00:00:00</div>
      <div class="caption" data-role="cap">&nbsp;</div>
      {% if key == 'now' %}<div class="progress"><i data-role="bar"></i></div>{% endif %}
      <h2>{{ item.lesson }}</h2>
      <div class="chips">
        <span class="chip">🗓 {{ item.when }}</span>
        <span class="chip">⏰ {{ item.start }} – {{ item.end }}</span>
        <span class="chip">{{ item.pair }} пара</span>
        {% if item.teacher_short %}<span class="chip">👤 {{ item.teacher_short }}</span>{% endif %}
        {% if item.room %}<span class="chip">🏫 ауд. {{ item.room }}</span>{% endif %}
      </div>
      <div class="hero-actions">
        {% if item.zoom_link %}
          <a class="btn-white" href="{{ item.zoom_link }}" target="_blank" rel="noopener noreferrer">🎥 Підключитися до Zoom</a>
        {% endif %}
        {% if key == 'next' %}<button type="button" class="btn-ghost notify">🔔 Нагадати за 5 хв</button>{% endif %}
      </div>
      {% if item.code %}
      <div class="hero-code">🔑 Код доступу: <b>{{ item.code }}</b> <button type="button" class="copy" data-code="{{ item.code }}">Скопіювати</button></div>
      {% endif %}
    </div>
    {% endif %}
    {% endfor %}

    {% if hero_next and hero_next.zoom_link %}
    <label class="switch">
      <input type="checkbox" id="autoJoin">
      <div><b>Автопідключення</b><span>Zoom наступної пари відкриється сам за хвилину до початку. Вкладка з сайтом має бути відкрита.</span></div>
    </label>
    {% endif %}
  </section>
  {% endif %}

  <nav class="week" aria-label="Дні тижня">
    {% for d in week_days %}
      <a href="/?date={{ d.iso }}" class="day{% if d.selected %} sel{% endif %}{% if d.today %} today{% endif %}"{% if d.selected %} aria-current="date"{% endif %}>
        <span class="wd">{{ d.label }}</span>
        <span class="dn">{{ d.num }}</span>
        <span class="cnt">{{ d.count }}</span>
      </a>
    {% endfor %}
  </nav>

  <div class="dayhead">
    <div>
      <h2>{{ day_title }}{% if is_today %}<span class="badge-today">Сьогодні</span>{% endif %}</h2>
      {% if day_summary %}<p class="summary">{{ day_summary }}</p>{% endif %}
    </div>
    <div class="steps">
      {% if not is_today %}<a class="jump" href="/?date={{ today_iso }}">Сьогодні</a>{% endif %}
      <a class="step" id="prevDay" href="/?date={{ prev_date }}" aria-label="Попередній день">‹</a>
      <a class="step" id="nextDay" href="/?date={{ next_date }}" aria-label="Наступний день">›</a>
    </div>
  </div>

  {% if sat_note %}<div class="satnote">📌 {{ sat_note }}</div>{% endif %}

  {% if lessons %}
  <ol class="lessons">
    {% for item in lessons %}
    <li class="lesson {{ item.state }}">
      <div class="num"><b>{{ item.pair }}</b><small>пара</small></div>
      <div class="info">
        {% if item.state == 'current' %}<span class="status live">Зараз іде</span>{% elif item.state == 'past' %}<span class="status done">Завершилась</span>{% endif %}
        <div class="name">{{ item.lesson }}</div>
        <div class="chips">
          <span class="chip">⏰ {{ item.start }} – {{ item.end }}</span>
          {% if item.room %}<span class="chip">🏫 ауд. {{ item.room }}</span>{% endif %}
        </div>
        {% if item.teacher %}<div class="teacher">👤 {{ item.teacher }}</div>{% endif %}
        {% if item.group %}<div class="group">👥 {{ item.group }}</div>{% endif %}
        {% if item.code %}<div class="code">🔑 Код доступу: <b>{{ item.code }}</b> <button type="button" class="copy" data-code="{{ item.code }}">Скопіювати</button></div>{% endif %}
        {% if item.backup_url %}<div class="group">Запасний варіант: <a class="alt" href="{{ item.backup_url }}" target="_blank" rel="noopener noreferrer">Zoom викладача</a>, код <b class="mono">{{ item.backup_code }}</b></div>{% endif %}
      </div>
      {% if item.zoom_link %}
        <a class="zoom" href="{{ item.zoom_link }}" target="_blank" rel="noopener noreferrer">🎥 Підключитися до Zoom</a>
      {% else %}
        <span class="nolink">Посилання на Zoom немає</span>
      {% endif %}
    </li>
    {% endfor %}
  </ol>
  {% else %}
  <div class="empty">
    <div class="big">🎉</div>
    На цей день занять немає.<br><a href="/?date={{ next_date }}">Подивитись наступний день</a>
  </div>
  {% endif %}
</div>

<script>
/* Кнопка «Скопіювати» біля кодів доступу */
(function () {
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('.copy') : null;
    if (!b) return;
    var code = b.dataset.code, old = b.textContent;
    function done() { b.textContent = 'Скопійовано'; setTimeout(function () { b.textContent = old; }, 1500); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(done, done);
    } else {
      var ta = document.createElement('textarea'); ta.value = code; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (err) {}
      document.body.removeChild(ta); done();
    }
  });
})();

/* Стрілки ← → перемикають дні */
document.addEventListener('keydown', function (e) {
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  var id = e.key === 'ArrowLeft' ? 'prevDay' : (e.key === 'ArrowRight' ? 'nextDay' : null);
  var a = id && document.getElementById(id);
  if (a) a.click();
});

/* Верхній блок: відлік, перемикач, автооновлення, автопідключення */
(function () {
  var loadedAt = Date.now(), loadedDay = new Date().toDateString(), baseTitle = document.title;

  function store(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
  }
  // Автооновлення сторінки (не частіше 3 разів за 2 хв, щоб не зациклитись при неточному годиннику)
  function refresh() {
    try {
      var now = Date.now();
      var arr = JSON.parse(sessionStorage.getItem('reloads') || '[]').filter(function (t) { return now - t < 120000; });
      if (arr.length >= 3) return;
      arr.push(now);
      sessionStorage.setItem('reloads', JSON.stringify(arr));
    } catch (e) {}
    location.reload();
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && Date.now() - loadedAt > 60000) refresh();
  });

  var hero = document.getElementById('hero');
  if (!hero) return;

  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmt(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60); s -= m * 60;
    return (d ? d + ' д ' : '') + pad(h) + ':' + pad(m) + ':' + pad(s);
  }
  function human(ms) {
    var m = Math.max(0, Math.ceil(ms / 60000));
    if (m < 1) return 'менше хвилини';
    var d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60, out = [];
    if (d) out.push(d + ' д');
    if (h) out.push(h + ' год');
    if (mm && !d) out.push(mm + ' хв');
    return out.join(' ');
  }

  var panels = {}, active = null;
  [].forEach.call(hero.querySelectorAll('.view'), function (p) {
    panels[p.dataset.view] = {
      el: p, start: new Date(p.dataset.start), end: new Date(p.dataset.end),
      title: p.dataset.title, zoom: p.dataset.zoom,
      cd: p.querySelector('[data-role=cd]'), cap: p.querySelector('[data-role=cap]'),
      bar: p.querySelector('[data-role=bar]')
    };
    if (!p.classList.contains('hidden')) active = p.dataset.view;
  });

  // Перемикач «Зараз іде» / «Наступна пара»
  [].forEach.call(hero.querySelectorAll('.seg-btn'), function (btn) {
    btn.addEventListener('click', function () {
      active = btn.dataset.view;
      [].forEach.call(hero.querySelectorAll('.seg-btn'), function (b) { b.classList.toggle('on', b === btn); });
      for (var k in panels) panels[k].el.classList.toggle('hidden', k !== active);
      tick();
    });
  });

  // Автопідключення до Zoom наступної пари
  var auto = document.getElementById('autoJoin');
  var nextP = panels.next, nowP = panels.now;
  var doneKey = nextP ? 'autojoin_done_' + nextP.el.dataset.start : '';
  var openedBeforeStart = nextP ? new Date() < nextP.start : false;
  if (auto) {
    auto.checked = store('autojoin') === '1';
    auto.addEventListener('change', function () { store('autojoin', auto.checked ? '1' : '0'); });
  }

  // Нагадування за 5 хвилин
  var notify = hero.querySelector('.notify');
  if (notify && nextP) {
    notify.addEventListener('click', function () {
      if (!('Notification' in window)) { notify.textContent = 'Браузер не підтримує сповіщень'; return; }
      Notification.requestPermission().then(function (perm) {
        if (perm !== 'granted') { notify.textContent = 'Сповіщення заборонені'; return; }
        var wait = nextP.start - new Date() - 5 * 60 * 1000;
        if (wait <= 0) { notify.textContent = 'Пара вже скоро почнеться'; return; }
        setTimeout(function () { new Notification('Пара через 5 хвилин', { body: nextP.title }); }, wait);
        notify.textContent = '✅ Нагадаю (не закривайте вкладку)';
        notify.disabled = true;
      });
    });
  }

  function tick() {
    var now = new Date();
    if (new Date().toDateString() !== loadedDay) { refresh(); return; }

    if (nowP) {
      var left = nowP.end - now;
      if (left < -2000) { refresh(); return; }               // пара закінчилась: оновлюємо сторінку
      nowP.cd.textContent = fmt(left);
      nowP.cd.classList.toggle('long', nowP.cd.textContent.length > 9);
      nowP.cap.textContent = 'до кінця пари: ' + human(left);
      if (nowP.bar) nowP.bar.style.width = Math.min(100, Math.max(0, (now - nowP.start) / (nowP.end - nowP.start) * 100)) + '%';
    }
    if (nextP) {
      var wait = nextP.start - now;
      if (wait < -2000) { refresh(); return; }               // наступна пара почалась: оновлюємо сторінку
      nextP.cd.textContent = fmt(wait);
      nextP.cd.classList.toggle('long', nextP.cd.textContent.length > 9);
      nextP.cap.textContent = 'до наступної пари: ' + human(wait);
      if (auto && auto.checked && nextP.zoom && openedBeforeStart && !store(doneKey) &&
          now >= new Date(nextP.start.getTime() - 60 * 1000) && now < nextP.end) {
        store(doneKey, '1');
        window.location.href = nextP.zoom;
        return;
      }
    }
    // Заголовок вкладки з відліком, щоб було видно, навіть коли вкладка у фоні
    var cur = panels[active];
    if (cur) {
      var ms = (active === 'now' ? cur.end : cur.start) - now;
      document.title = human(ms) + ' · ' + cur.title;
    }
  }
  tick();
  setInterval(tick, 1000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)