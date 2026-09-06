import base64
import json
from logging import info
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query, Body, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.background import BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import or_
from core.auth import get_current_user_or_ak
from core.db import DB
from .base import success_response, error_response
from datetime import datetime
from core.config import cfg
from core.res import save_avatar_locally
from core.models.feed import FEATURED_MP_ID, FEATURED_MP_NAME, FEATURED_MP_INTRO
from core.models.base import DATA_STATUS
from core.cache import clear_cache_pattern
import io
import os
from jobs.article import UpdateArticle
from driver.wxarticle import WXArticleFetcher
from core.wx.model.weread import MpsWeread
import threading
from uuid import uuid4
router = APIRouter(prefix=f"/mps", tags=["公众号管理"])


class MpConfirmRequest(BaseModel):
    mp_name: str = Field(..., min_length=1, max_length=255)
    mp_id: str = Field(..., min_length=1, max_length=255)
    avatar: Optional[str] = Field(None, max_length=500)
    mp_cover: Optional[str] = Field(None, max_length=500)
    mp_intro: Optional[str] = Field(None, max_length=255)


class ArticleSubscriptionRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2000)


def _format_unix_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value)).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _rss_url(mp_id: str, request: Optional[Request] = None):
    base_url = str(
        cfg.get("rss.base_url", cfg.get("rss_base_url", "")) or ""
    ).rstrip("/")
    if not base_url and request is not None:
        base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/rss/{mp_id}" if base_url else f"/rss/{mp_id}"


def _serialize_mp(mp):
    from jobs.feed_frequency import normalize_frequency
    recent_update_time = mp.update_time or mp.sync_time
    return {
        "id": mp.id,
        "mp_name": mp.mp_name,
        "mp_cover": mp.mp_cover,
        "mp_intro": mp.mp_intro,
        "status": mp.status,
        "frequency": normalize_frequency(getattr(mp, "frequency", None)),
        "recent_update_time": recent_update_time,
        "recent_update_time_text": _format_unix_timestamp(recent_update_time),
        "rss_url": _rss_url(mp.id),
        "created_at": mp.created_at.isoformat() if mp.created_at else None,
        "updated_at": mp.updated_at.isoformat() if mp.updated_at else None,
    }


def _search_item_payload(item: dict):
    cover = item.get("round_head_img") or item.get("avatar") or item.get("mp_cover")
    return {
        "mp_name": item.get("nickname") or item.get("mp_name") or item.get("name"),
        "mp_id": item.get("fakeid") or item.get("mp_id") or item.get("id"),
        "avatar": cover,
        "mp_cover": cover,
        "mp_intro": item.get("signature") or item.get("mp_intro") or item.get("description"),
    }


def _task_mps_payload(feeds):
    return [
        {
            "id": feed.id,
            "mp_name": feed.mp_name,
            "mp_cover": feed.mp_cover,
            "mp_intro": feed.mp_intro,
            "status": feed.status,
            "created_at": feed.created_at.isoformat() if feed.created_at else None,
        }
        for feed in feeds
    ]


def sync_all_message_tasks(session):
    from core.models.feed import Feed
    from core.models.message_task import MessageTask
    feeds = session.query(Feed).filter(Feed.id != FEATURED_MP_ID).order_by(
        Feed.created_at.desc()
    ).all()
    payload = json.dumps(_task_mps_payload(feeds), ensure_ascii=False)
    tasks = session.query(MessageTask).all()
    changed = 0
    for task in tasks:
        if task.mps_id != payload:
            task.mps_id = payload
            changed += 1
    return changed


def _reload_scheduler_jobs():
    try:
        from jobs.mps import reload_job
        reload_job()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("重载消息任务调度失败")


def _decode_mp_id(value: str):
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        return None


def _confirm_mp_subscription(session, payload: MpConfirmRequest):
    from core.models.feed import Feed
    decoded_id = _decode_mp_id(payload.mp_id)
    if not decoded_id:
        return None, False
    now = datetime.now()
    cover = payload.avatar or payload.mp_cover
    local_cover = str(save_avatar_locally(cover)) if cover else ""
    internal_id = f"MP_WXS_{decoded_id}"
    feed = session.query(Feed).filter(
        or_(Feed.faker_id == payload.mp_id, Feed.id == internal_id)
    ).first()
    if feed:
        feed.mp_name = payload.mp_name
        if local_cover:
            feed.mp_cover = local_cover
        feed.mp_intro = payload.mp_intro
        feed.updated_at = now
        return feed, False
    feed = Feed(
        id=internal_id,
        mp_name=payload.mp_name,
        mp_cover=local_cover,
        mp_intro=payload.mp_intro,
        status=1,
        frequency="daily",
        created_at=now,
        updated_at=now,
        faker_id=payload.mp_id,
        update_time=0,
        sync_time=0,
    )
    session.add(feed)
    return feed, True


