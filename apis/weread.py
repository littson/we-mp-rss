import re
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from apis.base import error_response, success_response
from core.auth import get_current_user_or_ak
from core.db import DB
from core.models.feed import FEATURED_MP_ID, Feed
from core.wx.model.weread import MpsWeread
from driver.weread import WEREAD_LOGIN


router = APIRouter(prefix="/weread", tags=["微信读书"])


class WereadShelfAddRequest(BaseModel):
    book_ids: List[str] = Field(..., min_length=1, max_length=100)


class WereadSubscriptionRequest(BaseModel):
    mp_name: str = Field(..., min_length=1, max_length=255)
    mp_id: str = Field(..., min_length=1, max_length=255)
    avatar: Optional[str] = Field(None, max_length=500)
    mp_cover: Optional[str] = Field(None, max_length=500)
    mp_intro: Optional[str] = Field(None, max_length=255)


@router.get("/auth/qr/code", summary="获取微信读书登录二维码")
async def weread_qr_code(
    force: bool = False,
    current_user: dict = Depends(get_current_user_or_ak),
):
    result = await WEREAD_LOGIN.start_login(force=force)
    if result["state"] == "failed":
        return error_response(code=50001, message=result["error"] or "二维码生成失败")
    return success_response(result)


@router.get("/auth/qr/status", summary="获取微信读书扫码状态")
async def weread_qr_status(
    current_user: dict = Depends(get_current_user_or_ak),
):
    return success_response(WEREAD_LOGIN.status())


@router.post("/auth/unbind", summary="解除微信读书授权")
async def weread_unbind(
    current_user: dict = Depends(get_current_user_or_ak),
):
    return success_response(await WEREAD_LOGIN.unbind())


@router.post("/shelf/add", summary="添加内容到微信读书书架")
async def weread_shelf_add(
    payload: WereadShelfAddRequest = Body(...),
    current_user: dict = Depends(get_current_user_or_ak),
):
    try:
        result = MpsWeread().add_missing_to_shelf(payload.book_ids)
        return success_response(result)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(code=40001, message=str(exc)),
        ) from exc


@router.post("/shelf/add-all", summary="将全部现有公众号加入微信读书书架")
async def weread_shelf_add_all(
    current_user: dict = Depends(get_current_user_or_ak),
):
    session = DB.get_session()
    try:
        all_ids = [
            row[0]
            for row in session.query(Feed.id).filter(
                Feed.id != FEATURED_MP_ID
            ).all()
            if row[0]
        ]
        book_ids = [
            item for item in dict.fromkeys(all_ids)
            if re.fullmatch(r"MP_WXS_[A-Za-z0-9_-]+", item)
        ]
        skipped_count = len(set(all_ids)) - len(book_ids)
        if not book_ids:
            return success_response({
                "succ": 1,
                "book_ids": [],
                "added_book_ids": [],
                "existing_book_ids": [],
                "added_count": 0,
                "existing_count": 0,
                "skipped_count": skipped_count,
                "total": 0,
            })
        result = MpsWeread().add_missing_to_shelf(book_ids)
        result["skipped_count"] = skipped_count
        return success_response(result)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(code=40003, message=str(exc)),
        ) from exc
    finally:
        session.close()


@router.post("/subscriptions", summary="添加微信读书公众号订阅")
async def add_weread_subscription(
    payload: WereadSubscriptionRequest = Body(...),
    current_user: dict = Depends(get_current_user_or_ak),
):
    from apis.mps import (
        MpConfirmRequest,
        _enqueue_initial_fetch,
        _reload_scheduler_jobs,
        _serialize_mp,
        _subscribe_with_weread,
    )

    session = DB.get_session()
    try:
        feed, created, shelf_result, changed_tasks = _subscribe_with_weread(
            session,
            MpConfirmRequest(**payload.model_dump()),
        )
        session.commit()
        session.refresh(feed)
        _reload_scheduler_jobs()
        if created:
            _enqueue_initial_fetch(feed)
        return success_response({
            **_serialize_mp(feed),
            "created": created,
            "shelf": shelf_result,
            "message_task_updated": changed_tasks,
        })
    except Exception as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(code=40002, message=str(exc)),
        ) from exc
    finally:
        session.close()
