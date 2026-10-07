import {db,requireAdmin,response,safe} from '@/lib/server';
import {telegramReady} from '@/lib/telegram';
export async function GET(){return safe(async()=>{await requireAdmin();const r=await db().prepare('SELECT id,name,phone,contact,contact_method,comment,delivery,items,total,status,notified,created_at FROM orders ORDER BY created_at DESC LIMIT 200').all();return response({orders:r.results.map(x=>({...x,items:JSON.parse(String(x.items)),delivery:JSON.parse(String(x.delivery||"{}"))})),telegramReady:await telegramReady()});});}
