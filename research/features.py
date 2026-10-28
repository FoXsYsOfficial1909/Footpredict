"""Point-in-time feature rows. Unknown availability stays null, never 'healthy'."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math

FEATURE_VERSION = 'apl-evidence-features-1'


def instant(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('A timezone is required')
    return result.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def number(value, *, minimum=0):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= minimum else None


def feature_row(packet):
    fixture = packet['fixture']
    if fixture.get('competition') != 'PL' or len(packet.get('teams', [])) != 2:
        raise ValueError('Expected a two-team Premier League snapshot')
    cutoff = instant(packet['featureCutoffAt'])
    collected = instant(packet['collectedAt'])
    kickoff = instant(fixture['kickoff'])
    if cutoff != collected or (kickoff - cutoff).total_seconds() < 300:
        raise ValueError('The complete snapshot must precede kickoff by at least five minutes')
    if instant(packet.get('collectionStartedAt', packet['collectedAt'])) > cutoff:
        raise ValueError('Invalid collection interval')
    articles = packet.get('articles', [])
    for article in articles:
        if instant(article['publishedAt']) > cutoff or instant(article['fetchedAt']) > cutoff:
            raise ValueError('News was not available at the feature cutoff')
    candidate = packet.get('candidate', {})
    probabilities = candidate.get('probabilities')
    if candidate.get('status') != 'experimental' or not isinstance(probabilities, list) or len(probabilities) != 3:
        raise ValueError('A frozen experimental forecast is required')
    if any(number(value) is None or value > 1 for value in probabilities) or abs(sum(probabilities) - 1) > 1e-8:
        raise ValueError('Invalid forecast probabilities')
    if instant(candidate['generatedAt']) > cutoff:
        raise ValueError('Forecast was generated after the snapshot cutoff')
    if instant(candidate['asOf'] + 'T00:00:00Z') >= kickoff:
        raise ValueError('Model training cutoff must precede kickoff')
    features = {'horizon_hours': (kickoff - cutoff).total_seconds() / 3600}
    for side, team in zip(('home', 'away'), packet['teams']):
        if team.get('name') != fixture[side]:
            raise ValueError('Team order does not match the fixture')
        form = team.get('form', [])[:5]
        for game in form:
            if (cutoff - instant(game['kickoff'])).total_seconds() < 4 * 3600:
                raise ValueError('Form contains a match inside the four-hour information fence')
            if number(game.get('goalsFor')) is None or number(game.get('goalsAgainst')) is None:
                raise ValueError('Invalid past score')
        prefix = side + '_'
        features.update({prefix + 'form_games': len(form),
                         prefix + 'form_points': sum(3 if g['goalsFor'] > g['goalsAgainst'] else 1 if g['goalsFor'] == g['goalsAgainst'] else 0 for g in form) if form else None,
                         prefix + 'form_goals_for': sum(g['goalsFor'] for g in form) / len(form) if form else None,
                         prefix + 'form_goals_against': sum(g['goalsAgainst'] for g in form) / len(form) if form else None})
        for venue in ('home', 'away'):
            values = team.get(venue, {})
            features[prefix + venue + '_games'] = number(values.get('games'))
            for key in ('goalsFor', 'goalsAgainst'):
                features[prefix + venue + '_' + key] = number(values.get(key)) if values.get('games', 0) else None
        calendar = team.get('calendar', {})
        features[prefix + 'calendar_gap_days'] = number(calendar.get('gapDays'))
        features[prefix + 'calendar_complete'] = calendar.get('coverage') == 'complete'
        features[prefix + 'calendar_uses_scheduled_match'] = calendar.get('basedOnScheduledMatch', False)
        injuries = team.get('injuries', {})
        received = injuries.get('status') in ('reported_by_api', 'no_reports_found')
        features[prefix + 'injury_source_available'] = received
        features[prefix + 'injury_reports'] = len(injuries.get('reports', [])) if received else None
        lineup = team.get('lineup', {})
        features[prefix + 'lineup_available'] = lineup.get('status') == 'provided_by_api'
        features[prefix + 'lineup_players'] = len(lineup.get('players', [])) if features[prefix + 'lineup_available'] else None
        features[prefix + 'news_articles'] = len({a['url'] for a in articles if a.get('team') == team['name']})
        ai = packet.get('ai', {})
        usable_ai = ai.get('status') in ('complete', 'partial')
        features[prefix + 'ai_source_available'] = usable_ai
        facts = {(fact['sourceId'], fact['quote']) for fact in ai.get('facts', []) if fact.get('team') == team['name'] and fact.get('category') in ('injury', 'availability', 'suspension')}
        features[prefix + 'availability_mentions'] = len(facts) if usable_ai else None
    return {'schema_version': FEATURE_VERSION, 'fixture_id': fixture['id'], 'kickoff': fixture['kickoff'],
            'home': fixture['home'], 'away': fixture['away'], 'collected_at': packet['collectedAt'],
            'snapshot_sha256': digest(packet), 'features': features,
            'forecast': {'model_version': candidate['modelVersion'], 'generated_at': candidate['generatedAt'],
                         'training_cutoff': candidate['asOf'], 'probabilities': probabilities,
                         'news_used_in_probabilities': candidate.get('newsUsedInProbabilities', False)},
            'target': None,
            'interpretation': 'Reports and missingness describe available sources, not confirmed player health. No outcome is a feature.'}
