import {requireAdmin,sameOrigin,jsonBody,response,safe} from '@/lib/server';
import {getReviews,saveReviews} from '@/lib/reviews';
export async function GET(){return safe(async()=>{await requireAdmin();return response({reviews:await getReviews(true)})})}
export async function PUT(r:Request){return safe(async()=>{await requireAdmin();sameOrigin(r);return response({reviews:await saveReviews(await jsonBody(r,100000))})})}
