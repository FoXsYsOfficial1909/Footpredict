import {AVAILABILITY_POLICY,validateEvidenceChoices,buildEvidence} from './availability.mjs';

export const AI_CACHE_VERSION='excerpt-review-1';
const DAY=86400000,LEASE=120000;

export const aiCacheSchema=()=>[
  `CREATE TABLE IF NOT EXISTS ai_evidence_cache (
    cache_key TEXT PRIMARY KEY,payload TEXT,created_at INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL DEFAULT 0,lease_until INTEGER NOT NULL DEFAULT 0,lease_token TEXT)`,
  'CREATE INDEX IF NOT EXISTS ai_evidence_expiry ON ai_evidence_cache(expires_at)'
];

export function evidenceCache(db){return {
  async get(key,now){
    const row=await db.prepare('SELECT payload,created_at,expires_at FROM ai_evidence_cache WHERE cache_key=?1').bind(key).first();
    if(!row?.payload||row.expires_at<=now)return null;
    if(row.created_at>now)return {invalid:true};
    try{return JSON.parse(row.payload);}catch{return {invalid:true};}
  },
  async invalidate(key,now){await db.prepare('UPDATE ai_evidence_cache SET expires_at=0 WHERE cache_key=?1 AND lease_until<=?2').bind(key,now).run();},
  async claim(key,now){
    const token=crypto.randomUUID();
    const row=await db.prepare(`INSERT INTO ai_evidence_cache(cache_key,lease_until,lease_token) VALUES(?1,?2,?3)
      ON CONFLICT(cache_key) DO UPDATE SET lease_until=excluded.lease_until,lease_token=excluded.lease_token
      WHERE expires_at<=?4 AND lease_until<=?4 RETURNING lease_token`).bind(key,now+LEASE,token,now).first();
    return row?.lease_token||null;
  },
  async put(key,token,value,expires,now){
    const result=await db.prepare(`UPDATE ai_evidence_cache SET payload=?1,created_at=?2,expires_at=?3,lease_until=0,lease_token=NULL
      WHERE cache_key=?4 AND lease_token=?5 AND lease_until>?2`).bind(JSON.stringify(value),now,expires,key,token).run();
    return result.meta.changes>0;
  },
  async release(key,token){await db.prepare('UPDATE ai_evidence_cache SET lease_until=0,lease_token=NULL WHERE cache_key=?1 AND lease_token=?2').bind(key,token).run();},
  async prune(now){await db.prepare('DELETE FROM ai_evidence_cache WHERE cache_key IN (SELECT cache_key FROM ai_evidence_cache WHERE expires_at<?1 AND lease_until<=?2 LIMIT 50)').bind(now-7*DAY,now).run();}
};}

export async function evidenceKey(item,model){
  const a=item.article;
  let address=a.url;
  try{const url=new URL(address);for(const key of [...url.searchParams.keys()])if(/^(?:utm_|at_medium$|at_campaign$)/i.test(key))url.searchParams.delete(key);url.searchParams.sort();url.hash='';address=url.href;}catch{/* Validated source URLs normally parse. */}
  const text=JSON.stringify([AI_CACHE_VERSION,AVAILABILITY_POLICY,model,a.team,a.sourceType||'news_report',address,a.publishedAt,a.title,a.summary,item.quote]);
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))].map(v=>v.toString(16).padStart(2,'0')).join('');
}

export async function warmEvidenceCache(db,{model,now}){
  const cache=evidenceCache(db);
  const rows=await db.prepare(`SELECT payload FROM research_snapshots WHERE collected_at>=?1 AND collected_at<=?2
    AND json_extract(payload,'$.ai.model')=?3 AND json_extract(payload,'$.ai.status') IN ('complete','partial')
    ORDER BY collected_at DESC LIMIT 4`).bind(new Date(now-10*DAY).toISOString(),new Date(now).toISOString(),model).all();
  let imported=0;
  for(const row of rows.results){
    const packet=JSON.parse(row.payload);
    if(!['research-0.6.0','research-0.7.0'].includes(packet.version))continue;
    const evidence=buildEvidence(packet.articles||[]);
    for(const fact of packet.ai.facts||[]){
      if(fact.extractionMethod!=='ai_selection')continue;
      const item=evidence.find(e=>e.article.url===fact.url&&e.article.team===fact.team&&e.quote===fact.quote);
      if(!item)continue;
      const validated=validateEvidenceChoices({facts:[{evidenceId:item.id,subject:fact.subject}]},[item]);
      if(!validated.facts.length)continue;
      const key=await evidenceKey(item,model);
      if(await cache.get(key,now))continue;
      const selectedAt=fact.aiSelectedAt||packet.collectedAt;
      const publication=Date.parse(item.article.publishedAt),selected=Date.parse(selectedAt);
      const expires=Math.min(now+7*DAY,publication+10*DAY);
      if(!Number.isFinite(expires)||expires<=now||!Number.isFinite(selected)||selected>now)continue;
      const token=await cache.claim(key,now);if(!token)continue;
      try{
        if(await cache.put(key,token,{version:AI_CACHE_VERSION,model,policy:AVAILABILITY_POLICY,useful:true,
          subject:validated.facts[0].subject,selectedAt,origin:'archived_selection',
          originResearchVersion:packet.version,responseFormat:packet.ai.responseFormat||null},expires,now))imported++;
      }finally{await cache.release(key,token);}
    }
  }
  return {snapshots:rows.results.length,imported};
}

