from pydantic import BaseModel,Field,model_validator
class SpacePlanInput(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    dataset_id:int=Field(gt=0)
    memtable_limit:int=Field(ge=2,le=64)
    capacity_bytes:int=Field(ge=1,le=2_000_000_000)
    target_bytes:int=Field(ge=0,le=2_000_000_000)
    @model_validator(mode='after')
    def check_target(self):
        if self.target_bytes>=self.capacity_bytes:raise ValueError('收缩目标必须小于容量上限')
        return self
class AdvanceInput(BaseModel):
    expected_step:int=Field(ge=0)
