import test from 'node:test';
import assert from 'node:assert/strict';
import {officialUrl,discoverOfficialLinks,parseOfficialArticle,publicIndexArticles,robotsAllows,collectOfficialNews,storedOfficialNews} from '../worker/official.mjs';

const config={index_url:'https://www.manutd.com/en/news',article_prefixes:['/en/news/']};
const now=Date.parse('2026-10-29T08:00:00Z'),url='https://www.manutd.com/en/news/smith-training-update';
const html=(date='2026-10-28T12:00:00Z',title='Smith returns to training')=>'<html><head><meta name="description" content="Alex Smith has returned to training."><script type="application/ld+json">'+JSON.stringify({'@type':'NewsArticle',url,headline:title,datePublished:date,isAccessibleForFree:false,articleBody:'PROTECTED BODY MUST NOT BE USED'})+'</script></head></html>';

test('official URLs require exact club hosts and article paths',()=>{
  assert.equal(officialUrl('/en/news/smith-training-update',config,{article:true}),url);
  for(const value of ['https://evil.example/en/news/injury','https://www.manutd.com.evil.example/en/news/injury','https://user@www.manutd.com/en/news/injury','http://www.manutd.com/en/news/injury','https://www.manutd.com/assets/injury.js'])assert.equal(officialUrl(value,config,{article:true}),null);
});

test('public metadata produces dated official reports without using protected body',()=>{
  const result=parseOfficialArticle(html(),{url,team:'Manchester United FC',config,now});
  assert.equal(result.sourceType,'official_club');assert.equal(result.access,'public_metadata_only');
  assert.ok(!JSON.stringify(result).includes('PROTECTED'));assert.equal(result.publishedAt,'2026-10-28T12:00:00.000Z');
  assert.equal(parseOfficialArticle(html('2026-10-30T12:00:00Z'),{url,team:'Manchester United FC',config,now}),null);
  assert.equal(parseOfficialArticle(html('2026-10-01T12:00:00Z'),{url,team:'Manchester United FC',config,now}),null);
  assert.equal(parseOfficialArticle(html('2026-10-28T12:00:00Z','United Women injury update'),{url,team:'Manchester United FC',config,now}),null);
});

test('public preview hydration resolves explicit metadata only and keeps its publication date',()=>{
  const data=[{postTitle:1,postSlug:2,publishedDateTime:3,description:4,postCategoryName:5,postStatus:6},
    'Smith injury update','/en/news/smith-training-update','2026-10-28T12:00:00Z','Alex Smith is a doubt for Saturday.','First Team','Published',
    {articleBody:'PROTECTED BODY MUST NOT BE USED'}];
  const page='<script id="__NUXT_DATA__" type="application/json">'+JSON.stringify(data)+'</script>';
  const results=publicIndexArticles(page,{team:'Manchester United FC',config,now});
  assert.equal(results.length,1);assert.equal(results[0].metadataOrigin,'public_index');
  assert.equal(results[0].publishedAt,'2026-10-28T12:00:00.000Z');assert.ok(!JSON.stringify(results).includes('PROTECTED'));
});

test('robots policies honor the specific agent, wildcards and more-specific allow rules',()=>{
  const text='User-agent: *\nDisallow: /private/\nDisallow: /news/*?preview=\nAllow: /private/public/\nUser-agent: OtherBot\nDisallow: /';
  assert.equal(robotsAllows(text,'https://club.test/private/x'),false);
  assert.equal(robotsAllows(text,'https://club.test/private/public/item'),true);
  assert.equal(robotsAllows(text,'https://club.test/news/a?preview=1'),false);
  assert.equal(robotsAllows(text,'https://club.test/news/a'),true);
});

test('article discovery ignores women, unrelated news and foreign links',()=>{
  const result=discoverOfficialLinks('<a href="/en/news/smith-training-update">Smith returns to training</a><a href="/en/news/women-injury">Women injury update</a><a href="https://evil.example/en/news/injury">Injury</a>',config);
  assert.equal(result.length,1);assert.equal(result[0].url,url);
});

test('a blocked robots request stops collection without probing article pages',async()=>{
  const requests=[],cache=new Map();
  const services={now:()=>now,reserve:async()=>{},fetch:async target=>{requests.push(target);return new Response('',{status:403});},readLimited:async body=>new Response(body).text(),cachePut:async(key,value)=>cache.set(key,value)};
  const result=await collectOfficialNews({name:'Manchester United FC'},{FOOTPREDICT_CACHE:{get:async()=>null}},services);
  assert.equal(result.status,'unavailable');assert.equal(result.error.status,403);assert.equal(requests.length,1);
  assert.ok(requests[0].endsWith('/robots.txt'));
});

test('an off-site or non-news redirect is not followed',async()=>{
  for(const target of ['https://evil.example/collect','https://www.manutd.com/account/login']){
    const requested=[];
    const services={now:()=>now,reserve:async()=>{},cachePut:async()=>{},readLimited:async body=>new Response(body).text(),
      fetch:async address=>{requested.push(address);return address.endsWith('/robots.txt')?new Response('User-agent: *\nAllow: /'):new Response('',{status:302,headers:{Location:target}});}};
    const result=await collectOfficialNews({name:'Manchester United FC'},{FOOTPREDICT_CACHE:{get:async()=>null}},services);
    assert.equal(result.status,'unavailable');assert.ok(!requested.includes(target));assert.equal(requested.length,2);
  }
});

test('official collection and stored-only reads use bounded configured URLs',async()=>{
  const cache=new Map(),requests=[];
  const env={FOOTPREDICT_CACHE:{get:async key=>cache.get(key)||null}};
  const services={now:()=>now,reserve:async()=>{},cachePut:async(key,value)=>cache.set(key,value),readLimited:async body=>new Response(body).text(),
    fetch:async target=>{requests.push(target);return new Response(target.endsWith('/robots.txt')?'User-agent: *\nAllow: /':target===url?html():'<a href="'+url+'">Smith returns to training</a>');}};
  const team={name:'Manchester United FC'},result=await collectOfficialNews(team,env,services);
  assert.equal(result.status,'available');assert.equal(result.articles.length,1);assert.equal(requests.length,3);
  const stored=await storedOfficialNews(team,env,services);assert.equal(stored.status,'available');assert.equal(requests.length,3);
});
