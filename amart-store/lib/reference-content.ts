import {env} from './runtime';
import {defaultStoreSettings,type Banner,type ReviewSettings} from './shop';
import catalog from './reference-products.json';

// A single, requested content import. Subsequent admin changes are never reset.
const marker='kristal_content_20261006';
export const referenceImages=new Set(catalog.products.flatMap(p=>p.images));
export const referenceBannerImage='/images/kristal/hero.jpg';
const settings={...defaultStoreSettings,name:'Кристал',catalogEyebrow:'',catalogTitle:'Товары',catalogDescription:'',aboutEyebrow:'СОТРУДНИЧЕСТВО',aboutTitle:'Красивые подарки для вашего бизнеса',aboutDescription:'Приглашаем к сотрудничеству цветочные магазины, бьюти-студии и свадебные агентства. Создаём подарки для гостей, команд и особенных событий. Принимаем оптовые и корпоративные заказы.',deliveryEyebrow:'',deliveryTitle:'Варианты доставки',deliveryDescription:'',deliveryPickupTitle:'Самовывоз',deliveryPickupPrice:'Бесплатно',deliveryPickupText:'Челябинск, Советский район, АМЗ.\nВремя и точный адрес согласуем при подтверждении заказа.',deliveryCourierTitle:'Доставка курьером',deliveryCourierPrice:'От 300 ₽',deliveryCourierText:'По Челябинску — курьером Яндекс Go. Итоговая стоимость зависит от адреса и тарифа сервиса.',deliveryShippingTitle:'Почта и СДЭК',deliveryShippingPrice:'От 300 ₽',deliveryShippingText:'Отправляем в другие города Почтой России и СДЭК. Стоимость рассчитаем по адресу и параметрам посылки.',productionText:'Изготовление заказа — 4 недели, без учёта доставки.',footerTagline:'По всем вопросам обращайтесь:',footerNote:'Кристал · Свечи и декор ручной работы',contactPerson:'Кучерюк Кристина Александровна',contactTaxId:'744810010457',contactPhone:'+7 908 046-19-90',contactAddress:'Челябинск, Советский район, АМЗ',contactTelegram:'https://t.me/kristal_inspire',contactPersonalTelegram:'https://t.me/Kffed',contactVk:'https://vk.com/kristal_inspire',contactInstagram:'https://instagram.com/kristal_inspire',contactWhatsApp:'https://wa.me/79080461990',contactMax:'',contactEmail:'',legalOffer:'https://kristal.inspire.tilda.ws/page2',legalPrivacy:'https://kristal.inspire.tilda.ws/page70613981.html',legalDataConsent:'https://kristal.inspire.tilda.ws/page70614207.html',legalTransferConsent:'https://kristal.inspire.tilda.ws/page70614397.html',orderSuccessTitle:'Спасибо за вашу заявку!',orderSuccessMessage:'Свяжемся с вами выбранным способом, уточним детали, стоимость и доставку.\nОплата — после личного подтверждения заказа.',version:marker};
const banner:Banner={eyebrow:'Кристал',title:'Интерьерные свечи и декор ручной работы\nдля уютных моментов дома.',description:'',buttonText:'Заказать свечи · изготовление 4 недели',buttonLink:'#catalog',image:referenceBannerImage,imageAlt:'Свечи и интерьерный декор Кристал на уютном столе',enabled:true,version:marker};
const reviews:ReviewSettings={title:'Тёплые слова',enabled:true,version:marker,items:[
 {id:'afd53789-32bb-4034-a4d3-0a6348b74ac1',author:'О доме',text:'Уют начинается с мелочей. С мягкого света, любимой чашки и ощущения, что прямо сейчас можно никуда не спешить.',image:'',date:'Маленькие ритуалы',visible:true},
 {id:'afd53789-32bb-4034-a4d3-0a6348b74ac2',author:'О подарках',text:'Самые тёплые подарки говорят без слов: «Я подумал о тебе». Именно это чувство хочется бережно завернуть и передать дальше.',image:'',date:'С заботой о близких',visible:true},
 {id:'afd53789-32bb-4034-a4d3-0a6348b74ac3',author:'О красоте',text:'Красивым вещам не нужен особый повод. Пусть обычный вечер станет тем самым моментом, для которого вы их берегли.',image:'',date:'Вдохновение каждый день',visible:true},
 {id:'afd53789-32bb-4034-a4d3-0a6348b74ac4',author:'О тепле',text:'Иногда для хорошего вечера достаточно зажечь свечу, поставить чайник и позволить дому обнять вас тишиной.',image:'',date:'Время для себя',visible:true},
 {id:'afd53789-32bb-4034-a4d3-0a6348b74ac5',author:'О внимании',text:'Ручная работа хранит маленькие несовершенства и большое внимание. В них — характер вещи, которую хочется оставить рядом.',image:'',date:'Красота в деталях',visible:true}
]};
export async function ensureReferenceContent(){
 const d=env.DB;if(!d)throw new Error('Store database unavailable');
 if(await d.prepare('SELECT key FROM settings WHERE key=?').bind(marker).first())return;
 const condition=`NOT EXISTS (SELECT 1 FROM settings WHERE key='${marker}')`;
 const existingCategories=await d.prepare('SELECT id,name_key FROM categories').all<{id:string;name_key:string}>();
 const categoryIds=new Map(catalog.categories.map(c=>[c.id,existingCategories.results.find(x=>x.name_key===c.name.normalize('NFKC').trim().replace(/\s+/g,' ').toLocaleLowerCase('ru-RU'))?.id||c.id]));
 const now=new Date().toISOString();
 const statements=[
  // Backup the presentation being replaced, never touch credentials or orders.
  d.prepare(`INSERT OR IGNORE INTO settings (key,value) SELECT 'before_kristal_content',json_object('settings',(SELECT json_group_array(json_object('key',key,'value',value)) FROM settings WHERE key IN ('store_config','banner_config','reviews_config')),'products',(SELECT json_group_array(json_object('id',id,'name',name,'description',description,'category',category,'volume',volume,'price',price,'images',images,'visible',visible,'available',available,'created_at',created_at,'updated_at',updated_at)) FROM products)) WHERE ${condition}`),
  d.prepare(`UPDATE products SET visible=0,updated_at=? WHERE ${condition}`).bind(now),
  ...catalog.categories.map((c,i)=>d.prepare(`INSERT OR IGNORE INTO categories (id,name,name_key,position,updated_at) SELECT ?,?,?,?,? WHERE ${condition}`).bind(c.id,c.name,c.name.normalize('NFKC').trim().replace(/\s+/g,' ').toLocaleLowerCase('ru-RU'),i+10,now)),
  ...catalog.products.map((p,i)=>d.prepare(`INSERT OR IGNORE INTO products (id,name,description,category,volume,price,images,visible,available,created_at,updated_at) SELECT ?,?,?,?,?,?,?,?,?,?,? WHERE ${condition}`).bind(p.id,p.name,p.description,categoryIds.get(p.category)||p.category,p.volume,p.price,JSON.stringify(p.images),1,1,new Date(Date.now()+i).toISOString(),now)),
  ...[['store_config',settings],['banner_config',banner],['reviews_config',reviews]].map(([key,value])=>d.prepare(`INSERT INTO settings (key,value) SELECT ?,? WHERE ${condition} ON CONFLICT(key) DO UPDATE SET value=excluded.value`).bind(String(key),JSON.stringify(value))),
  d.prepare("INSERT OR IGNORE INTO settings (key,value) VALUES ('seeded','1')"),
  d.prepare("INSERT OR IGNORE INTO settings (key,value) VALUES ('categories_seeded','1')"),
  d.prepare('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)').bind(marker,now)
 ];
 await d.batch(statements);
}
