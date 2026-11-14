# История клубов между АПЛ и Чемпионшипом

Один фиксированный вариант с общими параметрами клубов и отдельными средними результативности, домашним преимуществом и голевым масштабом ударов для каждой лиги. Отдельный Elo E1 сохранён.

| Период | Общих матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |
| --- | ---: | --- | ---: | ---: | ---: |
| validation | 1061 | joint | 0.617009 | 1.028333 | 48.2% |
| validation | 1061 | shots | 0.615535 | 1.025802 | 47.8% |
| validation | 1061 | v1 | 0.632445 | 1.048880 | 46.6% |
| validation | 1061 | base | 0.647336 | 1.070663 | 45.1% |
| audit | 533 | joint | 0.639212 | 1.058843 | 43.5% |
| audit | 533 | shots | 0.640247 | 1.060224 | 44.3% |
| audit | 533 | v1 | 0.645334 | 1.066056 | 42.8% |
| audit | 533 | base | 0.658634 | 1.086975 | 41.7% |
| monitoring | 159 | joint | 0.638697 | 1.056913 | 44.7% |
| monitoring | 159 | shots | 0.646101 | 1.067454 | 44.0% |
| monitoring | 159 | v1 | 0.632384 | 1.049836 | 48.4% |
| monitoring | 159 | base | 0.650672 | 1.074896 | 44.0% |

Критерий средних ошибок: False. Критерий исторического преимущества: False.

joint — совместное обучение; shots — сохранённый кандидат только по истории E1; v1 и base — прежние контрольные формулы.

История источников: 10532 игр. Обучение и проверка после загрузки: 8.86 с.

Это дальнейшая разработка на уже изученных исторических периодах. Исходные 100 и 36 будущих прогнозов не перезаписываются. Основная модель не меняется автоматически.

Ограничения:

- All ELC evaluation periods have been inspected in earlier experiments. This is retrospective development, not an untouched test or a new prospective score.
- A shared club strength assumes useful persistence across seasons. Transfers, coaches and current roster changes are not modelled.
- Same-division goals and home advantages remain separate. Cross-division strength is learned indirectly from clubs moving between divisions, not from direct inter-division league matches.
- Clubs arriving from League One receive no additional lower-division history in this two-division experiment.
- Shots on target are a proxy rather than measured xG. Dates lack reliable result-publication timestamps, so the previous calendar day is excluded conservatively.
- The fixed E1-only Elo component remains unchanged; this experiment does not tune a new shared Elo or search for new mixture weights.
- No historical result can authorize an automatic public-model change. The existing 100-match and 36-match packets remain byte-for-byte unchanged.
