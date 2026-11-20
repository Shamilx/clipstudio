"""Clip style template CRUD."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import templates as tpl

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _out(t: tpl.Template) -> dict[str, Any]:
    d = t.to_dict()
    d["builtin"] = tpl.is_builtin(t.name)
    d["user"] = tpl.is_user(t.name)
    return d


@router.get("")
async def list_templates() -> list[dict[str, Any]]:
    return await asyncio.to_thread(lambda: [_out(t) for t in tpl.list_templates()])


@router.get("/{name}")
async def get_template(name: str) -> dict[str, Any]:
    key = tpl.safe_name(name)
    if not any(t.name == key for t in tpl.list_templates()):
        raise HTTPException(status_code=404, detail="Template not found.")
    return _out(tpl.load_template(key))


@router.put("/{name}")
async def put_template(name: str, body: dict[str, Any]) -> dict[str, Any]:
    key = tpl.safe_name(name)
    if not key:
        raise HTTPException(status_code=400, detail="Invalid template name.")
    saved = await asyncio.to_thread(tpl.save_template, tpl.validate({**body, "name": key}, key))
    return _out(saved)


@router.delete("/{name}", status_code=204)
async def delete_template(name: str) -> None:
    if not tpl.is_user(name):
        raise HTTPException(
            status_code=404 if not tpl.is_builtin(name) else 400,
            detail="Built-in templates cannot be deleted."
            if tpl.is_builtin(name)
            else "User template not found.",
        )
    await asyncio.to_thread(tpl.delete_template, name)
