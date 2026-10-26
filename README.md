# Мій розклад (lCloud)

Сайт показує розклад з lCloud: наступна пара з відліком, Zoom, викладач і код доступу, суботи за циклом.

## Файли
- `app.py` — весь сайт (Flask)
- `teachers.py` — викладачі та коди Zoom з PDF (**репозиторій має бути приватним!**) + список «предмет → викладач»
- `requirements.txt` — бібліотеки
- `.gitignore` — що не потрапляє в Git

## Налаштування суботи (на початку `app.py`)
- `SATURDAY_ANCHOR` + `SATURDAY_ANCHOR_WEEKDAY` — опорна субота й за який день вона (0=Пн … 4=Пт)
- `SATURDAY_BELLS` — час суботніх пар (перевір під свій коледж)
- `SATURDAY_OVERRIDES` — якщо якась субота випадає з циклу

## Новий ноутбук: що робити
```bash
# 1. Встанови Git і Python 3.9+ (на Mac: xcode-select --install), потім:
git clone https://github.com/ttepx/schedule.git
cd schedule

# 2. Віртуальне середовище й залежності
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Запуск (SECRET_KEY будь-який довгий рядок)
export SECRET_KEY="будь-який_довгий_випадковий_рядок"
python app.py        # відкрий http://localhost:5000
```
Якщо файли на новому ноутбуці беруться не з GitHub, а з цієї папки, просто скопіюй їх у папку проєкту.

## Відправка оновлень (Vercel підхопить сам)
```bash
git add .
git commit -m "Що змінив"
git push
```
Перший `git push` на новому ноутбуці попросить авторизацію: простіше всього `brew install gh` і `gh auth login`.

## Vercel
Змінна середовища **SECRET_KEY** (Settings → Environment Variables) має бути задана. Згенерувати:
`python3 -c "import secrets; print(secrets.token_hex(32))"`
