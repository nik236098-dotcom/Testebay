import {NextResponse} from 'next/server';
import {logout,sessionCookie} from '@/lib/auth';
import {sameOrigin,safe} from '@/lib/server';
export async function POST(request:Request){return safe(async()=>{sameOrigin(request);await logout();const response=NextResponse.json({ok:true},{headers:{'Cache-Control':'no-store'}});response.cookies.set(sessionCookie,'',{httpOnly:true,sameSite:'strict',path:'/',maxAge:0});return response})}
