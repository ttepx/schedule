import base64
import datetime
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, make_response, redirect, render_template_string, request

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
    except Exception:
        # Свідомо без тексту помилки: він міг би містити URL із паролем.
        return False, "Не вдалося зв'язатися з lCloud. Спробуйте пізніше.", None

    ok = data.get("st") in (True, 1, "1", "true")
    msg = BeautifulSoup(str(data.get("text", "")), "html.parser").get_text(" ", strip=True)
    if not ok:
        return False, msg or "Невірний логін або пароль.", None
    return True, "", s.cookies.get_dict()


def safe_link(url):
    if not url:
        return None
    return url if urlparse(url).scheme in ("http", "https") else None


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
        raise SessionExpired()
    if not isinstance(data, dict) or "text" not in data:
        raise SessionExpired()
    return parse_cards(data["text"])


_CACHE = {}


def get_schedule(cookies, monday, extra_day=None):
    """Повертає {date: [картки]} для тижня Пн–Пт (+ додатковий день)."""
    days = [monday + datetime.timedelta(days=i) for i in range(5)]
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


# Просте обмеження спроб входу (в межах одного інстанса сервера)
_ATTEMPTS = {}


def too_many_attempts():
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "?")).split(",")[0].strip()
    now = time.time()
    hits = [t for t in _ATTEMPTS.get(ip, []) if now - t < 600]
    hits.append(now)
    _ATTEMPTS[ip] = hits
    if len(_ATTEMPTS) > 1000:
        _ATTEMPTS.clear()
    return len(hits) > 10


@app.after_request
def security_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ───────────────────────── Допоміжні ─────────────────────────
def step_weekday(d, direction):
    d += datetime.timedelta(days=direction)
    while d.weekday() > 4:
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


def find_next_lesson(by_date, now):
    for d in sorted(by_date):
        for c in by_date[d]:
            end = datetime.datetime.combine(
                d, datetime.datetime.strptime(c["end"], "%H:%M").time(), tzinfo=TZ)
            if end > now:
                start = datetime.datetime.combine(
                    d, datetime.datetime.strptime(c["start"], "%H:%M").time(), tzinfo=TZ)
                diff = (d - now.date()).days
                if diff == 0:
                    when = "Сьогодні"
                elif diff == 1:
                    when = "Завтра"
                else:
                    when = f"{DAY_NAMES[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"
                return {**c, "start_iso": start.isoformat(), "end_iso": end.isoformat(),
                        "when": when}
    return None