export async function useEvidenceCache(evidence,{cache,now,model},query){
  const cached=[],unknown=[],leases=[],stats={enabled:true,hits:0,positiveHits:0,misses:0,busy:0,readFailures:0,writeFailures:0,called:false,evaluated:0};
  for(const item of evidence){
    const key=await evidenceKey(item,model);let decision=null;
    try{decision=await cache.get(key,now());}catch{stats.readFailures++;}
    const valid=decision?.version===AI_CACHE_VERSION&&decision.model===model&&decision.policy===AVAILABILITY_POLICY&&
      typeof decision.useful==='boolean'&&Number.isFinite(Date.parse(decision.selectedAt))&&Date.parse(decision.selectedAt)<=now();
    if(valid){
      const checked=decision.useful?validateEvidenceChoices({facts:[{evidenceId:item.id,subject:decision.subject}]},[item]):null;
      if(!decision.useful||checked.facts.length){
        stats.hits++;
        if(decision.useful){stats.positiveHits++;cached.push({...checked.facts[0],extractionMethod:'ai_selection_cached',aiSelectedAt:decision.selectedAt});}
        continue;
      }
    }
    if(decision)try{await cache.invalidate(key,now());}catch{stats.readFailures++;}
    stats.misses++;unknown.push({item,key});
  }
  const batch=[];
  for(const entry of unknown){
    if(batch.length>=4)break;
    try{
      const token=await cache.claim(entry.key,now());
      if(!token){stats.busy++;continue;}
      leases.push({...entry,token});batch.push(entry.item);
    }catch{stats.readFailures++;batch.push(entry.item);}
  }
  let result={status:stats.busy?'pending':'complete',facts:[],model};
  try{
    if(batch.length){
      result=await query(batch);
      stats.called=!['budget_reached','not_connected'].includes(result.status);stats.evaluated=stats.called?batch.length:0;
      const selectedAt=new Date(now()).toISOString();
      result={...result,facts:result.facts.map(f=>({...f,aiSelectedAt:selectedAt}))};
      // Only a fully validated answer establishes negative decisions. A missing
      // selection in a rejected/partial answer is never cached as a negative.
      if(['complete','partial'].includes(result.status))for(const lease of leases){
        const fact=result.facts.find(f=>f.evidenceId===lease.item.id);
        if(!fact&&result.status!=='complete')continue;
        const publication=Date.parse(lease.item.article.publishedAt),ttl=fact?7*DAY:6*3600000;
        const expires=Math.min(now()+ttl,Number.isFinite(publication)?publication+10*DAY:now()+ttl);
        if(expires<=now())continue;
        try{const saved=await cache.put(lease.key,lease.token,{version:AI_CACHE_VERSION,model,policy:AVAILABILITY_POLICY,
          useful:!!fact,subject:fact?.subject??null,selectedAt,responseFormat:result.responseFormat||null},expires,now());if(!saved)stats.writeFailures++;}
        catch{stats.writeFailures++;}
      }
    }
  }finally{
    for(const lease of leases)try{await cache.release(lease.key,lease.token);}catch{stats.writeFailures++;}
    try{await cache.prune(now());}catch{stats.writeFailures++;}
  }
  const order=new Map(evidence.map((item,index)=>[item.id,index]));
  const facts=[...cached,...result.facts].sort((a,b)=>order.get(a.evidenceId)-order.get(b.evidenceId)).slice(0,4);
  const failed=!['complete','partial'].includes(result.status);
  return {...result,status:failed&&facts.length?'partial':result.status,facts,model,
    evidenceAvailable:evidence.length,evidenceConsidered:stats.hits+stats.evaluated,cache:stats,
    ...(failed&&facts.length?{deferredReason:result.status}:{})};
}
