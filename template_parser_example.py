"""
模板引擎使用示例

基础用法:
1. 简单变量替换: {{variable}}
2. 条件判断: {% if condition %}...{% endif %}
3. 循环结构: {% for item in items %}...{% endfor %}
"""

from core.lax import TemplateParser


from core.models import Feed, Article
from core.db import DB
# 示例4: 使用Feed和Article模型数据
feed_template = """订阅源信息:
名称:{{feed.mp_name}}
描述:{{feed.mp_intro}}
最新文章:{% if articles %}
{% for article in articles %}
- {{ article.title }} ({{ article.pub_date }})
{% endfor %}
{% else %}
暂无文章
{% endif %}
"""

# 从数据库获取真实Feed和Article数据
session = DB.get_session()
try:
    from datetime import datetime
    feed = session.query(Feed).first()
    articles = session.query(Article).order_by(Article.publish_time.desc()).limit(5).all()
    context4 = {
        "feed": {
            "mp_name": feed.mp_name,
            "mp_intro": feed.mp_intro
        },
        "articles": [
            {
                "title": article.title,
                "pub_date": datetime.strftime(datetime.fromtimestamp(article.publish_time), "%Y/%m/%d %H:%M:%S")
            } for article in articles
        ]
    }

parser4 = TemplateParser(feed_template)
result4 = parser4.render({})
print("示例4结果:", result4)