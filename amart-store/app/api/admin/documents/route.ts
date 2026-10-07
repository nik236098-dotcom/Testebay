import {requireAdmin,safe,response} from '@/lib/server';
import {getLegalDocuments} from '@/lib/legal-server';
export async function GET(){return safe(async()=>{await requireAdmin();return response({documents:await getLegalDocuments()})})}
