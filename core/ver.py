import requests
from core.log import logger
try:
    response = requests.get('https://api.github.com/repos/rachelos/we-mp-rss/releases/latest', timeout=2)
    response.raise_for_status()  # 检查请求是否成功
    data = response.json()
    LATEST_VERSION = data.get('tag_name', '').replace('v', '')
except Exception as e:
    print(f"Failed to fetch latest version: {e}")
    logger.exception("Failed to fetch latest version from GitHub")
    LATEST_VERSION = ''

#API接口前缀
API_BASE = "/api/v1/wx"
#当前程序版本
VERSION = '1.3.9'

#工作目录
WORK_DIR="./work"

#静态文件目录
STATIC_DIR="./static"