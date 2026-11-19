import{api,post}from'./client.js';
export const list=()=>api('/space-plans');
export const create=data=>post('/space-plans',data);
export const detail=id=>api('/space-plans/'+id);
export const advance=(id,expected_step)=>post('/space-plans/'+id+'/advance',{expected_step});