def _weread_payload(payload: MpConfirmRequest, profile: dict):
    cover = profile.get("mp_cover") or payload.avatar or payload.mp_cover
    return MpConfirmRequest(
        mp_name=profile.get("mp_name") or payload.mp_name,
        mp_id=payload.mp_id,
        avatar=cover,
        mp_cover=cover,
        mp_intro=profile.get("mp_intro") or payload.mp_intro,
    )


def _subscribe_with_weread(session, payload: MpConfirmRequest, profile=None):
    """校验微信读书资料、加入书架并创建或更新本地订阅。"""
    decoded_id = _decode_mp_id(payload.mp_id)
    if not decoded_id:
        raise ValueError("公众号唯一ID无效")
    book_id = f"MP_WXS_{decoded_id}"
    weread = MpsWeread()
    verified_profile = profile or weread.get_mp_profile(book_id)
    if verified_profile.get("book_id") != book_id:
        raise ValueError("微信读书公众号 ID 校验失败")
    feed, created = _confirm_mp_subscription(
        session,
        _weread_payload(payload, verified_profile),
    )
    shelf = weread.add_missing_to_shelf([book_id])
    changed_tasks = sync_all_message_tasks(session)
    return feed, created, shelf, changed_tasks


def _enqueue_initial_fetch(feed):
    from core.queue import TaskQueue
    from core.wx import WxGather
    max_page = int(cfg.get("max_page", "2"))
    TaskQueue.add_task(
        WxGather().Model().get_Articles,
        faker_id=feed.faker_id,
        Mps_id=feed.id,
        CallBack=UpdateArticle,
        MaxPage=max_page,
        Mps_title=feed.mp_name,
        task_name=feed.mp_name,
    )
# import core.db as db
# UPDB=db.Db("数据抓取")
# def UpdateArticle(art:dict):
#             return UPDB.add_article(art)


def build_featured_mp_item():
    now = datetime.now().isoformat()
    return {
        "id": FEATURED_MP_ID,
        "mp_name": FEATURED_MP_NAME,
        "mp_cover": "/static/logo.svg",
        "mp_intro": FEATURED_MP_INTRO,
        "status": 1,
        "created_at": now,
        "is_system": True
    }


_featured_article_tasks = {}
_featured_article_tasks_lock = threading.Lock()


def _set_featured_article_task(task_id: str, data: dict):
    with _featured_article_tasks_lock:
        _featured_article_tasks[task_id] = data


def _ensure_featured_feed(session):
    from core.models.feed import Feed

    featured_feed = session.query(Feed).filter(Feed.id == FEATURED_MP_ID).first()
    if featured_feed:
        return featured_feed

    now = datetime.now()
    featured_feed = Feed(
        id=FEATURED_MP_ID,
        mp_name=FEATURED_MP_NAME,
        mp_cover="",
        mp_intro=FEATURED_MP_INTRO,
        status=1,
        sync_time=0,
        update_time=0,
        created_at=now,
        updated_at=now,
        faker_id=FEATURED_MP_ID
    )
    session.add(featured_feed)
    return featured_feed


def _run_add_featured_article_task_wrapper(task_id: str, url: str):
    """包装器:在线程中运行 async 函数"""
    import asyncio
    asyncio.run(_run_add_featured_article_task(task_id, url))

