import {getReviews} from '@/lib/reviews';
import {getProducts,getCategories,getBanner,getStoreSettings,response,safe} from '@/lib/server';
export const dynamic='force-dynamic';
export async function GET(){return safe(async()=>response({products:await getProducts(),categories:await getCategories(),banner:await getBanner(),settings:await getStoreSettings(),reviews:await getReviews()}));}
