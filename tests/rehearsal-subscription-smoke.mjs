import fs from 'node:fs/promises';
import {spawn,execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
const root=process.env.SMOKE_ROOT || '/work';
const bin=process.env.PG_BIN || '/usr/lib/postgresql/18/bin/';
await fs.mkdir(root+'/socket',{recursive:true});
const env={...process.env,PGHOST:root+'/socket',PGPORT:'15439',PGUSER:process.env.SMOKE_USER || 'nobody'};
const run=(cmd,args)=>execFileSync(bin+cmd,args,{env,encoding:'utf8',stdio:['ignore','pipe','pipe']});
run('initdb',['-D',root+'/pg','-A','trust','--no-locale']);
const server=spawn(bin+'postgres',['-D',root+'/pg','-k',root+'/socket','-p','15439','-c','listen_addresses=','-c','max_logical_replication_workers=0'],{env,stdio:'ignore'});
try{
 for(let i=0;i<100;i++){try{run('pg_isready',['-q']);break}catch{await new Promise(r=>setTimeout(r,100));}}
 const sql=(db,s)=>run('psql',['-X','-At','-v','ON_ERROR_STOP=1','-d',db,'-c',s]);
 const script=(db,apply)=>run('psql',['-X','-At','-v','ON_ERROR_STOP=1','-v','apply='+apply,'-d',db,'-f',root+'/pause-obsolete-rehearsal-subscription.sql']);
 sql('postgres','CREATE DATABASE nutsnews_primary_shadow');sql('postgres','CREATE DATABASE nutsnews_restore_rehearsal');
 sql('nutsnews_primary_shadow','CREATE TABLE preserved(id int primary key); INSERT INTO preserved VALUES(42)');
 sql('nutsnews_restore_rehearsal',`CREATE TABLE preserved(id int primary key); INSERT INTO preserved VALUES(7); CREATE SUBSCRIPTION nutsnews_backend_migration_sub CONNECTION 'host=127.0.0.1 port=1 dbname=unused' PUBLICATION nutsnews_backend_migration_pub WITH (connect=false,slot_name='nutsnews_backend_migration_slot'); ALTER SUBSCRIPTION nutsnews_backend_migration_sub ENABLE`);
 const state=()=>sql('postgres','SELECT subenabled FROM pg_subscription WHERE subname=\'nutsnews_backend_migration_sub\'').trim();
 assert.equal(state(),'t'); script('nutsnews_restore_rehearsal','false');assert.equal(state(),'t');
 assert.throws(()=>script('nutsnews_primary_shadow','true'));assert.equal(state(),'t');
 const blocker=spawn(bin+'psql',['-X','-d','nutsnews_restore_rehearsal','-c','SELECT pg_sleep(15)'],{env,stdio:'ignore'});await new Promise(r=>setTimeout(r,300));
 assert.throws(()=>script('nutsnews_restore_rehearsal','true'));assert.equal(state(),'t');blocker.kill('SIGTERM');await new Promise(r=>blocker.on('exit',r));
 // Killing psql may leave its server query alive until socket detection; cancel
 // this isolated test session explicitly before testing the permitted operation.
 sql('postgres',"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='nutsnews_restore_rehearsal'");
 for(let i=0;i<50;i++){
  if(sql('postgres',"SELECT count(*) FROM pg_stat_activity WHERE datname='nutsnews_restore_rehearsal'").trim()==='0')break;
  await new Promise(r=>setTimeout(r,100));
 }
 assert.equal(sql('postgres',"SELECT count(*) FROM pg_stat_activity WHERE datname='nutsnews_restore_rehearsal'").trim(),'0');
 script('nutsnews_restore_rehearsal','true');assert.equal(state(),'f');script('nutsnews_restore_rehearsal','true');assert.equal(state(),'f');
 assert.equal(sql('nutsnews_primary_shadow','TABLE preserved').trim(),'42');assert.equal(sql('nutsnews_restore_rehearsal','TABLE preserved').trim(),'7');
 sql('nutsnews_restore_rehearsal',"ALTER SUBSCRIPTION nutsnews_backend_migration_sub SET (slot_name='unexpected_slot')");
 assert.throws(()=>script('nutsnews_restore_rehearsal','true'));
 await fs.writeFile(root+'/result.json',JSON.stringify({pass:true,tests:['read-only preflight','production target rejected','active rehearsal client rejected','disable','idempotent apply','production and rehearsal rows preserved','unexpected slot rejected'],isolated:true}));
 console.log('Seven isolated PostgreSQL safety checks passed');
}finally{server.kill('SIGTERM');await new Promise(r=>server.on('exit',r));}