async def _run_add_featured_article_task(task_id: str, url: str):
    session = DB.get_session()
    fetcher = None
    try:
        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": url,
            "status": "running",
            "message": "任务执行中"
        })

        from core.models.article import Article

        target_url = str(url or "").strip()
        if not target_url:
            raise ValueError("请输入文章链接")

        fetcher = WXArticleFetcher()
        info = await fetcher.get_article_content(target_url)
        if not info or info.get("fetch_error"):
            raise ValueError(info.get("fetch_error") or "文章抓取失败，请检查链接或登录状态")

        if info.get("content") == "DELETED":
            raise ValueError("该文章暂不可访问或已删除")

        raw_article_id = info.get("id") or fetcher.extract_id_from_url(target_url)
        if not raw_article_id:
            raise ValueError("无法解析文章ID，请确认链接格式")

        _ensure_featured_feed(session)

        article_id = f"{FEATURED_MP_ID}-{raw_article_id}".replace("MP_WXS_", "")
        now = datetime.now()
        publish_time = info.get("publish_time")
        if not isinstance(publish_time, int):
            try:
                publish_time = int(publish_time)
            except Exception:
                publish_time = int(now.timestamp())

        article_data = {
            "title": info.get("title") or target_url,
            "description": info.get("description") or fetcher.get_description(info.get("content") or ""),
            "content": info.get("content") or "",
            "publish_time": publish_time,
            "url": target_url,
            "pic_url": info.get("topic_image") or info.get("pic_url") or "",
        }

        existing = session.query(Article).filter(Article.id == article_id).first()
        if existing:
            existing.mp_id = FEATURED_MP_ID
            existing.title = article_data["title"]
            existing.description = article_data["description"]
            existing.content = article_data["content"]
            existing.publish_time = article_data["publish_time"]
            existing.url = article_data["url"]
            existing.pic_url = article_data["pic_url"]
            existing.status = DATA_STATUS.ACTIVE
            existing.updated_at = int(now.timestamp())
            existing.updated_at_millis = int(now.timestamp() * 1000)
            created = False
        else:
            session.add(Article(
                id=article_id,
                mp_id=FEATURED_MP_ID,
                title=article_data["title"],
                description=article_data["description"],
                content=article_data["content"],
                publish_time=article_data["publish_time"],
                url=article_data["url"],
                pic_url=article_data["pic_url"],
                status=DATA_STATUS.ACTIVE,
                created_at=now,
                updated_at=int(now.timestamp()),
                updated_at_millis=int(now.timestamp() * 1000),
                is_read=0,
                is_favorite=0
            ))
            created = True

        session.commit()
        clear_cache_pattern("articles_list")
        clear_cache_pattern("article_detail")
        clear_cache_pattern("home_page")
        clear_cache_pattern("tag_detail")

        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": target_url,
            "status": "success",
            "message": "精选文章添加成功" if created else "精选文章更新成功",
            "id": article_id,
            "mp_id": FEATURED_MP_ID,
            "mp_name": FEATURED_MP_NAME,
            "title": article_data["title"],
            "created": created
        })
    except Exception as e:
        session.rollback()
        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": url,
            "status": "failed",
            "message": str(e)
        })
    finally:
        if fetcher is not None:
            try:
                await fetcher.Close()
            except Exception:
                pass
        session.close()


@router.get("/search/{kw}", summary="搜索公众号")
async def search_mp(
    kw: str = "",
    limit: int = 10,
    offset: int = 0,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        query = session.query(Feed).filter(Feed.id != FEATURED_MP_ID)
        if kw:
            query = query.filter(Feed.mp_name.ilike(f"%{kw}%"))
        total = query.count()
        feeds = query.order_by(Feed.created_at.desc()).limit(limit).offset(offset).all()
        items = [{
            "nickname": feed.mp_name,
            "fakeid": feed.faker_id,
            "round_head_img": feed.mp_cover,
            "signature": feed.mp_intro,
        } for feed in feeds]
        data={
            'list': [
                {**item, "subscribe_payload": _search_item_payload(item)}
                for item in (items or [])
            ],
            'page':{
                'limit':limit,
                'offset':offset
            },
            'total':total
        }
        return success_response(data)
    except Exception as e:
        print(f"搜索公众号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="搜索项目内公众号失败",
            )
        )
    finally:
        session.close()

@router.get("", summary="获取公众号列表")
async def get_mps(
    limit: int = Query(1000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    kw: str = Query(""),
    status: int = Query(None, description="状态筛选: 1=启用, 0=停用, 不传=全部"),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        query = session.query(Feed).filter(Feed.id != FEATURED_MP_ID)
        if kw:
            query = query.filter(Feed.mp_name.ilike(f"%{kw}%"))
        if status is not None:
            query = query.filter(Feed.status == status)
        total = query.count()
        mps = query.order_by(Feed.created_at.desc()).limit(limit).offset(offset).all()
        mps_list = [_serialize_mp(mp) for mp in mps]
        return success_response({
            "list": mps_list,
            "page": {
                "limit": limit,
                "offset": offset,
                "total": total
            },
            "total": total
        })
    except Exception as e:
        print(f"获取公众号列表错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="获取公众号列表失败"
            )
        )


@router.post("/confirm", summary="确认添加公众号订阅")
async def confirm_mp_subscription(
    payload: MpConfirmRequest = Body(...),
    current_user: dict = Depends(get_current_user_or_ak),
):
    session = DB.get_session()
    try:
        feed, created, shelf, changed_tasks = _subscribe_with_weread(
            session, payload
        )
        session.commit()
        session.refresh(feed)
        result = {
            **_serialize_mp(feed),
            "created": created,
            "shelf": shelf,
            "message_task_updated": changed_tasks,
        }
        _reload_scheduler_jobs()
        if created:
            _enqueue_initial_fetch(feed)
        return success_response(result)
    except Exception:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(code=50001, message="确认添加公众号订阅失败"),
        )
    finally:
        session.close()


