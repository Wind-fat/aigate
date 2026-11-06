"""prompt 版本库：让「改了提示词」这件事可追溯、可回滚、可解释账单。

网关不渲染 prompt——渲染是业务的事。这里只解决三个具体问题：

1. 改了 prompt 却还命中旧缓存 → 请求头 `X-Prompt-Version` 现在可以从这里取当前版本，
   而缓存 key 已经包含版本，所以升级版本天然失效。
2. 账单说不清「为什么这周变贵了」→ 版本号随 usage_records 落库，能按版本聚合。
3. 回滚只能改代码重新部署 → 现在点一下把旧版本设为 active。

刻意不做的：变量渲染（`{{name}}` 原样存）、A/B 分配、审批流。它们是上层应用的事，
做成网关功能会把这一层变成半个 prompt 平台。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigate.api.deps import Actor, get_actor, get_session, require_admin, require_capability
from aigate.models import PromptTemplate

router = APIRouter(prefix="/admin/v1", tags=["control-plane"], dependencies=[Depends(require_admin)])


class PromptIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    version: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9._-]+$")
    content: str = Field(min_length=1, max_length=20000)
    notes: str = Field(default="", max_length=200)


def _dump(row: PromptTemplate, *, full: bool = True) -> dict[str, Any]:
    body = row.content or ""
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "active": bool(row.active),
        "notes": row.notes,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "content": body if full else body[:200],
        "chars": len(body),
    }


@router.get("/prompts")
async def list_prompts(
    name: str | None = Query(None),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "routes:read")
    stmt = select(PromptTemplate).order_by(PromptTemplate.name, PromptTemplate.created_at.desc())
    if name:
        stmt = stmt.where(PromptTemplate.name == name)
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [_dump(r, full=False) for r in rows], "count": len(rows)}


@router.get("/prompts/{prompt_id}")
async def get_prompt(
    prompt_id: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "routes:read")
    row = (
        await session.execute(select(PromptTemplate).where(PromptTemplate.id == prompt_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.prompt_not_found", "message": prompt_id},
        )
    return {"item": _dump(row)}


@router.post("/prompts", status_code=status.HTTP_201_CREATED)
async def create_prompt(
    payload: PromptIn,
    activate: bool = Query(False, description="创建后立即设为该名字的当前版本"),
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "routes:write")
    exists = (
        await session.execute(
            select(PromptTemplate).where(
                PromptTemplate.name == payload.name, PromptTemplate.version == payload.version
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "gateway.prompt_exists",
                "message": f"{payload.name}@{payload.version} 已存在，换个版本号",
                "existing_id": exists.id,
            },
        )

    row = PromptTemplate(
        name=payload.name,
        version=payload.version,
        content=payload.content,
        notes=payload.notes,
        active=False,
        created_by=actor.name,
    )
    session.add(row)
    await session.flush()

    if activate:
        await _activate(session, row)

    await session.commit()
    return {"item": _dump(row)}


async def _activate(session: AsyncSession, target: PromptTemplate) -> None:
    siblings = (
        await session.execute(
            select(PromptTemplate).where(
                PromptTemplate.name == target.name, PromptTemplate.id != target.id
            )
        )
    ).scalars()
    for row in siblings:
        row.active = False
    target.active = True


@router.post("/prompts/{prompt_id}/activate")
async def activate_prompt(
    prompt_id: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_capability(actor, "routes:write")
    row = (
        await session.execute(select(PromptTemplate).where(PromptTemplate.id == prompt_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.prompt_not_found", "message": prompt_id},
        )
    await _activate(session, row)
    await session.commit()
    return {
        "item": _dump(row, full=False),
        "note": "下一次带 X-Prompt-Version: {version} 的请求会用它；不带的仍按调用方自己传的版本走",
    }


@router.get("/prompts/{name}/active", include_in_schema=False)
async def active_prompt_alias(
    name: str,
    actor: Actor = Depends(get_actor),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """按名字取当前版本（业务方读这个，就不用把 prompt 硬编码在代码里）。"""
    require_capability(actor, "routes:read")
    row = (
        await session.execute(
            select(PromptTemplate).where(
                PromptTemplate.name == name, PromptTemplate.active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "gateway.prompt_not_active", "message": name},
        )
    return {"item": _dump(row)}