# ───────────────────────── Сторінки ─────────────────────────
@app.route("/login", methods=["POST"])
def login():
    if too_many_attempts():
        return render_template_string(LOGIN_TEMPLATE, error="Забагато спроб. Спробуйте за 10 хвилин."), 429
    login_value = (request.form.get("login") or "").strip()
    password = request.form.get("password") or ""
    if not login_value or not password:
        return render_template_string(LOGIN_TEMPLATE, error="Введіть логін і пароль."), 400
    ok, msg, cookies = lcloud_login(login_value, password)
    password = None  # пароль більше не потрібен
    if not ok:
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
        selected = today if today.weekday() <= 4 else step_weekday(today, 1)
    monday = selected - datetime.timedelta(days=selected.weekday())

    try:
        by_date = get_schedule(cookies, monday, extra_day=selected)
    except SessionExpired:
        resp = make_response(render_template_string(
            LOGIN_TEMPLATE, error="Сесія завершилась. Увійдіть ще раз."))
        clear_session(resp)
        return resp
    except Exception:
        return render_template_string(
            ERROR_TEMPLATE, message="Не вдалося отримати розклад. Спробуйте оновити сторінку."), 502

    week_days = [{
        "iso": (monday + datetime.timedelta(days=i)).isoformat(),
        "label": DAY_SHORT[i],
        "num": (monday + datetime.timedelta(days=i)).day,
        "count": pairs_word(len(by_date.get(monday + datetime.timedelta(days=i), []))),
        "selected": (monday + datetime.timedelta(days=i)) == selected,
        "today": (monday + datetime.timedelta(days=i)) == today,
    } for i in range(5)]

    next_lesson = None
    if monday <= today <= monday + datetime.timedelta(days=4):
        next_lesson = find_next_lesson(by_date, now)

    lessons = []
    for c in by_date.get(selected, []):
        state = "upcoming"
        if selected == today:
            st = datetime.datetime.combine(selected, datetime.datetime.strptime(c["start"], "%H:%M").time(), tzinfo=TZ)
            en = datetime.datetime.combine(selected, datetime.datetime.strptime(c["end"], "%H:%M").time(), tzinfo=TZ)
            state = "past" if en <= now else ("current" if st <= now else "upcoming")
        elif selected < today:
            state = "past"
        lessons.append({**c, "state": state})

    year_part = f" {selected.year}" if selected.year != today.year else ""
    return render_template_string(
        MAIN_TEMPLATE,
        lessons=lessons,
        week_days=week_days,
        day_title=f"{DAY_NAMES[selected.weekday()]}, {selected.day} {MONTHS[selected.month - 1]}{year_part}",
        is_today=selected == today,
        prev_date=step_weekday(selected, -1).isoformat(),
        next_date=step_weekday(selected, 1).isoformat(),
        today_iso=today.isoformat(),
        next_lesson=next_lesson,
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
.week { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin-bottom: 22px; }
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
@media (min-width: 560px) {
  .lesson { grid-template-columns: 58px 1fr auto; align-items: center; }
  .zoom, .nolink { grid-column: auto; }
  .zoom { padding: 11px 18px; white-space: nowrap; }
}
.empty { text-align: center; padding: 36px 20px; border-radius: 20px; background: var(--card); border: 1px solid var(--card-line); color: var(--muted); }
.empty .big { font-size: 34px; margin-bottom: 6px; }
.empty a { color: var(--blue); }

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

  {% if next_lesson %}
  <section class="hero" id="nextCard" aria-label="Наступна пара"
           data-start="{{ next_lesson.start_iso }}" data-end="{{ next_lesson.end_iso }}"
           data-title="{{ next_lesson.lesson }}" data-zoom="{{ next_lesson.zoom_link or '' }}">
    <span class="tag">Наступна пара</span>
    <div class="countdown" id="countdown">00:00:00</div>
    <div class="caption" id="nextLabel">до початку пари</div>
    <h2>{{ next_lesson.lesson }}</h2>
    <div class="chips">
      <span class="chip">🗓 {{ next_lesson.when }}</span>
      <span class="chip">⏰ {{ next_lesson.start }} – {{ next_lesson.end }}</span>
      <span class="chip">{{ next_lesson.pair }} пара</span>
      {% if next_lesson.room %}<span class="chip">🏫 ауд. {{ next_lesson.room }}</span>{% endif %}
    </div>
    <div class="hero-actions">
      {% if next_lesson.zoom_link %}
        <a class="btn-white" href="{{ next_lesson.zoom_link }}" target="_blank" rel="noopener noreferrer">🎥 Підключитися до Zoom</a>
      {% endif %}
      <button type="button" class="btn-ghost" id="notifyBtn">🔔 Нагадати за 5 хв</button>
    </div>
    {% if next_lesson.zoom_link %}
    <label class="switch">
      <input type="checkbox" id="autoJoin">
      <div><b>Автопідключення</b><span>Zoom відкриється сам за хвилину до початку. Вкладка з сайтом має бути відкрита.</span></div>
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
    <h2>{{ day_title }}{% if is_today %}<span class="badge-today">Сьогодні</span>{% endif %}</h2>
    <div class="steps">
      {% if not is_today %}<a class="jump" href="/?date={{ today_iso }}">Сьогодні</a>{% endif %}
      <a class="step" href="/?date={{ prev_date }}" aria-label="Попередній день">‹</a>
      <a class="step" href="/?date={{ next_date }}" aria-label="Наступний день">›</a>
    </div>
  </div>

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
        {% if item.group %}<div class="group">👥 {{ item.group }}</div>{% endif %}
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
(function () {
  var card = document.getElementById('nextCard');
  if (!card) return;
  var start = new Date(card.dataset.start), end = new Date(card.dataset.end);
  var cd = document.getElementById('countdown'), label = document.getElementById('nextLabel');
  var zoom = card.dataset.zoom;
  var auto = document.getElementById('autoJoin');
  var doneKey = 'autojoin_done_' + card.dataset.start;
  var openedBeforeStart = new Date() < start;
  function store(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
  }
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmt(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60); s -= m * 60;
    return (d ? d + ' д ' : '') + pad(h) + ':' + pad(m) + ':' + pad(s);
  }
  if (auto) {
    auto.checked = store('autojoin') === '1';
    auto.addEventListener('change', function () { store('autojoin', auto.checked ? '1' : '0'); });
  }
  function tick() {
    var now = new Date();
    if (auto && auto.checked && zoom && openedBeforeStart && !store(doneKey) &&
        now >= new Date(start.getTime() - 60 * 1000) && now < end) {
      store(doneKey, '1');
      window.location.href = zoom;
      return;
    }
    if (now >= end) { label.textContent = 'пара завершилась, оновіть сторінку'; cd.textContent = '00:00:00'; return; }
    if (now >= start) { label.textContent = 'до кінця пари'; cd.textContent = fmt(end - now); return; }
    label.textContent = 'до початку пари';
    cd.textContent = fmt(start - now);
  }
  tick(); setInterval(tick, 1000);

  var btn = document.getElementById('notifyBtn');
  btn.addEventListener('click', function () {
    if (!('Notification' in window)) { btn.textContent = 'Браузер не підтримує сповіщень'; return; }
    Notification.requestPermission().then(function (perm) {
      if (perm !== 'granted') { btn.textContent = 'Сповіщення заборонені'; return; }
      var wait = start - new Date() - 5 * 60 * 1000;
      if (wait <= 0) { btn.textContent = 'Пара вже скоро почнеться'; return; }
      setTimeout(function () { new Notification('Пара через 5 хвилин', { body: card.dataset.title }); }, wait);
      btn.textContent = '✅ Нагадаю (не закривайте вкладку)';
      btn.disabled = true;
    });
  });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)