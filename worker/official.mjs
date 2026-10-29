// Public club metadata only. No login, protected article body or arbitrary URLs.
import SOURCES from './research-sources.mjs';
import {cleanText} from './text.mjs';
import {availabilityPriority,isFirstTeamText} from './availability.mjs';

export const OFFICIAL_VERSION='official-metadata-1';
export const OFFICIAL_LIMITS=Object.freeze({dailyRequests:180,refreshHours:12,htmlBytes:2500000,articlesPerClub:1});
const DAY=86400000,UA='FootpredictResearch/0.5';
const configFor=name=>SOURCES.teams[name]?.official;

function attributes(text){const values={};for(const m of text.matchAll(/([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g))values[m[1].toLowerCase()]=cleanText(m[2]??m[3],3000);return values;}

export function officialUrl(value,config,{article=false}={}) {
  try{
    const base=new URL(config.index_url),url=new URL(value,base),bare=base.hostname.replace(/^www\./,'');
    if(url.protocol!=='https:'||url.username||url.password||url.port||!new Set([bare,'www.'+bare]).has(url.hostname))return null;
    if(url.pathname.length>600||/\.(?:js|json|css|svg|png|jpe?g|webp|gif|mp4|pdf)$/i.test(url.pathname))return null;
    if(article&&(!config.article_prefixes.some(prefix=>url.pathname.startsWith(prefix))||/\/(?:listing|category|latest-news)(?:\/|$)/i.test(url.pathname)))return null;
    url.hash='';url.search='';return url.href;
  }catch{return null;}
}

function structured(html){
  const nodes=[];
  const visit=(value,depth=0)=>{
    if(depth>8||nodes.length>600||!value||typeof value!=='object')return;
    if(Array.isArray(value)){for(const item of value.slice(0,100))visit(item,depth+1);return;}
    nodes.push(value);
    for(const key of ['@graph','hasPart','itemListElement','mainEntity','item'])visit(value[key],depth+1);
  };
  for(const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)){
    if(!/application\/ld\+json/i.test(m[1])||m[2].length>400000)continue;
    try{visit(JSON.parse(m[2]));}catch{/* Invalid metadata is not evidence. */}
  }
  return nodes;
}

export function discoverOfficialLinks(html,config){
  const candidates=new Map();
  const add=(address,title='',section='')=>{
    const url=officialUrl(address,config,{article:true});
    if(!url||!isFirstTeamText(url+' '+title+' '+section)||!availabilityPriority(url.replace(/-/g,' ')+' '+title))return;
    const old=candidates.get(url);if(!old||title.length>old.title.length)candidates.set(url,{url,title:cleanText(title,400),section});
  };
  for(const node of structured(html))add(node.url||node.mainEntityOfPage?.['@id']||'',node.headline||node.name||'',node.articleSection||'');
  const ranked=()=>[...candidates.values()].sort((a,b)=>availabilityPriority((b.title||b.url).replace(/-/g,' '))-availabilityPriority((a.title||a.url).replace(/-/g,' '))).slice(0,8);
  if(candidates.size)return ranked();
  for(const m of html.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/gi)){
    const attrs=attributes(m[1]);add(attrs.href||'',cleanText(m[2],400)||attrs['aria-label']||attrs.title||'');
  }
  // Club sites also publish article URLs in client hydration data. Only exact
  // configured hosts and news paths are followed, never external assets or APIs.
  const decoded=html.replace(/&quot;/gi,'"').replace(/&amp;/gi,'&').replace(/\\"/g,'"').replace(/\\\//g,'/');
  for(const m of decoded.matchAll(/(?:https:\/\/[^\s"<>\\]+)?\/(?:en\/)?news\/[a-zA-Z0-9/_-]{8,}\/?/g))add(m[0]);
  return ranked();
}

export function publicPayloadPath(html,config){
  const tag=html.match(/<script\b([^>]*\bid=["']__NUXT_DATA__["'][^>]*)>/i);
  if(!tag)return null;
  const value=attributes(tag[1])['data-src'];if(!value)return null;
  try{const url=new URL(value,config.index_url),base=new URL(config.index_url);return url.origin===base.origin&&/\/news\/_payload\.json$/.test(url.pathname)?url.href:null;}catch{return null;}
}

export function payloadLinks(body,config){
  if(typeof body!=='string'||body.length>OFFICIAL_LIMITS.htmlBytes)return [];
  // Devalue hydration strings retain public URLs; no execution or recursive
  // object reconstruction is necessary to discover the linked article.
  return discoverOfficialLinks(body,config);
}

function publicationDate(value,url){
  let raw=cleanText(value,100),precision='second';
  if(!raw){
    const match=url.match(/\/news\/(20\d{2})\/([a-z]+|\d{2})\/(\d{1,2})\//i);
    if(match){const months=['january','february','march','april','may','june','july','august','september','october','november','december'];
      const month=/^\d+$/.test(match[2])?Number(match[2]):months.indexOf(match[2].toLowerCase())+1;
      if(month>=1&&month<=12)raw=match[1]+'-'+String(month).padStart(2,'0')+'-'+match[3].padStart(2,'0');}
  }
  if(/^\d{4}-\d{2}-\d{2}$/.test(raw)){raw+='T00:00:00Z';precision='day';}
  const time=Date.parse(raw);
  return Number.isFinite(time)?{publishedAt:new Date(time).toISOString(),publishedPrecision:precision}:null;
}

export function parseOfficialArticle(html,{url,team,config,now=Date.now(),fetchedAt=new Date(now).toISOString()}){
  const valid=officialUrl(url,config,{article:true});if(!valid)return null;
  const nodes=structured(html),node=nodes.find(node=>{
    const identity=node.url||node.mainEntityOfPage?.['@id']||(typeof node.mainEntityOfPage==='string'?node.mainEntityOfPage:null);
    return [node['@type']].flat().some(type=>/^(?:NewsArticle|Article|BlogPosting)$/.test(type))&&(!identity||officialUrl(identity,config,{article:true})===valid);
  });
  const meta={};for(const m of html.matchAll(/<meta\b([^>]*)>/gi)){const a=attributes(m[1]);if(a.property||a.name)meta[(a.property||a.name).toLowerCase()]=a.content||'';}
  const title=cleanText(node?.headline||meta['og:title']||meta['twitter:title']||'',400);
  const summary=cleanText(meta.description||meta['og:description']||node?.description||'',700);
  const section=cleanText(node?.articleSection||meta['article:section']||'',100);
  if(!title||!isFirstTeamText(title+' '+summary+' '+url+' '+section+' '+jsonKeywords(node?.keywords))||!availabilityPriority(title+' '+summary))return null;
  const date=publicationDate(node?.datePublished||meta['article:published_time']||meta.datepublished||meta.pubdate||'',valid);
  if(!date||Date.parse(date.publishedAt)>now||now-Date.parse(date.publishedAt)>10*DAY)return null;
  return {id:'official:'+SOURCES.teams[team]?.bbc_slug+':'+new URL(valid).pathname,team,source:'Официальный сайт '+team,
    sourceType:'official_club',url:valid,title,summary,section,...date,fetchedAt,
    access:'public_metadata_only',verification:'official_club_report',
    note:'Используются публичный заголовок и описание, а не закрытый текст. Это сообщение клуба, не подтверждение готовности к выбранному матчу.'};
}

function jsonKeywords(value){return Array.isArray(value)?value.join(' '):typeof value==='string'?value:'';}

export function publicIndexArticles(html,{team,config,now=Date.now(),fetchedAt=new Date(now).toISOString()}){
  const articles=[];
  const add=node=>{
    const url=officialUrl(node.url,config,{article:true});if(!url||!node.title||!node.date)return;
    const schema={'@type':'NewsArticle',url,headline:node.title,description:node.summary||'',datePublished:node.date,articleSection:node.section||'',keywords:node.keywords||[]};
    const safe=JSON.stringify(schema).replace(/</g,'\\u003c');
    const article=parseOfficialArticle('<script type="application/ld+json">'+safe+'</script>',{url,team,config,now,fetchedAt});
    if(article&&!articles.some(old=>old.url===url))articles.push({...article,metadataOrigin:'public_index',observedAtUrl:config.index_url});
  };
  for(const node of structured(html)){
    if(![node['@type']].flat().some(type=>['NewsArticle','Article','BlogPosting'].includes(type)))continue;
    add({url:node.url||node.mainEntityOfPage?.['@id'],title:node.headline,summary:node.description,date:node.datePublished,section:node.articleSection,keywords:node.keywords});
  }
  for(const m of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)){
    if(!/__NUXT_DATA__/.test(m[1])||m[2].length>250000)continue;
    try{
      const data=JSON.parse(m[2]);if(!Array.isArray(data))continue;
      const ref=value=>Number.isInteger(value)&&value>=0&&value<data.length?data[value]:value;
      for(const node of data){
        if(!node||typeof node!=='object'||Array.isArray(node)||!('postTitle' in node)||!('publishedDateTime' in node))continue;
        if(node.postStatus!==undefined&&String(ref(node.postStatus)).toLowerCase()!=='published')continue;
        add({url:ref(node.postSlug),title:ref(node.postTitle),summary:ref(node.description),date:ref(node.publishedDateTime),section:ref(node.postCategoryName)});
      }
    }catch{/* Malformed public preview metadata is ignored, never executed. */}
  }
  return articles.sort((a,b)=>availabilityPriority(b.title+' '+b.summary)-availabilityPriority(a.title+' '+a.summary)||Date.parse(b.publishedAt)-Date.parse(a.publishedAt));
}

export function robotsAllows(text,address,userAgent='FootpredictResearch'){
  const groups=[];let group={agents:[],rules:[]},seenRules=false;
  for(const raw of String(text).split(/\r?\n/).slice(0,3000)){
    const line=raw.replace(/#.*$/,'').trim(),split=line.indexOf(':');if(split<0)continue;
    const key=line.slice(0,split).trim().toLowerCase(),value=line.slice(split+1).trim();
    if(key==='user-agent'){
      if(seenRules){groups.push(group);group={agents:[],rules:[]};seenRules=false;}group.agents.push(value.toLowerCase());
    }else if((key==='allow'||key==='disallow')&&group.agents.length){seenRules=true;if(value)group.rules.push({allow:key==='allow',path:value});}
  }
  if(group.agents.length)groups.push(group);
  const ua=userAgent.toLowerCase(),matched=groups.map(g=>({...g,specificity:Math.max(-1,...g.agents.map(agent=>agent==='*'?0:ua.includes(agent)?agent.length:-1))}));
  const specificity=Math.max(-1,...matched.map(g=>g.specificity)),url=new URL(address),path=url.pathname+url.search;
  let winner=null;
  for(const g of matched.filter(g=>g.specificity===specificity&&specificity>=0))for(const rule of g.rules.slice(0,500)){
    const end=rule.path.endsWith('$'),literal=end?rule.path.slice(0,-1):rule.path;
    const pattern='^'+literal.split('*').map(part=>part.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('.*')+(end?'$':'');
    if(!new RegExp(pattern).test(path))continue;
    const length=literal.replace(/\*/g,'').length;
    if(!winner||length>winner.length||length===winner.length&&rule.allow)winner={...rule,length};
  }
  return winner?winner.allow:true;
}

async function request(url,env,services,maximum=OFFICIAL_LIMITS.htmlBytes){
  const day=new Date(services.now()).toISOString().slice(0,10);
  await services.reserve('official:day:'+day,OFFICIAL_LIMITS.dailyRequests);
  const response=await services.fetch(url,{headers:{'User-Agent':UA,Accept:'text/html,application/json;q=0.9,text/plain;q=0.8'},redirect:'manual',signal:AbortSignal.timeout(15000)});
  return {response,body:response.ok?await services.readLimited(response.body,maximum):''};
}

async function allowedPage(url,config,robots,env,services,{payload=false}={}){
  const base=new URL(config.index_url);
  for(let hop=0;hop<3;hop++){
    const current=new URL(url);
    if(current.origin!==base.origin||!robotsAllows(robots,current.href))throw Object.assign(new Error('robots_disallowed'),{status:403});
    if(current.pathname!==base.pathname&&!config.article_prefixes.some(prefix=>current.pathname.startsWith(prefix)))throw new Error('unsupported_redirect_path');
    if(payload&&!/\/news\/_payload\.json$/.test(current.pathname))throw new Error('unsupported_payload_path');
    const {response,body}=await request(current.href,env,services);
    if([301,302,303,307,308].includes(response.status)){
      const next=new URL(response.headers.get('Location')||'',current);
      if(next.origin!==base.origin)throw new Error('cross_origin_redirect');url=next.href;continue;
    }
    if(!response.ok)throw Object.assign(new Error('official_http_'+response.status),{status:response.status});
    return {body,url:current.href};
  }
  throw new Error('redirect_limit');
}

export async function storedOfficialNews(team,env,services){
  const mapping=SOURCES.teams[team.name];
  if(!mapping?.official)return {status:'not_configured',articles:[]};
  try{const cached=await env.FOOTPREDICT_CACHE?.get('research:official:'+mapping.bbc_slug,'json');
    const age=cached?services.now()-Date.parse(cached.fetchedAt):NaN;
    if(cached?.version===OFFICIAL_VERSION&&age>=0&&age<DAY)return {...cached,stale:age>12*3600000};
  }catch{/* A cache failure never becomes a healthy-player claim. */}
  return {status:'pending_collection',articles:[],url:mapping.official.index_url};
}

export async function collectOfficialNews(team,env,services){
  const mapping=SOURCES.teams[team.name],config=mapping?.official,now=services.now();
  if(!config)return {status:'not_configured',articles:[]};
  const result={version:OFFICIAL_VERSION,team:team.name,status:'unavailable',articles:[],fetchedAt:new Date(now).toISOString(),url:config.index_url};
  try{
    const origin=new URL(config.index_url).origin,key='official:robots:'+new URL(origin).hostname;
    let robots=await env.FOOTPREDICT_CACHE?.get(key,'json');
    if(!robots||now-Date.parse(robots.fetchedAt)>=DAY){
      const source=await request(origin+'/robots.txt',env,services,200000);
      if([404,410].includes(source.response.status))robots={text:'',fetchedAt:result.fetchedAt};
      else if(source.response.ok)robots={text:source.body,fetchedAt:result.fetchedAt};
      else throw Object.assign(new Error('robots_http_'+source.response.status),{status:source.response.status});
      try{await services.cachePut(key,robots,86400);}catch{/* Optional cache. */}
    }
    const listing=await allowedPage(config.index_url,config,robots.text,env,services);
    const listingObserved=structured(listing.body).some(node=>['NewsArticle','Article','BlogPosting'].includes(node['@type'])&&
      (node.headline||node.name||node.description)&&officialUrl(node.url||node.mainEntityOfPage?.['@id']||'',config,{article:true}))||
      /"postTitle"\s*:/.test(listing.body)&&/"publishedDateTime"\s*:/.test(listing.body);
    result.listingObserved=listingObserved;
    const previews=publicIndexArticles(listing.body,{team:team.name,config,now:services.now(),fetchedAt:new Date(services.now()).toISOString()});
    if(previews.length){result.articles=previews.slice(0,OFFICIAL_LIMITS.articlesPerClub);result.candidates=previews.length;result.status='available';}
    let candidates=previews.length?[]:discoverOfficialLinks(listing.body,config);
    const payload=publicPayloadPath(listing.body,config);
    if(!previews.length&&!candidates.length&&payload){
      const data=await allowedPage(payload,config,robots.text,env,services,{payload:true});
      candidates=payloadLinks(data.body,config);
    }
    if(!previews.length)result.candidates=candidates.length;
    for(const item of candidates.slice(0,OFFICIAL_LIMITS.articlesPerClub)){
      try{
        const page=await allowedPage(item.url,config,robots.text,env,services);
        const article=parseOfficialArticle(page.body,{url:page.url,team:team.name,config,now:services.now(),fetchedAt:new Date(services.now()).toISOString()});
        if(article)result.articles.push(article);
      }catch(error){result.articleError={status:error.status||null,reason:String(error.message).slice(0,100)};}
    }
    result.status=result.articles.length?'available':candidates.length||!listingObserved?'no_usable_metadata':'no_relevant_reports';
  }catch(error){result.error={status:Number.isInteger(error.status)?error.status:null,reason:cleanText(error.message,100)};}
  result.fetchedAt=new Date(services.now()).toISOString();
  try{await services.cachePut('research:official:'+mapping.bbc_slug,result,86400);}catch{/* Keep the source outcome visible to the caller. */}
  return result;
}

export const officialConfiguration=team=>configFor(team);
