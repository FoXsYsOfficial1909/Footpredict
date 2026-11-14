# Чемпионшип: прошлые удары в створ в обучении

Исходных матчей: 6252; полная статистика ударов в створ: 6250; возврат к реальному счёту из-за неполных данных: 2.

Проверен один фиксированный вариант: половина фактических голов и половина прошлых ударов в створ, переведённых в голевой масштаб по более ранним играм этой лиги. Параметры затухания, регуляризации и смесь с Elo сохранены от предыдущего исследования. Это не измеренный xG. Статистика прогнозируемого матча не используется.

| Период | Общих матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |
| --- | ---: | --- | ---: | ---: | ---: |
| validation | 1061 | shots | 0.615535 | 1.025802 | 47.8% |
| validation | 1061 | goals | 0.617774 | 1.029001 | 46.7% |
| validation | 1061 | v1 | 0.632445 | 1.048880 | 46.6% |
| validation | 1061 | base | 0.647336 | 1.070663 | 45.1% |
| audit | 533 | shots | 0.640247 | 1.060224 | 44.3% |
| audit | 533 | goals | 0.642136 | 1.063151 | 45.0% |
| audit | 533 | v1 | 0.645334 | 1.066056 | 42.8% |
| audit | 533 | base | 0.658634 | 1.086975 | 41.7% |
| monitoring | 159 | shots | 0.646101 | 1.067454 | 44.0% |
| monitoring | 159 | goals | 0.654572 | 1.079895 | 41.5% |
| monitoring | 159 | v1 | 0.632384 | 1.049836 | 48.4% |
| monitoring | 159 | base | 0.650672 | 1.074896 | 44.0% |

`shots` — новый вариант; `goals` — тот же рецепт без ударов; `v1` — датовый пересчёт базовой модели; `base` — прошлые частоты исходов лиги.

Критерий средних ошибок: False. Критерий исторического преимущества с интервалами: False.

Все периоды уже изучались. Это ретроспективная разработка; будущая точность не установлена. Основная модель, её ежедневный кандидат без ударов и исходная сотня прогнозов не заменяются.

Время расчёта после загрузки данных: 13.08 с.

Ограничения:

- All historical periods were inspected in the preceding ELC experiment. This is further retrospective development, not an untouched test or proof of future accuracy.
- Only one fixed shot recipe is evaluated; neither its mixture weight nor decay is retuned after inspecting later results.
- Shots on target do not identify shot location or chance quality and must not be presented as measured xG.
- CSV publication times are unavailable, so the immediately preceding calendar day is excluded conservatively.
- Same-division history can be sparse for promoted or relegated clubs; the common v1 eligibility rule and all excluded counts remain visible.
- The archive contains missing and inconsistent statistics. Such fixtures retain verified goals and are never removed to improve measured accuracy.
- Player availability, lineups, weather and betting odds are not numeric inputs in this experiment.
- The original one-hundred-match series, public model and goal-only study remain unchanged. Any prospective comparison requires its own actual registration before kickoff.
