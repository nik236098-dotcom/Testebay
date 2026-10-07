import {requireAdmin,sameOrigin,jsonBody,response,safe} from '@/lib/server';
import {getTelegramSettings,saveTelegramSettings,testTelegramSettings} from '@/lib/telegram';
export async function GET(){return safe(async()=>{await requireAdmin();return response({telegram:await getTelegramSettings()})})}
export async function PUT(r:Request){return safe(async()=>{await requireAdmin();sameOrigin(r);return response({telegram:await saveTelegramSettings(await jsonBody(r))})})}
export async function POST(r:Request){return safe(async()=>{await requireAdmin();sameOrigin(r);const v=await jsonBody(r);await testTelegramSettings(v.version);return response({ok:true})})}
