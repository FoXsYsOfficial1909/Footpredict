import {DatabaseSync} from 'node:sqlite';

// Execute real SQLite statements with D1's async interface. No remote database.
export function memoryD1() {
  const sqlite=new DatabaseSync(':memory:');
  const wrap=(sql,values=[])=>({
    bind:(...args)=>wrap(sql,args),
    first:async()=>sqlite.prepare(sql).get(...values)||null,
    all:async()=>({results:sqlite.prepare(sql).all(...values),success:true}),
    run:async()=>{const result=sqlite.prepare(sql).run(...values);return {success:true,meta:{changes:Number(result.changes)}};}
  });
  return {prepare:sql=>wrap(sql),withSession(){return this;},
    async batch(statements){
      sqlite.exec('BEGIN');
      try{const results=[];for(const statement of statements)results.push(await statement.run());sqlite.exec('COMMIT');return results;}
      catch(error){sqlite.exec('ROLLBACK');throw error;}
    },sqlite,close:()=>sqlite.close()};
}
