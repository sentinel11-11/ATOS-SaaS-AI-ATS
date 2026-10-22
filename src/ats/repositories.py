from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ats.domain import Problem


class TenantRepository:
    """Explicit tenant scoping. Never take the tenant from a request body."""

    def __init__(self, db: Session, tenant_id: UUID):
        self.db, self.tenant_id = db, tenant_id

    def query(self, model):
        return select(model).where(model.tenant_id == self.tenant_id)

    def get(self, model, entity_id, *, lock=False):
        query = self.query(model).where(model.id == entity_id)
        if lock:
            query = query.with_for_update()
        result = self.db.scalar(query)
        if result is None:
            raise Problem(404, "not_found")
        return result

    def add(self, model, **values):
        result = model(tenant_id=self.tenant_id, **values)
        self.db.add(result)
        return result
