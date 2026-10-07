import Admin from './admin-client';
import Login from './login';
import {isAdmin,getStoreSettings} from '@/lib/server';
import {defaultStoreSettings} from '@/lib/shop';
export const dynamic='force-dynamic';
export const metadata={title:'Управление магазином',robots:{index:false,follow:false}};
export default async function Page(){let settings=defaultStoreSettings;try{settings=await getStoreSettings()}catch{}if(!await isAdmin())return <Login name={settings.name}/>;return <Admin initialSettings={settings}/>}
