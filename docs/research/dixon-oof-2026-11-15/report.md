# Один параметр Dixon–Coles по прошлым прогнозам

Поправка оценена по прежним прогнозам интенсивности голов, рассчитанным до соответствующих игр. Это дальнейшая разработка на известной истории, а не первый опыт Dixon–Coles и не независимое подтверждение.

Раннее обучение rho: 1900 игр сезонов 2018/19–2022/23. Получено rho = 0.03089299. На валидации выбран `one_oof_fitted_rho` (rho = 0.03089299); выбор сохранён до расчёта поздних метрик.

Выбранный `selected` совпадает с `fitted`, если поправка выбрана, иначе с `uncorrected`. Это одна и та же выбранная модель, а не дополнительное независимое подтверждение.

| Период | Всего | Общих | Исключено у всех | Модель | Попадания | Log Loss ↓ | Brier ↓ |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| validation | 760 | 736 | 24 | selected | 55.71% | 0.961342510 | 0.570911 |
| validation | 760 | 736 | 24 | fitted | 55.71% | 0.961342510 | 0.570911 |
| validation | 760 | 736 | 24 | uncorrected | 55.71% | 0.961342850 | 0.570961 |
| validation | 760 | 736 | 24 | v1 | 52.85% | 0.994262347 | 0.591770 |
| validation | 760 | 736 | 24 | base | 43.61% | 1.072321621 | 0.649403 |
| audit | 380 | 369 | 11 | selected | 47.43% | 1.026410493 | 0.616280 |
| audit | 380 | 369 | 11 | fitted | 47.43% | 1.026410493 | 0.616280 |
| audit | 380 | 369 | 11 | uncorrected | 47.43% | 1.025578651 | 0.615846 |
| audit | 380 | 369 | 11 | v1 | 48.51% | 1.050059650 | 0.632330 |
| audit | 380 | 369 | 11 | base | 41.73% | 1.088061662 | 0.659003 |
| monitoring | 100 | 89 | 11 | selected | 47.19% | 1.046047331 | 0.626703 |
| monitoring | 100 | 89 | 11 | fitted | 47.19% | 1.046047331 | 0.626703 |
| monitoring | 100 | 89 | 11 | uncorrected | 47.19% | 1.045227288 | 0.626174 |
| monitoring | 100 | 89 | 11 | v1 | 41.57% | 1.085724479 | 0.658472 |
| monitoring | 100 | 89 | 11 | base | 41.57% | 1.100106880 | 0.667714 |

Ничьи: средняя вероятность / фактическая частота на общей выборке.

| Период | Модель | Оценка ничьей | Частота ничьих | ECE ничьих ↓ |
| --- | --- | ---: | ---: | ---: |
| validation | selected | 23.26% | 22.96% | 0.007321 |
| validation | uncorrected | 23.73% | 22.96% | 0.010815 |
| validation | v1 | 22.86% | 22.96% | 0.015207 |
| audit | selected | 24.46% | 27.64% | 0.031857 |
| audit | uncorrected | 24.96% | 27.64% | 0.026822 |
| audit | v1 | 24.53% | 27.64% | 0.031157 |
| monitoring | selected | 25.13% | 28.09% | 0.036521 |
| monitoring | uncorrected | 25.65% | 28.09% | 0.031419 |
| monitoring | v1 | 24.80% | 28.09% | 0.041508 |

Описательный исторический критерий: **False**. Независимое преимущество не установлено. Основная модель и все зафиксированные прогнозы неизменны.

Положительный rho уменьшает вероятности 0:0 и 1:1 относительно независимого Пуассона. Знак получен из ранних данных, не задан по наблюдаемой частоте ничьих позднего периода. Совпадение общей частоты ничьих не является самостоятельным критерием качества.

Brier — средняя сумма трёх квадратов ошибок, шкала 0–2; Log Loss — натуральный отрицательный логарифм вероятности фактического исхода. Для обеих ошибок меньше лучше. Равные максимумы отдельно отмечаются и исключаются только из знаменателя unique-top accuracy. Полный охват и список пропусков сохранены.

Все варианты используют одинаковую оценку силы клубов и один Elo; различается только зависимость четырёх низких счетов. Точная сила пересчитывается по прошлым играм перед каждой датой. Поправка rho обучена один раз на ранних OOF-прогнозах; поздние результаты не меняют её. Глобальная область rho сохраняет положительность при всех допустимых интенсивностях, без отдельного изменения параметра для каждого матча.

95% интервал отличия Log Loss выбранного варианта от uncorrected на периоде audit:

```json
{
  "difference": 0.0008318424312877183,
  "ci95": [
    -0.00041429906267589515,
    0.0021492749291187408
  ],
  "method": "paired resampling of calendar-day clusters",
  "samples": 1500
}
```

Ограничения:

- Every reported season and its aggregate draw frequency have already been inspected. This is further retrospective development, not an untouched test.
- A marginal draw-frequency gap alone does not establish conditional miscalibration or prove this correction useful.
- The strength recipe was selected previously using the same validation seasons; this study does not repeat that grid search.
- Goal scores alone are used. News, injuries, lineups and measured xG are not available historical inputs here.
- No future packet or new daily job is warranted for an identical rho-zero selection; preserve all existing forecasts.

## Воспроизведение без сети

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.dixon_experiment \
  --output /tmp/footpredict-dixon-oof
```
