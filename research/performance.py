"""Past-match performance observations; target statistics are never features."""
from __future__ import annotations

import csv
from dataclasses import dataclass,replace
from datetime import date,timedelta
import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np

from research.data import Match,canonical,parse_csv,parse_day
from research.models import LearnedPoisson


@dataclass(frozen=True)
class Performance:
    match: Match
    home_shots: float|None
    away_shots: float|None
    home_sot: float|None
    away_sot: float|None
    home_xg: float|None=None
    away_xg: float|None=None
    issues: tuple=()


def optional(value,maximum):
    if value is None or not value.strip():return None
    result=float(value)
    if not math.isfinite(result) or not 0<=result<=maximum:raise ValueError('Invalid historical performance statistic')
    return result


def parse_performance(body,season):
    matches={m.key:m for m in parse_csv(body,season)}
    result=[]
    for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
        if not (row.get('Date') or '').strip():continue
        key=(parse_day(row['Date']).isoformat(),canonical(row['HomeTeam']),canonical(row['AwayTeam']))
        issues=[]
        def stat(key,maximum):
            try:return optional(row.get(key),maximum)
            except (ValueError,TypeError):issues.append(key+':invalid');return None
        hs,aws,hst,ast=[stat(k,100) for k in ('HS','AS','HST','AST')]
        if hs is not None and hst is not None and hst>hs:hs=hst=None;issues.append('home_shots:inconsistent')
        if aws is not None and ast is not None and ast>aws:aws=ast=None;issues.append('away_shots:inconsistent')
        hxg,axg=stat('HxG',15),stat('AxG',15)
        result.append(Performance(matches[key],hs,aws,hst,ast,hxg,axg,tuple(issues)))
    return sorted(result,key=lambda row:row.match.key)


def load_performance(directory,seasons):
    observations=[];manifest=[]
    for season in seasons:
        path=Path(directory)/(str(season)+'-E0.csv');body=path.read_bytes();rows=parse_performance(body,season)
        observations.extend(rows)
        manifest.append({'season':season,'rows':len(rows),'sha256':hashlib.sha256(body).hexdigest(),
            'shotsRows':sum(r.home_sot is not None and r.away_sot is not None for r in rows),
            'xgRows':sum(r.home_xg is not None and r.away_xg is not None for r in rows),
            'invalidStatisticsRows':sum(bool(r.issues) for r in rows),
            'source':f'https://www.football-data.co.uk/mmz4281/{season%100:02d}{(season+1)%100:02d}/E0.csv'})
    observations.sort(key=lambda row:row.match.key)
    if len({r.match.key for r in observations})!=len(observations):raise ValueError('Duplicate performance fixture')
    return observations,manifest


class ShotPoisson(LearnedPoisson):
    """Smooth goal-count targets using past shots on target and a past-only scale.

    This is a shot-based proxy, not xG. The Poisson quasi-likelihood accepts
    non-negative continuous targets; all observed actual results stay separate.
    """
    def __init__(self,goal_weight=.5,**settings):
        super().__init__(**settings)
        if not 0<=goal_weight<=1:raise ValueError('Invalid goal weight')
        self.goal_weight=goal_weight;self.shot_scale=None

    def fit(self,observations,day):
        prior=[r for r in observations if 0<(day-r.match.day).days<=self.window_days]
        covered=[r for r in prior if r.home_sot is not None and r.away_sot is not None]
        if len(covered)<100:raise ValueError('At least 100 past games with shots on target are required')
        weights=[math.exp(-math.log(2)*(day-r.match.day).days/self.half_life) for r in covered]
        goals=sum(w*(r.match.home_goals+r.match.away_goals) for w,r in zip(weights,covered))
        shots=sum(w*(r.home_sot+r.away_sot) for w,r in zip(weights,covered))
        if shots<=0:raise ValueError('No usable past shots on target')
        self.shot_scale=goals/shots
        modified=[]
        for r in prior:
            hg=r.match.home_goals;ag=r.match.away_goals
            if r.home_sot is not None and r.away_sot is not None:
                hg=self.goal_weight*hg+(1-self.goal_weight)*self.shot_scale*r.home_sot
                ag=self.goal_weight*ag+(1-self.goal_weight)*self.shot_scale*r.away_sot
            modified.append(replace(r.match,home_goals=hg,away_goals=ag))
        super().fit(modified,day)
        self.fit_info['shot_games']=len(covered)
        return self

    def export(self):
        result=super().export();result['model_version']='shot-poisson-0.1'
        result['shot_target']={'goal_weight':self.goal_weight,'goals_per_shot_on_target':self.shot_scale,'is_xg':False}
        return result


