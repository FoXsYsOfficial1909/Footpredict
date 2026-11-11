"""Dated provider reports as features, never a declaration of medical fitness."""
from datetime import timedelta

from research.features_time import instant

STATUSES = ('injured', 'suspended', 'unavailable', 'doubtful', 'available', 'unknown')
SHARES = ('reported_loss_minutes_share', 'doubtful_minutes_share', 'uncertain_minutes_share')


def _date(value, cutoff, label):
    parsed = instant(value)
    if parsed > cutoff:
        raise ValueError(label + ' postdates the feature cutoff')
    return parsed


def _team(rows, name):
    selected = [row for row in rows if row.get('team') == name]
    if len(selected) != 1:
        raise ValueError('Player source team identity is missing or ambiguous')
    return selected[0]


def structured_player_features(packet):
    fixture = packet['fixture']
    cutoff, kickoff = instant(packet['featureCutoffAt']), instant(fixture['kickoff'])
    features, metadata = {}, {'scope': 'provider reports, not confirmed absences', 'lineups': {}}
    source = packet.get('playerAvailability') or {}
    active = source.get('status') in ('available', 'partial')
    if active:
        fetched = _date(source['fetchedAt'], cutoff, 'FPL source')
        match = source.get('fixtureMatch') or {}
        if (cutoff-fetched > timedelta(hours=1) or source.get('sourceType') != 'official_fpl_metadata'
            or match.get('validation') != 'exact_clubs_and_utc_kickoff'
            or match.get('home') != fixture['home'] or match.get('away') != fixture['away']
            or instant(match['kickoff']) != kickoff or not source.get('coverage', {}).get('fixtureMatched')):
            raise ValueError('FPL source fixture or freshness mismatch')
        metadata['fpl_fetched_at'] = source['fetchedAt']
    for side in ('home', 'away'):
        prefix = side + '_fpl_'
        features.update({prefix+'source_available': False, prefix+'reports': None,
            prefix+'stale_reports': None, prefix+'minutes_complete': False,
            **{prefix+key: None for key in SHARES}})
        if not active:
            continue
        team = _team(source.get('teams', []), fixture[side])
        if not team.get('registeredPlayerCount'):
            continue
        reports, seen = team.get('reports', []), set()
        for report in reports:
            if report.get('playerId') in seen or report.get('team') != fixture[side]:
                raise ValueError('Duplicate player report or wrong team')
            seen.add(report.get('playerId'))
            _date(report['fetchedAt'], cutoff, 'FPL report')
            if report.get('publishedAt') and _date(report['publishedAt'], cutoff, 'FPL publication') > fetched:
                raise ValueError('FPL publication postdates its source fetch')
        features.update({prefix+'source_available': True, prefix+'reports': len(reports),
            prefix+'stale_reports': sum(bool(r.get('stale')) for r in reports)})
        roster = team.get('roster')
        if roster is None:  # Old snapshots cannot borrow today's minutes.
            continue
        if not isinstance(roster, list) or len(roster) != team['registeredPlayerCount'] or len(roster)>120:
            raise ValueError('Invalid FPL registered roster')
        ids = [p.get('playerId') for p in roster]
        if any(not isinstance(i, int) or isinstance(i, bool) or i<=0 for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('Invalid or duplicate FPL player identity')
        if not seen.issubset(set(ids)):
            raise ValueError('Report player is outside the registered roster')
        complete = True
        weights = dict.fromkeys(SHARES, 0.)
        total = 0
        for player in roster:
            minutes = player.get('seasonMinutes')
            if player.get('newsPublishedAt') and _date(player['newsPublishedAt'], cutoff, 'Roster news') > fetched:
                raise ValueError('Roster publication postdates its source fetch')
            if player.get('status') not in STATUSES or player.get('position') not in (1, 2, 3, 4):
                raise ValueError('Invalid FPL player status or position')
            if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0<=minutes<=10000:
                complete = False
                continue
            total += minutes
            status = player['status']
            if status == 'available':
                continue  # Unflagged metadata is not medical clearance.
            recent = (player.get('newsFreshness') == 'recent' and player.get('newsPublishedAt')
                and fetched-instant(player['newsPublishedAt']) <= timedelta(days=14))
            key = ('reported_loss_minutes_share' if recent and status in ('injured', 'suspended', 'unavailable')
                else 'doubtful_minutes_share' if recent and status == 'doubtful' else 'uncertain_minutes_share')
            weights[key] += minutes
        if complete and total>0:
            features[prefix+'minutes_complete'] = True
            for key in SHARES:
                features[prefix+key] = weights[key]/total
    lineups = packet.get('reportedLineups') or {}
    provided = lineups.get('status') == 'reported_by_provider'
    if provided:
        fetched = _date(lineups['fetchedAt'], cutoff, 'Lineup source')
        match = lineups.get('fixture') or {}
        if (match.get('id') != fixture['id'] or match.get('home') != fixture['home']
            or match.get('away') != fixture['away'] or instant(match['kickoff']) != kickoff
            or kickoff-cutoff>timedelta(minutes=90) or cutoff-fetched>timedelta(minutes=10)):
            raise ValueError('Lineup fixture or late forecast window mismatch')
    for side in ('home', 'away'):
        features[side+'_provider_xi_received'] = False
        if not provided:
            continue
        team = _team(lineups.get('teams', []), fixture[side])
        if team.get('status') != 'reported_by_provider':
            continue
        players = team.get('players', [])
        if len(players)!=11 or len({p.get('id') for p in players})!=11:
            raise ValueError('A provider starting XI requires eleven unique players')
        features[side+'_provider_xi_received'] = True
        metadata['lineups'][side] = {'players': players, 'fetched_at': lineups['fetchedAt'],
            'official_confirmation': False}
    return features, metadata
