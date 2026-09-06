import re
from html import unescape
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine, Engine,Text,event, inspect, or_, text
from sqlalchemy.orm import sessionmaker, declarative_base,scoped_session
from sqlalchemy import Column, Integer, String, DateTime
from typing import Optional, List
from .models import Feed, Article
from .config import cfg
from core.models.base import Base, DATA_STATUS  
from core.print import print_warning,print_info,print_error,print_success
# 声明基类
# Base = declarative_base()

ARTICLE_MATCH_TOLERANCE_SECONDS = 600


def _mp_id_prefix(mp_id: str) -> str:
    return str(mp_id or "").replace("MP_WXS_", "", 1)


def _wechat_link_identity(url: str) -> tuple[str | None, str | None]:
    """Return the stable ``mid_idx`` identity and short-link token, if present."""
    value = unescape(str(url or "").strip())
    if not value:
        return None, None

    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    mid = (query.get("mid") or query.get("appmsgid") or [None])[0]
    idx = (query.get("idx") or query.get("itemidx") or [None])[0]
    stable_id = f"{mid}_{idx}" if mid and idx else None

    token = None
    match = re.search(r"/s/([^/?#]+)", parsed.path)
    if match:
        token = match.group(1)
    return stable_id, token


def _short_token_from_id(mp_id: str, article_id: str) -> str | None:
    prefix = _mp_id_prefix(mp_id)
    value = str(article_id or "")
    for marker in (f"{prefix}-{prefix}_", f"{prefix}_", f"{prefix}-"):
        if value.startswith(marker):
            token = value[len(marker):]
            return token or None
    return None


def article_id_candidates(mp_id: str, article_id: str, url: str = "") -> tuple[str, set[str]]:
    """Build a stable ID and aliases used by historical we-mp-rss versions."""
    prefix = _mp_id_prefix(mp_id)
    raw_id = str(article_id or "").replace("MP_WXS_", "", 1)
    link_id, _ = _wechat_link_identity(url)

    if link_id:
        preferred_id = f"{prefix}-{link_id}" if prefix else link_id
    elif re.fullmatch(r"\d+_\d+", raw_id):
        preferred_id = raw_id
    elif raw_id.startswith((f"{prefix}_", f"{prefix}-")):
        preferred_id = raw_id
    else:
        preferred_id = f"{prefix}-{raw_id}" if prefix else raw_id

    aliases = {value for value in (
        preferred_id,
        link_id,
        raw_id,
        f"{prefix}-{raw_id}" if prefix and raw_id else "",
    ) if value}
    return preferred_id, aliases


def _same_article_fallback(existing: Article, incoming: Article, raw_id: str) -> bool:
    if not incoming.title or existing.title != incoming.title or existing.mp_id != incoming.mp_id:
        return False

    incoming_link_id, incoming_token = _wechat_link_identity(incoming.url)
    existing_link_id, existing_token = _wechat_link_identity(existing.url)
    incoming_token = incoming_token or _short_token_from_id(incoming.mp_id, raw_id)
    existing_token = existing_token or _short_token_from_id(existing.mp_id, existing.id)
    if incoming_link_id and existing_link_id and incoming_link_id == existing_link_id:
        return True
    if incoming_token and existing_token and incoming_token == existing_token:
        return True

    if incoming.publish_time is None or existing.publish_time is None:
        return False
    try:
        return abs(int(incoming.publish_time) - int(existing.publish_time)) <= ARTICLE_MATCH_TOLERANCE_SECONDS
    except (TypeError, ValueError):
        return False

