import  requests
from langchain_core.tools import tool
from app.core.config import get_settings

@tool
def ds_web_search(query: str, ori_question:str, max_results: int = 10,  localization: str = "",need_content: bool = False) -> str:
    """deepseek联网搜索，返回若干条结果。

    当问题涉及使用deepseek进行查询最新信息、实时数据、版本变化、官方文档等训练数据里没有或可能过时的内容时使用。
    需要读某个结果的完整内容时，用 fetch_web_page 打开它的链接。

    Args:
        query: 搜索词，1～128 个 Unicode 字符，不能全为空白，支持下方列出的搜索语法。
        count: 返回条数，1～50，默认值为10。
        ori_question: 可选，触发搜索的用户原始问题，1～128 个字符，有助于更好地理解搜索意图，传 null 视为未提供,默认为null。
        localization: 可选，用于优先返回本地化搜索结果的 IP 地址，默认为""。
        need_content: 可选，是否在 content 中返回网页正文，为 false 时 content 恒为空字符串，默认为false。
    """
    settings = get_settings()
    response = requests.post(
        "https://api.deepseek.com/v1/tools/search",
        headers={
            "Authorization": "Bearer "+settings.api_key_value,
        },
        json={
            "query": query,
            "count": max_results,
            "ori_question": ori_question,
            "localization": localization,
            "need_content": need_content
        },
    )
    response.raise_for_status()
    return response.json()
