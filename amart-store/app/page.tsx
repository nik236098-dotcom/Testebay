import {getReviews} from '@/lib/reviews';
import Shop from './shop-client';
import {getBanner,getStoreSettings,getProducts,getCategories} from '@/lib/server';
import {defaultBanner,defaultStoreSettings,defaultReviewSettings,Product,Category} from '@/lib/shop';
export const dynamic='force-dynamic';
export default async function Page(){let banner=defaultBanner,settings=defaultStoreSettings,reviews=defaultReviewSettings,products:Product[]=[],categories:Category[]=[];try{[banner,settings,reviews,products,categories]=await Promise.all([getBanner(),getStoreSettings(),getReviews(),getProducts(),getCategories()])}catch{}return <Shop initialBanner={banner} initialSettings={settings} initialReviews={reviews} initialProducts={products} initialCategories={categories}/>}
