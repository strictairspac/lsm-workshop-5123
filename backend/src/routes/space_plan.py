from fastapi import APIRouter,Depends
from ..config.database import get_db
from ..schemas.space_plan import SpacePlanInput,AdvanceInput
from ..services.auth import current_user,writer
from ..services.space_plan import listing,create,detail,advance
router=APIRouter(prefix='/space-plans')
@router.get('')
def get(db=Depends(get_db),user=Depends(current_user)):return listing(db)
@router.post('',status_code=201)
def add(data:SpacePlanInput,db=Depends(get_db),user=Depends(writer)):return create(db,data)
@router.get('/{ident}')
def one(ident:int,db=Depends(get_db),user=Depends(current_user)):return detail(db,ident)
@router.post('/{ident}/advance')
def step(ident:int,data:AdvanceInput,db=Depends(get_db),user=Depends(writer)):return advance(db,ident,data.expected_step)