FEATURES=['elo_difference']
for window in (5,15):
    FEATURES.extend(f'{side}_{stat}_{window}' for side in ('home','away') for stat in ('goals_for','goals_against','shots_for','shots_against','sot_for','sot_against','points'))
FEATURES+=['home_league_interval_days','away_league_interval_days','home_history_games','away_history_games']


def feature_state(observations,cutoff):
    """Serializable source state, made only from dates strictly before cutoff."""
    prior=[r for r in observations if r.match.day<cutoff]
    histories={};ratings={};season=None
    for r in prior:
        m=r.match
        if m.season!=season:
            if season is not None:ratings={k:1500+(v-1500)*.85 for k,v in ratings.items()}
            season=m.season
        # No club plays twice on a league calendar day; per-match updates are
        # equivalent to the batch Elo updates for valid league data.
        delta=ratings.get(m.home,1500)-ratings.get(m.away,1500)+80
        expected=1/(1+10**(-delta/400));actual=1 if m.outcome==0 else .5 if m.outcome==1 else 0
        movement=30*(actual-expected)
        ratings[m.home]=ratings.get(m.home,1500)+movement;ratings[m.away]=ratings.get(m.away,1500)-movement
        for name,hg,ag,hs,aws,hst,ast in [(m.home,m.home_goals,m.away_goals,r.home_shots,r.away_shots,r.home_sot,r.away_sot),
            (m.away,m.away_goals,m.home_goals,r.away_shots,r.home_shots,r.away_sot,r.home_sot)]:
            histories.setdefault(name,[]).append({'date':m.day.isoformat(),'goals_for':hg,'goals_against':ag,
                'shots_for':hs,'shots_against':aws,'sot_for':hst,'sot_against':ast,'points':3 if hg>ag else 1 if hg==ag else 0})
            histories[name]=histories[name][-15:]
    return {'cutoffExclusive':cutoff.isoformat(),'latestMatch':max((r.match.day.isoformat() for r in prior),default=None),'histories':histories,'ratings':ratings}


def vector(state,home,away,target_day):
    if target_day<date.fromisoformat(state['cutoffExclusive']):raise ValueError('Feature state contains future observations')
    result=[(state['ratings'].get(home,1500)-state['ratings'].get(away,1500))/400]
    priors={'goals_for':1.4,'goals_against':1.4,'shots_for':13.,'shots_against':13.,'sot_for':4.5,'sot_against':4.5,'points':1.4}
    for window in (5,15):
        for team in (home,away):
            history=state['histories'].get(team,[])[-window:]
            for stat in ('goals_for','goals_against','shots_for','shots_against','sot_for','sot_against','points'):
                values=[game[stat] for game in history if game[stat] is not None]
                result.append((sum(values)+3*priors[stat])/(len(values)+3))
    for team in (home,away):
        history=state['histories'].get(team,[])
        result.append(min(60,(target_day-date.fromisoformat(history[-1]['date'])).days) if history else 60)
    result.extend(len(state['histories'].get(team,[])) for team in (home,away))
    if len(result)!=len(FEATURES) or not np.all(np.isfinite(result)):raise ValueError('Invalid feature vector')
    return result


def feature_rows(observations):
    result=[];last=None;state=None
    for r in observations:
        m=r.match;cutoff=m.day-timedelta(days=1)
        if cutoff!=last:state=feature_state(observations,cutoff);last=cutoff
        result.append({'date':m.day.isoformat(),'cutoff_date':cutoff.isoformat(),'season':m.season,'home':m.home,'away':m.away,
            'actual':m.outcome,'features':vector(state,m.home,m.away,m.day)})
    return result
