import {DatabaseSync,backup} from 'node:sqlite';
import {mkdir,cp} from 'node:fs/promises';
import {join,resolve} from 'node:path';
const root=resolve(process.env.DATA_DIR||'data');
const target=join(root,'backups',new Date().toISOString().replace(/[:.]/g,'-'));
await mkdir(target,{recursive:true,mode:0o700});
const db=new DatabaseSync(join(root,'shop.sqlite'),{readOnly:true});
try{await backup(db,join(target,'shop.sqlite'))}finally{db.close()}
await cp(join(root,'uploads'),join(target,'uploads'),{recursive:true,filter:path=>!path.endsWith('.tmp')}).catch(error=>{if(error.code!=='ENOENT')throw error});
console.log(target);
