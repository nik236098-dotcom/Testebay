import type {ReviewSettings} from './shop';

// Owner-supplied finished review images, kept unchanged and shown in full.
// Text is an accessible image description only; no captions are shown below.
export const customerReviews:ReviewSettings={
 title:'Отзывы покупателей',enabled:true,version:'amart_supplied_review_cards_20261007_v1',items:[
  {id:'e2b1d6a4-619c-4fee-a23f-9183b465cf11',author:'',date:'',image:'/images/customer-reviews/review-9049.jpg',visible:true,text:'Такой приятный аромат исходит из свечи!'},
  {id:'e2b1d6a4-619c-4fee-a23f-9183b465cf12',author:'',date:'',image:'/images/customer-reviews/review-9050.jpg',visible:true,text:'Спасибо невестке за такой замечательный подарок! Свеча ручной работы с ароматом груши в бренди — это восхитительное сочетание, которое наполняет воздух неповторимым шармом и роскошью.'},
  {id:'e2b1d6a4-619c-4fee-a23f-9183b465cf13',author:'',date:'',image:'/images/customer-reviews/review-9051.jpg',visible:true,text:'Мои родные девочки знают, как меня порадовать! Тот самый поцелуй. Остановись мгновение!'},
  {id:'e2b1d6a4-619c-4fee-a23f-9183b465cf14',author:'',date:'',image:'/images/customer-reviews/review-9052.jpg',visible:true,text:'Хотела поблагодарить за арома-саше. Повесила в шкаф — аромат обалденный. Просто бомба! Свечка как отдельное искусство. Очень нежный и весенний цыплёнок. Спасибо большое.'},
  {id:'e2b1d6a4-619c-4fee-a23f-9183b465cf15',author:'',date:'',image:'/images/customer-reviews/review-9053.jpg',visible:true,text:'Бабуля со стороны папы: мне такие подарки никогда не дарили. Сразу на стол поставила.'},
  {id:'e2b1d6a4-619c-4fee-a23f-9183b465cf16',author:'',date:'',image:'/images/customer-reviews/review-9054.jpg',visible:true,text:'Рада, что заказала себе ароматическую свечу в виде пасхального кулича! Спасибо за такую душевную работу.'}
 ]
};
export const customerReviewImages=new Set(customerReviews.items.map(r=>r.image));
