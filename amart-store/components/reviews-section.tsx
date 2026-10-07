'use client';
import {useEffect,useState} from 'react';
import {ChevronLeft,ChevronRight,MessageSquareQuote} from 'lucide-react';
import {Carousel,CarouselApi,CarouselContent,CarouselItem} from '@/components/ui/carousel';
import type {ReviewSettings} from '@/lib/shop';

export default function ReviewsSection({reviews}:{reviews:ReviewSettings}){
 const [api,setApi]=useState<CarouselApi>(),[index,setIndex]=useState(0),[pages,setPages]=useState(0);
 useEffect(()=>{if(!api)return;const update=()=>{setIndex(api.selectedScrollSnap());setPages(api.scrollSnapList().length)};update();api.on('select',update);api.on('reInit',update);return()=>{api.off('select',update);api.off('reInit',update)}},[api]);
 if(!reviews.enabled)return null;
 const items=reviews.items.filter(x=>x.visible);
 return <section id="reviews" className="reviews section customer-reviews"><div className="section-title"><h2>{reviews.title}</h2></div>
 {!items.length?<div className="reviews-empty"><MessageSquareQuote size={30} strokeWidth={1.25}/><p>Первые отзывы скоро появятся здесь.</p></div>:<Carousel opts={{align:'start',loop:true,slidesToScroll:1,skipSnaps:false,duration:32,breakpoints:{'(prefers-reduced-motion: reduce)':{duration:0}}}} setApi={setApi} className="reviews-carousel" aria-label={reviews.title} tabIndex={0}>
 <CarouselContent className="review-track">{items.map((review,i)=><CarouselItem key={review.id} className="review-slide" aria-label={(i+1)+' из '+items.length}>
 <article className="review-art-card">{review.image?<img className="review-art-image" src={review.image} alt={review.text||'Карточка покупателя'} width={720} height={1280} loading="lazy" decoding="async" draggable={false}/>:<blockquote className="review-text-card">{review.text}</blockquote>}</article>
 </CarouselItem>)}</CarouselContent>
 {pages>1&&<div className="review-navigation"><button type="button" className="review-nav-button" aria-label="Предыдущий отзыв" onClick={()=>api?.scrollPrev()}><ChevronLeft size={22} aria-hidden="true"/></button><div className="review-dots">{Array.from({length:pages},(_,i)=><button type="button" key={i} aria-label={'Отзыв '+(i+1)} aria-current={i===index?'true':undefined} className={i===index?'active':''} onClick={()=>api?.scrollTo(i)}/>)}</div><button type="button" className="review-nav-button" aria-label="Следующий отзыв" onClick={()=>api?.scrollNext()}><ChevronRight size={22} aria-hidden="true"/></button></div>}
 </Carousel>}
 </section>;
}
