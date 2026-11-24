import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, make_response, redirect, render_template_string, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

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
    raise RuntimeError("Задайте SECRET_KEY як випадковий рядок щонайменше з 32 символів.")
if len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY має містити щонайменше 32 символи.")
fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))
csrf_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="csrf-token")

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


def make_csrf_token():
    return csrf_serializer.dumps(secrets.token_urlsafe(32))


def valid_csrf_token(token):
    if not token:
        return False
    try:
        csrf_serializer.loads(token, max_age=3600)
        return True
    except BadSignature:
        return False


# Обмеження НЕВДАЛИХ спроб входу (в межах одного інстанса сервера).
# Вдалі входи не рахуються, щоб клас в одній мережі не блокував сам себе.
_FAILED = {}
MAX_FAILED = 20


def _client_ip():
    return request.remote_addr or "?"


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
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
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
    if not valid_csrf_token(request.form.get("csrf_token")):
        return render_template_string(
            LOGIN_TEMPLATE, error="Форма застаріла. Оновіть сторінку.", csrf_token=make_csrf_token()), 400
    if is_blocked():
        return render_template_string(
            LOGIN_TEMPLATE, error="Забагато спроб. Спробуйте за 10 хвилин.",
            csrf_token=make_csrf_token()), 429
    login_value = (request.form.get("login") or "").strip()
    password = request.form.get("password") or ""
    if not login_value or not password or len(login_value) > 200 or len(password) > 100:
        return render_template_string(
            LOGIN_TEMPLATE, error="Перевірте логін і пароль.", csrf_token=make_csrf_token()), 400
    ok, msg, cookies = lcloud_login(login_value, password)
    password = None  # пароль більше не потрібен
    if not ok:
        record_failure()
        return render_template_string(LOGIN_TEMPLATE, error=msg, csrf_token=make_csrf_token()), 401
    resp = make_response(redirect("/"))
    set_session(resp, cookies)
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    if not valid_csrf_token(request.form.get("csrf_token")):
        return "Форма застаріла. Оновіть сторінку.", 400
    resp = make_response(redirect("/"))
    clear_session(resp)
    return resp


@app.route("/favicon.ico")
def favicon():
    resp = make_response("""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#294b39"/>
<text x="32" y="42" text-anchor="middle" fill="#fff" font-family="Georgia,serif" font-size="24" font-weight="700">МР</text>
</svg>""")
    resp.headers["Content-Type"] = "image/svg+xml"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/")
def home():
    cookies = read_session()
    if not cookies:
        return render_template_string(LOGIN_TEMPLATE, error=None, csrf_token=make_csrf_token())

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
            LOGIN_TEMPLATE, error="Сесія завершилась. Увійдіть ще раз.",
            csrf_token=make_csrf_token()))
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
        csrf_token=make_csrf_token(),
    )


