/** @typedef {{id:number,name:string,description:string,entries:Array}} Dataset */
/** @typedef {{id:number,name:string,memtable_limit:number,l0_trigger:number,max_levels:number,final_compaction:boolean}} Config */
/** @typedef {{id:number,name:string,dataset_id:number,config_id:number,status:string,result:Object}} Trial */
export const entityNames={dataset:'键值批次',config:'存储配置',trial:'合并试验'};
/** @typedef {{id:number,name:string,dataset_id:number,memtable_limit:number,capacity_bytes:number,target_bytes:number,status:string,feasible:boolean,current_bytes:number,peak_bytes:number,step_index:number,total_steps:number}} SpacePlan */
entityNames.spacePlan='空间回收方案';
