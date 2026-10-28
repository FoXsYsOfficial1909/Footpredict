"""One bounded monitoring pass: news evidence and current app health, no training or writes to prediction history."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from research.data import parse_csv
from scripts.overnight import atomic_json,locked_state

ROOT=Path(__file__).resolve().parents[1]
API='https://footpredict-api.wzjp5747md.workers.dev'
HEADERS={'Origin':'https://foxsysofficial1909.github.io','User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36'}


def fetch(url,maximum=500000):
    request=urllib.request.Request(url,headers=HEADERS)
    with urllib.request.urlopen(request,timeout=20) as response:
        body=response.read(maximum+1)
        if len(body)>maximum:raise ValueError('Response size exceeded')
        return body,{'http':response.status,'fetched_at':datetime.now(timezone.utc).isoformat(),
                     'sha256':hashlib.sha256(body).hexdigest(),'url':url}


def feed_articles(body,now):
    tree=ET.fromstring(body)
    if tree.tag!='rss':raise ValueError('Not an RSS document')
    articles=[];seen=set()
    for item in tree.findall('./channel/item')[:50]:
        title=(item.findtext('title') or '').strip()[:500]
        url=(item.findtext('link') or '').strip()
        try:published=parsedate_to_datetime(item.findtext('pubDate') or '')
        except (TypeError,ValueError):continue
        if published.tzinfo is None:continue
        if not title or not url.startswith(('https://www.bbc.com/','https://www.bbc.co.uk/')) or url in seen:continue
        if published>now or published<now-timedelta(days=10):continue
        seen.add(url)
        articles.append({'title':title,'url':url,'published_at':published.isoformat(),
                         'summary':(item.findtext('description') or '').strip()[:900]})
        if len(articles)>=12:break
    return articles


def one_team(task):
    name,team,root,now=task
    url='https://feeds.bbci.co.uk/sport/football/teams/'+team['bbc_slug']+'/rss.xml'
    try:
        body,meta=fetch(url)
        articles=feed_articles(body,now)
        destination=root/'news-objects'/(meta['sha256']+'.xml')
        destination.parent.mkdir(exist_ok=True)
        if not destination.exists():destination.write_bytes(body)
        return {'team':name,'status':'available','source':meta,'articles':articles}
    except (urllib.error.URLError,ValueError,ET.ParseError,TimeoutError) as error:
        return {'team':name,'status':'unavailable','url':url,'error_type':type(error).__name__,
                'http':getattr(error,'code',None),'articles':[]}


def run(root,fixture_id):
    root=Path(root);data_dir=root/'data';data_dir.mkdir(exist_ok=True)
    now=datetime.now(timezone.utc);tag=now.strftime('%Y%m%dT%H%M%SZ')
    sources=json.loads((ROOT/'research/sources.json').read_text())['teams']
    current_path=data_dir/'2026-E0.csv'
    if not current_path.exists():raise ValueError('Validated current-season CSV is required for the monitored club list')
    games=parse_csv(current_path.read_bytes(),2026)
    current={name for game in games for name in (game.home,game.away)}
    tasks=[(name,team,data_dir,now) for name,team in sources.items() if team['csv_name'] in current]
    if len(tasks)!=20:raise ValueError('Current Premier League source coverage is not exactly 20 clubs')
    with ThreadPoolExecutor(max_workers=2) as pool:feeds=list(pool.map(one_team,tasks))
    snapshot={'started_at':now.isoformat(),'collected_at':datetime.now(timezone.utc).isoformat(),
              'scope':'Premier League current clubs; BBC Sport reports; not a confirmed injury database','feeds':feeds}
    path=data_dir/('news-snapshot-'+tag+'.json');atomic_json(path,snapshot)
    available=sum(feed['status']=='available' for feed in feeds)
    urls={item['url'] for feed in feeds for item in feed['articles']}
    summary={'snapshot':str(path),'clubs_attempted':len(feeds),'feeds_available':available,'unique_articles':len(urls),'checked_at':snapshot['collected_at']}
    try:
        body,_=fetch(API+'/v1/health',100000);health=json.loads(body)
        summary['health']={'version':health.get('version'),'configured':health.get('configured'),'research':health.get('research')}
    except Exception as error:summary['health']={'error':type(error).__name__}
    try:
        body,_=fetch(API+'/v1/history',6000000);history=json.loads(body)
        ids={str(item['prediction']['predictionId']):hashlib.sha256(json.dumps(item['prediction'],sort_keys=True,separators=(',',':')).encode()).hexdigest() for item in history['items']}
        baseline=data_dir/'prediction-history-hashes.json'
        previous=json.loads(baseline.read_text()) if baseline.exists() else ids
        changed=[name for name,value in previous.items() if name in ids and ids[name]!=value]
        if not baseline.exists():atomic_json(baseline,ids)
        summary['history']={'items':len(ids),'changed_preexisting_predictions':changed,
                            'settled':sum(item.get('latest',{}).get('status')=='settled' for item in history['items'])}
        if changed:raise RuntimeError('Previously saved predictions changed')
    except Exception as error:
        summary.setdefault('history',{})['error']=type(error).__name__
    try:
        body,_=fetch(API+'/v1/research/'+str(fixture_id)+'/stored',500000);card=json.loads(body)
        atomic_json(data_dir/('research-snapshot-'+tag+'.json'),card)
        summary['research']={'version':card.get('version'),'status':card.get('status'),'ai_status':card.get('ai',{}).get('status'),
                             'ai_facts':len(card.get('ai',{}).get('facts',[])),'articles':len(card.get('articles',[])),'coverage':card.get('coverage'),
                             'source_errors':card.get('sourceErrors',[])}
    except Exception as error:summary['research']={'error':type(error).__name__,'http':getattr(error,'code',None)}
    atomic_json(data_dir/'source-summary-latest.json',summary)
    with locked_state(root) as state:
        previous=state.get('monitor_summary')
        state['monitor_summary']=summary
        if not previous:
            state['updates'].append({'at':snapshot['collected_at'],'message':f'Запущен сбор новостных снимков по 20 клубам АПЛ. Первый проход: {available}/20 доступных RSS, {len(urls)} уникальных материалов.'})
        for key in ('history','health'):
            if summary.get(key,{}).get('error'):
                message='Мониторинг '+key+': '+summary[key]['error']
                if message not in state['blockers']:state['blockers'].append(message)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--fixture-id',type=int,default=560626)
    args=parser.parse_args();run(args.run_dir,args.fixture_id)