@router.post("/featured/article", summary="添加精选文章")
async def add_featured_article(
    url: str = Body(..., embed=True, min_length=1),
    current_user: dict = Depends(get_current_user_or_ak)
):
    try:
        target_url = str(url or "").strip()
        if not target_url:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40001,
                    message="请输入文章链接"
                )
            )
        if "mp.weixin.qq.com/s/" not in target_url:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40002,
                    message="请输入有效的公众号文章链接"
                )
            )

        task_id = str(uuid4())
        _set_featured_article_task(task_id, {
            "task_id": task_id,
            "url": target_url,
            "status": "pending",
            "message": "任务已创建"
        })
        threading.Thread(
            target=_run_add_featured_article_task_wrapper,
            args=(task_id, target_url),
            daemon=True
        ).start()

        return success_response({
            "task_id": task_id,
            "url": target_url,
            "status": "pending"
        }, message="已开始添加/抓取，请稍后刷新查看结果")
    except HTTPException:
        raise
    except Exception as e:
        print(f"添加精选文章任务启动失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="添加精选文章失败"
            )
        )


@router.get("/featured/article/tasks/{task_id}", summary="查询精选文章添加任务状态")
async def get_featured_article_task_status(
    task_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    with _featured_article_tasks_lock:
        task = _featured_article_tasks.get(task_id)
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(
                code=40404,
                message="任务不存在"
            )
        )
    return success_response(task)

@router.get("/update/{mp_id}", summary="更新公众号文章")
async def update_mps(
     mp_id: str,
     start_page: int = 0,
     end_page: int = 1,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
           return error_response(
                    code=40401,
                    message="请选择一个公众号"
                )
        import time
        sync_interval=cfg.get("sync_interval",60)
        if mp.update_time is None:
            mp.update_time=int(time.time())-sync_interval
        time_span=int(time.time())-int(mp.update_time)
        if time_span<sync_interval:
           return error_response(
                    code=40402,
                    message="请不要频繁更新操作",
                    data={"time_span":time_span}
                )
        result=[]    
        def UpArt(mp):
            from core.wx import WxGather
            wx=WxGather().Model()
            wx.get_Articles(mp.faker_id,Mps_id=mp.id,Mps_title=mp.mp_name,CallBack=UpdateArticle,start_page=start_page,MaxPage=end_page)
            result=wx.articles
        import threading
        threading.Thread(target=UpArt,args=(mp,)).start()
        return success_response({
            "time_span":time_span,
            "list":result,
            "total":len(result),
            "mps":mp
        })
    except Exception as e:
        print(f"更新公众号文章: {str(e)}",e)
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message=f"更新公众号文章{str(e)}"
            )
        )

@router.get("/{mp_id}", summary="获取公众号详情")
async def get_mp(
    mp_id: str,
    # current_user: dict = Depends(get_current_user)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="公众号不存在"
                )
            )
        return success_response(mp)
    except Exception as e:
        print(f"获取公众号详情错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="获取公众号详情失败"
            )
        )
@router.post("/by_article", summary="通过文章链接获取公众号详情")
async def get_mp_by_article(
    url: str=Query(..., min_length=1),
    current_user: dict = Depends(get_current_user_or_ak)
):
    try:
        info = await WXArticleFetcher().get_article_content(url)
        
        if not info:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="公众号不存在"
                )
            )
        mp_id = info.get("mp_id")
        biz = (info.get("mp_info") or {}).get("biz")
        if not mp_id or not biz:
            raise ValueError("文章页面未返回公众号唯一 ID")
        profile = MpsWeread().get_mp_profile(mp_id)
        info["mp_info"] = {
            "mp_name": profile["mp_name"],
            "logo": profile.get("mp_cover") or (info.get("mp_info") or {}).get("logo", ""),
            "biz": biz,
        }
        return success_response(info)
    except Exception as e:
        print(f"获取公众号详情错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="请输入正确的公众号文章链接"
            )
        )