STYLE = """
:root {
  color-scheme: light;
  --bg: #f2efe7; --card: #fffdf8; --card-line: #e4ddcf;
  --text: #292b25; --muted: #777267;
  --blue: #294b39; --blue-2: #1e392c; --green: #4d7554; --amber: #ad7435; --red: #a64e3d;
  --accent: #c76d4b;
}
* { box-sizing: border-box; }
body {
  margin: 0; color: var(--text); min-height: 100vh;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px; line-height: 1.55; -webkit-font-smoothing: antialiased;
  background-color: var(--bg);
  background-image: linear-gradient(90deg, transparent 0 96%, rgba(86,72,48,.035) 96% 96.15%, transparent 96.15%);
  background-size: 56px 100%;
}
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 8px; }
.wrap { max-width: 860px; margin: 0 auto; padding: 32px 24px 72px; }

.topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; padding-bottom: 17px; border-bottom: 1px solid #d8d0c0; }
.eyebrow { margin: 0 0 5px; color: var(--muted); font-size: 10px; font-weight: 700; letter-spacing: .14em; }
.title {
  margin: 0; color: var(--text); font: 700 25px/1.1 Georgia, "Times New Roman", serif; letter-spacing: -.035em;
}
.logout {
  background: transparent; border: 1px solid #d8d0c0; color: var(--muted);
  padding: 8px 14px; border-radius: 5px; font: inherit; font-size: 13px; cursor: pointer;
}
.logout:hover { color: var(--text); border-color: #aaa18e; }

/* ── Наступна пара ── */
.hero {
  position: relative; overflow: hidden; border-radius: 10px; padding: 28px 30px; margin-bottom: 28px;
  color: #f6f1e7; background: #294b39; border: 1px solid #294b39;
}
.hero::after {
  content: ""; position: absolute; top: 0; right: 0; width: 5px; height: 100%; background: var(--accent);
}
.hero .tag {
  display: inline-block; color: #e5b391; font-size: 11px; font-weight: 700;
  letter-spacing: .12em; text-transform: uppercase;
}
.countdown {
  color: #f7f1e6; font: 600 clamp(48px, 9vw, 68px)/1 Georgia, "Times New Roman", serif; letter-spacing: -.04em;
  font-variant-numeric: tabular-nums; margin: 14px 0 0;
}
.hero .caption { color: #c8d0c7; font-size: 13px; margin-bottom: 18px; }
.hero h2 { margin: 0 0 10px; color: #fffaf0; font: 600 23px/1.3 Georgia, "Times New Roman", serif; }
.hero .chips { display: flex; flex-wrap: wrap; gap: 8px; }
.hero .chip { background: transparent; color: #d7dfd4; padding: 0; }
.hero-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.btn-white {
  background: var(--accent); color: #fffaf5; border: 0; border-radius: 5px; padding: 10px 15px;
  font: inherit; font-size: 13px; font-weight: 650; text-decoration: none; cursor: pointer;
}
.btn-white:hover { background: #ad593a; }
.btn-ghost {
  background: transparent; color: #f4eee3; border: 1px solid #718477;
  border-radius: 5px; padding: 9px 14px; font: inherit; font-size: 13px; cursor: pointer;
}
.btn-ghost:hover { background: rgba(255,255,255,.08); }
.btn-ghost:disabled { opacity: .75; cursor: default; }
.switch {
  display: flex; gap: 12px; align-items: flex-start; margin-top: 18px; padding-top: 16px;
  border-top: 1px solid #536e5d; cursor: pointer;
}
.switch input {
  -webkit-appearance: none; appearance: none; flex: 0 0 auto; width: 46px; height: 28px; margin: 1px 0 0;
  border-radius: 14px; background: #c3c9c3; position: relative; cursor: pointer;
  transition: background .2s;
}
.switch input::after {
  content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px;
  border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.2); transition: transform .2s;
}
.switch input:checked { background: var(--accent); }
.switch input:checked::after { transform: translateX(18px); }
.switch b { display: block; color: #fffaf0; font-weight: 650; font-size: 14px; }
.switch span { display: block; color: #c8d0c7; font-size: 13px; line-height: 1.45; }

/* ── Дні тижня ── */
.week { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; margin-bottom: 28px; }
.day {
  display: flex; flex-direction: column; align-items: center; gap: 1px; padding: 9px 2px;
  border-radius: 7px; background: transparent; border: 1px solid transparent;
  color: var(--text); text-decoration: none; transition: background .15s, border-color .15s;
}
.day:hover { background: #ebe9e3; }
.day .wd { color: var(--muted); font-size: 12px; }
.day .dn { font-size: 20px; font-weight: 650; line-height: 1.35; font-variant-numeric: tabular-nums; }
.day .cnt { color: var(--muted); font-size: 11px; white-space: nowrap; }
.day.today:not(.sel) { border-color: #c9b991; }
.day.today:not(.sel) .wd { color: var(--amber); }
.day.sel { background: #294b39; border-color: #294b39; color: #fffaf0; }
.day.sel .wd, .day.sel .cnt { color: rgba(255,255,255,.8); }

.dayhead { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.dayhead h2 { margin: 0; font: 600 22px/1.25 Georgia, "Times New Roman", serif; }
.badge-today {
  display: inline-block; margin-left: 8px; white-space: nowrap; padding: 2px 8px; border-radius: 5px; vertical-align: 2px;
  font-size: 12px; font-weight: 650; color: #294b39; background: #e8eee6;
}
.steps { display: flex; gap: 8px; align-items: center; }
.step {
  width: 38px; height: 38px; display: grid; place-items: center; border-radius: 7px;
  background: var(--card); border: 1px solid var(--card-line); color: var(--text);
  text-decoration: none; font-size: 19px;
}
.step:hover { background: #f0efe9; }
.jump { font-size: 14px; color: var(--blue); text-decoration: none; padding: 0 4px; }
.jump:hover { text-decoration: underline; }

.lessons { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; }
.lesson {
  display: grid; grid-template-columns: 52px 1fr; gap: 16px; padding: 17px; border-radius: 7px;
  background: var(--card); border: 1px solid var(--card-line); box-shadow: 0 2px 6px rgba(49,42,28,.035);
}
.lesson.past { opacity: .62; }
.lesson.current { border-left: 3px solid var(--accent); }
.num {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  height: 52px; border-radius: 5px; background: #f0ede5; color: #294b39;
}
.num b { font-size: 22px; font-weight: 700; line-height: 1; }
.num small { font-size: 11px; margin-top: 2px; }
.lesson.current .num { background: #f5e7dd; color: #a75439; }
.name { font: 600 18px/1.35 Georgia, "Times New Roman", serif; }
.status { display: inline-block; font-size: 12px; font-weight: 650; padding: 2px 8px; border-radius: 5px; margin-bottom: 5px; }
.status.live { color: #294b39; background: #e8eee6; }
.status.done { color: var(--muted); background: #f0efe9; }
.chips { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 7px; }
.chip { font-size: 13px; padding: 2px 0; color: #585d56; }
.group { font-size: 13px; color: var(--muted); margin-top: 7px; }
.zoom, .nolink { grid-column: 1 / -1; text-align: center; }
.zoom {
  display: block; padding: 10px 14px; border-radius: 7px; font-size: 14px; font-weight: 650;
  color: #294b39; text-decoration: none; background: #edf1e9; border: 1px solid #d9e0d4;
  transition: background .15s, border-color .15s;
}
.zoom:hover { background: #e3eadd; border-color: #b8c5b4; }
.nolink { color: var(--muted); font-size: 13px; }
.teacher { color: #4c534c; font-size: 14px; font-weight: 550; margin-top: 7px; }
.code { color: #4c534c; font-size: 14px; margin-top: 7px; }
.code b, .hero-code b, .mono { font-family: ui-monospace, Menlo, Consolas, monospace; letter-spacing: .03em; }
.hero-code { margin-top: 16px; font-size: 14px; }
.copy {
  margin-left: 5px; padding: 2px 8px; border-radius: 5px; font: inherit; font-size: 12px; cursor: pointer;
  color: #294b39; background: transparent; border: 1px solid #c8d0c3;
}
.copy:hover { background: #edf1e9; }
.hero .copy { color: #f6f1e7; background: transparent; border-color: #718477; }
.alt { color: var(--blue); }
@media (min-width: 640px) {
  .lesson { grid-template-columns: 50px 1fr auto; align-items: center; }
  .zoom, .nolink { grid-column: auto; }
  .zoom { padding: 9px 13px; white-space: nowrap; }
}
.empty { text-align: center; padding: 32px 20px; border-radius: 8px; background: var(--card); border: 1px solid var(--card-line); color: var(--muted); }
.empty .big { color: var(--text); font-size: 18px; font-weight: 650; margin-bottom: 4px; }
.empty a { color: var(--blue); }

/* ── Верхній блок: перемикач, прогрес ── */
.seg { display: inline-flex; background: #dce5dc; border-radius: 6px; padding: 3px; margin-bottom: 14px; }
.seg-btn {
  border: 0; background: transparent; color: #4f5b52; font: inherit; font-size: 13px; font-weight: 600;
  padding: 6px 11px; border-radius: 4px; cursor: pointer;
}
.seg-btn.on { background: #fff; color: #244a39; }
.view.hidden { display: none; }
.progress { height: 4px; border-radius: 2px; background: #d1dbd2; margin: 0 0 16px; overflow: hidden; }
.progress i { display: block; height: 100%; width: 0; background: #34745a; border-radius: 2px; transition: width 1s linear; }
.hero .countdown { margin-top: 4px; }
.countdown.long { font-size: clamp(28px, 8.5vw, 40px); }
.summary { margin: 2px 0 0; color: var(--muted); font-size: 14px; }
.satnote {
  margin: 0 0 14px; padding: 10px 13px; border-radius: 7px; font-size: 13px;
  background: #f3eee3; border: 1px solid #e6dcc5; color: #735a30;
}
.dayhead { margin-bottom: 14px; }

/* ── Вхід ── */
.login {
  display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(330px, .75fr);
  align-items: center; gap: clamp(36px, 7vw, 92px); max-width: 980px;
  min-height: 100vh; margin: 0 auto; padding: 56px 28px;
}
.login-intro { max-width: 500px; }
.login-intro h1 {
  margin: 18px 0 16px; color: #294b39;
  font: 600 clamp(42px, 6vw, 68px)/1.02 Georgia, "Times New Roman", serif;
  letter-spacing: -.055em;
}
.login-intro > p:not(.eyebrow) { max-width: 410px; margin: 0; color: var(--muted); font-size: 16px; }
.intro-note { margin-top: 30px; padding-left: 13px; border-left: 2px solid var(--accent); color: #625f55; font-size: 13px; }
.panel { border-radius: 8px; padding: 30px 28px; background: var(--card); border: 1px solid var(--card-line); box-shadow: 0 12px 32px rgba(49,42,28,.06); }
.login > .panel:only-child { grid-column: 1 / -1; width: min(100%, 440px); justify-self: center; }
.logo { color: var(--accent); font-size: 10px; font-weight: 700; letter-spacing: .14em; margin-bottom: 12px; }
.panel h1, .panel h2 { margin: 0 0 6px; color: var(--text); font: 600 23px/1.25 Georgia, "Times New Roman", serif; }
.lead { color: var(--muted); margin: 0 0 22px; font-size: 13px; }
.f { display: block; font-size: 13px; font-weight: 600; margin: 0 0 6px; }
.field {
  width: 100%; padding: 11px 12px; margin-bottom: 15px; border-radius: 6px; font: inherit;
  border: 1px solid #d8d1c5; background: #fffefa; color: var(--text);
}
.field:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(49,93,75,.12); }
.submit {
  width: 100%; padding: 12px; border: 0; border-radius: 6px; color: #fff; font: inherit; font-weight: 650;
  font-size: 14px; cursor: pointer; background: #294b39;
}
.submit:hover { background: #1e392c; }
.retry { display: block; text-align: center; text-decoration: none; }
.note { display: flex; gap: 10px; margin-top: 18px; padding: 11px 12px; border-radius: 6px; font-size: 12px; line-height: 1.55; color: var(--muted); background: #f5f4f0; }
.err { padding: 11px 12px; margin-bottom: 16px; border-radius: 6px; font-size: 14px; color: #853b32; background: #fbefed; border: 1px solid #edd1cc; }
@media (max-width: 700px) {
  .login { grid-template-columns: 1fr; gap: 28px; max-width: 560px; min-height: auto; padding: 48px 20px; }
  .login-intro { max-width: none; }
  .login-intro h1 { margin: 12px 0; font-size: clamp(38px, 10vw, 54px); }
  .intro-note { margin-top: 18px; }
}
@media (max-width: 520px) {
  .wrap { padding: 18px 14px 48px; }
  .topbar { margin-bottom: 20px; }
  .hero { padding: 22px 18px; }
  .week { gap: 2px; }
  .day .wd { font-size: 11px; }
  .day .dn { font-size: 18px; }
  .day .cnt { font-size: 10px; }
  .lesson { padding: 12px; gap: 10px; }
  .login { padding: 32px 16px; }
  .panel { padding: 25px 20px; }
  .hero::after { width: 4px; }
}

/* ── Фінальна візуальна система ── */
.wrap { max-width: 920px; }
.topbar-brand { display: flex; align-items: center; gap: 12px; }
.brand-mark {
  width: 42px; height: 42px; display: grid; place-items: center; border-radius: 13px;
  color: #fff; background: #294b39; font: 700 13px/1 Georgia, serif; letter-spacing: -.04em;
  box-shadow: 0 8px 18px rgba(41,75,57,.16);
}
.topbar { margin-bottom: 22px; }
.logout { border-radius: 10px; transition: .18s ease; }

.hero {
  border-radius: 20px; padding: 30px 32px; margin-bottom: 24px;
  background: linear-gradient(135deg, #294b39 0%, #345d49 70%, #3b6651 100%);
  box-shadow: 0 18px 44px rgba(41,75,57,.16);
  isolation: isolate;
}
.hero::before {
  content: ""; position: absolute; width: 220px; height: 220px; right: -70px; bottom: -130px;
  border: 32px solid rgba(255,255,255,.055); border-radius: 50%; z-index: -1;
}
.hero::after { width: 6px; border-radius: 0 20px 20px 0; }
.seg { background: rgba(255,255,255,.11); border: 1px solid rgba(255,255,255,.09); border-radius: 10px; }
.seg-btn { border-radius: 7px; color: #dfe7df; }
.seg-btn.on { color: #294b39; box-shadow: 0 4px 12px rgba(0,0,0,.08); }
.countdown { letter-spacing: -.055em; }
.hero h2 { font-size: clamp(21px, 4vw, 27px); }
.btn-white { border-radius: 10px; box-shadow: 0 8px 20px rgba(199,109,75,.18); }
.btn-ghost { border-radius: 10px; }
.switch { border-radius: 12px; }

.week {
  gap: 7px; padding: 7px; margin-bottom: 24px; border-radius: 16px;
  background: rgba(255,253,248,.72); border: 1px solid var(--card-line);
  box-shadow: 0 8px 24px rgba(49,42,28,.04);
}
.day { border-radius: 11px; padding: 8px 3px; transition: transform .18s ease, background .18s ease, box-shadow .18s ease; }
.day:hover { transform: translateY(-2px); background: #f1efe7; }
.day.sel { background: #294b39; box-shadow: 0 7px 16px rgba(41,75,57,.16); }
.day.sel .dn { font-family: Georgia, serif; }

.dayhead { margin: 4px 2px 14px; }
.steps { gap: 6px; }
.step { width: 40px; height: 40px; border-radius: 11px; transition: .18s ease; }
.step:hover { transform: translateY(-1px); border-color: #c8cbbf; box-shadow: 0 6px 14px rgba(49,42,28,.06); }
.lesson {
  grid-template-columns: 58px 1fr; gap: 18px; border-radius: 16px; padding: 20px;
  box-shadow: 0 8px 24px rgba(49,42,28,.045);
  transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
}
.lesson:hover { transform: translateY(-2px); box-shadow: 0 13px 28px rgba(49,42,28,.075); border-color: #d5cdbb; }
.lesson.current { border-left-width: 4px; background: linear-gradient(90deg, #fffaf4 0%, #fffdf8 38%); }
.num { height: 58px; border-radius: 13px; background: #ecefe7; }
.lesson.current .num { background: #f3dfd2; }
.name { font-size: 20px; }
.status { border-radius: 999px; }
.zoom { border-radius: 11px; padding: 11px 16px; }
.empty { border-radius: 16px; }

.login {
  position: relative; overflow: hidden; max-width: 1040px;
  grid-template-columns: minmax(0, 1fr) minmax(340px, 430px);
  gap: clamp(48px, 8vw, 100px); padding: 60px 32px;
}
.login::before {
  content: ""; position: fixed; width: 420px; height: 420px; left: -260px; bottom: -220px;
  border: 70px solid rgba(41,75,57,.045); border-radius: 50%; pointer-events: none;
}
.login-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 36px; color: #294b39; }
.login-brand span {
  width: 36px; height: 36px; display: grid; place-items: center; border-radius: 11px;
  color: #fff; background: #294b39; font: 700 11px/1 Georgia, serif;
}
.login-brand b { font-size: 14px; letter-spacing: -.01em; }
.login-intro h1 {
  max-width: 590px; font-size: clamp(48px, 6vw, 72px); line-height: 1;
  background: linear-gradient(135deg, #294b39 20%, #66785e 100%);
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.login-intro > p:not(.eyebrow) { font-size: 17px; max-width: 440px; }
.intro-note { max-width: 430px; border-left-width: 3px; }
.panel {
  border-radius: 20px; padding: 34px; border-color: rgba(220,211,194,.9);
  box-shadow: 0 24px 60px rgba(49,42,28,.10);
  animation: panelIn .55s cubic-bezier(.2,.8,.2,1) both;
}
.logo { color: #a95f43; }
.panel h2 { font-size: 26px; }
.field { border-radius: 11px; height: 48px; margin-bottom: 16px; background: #fff; transition: .18s ease; }
.field:hover { border-color: #bfc5b8; }
.field:focus { border-color: #294b39; box-shadow: 0 0 0 4px rgba(41,75,57,.10); }
.password-wrap { position: relative; }
.password-wrap .field { padding-right: 82px; }
.password-toggle {
  position: absolute; right: 10px; top: 13px; border: 0; background: transparent;
  color: #294b39; font: 650 12px/1 inherit; cursor: pointer;
}
.submit { border-radius: 11px; height: 49px; background: #294b39; box-shadow: 0 10px 20px rgba(41,75,57,.14); }
.note { border-radius: 11px; }

@keyframes panelIn {
  from { opacity: 0; transform: translateY(16px); }
  to { opacity: 1; transform: translateY(0); }
}

@media (max-width: 700px) {
  .login { grid-template-columns: 1fr; gap: 26px; padding: 38px 18px 48px; }
  .login-brand { margin-bottom: 22px; }
  .login-intro h1 { font-size: clamp(43px, 13vw, 58px); }
  .intro-note { margin-top: 20px; }
  .panel { padding: 28px 22px; border-radius: 18px; }
}
@media (max-width: 520px) {
  .wrap { padding: 16px 12px 44px; }
  .topbar { align-items: center; }
  .brand-mark { width: 38px; height: 38px; border-radius: 11px; }
  .hero { border-radius: 16px; padding: 22px 18px; }
  .week { gap: 2px; border-radius: 14px; }
  .day { border-radius: 9px; }
  .lesson { grid-template-columns: 48px 1fr; gap: 12px; padding: 15px; border-radius: 14px; }
  .lesson .zoom, .lesson .nolink { grid-column: 1 / -1; }
  .num { width: 48px; height: 52px; border-radius: 11px; }
  .name { font-size: 18px; }
  .dayhead h2 { font-size: 20px; }
  .badge-today { display: none; }
  .jump { display: none; }
}

@media (min-width: 640px) {
  .lesson { grid-template-columns: 58px minmax(0, 1fr) auto; align-items: center; }
  .lesson .zoom, .lesson .nolink { grid-column: auto; }
  .lesson .zoom { max-width: 190px; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    scroll-behavior: auto !important;
    animation-duration: .01ms !important;
    transition-duration: .01ms !important;
  }
}
"""

