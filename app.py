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
                return {**c, "start_iso": start.isoformat(), "end_iso": end.isoformat(),
                        "date_str": f"{DAY_SHORT[d.weekday()]}, {d.strftime('%d.%m')}"}
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
        "num": (monday + datetime.timedelta(days=i)).strftime("%d.%m"),
        "selected": (monday + datetime.timedelta(days=i)) == selected,
        "today": (monday + datetime.timedelta(days=i)) == today,
    } for i in range(5)]

    next_lesson = None
    if monday <= today <= monday + datetime.timedelta(days=4):
        next_lesson = find_next_lesson(by_date, now)

    return render_template_string(
        MAIN_TEMPLATE,
        lessons=by_date.get(selected, []),
        week_days=week_days,
        day_title=f"{DAY_NAMES[selected.weekday()]}, {selected.strftime('%d.%m.%Y')}",
        is_today=selected == today,
        prev_date=step_weekday(selected, -1).isoformat(),
        next_date=step_weekday(selected, 1).isoformat(),
        today_iso=today.isoformat(),
        next_lesson=next_lesson,
    )


STYLE = """
        :root {
            --bg-gradient: radial-gradient(circle at top left, #121824, #080a0f);
            --card-bg: rgba(255, 255, 255, 0.03);
            --card-border: rgba(255, 255, 255, 0.08);
            --accent-glow: #3b82f6;
            --accent-hover: #60a5fa;
            --accent-green: #10b981;
            --zoom-color: #2d8cff;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", sans-serif;
            background: var(--bg-gradient);
            color: var(--text-main);
            margin: 0;
            padding: 24px 16px;
            min-height: 100vh;
            display: flex;
            justify-content: center;
        }

        .container {
            width: 100%;
            max-width: 720px;
        }

        .glass-card {
            background: var(--card-bg);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid var(--card-border);
            border-radius: 24px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
        }

        .header-title {
            font-size: 24px;
            font-weight: 800;
            background: linear-gradient(90deg, #60a5fa, #a7f3d0);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0 0 16px 0;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 20px;
        }

        .quick-btn {
            background: rgba(59, 130, 246, 0.12);
            color: var(--accent-hover);
            border: 1px solid rgba(59, 130, 246, 0.3);
            padding: 10px 18px;
            border-radius: 14px;
            text-decoration: none;
            font-weight: 600;
            font-size: 14px;
            transition: all 0.2s;
        }

        .quick-btn:hover, .quick-btn.active {
            background: var(--accent-glow);
            color: #fff;
            box-shadow: 0 0 15px rgba(59, 130, 246, 0.4);
        }

        .days-nav {
            display: flex;
            gap: 8px;
            overflow-x: auto;
            padding-bottom: 6px;
        }

        .day-tab {
            background: rgba(255, 255, 255, 0.04);
            color: var(--text-muted);
            padding: 10px 18px;
            border-radius: 14px;
            text-decoration: none;
            font-size: 14px;
            font-weight: 500;
            white-space: nowrap;
            border: 1px solid transparent;
            transition: all 0.2s;
        }

        .day-tab:hover {
            color: var(--text-main);
            background: rgba(255, 255, 255, 0.08);
        }

        .day-tab.selected {
            background: rgba(59, 130, 246, 0.2);
            color: #fff;
            border-color: var(--accent-glow);
            font-weight: 700;
        }

        .status-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 16px;
        }

        .lesson-card {
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid var(--card-border);
            border-radius: 18px;
            padding: 18px;
            margin-bottom: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            transition: all 0.2s;
        }

        .lesson-card:hover {
            border-color: rgba(96, 165, 250, 0.4);
            background: rgba(255, 255, 255, 0.04);
        }

        .pair-badge {
            font-weight: 800;
            color: var(--accent-hover);
            font-size: 18px;
        }

        .time {
            color: var(--text-muted);
            font-size: 13px;
            margin-top: 4px;
        }

        .lesson-name {
            font-size: 16px;
            font-weight: 700;
            margin-bottom: 4px;
        }

        .teacher-name {
            font-size: 13px;
            color: var(--text-muted);
        }

        .zoom-btn {
            background: var(--zoom-color);
            color: #fff;
            padding: 10px 16px;
            border-radius: 12px;
            text-decoration: none;
            font-weight: 700;
            font-size: 13px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            box-shadow: 0 4px 12px rgba(45, 140, 255, 0.3);
            transition: all 0.2s;
            white-space: nowrap;
        }

        .zoom-btn:hover {
            opacity: 0.9;
            transform: translateY(-2px);
            box-shadow: 0 6px 18px rgba(45, 140, 255, 0.5);
        }

        .empty {
            text-align: center;
            color: var(--text-muted);
            padding: 40px;
            font-size: 16px;
        }
    
        .title-row { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }
        .title-row .header-title { margin:0; }
        .link-btn { background:none; border:1px solid var(--card-border); color:var(--text-muted); padding:8px 14px; border-radius:12px; cursor:pointer; font-size:13px; }
        .link-btn:hover { color:var(--text-main); border-color:rgba(255,255,255,0.25); }
        .nav-row { display:flex; gap:8px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
        .arrow { padding:10px 14px; }
        .day-tab small { display:block; font-size:11px; opacity:.7; }
        .next-card { border-color: rgba(59,130,246,.45); }
        .next-label { font-size:12px; color:var(--accent-hover); font-weight:700; text-transform:uppercase; letter-spacing:.08em; margin-bottom:6px; }
        .next-title { font-size:20px; font-weight:800; margin-bottom:6px; }
        .countdown { font-size:28px; font-weight:800; margin:12px 0; font-variant-numeric: tabular-nums; }
        .next-actions { display:flex; gap:10px; flex-wrap:wrap; }
        .form-field { width:100%; box-sizing:border-box; padding:14px 16px; margin-bottom:12px; border-radius:14px; border:1px solid var(--card-border); background:rgba(255,255,255,.05); color:var(--text-main); font-size:16px; }
        .form-field:focus { outline:none; border-color:var(--accent-glow); }
        .primary-btn { width:100%; padding:14px; border-radius:14px; border:none; background:var(--accent-glow); color:#fff; font-weight:700; font-size:16px; cursor:pointer; }
        .primary-btn:hover { background:var(--accent-hover); }
        .notice { font-size:12.5px; color:var(--text-muted); line-height:1.5; margin-top:14px; }
        .error { background:rgba(239,68,68,.12); border:1px solid rgba(239,68,68,.35); color:#fca5a5; padding:12px 14px; border-radius:12px; margin-bottom:14px; font-size:14px; }
        .room { color: var(--text-muted); font-size:13px; margin-top:4px; }
"""

LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Мій Розклад</title>
    <style>__STYLE__</style>
</head>
<body>
<div class="container" style="align-self:center;">
  <div class="glass-card">
    <div class="header-title"><span>⚡ Мій розклад</span></div>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <form method="POST" action="/login" autocomplete="on">
      <input class="form-field" type="text" name="login" placeholder="Логін або телефон lCloud"
             autocomplete="username" maxlength="200" required autofocus>
      <input class="form-field" type="password" name="password" placeholder="Пароль"
             autocomplete="current-password" maxlength="100" required>
      <button class="primary-btn" type="submit">Увійти</button>
    </form>
    <p class="notice">🔒 Це неофіційний сайт, він не пов'язаний з коледжем чи lCloud. Пароль використовується
    один раз, щоб увійти в lCloud, і <b>ніде не зберігається</b>. У вашому браузері лишається лише зашифрована
    сесія на 12 годин; кнопка «Вийти» її видаляє. Користуйтесь на свій розсуд.</p>
  </div>
</div>
</body>
</html>
""".replace("__STYLE__", STYLE)
ERROR_TEMPLATE = """<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Мій Розклад</title>
    <style>__STYLE__</style>
</head>
<body>
<div class="container" style="align-self:center;">
  <div class="glass-card">
    <div class="error">{{ message }}</div>
    <a class="quick-btn" href="/">Оновити</a>
  </div>
</div>
</body>
</html>
""".replace("__STYLE__", STYLE)
MAIN_TEMPLATE = """<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Мій Розклад</title>
    <style>__STYLE__</style>
