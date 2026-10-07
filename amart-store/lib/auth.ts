import {cookies} from 'next/headers';
import {createHash,randomBytes,timingSafeEqual} from 'node:crypto';
import {env} from './runtime';
export const sessionCookie='shop_session';
export const sessionSeconds=60*60*12;
const hash=(value:string)=>createHash('sha256').update(value).digest('hex');
export function clientIp(request:Request){return process.env.TRUST_PROXY==='1'?(request.headers.get('x-real-ip')||'unknown'):'local'}
export function configuredOrigin(request:Request){return process.env.PUBLIC_ORIGIN?new URL(process.env.PUBLIC_ORIGIN).origin:new URL(request.url).origin}
export async function authenticated(){const token=(await cookies()).get(sessionCookie)?.value;if(!token||!/^[a-f0-9]{64}$/.test(token))return false;return !!await env.DB.prepare('SELECT hash FROM _sessions WHERE hash=? AND expires>?').bind(hash(token),Date.now()).first()}
export async function login(password:string,ip:string){
 const now=Date.now();
 await env.DB.prepare('DELETE FROM _sessions WHERE expires<=?').bind(now).run();
 await env.DB.prepare('DELETE FROM _login_attempts WHERE reset<=?').bind(now).run();
 const attempts=await env.DB.prepare('SELECT attempts FROM _login_attempts WHERE ip=?').bind(ip).first<{attempts:number}>();
 if(attempts&&attempts.attempts>=10)return {error:'Слишком много попыток. Попробуйте через 15 минут.',status:429} as const;
 const expected=process.env.ADMIN_PASSWORD||'';
 if(expected.length<12)return {error:'Вход ещё не настроен на сервере.',status:503} as const;
 if(!timingSafeEqual(Buffer.from(hash(password)),Buffer.from(hash(expected)))){
  await env.DB.prepare('INSERT INTO _login_attempts(ip,attempts,reset) VALUES(?,1,?) ON CONFLICT(ip) DO UPDATE SET attempts=attempts+1').bind(ip,now+15*60*1000).run();
  return {error:'Неверный пароль.',status:401} as const;
 }
 const token=randomBytes(32).toString('hex');
 await env.DB.batch([env.DB.prepare('DELETE FROM _login_attempts WHERE ip=?').bind(ip),env.DB.prepare('INSERT INTO _sessions(hash,expires) VALUES(?,?)').bind(hash(token),now+sessionSeconds*1000)]);
 return {token,status:200} as const;
}
export async function logout(){const token=(await cookies()).get(sessionCookie)?.value;if(token)await env.DB.prepare('DELETE FROM _sessions WHERE hash=?').bind(hash(token)).run()}
