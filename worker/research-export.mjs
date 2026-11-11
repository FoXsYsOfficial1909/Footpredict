// Bounded, read-only archive used by scheduled scientific evaluation. No provider calls.
export const TRAINING_EXPORT_VERSION='availability-training-export-1';
const LIMIT=4;
const invalid=()=>Object.assign(new Error('Некорректный курсор архива.'),{status:400});
function decode(value){
  if(value==null)return 0;
  if(!/^[1-9]\d{0,14}$/.test(value))throw invalid();
  return Number(value);
}
async function checksum(value){
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)))].map(n=>n.toString(16).padStart(2,'0')).join('');
}
export async function researchTrainingPage(env,after){
  const cursor=decode(after);
  if(!env.FOOTPREDICT_DB)throw Object.assign(new Error('Архив недоступен.'),{status:503});
  const db=env.FOOTPREDICT_DB.withSession?env.FOOTPREDICT_DB.withSession('first-primary'):env.FOOTPREDICT_DB;
  // Insertion order also captures a delayed insert with an earlier collection time.
  const found=await db.prepare(`SELECT rowid AS seq,id,payload FROM research_snapshots
    WHERE rowid>?1 ORDER BY rowid LIMIT ${LIMIT+1}`).bind(cursor).all();
  const rows=(found.results||[]).slice(0,LIMIT);
  const snapshots=await Promise.all(rows.map(async row=>({id:row.id,sha256:await checksum(row.payload),payload:row.payload})));
  const last=rows.at(-1),current=last?String(last.seq):after||null;
  return {schemaVersion:1,protocol:TRAINING_EXPORT_VERSION,snapshots,cursor:current,
    nextCursor:found.results.length>LIMIT?current:null,
    note:'Immutable original research archives, not assertions of medical fitness. GET does not collect or retrain.'};
}
