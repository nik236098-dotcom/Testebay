import type {Metadata} from 'next';
import {Toaster} from '@/components/ui/sonner';
import {getStoreSettings} from '@/lib/server';
import {defaultStoreSettings} from '@/lib/shop';
import './globals.css';
import './redesign.css';
import './reviews.css';
import './documents.css';
export const dynamic='force-dynamic';
export async function generateMetadata():Promise<Metadata>{let settings=defaultStoreSettings;try{settings=await getStoreSettings()}catch{}return {title:settings.name+' — интернет-магазин',description:settings.catalogDescription.replace(/\s+/g,' ').slice(0,160)||'Каталог товаров. Выберите товары и оформите заказ без регистрации.',icons:{icon:'/favicon.svg'}}}
export default function Layout({children}:{children:React.ReactNode}){return <html lang="ru"><body>{children}<Toaster position="bottom-center" richColors/></body></html>}
