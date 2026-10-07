import {spawn} from 'node:child_process';
import {mkdtemp,rm,readFile,readdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {createServer} from 'node:net';
import assert from 'node:assert/strict';
const temp=await mkdtemp(join(tmpdir(),'amart-check-'));
const socket=createServer();await new Promise(r=>socket.listen(0,'127.0.0.1',r));const port=socket.address().port;await new Promise(r=>socket.close(r));
const base=`http://127.0.0.1:${port}`,password='local-test-password-only-123',data=join(temp,'data');
let child,logs='',cookie='',checks=0;
function ok(value,label){assert.ok(value,label);checks++;console.log('PASS '+label)}
async function req(path,{method='GET',body,auth=false,headers={},origin=base}={}){const h={...headers};if(auth)h.cookie=cookie;if(method!=='GET')h.origin=origin;if(body&&!(body instanceof FormData)){h['Content-Type']='application/json';body=JSON.stringify(body)}const r=await fetch(base+path,{method,headers:h,body});const raw=await r.text();let data;try{data=JSON.parse(raw)}catch{data=raw}return {status:r.status,data,headers:r.headers}}
async function start(){logs='';child=spawn(process.execPath,['.next/standalone/server.js'],{env:{...process.env,NODE_ENV:'production',PORT:String(port),HOSTNAME:'127.0.0.1',NEXT_TELEMETRY_DISABLED:'1',DATA_DIR:data,ADMIN_PASSWORD:password,PUBLIC_ORIGIN:base,TRUST_PROXY:'0',TELEGRAM_CONFIG_KEY:'ab'.repeat(32),TELEGRAM_BOT_TOKEN:'',TELEGRAM_CHAT_ID:''},stdio:['ignore','pipe','pipe']});for(const stream of [child.stdout,child.stderr])stream.on('data',d=>logs+=d);for(let i=0;i<100;i++){if(child.exitCode!==null)throw new Error(logs);try{const r=await req('/api/health');if(r.status===200)return}catch{}await new Promise(r=>setTimeout(r,100))}throw new Error('Server did not start: '+logs)}
async function stop(){if(child&&child.exitCode===null){const exited=new Promise(r=>child.once('exit',r));child.kill('SIGTERM');await exited}}
try{
 await start();
 let r=await req('/');ok(r.status===200&&r.data.includes('/images/customer-reviews/review-9049.jpg'),'storefront and owner-supplied review cards render on Node');
 r=await req('/admin');ok(r.status===200&&r.data.includes('Пароль')&&!r.data.includes('Войти через ChatGPT'),'admin has independent password login');
 r=await req('/api/admin/products',{headers:{'oai-authenticated-user-id':'fake','oai-authenticated-user-email':'fake@example.com'}});ok(r.status===403,'forged hosting headers cannot access admin');
 r=await req('/api/auth/login',{method:'POST',body:{password},origin:'https://other.test'});ok(r.status===403,'login checks request origin');
 r=await req('/api/auth/login',{method:'POST',body:{password:'wrong'}});ok(r.status===401,'wrong password is rejected');
 r=await req('/api/auth/login',{method:'POST',body:{password}});ok(r.status===200,'owner logs in');
 const setCookie=r.headers.get('set-cookie');ok(/httponly/i.test(setCookie)&&/samesite=strict/i.test(setCookie),'session cookie is HttpOnly and SameSite Strict');cookie=setCookie.split(';')[0];
 r=await req('/api/admin/products',{auth:true});ok(r.status===200&&r.data.products.length===6,'admin reads current catalog');const originalBanner=r.data.banner,transferCategory=r.data.categories[0].id;
 r=await req('/api/admin/categories',{method:'POST',auth:true,body:{name:'Проверка переноса'}});ok(r.status===201,'new category saves to SQLite');const category=r.data.category;
 const imageData=await readFile('public/images/car.png');const form=new FormData();form.append('file',new File([imageData],'test.png',{type:'image/png'}));
 r=await req('/api/admin/upload',{method:'POST',auth:true,body:form});ok(r.status===201,'product photo uploads to server disk');const image=r.data.url;
 const product={name:'Проверка сервера',description:'Тест сохранения',category:category.id,volume:'M',price:12300,images:[image],visible:true,available:true};
 r=await req('/api/admin/products',{method:'POST',auth:true,body:product});ok(r.status===201,'product saves to SQLite');const productId=r.data.id;
 r=await req('/api/products');ok(r.data.products.some(p=>p.id===productId),'new product is publicly visible');
 r=await req('/api/admin/banner',{method:'PUT',auth:true,body:{...originalBanner,title:'Баннер сервера',image}});ok(r.status===200,'banner text and uploaded image save');
 r=await req('/api/admin/banner',{method:'PUT',auth:true,body:originalBanner});ok(r.status===409,'stale banner writes cannot overwrite newer settings');
 r=await req('/api/admin/reviews',{auth:true});ok(r.status===200&&r.data.reviews.items.length===6,'all six cards are editable');
 r=await req('/api/admin/reviews',{method:'PUT',auth:true,body:{...r.data.reviews,items:[...r.data.reviews.items].reverse()}});ok(r.status===200,'owner-supplied image cards reorder and save');
 r=await req('/api/admin/documents/offer',{auth:true});const doc=r.data.document;
 r=await req('/api/admin/documents/offer',{method:'PUT',auth:true,body:{...doc,title:'Оферта на сервере'}});ok(r.status===200,'legal document edits save');
 const order={id:crypto.randomUUID(),name:'Тестовый покупатель',phone:'+79991234567',contactMethod:'whatsapp',contact:'+79991234567',comment:'Проверка',consent:true,transferConsent:true,offerConsent:true,delivery:{method:'pickup',city:'',address:'',productionConsent:true},items:[{id:productId,quantity:2}],total:24600};
 r=await req('/api/orders',{method:'POST',body:order});ok(r.status===201,'checkout stores an order');
 r=await req('/api/orders',{method:'POST',body:order});ok(r.status===200,'repeated order is not duplicated');
 r=await req('/api/admin/orders',{auth:true});ok(r.data.orders.length===1&&r.data.orders[0].total===24600,'admin reads the order and calculated total');
 r=await req('/api/admin/categories/'+category.id,{method:'DELETE',auth:true,body:{updatedAt:category.updatedAt,moveTo:transferCategory}});ok(r.status===200&&r.data.moved===1,'category deletion transfers products in a SQLite transaction');
 await stop();await start();
 r=await req('/api/admin/products',{auth:true});ok(r.status===200&&r.data.products.find(p=>p.id===productId)?.category===transferCategory,'products and admin session survive restart');
 r=await req('/api/admin/orders',{auth:true});ok(r.data.orders.length===1,'orders survive restart');
 r=await req('/');ok(r.data.includes('Баннер сервера'),'banner survives restart and import does not reset changes');
 const imageResponse=await fetch(base+image);ok(imageResponse.status===200&&Buffer.compare(Buffer.from(await imageResponse.arrayBuffer()),imageData)===0,'uploaded image survives restart byte-for-byte');
 r=await req('/documents/offer');ok(r.data.includes('Оферта на сервере'),'legal documents survive restart');
 r=await req('/api/auth/logout',{method:'POST',auth:true});ok(r.status===200,'logout succeeds');
 r=await req('/api/admin/products',{auth:true});ok(r.status===403,'logged-out session cannot be reused');
 for(let i=0;i<10;i++)await req('/api/auth/login',{method:'POST',body:{password:'wrong'}});
 r=await req('/api/auth/login',{method:'POST',body:{password}});ok(r.status===429,'login brute-force limit is enforced');
 await stop();
 const backup=spawn(process.execPath,['scripts/backup.mjs'],{env:{...process.env,DATA_DIR:data},stdio:['ignore','pipe','pipe']});let backupLog='';backup.stdout.on('data',d=>backupLog+=d);const exit=await new Promise(r=>backup.on('exit',r));ok(exit===0&&backupLog.includes('/backups/'),'consistent SQLite and photo backup completes');
 console.log(`${checks} server checks passed.`);
}catch(error){console.error(logs);throw error}finally{await stop();await rm(temp,{recursive:true,force:true})}
