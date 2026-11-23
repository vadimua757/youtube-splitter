# YouTube Splitter

Завантажує відео або аудіодоріжку з YouTube і за потреби ріже її на фрагменти за таймкодами з назвами.

## Запуск

Локально (потрібні Python 3.11+, ffmpeg і JS-рантайм **deno** або Node.js 22+; без нього `yt-dlp` для багатьох відео відповідає «This video is not available», а старий Node 20 він відхиляє як unsupported; у Docker-образі deno вже є):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.api:create_app --factory --port 8000
```

Docker:

```bash
docker compose up --build
```

Відкрийте http://localhost:8000.

## Таймкоди

Текст у форматі опису YouTube, по одному в рядку:

```
0:00 Вступ
3:45 - Перша пісня
1:02:03 – Фінал
```

Кінець фрагмента = початок наступного; останній триває до кінця відео. Якщо таймкодів немає, можна взяти розділи (chapters) відео (`use_chapters`); якщо немає й їх — повертається цілий файл.

### Обмеження

- Рядок, що починається з часу на кшталт `10:30 pm`, вважається таймкодом (`10:30`, назва «pm»).
- Рядки, де таймкод не на початку (наприклад, `Intro 3:45`), або нумерація без крапки (`01 5:10 Track`) ігноруються.

## API

```
POST /jobs   {"url": "...", "format": "mp3|m4a|opus|mp4", "timecodes": "...", "use_chapters": false,
              "video_quality": 1080, "video_mode": "fast|exact"}   → 202 {"job_id": "..."}
GET  /jobs/{id}                 статус, прогрес, список фрагментів
GET  /jobs/{id}/files/{n}       окремий фрагмент (n з 1)
GET  /jobs/{id}/download        zip з усіма фрагментами (або сам файл, якщо він один)
DELETE /jobs/{id}               видалити завершене (done/failed) завдання і файли → 204; 404 якщо немає, 409 якщо ще виконується
```

Статуси: `queued → downloading → splitting → done | failed`. Коди помилок: `VIDEO_UNAVAILABLE`, `INVALID_TIMECODES`, `TOO_LONG`, `DOWNLOAD_FAILED`, `SPLIT_FAILED`, `INTERNAL_ERROR`.

## Налаштування (змінні оточення)

| Змінна | За замовчуванням | Значення |
|---|---|---|
| `DATA_DIR` | `data` | куди писати БД і файли |
| `API_KEY` | порожньо | якщо задано, API вимагає заголовок `X-API-Key` |
| `MAX_DURATION_MIN` | `0` | максимальна тривалість відео, 0 = без ліміту |
| `MAX_CONCURRENT_JOBS` | `2` | кількість завдань, що виконуються паралельно |
| `FILE_TTL_HOURS` | `24` | видаляти завершені завдання та файли старші за N годин; 0 = ніколи |
| `RATE_LIMIT_PER_MIN` | `0` | ліміт створення завдань з однієї IP за хвилину, 0 = вимкнено |

## Обслуговування

YouTube часто змінюється, тому `yt-dlp` потрібно оновлювати: `pip install -U yt-dlp` (для Docker — перезбирати образ).

Якщо сервіс працює за реверс-проксі, uvicorn потрібно запускати з `--proxy-headers --forwarded-allow-ips=<ip проксі>`, щоб ліміт запитів використовував справжні IP клієнтів.

Завершені завдання та файли за замовчуванням видаляються через 24 години (`FILE_TTL_HOURS`; `0` вимикає очищення).

## Правові умови

Завантаження з YouTube може суперечити його умовам використання. Публічну версію сервісу слід обмежувати контентом, на який у користувачів є права.

## Тести

```bash
pip install -r requirements-dev.txt
pytest                  # без мережі
pytest -m network       # smoke-тест із реальним YouTube
```