HEAD = """<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f4f3ef">
  <link rel="icon" href="/favicon.ico" type="image/svg+xml">
  <title>Мій розклад</title>
  <style>__STYLE__</style>
</head>
""".replace("__STYLE__", STYLE)

LOGIN_TEMPLATE = HEAD + """<body>
<main class="login">
  <section class="login-intro">
    <div class="login-brand"><span>МР</span><b>Мій розклад</b></div>
    <p class="eyebrow">lCloud · особистий розклад</p>
    <h1>Пари — за планом.<br>Не навмання.</h1>
    <p>Час, аудиторії, викладачі й посилання на онлайн-заняття — в одному місці.</p>
    <div class="intro-note">Увійдіть через свій акаунт lCloud, щоб побачити розклад групи.</div>
  </section>
  <div class="panel">
    <div class="logo">ВХІД ДО РОЗКЛАДУ</div>
    <h2>Увійти в lCloud</h2>
    <p class="lead">Використайте логін і пароль від свого акаунта.</p>
    {% if error %}<div class="err" role="alert">{{ error }}</div>{% endif %}
    <form id="login-form" method="POST" action="/login">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <label class="f" for="login">Логін або телефон</label>
      <input class="field" id="login" type="text" name="login" autocomplete="username" maxlength="200" required autofocus>
      <label class="f" for="password">Пароль</label>
      <div class="password-wrap">
        <input class="field" id="password" type="password" name="password" autocomplete="current-password" maxlength="100" required>
        <button class="password-toggle" type="button" aria-label="Показати пароль" aria-pressed="false">Показати</button>
      </div>
      <button class="submit" type="submit">Увійти</button>
    </form>
    <div class="note"><span>Сайт неофіційний і не пов’язаний з коледжем чи lCloud. Пароль не зберігається; в браузері залишається зашифрована сесія на 12 годин. Після автозаповнення перевірте дані й натисніть «Увійти».</span></div>
  </div>
</main>
<script>
  (function () {
    var password = document.getElementById("password");
    var passwordToggle = document.querySelector('.password-toggle');
    var form = document.getElementById("login-form");
    var submit = form ? form.querySelector('.submit') : null;

    if (passwordToggle) {
      passwordToggle.addEventListener('click', function () {
        var show = password.type === 'password';
        password.type = show ? 'text' : 'password';
        passwordToggle.textContent = show ? 'Сховати' : 'Показати';
        passwordToggle.setAttribute('aria-pressed', show ? 'true' : 'false');
        passwordToggle.setAttribute('aria-label', show ? 'Сховати пароль' : 'Показати пароль');
      });
    }

    if (form && submit) {
      form.addEventListener('submit', function () {
        if (!form.checkValidity()) return;
        submit.disabled = true;
        submit.setAttribute('aria-busy', 'true');
        submit.textContent = 'Входимо…';
      });
    }
  })();
</script>
</body>
</html>
"""

