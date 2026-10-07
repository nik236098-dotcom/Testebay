'use client';
import {useCallback,useEffect,useRef,useState,type SetStateAction} from 'react';

type Cart=Record<string,number>;
const storageKey='aromaty-cart';
function readCart():Cart{
 try{const value=JSON.parse(localStorage.getItem(storageKey)||'{}');
  if(value&&typeof value==='object'&&!Array.isArray(value))return Object.fromEntries(Object.entries(value).filter(([,n])=>Number.isInteger(n)&&Number(n)>0&&Number(n)<=99)) as Cart;
 }catch{}
 return {};
}
export function useCart(){
 const [cart,updateCart]=useState<Cart>({}),[ready,setReady]=useState(false),current=useRef<Cart>({});
 useEffect(()=>{
  const reload=()=>{current.current=readCart();updateCart(current.current);setReady(true)};
  const onStorage=(event:StorageEvent)=>{if(event.key===storageKey||event.key===null)reload()};
  reload();window.addEventListener('pageshow',reload);window.addEventListener('storage',onStorage);
  return()=>{window.removeEventListener('pageshow',reload);window.removeEventListener('storage',onStorage)};
 },[]);
 const setCart=useCallback((action:SetStateAction<Cart>)=>{
  const next=typeof action==='function'?action(current.current):action;
  current.current=next;updateCart(next);
  try{localStorage.setItem(storageKey,JSON.stringify(next))}catch{}
 },[]);
 return {cart,setCart,ready};
}
