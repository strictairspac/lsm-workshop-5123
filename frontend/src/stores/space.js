import{list}from'../api/space.js';
export const spacePlans={items:[],async load(){this.items=await list();return this.items}};
