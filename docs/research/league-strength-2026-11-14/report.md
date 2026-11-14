# Отдельные модели атаки и защиты для четырёх лиг

Исходных игр: 2434; прогнозов v1: 2202; проверено новых прогнозов: 518 из 518. Исключено: 0.

Один фиксированный рецепт: 75% обученного Пуассона + 25% Elo. Настройки взяты из прежнего `selection.json`; параметры команд и средние обучены отдельно для каждой лиги. Перебора настроек и выбора лучшей лиги по результатам нет.

Обучение обновляется последовательно перед каждым прогнозом. Используются только более ранние результаты; из-за датового формата модели исключён также весь пограничный день четырёхчасового барьера. Модель v1 сохраняет исходный, менее консервативный барьер.

| Период | Матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |
| --- | ---: | --- | ---: | ---: | ---: |
| validation | 223 | v1 | 0.645508 | 1.067311 | 42.6% |
| validation | 223 | base | 0.670519 | 1.103645 | 39.9% |
| validation | 223 | candidate | 0.623137 | 1.034787 | 43.5% |
| audit | 295 | v1 | 0.595871 | 0.998037 | 52.9% |
| audit | 295 | base | 0.637801 | 1.057547 | 48.1% |
| audit | 295 | candidate | 0.591177 | 0.992401 | 52.9% |

Разбивка позднего периода по лигам:

| Лига | Матчей | Brier v1 | Brier кандидат | Log Loss v1 | Log Loss кандидат |
| --- | ---: | ---: | ---: | ---: | ---: |
| BSA | 73 | 0.557940 | 0.533044 | 0.943104 | 0.909033 |
| ELC | 97 | 0.622654 | 0.643032 | 1.036492 | 1.064302 |
| PD | 65 | 0.557194 | 0.544417 | 0.944633 | 0.926558 |
| PL | 60 | 0.640621 | 0.628727 | 1.060557 | 1.048924 |

Парные отличия кандидата от v1 в позднем периоде (95% интервалы по UTC дням):

- brier: -0.004694, интервал [-0.022703; +0.011276].
- logloss: -0.005635, интервал [-0.031934; +0.019191].

Предварительный критерий: False. Критерий исторического преимущества: False.

Выполнено 167 обучений для уникальных дат среза; обучение и прогнозы заняли 0.51 с на этой машине. Матчи с одной датой среза используют одну обученную модель.

Это ретроспективная разработка на уже изучавшейся истории. Основная модель и исходные 100 прогнозов не изменены; будущая точность пока не доказана. Критерий требует улучшения обеих общих метрик в обоих периодах, отсутствия ухудшения по каждой лиге позднего периода и верхних границ обоих общих интервалов ниже нуля.

Ограничения:

- These historical periods have already been inspected. This is retrospective development, not an untouched test or evidence of future accuracy.
- The recipe was selected earlier using EPL data; transfer to the other leagues is an experiment, not a new parameter search.
- Production v1 retains its exact 24-hour replay and four-hour result fence. The date-based candidate conservatively excludes the entire boundary date, potentially losing up to 24 additional hours of otherwise available results.
- Only the two frozen seasons per league are available; promoted clubs can have little history. Coverage and all exclusions must be reported.
- Historical final scores and kickoff times are observed now; contemporaneous result-publication and rescheduling logs are unavailable.
- Player availability, confirmed lineups, weather, xG and betting odds are not used.
- The original one-hundred-match packet, probabilities and source inputs remain immutable. Any later candidate series must have its own actual registration timestamp and exclude fixtures less than 24 hours away.

Воспроизведение без сетевых запросов:

```bash
node scripts/replay_historical_v1.mjs /tmp/footpredict-league-strength
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.league_strength \
  --replay /tmp/footpredict-league-strength/replay.json \
  --output /tmp/footpredict-league-strength/report
```