</head>
<body>
<div class="container">

  <div class="glass-card">
    <div class="title-row">
      <div class="header-title"><span>⚡ Мій розклад</span></div>
      <form method="POST" action="/logout" style="margin:0"><button class="link-btn" type="submit">Вийти</button></form>
    </div>

    <div class="nav-row">
      <a class="quick-btn arrow" href="/?date={{ prev_date }}" aria-label="Попередній день">◀</a>
      <a class="quick-btn arrow" href="/?date={{ next_date }}" aria-label="Наступний день">▶</a>
      <a class="quick-btn" href="/?date={{ today_iso }}">🔥 Сьогодні</a>
    </div>

    <div class="days-nav">
      {% for d in week_days %}
        <a href="/?date={{ d.iso }}" class="day-tab {% if d.selected %}selected{% endif %}">
          {{ d.label }}<small>{{ d.num }}{% if d.today %} · сьогодні{% endif %}</small>
        </a>
      {% endfor %}
    </div>
  </div>

  {% if next_lesson %}
  <div class="glass-card next-card" id="nextCard"
       data-start="{{ next_lesson.start_iso }}" data-end="{{ next_lesson.end_iso }}"
       data-title="{{ next_lesson.lesson }}">
    <div class="next-label" id="nextLabel">Наступна пара</div>
    <div class="next-title">{{ next_lesson.lesson }}</div>
    <div class="time">📅 {{ next_lesson.date_str }} · {{ next_lesson.pair }} пара · ⏰ {{ next_lesson.time }}</div>
    <div class="countdown" id="countdown">…</div>
    <div class="next-actions">
      {% if next_lesson.zoom_link %}
        <a href="{{ next_lesson.zoom_link }}" target="_blank" rel="noopener noreferrer" class="zoom-btn">🎥 Підключитися</a>
      {% endif %}
      <button type="button" class="quick-btn" id="notifyBtn">🔔 Нагадати за 5 хв</button>
    </div>
  </div>
  {% endif %}

  <div class="glass-card">
    <div class="status-header">
      <h2 style="margin:0; font-size:20px; font-weight:700;">{{ day_title }}
        {% if is_today %}<span style="color: var(--accent-green); font-size:13px; font-weight:600; margin-left:8px; background: rgba(16,185,129,0.15); padding:4px 10px; border-radius:8px;">Сьогодні</span>{% endif %}
      </h2>
    </div>

    {% if lessons %}
      {% for item in lessons %}
      <div class="lesson-card">
        <div>
          <div class="pair-badge">{{ item.pair }} пара</div>
          <div class="time">⏰ {{ item.time }}</div>
        </div>
        <div style="flex-grow:1;">
          <div class="lesson-name">{{ item.lesson }}</div>
          <div class="teacher-name">👥 {{ item.group }}</div>
          {% if item.room %}<div class="room">🏫 Аудиторія: {{ item.room }}</div>{% endif %}
        </div>
        <div>
          {% if item.zoom_link %}
            <a href="{{ item.zoom_link }}" target="_blank" rel="noopener noreferrer" class="zoom-btn">🎥 Zoom</a>
          {% else %}
            <span style="font-size:12px; color: var(--text-muted);">Немає посилання</span>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    {% else %}
      <div class="empty">🎉 На цей день занять немає або вихідний!</div>
    {% endif %}
  </div>
</div>

<script>
(function () {
  var card = document.getElementById('nextCard');
  if (!card) return;
  var start = new Date(card.dataset.start), end = new Date(card.dataset.end);
  var cd = document.getElementById('countdown'), label = document.getElementById('nextLabel');
  function pad(n) { return n < 10 ? '0' + n : '' + n; }
  function fmt(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60); s -= m * 60;
    return (d ? d + ' д ' : '') + pad(h) + ':' + pad(m) + ':' + pad(s);
  }
  function tick() {
    var now = new Date();
    if (now >= end) { label.textContent = 'Пара завершилась'; cd.textContent = 'Оновіть сторінку'; return; }
    if (now >= start) { label.textContent = 'Іде зараз'; cd.textContent = 'до кінця ' + fmt(end - now); return; }
    label.textContent = 'Наступна пара';
    cd.textContent = 'до початку ' + fmt(start - now);
  }
  tick(); setInterval(tick, 1000);

  var btn = document.getElementById('notifyBtn');
  btn.addEventListener('click', function () {
    if (!('Notification' in window)) { btn.textContent = 'Браузер не підтримує'; return; }
    Notification.requestPermission().then(function (perm) {
      if (perm !== 'granted') { btn.textContent = 'Сповіщення заборонені'; return; }
      var wait = start - new Date() - 5 * 60 * 1000;
      if (wait <= 0) { btn.textContent = 'Вже скоро початок'; return; }
      setTimeout(function () {
        new Notification('Пара через 5 хвилин', { body: card.dataset.title });
      }, wait);
      btn.textContent = '✅ Нагадаю (залиште вкладку відкритою)';
      btn.disabled = true;
    });
  });
})();
</script>
</body>
</html>
""".replace("__STYLE__", STYLE)


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)