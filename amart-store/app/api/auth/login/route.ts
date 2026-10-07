import {NextResponse} from 'next/server';
import {login,clientIp,configuredOrigin,sessionCookie,sessionSeconds} from '@/lib/auth';
import {sameOrigin,jsonBody,safe,HttpError} from '@/lib/server';
export async function POST(request:Request){return safe(async()=>{
 sameOrigin(request);const body=await jsonBody(request,4096);
 if(typeof body.password!=='string'||body.password.length>512)throw new HttpError(400,'Введите пароль.');
 const result=await login(body.password,clientIp(request));
 if('error' in result)throw new HttpError(result.status,result.error!);
 const response=NextResponse.json({ok:true},{headers:{'Cache-Control':'no-store'}});
 response.cookies.set(sessionCookie,result.token!,{httpOnly:true,secure:configuredOrigin(request).startsWith('https:'),sameSite:'strict',path:'/',maxAge:sessionSeconds});return response;
})}
