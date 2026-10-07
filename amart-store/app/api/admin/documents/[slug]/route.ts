import {requireAdmin,sameOrigin,jsonBody,safe,response} from '@/lib/server';
import {getLegalDocument,saveLegalDocument} from '@/lib/legal-server';
export async function GET(_request:Request,{params}:{params:Promise<{slug:string}>}){return safe(async()=>{await requireAdmin();return response({document:await getLegalDocument((await params).slug)})})}
export async function PUT(request:Request,{params}:{params:Promise<{slug:string}>}){return safe(async()=>{await requireAdmin();sameOrigin(request);return response({document:await saveLegalDocument((await params).slug,await jsonBody(request,180000))})})}
