import Checkout from './checkout-client';
import {getProducts,getStoreSettings} from '@/lib/server';
import {defaultStoreSettings,type Product} from '@/lib/shop';
export const dynamic='force-dynamic';
export default async function CheckoutPage(){
 let products:Product[]=[],settings=defaultStoreSettings;
 try{[products,settings]=await Promise.all([getProducts(),getStoreSettings()])}catch{}
 return <Checkout initialProducts={products} initialSettings={settings}/>;
}
