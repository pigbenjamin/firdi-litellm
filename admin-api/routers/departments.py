from fastapi import APIRouter, Depends

from auth import verify_admin_key
from models import DepartmentIn, DepartmentOut, DepartmentPatch
from services import departments_service

router = APIRouter(prefix="/api/v1/departments", dependencies=[Depends(verify_admin_key)])


@router.get("", response_model=list[DepartmentOut])
def list_departments():
    return departments_service.list_departments()


@router.post("", response_model=DepartmentOut, status_code=201)
def create_department(body: DepartmentIn):
    return departments_service.create_department(body)


@router.get("/{dept_id}", response_model=DepartmentOut)
def get_department(dept_id: str):
    return departments_service.get_department(dept_id)


@router.put("/{dept_id}", response_model=DepartmentOut)
def update_department(dept_id: str, body: DepartmentIn):
    return departments_service.update_department(dept_id, body)


@router.patch("/{dept_id}", response_model=DepartmentOut)
def patch_department(dept_id: str, body: DepartmentPatch):
    return departments_service.patch_department(dept_id, body)


@router.delete("/{dept_id}", status_code=204)
def delete_department(dept_id: str):
    departments_service.delete_department(dept_id)