class Db:
    connection_str: str=""
    def __init__(self,tag:str="默认",User_In_Thread=True):
        self.Session= None
        self.engine = None
        self.User_In_Thread=User_In_Thread
        self.tag=tag
        print_success(f"[{tag}]连接初始化")
        self.init(cfg.get("db","")) # type: ignore
    def get_engine(self) -> Engine:
        """Return the SQLAlchemy engine for this database connection."""
        if self.engine is None:
            raise ValueError("Database connection has not been initialized.")
        return self.engine
    def get_session_factory(self):
        return sessionmaker(bind=self.engine, autoflush=True, expire_on_commit=True, future=True)
    def init(self, con_str: str) -> None:
        """Initialize database connection and create tables"""
        try:
            self.connection_str=con_str
            # 检查SQLite数据库文件是否存在
            if con_str.startswith('sqlite:///'):
                import os
                db_path = con_str[10:]  # 去掉'sqlite:///'前缀
                if not os.path.exists(db_path):
                    try:
                        os.makedirs(os.path.dirname(db_path), exist_ok=True)
                    except Exception as e:
                        pass
                    open(db_path, 'w').close()
            
            # SQLite 连接参数
            connect_args = {}
            if con_str.startswith('sqlite:///'):
                connect_args = {"check_same_thread": False}
            
            self.engine = create_engine(con_str,
                                     pool_size=2,          # 最小空闲连接数
                                     max_overflow=20,      # 允许的最大溢出连接数
                                     pool_timeout=30,      # 获取连接时的超时时间（秒）
                                     echo=False,
                                     pool_recycle=60,  # 连接池回收时间（秒）
                                     isolation_level="AUTOCOMMIT",  # 设置隔离级别
                                    #  isolation_level="READ COMMITTED",  # 设置隔离级别
                                    #  query_cache_size=0,
                                     connect_args=connect_args
                                     )
            
            # 添加SQL执行事件监听器，打印执行的SQL语句
            @event.listens_for(self.engine, "before_cursor_execute")
            def receive_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                print_info(f"[SQL] {statement}")
                if parameters:
                    print_info(f"[参数] {parameters}")
            
            # 为 SQLite 设置 text_factory 处理无效 UTF-8 字符
            if con_str.startswith('sqlite:///'):
                @event.listens_for(self.engine, "connect")
                def set_sqlite_text_factory(dbapi_conn, connection_record):
                    # 将无效 UTF-8 字符替换为 �
                    dbapi_conn.execute("PRAGMA journal_mode=WAL")
                    dbapi_conn.execute("PRAGMA busy_timeout=10000")
                    dbapi_conn.execute("PRAGMA synchronous=NORMAL")
                    dbapi_conn.text_factory = lambda x: x.decode('utf-8', errors='replace')
            
            self.session_factory=self.get_session_factory()
            self.ensure_article_columns()
            self.ensure_feed_columns()
            self.ensure_message_task_columns()
        except Exception as e:
            print(f"Error creating database connection: {e}")
            raise
    def ensure_article_columns(self):
        """Ensure required columns exist for legacy articles tables."""
        try:
            inspector = inspect(self.engine)
            if "articles" not in inspector.get_table_names(): # type: ignore
                return

            columns = {column["name"] for column in inspector.get_columns("articles")} # type: ignore
            required_columns = {
                "extinfo": "TEXT",
                "create_time": "INTEGER",
                "publish_type": "INTEGER",
                "publish_src": "INTEGER",
                "publish_status": "TEXT",
                "art_type": "INTEGER",
                "show_type": "INTEGER",
                "publish_info": "TEXT",
                "original_check_type": "INTEGER",
                "in_profile": "INTEGER",
                "pre_publish_status": "INTEGER",
                "service_type": "INTEGER",
                "item_show_type": "INTEGER",
                "copyright_stat": "INTEGER",
                "has_red_packet_cover": "INTEGER",
                "updated_at_millis": "BIGINT",
                "is_read": "INTEGER DEFAULT 0",
                "is_favorite": "INTEGER DEFAULT 0",
                "fix_fail_count": "INTEGER DEFAULT 0",
                "content_html": "TEXT",
                "has_content": "INTEGER DEFAULT 0",
                "fetch_started_at": "BIGINT",
            }
            alter_statements = [
                f"ALTER TABLE articles ADD COLUMN {name} {definition}"
                for name, definition in required_columns.items()
                if name not in columns
            ]

            if not alter_statements:
                return

            with self.engine.begin() as conn: # type: ignore
                for stmt in alter_statements:
                    conn.execute(text(stmt))
                # Existing content is not historical backfill. Mark it as present
                # so the content repair queue does not fetch it again.
                if "content" in columns:
                    conn.execute(text(
                        "UPDATE articles SET has_content = 1 "
                        "WHERE content IS NOT NULL AND TRIM(content) != '' "
                        "AND (has_content IS NULL OR has_content = 0)"
                    ))

            print_info(f"[{self.tag}] 文章表结构已自动更新: {', '.join(alter_statements)}")
        except Exception as e:
            print_warning(f"[{self.tag}] 检查/更新 articles 表结构失败: {e}")
    def ensure_feed_columns(self):
        """Ensure adaptive scheduling columns exist on legacy feed tables."""
        try:
            inspector = inspect(self.engine)
            if "feeds" not in inspector.get_table_names(): # type: ignore
                return

            columns = {column["name"] for column in inspector.get_columns("feeds")} # type: ignore
            statements = []
            if "frequency" not in columns:
                statements.append("ALTER TABLE feeds ADD COLUMN frequency VARCHAR(20) DEFAULT 'daily'")
            if "consecutive_failures" not in columns:
                statements.append("ALTER TABLE feeds ADD COLUMN consecutive_failures INTEGER DEFAULT 0")
            if "last_success_sync_time" not in columns:
                statements.append("ALTER TABLE feeds ADD COLUMN last_success_sync_time INTEGER")

            with self.engine.begin() as conn: # type: ignore
                for stmt in statements:
                    conn.execute(text(stmt))
                conn.execute(text(
                    "UPDATE feeds SET frequency = 'daily' "
                    "WHERE frequency IS NULL OR frequency = '' OR frequency IN ('high', 'low')"
                ))
                conn.execute(text(
                    "UPDATE feeds SET consecutive_failures = 0 "
                    "WHERE consecutive_failures IS NULL"
                ))
            if statements:
                print_info(f"[{self.tag}] 订阅表结构已自动更新: {', '.join(statements)}")
        except Exception as e:
            print_warning(f"[{self.tag}] 检查/更新 feeds 表结构失败: {e}")
    def ensure_message_task_columns(self):
        """Ensure request metadata columns exist for legacy message task tables."""
        try:
            inspector = inspect(self.engine)
            if "message_tasks" not in inspector.get_table_names(): # type: ignore
                return

            columns = {column["name"] for column in inspector.get_columns("message_tasks")} # type: ignore
            statements = [
                f"ALTER TABLE message_tasks ADD COLUMN {name} TEXT"
                for name in ("headers", "cookies")
                if name not in columns
            ]
            if not statements:
                return

            with self.engine.begin() as conn: # type: ignore
                for stmt in statements:
                    conn.execute(text(stmt))
            print_info(f"[{self.tag}] 消息任务表结构已自动更新: {', '.join(statements)}")
        except Exception as e:
            print_warning(f"[{self.tag}] 检查/更新 message_tasks 表结构失败: {e}")
    def create_tables(self):
        """Create all tables defined in models"""
        from core.models.base import Base as B # 导入所有模型
        try:
            B.metadata.create_all(self.engine)
        except Exception as e:
            print_error(f"Error creating tables: {e}")

        print('All Tables Created Successfully!')    
        
    def close(self) -> None:
        """Close the database connection"""
        if self.Session:
            self.Session.close() # type: ignore
            self.Session.remove() # type: ignore
            
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    def delete_article(self,article_data:dict)->bool:
        session = None
        try:
            art = Article(**article_data)
            if art.id: # type: ignore
               art.id=f"{str(art.mp_id)}-{art.id}".replace("MP_WXS_","") # type: ignore
            session=DB.get_session()
            article = session.query(Article).filter(Article.id == art.id).first()
            if article is not None:
                session.delete(article)
                session.commit()
                return True
        except Exception as e:
            print_error(f"delete article:{str(e)}")
            pass      
        finally:
            if session is not None:
                session.close()
        return False
     
    def add_article(self, article_data: dict,check_exist=True) -> bool:
        session = None
        try:
            session=self.get_session()
            from datetime import datetime
            art = Article(**article_data)
            raw_id = str(art.id or "")
            preferred_id, id_aliases = article_id_candidates(art.mp_id, raw_id, art.url)
            art.id = preferred_id
            if check_exist:
                conditions = [Article.id.in_(id_aliases)]
                if art.url:
                    conditions.append(Article.url == art.url)
                existing_article = session.query(Article).filter(
                    Article.mp_id == art.mp_id,
                    or_(*conditions),
                ).first()
                if existing_article is None and art.title:
                    same_title_articles = session.query(Article).filter(
                        Article.mp_id == art.mp_id,
                        Article.title == art.title,
                    ).all()
                    existing_article = next((
                        item for item in same_title_articles
                        if _same_article_fallback(item, art, raw_id)
                    ), None)
                if existing_article is not None:
                    # 当更新时间和状态都相同时，不需要更新
                    if art.status == existing_article.status and existing_article.publish_time==art.publish_time \
                    and existing_article.item_show_type==art.item_show_type\
                    and existing_article.status!=DATA_STATUS.DELETED \
                    and art.title==existing_article.title \
                    and (not art.url or bool(existing_article.url)) \
                    and (not art.content or bool(existing_article.content)): # type: ignore
                        return False

                    # Preserve stable identity and existing non-empty fields. A partial
                    # crawler response must not erase the URL used as the RSS GUID.
                    for column in Article.__table__.columns:
                        name = column.name
                        if name in {"id", "mp_id", "created_at"}:
                            continue
                        incoming_value = getattr(art, name)
                        existing_value = getattr(existing_article, name)
                        if name == "url" and existing_value:
                            continue
                        if incoming_value is not None and incoming_value != "":
                            setattr(existing_article, name, incoming_value)
                    session.commit()
                    print_warning(f"Article already exists: {existing_article.id}")
                    print_info(f"Updated article (CHECK_EXIST): {existing_article.id}")
                    return False
                
            if art.created_at is None:
                art.created_at=datetime.now() # type: ignore
            if isinstance(art.created_at, str):
                art.created_at=datetime.strptime(art.created_at ,'%Y-%m-%d %H:%M:%S') # type: ignore
            # 先处理毫秒，用原始值作为fallback，再转换秒
            original_updated_at = art.updated_at
            from core.timestamp import _to_unix_millis, _to_unix_seconds
            art.updated_at_millis = _to_unix_millis(art.updated_at_millis, original_updated_at) # type: ignore
            art.updated_at = _to_unix_seconds(art.updated_at) # type: ignore
            
            # 清理编码问题，确保存储的数据是合法的UTF-8
            from tools.fix import sanitize_utf8
            art.content = sanitize_utf8(art.content) if art.content else None # type: ignore
            art.content_html = sanitize_utf8(art.content_html) if art.content_html else None # type: ignore

            if art.content is not None:
                from tools.fix import fix_html
                art.content_html = fix_html(art.content) # type: ignore

            # 设置 has_content 字段
            art.has_content = 1 if (art.content and art.content.strip()) else 0 # type: ignore

            session.add(art)
            print_info(f"Added article: {art.id}")
            sta=session.commit()
            return True
        except Exception as e:
            if session:
                session.rollback()  # 回滚事务，确保session状态正常
            if "UNIQUE" in str(e) or "Duplicate entry" in str(e):
                print_warning(f"Article already exists: {art.id}")
            else:
                print_error(f"Failed to add article: {e}")
            return False
        finally:
            if session is not None:
                session.close()
    def get_articles(self, id:str=None, limit:int=30, offset:int=0) -> List[Article]: # type: ignore
        session = None
        try:
            session = self.get_session()
            data = session.query(Article).limit(limit).offset(offset)
            return data
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e # type: ignore   
        finally:
            if session is not None:
                session.close()
             
    def get_all_mps(self) -> List[Feed]:
        """Get all Feed records"""
        session = None
        try:
            session = self.get_session()
            return session.query(Feed).filter(Feed.status == 1, Feed.faker_id != "MP_WXS_FEATURED_ARTICLES").all()
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e # type: ignore
        finally:
            if session is not None:
                session.close()
            
    def get_mps_list(self, mp_ids:str) -> List[Feed]:
        session = None
        try:
            ids=mp_ids.split(',')
            session = self.get_session()
            data = session.query(Feed).filter(Feed.id.in_(ids)).all()
            return data
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e # type: ignore
        finally:
            if session is not None:
                session.close()
    def get_mps(self, mp_id:str) -> Optional[Feed]:
        session = None
        try:
            ids=mp_id.split(',')
            session = self.get_session()
            data = session.query(Feed).filter_by(id= mp_id).first()
            return data
        except Exception as e:
            print(f"Failed to fetch Feed: {e}")
            return e # type: ignore
        finally:
            if session is not None:
                session.close()
    def get_faker_id(self, mp_id:str):
        data = self.get_mps(mp_id)
        return data.faker_id # type: ignore
    def expire_all(self):
        if self.Session:
            self.Session.expire_all()    
    def bind_event(self,session):
        # Session Events
        @event.listens_for(session, 'before_commit')
        def receive_before_commit(session):
            print("Transaction is about to be committed.")

        @event.listens_for(session, 'after_commit')
        def receive_after_commit(session):
            print("Transaction has been committed.")

        # Connection Events
        @event.listens_for(self.engine, 'connect')
        def connect(dbapi_connection, connection_record):
            print("New database connection established.")

        @event.listens_for(self.engine, 'close')
        def close(dbapi_connection, connection_record):
            print("Database connection closed.")
    def get_session(self):
        """获取新的数据库会话"""
        UseInThread=self.User_In_Thread
        def _session():
            if UseInThread:
                self.Session=scoped_session(self.session_factory)
                # self.Session=self.session_factory
            else:
                self.Session=self.session_factory
            # self.bind_event(self.Session)
            return self.Session
        
        
        if self.Session is None:
            _session()
        
        session = self.Session()  # type: ignore
        # session.expire_all()
        # session.expire_on_commit = True  # 确保每次提交后对象过期
        # 检查会话是否已经关闭
        if not session.is_active:
            from core.print import print_info
            print_info(f"[{self.tag}] Session is already closed.")
            _session()
            return self.Session() # type: ignore
        # 检查数据库连接是否已断开
        try:
            from core.models import User
            # 尝试执行一个简单的查询来检查连接状态
            session.query(User.id).count()
        except Exception as e:
            from core.print import print_warning
            print_warning(f"[{self.tag}] Database connection lost: {e}. Reconnecting...")
            self.init(self.connection_str)
            _session()
            return self.Session() # type: ignore
        return session
    def auto_refresh(self):
        # 定义一个事件监听器，在对象更新后自动刷新
        def receive_after_update(mapper, connection, target):
            print(f"Refreshing object: {target}")
        from core.models import MessageTask,Article
        event.listen(Article,'after_update', receive_after_update)
        event.listen(MessageTask,'after_update',receive_after_update)
        
    def session_dependency(self):
        """FastAPI依赖项，用于请求范围的会话管理"""
        session = self.get_session()
        try:
            yield session
        finally:
            session.remove()

# 全局数据库实例
DB = Db(User_In_Thread=True)
DB.init(cfg.get("db")) # type: ignore
