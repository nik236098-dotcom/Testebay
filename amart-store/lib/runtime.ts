import {DatabaseSync, type SQLInputValue} from 'node:sqlite';
import {mkdirSync,readFileSync,readdirSync} from 'node:fs';
import {readFile,writeFile,rename,unlink} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {randomUUID} from 'node:crypto';

const dataPath=()=>resolve(process.env.DATA_DIR||'data');
let connection:DatabaseSync|undefined;
function database(){
 if(connection)return connection;
 mkdirSync(dataPath(),{recursive:true,mode:0o700});
 const d=new DatabaseSync(join(dataPath(),'shop.sqlite'));
 d.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=10000;');
 try{
  d.exec('BEGIN IMMEDIATE; CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY);');
  const folder=resolve('drizzle');
  for(const name of readdirSync(folder).filter(n=>n.endsWith('.sql')).sort()){
   if(d.prepare('SELECT name FROM _migrations WHERE name=?').get(name))continue;
   d.exec(readFileSync(join(folder,name),'utf8'));
   d.prepare('INSERT INTO _migrations(name) VALUES(?)').run(name);
  }
  d.exec('CREATE TABLE IF NOT EXISTS _sessions (hash TEXT PRIMARY KEY, expires INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS _login_attempts (ip TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset INTEGER NOT NULL); COMMIT;');
 }catch(error){try{d.exec('ROLLBACK')}finally{d.close()}throw error}
 connection=d;return d;
}
class Statement{
 constructor(private sql:string,private values:SQLInputValue[]=[]){ }
 bind(...values:SQLInputValue[]){return new Statement(this.sql,values)}
 async first<T=Record<string,unknown>>():Promise<T|null>{return (database().prepare(this.sql).get(...this.values) as T|undefined)??null}
 async all<T=Record<string,unknown>>(){return {results:database().prepare(this.sql).all(...this.values) as T[]}}
 runSync(){const result=database().prepare(this.sql).run(...this.values);return {success:true,meta:{changes:Number(result.changes),last_row_id:Number(result.lastInsertRowid)}}}
 async run(){return this.runSync()}
}
const DB={
 prepare(sql:string){return new Statement(sql)},
 async batch(statements:Statement[]){const d=database();d.exec('BEGIN IMMEDIATE');try{const result=statements.map(s=>s.runSync());d.exec('COMMIT');return result}catch(error){d.exec('ROLLBACK');throw error}}
};
function imagePath(key:string){if(!/^[a-f0-9-]+\.(jpg|png|webp)$/.test(key))throw new Error('Invalid image key');const folder=join(dataPath(),'uploads');mkdirSync(folder,{recursive:true,mode:0o700});return join(folder,key)}
const BUCKET={
 async put(key:string,data:ArrayBuffer,_options?:unknown){const path=imagePath(key),temporary=path+'.'+randomUUID()+'.tmp';try{await writeFile(temporary,new Uint8Array(data),{mode:0o600});await rename(temporary,path)}finally{await unlink(temporary).catch(()=>{})}},
 async get(key:string){try{const data=await readFile(imagePath(key));return {body:new Uint8Array(data),httpMetadata:{contentType:key.endsWith('.png')?'image/png':key.endsWith('.webp')?'image/webp':'image/jpeg'}}}catch(error){if((error as NodeJS.ErrnoException).code==='ENOENT')return null;throw error}},
 async delete(key:string){await unlink(imagePath(key)).catch(error=>{if(error.code!=='ENOENT')throw error})}
};
export const env={DB,BUCKET,get TELEGRAM_CONFIG_KEY(){return process.env.TELEGRAM_CONFIG_KEY||''},get TELEGRAM_BOT_TOKEN(){return process.env.TELEGRAM_BOT_TOKEN||''},get TELEGRAM_CHAT_ID(){return process.env.TELEGRAM_CHAT_ID||''}};
