from datetime import datetime
from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,JSON,ForeignKey,DateTime,BigInteger,func
from ..config.database import Base
class SpacePlan(Base):
    __tablename__='space_plans'
    id:Mapped[int]=mapped_column(primary_key=True)
    name:Mapped[str]=mapped_column(String(120),unique=True)
    dataset_id:Mapped[int]=mapped_column(ForeignKey('datasets.id'))
    memtable_limit:Mapped[int]=mapped_column(BigInteger)
    capacity_bytes:Mapped[int]=mapped_column(BigInteger)
    target_bytes:Mapped[int]=mapped_column(BigInteger)
    input_snapshot:Mapped[dict]=mapped_column(JSON)
    plan:Mapped[dict]=mapped_column(JSON)
    state:Mapped[dict]=mapped_column(JSON)
    created_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now())
    updated_at:Mapped[datetime]=mapped_column(DateTime,server_default=func.now(),onupdate=func.now())
