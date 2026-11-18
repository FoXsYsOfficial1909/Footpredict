"""Reuse verified past feature rows; rebuild each changed league's affected suffix.

The original neural feature builder remains the full reference implementation.
Changing a score, date, club, shot count or missing marker invalidates subsequent
rows in that division. No row is approximated and no optimizer setting changes.
"""
from collections import Counter
from datetime import date,timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
import zlib

from research.data import Match,DIVISIONS
from research.division_model import DivisionGame
from research.evaluate import base_distribution
from research.models import v1_statistics
from research.neural_experiment import feature_rows,verify_rows
from research.neural_features import (DIVISION_ORDER,_validate,_coverage,
                                    _probabilities,build_rows)
from research.nightly import sha
from research.performance import Performance,feature_state,vector


def implementation_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def normalized(games):
    return [[g.division,o.match.day.isoformat(),o.match.season,o.match.home,o.match.away,
             o.match.home_goals,o.match.away_goals,o.home_shots,o.away_shots,o.home_sot,o.away_sot,list(o.issues)]
            for g in games for o in [g.observation]]


def from_snapshot(records):
    if not isinstance(records,list):raise ValueError('Cached source must be a list')
    games=[]
    for row in records:
        if not isinstance(row,list) or len(row)!=12 or not isinstance(row[11],list):
            raise ValueError('Invalid cached neural source row')
        division,day,season,home,away,hg,ag,hs,aws,hst,ast,issues=row
        games.append(DivisionGame(division,Performance(Match(date.fromisoformat(day),season,home,away,hg,ag),
                                                    hs,aws,hst,ast,issues=tuple(issues))))
    _validate(games)
    return games


def key(row):return row[0],row[1],row[3],row[4]


def selected_rows(games,targets):
    """Build exact original features only for requested target identities."""
    result=[]
    for division in DIVISION_ORDER:
        observations=sorted((g.observation for g in games if g.division==division),key=lambda o:o.match.key)
        matches=[o.match for o in observations];previous=None;frequencies={}
        for observation in observations:
            m=observation.match
            if (division,*m.key) not in targets:continue
            cutoff=m.day-timedelta(days=1)
            if cutoff!=previous:
                state=feature_state(observations,cutoff)
                counts=Counter(team for prior in matches if prior.day<cutoff for team in (prior.home,prior.away))
                previous=cutoff
            cache_key=cutoff,m.season
            if cache_key not in frequencies:
                frequencies[cache_key]=_probabilities(base_distribution(matches,cutoff,m.season))
            frequency=list(frequencies[cache_key]);old=v1_statistics(matches,cutoff,m.season,m.home,m.away)
            v1=_probabilities(old['probabilities'].tolist()) if old is not None else None
            base=list(v1) if v1 is not None else list(frequency)
            values=list(vector(state,m.home,m.away,m.day))+_coverage(state,m.home,m.away)
            values.extend(float(d==division) for d in DIVISION_ORDER)
            values.extend(math.log(max(value,1e-12)) for value in base);values.append(float(v1 is not None))
            result.append({'date':m.day.isoformat(),'cutoff_date':cutoff.isoformat(),
                'season':m.season,'division':division,'league':DIVISIONS[division]['league'],
                'home':m.home,'away':m.away,'actual':m.outcome,
                'base':base,'v1':v1,'frequency':frequency,'v1_available':v1 is not None,
                'features':[float(v) for v in values],'latest_input_match':state['latestMatch'],
                'history_counts':{'home':counts[m.home],'away':counts[m.away]},
                'recent_history_counts':{side:len(state['histories'].get(team,[]))
                    for side,team in (('home',m.home),('away',m.away))},
                'unknown_teams':[team for team in (m.home,m.away) if team not in state['histories']]})
    return result


def prepare_rows(games,plan,directory):
    """Return all rows and measured coverage; a missing/invalid cache rebuilds fully."""
    games=list(games);_validate(games);directory=Path(directory)
    current=normalized(games)
    reason='cache_missing'
    try:
        if not (directory/'feature-rows.json.gz').is_file():raise FileNotFoundError('Feature rows missing')
        manifest=json.loads((directory/'feature-rows-manifest.json').read_text())
        if not isinstance(manifest,dict):raise ValueError('Invalid feature manifest')
        # An initial cache from the original full builder has no incremental hash.
        if manifest.get('incrementalCodeSha256',implementation_sha())!=implementation_sha():
            raise ValueError('Incremental implementation changed')
        previous=json.loads(gzip.decompress((directory/'training-data.json.gz').read_bytes()))
        if sha(previous)!=manifest.get('sourceDataSha256'):raise ValueError('Cached source checksum changed')
        old_games=from_snapshot(previous)
        cached=feature_rows(old_games,plan,directory/'feature-rows.json.gz')
    except (OSError,ValueError,TypeError,KeyError,EOFError,zlib.error):
        reason='cache_missing' if not (directory/'feature-rows-manifest.json').exists() else 'cache_rejected'
        rows=build_rows(games);verify_rows(rows,games,plan)
        return rows,{'mode':'full','reason':reason,'reusedRows':0,'rebuiltRows':len(rows),
                     'totalRows':len(rows),'invalidatedFrom':{}}
    old_index={key(row):row for row in previous};new_index={key(row):row for row in current}
    invalidated={}
    for identity in old_index.keys()|new_index.keys():
        if old_index.get(identity)!=new_index.get(identity):
            division,day,*_=identity
            invalidated[division]=min(day,invalidated.get(division,day))
    reused=[row for row in cached if (row['division'] not in invalidated or
            row['date']<invalidated[row['division']]) and
            (row['division'],row['date'],row['home'],row['away']) in new_index]
    keep={(r['division'],r['date'],r['home'],r['away']) for r in reused}
    rebuilt=selected_rows(games,new_index.keys()-keep)
    rows=sorted(reused+rebuilt,key=lambda r:(r['date'],r['division'],r['home'],r['away']))
    verify_rows(rows,games,plan)
    return rows,{'mode':'incremental' if rebuilt or invalidated else 'reused','reason':'verified_cache',
                 'reusedRows':len(reused),'rebuiltRows':len(rebuilt),'totalRows':len(rows),'invalidatedFrom':invalidated}
