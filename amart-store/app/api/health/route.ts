import {env} from '@/lib/runtime';
export const dynamic='force-dynamic';
export async function GET(){try{await env.DB.prepare('SELECT 1').first();return Response.json({ok:true},{headers:{'Cache-Control':'no-store'}})}catch{return Response.json({ok:false},{status:503})}}
