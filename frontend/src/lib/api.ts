const json=async<T>(url:string,options?:RequestInit):Promise<T>=>{const res=await fetch(url,{...options,headers:{'Content-Type':'application/json',...(options?.headers||{})}});if(!res.ok){const body=await res.text();let message=body||`HTTP ${res.status}`;try{const parsed=JSON.parse(body);if(typeof parsed.detail==='string')message=parsed.detail}catch{}throw new Error(message)}if(res.status===204)return undefined as T;return res.json()}
export const api={
  get:<T>(url:string)=>json<T>(url),
  post:<T>(url:string,data?:unknown)=>json<T>(url,{method:'POST',body:data===undefined?undefined:JSON.stringify(data)}),
  put:<T>(url:string,data:unknown)=>json<T>(url,{method:'PUT',body:JSON.stringify(data)}),
  delete:(url:string)=>json<void>(url,{method:'DELETE'})
}