ERROR_TEMPLATE = HEAD + """<body>
<main class="login">
  <div class="panel">
    <h1>Не вдалося завантажити</h1>
    <div class="err" role="alert">{{ message }}</div>
    <a class="submit retry" href="/">Спробувати ще раз</a>
  </div>
</main>
</body>
</html>
"""

MAIN_TEMPLATE = HEAD + """<body>
<div class="wrap">

  <header class="topbar">
    <div class="topbar-brand">
      <span class="brand-mark" aria-hidden="true">МР</span>
      <div>
      <p class="eyebrow">РОЗКЛАД НАВЧАНЬ</p>
      <h1 class="title">Мій розклад</h1>
      </div>
    </div>
    <form method="POST" action="/logout">
      <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
      <button class="logout" type="submit">Вийти</button>
    </form>
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
        <span class="chip">{{ item.when }}</span>
        <span class="chip">{{ item.start }} – {{ item.end }}</span>
        <span class="chip">{{ item.pair }} пара</span>
        {% if item.teacher_short %}<span class="chip">{{ item.teacher_short }}</span>{% endif %}
        {% if item.room %}<span class="chip">Ауд. {{ item.room }}</span>{% endif %}
      </div>
      <div class="hero-actions">
        {% if item.zoom_link %}
          <a class="btn-white" href="{{ item.zoom_link }}" target="_blank" rel="noopener noreferrer">Підключитися до Zoom</a>
        {% endif %}
        {% if key == 'next' %}<button type="button" class="btn-ghost notify">Нагадати за 5 хв</button>{% endif %}
      </div>
      {% if item.code %}
      <div class="hero-code">Код доступу: <b>{{ item.code }}</b> <button type="button" class="copy" data-code="{{ item.code }}">Скопіювати</button></div>
      {% endif %}
    </div>
    {% endif %}
    {% endfor %}

    {% if hero_next and hero_next.zoom_link %}
    <label class="switch">
      <input type="checkbox" id="autoJoin">
      <div><b>Автопідключення до Zoom</b><span>Посилання на наступну пару відкриється за хвилину до початку, якщо ця вкладка буде відкрита.</span></div>
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

  {% if sat_note %}<div class="satnote">{{ sat_note }}</div>{% endif %}

  {% if lessons %}
  <ol class="lessons">
    {% for item in lessons %}
    <li class="lesson {{ item.state }}">
      <div class="num"><b>{{ item.pair }}</b><small>пара</small></div>
      <div class="info">
        {% if item.state == 'current' %}<span class="status live">Зараз іде</span>{% elif item.state == 'past' %}<span class="status done">Завершилась</span>{% endif %}
        <div class="name">{{ item.lesson }}</div>
        <div class="chips">
          <span class="chip">{{ item.start }} – {{ item.end }}</span>
          {% if item.room %}<span class="chip">Ауд. {{ item.room }}</span>{% endif %}
        </div>
        {% if item.teacher %}<div class="teacher">{{ item.teacher }}</div>{% endif %}
        {% if item.group %}<div class="group">Група: {{ item.group }}</div>{% endif %}
        {% if item.code %}<div class="code">Код доступу: <b>{{ item.code }}</b> <button type="button" class="copy" data-code="{{ item.code }}" aria-live="polite" aria-atomic="true">Скопіювати</button></div>{% endif %}
        {% if item.backup_url %}<div class="group">Запасний варіант: <a class="alt" href="{{ item.backup_url }}" target="_blank" rel="noopener noreferrer">Zoom викладача</a>, код <b class="mono">{{ item.backup_code }}</b></div>{% endif %}
      </div>
      {% if item.zoom_link %}
        <a class="zoom" href="{{ item.zoom_link }}" target="_blank" rel="noopener noreferrer">Підключитися до Zoom</a>
      {% else %}
        <span class="nolink">Посилання на Zoom немає</span>
      {% endif %}
    </li>
    {% endfor %}
  </ol>
  {% else %}
  <div class="empty">
    <div class="big">Занять немає</div>
    <a href="/?date={{ next_date }}">Подивитись наступний день</a>
  </div>
  {% endif %}
</div>

<script>
/* Кнопка «Скопіювати» біля кодів доступу */
(function () {
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var copied = false;
    try { copied = document.execCommand('copy') === true; }
    catch (err) { copied = false; }
    document.body.removeChild(ta);
    return copied;
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('.copy') : null;
    if (!b) return;
    var code = b.dataset.code, old = b.textContent;
    function done(success) {
      b.textContent = success ? 'Скопійовано' : 'Не вдалося скопіювати';
      setTimeout(function () { b.textContent = old; }, 1800);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(function () { done(true); }, function () {
        done(fallbackCopy(code));
      });
    } else {
      done(fallbackCopy(code));
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
      Promise.resolve(Notification.requestPermission()).then(function (perm) {
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