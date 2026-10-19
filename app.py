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
  --paper: #f5f6f8; --surface: #ffffff; --ink: #1b2230; --muted: #667085; --line: #dde1e8;
  --accent: #2646d6; --mark: rgba(255, 205, 30, .7); --danger: #b42318;
  --sel-bg: #1b2230; --sel-fg: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --paper: #12151b; --surface: #1a1f27; --ink: #e9ecf1; --muted: #9aa3b2; --line: #2b313c;
    --accent: #3d5bf0; --mark: rgba(255, 205, 30, .45); --danger: #f97066;
    --sel-bg: #e9ecf1; --sel-fg: #12151b;
  }
}
* { box-sizing: border-box; }
html { color-scheme: light dark; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font-family: "Onest", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 16px; line-height: 1.5; -webkit-font-smoothing: antialiased;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.wrap { max-width: 640px; margin: 0 auto; padding: 20px 20px 56px; }

.topbar { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; }
.brand { font-weight: 700; font-size: 18px; letter-spacing: -.01em; }
.text-btn {
  background: none; border: 0; padding: 6px 0; font: inherit; font-size: 14px; color: var(--muted);
  cursor: pointer; text-decoration: underline; text-underline-offset: 3px;
}
.text-btn:hover { color: var(--ink); }

.next { background: var(--accent); color: #fff; border-radius: 14px; padding: 22px 22px 20px; margin-bottom: 28px; }
.next .state { font-size: 14px; opacity: .85; }
.countdown {
  font-size: clamp(40px, 12vw, 56px); font-weight: 700; line-height: 1.05; letter-spacing: -.02em;
  font-variant-numeric: tabular-nums; margin: 2px 0 14px;
}
.next h2 { margin: 0; font-size: 20px; font-weight: 600; line-height: 1.3; }
.next .when { margin: 4px 0 0; font-size: 14px; opacity: .85; }
.next-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.btn-light {
  background: #fff; color: #1b2230; border: 0; border-radius: 8px; padding: 10px 16px;
  font: inherit; font-size: 15px; font-weight: 600; text-decoration: none; cursor: pointer;
}
.btn-line {
  background: transparent; color: #fff; border: 1px solid rgba(255,255,255,.55); border-radius: 8px;
  padding: 10px 16px; font: inherit; font-size: 15px; cursor: pointer;
}
.btn-line:disabled { opacity: .7; cursor: default; }
.auto { display: flex; gap: 10px; align-items: flex-start; margin-top: 16px; font-size: 14px; cursor: pointer; }
.auto input { width: 18px; height: 18px; margin-top: 2px; accent-color: #fff; flex-shrink: 0; }

.week { display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px; margin-bottom: 26px; }
.day {
  display: flex; flex-direction: column; align-items: center; padding: 8px 0 10px; border-radius: 10px;
  border: 1px solid var(--line); background: var(--surface); color: var(--ink); text-decoration: none;
}
.day:hover { border-color: var(--muted); }
.day .wd { font-size: 13px; color: var(--muted); }
.day .dn { font-size: 20px; font-weight: 600; line-height: 1.3; font-variant-numeric: tabular-nums; }
.day.today .dn span { box-shadow: inset 0 -.4em 0 var(--mark); }
.day.sel { background: var(--sel-bg); border-color: var(--sel-bg); color: var(--sel-fg); }
.day.sel .wd { color: inherit; opacity: .75; }

.dayhead { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.dayhead h1 { font-size: 22px; font-weight: 700; margin: 0; letter-spacing: -.01em; }
.steps { display: flex; gap: 6px; align-items: center; }
.step {
  width: 36px; height: 36px; display: grid; place-items: center; border: 1px solid var(--line);
  border-radius: 8px; background: var(--surface); color: var(--ink); text-decoration: none; font-size: 18px;
}
.step:hover { border-color: var(--muted); }
.jump { font-size: 14px; color: var(--accent); text-decoration: none; margin-right: 6px; }
.jump:hover { text-decoration: underline; }

.list { list-style: none; margin: 12px 0 0; padding: 0; border-top: 1px solid var(--line); }
.row {
  display: grid; grid-template-columns: 60px 1fr auto; gap: 16px; align-items: start;
  padding: 16px 0; border-bottom: 1px solid var(--line); position: relative;
}
.row.past { opacity: .5; }
.row.current::before {
  content: ""; position: absolute; left: -14px; top: 14px; bottom: 14px; width: 4px;
  border-radius: 2px; background: var(--accent);
}
.t-start { font-size: 20px; font-weight: 700; line-height: 1.2; font-variant-numeric: tabular-nums; }
.t-end { font-size: 14px; color: var(--muted); font-variant-numeric: tabular-nums; }
.name { font-size: 17px; font-weight: 600; line-height: 1.3; }
.meta { font-size: 14px; color: var(--muted); margin-top: 2px; }
.nb { white-space: nowrap; }
.now { font-size: 13px; font-weight: 600; color: var(--accent); margin-top: 4px; }
.zoom {
  background: var(--accent); color: #fff; text-decoration: none; font-size: 14px; font-weight: 600;
  padding: 8px 14px; border-radius: 8px;
}
.zoom:hover { filter: brightness(1.1); }
.empty { padding: 28px 0; color: var(--muted); }
.empty a { color: var(--accent); }

.login { max-width: 400px; margin: 0 auto; padding: 56px 20px; }
.login h1 { font-size: 28px; margin: 0 0 8px; letter-spacing: -.02em; }
.lead { color: var(--muted); margin: 0 0 28px; }
.f { display: block; font-size: 14px; font-weight: 600; margin: 0 0 6px; }
.field {
  width: 100%; padding: 12px 14px; margin-bottom: 18px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--surface); color: var(--ink); font: inherit;
}
.field:focus { outline: 2px solid var(--accent); outline-offset: 1px; border-color: var(--accent); }
.submit {
  width: 100%; padding: 13px; border: 0; border-radius: 8px; background: var(--accent); color: #fff;
  font: inherit; font-weight: 600; cursor: pointer;
}
.submit:hover { filter: brightness(1.1); }
.note { font-size: 13.5px; color: var(--muted); margin-top: 22px; line-height: 1.55; }
.err { border-left: 3px solid var(--danger); padding: 8px 12px; margin-bottom: 20px; color: var(--danger); font-size: 15px; }
"""

HEAD = """<!DOCTYPE html>
<html lang="uk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#2646d6">
  <title>Розклад</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>__STYLE__</style>
</head>
""".replace("__STYLE__", STYLE)

LOGIN_TEMPLATE = HEAD + """<body>
<main class="login">
  <h1>Вхід у lCloud</h1>
  <p class="lead">Покажемо розклад вашої групи на тиждень.</p>
  {% if error %}<div class="err" role="alert">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <label class="f" for="login">Логін або телефон</label>
    <input class="field" id="login" type="text" name="login" autocomplete="username" maxlength="200" required autofocus>
    <label class="f" for="password">Пароль</label>
    <input class="field" id="password" type="password" name="password" autocomplete="current-password" maxlength="100" required>
    <button class="submit" type="submit">Увійти</button>
  </form>
  <p class="note">Сайт неофіційний і не пов'язаний з коледжем чи lCloud. Пароль потрібен один раз, щоб увійти в lCloud, і ніде не зберігається. У браузері лишається зашифрована сесія на 12 годин, а кнопка «Вийти» її видаляє.</p>
</main>
</body>
</html>
"""

ERROR_TEMPLATE = HEAD + """<body>
<main class="login">
  <h1>Не вдалося завантажити</h1>
  <div class="err" role="alert">{{ message }}</div>
  <p><a href="/" style="color: var(--accent)">Спробувати ще раз</a></p>
</main>
</body>
</html>
"""

MAIN_TEMPLATE = HEAD + """<body>
<div class="wrap">

  <header class="topbar">
    <span class="brand">Розклад</span>
    <form method="POST" action="/logout"><button class="text-btn" type="submit">Вийти</button></form>
  </header>

  {% if next_lesson %}
  <section class="next" id="nextCard" aria-label="Наступна пара"
           data-start="{{ next_lesson.start_iso }}" data-end="{{ next_lesson.end_iso }}"
           data-title="{{ next_lesson.lesson }}" data-zoom="{{ next_lesson.zoom_link or '' }}">
    <div class="state" id="nextLabel">До початку пари</div>
    <div class="countdown" id="countdown">00:00:00</div>
    <h2>{{ next_lesson.lesson }}</h2>
    <p class="when">{{ next_lesson.when }}, {{ next_lesson.pair }} пара, {{ next_lesson.start }}–{{ next_lesson.end }}{% if next_lesson.room %}, аудиторія {{ next_lesson.room }}{% endif %}</p>
    <div class="next-actions">
      {% if next_lesson.zoom_link %}
        <a class="btn-light" href="{{ next_lesson.zoom_link }}" target="_blank" rel="noopener noreferrer">Підключитися до Zoom</a>
      {% endif %}
      <button type="button" class="btn-line" id="notifyBtn">Нагадати за 5 хвилин</button>
    </div>
    {% if next_lesson.zoom_link %}
    <label class="auto"><input type="checkbox" id="autoJoin"> Відкрити Zoom автоматично за хвилину до початку (вкладка має бути відкрита)</label>
    {% endif %}
  </section>
  {% endif %}

  <nav class="week" aria-label="Дні тижня">
    {% for d in week_days %}
      <a href="/?date={{ d.iso }}" class="day{% if d.selected %} sel{% endif %}{% if d.today %} today{% endif %}"{% if d.selected %} aria-current="date"{% endif %}>
        <span class="wd">{{ d.label }}</span>
        <span class="dn"><span>{{ d.num }}</span></span>
      </a>
    {% endfor %}
  </nav>

  <div class="dayhead">
    <h1>{{ day_title }}</h1>
    <div class="steps">
      {% if not is_today %}<a class="jump" href="/?date={{ today_iso }}">Сьогодні</a>{% endif %}
      <a class="step" href="/?date={{ prev_date }}" aria-label="Попередній день">‹</a>
      <a class="step" href="/?date={{ next_date }}" aria-label="Наступний день">›</a>
    </div>
  </div>

  {% if lessons %}
  <ol class="list">
    {% for item in lessons %}
    <li class="row {{ item.state }}">
      <div>
        <div class="t-start">{{ item.start }}</div>
        <div class="t-end">{{ item.end }}</div>
      </div>
      <div>
        <div class="name">{{ item.lesson }}</div>
        <div class="meta">{{ item.pair }} пара{% if item.room %}, аудиторія {{ item.room }}{% endif %}{% if item.group %}, <span class="nb">{{ item.group }}</span>{% endif %}</div>
        {% if item.state == 'current' %}<div class="now">Іде зараз</div>{% endif %}
      </div>
      <div>
        {% if item.zoom_link %}<a class="zoom" href="{{ item.zoom_link }}" target="_blank" rel="noopener noreferrer">Zoom</a>{% endif %}
      </div>
    </li>
    {% endfor %}
  </ol>
  {% else %}
  <p class="empty">На цей день занять немає. <a href="/?date={{ next_date }}">Наступний день</a></p>
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
    if (now >= end) { label.textContent = 'Пара завершилась, оновіть сторінку'; cd.textContent = '00:00:00'; return; }
    if (now >= start) { label.textContent = 'До кінця пари'; cd.textContent = fmt(end - now); return; }
    label.textContent = 'До початку пари';
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
      btn.textContent = 'Нагадаю (не закривайте вкладку)';
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