# Chat Context And Project Decisions

Этот файл описывает, о чем шла работа в исходном чате и какие решения уже приняты. Он дополняет `automation/CODEX_CONTEXT.md`, который больше похож на техническую инструкцию.

## Общая цель

Пользователь хочет автоматизировать текущий research pipeline для поиска opportunities на CS skins между Steam и другой площадкой.

Идея не в HFT и не в auto-trading, а в scheduled monitoring:

```text
ночью:
  risk/liquidity rebuild
  hard filters
  high-CV filter
  base snapshot

днем:
  каждые 10-30 минут брать следующий batch items
  собирать Steam listings
  считать model fair value / edge / opportunity
  отправлять хорошие сигналы в Telegram
```

Главная пользовательская цель: получать Telegram alerts даже когда локальный компьютер выключен.

## Исходный research flow

В репозитории уже были research/notebook части:

- risk/liquidity metrics по Steam;
- CSV `risk_metrics.csv`;
- фильтры в risk notebook;
- high-CV logic из skin screener analysis;
- Steam listings сборщик;
- base price snapshot;
- JSON-модели float curves;
- notebook `listings_analysis`, который считает predictions/spreads/opportunities.

Мы решили не переписывать весь research repo, а вытащить минимальный automation layer вокруг уже существующих рабочих частей.

## Почему появилась папка automation

Пользователь сначала спрашивал, нужно ли делать новое чистое repo или папку внутри текущего.

Решение:

1. Сначала сделать `automation/` внутри текущего repo, чтобы не ломать research.
2. Все runtime outputs складывать в `automation_runtime/`.
3. Позже вынести `automation/` и `automation_runtime/` в отдельный новый repo, который можно спокойно засорять runtime commits.

Почему отдельное repo потом лучше:

- GitHub Actions будет обновлять `state.json` и latest CSV;
- это может создавать много автоматических commits;
- research repo не должен превращаться в operational log.

## Nightly layer decisions

Ночной уровень должен делать:

```text
risk CSV -> monitor list -> base snapshot
```

Heavy risk rebuild пока отключен по умолчанию, потому что:

- он может идти 2-3 часа;
- зависит от Steam;
- может требовать cookies;
- рискованно случайно запускать его при каждом тесте.

Поэтому в `automation/configs/nightly.json`:

```json
"risk_rebuild": {
  "enabled": false
}
```

Если нужно, можно форсировать:

```powershell
python automation\nightly\run_level1.py --run-risk
```

Ночной runner:

```text
automation/nightly/run_level1.py
```

Отдельные шаги:

```text
automation/nightly/build_monitor_list.py
automation/nightly/build_base_snapshot.py
```

## Monitor list decisions

Первый automation milestone был: построить monitor list из уже существующих offline данных.

Входы:

```text
skin_homog/screener_preprocess_risk/risk_metrics.csv
skin_homog/data_skins_big/_summary.csv
skin_homog/data_skins_big/base.csv
skin_homog/data_skins_big/predicted.csv
```

Risk filters взяты из текущей фактической логики:

```text
steam_daily_ret_7d >= -0.03
steam_daily_downside_14d_pct <= 0.17
steam_sales_7d_n >= 21
steam_sales_7d_tail_ratio >= 0.85
n_listings >= 20
```

High-CV filters:

```text
pred_cv > 0.075
pred_range_over_mean > 0.3
n_listings >= 3
```

Default:

```text
risk_pass AND high_cv_pass
```

Если CV отсутствует, item временно считается eligible, чтобы не потерять его до Steam screening.

Outputs:

```text
automation_runtime/monitor_list_latest.csv
automation_runtime/monitor_list_latest.py
```

CSV нужен как audit trail. `.py` нужен для совместимости со старыми скриптами, которые ждут `ITEMS = [...]`.

## Expected range

В nightly config добавлен sanity-check размера monitor list.

Идея:

```text
ожидаем примерно 100-300 items
если получили 0, 15 или 900, вероятно что-то сломалось
```

Режимы:

```json
"fail_if_outside_expected_range": false
```

Только warning.

```json
"fail_if_outside_expected_range": true
```

Остановить nightly job после monitor-list, не собирать base snapshot на подозрительном списке.

На этапе тестов пользователь поставил широкий range `0..1000`, чтобы не мешало.

## Base snapshot decisions

Пользователь предложил собирать base ночью, а не при каждом monitoring run, потому что base по item меняется не так часто, а лишние запросы только увеличивают риск rate limit.

Решение:

```text
base_snapshot_latest.csv строится ночью после monitor list
monitoring днем только читает этот snapshot
```

Добавлена 429 policy:

```text
если 429:
  wait 15 min
  retry same item
если снова 429:
  wait 16 min
потом 17, 18, ...
```

Также base snapshot пишет partial CSV после каждого item, чтобы прогресс не терялся.

## Monitoring layer decisions

Monitoring runner:

```text
automation/monitoring/run_batch.py
```

Он делает один batch и завершает процесс. Он не крутится бесконечным loop.

Почему:

- scheduler/cron должен отвечать за паузу между runs;
- один запуск = один batch = понятный state update;
- если процесс зависнет, scheduler проще контролировать.

Пауза между runs задается не Python sleep, а внешним scheduler:

```text
GitHub Actions cron
Windows Task Scheduler
VPS cron
```

В `monitoring.json` есть:

```json
"interval_minutes": 10
```

Это intended schedule metadata и будущий источник для GitHub workflow, а не таймер внутри Python.

## Steam SCM decisions

`steam_scm` в `monitoring.json` управляет Steam Community Market listings fetch:

```text
market_hash_name
  -> Steam /market/listings/730/.../render/
  -> listing_id, asset_id, ask, float, paint_seed
```

Текущие pauses были увеличены пользователем:

```json
"delay_between_skins_min_sec": 10.0,
"delay_between_skins_max_sec": 20.0,
"delay_between_render_pages_min_sec": 5.0,
"delay_between_render_pages_max_sec": 10.0
```

`max_listings_per_item = 200`, значит максимум две страницы по 100.

Smoke tests показали, что Steam listings locally работал без cookies.

## Opportunity vs Alert separation

Принято важное решение разделить:

```text
opportunity_filter = что попадает в opportunities_latest.csv
alerts = что реально отправляется в Telegram
```

Почему:

- CSV useful as broad audit/debug table;
- Telegram должен быть строже, чтобы не спамить;
- можно менять alert thresholds без изменения аналитической таблицы.

Пример будущего ужесточения:

```json
"alerts": {
  "spread_hybrid_disc_max": 0.10,
  "ask_min": 5,
  "ask_max": 200
}
```

## Telegram decisions

Telegram уже подключен через отдельный sender.

Secrets не должны храниться в repo.

Env/secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Пользователь уже имеет bot/channel от другого проекта. В чате был тест реальной отправки, она работала. Токен не должен попадать в markdown/repo.

Сообщение форматируется HTML и включает:

- item;
- Steam market link;
- ask;
- fair disc;
- spread;
- fair/ask gap;
- float;
- listing id;
- risk metrics;
- colored emoji bands.

## State decisions

State пока JSON, не SQLite.

Причина:

- текущий объем маленький;
- JSON проще debug;
- SQLite можно добавить позже, если нужна история запусков/алертов.

`state.json` хранит:

```text
batch_pointer
sent_alerts
last statuses
last listing errors
consecutive_errors
last rows counts
last alert stats
```

Cooldown/dedupe идет по listing key.

## GitHub Actions and persistence

Пользователь хочет, чтобы alerts приходили, когда компьютер выключен.

Значит нужен внешний runner:

```text
GitHub Actions
VPS
другой cloud scheduler
```

VPS стоит денег, поэтому обсуждался GitHub Actions.

Главная проблема GitHub Actions:

```text
каждый run стартует из fresh checkout
state.json и runtime CSV не сохраняются сами по себе
```

Возможные решения:

1. Commit `automation_runtime/` back to repo after each run.
2. Use artifacts/cache.
3. Use external storage.
4. Use VPS.

Было принято временное практичное решение:

```text
создать отдельное automation repo
пусть оно засоряется runtime commits
research repo оставить чистым
```

## Почему новый repo

Пользователь предложил:

```text
сделать новое repo, скопировать туда automation и automation_runtime,
и пусть оно бежит и засоряется
```

Это было признано хорошим решением.

План:

1. Создать private repo, например `cs-skins-monitoring-bot`.
2. Скопировать туда `automation/`, `automation_runtime/` и минимальные зависимости/data.
3. Добавить GitHub Secrets.
4. Добавить workflows.
5. Позволить workflows commit runtime files back.

## Что уже тестировалось

Live monitoring smoke:

```powershell
python automation\monitoring\run_batch.py --batch-size 2 --telegram-dry-run
```

Результат последнего теста:

```text
schedule: outside active window, but not enforced
batch start pointer: 8
items:
  Glock-18 | Steel Disruption (Factory New)
  M4A4 | The Emperor (Field-Tested)
Steam listings rows: 135
listing errors: 0
enriched rows: 135
opportunities: 0
telegram dry-run stats: loaded=0 filtered=0 considered=0 sent=0 skipped=0
next pointer: 10
state updated ok
```

Earlier test found two Glock Nuclear Garden opportunities and real Telegram send worked. Those alert keys are in `state.json` sent history.

## Open questions

1. How exactly to persist state in GitHub Actions?
   Current likely answer: commit `automation_runtime/` back to the separate automation repo.

2. Which minimal files are truly required in new repo?
   Need smoke after copying.

3. Are Steam cookies required for GitHub Actions?
   Listings worked locally without cookies; full risk rebuild might require cookies.

4. Should real Telegram be enabled by config or only CLI flag?
   Currently `telegram.enabled=false`; real send can be forced with `--send-telegram`.

5. Should monitoring enforce active window?
   Currently `enforce_active_window=false`.

6. Should `alerts` become stricter than `opportunity_filter`?
   Likely yes after a few live dry-runs.

## Suggested next prompt in new chat

```text
Прочитай automation/CODEX_CONTEXT.md и automation/CHAT_CONTEXT.md.
Это новый repo для CS skins monitoring bot. Помоги проверить, что все нужные файлы на месте, запусти import/smoke checks, потом подготовь GitHub Actions workflows с persistence через commits в automation_runtime.
```