@router.post(
    "/by_article/subscribe",
    summary="通过公众号文章链接新增订阅",
)
async def subscribe_mp_by_article(
    request: Request,
    payload: ArticleSubscriptionRequest = Body(...),
    current_user: dict = Depends(get_current_user_or_ak),
):
    session = DB.get_session()
    try:
        identity = await WXArticleFetcher().get_mp_identity(payload.url)
        mp_id = identity.get("mp_id")
        mp_info = identity.get("mp_info") or {}
        biz = mp_info.get("biz")
        if not mp_id or not biz:
            raise ValueError("无法识别文章所属公众号")
        profile = MpsWeread().get_mp_profile(mp_id)
        feed, created, shelf, changed_tasks = _subscribe_with_weread(
            session,
            MpConfirmRequest(
                mp_name=profile["mp_name"],
                mp_id=biz,
                avatar=profile.get("mp_cover") or mp_info.get("logo"),
                mp_intro=profile.get("mp_intro"),
            ),
            profile=profile,
        )
        session.commit()
        session.refresh(feed)
        _reload_scheduler_jobs()
        if created:
            _enqueue_initial_fetch(feed)
        result = {
            **_serialize_mp(feed),
            "rss_url": _rss_url(feed.id, request),
            "created": created,
            "shelf_added": bool(shelf.get("added_count")),
            "initial_fetch_queued": created,
            "message_task_updated": changed_tasks,
        }
        return success_response(
            result,
            message="订阅添加成功" if created else "订阅已存在",
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(code=40001, message=str(exc)),
        ) from exc
    except Exception as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(code=40002, message=str(exc)),
        ) from exc
    finally:
        session.close()

@router.post("", summary="添加公众号")
async def add_mp(
    mp_name: str = Body(..., min_length=1, max_length=255),
    mp_cover: str = Body(None, max_length=255),
    mp_id: str = Body(None, max_length=255),
    avatar: str = Body(None, max_length=500),
    mp_intro: str = Body(None, max_length=255),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        payload = MpConfirmRequest(
            mp_name=mp_name,
            mp_id=mp_id,
            avatar=avatar,
            mp_cover=mp_cover,
            mp_intro=mp_intro,
        )
        feed, created, shelf, changed_tasks = _subscribe_with_weread(
            session, payload
        )
        session.commit()
        session.refresh(feed)
        _reload_scheduler_jobs()
        if created:
            _enqueue_initial_fetch(feed)
        return success_response({
            **_serialize_mp(feed),
            "faker_id": mp_id,
            "created": created,
            "shelf": shelf,
            "message_task_updated": changed_tasks,
        })
    except Exception as e:
        session.rollback()
        print(f"添加公众号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="添加公众号失败"
            )
        )


@router.delete("/{mp_id}", summary="删除订阅号")
async def delete_mp(
    mp_id: str,
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="订阅号不存在"
                )
            )
        
        from core.models.article import Article
        session.query(Article).filter(Article.mp_id == mp_id).delete()
        session.delete(mp)
        changed_tasks = sync_all_message_tasks(session)
        session.commit()
        _reload_scheduler_jobs()
        from core.rss import RSS
        RSS(mp_id).clear_cache(mp_id)
        return success_response({
            "message": "订阅号删除成功",
            "id": mp_id,
            "message_task_updated": changed_tasks,
        })
    except Exception as e:
        session.rollback()
        print(f"删除订阅号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="删除订阅号失败"
            )
        )

@router.put("/{mp_id}", summary="更新订阅号状态")
async def update_mp_status(
    mp_id: str,
    mp_name: str = Body(None),
    mp_cover: str = Body(None),
    mp_intro: str = Body(None),
    status: int = Body(None),
    current_user: dict = Depends(get_current_user_or_ak)
):
    session = DB.get_session()
    try:
        from core.models.feed import Feed
        mp = session.query(Feed).filter(Feed.id == mp_id).first()
        if not mp:
            raise HTTPException(
                status_code=status.HTTP_201_CREATED,
                detail=error_response(
                    code=40401,
                    message="订阅号不存在"
                )
            )
        
        if mp_name is not None:
            mp.mp_name = mp_name
        if mp_cover is not None:
            mp.mp_cover = mp_cover
        if mp_intro is not None:
            mp.mp_intro = mp_intro
        if status is not None:
            mp.status = status
        
        mp.updated_at = datetime.now()
        session.commit()
        
        return success_response({
            "message": "更新成功",
            "id": mp_id,
            "status": mp.status
        })
    except Exception as e:
        session.rollback()
        print(f"更新订阅号错误: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_201_CREATED,
            detail=error_response(
                code=50001,
                message="更新订阅号失败"
            )
        )
