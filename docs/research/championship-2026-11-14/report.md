# Обучение Чемпионшипа на расширенной истории

Проверено исходных игр: 6252. Выбрано сочетаний настроек: 18; выбран рецепт `glm-730-r0.03-w0.75`.

Настройки выбраны на сезонах 2023/24 и 2024/25 и сохранены до расчёта более поздних метрик. Поздние сезоны уже частично изучались раньше; это ретроспективная разработка.

| Период | Общих матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |
| --- | ---: | --- | ---: | ---: | ---: |
| audit | 533 | candidate | 0.642136 | 1.063151 | 45.0% |
| audit | 533 | transferred | 0.640859 | 1.061102 | 45.2% |
| audit | 533 | v1 | 0.645334 | 1.066056 | 42.8% |
| audit | 533 | base | 0.658634 | 1.086975 | 41.7% |
| monitoring | 159 | candidate | 0.654572 | 1.079895 | 41.5% |
| monitoring | 159 | transferred | 0.650810 | 1.074712 | 42.8% |
| monitoring | 159 | v1 | 0.632384 | 1.049836 | 48.4% |
| monitoring | 159 | base | 0.650672 | 1.074896 | 44.0% |

`transferred` — прежний рецепт АПЛ, переобученный на этой же расширенной истории Чемпионшипа. `v1` — формула базовой модели с тем же консервативным барьером дат, а не исходная серия из 100 прогнозов.

Критерий исторического преимущества: False. Основная модель автоматически не меняется.

Отличия кандидата от v1 в сезоне 2025/26, 95% интервалы по UTC дням:

- brier: -0.003197; [-0.016509; +0.010468].
- logloss: -0.002905; [-0.020961; +0.016446].

Время исследования после загрузки данных: 14.46 с. Обновление финальных весов выполняется отдельно, с уже выбранными настройками.

Ограничения:

- Retrospective development: recent ELC periods were inspected in the previous four-league experiment; no historical period is claimed to be an untouched prospective test.
- CSV dates lack reliable publication timestamps, so the immediately preceding calendar day is conservatively excluded.
- The CSV v1 reference uses the same past-only date fence as the candidate. It is a dated replay of the production formula, not the original one-hundred-match packet or exactly the timestamp-based online calculation.
- Model settings are selected on earlier ELC seasons only. Earlier evaluation results can subsequently train later rolling forecasts once they cross the information fence.
- Promoted teams may have little same-division history; primary comparisons include only fixtures eligible for v1, with exclusions and team-history coverage reported.
- Only goals and prior results are used; dated injuries, lineups and xG are not supplied by this archive.
- A historical improvement requires a separate future frozen comparison before changing the public model. Original one-hundred-match probabilities remain immutable.
