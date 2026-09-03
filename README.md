# Клиент БКС Торгового API: портфель и история сделок

Программа на Python для [БКС Торгового API](https://trade-api.bcs.ru): подключается к
`https://be.broker.ru`, авторизуется по refresh-токену и читает

* **текущий портфель** — позиции, денежные остатки, P/L, доли, АКД (сервис «Портфель» и, отдельно, «Лимиты»);
* **историю сделок** — с фильтрами по датам/тикерам/стороне и автоматическим обходом страниц;
* заявки и неторговые операции (купоны, дивиденды, комиссии) — бонусом;
* умеет сохранять всё это в JSON / CSV / Markdown.

Внутри: только `requests` + стандартная библиотека, кэш токенов с правами `0600`,
соблюдение лимита 10 RPS, авто-повторы на `429`/`5xx` и разбор фирменного формата ошибок БКС.

---

## 1. Что нужно получить заранее

1. Брокерский счёт в БКС (без него API не работает).
2. **Refresh-токен**: в [веб-версии «БКС Мир инвестиций»](https://lk.bcs.ru/) →
   **Профиль** → **Счета и тарифы** → клик по нужному счёту → **Токены API** → **Выпустить токен**.
   Токен показывают **один раз** — сохраните его сразу.

| Факт из документации | Что это значит для программы |
| --- | --- |
| access-токен живёт **24 часа** | программа перевыпускает его сама, пока жив refresh-токен |
| refresh-токен живёт **90 суток** | раз в ~3 месяца нужно выпустить новый токен |
| один токен = **один брокерский счёт** | несколько счетов → несколько токенов и несколько файлов кэша |
| права: «только чтение» / «торговля + чтение» | `client_id` должен совпадать: `trade-api-read` / `trade-api-write` |
| **сделки и заявки отдаются только с 26.01.2026** | более ранняя история через этот API недоступна |
| лимит HTTP — **10 RPS** (неторговые операции — **3 RPS**) | встроен токен-бакет, превышать лимит не даст |

## 2. Быстрый старт

```bash
cd bcs-api-portfolio
pip install -r requirements.txt          # нужен только requests

export BCS_REFRESH_TOKEN='ВАШ_ТОКЕН'      # не пишите токен в историю команд: лучше файлом (шаг 3)
python3 bcs_portfolio.py report           # портфель + лимиты + сделки за 30 дней
```

Ожидаемый вид (данные демонстрационные — `python3 -m bcs_api demo` рисует ровно это без сети):

```text
Портфель (term=T0): 8 поз. · данные на 2026-09-03 10:03

Показатель               Значение
-------------------  ------------
Стоимость бумаг      902 687.89 ₽
Денежный остаток      74 435.01 ₽
Итого портфель       977 122.90 ₽
Нереализованный P/L  +19 849.30 ₽
За день               -9 581.89 ₽

Денежные остатки
Валюта      Всего    Занято   Свободно          В ₽
------  ---------  --------  ---------  -----------
RUB     62 113.05  1 863.39  60 249.66  62 113.05 ₽
CNY        993.71     29.81      963.9   12 321.96 ₽

Позиции
Тикер    Название         Кол-во  Цена поз.  Цена тек.   Стоимость ₽           P/L    P/L %  За день %    Доля  Тип
-------  ---------------  ------  ---------  ---------  ------------  ------------  -------  ---------  ------  ---------
SBER     Сбербанк            120     322.24     287.06   34 447.26 ₽   -4 221.77 ₽  -10.92%     +0.25%   3.82%  Акция РФ
LKOH     Лукойл              120   6 841.52   7 028.40  843 407.47 ₽  +22 424.70 ₽   +2.73%     -0.03%  93.43%  Акция РФ
```

## 3. Как сообщить программе токен

Приоритет: ключи командной строки → переменные окружения → `bcs-config.json`.

```bash
# вариант 1: переменная окружения
export BCS_REFRESH_TOKEN='...'

# вариант 2: файл (безопаснее — токен не попадёт в ps/history)
cp bcs-config.example.json bcs-config.json && chmod 600 bcs-config.json   # вписать токен

# вариант 3: спросить в интерактиве (токен вводится скрыто, без `echo`)
python3 -m bcs_api portfolio
```

Переменные окружения: `BCS_REFRESH_TOKEN`, `BCS_CLIENT_ID`, `BCS_TOKEN_CACHE`,
`BCS_CONFIG`, `BCS_API_BASE_URL`, `BCS_TIMEOUT`, `BCS_MAX_RETRIES`, `BCS_RPS`.

Файл `.bcs-tokens.json` (путь настраивается) — кэш access-токена **и refresh-токена**.
Права `0600`, запись атомарная. Нужен потому, что Keycloak может повернуть refresh-токен
при обмене: потеряешь новый — старый становится невалидным.

**Где программа ищет конфиг** (первое найденное выигрывает): `$BCS_CONFIG` →
`./bcs-config.json` → `./.bcs-config.json` → `~/.config/bcs/config.json` → `~/bcs-config.json`.
Поиск идёт от **текущей папки**, а не от папки скрипта: если запускать из другой директории,
положите конфиг домой или укажите `BCS_CONFIG=/путь/к/bcs-config.json`. Имя файла строгое —
`bsc-config.json` (опечатка) прочитан не будет.

**Значение токена чистится автоматически**: пробелы, переносы строки, кавычки, `Bearer ` в начале,
неразрывные пробелы и «…» из примеров документации удаляются, а сам факт чистки печатается —
это спасает от «токен ведь правильный, а брокер его не принимает».

### Не открывается: `invalid_grant / Invalid refresh token`

Ключ к разгадке — **откуда именно** программа взяла значение. Приоритет такой, и он может быть
неожиданным: `--refresh-token` → `BCS_REFRESH_TOKEN` → файл → кэш. Отдельная ловушка: `export
BCS_REFRESH_TOKEN=...`, сделанный один раз в сессии терминала, **перебивает** то, что вы вписали
в `bcs-config.json`.

Одна команда отвечает на все вопросы (секреты не печатает — только длину, маску и claims из JWT):

```bash
python3 -m bcs_api token --check
```

Она показывает: источник каждого параметра, какие JSON-файлы лежат рядом и какой из них реально
читается (опечатка в имени видна сразу), есть ли конфликт с кэшем, похожа ли строка на токен,
когда у него срок действия (`exp`) и какой `client_id` (`azp`) в него зашит.

| Причина | Как увидеть | Что делать |
| --- | --- | --- |
| Осталась `export BCS_REFRESH_TOKEN=...` из прошлой сессии, и она перебивает файл | в `token --check` источник = `переменная окружения BCS_REFRESH_TOKEN` + предупреждение про переопределение | `unset BCS_REFRESH_TOKEN` |
| Файл называется не `bcs-config.json` (например, `bsc-config.json`) | `token --check` → «НЕ читается: имя не то» | переименовать файл или задать `BCS_CONFIG` |
| Программа запущена не из папки с конфигом | в отчёте нет ни одного прочитанного файла | `BCS_CONFIG=/путь/bcs-config.json` либо конфиг в `~/.config/bcs/config.json` |
| В строку попали пробел/перенос/кавычки | `Что вычищено из значения` / проблема про недопустимые символы | перескопировать значение (программа уже чистит его сама) |
| Скопирован не весь токен (обрезка, `…` из примера) | «длина N символов — похоже, скопирована не вся строка» | скопировать из ЛК целиком; токен показывают один раз |
| Не тот `client_id` относительно прав токена | проблема вида «в токене зашит client_id='trade-api-read', а запрос идёт с 'trade-api-write'» | в `bcs-config.json` поставить `client_id` из токена |
| В кэше осел чужой/повёрнутый refresh-токен | в `token --check` видны два кандидата: «кэш» и «конфиг/env» | `python3 -m bcs_api token --reset` (программа и сама пробует кандидатов по очереди) |
| Токен отозвали в ЛК или прошло 90 суток | «срок действия токена истёк N дн. назад» либо «локальных признаков не найдено ⇒ вероятно, отозван» | выпустить новый токен |
| Токен от другого счёта | локально не видно (у токена нет номера счёта в открытом виде) | выпустить токен на нужном счёте: Профиль → Счета и тарифы |
| Сбиты часы в системе | «токен выдан «из будущего»» | синхронизировать время |

Полезно знать: `POST /token` БКС отвечает `invalid_grant` и на **пустой/мусорный** refresh-токен,
и на **просроченный**, и на **чужой** — сам сервер не различает эти случаи, поэтому вся точность
локальная, из `token --check`.

## 4. Команды CLI

```bash
python3 -m bcs_api КОМАНДА [опции]        # или python3 bcs_portfolio.py КОМАНДА [опции]
```

| Команда | Назначение |
| --- | --- |
| `token` | проверить конфиг и TTL токена; `--check` — диагностика «откуда токен и что с ним не так», `--force` — перевыпустить, `--reset` — очистить кэш |
| `portfolio` | позиции + деньги + P/L + разбивка по классам активов |
| `limits` | данные сервиса «Лимиты» — деньги, бумаги, ГО и вариационная маржа |
| `trades` | история биржевых сделок |
| `orders` | история заявок (с 26.01.2026) |
| `operations` | купоны, дивиденды, комиссии, пополнения/выводы |
| `report` | портфель + лимиты + сделки одним прогоном (значение по умолчанию для `bcs_portfolio.py`) |
| `export` | то же в файлы JSON/CSV/Markdown |
| `watch` | обновлять портфель каждые N секунд |
| `demo` | тот же вывод на синтетических данных, без сети и токена |

Полезные опции:

```bash
-f table|json|csv|md      # формат вывода (--raw — сырой JSON ответа API)
--days 30                 # последние N дней
--since 2026-01-26        # или конкретная дата/время (принимает и 26.01.2026 10:00)
--until 2026-06-01
--ticker SBER --ticker LKOH
--class-code TQBR
--side buy|sell
--all-pages --size 100 --limit 500 --sort tradeDateTime,desc
--term T0|T1|all          # режим расчётов у портфеля (см. «нюансы» ниже)
--max-retries 4 --rps 10 --timeout 30 -v|-vv
```

Примеры:

```bash
# 30 дней сделок по двум бумагам, в CSV (можно перенаправить в файл)
python3 -m bcs_api trades --days 30 --ticker SBER --ticker LKOH -f csv > trades.csv

# только продажи за квартал
python3 -m bcs_api trades --since 2026-04-01 --until 2026-07-01 --side sell

# купоны и дивиденды
python3 -m bcs_api operations --type Dividend --type BondPayingOff --days 120

# выгрузка отчёта целиком
python3 -m bcs_api export --days 90 --out reports --include-orders --include-operations

# мониторить портфель раз в минуту
python3 -m bcs_api watch --interval 60
```

## 5. Использование как библиотеки

```python
from datetime import datetime, timedelta, timezone

from bcs_api import BcsClient

client = BcsClient()                      # токен из BCS_REFRESH_TOKEN / bcs-config.json

# --- портфель
portfolio = client.get_portfolio()        # term="T0" по умолчанию — без дублей по режимам расчётов
print(f"Портфель: {portfolio.total_value_rub:,.2f} ₽, позиций: {len(portfolio.positions)}")
print(f"Нереализованный P/L: {portfolio.total_unrealized_pl:+,.2f} ₽ за день: {portfolio.total_daily_pl:+,.2f} ₽")
for p in portfolio.top_positions(5):
    print(f"{p.ticker:8} {p.quantity:>8.0f} шт  {p.current_value_rub:>14,.2f} ₽  {p.unrealized_percent_pl:+6.2f}%")
print("по классам:", portfolio.by_type())

# --- история сделок: обход страниц сам, фильтры — именованными аргументами
since = datetime.now(timezone.utc) - timedelta(days=90)
trades = list(client.iter_trades(since=since, size=100, sort=["tradeDateTime,desc"]))
print(f"сделок: {len(trades)}, оборот: {sum(t.volume or 0 for t in trades):,.2f}")
for t in trades[:10]:
    print(t.date_time, t.ticker, t.side_label, t.trade_quantity, "@", t.price, t.settlement_currency)

# --- одна страница (если нужен контроль пагинации) и «сырой» JSON
page = client.search_trades(size=100, page=0, tickers=["SBER"])
print(page.total_records, page.total_pages, len(page.records))
raw = client.get_portfolio_raw()

# --- лимиты, заявки, неторговые операции
limits = client.get_limits()
orders = list(client.iter_orders(since=since))
ops = client.search_operations(operation_types=["Dividend", "Commission"], since=since)
```

## 6. Как это устроено

```text
bcs_api/
  __main__.py     запуск `python -m bcs_api`
  cli.py          argparse, команды, человекочитаемые ошибки и коды возврата
  client.py      BcsClient: эндпоинты, модели Portfolio/Position/Trade/Order, пагинация, конфиг
  http_client.py BcsHttp: обмен токенов, токен-бакет (10/3 RPS), ретраи 429/5xx, разбор ошибок
  tokens.py      TokenSet/TokenStore: кэш токенов, rotation refresh-токена, файл 0600
  diagnostics.py осмотр refresh-токена и поиска конфига: нормализация значения, срок/azp из JWT,
                 найденные файлы (включая опечатки в имени) — без вывода секрета
  formatting.py  таблицы (с шириной кириллицы), money/qty/percent, цвет P/L
  export.py      JSON/CSV/Markdown-отчёты (CSV с BOM — открывается в Excel)
  errors.py      ApiError + типизированные подклассы (401, 429, 400…)
  demo.py        синтетические данные для --demo и тестов
tests/
  mock_server.py локальный мок BCS API (тот же контракт, включая ошибки) — можно запускать самому
  test_client.py 66 тестов: юниты + интеграция через реальный HTTP против мока
```

Запросы, которые реально уходят в БКС:

| Метод | URL |
| --- | --- |
| `POST` | `https://be.broker.ru/trade-api-keycloak/realms/tradeapi/protocol/openid-connect/token`<br>`grant_type=refresh_token&client_id=trade-api-read&refresh_token=…` |
| `GET` | `https://be.broker.ru/trade-api-bff-portfolio/api/v1/portfolio` |
| `GET` | `https://be.broker.ru/trade-api-bff-limit/api/v1/limits` |
| `POST` | `https://be.broker.ru/trade-api-bff-trade-details/api/v1/trades/search?page=0&size=100`<br>body: `{startDateTime,endDateTime,tickers[],classCodes[],side,tradeNums[]}` |
| `POST` | `https://be.broker.ru/trade-api-bff-order-details/api/v1/orders/search?page=0&size=100` |
| `POST` | `https://be.broker.ru/trade-api-bff-nontrade-operations/api/v1/operations/search?page=0&size=100` |

Во всех, кроме авторизации: `Authorization: Bearer <access_token>`.

## 7. Нюансы, о которых стоит знать

* **`term` и дубли позиций.** «Портфель» отдаёт одну бумагу отдельными строками под каждый режим
  расчётов (`T0`, `T1`, `T2`, `T365`). По умолчанию выбран `T0`, иначе итоги завышаются.
  `--term all` показывает все строки как есть.
* **История сделок ограничена 26.01.2026** — это ограничение API, а не программы. Более ранние
  сделки берите из отчётов личного кабинета.
* **Нет истории денежных операций в полном виде** — `operations` покрывает купоны/дивиденды/
  комиссии/пополнения, но налоговые и XIRR-отчёты по этому API не собрать.
* **Блокированные активы** отображаются в портфеле с `isBlocked=true`; торговать ими через API
  может быть нельзя (покупка — только для квалифицированных инвесторов), продажа — обычно доступна.
* **Данные в «Лимитах» — снимок на начало дня/клиринг**, в «Портфеле» — ближе к текущему моменту;
  поэтому цифры могут немного различаться. Это нормально, сравнение двух сервисов как раз в `report`.
* **Песочницы нет**: все команды реально ходят на боевой контур. Программа только читает
  (`trade-api-read`), торговых заявок она не выставляет.

## 8. Безопасность

* Токен не должен попасть в git/логи: конфиг и кэш по умолчанию в `.gitignore`, в выводе
  `token` секреты показаны как `vali…oken`.
* `-vv` логирует фактические HTTP-запросы — при выводе в тикет/чат убедитесь, что заголовка
  `Authorization` там нет (клиент его в логи не пишет).
* Refresh-токен привязан к счёту и даёт доступ к финансам: храните его как пароль, отзыв —
  через «Токены API» в личном кабинете (access-токен «только чтение» после отзыва доживает до 24 ч).
* Файлы отчётов (`reports/`) могут содержать персональные финансовые данные — не выкладывайте их.

## 9. Коды возврата CLI

| Код | Смысл |
| --- | --- |
| `0` | успех |
| `1` | прочая ошибка (в т.ч. 429 после исчерпания повторов) |
| `2` | проблема авторизации: нет/отозван/истёк refresh-токен, не тот `client_id` |
| `130` | прервано Ctrl+C |

## 10. Проверка без реального счёта

```bash
pip install -r requirements-dev.txt   # pytest + ruff (для проверки, не для работы программы)
python3 -m pytest tests -q        # 66 тестов, все проходят офлайн
python3 -m ruff check bcs_api tests   # All checks passed!
python3 -m bcs_api demo           # как выглядит вывод

# «живой» прогон против мока с реальными HTTP-запросами
python3 tests/mock_server.py --port 8765 &
BCS_API_BASE_URL=http://127.0.0.1:8765 BCS_REFRESH_TOKEN=valid-refresh-token \
  python3 -m bcs_api report --days 30
```

## 11. Источники

* Инструкция и быстрый старт, авторизация, права токенов: <https://trade-api.bcs.ru/>
* Разделы HTTP API: [`/http/authorization`](https://trade-api.bcs.ru/http/authorization),
  [`/http/portfolio`](https://trade-api.bcs.ru/http/portfolio),
  [`/http/limits`](https://trade-api.bcs.ru/http/limits),
  [`/http/trades/get-trades`](https://trade-api.bcs.ru/http/trades/get-trades),
  [`/http/operations/all-order-list`](https://trade-api.bcs.ru/http/operations/all-order-list),
  [`/http/get-operation-history`](https://trade-api.bcs.ru/http/get-operation-history)
* Лимиты запросов: <https://trade-api.bcs.ru/restrictions>
* WebSocket-каналы (портфель/лимиты/рынок в реальном времени) описаны в `/websocket/*` —
  в эту программу не включены: для разовой выгрузки портфеля и сделок достаточно HTTP.
